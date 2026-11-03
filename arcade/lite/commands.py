"""Every Lite action, as a command: one list for the UI, the console, the terminal and the help.

A command is a function of the caller and its arguments that returns something JSON can carry. Its docstring is its
help: the first line is the summary, the rest the detail. Each command says who may run it: anyone who reaches the
arcade, a signed-in user (on their own account only), or the operator. `help` lists only what the caller may run.

Each command also belongs to a module (arcade/modules.py). A module that is off takes its commands with it, and the
operator switches modules with `install` and `remove`, as packages are on a Linux machine. `<command> -help` explains
any command.
"""
from __future__ import annotations

import inspect
import json
import shlex
from dataclasses import dataclass
from typing import Any, Callable

from .. import modules as modulelib

#: Who may run a command, least to most.
AUDIENCES = ("anyone", "user", "operator")


class CommandError(Exception):
    """A command could not run: an unknown name, a wrong argument, or the node could not answer."""


@dataclass(frozen=True)
class Caller:
    """Who is asking: the node's state, their audience, and their account's public key when signed in.

    `address(chain)` and `name_of(address)` are the web app's own account lookups. `node` is the node's wallet as
    Admin drives it (the operator's only), and `local` says the operator is at the node's own machine. `reads` is what
    anyone may ask of the node: what its public pages show."""
    state: Any
    audience: str = "anyone"
    account: str | None = None
    address: Callable[[Any], str] = lambda chain: ""
    name_of: Callable[[str], str] = lambda address: ""
    node: Any = None
    local: bool = False
    reads: Any = None

    def may(self, audience: str) -> bool:
        return AUDIENCES.index(self.audience) >= AUDIENCES.index(audience)


@dataclass(frozen=True)
class Command:
    name: str
    category: str
    audience: str
    fn: Callable[..., Any]
    module: str = "node"

    @property
    def summary(self) -> str:
        return (inspect.getdoc(self.fn) or "").split("\n", 1)[0]

    @property
    def usage(self) -> str:
        params = list(inspect.signature(self.fn).parameters.values())[1:]   # the first is the caller
        words = [f"{p.name}..." if p.kind is p.VAR_POSITIONAL else p.name if p.default is inspect.Parameter.empty
                 else f"[{p.name}]" for p in params]
        return " ".join([self.name, *words])


COMMANDS: dict[str, Command] = {}


def command(category: str, audience: str = "anyone", module: str = "node"):
    assert audience in AUDIENCES, audience
    assert module in modulelib.MODULES, module

    def register(fn):
        COMMANDS[fn.__name__] = Command(fn.__name__, category, audience, fn, module)
        return fn
    return register


#: Asked for a command's help in the way a terminal command is: `send -help`.
HELP_FLAGS = ("-help", "--help", "-h")


def _installed(caller: Caller, cmd: Command) -> bool:
    try:
        return cmd.module in modulelib.enabled(caller.state.settings())
    except Exception:                                      # noqa: BLE001 -- a state with no settings runs the core
        return modulelib.MODULES[cmd.module].core


def run(caller: Caller, name: str, *args: Any) -> Any:
    cmd = COMMANDS.get(name)
    if cmd is None or not caller.may(cmd.audience):
        # One answer for both, so a visitor cannot list the operator's commands by trying names.
        raise CommandError(f"no command {name!r}; `help` lists them")
    if not _installed(caller, cmd):
        hint = f"; `install {cmd.module}` switches it on" if caller.may("operator") else ""
        raise CommandError(f"{name}: the {modulelib.MODULES[cmd.module].title} module is off on this arcade{hint}")
    try:
        inspect.signature(cmd.fn).bind(caller, *args)
    except TypeError:
        raise CommandError(f"usage: {cmd.usage}") from None
    return cmd.fn(caller, *args)


def available(caller: Caller) -> list[Command]:
    return sorted((c for c in COMMANDS.values() if caller.may(c.audience) and _installed(caller, c)),
                  key=lambda c: (c.category, c.name))


def run_line(caller: Caller, line: str) -> Any:
    """One console line, `name arg ...` as in a Core's console. A whole number, true, false, null, a list or an
    object is taken as JSON; everything else, an amount such as 0.00000001 included, as the text typed. A command
    that takes the rest of the line (sendmessage's text) gets it as written, apostrophes and all."""
    rest = _rest_of_line(line)
    if rest is not None:
        name, fixed, tail = rest
        return run(caller, name, *(_value(w) for w in fixed), *([tail] if tail else []))
    try:
        words = shlex.split(line)
    except ValueError as exc:
        raise CommandError(str(exc)) from None
    if not words:
        raise CommandError("type a command; `help` lists them")
    if words[0] in HELP_FLAGS:
        words = ["help", *words[1:]]
    elif any(w in HELP_FLAGS for w in words[1:]):
        return run(caller, "help", words[0])
    return run(caller, words[0], *(_value(w) for w in words[1:]))


def _rest_of_line(line: str) -> tuple[str, list[str], str] | None:
    """(name, its fixed arguments, the rest of the line) for a command whose last argument is the rest of the line;
    None for any other line. The rest is unquoted as a shell would (arcade-cli quotes what it sends) when it can be,
    and kept as typed when it cannot (an apostrophe)."""
    parts = line.split(None, 1)
    cmd = COMMANDS.get(parts[0]) if parts else None
    if cmd is None:
        return None
    params = list(inspect.signature(cmd.fn).parameters.values())[1:]
    if not params or params[-1].kind is not inspect.Parameter.VAR_POSITIONAL:
        return None
    fixed = len(params) - 1
    pieces = line.strip().split(None, fixed + 1)
    if any(w in HELP_FLAGS for w in pieces[1:2]) or (len(pieces) == fixed + 2 and pieces[-1].strip() in HELP_FLAGS):
        return None                                        # `sendmessage -help`: the help, by the usual path
    tail = pieces[fixed + 1] if len(pieces) > fixed + 1 else ""
    try:
        tail = " ".join(shlex.split(tail))
    except ValueError:
        pass
    words = []
    for w in pieces[1:fixed + 1]:
        try:
            words += shlex.split(w)
        except ValueError:
            words.append(w)
    return pieces[0], words, tail


def _value(word: str) -> Any:
    if not (word.lstrip("-").isdigit() or word in ("true", "false", "null") or word[:1] in "[{"):
        return word
    try:
        return json.loads(word)
    except ValueError:
        return word


# --- help ---------------------------------------------------------------------------------------------------------


@command("help")
def help(caller, name: str = "") -> str:
    """List every command, or explain one.

    help            every command, by category
    help <command>  what it does and what it takes"""
    if name:
        cmd = COMMANDS.get(name)
        if cmd is None or not caller.may(cmd.audience):
            raise CommandError(f"no command {name!r}")
        return f"{cmd.usage}\n\n{inspect.getdoc(cmd.fn) or ''}"
    out, last = [], None
    for cmd in available(caller):
        if cmd.category != last:
            out.append(f"== {cmd.category.capitalize()} ==")
            last = cmd.category
        out.append(cmd.usage)
    return "\n".join(out)


# --- the node -----------------------------------------------------------------------------------------------------


@command("node")
def getnodeinfo(caller) -> dict:
    """This arcade: its version, face, operator and the names it is published under."""
    from ..web import brand as brandlib
    state = caller.state
    mesh = getattr(state, "mesh", None)
    return {"edition": modulelib.edition(state.settings()), "version": state.running_version,
            "face": brandlib.current().name, "operator": state.operator_tag() or None,
            "public_names": list(state.public_hosts), "mesh_id": getattr(mesh, "node_id", None)}


# --- the node's wallet: what Admin's wallet cards do, in the same two steps for a send -------------------------------


def _wallet(caller):
    if caller.node is None:
        raise CommandError("the node answers through the arcade's web app: run this in its console or arcade-cli")
    return caller.node


def _ask(fn, *args):
    try:
        return fn(*args)
    except CommandError:
        raise
    except Exception as exc:                               # noqa: BLE001 -- the node's own words
        raise CommandError(str(exc) or type(exc).__name__) from None


@command("wallet", audience="operator", module="wallet")
def getbalances(caller) -> list[dict]:
    """Every wallet of the node: its name for the other wallet commands, its chain, and what it holds.

    A Core wallet is named by its chain (messaging, ledger, or an add-on chain's network); a chain where the node
    holds coins with its own key is key:<network>."""
    return _ask(_wallet(caller).wallets)


@command("wallet", audience="operator", module="wallet")
def getnewaddress(caller, wallet: str) -> str:
    """An address the node's wallet is paid at: a new one from a Core wallet, the key's own on a key wallet.

    getnewaddress <wallet>   for example: getnewaddress ledger"""
    return _ask(_wallet(caller).receive, str(wallet))


#: The most transactions one listtransactions answers.
MOST_TRANSACTIONS = 100


@command("wallet", audience="operator", module="wallet")
def listtransactions(caller, wallet: str, count: int = 15) -> list[dict]:
    """A Core wallet's last transactions, newest first.

    listtransactions <wallet> [count]   up to 100"""
    if not isinstance(count, int) or count < 1:
        raise CommandError("count is a whole number, 1 or more")
    rows = _ask(_wallet(caller).history, str(wallet), min(count, MOST_TRANSACTIONS))
    keep = ("txid", "category", "amount", "fee", "address", "confirmations", "time")
    return [{k: (str(r[k]) if k in ("amount", "fee") else r[k]) for k in keep if k in r} for r in rows]


@command("wallet", audience="operator", module="wallet")
def listunspent(caller, wallet: str) -> list[dict]:
    """The coins a wallet of the node can spend: a Core wallet's or a key wallet's.

    listunspent <wallet>"""
    rows = _ask(_wallet(caller).unspent, str(wallet))
    return [{k: (str(v) if k == "amount" else v) for k, v in r.items()
             if k in ("txid", "vout", "address", "amount", "confirmations", "value", "height")} for r in rows]


@command("wallet", audience="operator", module="wallet")
def send(caller, wallet: str, to: str, amount: str) -> dict:
    """Prepare a send from the node's wallet and show its fee. Nothing goes until `confirm`.

    send <wallet> <@name or address> <amount>   for example: send ledger @alice 1.5
    The send is held for ten minutes under a ticket, the same as Admin's Send; `confirm <ticket>` broadcasts it."""
    held = _ask(_wallet(caller).prepare, str(wallet), str(to), str(amount))
    return {**held, "next": f"confirm {held['ticket']}"}


@command("wallet", audience="operator", module="wallet")
def confirm(caller, ticket: str) -> dict:
    """Broadcast a send that `send` prepared. Only at the node's own machine.

    confirm <ticket>
    From anywhere else, Admin -> Send asks for the admin password first; the console never takes a password."""
    if not caller.local:
        raise CommandError("a send is confirmed at the node's own machine; from elsewhere, Admin asks for the "
                           "admin password")
    return {"txid": _ask(_wallet(caller).broadcast, str(ticket))}


# --- messages: the node's own Messenger, as the Messages tab reads and sends it ---------------------------------------


def _text(body) -> str:
    return body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body or "")


def _store(caller):
    state = caller.state
    if not state.store_path.exists() or not getattr(state, "unlocked", False):
        try:
            state.ensure_identity()
        except Exception as exc:                           # noqa: BLE001
            raise CommandError(f"the node's identity is not open yet: {exc}") from None
    return state.store()


@command("messages", audience="operator", module="messages")
def listconversations(caller) -> list[dict]:
    """Everyone the node has written to or heard from, newest first, with what is unread.

    `readmessages <who>` opens one: their key, @name, contact code or address."""
    with _store(caller) as store:
        threads = store.conversations(caller.state.identity.fingerprint)
    return [{"who": t.get("name") or caller.name_of(t.get("address") or "") or t.get("address") or "",
             "key": t["pubkey"].hex(), "address": t.get("address") or "", "last": t.get("last"),
             "unread": t.get("unread", 0), "count": t.get("count", 0), "outgoing": t.get("outgoing", False),
             "preview": _text(t.get("preview"))[:80]} for t in threads]


@command("messages", audience="operator", module="messages")
def readmessages(caller, who: str, count: int = 20) -> list[dict]:
    """One conversation's last messages, oldest first; they are then read, as when the conversation is opened.

    readmessages <who> [count]   who: their key, @name, contact code or address"""
    if not isinstance(count, int) or count < 1:
        raise CommandError("count is a whole number, 1 or more")
    peer = _ask(_wallet(caller).peer, str(who))
    with _store(caller) as store:
        fingerprint = caller.state.identity.fingerprint
        items = store.thread(fingerprint, peer)[-count:]
        store.mark_thread_read(fingerprint, peer)
    return [{"when": m.get("when"), "mine": m.get("mine", False), "text": _text(m.get("body")),
             "txid": m.get("txid"), "confirmed": m.get("height") is not None} for m in items]


@command("messages", audience="operator", module="messages")
def sendmessage(caller, who: str, *text: str) -> dict:
    """Send a message from the node, on testnet, at once, as the Messages tab sends one.

    sendmessage <who> <text...>   for example: sendmessage @alice see you at the table
    A message long enough to need more than one transaction is sent from Messages, which shows its progress."""
    return _ask(_wallet(caller).message, str(who), " ".join(str(t) for t in text))


# --- the feed: read by anyone, as the Feed tab is; written by the node's operator --------------------------------------


#: The most posts one read answers.
MOST_POSTS = 100


def _reads(caller):
    """The operator's own reads (which say what is the node's), else anyone's."""
    tools = caller.node if caller.node is not None else caller.reads
    if tools is None:
        raise CommandError("the node answers through the arcade's web app: run this in its console or arcade-cli")
    return tools


def _count(count) -> int:
    if not isinstance(count, int) or count < 1:
        raise CommandError("count is a whole number, 1 or more")
    return min(count, MOST_POSTS)


@command("feed", module="feed")
def readfeed(caller, order: str = "popular", count: int = 20) -> list[dict]:
    """The feed, as its tab shows it: popular (the endorsed first) or new.

    readfeed [popular|new] [count]   up to 100"""
    if order not in ("popular", "new"):
        raise CommandError("usage: readfeed [popular|new] [count]")
    return _ask(_reads(caller).feed, order, "", _count(count))


@command("feed", module="feed")
def readposts(caller, who: str, count: int = 20) -> list[dict]:
    """One person's posts, newest first, as their page shows them.

    readposts <@name or address> [count]"""
    return _ask(_reads(caller).feed, "new", str(who), _count(count))


@command("feed", audience="operator", module="feed")
def post(caller, *text: str) -> dict:
    """Post on the feed as the node's @name, in one transaction.

    post <text...>   for example: post the arcade is open tonight"""
    return {"txid": _ask(_wallet(caller).feed_post, " ".join(str(t) for t in text))}


def _act(caller, doing: str, txid: str, text: str = "") -> dict:
    return {"txid": _ask(_wallet(caller).feed_act, doing, str(txid), text)}


@command("feed", audience="operator", module="feed")
def reply(caller, txid: str, *text: str) -> dict:
    """Reply to a post or a reply.

    reply <txid> <text...>   the txid is readfeed's"""
    if not text:
        raise CommandError("usage: reply txid text...")
    return _act(caller, "reply", txid, " ".join(str(t) for t in text))


@command("feed", audience="operator", module="feed")
def like(caller, txid: str) -> dict:
    """Like a post or a reply."""
    return _act(caller, "like", txid)


@command("feed", audience="operator", module="feed")
def unlike(caller, txid: str) -> dict:
    """Take a like back."""
    return _act(caller, "unlike", txid)


@command("feed", audience="operator", module="feed")
def share(caller, txid: str) -> dict:
    """Share a post onto the node's own feed."""
    return _act(caller, "share", txid)


@command("feed", audience="operator", module="feed")
def deletepost(caller, txid: str) -> dict:
    """Delete one of the node's own posts or replies. The chain keeps it; every arcade stops showing it."""
    return _act(caller, "delete", txid)


# --- modules: installed and removed as packages are -----------------------------------------------------------------


@command("modules")
def listmodules(caller) -> list[dict]:
    """What runs on this arcade: the core, and each app, theme and node module, on or off.

    The operator switches them with `install` and `remove`, or in Admin -> Edition & modules."""
    return [{"id": m["id"], "kind": m["kind"], "title": m["title"], "on": m["on"]}
            for m in modulelib.describe(caller.state.settings())["modules"]]


#: What `remove` and `edition` take as "yes, I read the warning", as apt takes -y.
YES = ("yes", "-y", "--yes")


def _apply(caller) -> None:
    """Routes follow at once (web/modules_web.py hands the state its gate); jobs at the next restart."""
    apply = getattr(caller.state, "apply_modules", None)
    if callable(apply):
        apply()


def _switched(caller, before: frozenset) -> str:
    after = modulelib.enabled(caller.state.settings())
    on, off = sorted(after - before), sorted(before - after)
    said = [f"on: {', '.join(on)}"] if on else []
    said += [f"off: {', '.join(off)}"] if off else []
    if any(modulelib.MODULES[m].jobs for m in on + off):
        said.append("pages follow now; background work follows at the next restart")
    return "; ".join(said) or "nothing changed"


@command("modules", audience="operator")
def install(caller, module: str) -> str:
    """Switch a module on, and the modules it needs.

    install <module>   for example: install feed
    Every module ships with the arcade, so nothing is downloaded; `listmodules` names them. Its pages answer at once,
    its background work starts at the next restart."""
    before = modulelib.enabled(caller.state.settings())
    try:
        switches = modulelib.switch(caller.state.settings(), module, True)
    except ValueError as exc:
        raise CommandError(str(exc)) from None
    caller.state.set_setting(modulelib.SWITCH_SETTING, switches)
    _apply(caller)
    return _switched(caller, before)


@command("modules", audience="operator")
def remove(caller, module: str, confirm: str = "") -> str:
    """Switch a module off. Its data stays where it is, for when it is installed again.

    remove <module> [yes]   a module with a warning (`remove exchange`) asks for yes
    Modules that need it stop with it."""
    found = modulelib.MODULES.get(module)
    if found is not None and found.caution and confirm not in YES:
        raise CommandError(f"{found.caution}\nType `remove {module} yes` to switch it off.")
    before = modulelib.enabled(caller.state.settings())
    try:
        switches = modulelib.switch(caller.state.settings(), module, False)
    except ValueError as exc:
        raise CommandError(str(exc)) from None
    caller.state.set_setting(modulelib.SWITCH_SETTING, switches)
    _apply(caller)
    return _switched(caller, before)


@command("modules", audience="operator")
def edition(caller, name: str = "", confirm: str = "") -> str:
    """Say this arcade's edition, or change it.

    edition              full or lite
    edition full         every module on
    edition lite yes     the core only; nothing is deleted
    A change of edition sets every module to that edition's default."""
    settings = caller.state.settings()
    if not name:
        return modulelib.edition(settings) + (" (not chosen yet)" if modulelib.asking(settings) else "")
    if name not in modulelib.EDITIONS:
        raise CommandError("usage: edition [full|lite] [yes]")
    if name == "lite" and confirm not in YES:
        raise CommandError("ARCADE Lite switches off every module but the core (nothing is deleted).\n"
                           "Type `edition lite yes` to switch.")
    before = modulelib.enabled(settings)
    caller.state.set_setting(modulelib.EDITION_SETTING, name)
    caller.state.set_setting(modulelib.SWITCH_SETTING, None)
    _apply(caller)
    return f"edition {name}; " + _switched(caller, before)


@command("node")
def listchains(caller) -> list[dict]:
    """Every chain this arcade has: run here or borrowed through the mesh, and how far each has synced.

    The operator also sees why a chain is offline."""
    out = []
    for chain in caller.state.all_chains:
        status = chain.status()
        out.append({"network": chain.network, "label": chain.label, "role": chain.role,
                    "where": "borrowed" if chain.borrowed else "here",
                    "online": bool(status.get("online")), "blocks": status.get("blocks"),
                    "headers": status.get("headers"), "synced": status.get("synced"),
                    "peers": status.get("peers")}
                   | ({"error": status.get("error")} if caller.may("operator") else {}))
    return out


# --- the mesh -----------------------------------------------------------------------------------------------------


@command("mesh", audience="operator")
def getmeshinfo(caller) -> dict:
    """This arcade on the mesh: its id, how many arcades it is linked to, and how others reach it."""
    mesh = getattr(caller.state, "mesh", None)
    if mesh is None or getattr(mesh, "loop", None) is None:
        return {"on": False}
    said = mesh.status()
    return {"on": True, "id": said.get("node"), "network": said.get("network"), "links": said.get("peers"),
            "known": said.get("known"), "listening": said.get("listening"), "reachable": said.get("reachable"),
            "tunnel": said.get("tunnel")}


@command("mesh", audience="user")
def listarcades(caller) -> list[dict]:
    """Every arcade heard from on the mesh, linked ones first: its id, operator @name, face and version."""
    mesh = getattr(caller.state, "mesh", None)
    operators = getattr(mesh, "operators", None) if mesh is not None else None
    if operators is None or getattr(mesh, "loop", None) is None:
        return []
    return [{"id": row.get("node"), "linked": bool(row.get("linked")), "operator": row.get("tag") or None,
             "face": row.get("name") or None, "version": row.get("version") or None,
             "hosts": list(row.get("hosts") or [])}
            for row in operators.status()]


# --- you ------------------------------------------------------------------------------------------------------------


@command("you", audience="user")
def whoami(caller) -> dict:
    """You: your @name and your address on each chain this arcade has.

    The operator at the arcade's own machine, with nobody signed in, is the operator's account."""
    out = {"audience": caller.audience, "account": caller.account, "name": None, "addresses": {}}
    for chain in caller.state.all_chains:
        address = caller.address(chain) if caller.account else ""
        if address:
            out["addresses"][chain.network] = address
            out["name"] = out["name"] or (caller.name_of(address) or None)
    return out

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
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import modules as modulelib
from .. import packages as packagelib

#: Who may run a command, least to most.
AUDIENCES = ("anyone", "user", "operator")


class CommandError(Exception):
    """A command could not run: an unknown name, a wrong argument, or the node could not answer."""


class CommandRefused(CommandError):
    """The caller's API key does not let it run this command: a module it does not open, or a change by a key that
    reads only. The web layer answers it with 403 (/api/v1/run)."""


@dataclass(frozen=True)
class Caller:
    """Who is asking: the node's state, their audience, and their account's public key when signed in.

    `address(chain)` and `name_of(address)` are the web app's own account lookups. `node` is the node's wallet as
    Admin drives it (the operator's only), and `local` says the operator is at the node's own machine. `reads` is what
    anyone may ask of the node: what its public pages show. `pubkeys` are the keys the caller's own wallet said it
    holds, by chain ({network: hex}), checked against their addresses before a node module sees one, and `offer` makes
    a node module's prepared transaction an offer the caller's wallet signs (arcade/modhost.py)."""
    state: Any
    audience: str = "anyone"
    account: str | None = None
    address: Callable[[Any], str] = lambda chain: ""
    name_of: Callable[[str], str] = lambda address: ""
    node: Any = None
    local: bool = False
    reads: Any = None
    pubkeys: dict = field(default_factory=dict)
    offer: Any = None
    #: False only when the web layer has decided this caller may not sign the node wallet (an operator API key
    #: that has not given the admin password). A caller built here keeps True, which is what the console's own
    #: tests use; `confirm` is still local-only.
    may_spend: bool = True
    #: An API key's own limits (/api/v1/run, web/apikeys_web.py): the modules it opens (None for every one), and
    #: whether it was made to read only. The console, the terminal and a signed-in window keep None and False.
    modules: frozenset | None = None
    reads_only: bool = False

    def may(self, audience: str) -> bool:
        return AUDIENCES.index(self.audience) >= AUDIENCES.index(audience)


@dataclass(frozen=True)
class Command:
    name: str
    category: str
    audience: str
    fn: Callable[..., Any]
    module: str = "node"
    #: A node module's form for this command (packages._check_form): drawn by the Lite window above the module's page.
    form: dict | None = field(default=None, compare=False)
    #: Where it stands in its module's manifest: the window draws the forms in that order, the steps' own.
    order: int = 0
    #: True when it changes anything: sends, posts, switches, installs, or marks something read. A key that reads only
    #: does not run it. A node module's commands count as changes: its manifest does not say which only read.
    writes: bool = False

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


def command(category: str, audience: str = "anyone", module: str = "node", writes: bool = False):
    assert audience in AUDIENCES, audience
    assert module in modulelib.MODULES, module

    def register(fn):
        COMMANDS[fn.__name__] = Command(fn.__name__, category, audience, fn, module, writes=writes)
        return fn
    return register


#: Asked for a command's help in the way a terminal command is: `send -help`.
HELP_FLAGS = ("-help", "--help", "-h")


def _installed(caller: Caller, cmd: Command) -> bool:
    """Whether the command's module is on here, and open to this caller (Admin -> Edition & modules, "Who may use
    it"): a module closed to a caller is, to them, a module this arcade does not run."""
    try:
        settings = caller.state.settings()
        return cmd.module in modulelib.enabled(settings) and modulelib.may_use(settings, cmd.module, caller.audience)
    except Exception:                                      # noqa: BLE001 -- a state with no settings runs the core
        return getattr(modulelib.MODULES.get(cmd.module), "core", False)


def _refused(caller: Caller, cmd: Command) -> str:
    """Why the caller's API key may not run this command, or "" when it may. A key limited to some modules runs their
    commands, and the core's that only read and are not the operator's (help, getnodeinfo, listchains, whoami ...),
    which tell anyone about this arcade and the caller; a key that reads only runs no command that changes anything."""
    if caller.modules is not None and cmd.module not in caller.modules \
            and not (cmd.module == "node" and not cmd.writes and cmd.audience != "operator"):
        title = getattr(modulelib.MODULES.get(cmd.module), "title", cmd.module)
        return f"{cmd.name}: this API key does not open the {title} module ({cmd.module})"
    if caller.reads_only and cmd.writes:
        return f"{cmd.name}: this API key reads only, and {cmd.name} changes something"
    return ""


def _commands(caller: Caller) -> dict[str, Command]:
    """Every command: the arcade's own, and those of the node modules installed from packages (`_module_commands`).
    A module's command never takes the name of one of the arcade's."""
    return {**_module_commands(caller), **COMMANDS}


def run(caller: Caller, name: str, *args: Any) -> Any:
    cmd = _commands(caller).get(name)
    if cmd is None or not caller.may(cmd.audience):
        # One answer for both, so a visitor cannot list the operator's commands by trying names.
        raise CommandError(f"no command {name!r}; `help` lists them")
    refused = _refused(caller, cmd)
    if refused:
        raise CommandRefused(refused)
    if not _installed(caller, cmd):
        hint = f"; `install {cmd.module}` switches it on" if caller.may("operator") else ""
        title = getattr(modulelib.MODULES.get(cmd.module), "title", cmd.module)
        raise CommandError(f"{name}: the {title} module is off on this arcade{hint}")
    try:
        inspect.signature(cmd.fn).bind(caller, *args)
    except TypeError:
        raise CommandError(f"usage: {cmd.usage}") from None
    return cmd.fn(caller, *args)


def available(caller: Caller) -> list[Command]:
    return sorted((c for c in _commands(caller).values()
                   if caller.may(c.audience) and _installed(caller, c) and not _refused(caller, c)),
                  key=lambda c: (c.category, c.name))


def run_line(caller: Caller, line: str) -> Any:
    """One console line, `name arg ...` as in a Core's console. A whole number, true, false, null, a list or an
    object is taken as JSON; everything else, an amount such as 0.00000001 included, as the text typed. A command
    that takes the rest of the line (sendmessage's text) gets it as written, apostrophes and all."""
    rest = _rest_of_line(line, _commands(caller))
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


def _rest_of_line(line: str, commands: dict[str, Command]) -> tuple[str, list[str], str] | None:
    """(name, its fixed arguments, the rest of the line) for a command whose last argument is the rest of the line;
    None for any other line. The rest is unquoted as a shell would (arcade-cli quotes what it sends) when it can be,
    and kept as typed when it cannot (an apostrophe)."""
    parts = line.split(None, 1)
    cmd = commands.get(parts[0]) if parts else None
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
        cmd = _commands(caller).get(name)
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
    """This arcade: its version, face, operator and the names it is published under. `operator` is the @name it answers
    to; `node_name` and `owner` are its node's own and its owner account's, and `owned_by` is state.owner_kind()
    (2026-11-04, "Who runs this node")."""
    from ..web import brand as brandlib
    state = caller.state
    mesh = getattr(state, "mesh", None)
    return {"edition": modulelib.edition(state.settings()), "version": state.running_version,
            "face": brandlib.current().name, "operator": state.operator_tag() or None,
            "node_name": state.node_tag() or None, "owner": state.owner_tag() or None, "owned_by": state.owner_kind(),
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


@command("wallet", audience="operator", module="wallet", writes=True)
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


@command("wallet", audience="operator", module="wallet", writes=True)
def send(caller, wallet: str, to: str, amount: str) -> dict:
    """Prepare a send from the node's wallet and show its fee. Nothing goes until `confirm`.

    send <wallet> <@name or address> <amount>   for example: send ledger @alice 1.5
    The send is held for ten minutes under a ticket, the same as Admin's Send; `confirm <ticket>` broadcasts it.
    An operator API key is not the machine, so it prepares a send only with the admin password. `confirm` stays
    at the machine either way."""
    if not caller.may_spend:
        raise CommandError("the admin password, to send the node's coins")
    held = _ask(_wallet(caller).prepare, str(wallet), str(to), str(amount))
    return {**held, "next": f"confirm {held['ticket']}"}


@command("wallet", audience="operator", module="wallet", writes=True)
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


#: An account's messages are sealed to its own key and opened in its own browser: the node cannot read or write
#: them, so for an account these three run in the Lite window's console (templates/_messages.html), never here.
IN_THE_BROWSER = ("your messages open only in your own browser: run this in ArcadeCore's console, with your "
                  "wallet unlocked")


def _the_nodes(caller) -> None:
    if caller.audience != "operator":
        raise CommandError(IN_THE_BROWSER)


@command("messages", audience="user", module="messages")
def listconversations(caller) -> list[dict]:
    """Everyone you have written to or heard from, newest first, with what is unread. The node's, for its operator.

    `readmessages <who>` opens one: their key, @name, contact code or address."""
    _the_nodes(caller)
    with _store(caller) as store:
        threads = store.conversations(caller.state.identity.fingerprint)
    return [{"who": t.get("name") or caller.name_of(t.get("address") or "") or t.get("address") or "",
             "key": t["pubkey"].hex(), "address": t.get("address") or "", "last": t.get("last"),
             "unread": t.get("unread", 0), "count": t.get("count", 0), "outgoing": t.get("outgoing", False),
             "preview": _text(t.get("preview"))[:80]} for t in threads]


@command("messages", audience="user", module="messages", writes=True)
def readmessages(caller, who: str, count: int = 20) -> list[dict]:
    """One conversation's last messages, oldest first; they are then read, as when the conversation is opened.

    readmessages <who> [count]   who: their key, @name, contact code or address"""
    if not isinstance(count, int) or count < 1:
        raise CommandError("count is a whole number, 1 or more")
    _the_nodes(caller)
    peer = _ask(_wallet(caller).peer, str(who))
    with _store(caller) as store:
        fingerprint = caller.state.identity.fingerprint
        items = store.thread(fingerprint, peer)[-count:]
        store.mark_thread_read(fingerprint, peer)
    return [{"when": m.get("when"), "mine": m.get("mine", False), "text": _text(m.get("body")),
             "txid": m.get("txid"), "confirmed": m.get("height") is not None} for m in items]


@command("messages", audience="user", module="messages", writes=True)
def sendmessage(caller, who: str, *text: str) -> dict:
    """Send a message, on testnet, at once, as the Messages tab sends one: yours, or the node's for its operator.

    sendmessage <who> <text...>   for example: sendmessage @alice see you at the table
    A message long enough to need more than one transaction is sent from Messages, which shows its progress."""
    _the_nodes(caller)
    return _ask(_wallet(caller).message, str(who), " ".join(str(t) for t in text))


# --- the feed: read by anyone, as the Feed tab is; written by the node's operator --------------------------------------


#: The most posts one read answers.
MOST_POSTS = 100


def _read(caller, name: str):
    """A read of the node's: the operator's own where there is one (it says what is the node's), else anyone's."""
    for tools in (caller.node, caller.reads):
        if tools is not None and hasattr(tools, name):
            return getattr(tools, name)
    raise CommandError("the node answers through the arcade's web app: run this in its console or arcade-cli")


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
    return _ask(_read(caller, "feed"), order, "", _count(count))


@command("feed", module="feed")
def readposts(caller, who: str, count: int = 20) -> list[dict]:
    """One person's posts, newest first, as their page shows them.

    readposts <@name or address> [count]"""
    return _ask(_read(caller, "feed"), "new", str(who), _count(count))


@command("feed", audience="operator", module="feed", writes=True)
def post(caller, *text: str) -> dict:
    """Post on the feed as the node's @name, in one transaction.

    post <text...>   for example: post the arcade is open tonight"""
    return {"txid": _ask(_wallet(caller).feed_post, " ".join(str(t) for t in text))}


def _act(caller, doing: str, txid: str, text: str = "") -> dict:
    return {"txid": _ask(_wallet(caller).feed_act, doing, str(txid), text)}


@command("feed", audience="operator", module="feed", writes=True)
def reply(caller, txid: str, *text: str) -> dict:
    """Reply to a post or a reply.

    reply <txid> <text...>   the txid is readfeed's"""
    if not text:
        raise CommandError("usage: reply txid text...")
    return _act(caller, "reply", txid, " ".join(str(t) for t in text))


@command("feed", audience="operator", module="feed", writes=True)
def like(caller, txid: str) -> dict:
    """Like a post or a reply."""
    return _act(caller, "like", txid)


@command("feed", audience="operator", module="feed", writes=True)
def unlike(caller, txid: str) -> dict:
    """Take a like back."""
    return _act(caller, "unlike", txid)


@command("feed", audience="operator", module="feed", writes=True)
def share(caller, txid: str) -> dict:
    """Share a post onto the node's own feed."""
    return _act(caller, "share", txid)


@command("feed", audience="operator", module="feed", writes=True)
def deletepost(caller, txid: str) -> dict:
    """Delete one of the node's own posts or replies. The chain keeps it; every arcade stops showing it."""
    return _act(caller, "delete", txid)


# --- the market: what the Exchange's public pages show, for anyone ---------------------------------------------------


def _number(value, what: str) -> int:
    if not isinstance(value, int) or value < 0:
        raise CommandError(f"{what} is a token's number; listtokens lists them")
    return value


@command("market", module="tokens")
def listtokens(caller) -> list[dict]:
    """The tokens this arcade's index knows: number, name, supply and holders."""
    return _ask(_read(caller, "tokens"))


@command("market", module="exchange")
def getbook(caller, token: int) -> dict:
    """A token's resting sell orders for the chain's coin, cheapest first, as its market page shows them.

    getbook <token number>"""
    token = _number(token, "token")
    return _ask(_read(caller, "book"), token)


@command("market", module="exchange")
def getpair(caller, base: int, quote: int, count: int = 20) -> dict:
    """A token-for-token market: its asks, its bids, and its last trades, newest first.

    getpair <base token> <quote token> [count]   prices are quote per whole base"""
    base, quote, count = _number(base, "base"), _number(quote, "quote"), _count(count)
    return _ask(_read(caller, "pair"), base, quote, count)


@command("market", module="exchange")
def listdoginals(caller, chain: str = "doge") -> list[dict]:
    """Doginals listed for sale here while their coin is unspent, or another ord chain's (main: Pepecoin mainnet).

    listdoginals [doge|main]"""
    return _ask(_read(caller, "doginals"), str(chain))


# --- modules: installed and removed as packages are -----------------------------------------------------------------


# --- apps: the App Store's cards, for anyone, as its tab lists them ----------------------------------------------------


@command("apps", module="apps")
def listapps(caller, sort: str = "popular", count: int = 30) -> list[dict]:
    """The apps and games on this arcade's chains, as the App Store ranks them: popular or new.

    listapps [popular|new] [count]   up to 100. An app plays at /inscriptions/<opens>/full (Ord indexes on)."""
    if sort not in ("popular", "new"):
        raise CommandError("sort is popular or new")
    return _read(caller, "apps")(sort, _count(count))


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
    host = getattr(caller.state, "module_host", None)
    for name in off:
        if name not in modulelib.MODULES and host is not None:
            host.stop(name)                                # a node module's process stops with its switch
    if any(getattr(modulelib.MODULES.get(m), "jobs", ()) for m in on + off):
        said.append("pages follow now; background work follows at the next restart")
    return "; ".join(said) or "nothing changed"


@command("modules", audience="operator", writes=True)
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


@command("modules", audience="operator", writes=True)
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


@command("modules", audience="operator", writes=True)
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
        raise CommandError("ArcadeCore (Lite) switches off every module but the core (nothing is deleted).\n"
                           "Type `edition lite yes` to switch.")
    before = modulelib.enabled(settings)
    caller.state.set_setting(modulelib.EDITION_SETTING, name)
    caller.state.set_setting(modulelib.SWITCH_SETTING, None)
    caller.state.set_setting(modulelib.LOOK_SETTING, "core" if name == "lite" else None)
    _apply(caller)
    return f"edition {name}; " + _switched(caller, before)


@command("modules", audience="operator", writes=True)
def look(caller, name: str = "") -> str:
    """Say the look this arcade opens in, or change it.

    look                 the look now
    look core            ArcadeCore, this window
    look ubuntu|w98      a Desktop View look (needs the desktop module)
    look web             the website's pages
    Each person can wear another look; this is the one a new visitor gets."""
    settings = caller.state.settings()
    if not name:
        return modulelib.look(settings)
    if name not in modulelib.LOOKS:
        raise CommandError("usage: look [" + "|".join(modulelib.LOOKS) + "]")
    if name in modulelib.DESKTOP_LOOKS and "desktop" not in modulelib.enabled(settings):
        raise CommandError("the Desktop View module is off: `install desktop` first")
    caller.state.set_setting(modulelib.LOOK_SETTING, name)
    return f"look {name} ({modulelib.LOOK_TITLES[name]})"


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


@command("mining", audience="operator", module="mining", writes=True)
def mine(caller, chain: str, blocks: int = 0, to: str = "") -> dict:
    """Mine a test chain's blocks to your own account, on this machine's CPU, until `mine stop`.

    mine <chain> [blocks] [address]   for example: mine dgbtest; then `mining`, and `mine stop`
    To your account's address on that chain, or the address given. From arcade-cli, to the operator's account once
    its wallet has been opened here. The chain's own node mines: DigiByte testnet mines odo, qubit and skein side by
    side, a thread each. Coins from a mined block can be spent once the chain is 100 blocks further."""
    from .. import testmine
    from ..blockminer import output_script
    if str(chain) == "stop":
        return {"stopping": testmine.stop(caller.state)}
    found = next((c for c in caller.state.all_chains if c.network == str(chain)), None)
    if found is None:
        raise CommandError(f"this arcade has no {chain} chain; `listchains` lists them")
    if not isinstance(blocks, int) or blocks < 0:
        raise CommandError("blocks is a whole number: how many to mine before stopping, 0 for until `mine stop`")
    address = str(to or "").strip() or (caller.address(found) if caller.account else "")
    op = getattr(caller.state, "operator", "") if not (to or caller.account) else ""
    if op:                                                 # arcade-cli: the operator's own account, as its wallet said
        address = str(caller.state.setting(f"address:{found.network}:{op}", "") or "")
    if to:
        try:
            output_script(address, found.params)
        except Exception as exc:                           # noqa: BLE001 -- said to the operator
            raise CommandError(f"mine: {to} is not a {found.label} address: {exc}") from exc
    try:
        return testmine.start(caller.state, found, address, blocks)
    except Exception as exc:                               # noqa: BLE001 -- said to the operator
        raise CommandError(f"mine: {exc}") from exc


@command("mining", audience="operator", module="mining")
def mining(caller) -> dict:
    """How the mining goes: the chain, the blocks found, each algorithm's hashes a second; or how it last ended."""
    from .. import testmine
    return testmine.status(caller.state)


@command("mining", audience="operator", module="mining")
def listmining(caller) -> list[dict]:
    """Every chain here and how it can be mined: by its node or this CPU (test chains), by ASICs on this network."""
    from .. import mining as mininglib
    return mininglib.table(caller.state)


@command("mining", audience="operator", module="mining", writes=True)
def stratum(caller, action: str = "", chain: str = "", algo: str = "", address: str = "", port: int = 0) -> dict:
    """The stratum server for ASICs on this network: its state, or start and stop it.

    stratum                                   what it is doing: its URL, the miners on it, their hashrate, its blocks
    stratum start <chain> <algo> <address> [port]   for example: stratum start ltc scrypt <your address> 3333
    stratum stop
    Point an ASIC at the URL it gives, any worker name and password. Solo mining: a block it finds pays the address
    whole. A chain whose node runs here and is not merge-mined, in scrypt or sha256d; only miners on this network."""
    from .. import stratum as stratumlib
    if getattr(caller.state, "stratum", None) is None:
        caller.state.stratum = stratumlib.Service(caller.state)
    service = caller.state.stratum
    if action in ("", "status"):
        return service.status()
    if action == "stop":
        return service.configure(on=False)
    if action != "start":
        raise CommandError("stratum, stratum start <chain> <algo> <address> [port], or stratum stop")
    if not (chain and algo and address):
        raise CommandError("stratum start <chain> <algo> <address> [port]: `listmining` says what each chain takes")
    got = service.configure(on=True, net=str(chain), algo=str(algo), address=str(address),
                            port=int(port) if port else None)
    if not got.get("running"):
        raise CommandError(f"stratum: {got.get('error') or 'did not start'}")
    return got


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


# --- themes ---------------------------------------------------------------------------------------------------------
#
# A theme is a package the operator makes or chooses (arcade/packages.py), kept in the arcade's home. The operator
# sets the arcade's own and which ones people may pick; each person picks theirs. The operator's own choice, and what
# `trytheme` shows, are the machine's: from a folder on it.


def _theme(caller, said: str) -> str:
    """An installed theme by its name, or its id or the start of it (8 characters or more)."""
    said = (said or "").strip().lower()
    have = caller.state.setting(packagelib.SETTING) or {}
    found = [pid for pid, row in have.items() if row.get("kind") == "theme"
             and (row.get("name") == said or (len(said) >= 8 and pid.startswith(said)))]
    if len(found) != 1:
        raise CommandError(f"no installed theme '{said}'" if not found
                           else f"more than one theme is called '{said}': use its id")
    return found[0]


def _folder(caller, folder: str, kind: str = "theme"):
    if not caller.local:
        raise CommandError(f"a {kind} is read from a folder on the arcade's own machine: run this there")
    try:
        manifest, files = packagelib.read(folder)
    except (packagelib.PackageError, OSError) as exc:
        raise CommandError(str(exc)) from None
    if manifest["kind"] != kind:
        raise CommandError(f"that folder is a {manifest['kind']}, not a {kind}")
    return manifest, files


def _owner(caller) -> str:
    return caller.account.lower() if caller.account else "node"


@command("themes")
def listthemes(caller) -> dict:
    """The arcade's theme, the ones you may pick, and yours. The operator also sees every installed one."""
    settings = caller.state.settings()
    have = {pid: row for pid, row in (settings.get(packagelib.SETTING) or {}).items() if row.get("kind") == "theme"}
    allowed = set(settings.get(packagelib.ALLOWED) or [])
    row = lambda pid: {"id": pid, "name": have[pid]["name"], "title": have[pid]["title"],  # noqa: E731
                       "version": have[pid]["version"], "summary": have[pid]["summary"], "allowed": pid in allowed}
    out = {"arcade": settings.get(packagelib.THEME) or None,
           "pick": [row(pid) for pid in sorted(have, key=lambda p: have[p]["title"]) if pid in allowed]}
    if caller.may("user"):          # your own pick while you may wear it, not the arcade's that you see without one
        seen = packagelib.chosen(settings, _owner(caller))
        out["yours"] = seen if seen and settings.get(f"{packagelib.THEME}:{_owner(caller)}") == seen else None
    if caller.may("operator"):
        out["installed"] = [row(pid) for pid in sorted(have, key=lambda p: have[p]["title"])]
        out["trying"] = settings.get(packagelib.TRY) or None
    return out


@command("themes", audience="user", writes=True)
def usetheme(caller, theme: str = "none") -> str:
    """Pick your own theme from the ones this arcade lets people pick, or `none` for the arcade's.

    usetheme <name>   listthemes names them
    usetheme none"""
    owner = _owner(caller)
    if theme == "none":
        caller.state.set_setting(f"{packagelib.THEME}:{owner}", None)
        return "you see the arcade's theme"
    pid = _theme(caller, theme)
    if owner != "node" and pid not in (caller.state.setting(packagelib.ALLOWED) or []):
        raise CommandError("this arcade's operator has not let people pick that theme")
    caller.state.set_setting(f"{packagelib.THEME}:{owner}", pid)
    return f"you see {theme}"


@command("themes", audience="operator")
def checktheme(caller, folder: str) -> dict:
    """Read a theme's folder and say what is wrong with it, or its id. Nothing is kept.

    checktheme ~/my-theme   at the arcade's own machine
    `arcade-cli new theme <name>` makes a folder to start from."""
    manifest, files = _folder(caller, folder)
    return {"id": packagelib.package_id(files), "name": manifest["name"], "version": manifest["version"],
            "files": sorted(files)}


@command("themes", audience="operator", writes=True)
def trytheme(caller, folder: str) -> str:
    """Show a theme's folder in this machine's browser only, as it is now, until `trytheme off`.

    trytheme ~/my-theme   change the folder and reload the page to see it again
    trytheme off"""
    if folder == "off":
        caller.state.set_setting(packagelib.TRY, None)
        return "trying nothing"
    _folder(caller, folder)
    caller.state.set_setting(packagelib.TRY, str(Path(folder).expanduser().resolve()))
    return "reload the page on this machine to see it; nobody else does"


@command("themes", audience="operator", writes=True)
def installtheme(caller, folder: str) -> dict:
    """Keep a theme from a folder on this machine, under its id. It shows nowhere until it is chosen.

    installtheme ~/my-theme   then settheme or allowtheme"""
    _folder(caller, folder)
    try:
        pid, row = packagelib.install(caller.state.home, folder)
    except (packagelib.PackageError, OSError) as exc:
        raise CommandError(str(exc)) from None
    have = dict(caller.state.setting(packagelib.SETTING) or {})
    have[pid] = row
    caller.state.set_setting(packagelib.SETTING, have)
    return {"id": pid, "name": row["name"], "version": row["version"]}


@command("themes", audience="operator", writes=True)
def settheme(caller, theme: str = "none") -> str:
    """Make an installed theme this arcade's own look, for everyone who has not picked one, or `none`.

    settheme <name>
    settheme none   ArcadeCore's own look"""
    pid = None if theme == "none" else _theme(caller, theme)
    caller.state.set_setting(packagelib.THEME, pid)
    return f"this arcade wears {theme}" if pid else "this arcade wears ArcadeCore's own look"


@command("themes", audience="operator", writes=True)
def allowtheme(caller, theme: str, on: str = "on") -> str:
    """Let people pick an installed theme for themselves, or stop them (`off`; who had it goes back to the arcade's).

    allowtheme <name> [on|off]"""
    pid = _theme(caller, theme)
    allowed = [p for p in caller.state.setting(packagelib.ALLOWED) or [] if p != pid]
    if on not in ("on", "off"):
        raise CommandError("usage: allowtheme <name> [on|off]")
    caller.state.set_setting(packagelib.ALLOWED, allowed + ([pid] if on == "on" else []))
    return f"people {'may' if on == 'on' else 'may not'} pick {theme}"


@command("themes", audience="operator", writes=True)
def removetheme(caller, theme: str) -> str:
    """Remove an installed theme. Whoever wore it sees the arcade's theme again."""
    pid = _theme(caller, theme)
    have = dict(caller.state.setting(packagelib.SETTING) or {})
    have.pop(pid, None)
    caller.state.set_setting(packagelib.SETTING, have)
    allowed = caller.state.setting(packagelib.ALLOWED) or []
    caller.state.set_setting(packagelib.ALLOWED, [p for p in allowed if p != pid])
    if caller.state.setting(packagelib.THEME) == pid:
        caller.state.set_setting(packagelib.THEME, None)
    shutil.rmtree(packagelib.home_of(caller.state.home, pid), ignore_errors=True)
    return f"removed {theme}"


# --- themes between arcades: offered, and taken (arcade/mesh/themeshare.py; Window -> Build) ---------------------------


def _themeshare(caller):
    share = getattr(getattr(caller.state, "mesh", None), "themeshare", None)
    if share is None:
        raise CommandError("this arcade is not on the mesh, so it cannot offer or take themes")
    return share


@command("themes", audience="operator", writes=True)
def sharetheme(caller, theme: str, on: str = "on") -> str:
    """Offer an installed theme to the arcades this one links with, or stop (`off`). Each of their operators sees it
    under Window -> Build -> Shared by others and chooses whether to install it; nothing installs by itself.

    sharetheme <name> [on|off]"""
    from ..mesh.themeshare import SHARED
    pid = _theme(caller, theme)
    if on not in ("on", "off"):
        raise CommandError("usage: sharetheme <name> [on|off]")
    shared = [p for p in caller.state.setting(SHARED) or [] if p != pid]
    caller.state.set_setting(SHARED, shared + ([pid] if on == "on" else []))
    return (f"{theme} is offered to the arcades you link with" if on == "on"
            else f"{theme} is no longer offered; arcades that installed it keep it")


@command("themes", audience="operator")
def sharedthemes(caller) -> dict:
    """The themes this arcade offers, and those linked arcades offer it (with which arcades offer each)."""
    from ..mesh.themeshare import SHARED
    settings = caller.state.settings()
    have = settings.get(packagelib.SETTING) or {}
    share = getattr(getattr(caller.state, "mesh", None), "themeshare", None)
    mine = [{"id": pid, "name": have[pid]["name"], "title": have[pid].get("title") or have[pid]["name"],
             "shared": pid in (settings.get(SHARED) or []), "from": have[pid].get("from") or "local"}
            for pid in sorted(have, key=lambda p: have[p].get("title") or "") if have[pid].get("kind") == "theme"]
    offered = [{**row, "installed": row["id"] in have} for row in (share.offered() if share is not None else [])]
    return {"mine": mine, "offered": offered, "mesh": share is not None}


@command("themes", audience="operator", writes=True)
def taketheme(caller, theme_id: str) -> dict:
    """Install a theme a linked arcade offers, by its id (`sharedthemes` lists them). It is checked against its id and
    as a theme from a folder is, then kept like one: it shows nowhere until you settheme or allowtheme it.

    taketheme <id>"""
    share = _themeshare(caller)
    theme_id = (theme_id or "").strip().lower()
    matches = [row["id"] for row in share.offered() if row["id"].startswith(theme_id)] if len(theme_id) >= 8 else []
    if len(matches) != 1:
        raise CommandError("no linked arcade offers a theme with that id" if not matches
                           else "more than one theme starts so: give more of its id")
    try:
        pid, row = share.install(matches[0])
    except Exception as exc:                               # noqa: BLE001 -- the console says what went wrong
        raise CommandError(f"not installed: {exc}") from None
    have = dict(caller.state.setting(packagelib.SETTING) or {})
    have[pid] = row
    caller.state.set_setting(packagelib.SETTING, have)
    return {"id": pid, "name": row["name"], "version": row["version"], "from": row["from"]}


# --- node modules ---------------------------------------------------------------------------------------------------
#
# A node module is a package too (arcade/packages.py): Python the arcade runs confined, in its own process, with no
# keys (arcade/modhost.py). Installing one keeps it; `install <name>` switches it on, as any module. Its commands join
# this list under its own name, for whoever its manifest names, and what spends comes back as an offer to sign.


def _module_commands(caller: Caller) -> dict[str, Command]:
    host = getattr(caller.state, "module_host", None)
    try:
        installed = host.installed(caller.state.settings()) if host is not None else {}
    except Exception:                                      # noqa: BLE001 -- a module that cannot be read adds nothing
        return {}
    out = {}
    for module, (_, manifest) in installed.items():
        for n, (name, said) in enumerate((manifest.get("commands") or {}).items()):
            if name not in COMMANDS and name not in out:
                out[name] = Command(name, module, said.get("audience", "anyone"), _forward(module, name, said), module,
                                    said.get("form"), n, writes=True)
    return out


def _forward(module: str, name: str, said: dict):
    """A command of a node module, as one of ours: its usage and help from the manifest, its answer from the module."""
    def fn(caller, *args):
        return _ask_module(caller, module, {"call": "command", "name": name, "args": list(args)})
    P = inspect.Parameter
    fn.__signature__ = inspect.Signature([P("caller", P.POSITIONAL_ONLY)] + [
        P(a.strip("[]"), P.POSITIONAL_OR_KEYWORD, default=None if a.startswith("[") else P.empty)
        for a in said.get("args", [])])
    fn.__doc__ = str(said.get("help") or "")
    fn.__name__ = name
    return fn


def _keyhash(address: str, params) -> bytes:
    from ..script import b58check_decode, segwit_decode
    if params.bech32_hrp and address.lower().startswith(params.bech32_hrp + "1"):
        return segwit_decode(params.bech32_hrp, address.lower())[1] or b""
    return b58check_decode(address)[1]


def module_caller(caller: Caller, chains) -> dict:
    """Who is asking, as a node module is told: their audience and account, their address on each chain it names,
    and the keys their wallet said it holds there, kept only where a key's hash is that address's."""
    from ..script import hash160
    out = {"audience": caller.audience, "account": caller.account, "address": {}, "pubkey": {}}
    for chain in caller.state.all_chains if caller.account else ():
        if chain.network not in chains:
            continue
        address = caller.address(chain)
        out["address"][chain.network] = address
        key = str(caller.pubkeys.get(chain.network) or "")
        try:
            if address and len(key) == 66 and hash160(bytes.fromhex(key)) == _keyhash(address, chain.params):
                out["pubkey"][chain.network] = key
        except ValueError:
            pass
    return out


def _ask_module(caller: Caller, module: str, request: dict):
    from .. import modhost
    host = caller.state.module_host
    settings = caller.state.settings()
    chains = host.installed(settings)[module][1].get("needs", {}).get("chains", {})
    who = module_caller(caller, chains)
    try:
        answer = host.ask(settings, module, request, who)
        if not (isinstance(answer, dict) and isinstance(answer.get("prepared"), dict)):
            return answer
        prepared = answer["prepared"]
        net = str(prepared.get("chain"))
        key = who.get("pubkey", {}).get(net)
        chain, unsigned = host.prepare(settings, module, prepared, who["address"].get(net),
                                       bytes.fromhex(key) if key else None)
    except modhost.ModuleFailed as exc:
        raise CommandError(str(exc)) from None
    said = {k: v for k, v in answer.items() if k != "prepared"}
    if caller.offer is None:
        return {**said, "prepared": {"chain": chain, **unsigned.as_json()},
                "sign": None, "how": "sign it in your wallet: run this in the arcade's window"}
    return {**said, "sign": caller.offer(chain, unsigned)}


def _module_named(caller, said: str) -> tuple[str, dict]:
    have = caller.state.setting(packagelib.SETTING) or {}
    found = [pid for pid, row in have.items() if row.get("kind") == "module"
             and (row.get("name") == said or (len(said) >= 8 and pid.startswith(said)))]
    if len(found) != 1:
        raise CommandError(f"no node module '{said}' is installed")
    return found[0], have[found[0]]


@command("modules", audience="operator")
def checkmodule(caller, folder: str) -> dict:
    """Read a node module's folder and say what is wrong with it, or what it would add. Nothing is kept or run.

    checkmodule ~/my-module   at the arcade's own machine"""
    manifest, files = _folder(caller, folder, "module")
    return {"id": packagelib.package_id(files), "name": manifest["name"], "version": manifest["version"],
            "commands": sorted(manifest.get("commands") or {}), "reads": manifest.get("needs", {}).get("chains", {}),
            "max_fee": manifest.get("needs", {}).get("max_fee", packagelib.MOST_FEE), "files": sorted(files)}


@command("modules", audience="operator", writes=True)
def installmodule(caller, folder: str) -> dict:
    """Keep a node module from a folder on this machine, under its id. It runs only once `install <name>` switches it
    on; a new version of one that is installed replaces it and is off until switched on again.

    installmodule ~/my-module"""
    manifest, files = _folder(caller, folder, "module")
    return keep_module(caller.state, folder, manifest)


def keep_module(state, folder, manifest: dict) -> dict:
    """installmodule's work, also Admin -> Edition & modules' Add for a module that comes with the arcade
    (web/modules_web.py): kept under its id and off; a new version replaces the old one and is off again."""
    name = manifest["name"]
    if name in modulelib.MODULES or any(c in COMMANDS for c in manifest.get("commands") or {}):
        raise CommandError(f"{name} takes a name the arcade's own modules or commands use")
    host = getattr(state, "module_host", None)
    why = host and host.why_not()
    try:
        pid, row = packagelib.install(state.home, folder)
    except (packagelib.PackageError, OSError) as exc:
        raise CommandError(str(exc)) from None
    have = dict(state.setting(packagelib.SETTING) or {})
    for old in [p for p, r in have.items() if r.get("kind") == "module" and r.get("name") == name and p != pid]:
        have.pop(old)
        shutil.rmtree(packagelib.home_of(state.home, old), ignore_errors=True)
        if host is not None:
            host.stop(name)
        switches = dict(state.setting(modulelib.SWITCH_SETTING) or {})
        switches.pop(name, None)
        state.set_setting(modulelib.SWITCH_SETTING, switches)
    have[pid] = row
    state.set_setting(packagelib.SETTING, have)
    return {"id": pid, "name": name, "version": row["version"],
            "next": f"cannot run on this machine: {why}" if why else f"`install {name}` switches it on"}


@command("modules", audience="operator", writes=True)
def removemodule(caller, module: str) -> str:
    """Remove an installed node module and stop it. Its data folder stays, for when it is installed again."""
    pid, row = _module_named(caller, module)
    caller.state.module_host.stop(row["name"])
    have = dict(caller.state.setting(packagelib.SETTING) or {})
    have.pop(pid, None)
    caller.state.set_setting(packagelib.SETTING, have)
    switches = dict(caller.state.setting(modulelib.SWITCH_SETTING) or {})
    switches.pop(row["name"], None)
    caller.state.set_setting(modulelib.SWITCH_SETTING, switches)
    shutil.rmtree(packagelib.home_of(caller.state.home, pid), ignore_errors=True)
    _apply(caller)
    return f"removed {row['name']}"

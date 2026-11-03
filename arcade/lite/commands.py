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

    `address(chain)` and `name_of(address)` are the web app's own account lookups."""
    state: Any
    audience: str = "anyone"
    account: str | None = None
    address: Callable[[Any], str] = lambda chain: ""
    name_of: Callable[[str], str] = lambda address: ""

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
        words = [p.name if p.default is inspect.Parameter.empty else f"[{p.name}]" for p in params]
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
    """One console line, `name arg ...` as in a Core's console. An argument that reads as JSON is taken as JSON."""
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


def _value(word: str) -> Any:
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

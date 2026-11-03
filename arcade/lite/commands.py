"""Every Lite action, as a command: one list for the UI, the console, the terminal and the help.

A command is a function of the caller and its arguments that returns something JSON can carry. Its docstring is its
help: the first line is the summary, the rest the detail. Each command says who may run it: anyone who reaches the
arcade, a signed-in user (on their own account only), or the operator. `help` lists only what the caller may run.
"""
from __future__ import annotations

import inspect
import json
import shlex
from dataclasses import dataclass
from typing import Any, Callable


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

    @property
    def summary(self) -> str:
        return (inspect.getdoc(self.fn) or "").split("\n", 1)[0]

    @property
    def usage(self) -> str:
        params = list(inspect.signature(self.fn).parameters.values())[1:]   # the first is the caller
        words = [p.name if p.default is inspect.Parameter.empty else f"[{p.name}]" for p in params]
        return " ".join([self.name, *words])


COMMANDS: dict[str, Command] = {}


def command(category: str, audience: str = "anyone"):
    assert audience in AUDIENCES, audience

    def register(fn):
        COMMANDS[fn.__name__] = Command(fn.__name__, category, audience, fn)
        return fn
    return register


def run(caller: Caller, name: str, *args: Any) -> Any:
    cmd = COMMANDS.get(name)
    if cmd is None or not caller.may(cmd.audience):
        # One answer for both, so a visitor cannot list the operator's commands by trying names.
        raise CommandError(f"no command {name!r}; `help` lists them")
    try:
        inspect.signature(cmd.fn).bind(caller, *args)
    except TypeError:
        raise CommandError(f"usage: {cmd.usage}") from None
    return cmd.fn(caller, *args)


def available(caller: Caller) -> list[Command]:
    return sorted((c for c in COMMANDS.values() if caller.may(c.audience)), key=lambda c: (c.category, c.name))


def run_line(caller: Caller, line: str) -> Any:
    """One console line, `name arg ...` as in a Core's console. An argument that reads as JSON is taken as JSON."""
    try:
        words = shlex.split(line)
    except ValueError as exc:
        raise CommandError(str(exc)) from None
    if not words:
        raise CommandError("type a command; `help` lists them")
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
    return {"edition": "lite", "version": state.running_version, "face": brandlib.current().name,
            "operator": state.operator_tag() or None, "public_names": list(state.public_hosts),
            "mesh_id": getattr(mesh, "node_id", None)}


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

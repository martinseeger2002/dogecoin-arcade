"""Routes an inscription declares, answered by this node on its behalf.

An inscribed page can ask another page a question -- what is your power, are
you ready, whose turn is it -- and get an answer whether or not its holder is
sitting in front of the screen. That is what makes two NFTs able to interact
rather than merely be looked at.

**Declared, never executed.** A route is a small statement about where the
answer lives, and this module is a fixed interpreter over that statement. The
alternative -- an inscription shipping code that a stranger's node runs beside
their wallet -- is remote code execution on somebody else's machine, and no
sandbox written in an evening makes that safe. Everything here is a lookup
(D-091).

The vocabulary is deliberately tiny, and each entry answers one question:

    {"json": "stats.power"}   a value out of the inscription's own JSON
    {"store": "ready"}        a value the page saved through arcade.storage
    {"owner": true}           who holds it now
    {"number": true}          its inscription number
    {"const": 42}             a fixed answer

The declaration itself is inscribed, so what a piece will answer is public,
permanent and decided by whoever made it -- not by whoever holds it today. A
holder cannot quietly widen it, and a caller can read the spec before asking.

What a route CANNOT do is change anything. There are no writes here: a page
that wants to move a token or mint a piece files an approval and a person
says yes (arcade/approvals.py). Answers carry information; the chain carries
consequences.
"""

from __future__ import annotations

import json
from typing import Any

#: Where an inscription declares its routes, inside its own JSON.
ROUTES_KEY = "api"

#: What a route may say. One key each, so a spec is unambiguous.
VERBS = ("json", "store", "owner", "number", "collection", "const")

MAX_ROUTES = 32
MAX_NAME = 40
#: An answer travels in a node-to-node message, which is a chain transaction.
MAX_ANSWER = 2000


class ApiError(Exception):
    """The route does not exist, or cannot be answered."""


def declared(row: dict) -> dict[str, Any]:
    """The routes an inscription declares, or {} if it declares none.

    Anything malformed is nothing rather than an error: an inscription is
    permanent and may have been written by anyone, so a piece whose `api` is
    a string, a number or nonsense simply has no routes.
    """
    try:
        meta = json.loads(row.get("json") or "")
    except (TypeError, ValueError):
        return {}
    if not isinstance(meta, dict):
        return {}
    routes = meta.get(ROUTES_KEY)
    if not isinstance(routes, dict):
        return {}
    out = {}
    for name, spec in list(routes.items())[:MAX_ROUTES]:
        if not isinstance(name, str) or not name or len(name) > MAX_NAME:
            continue
        if isinstance(spec, dict) and len(spec) == 1 and next(iter(spec)) in VERBS:
            out[name] = spec
    return out


def answer(row: dict, route: str, store_items: dict | None = None) -> Any:
    """What this inscription answers for `route`.

    `store_items` is what the page has saved, read by the caller so this stays
    a pure function of things already in hand -- which is what makes it
    testable without a database and safe to call from the message handler.
    """
    routes = declared(row)
    spec = routes.get(str(route))
    if spec is None:
        raise ApiError(f"this piece answers no route called {route!r}")
    verb, argument = next(iter(spec.items()))

    if verb == "const":
        return _sized(argument)
    if verb == "owner":
        return row.get("owner")
    if verb == "number":
        return row.get("number")
    if verb == "collection":
        return {"collection": row.get("collection"), "edition": row.get("edition")}
    if verb == "store":
        return _sized((store_items or {}).get(str(argument)))
    if verb == "json":
        try:
            meta = json.loads(row.get("json") or "")
        except (TypeError, ValueError):
            return None
        return _sized(_dig(meta, str(argument)))
    raise ApiError(f"unknown route kind {verb!r}")


def _dig(value: Any, path: str) -> Any:
    """`stats.power` through nested objects. Missing is None, never an error:
    a page asking about a field a piece does not have should be told nothing
    rather than handed an exception to render."""
    for step in path.split("."):
        if not step:
            continue
        if isinstance(value, dict):
            value = value.get(step)
        elif isinstance(value, list) and step.isdigit() and int(step) < len(value):
            value = value[int(step)]
        else:
            return None
    return value


def _sized(value: Any) -> Any:
    """Refuse an answer too big to carry. A node-to-node message is a chain
    transaction; an answer that cannot fit is better refused here than
    truncated into something a caller would misread."""
    try:
        if len(json.dumps(value)) > MAX_ANSWER:
            raise ApiError(f"that answer is over {MAX_ANSWER} bytes")
    except (TypeError, ValueError):
        raise ApiError("that answer is not something this can send") from None
    return value

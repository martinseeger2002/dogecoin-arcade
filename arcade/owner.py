"""A node's owner, said by the owner's own key (2026-11-04, "Who runs this node", Plan B, chosen by SadFrog).

An arcade is two things: the place, its node's own key and @name, and the person who owns it, an account whose keys
are in its own browser. One person may own several nodes. Each of them keeps a statement the owner's wallet signed
in Admin -> This node:

    arcade-owner
    <network>        the messaging chain's network, where the owner's @name lives ("main", "test")
    <node>           the node's mesh id: its Ed25519 key, hex
    <address>        the owner's address on that chain, the one its @name names
    <expires>        unix seconds, at most MOST_DAYS ahead; Admin asks again RENEW_DAYS before

signed as Core's `signmessage` (coins.js signWords), so any arcade checks it from the bytes (llm.signed_by), with no
Core. The words name the node, so a statement is good for that node only and can be passed on by anyone unchanged: an
arcade that says it in its "me" frame (mesh/operators.py) proves only what the owner's key signed.

What it proves is "this key said it owns that node until then", nothing more: no payment, permission or vouching
reads it. Admin -> Your nodes lists the nodes whose statements name the same address as this one's.
"""
from __future__ import annotations

import re
import time
from typing import Callable

MOST_DAYS = 400
RENEW_DAYS = 30

_NET = re.compile(r"^[a-z0-9]{2,16}$")
_NODE = re.compile(r"^[0-9a-f]{64}$")
_ADDR = re.compile(r"^[0-9A-Za-z]{20,90}$")
_SIG = re.compile(r"^[A-Za-z0-9+/=]{40,120}$")


def words(network: str, node: str, address: str, expires: int) -> str:
    """The exact words the owner's key signs (coins.js ownerWords builds the same)."""
    return f"arcade-owner\n{network}\n{node}\n{address}\n{int(expires)}"


def shaped(said) -> dict | None:
    """A statement {a: address, x: expires, s: signature} as it travels, or None when it is not that shape."""
    if not isinstance(said, dict):
        return None
    address, expires, sig = said.get("a"), said.get("x"), said.get("s")
    if not (isinstance(address, str) and _ADDR.match(address) and isinstance(sig, str) and _SIG.match(sig)
            and isinstance(expires, int) and not isinstance(expires, bool) and 0 < expires < 2 ** 40):
        return None
    return {"a": address, "x": expires, "s": sig}


def check(said, network: str, node: str, verify: Callable[[str, str, str], bool] | None = None,
          now: float | None = None) -> str:
    """The owner's address when `said` is a good statement that `node` is owned, "" otherwise. `verify` is
    (address, signature, words) -> bool; by default the signature is checked from its bytes (llm.signed_by)."""
    if verify is None:
        from .llm import signed_by
        verify = lambda address, sig, text: signed_by(sig, text, address)  # noqa: E731
    now = time.time() if now is None else now
    statement = shaped(said)
    if statement is None or not _NET.match(network or "") or not _NODE.match(node or ""):
        return ""
    if not now < statement["x"] <= now + MOST_DAYS * 86400 + 3600:
        return ""
    try:
        good = verify(statement["a"], statement["s"], words(network, node, statement["a"], statement["x"]))
    except Exception:                                      # noqa: BLE001 -- unproven is refused
        good = False
    return statement["a"] if good else ""


def due(said, now: float | None = None) -> bool:
    """Whether Admin should ask the owner to sign again: no statement, or one within RENEW_DAYS of its end."""
    statement = shaped(said)
    now = time.time() if now is None else now
    return statement is None or statement["x"] - now < RENEW_DAYS * 86400

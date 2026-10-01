"""Game state on NFTs: a game's own small record for each piece, public and ordered.

2026-10-01: items wear out and get repaired, and "damaged items should remain
damaged when traded". So a piece can carry STATE -- a condition, a charge, a
count -- that belongs to one game, travels with the piece through every trade,
sale and escrow, can be changed only by that game, and is readable by anyone.

* **On the messaging chain, as a public announcement.** A batch of
  {piece, seq, state} for one game family, in one transaction, read by every
  node's scanner like an instance or mesh announcement. Not on the ledger: the
  token engine never sees it, so nothing here can stop an index, and older
  nodes simply skip a type they do not know.
* **Only the game's publisher counts.** The transaction's sender is proved by
  the chain (it spent that address's coin). A game names its publisher in its
  JSON; a record from any other sender is ignored when read, whoever paid
  for it.
* **Ordered, and no going back.** Each record carries a sequence number per
  game and piece, and the highest one the publisher wrote is the state. An
  older record replayed, or published again, changes nothing.
* **Keyed by the piece, not its owner**, which is the whole of "travels with
  it": nothing about a trade touches it.
* **Game-agnostic.** The state is a small JSON object the game defines; the
  arcade stores it, orders it and shows it with the labels the game asks for.

    payload = header || {"f": family, "u": [[piece txid, seq, {state}], ...]}
"""

from __future__ import annotations

import json
import re
from typing import Any

PREFIX_KEY = "f"
FAMILY = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
TXID = re.compile(r"^[0-9a-f]{64}$")
#: One piece's state, as compact JSON: small on purpose, a condition or a
#: few counters, not a save file.
STATE_BYTES = 96
#: Pieces in one announcement: what fits one Class B payload with room to spare.
PER_TX = 60


class StateError(ValueError):
    """Not a game-state record this arcade reads."""


def _compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def check(family: Any, updates: list) -> list[tuple[str, int, dict]]:
    """Validated (piece, seq, state) triples, or StateError saying why not."""
    if not isinstance(family, str) or not FAMILY.match(family):
        raise StateError("a game family is 1 to 64 letters, digits, . _ : -")
    if not isinstance(updates, list) or not 1 <= len(updates) <= PER_TX:
        raise StateError(f"a record names 1 to {PER_TX} pieces")
    out = []
    for u in updates:
        if not isinstance(u, (list, tuple)) or len(u) != 3:
            raise StateError("each piece is [txid, seq, state]")
        piece, seq, state = str(u[0]).lower(), u[1], u[2]
        if not TXID.match(piece):
            raise StateError("a piece is named by its 64-character txid")
        if not isinstance(seq, int) or isinstance(seq, bool) or not 0 < seq < 2 ** 53:
            raise StateError("a sequence number is a positive whole number")
        if not isinstance(state, dict) or len(_compact(state).encode()) > STATE_BYTES:
            raise StateError(f"a piece's state is a JSON object of at most {STATE_BYTES} bytes")
        out.append((piece, seq, state))
    return out


def build(family: str, updates: list) -> bytes:
    """The announcement: the arcade header, then the compact JSON body."""
    from .messaging.envelope import Header, TYPE_GAMESTATE
    triples = check(family, [list(u) for u in updates])
    body = _compact({"f": family, "u": [[p, s, st] for p, s, st in triples]})
    return Header(type=TYPE_GAMESTATE).encode() + body.encode()


def parse(payload: bytes) -> dict | None:
    """{family, updates: [(piece, seq, state)]} from a payload, or None."""
    from .messaging.envelope import EnvelopeError, Header, MAGIC, VERSION, TYPE_GAMESTATE
    if len(payload) < 7 or payload[:4] != MAGIC or payload[4] != VERSION:
        return None
    if payload[5] != TYPE_GAMESTATE:
        return None
    try:
        head = Header.decode(payload)
        said = json.loads(payload[head.length:].rstrip(b"\x00").decode("utf-8"))
        return {"family": said["f"], "updates": check(said["f"], said["u"])}
    except (EnvelopeError, ValueError, KeyError, TypeError, UnicodeDecodeError):
        return None


def batches(updates: list) -> list[list]:
    """Updates split into announcements of at most PER_TX pieces."""
    return [updates[i:i + PER_TX] for i in range(0, len(updates), PER_TX)]


def current(rows: list[dict], publisher: str) -> dict | None:
    """The state a game's publisher last wrote for one piece: the highest
    sequence number among ITS records, chain order breaking a tie. Anybody
    else's record is not the game's, and is ignored."""
    mine = [r for r in rows if r["sender"] == publisher]
    if not mine:
        return None
    best = max(mine, key=lambda r: (int(r["seq"]), int(r["height"] or 1 << 40),
                                    int(r["position"] or 0), r["txid"]))
    return {"state": json.loads(best["state"]), "seq": int(best["seq"]),
            "height": int(best["height"] or 0), "txid": best["txid"]}

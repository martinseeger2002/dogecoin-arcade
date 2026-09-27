"""Listings on the chain, so a mintpad sells on every node (2026-09-27).

A listing is a leg its seller signed SINGLE|ANYONECANPAY: the piece and a coin
in, the price out, and anybody may add the rest and complete it. Until now a
node kept the legs it was handed in its own `listings.sqlite`, so a mintpad
inscription opened on another node said "nothing left". The operator chose the chain
("On the chain, batched"): the seller inscribes their pad's signed legs as one
small inscription per batch, of type `CONTENT_TYPE`, and every node files what
it reads through `Listings.register` -- the same checks a leg posted by a
browser gets, including that the key signing it hashes to the address that
inscribed the batch. Nothing in a batch is trusted that the chain and the
leg's own bytes do not say.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from .listings import ListingError, _serialise

log = logging.getLogger(__name__)

CONTENT_TYPE = "application/vnd.arcade.listings+json"
#: Legs per inscription: a leg is ~400 bytes of hex, and one Class B chunk
#: carries about 7.6 KB, so a batch stays one transaction.
PER_BATCH = 14
#: How often the watcher looks for new batches, at most.
EVERY = 30


def _varint(raw: bytes, at: int) -> tuple[int, int]:
    first = raw[at]
    if first < 0xfd:
        return first, at + 1
    size = {0xfd: 2, 0xfe: 4, 0xff: 8}[first]
    return int.from_bytes(raw[at + 1:at + 1 + size], "little"), at + 1 + size


def _pushes(script: bytes) -> list[bytes]:
    out, at = [], 0
    while at < len(script):
        op = script[at]
        at += 1
        if op <= 75:
            size = op
        elif op == 0x4c:
            size, at = script[at], at + 1
        elif op == 0x4d:
            size, at = int.from_bytes(script[at:at + 2], "little"), at + 2
        else:
            raise ListingError("a listing's scriptSig is pushes only")
        out.append(script[at:at + size])
        at += size
    return out


def unsign(leg_hex: str) -> tuple[str, list[str], bytes]:
    """A signed leg as `Listings.register` takes it: the bytes without their
    scriptSigs, one signature per input, and the one public key behind them."""
    raw = bytes.fromhex(leg_hex)
    version = int.from_bytes(raw[:4], "little")
    count, at = _varint(raw, 4)
    inputs, sigs, keys = [], [], set()
    for _ in range(count):
        txid = raw[at:at + 32][::-1].hex()
        vout = int.from_bytes(raw[at + 32:at + 36], "little")
        size, at = _varint(raw, at + 36)
        script = raw[at:at + size]
        at += size + 4                                   # the sequence
        pushed = _pushes(script)
        if len(pushed) != 2:
            raise ListingError("a listing input is a signature and a key")
        sigs.append(pushed[0].hex())
        keys.add(pushed[1])
        inputs.append((txid, vout, b""))
    count, at = _varint(raw, at)
    outputs = []
    for _ in range(count):
        value = int.from_bytes(raw[at:at + 8], "little")
        size, at = _varint(raw, at + 8)
        outputs.append((value, raw[at:at + size]))
        at += size
    locktime = int.from_bytes(raw[at:at + 4], "little")
    if len(keys) != 1:
        raise ListingError("a listing is signed by one key")
    return _serialise(inputs, outputs, version, locktime), sigs, keys.pop()


def batch_json(network: str, rows: list[dict]) -> bytes:
    """What goes on the chain for some of one seller's listings."""
    return json.dumps({
        "arcade": "listings", "v": 1, "network": network,
        "listings": [{"leg": r["leg"], "price": int(r["price"]),
                      "expires": int(r["expires"])} for r in rows],
    }, separators=(",", ":")).encode()


def import_announced(state: Any, chain: Any) -> int:
    """File every listing announced on the chain since the last look. Returns
    how many were filed. A leg this node already holds, one whose coins are
    spent, or one that is not the inscriber's own is skipped."""
    index = state.token_index(chain)
    key = f"listings_import:{chain.network}"
    last = int(state.setting(key, 0) or 0)
    with index.open() as db:
        rows = db.conn.execute(
            "SELECT txid, number, creator FROM inscription WHERE content_type=? "
            "AND number > ? ORDER BY number LIMIT 200", (CONTENT_TYPE, last)).fetchall()
    filed = 0
    for row in rows:
        try:
            found = index.inscription_content(row["txid"])
            body = json.loads(found[1]) if found else {}
        except Exception:
            body = {}
        if body.get("arcade") == "listings" and body.get("network") == chain.network:
            for one in body.get("listings") or []:
                filed += _file_one(state, chain, row["creator"], one)
        last = max(last, int(row["number"]))
    if rows:
        state.set_setting(key, last)
    return filed


def _file_one(state: Any, chain: Any, owner: str, one: dict) -> int:
    leg = str(one.get("leg") or "")
    expires = float(one.get("expires") or 0)
    if not leg or expires <= time.time():
        return 0
    if state.listings.has_leg(chain.network, leg):
        return 0
    try:
        raw, sigs, pubkey = unsign(leg)
        with chain.rpc() as rpc:
            state.listings.register(
                rpc, raw=raw, signatures=sigs, pubkey=pubkey, network=chain.network,
                owner=owner, price=int(one.get("price") or 0),
                seconds=max(60.0, expires - time.time()))
        return 1
    except Exception as exc:                   # spent, moved, not theirs: not a listing
        log.debug("announced listing not filed: %s", exc)
        return 0

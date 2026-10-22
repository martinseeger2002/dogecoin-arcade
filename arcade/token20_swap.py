"""A DRC-20 / PRC-20 token in a swap's lock (2026-10-22: "Add prc 20 and drc 20 tokens to the token/coin
exchange" -- in the same swap book, cross-chain, whole orders).

A transfer note credits its tokens on its FIRST move (token20_send), so a note cannot simply be sent INTO a lock: that
first move would give the tokens to the lock's address, and the claim out of it would move nothing. So the seller:

1. inscribes note 1 (its amount) on its own address -- the commit, then the reveal (token20_send);
2. FUNDS the lock: one transaction moves note 1's coin to the lock (its first move: the lock's address now holds the
   tokens) and pays the commit of note 2 -- so note 2 can only come after the funding on the chain;
3. LOCKS: note 2's reveal, the same amount, lands on the lock itself (valid: the lock's address holds the tokens),
   its coin as output 0 and the reserve beside it as output 1 -- a Doginal lock's shape (htlc.doginal_out), so the
   claim and the refund are a Doginal's: the claim is note 2's first move, the tokens to the claimer; a refund, back
   to the seller.

The other side counts the lock only once a copy of the ord index has read note 2 as a valid note owned by the lock's
address (`token20_seen`): until then the lock is "unconfirmed", whatever its depth, so nobody locks against tokens the
rules might not count (somebody else's note pinned on the lock address first, say). Note 1's coin stays at the lock
address (0.001 of the coin), spent by nobody.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from . import htlc as H, ord_rules, token20_send as t20
from . import trade as T

COIN = 100_000_000


def lock_outputs(redeem: bytes, reserve: int) -> list[tuple[int, bytes]]:
    """The lock transaction's outputs: note 2's coin, then its reserve, both under the lock."""
    script = H.p2sh_script(redeem)
    return [(t20.POSTAGE, script), (int(reserve), script)]


def note_of(script_sig_hex: str, protocol: str, network: str) -> dict | None:
    """The DRC-20 / PRC-20 operation a reveal's input carries, as the chain's index reads it, or None."""
    try:
        state, ctype, body = ord_rules.parse([bytes.fromhex(script_sig_hex)], ord_rules.dialect_of(network))
    except (ValueError, TypeError):
        return None
    if state != "complete":
        return None
    return ord_rules.token_op(protocol, ctype, body)


def _same_amount(text: Any, whole: int) -> bool:
    try:
        return Decimal(str(text)) == Decimal(int(whole))
    except (InvalidOperation, ValueError):
        return False


def token20_seen(seen: T.LockSeen | None, leg: T.Leg, redeem: bytes, rpc: Any, index: Any,
                 params: Any) -> T.LockSeen | None:
    """For a DRC-20 / PRC-20 leg, whether the lock holds its note: output 0 and output 1 under the lock, the note in
    the reveal's input exactly the agreed tick and amount, and a copy of the ord index saying that note is valid and
    the lock address's (DoginalIndex "note"). Until a copy has read it the lock counts as unconfirmed (0), never as
    good; read and not valid (or not the lock's), it is wrong."""
    if seen is None or seen.spent_by_script_sig:
        return seen
    lock_tx = rpc.call("getrawtransaction", seen.txid, 1)
    script = H.p2sh_script(redeem).hex()
    outs = lock_tx.get("vout") or []
    if seen.vout != 0 or len(outs) < 2 or any((o.get("scriptPubKey") or {}).get("hex") != script for o in outs[:2]):
        seen.asset_ok = False
        return seen
    seen.reserve = round(float(outs[1]["value"]) * COIN)
    op = note_of((lock_tx["vin"][0].get("scriptSig") or {}).get("hex", ""), t20.PROTOCOL[leg.network], leg.network)
    if not op or op.get("op") != "transfer" or str(op.get("tick", "")).lower() != leg.asset \
            or not _same_amount(op.get("amt"), leg.amount):
        seen.asset_ok = False
        return seen
    try:
        said = index.lookup({"what": "note", "id": f"{seen.txid}i0"})
    except Exception:                                   # noqa: BLE001 -- no copy could say: wait
        seen.confirmations = 0
        return seen
    height = None
    if seen.confirmations:
        try:
            height = int(rpc.get_block_count()) - int(seen.confirmations) + 1
        except Exception:                               # noqa: BLE001
            height = None
    if height is None or int(said.get("walked_to") or 0) <= height:
        seen.confirmations = 0                          # no copy has read the lock's block yet
        return seen
    note = said.get("note")
    seen.asset_ok = bool(note) and note.get("owner") == H.address_of(redeem, params) \
        and str(note.get("tick", "")).lower() == leg.asset
    return seen

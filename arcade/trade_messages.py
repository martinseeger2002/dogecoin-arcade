"""What two traders say to each other, carried by the PEP testnet Messenger.

2026-10-13: "Can't we use the Pepe coin test net node to node communication
protocol? That that's already built in." Yes: a trade step is an API message
(messaging/api.py) -- sealed to the other trader's messaging key, authenticated as from
the sender, carried as one testnet transaction that every node reads from the mempool in
seconds. A browser tab seals its own (/account/talk) and its node never reads them; a
facilitating node seals with its own identity.

What a message is NOT: evidence. Every step here is a pointer or a proposal -- "my lock
is at txid:vout", "these are the terms" -- and the state machine (arcade/trade.py) acts
only on what the chains confirm, checked against terms THIS trader agreed. A message
that lies can make a trader wait or walk away; it can never make one lose coins.

    hello    {order}                         is the lister there? (before anything is signed)
    here     {order}                         yes
    propose  {order, give, get, secret_hash, t1, t2}   the initiator's terms
    accept   {}                              the responder agrees to exactly those
    locked   {network, txid, vout}           where my lock is (look for yourself)
    claimed  {network, txid}                 I claimed yours (the secret is in that tx)
    refunded {network, txid}                 mine came back to me
    cancel   {reason}                        I am walking away (before locking: costs nothing)
"""

from __future__ import annotations

import json
import re
from typing import Any

from .config import NETWORKS
from .doginals import INSCRIPTION_ID
from .trade import Leg

KIND = "trade"
VERSION = 1
STEPS = ("hello", "here", "propose", "accept", "locked", "claimed", "refunded", "cancel")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_KEY = re.compile(r"^0[23][0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
#: A trade message is a small thing; anything near the Messenger's one-transaction
#: limit is not a trade step.
MAX_BYTES = 4000


class TradeMessageError(ValueError):
    """Not a trade step this version understands."""


def build(step: str, trade_id: str, **fields: Any) -> bytes:
    """One step, as the bytes an API message carries. Checked on the way out exactly as
    `parse` checks it on the way in, so a node never sends what another would refuse."""
    body = {"t": KIND, "v": VERSION, "id": trade_id, "step": step, **fields}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    parse(raw)
    return raw


def parse(raw: bytes) -> dict:
    """The step, validated field by field, or TradeMessageError."""
    if len(raw) > MAX_BYTES:
        raise TradeMessageError("a trade step is small; this is not one")
    try:
        said = json.loads(raw)
    except ValueError:
        raise TradeMessageError("not JSON") from None
    if not isinstance(said, dict) or said.get("t") != KIND:
        raise TradeMessageError("not a trade message")
    if said.get("v") != VERSION:
        raise TradeMessageError(f"trade message version {said.get('v')!r}, this node speaks {VERSION}")
    step = said.get("step")
    if step not in STEPS:
        raise TradeMessageError(f"unknown trade step {step!r}")
    if not isinstance(said.get("id"), str) or not _ID.match(said["id"]):
        raise TradeMessageError("a trade id is 8-64 letters, digits, - or _")
    check = _CHECKS[step]
    check(said)
    return said


def leg_json(leg: Leg) -> dict:
    return {"network": leg.network, "kind": leg.kind, "asset": leg.asset,
            "amount": int(leg.amount), "giver_key": leg.giver_key, "taker_key": leg.taker_key}


def leg_from(said: Any) -> Leg:
    if not isinstance(said, dict):
        raise TradeMessageError("a leg is an object")
    network = said.get("network")
    if network not in NETWORKS:
        raise TradeMessageError(f"no such network {network!r}")
    kind = said.get("kind")
    if kind not in ("coin", "token", "nft", "doginal"):
        raise TradeMessageError("a leg moves a coin, a token, an nft or a doginal")
    amount = said.get("amount")
    if not isinstance(amount, int) or amount <= 0 or amount > 21 * 10 ** 18:
        raise TradeMessageError("a leg's amount is a positive whole number")
    asset = str(said.get("asset", ""))
    if kind == "coin" and asset:
        raise TradeMessageError("a coin leg names no asset")
    if kind == "token" and not asset.isdigit():
        raise TradeMessageError("a token leg names its property id")
    if kind == "nft" and (not _HEX64.match(asset) or amount != 1):
        raise TradeMessageError("an nft leg names its inscription and moves exactly one")
    if kind == "doginal" and (network != "doge" or not INSCRIPTION_ID.match(asset) or amount != 1):
        raise TradeMessageError("a doginal leg is on Dogecoin, names its inscription id and moves exactly one")
    for side in ("giver_key", "taker_key"):
        if not isinstance(said.get(side), str) or not _KEY.match(said[side]):
            raise TradeMessageError(f"a leg's {side} is a compressed public key, in hex")
    if said["giver_key"] == said["taker_key"]:
        raise TradeMessageError("a leg goes from one person to another")
    return Leg(network, kind, asset, amount, said["giver_key"], said["taker_key"])


def _order(said: dict) -> None:
    if not isinstance(said.get("order"), str) or not _ID.match(said["order"]):
        raise TradeMessageError("an order id is 8-64 letters, digits, - or _")


def _propose(said: dict) -> None:
    _order(said)
    leg_from(said.get("give")), leg_from(said.get("get"))
    # No rule that each person uses ONE key on both legs: a browser wallet has a key per
    # chain (coin type 1 on testnet, 3 on mainnet and Dogecoin). Each lock names its own
    # two keys, and each is checked on its own chain against these terms.
    if not isinstance(said.get("secret_hash"), str) or not _HEX64.match(said["secret_hash"]):
        raise TradeMessageError("the secret's hash is 32 bytes, in hex")
    t1, t2 = said.get("t1"), said.get("t2")
    if not (isinstance(t1, int) and isinstance(t2, int) and 500_000_000 <= t2 < t1 < 2 ** 32):
        raise TradeMessageError("lock times are times, the responder's before the initiator's")


def _pointer(said: dict) -> None:
    if said.get("network") not in NETWORKS:
        raise TradeMessageError("a pointer names its network")
    if not isinstance(said.get("txid"), str) or not _HEX64.match(said["txid"]):
        raise TradeMessageError("a pointer names its txid")
    if said["step"] == "locked" and not (isinstance(said.get("vout"), int) and 0 <= said["vout"] < 10_000):
        raise TradeMessageError("a lock pointer names its output")


def _cancel(said: dict) -> None:
    if not isinstance(said.get("reason", ""), str) or len(said.get("reason", "")) > 200:
        raise TradeMessageError("a reason is a short sentence")


_CHECKS = {"hello": _order, "here": _order, "propose": _propose, "accept": lambda s: None,
           "locked": _pointer, "claimed": _pointer, "refunded": _pointer, "cancel": _cancel}


def same_terms(said: dict, trade: Any) -> bool:
    """Does a `propose` say exactly the trade this trader would agree to? From the
    RESPONDER's side: what it gives is the proposal's `get`, what it gets the `give`."""
    try:
        return (leg_from(said["give"]) == trade.get and leg_from(said["get"]) == trade.give
                and said["secret_hash"] == trade.secret_hash
                and said["t1"] == trade.initiator_locktime and said["t2"] == trade.responder_locktime)
    except (KeyError, TradeMessageError):
        return False

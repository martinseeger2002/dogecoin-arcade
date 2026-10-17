"""A node's half of trade messaging: send a step, read the steps that came back.

The carriage is the Messenger's node-to-node API message (messaging/api.py), exactly as
an inscribed page's `/node` talk uses it -- one testnet transaction, sealed to the
recipient's messaging key, authenticated as this node (2026-10-13: "Can't we use
the Pepe coin test net node to node communication protocol?"). A browser tab trading
for itself seals its own steps in the tab (/account/talk) and never needs this; this is
for a FACILITATING node's trader (arcade/node_trader.py, phase 5) and for tests.

Nothing read here is believed: `inbox` hands back parsed steps with who sent them, and
the trade state machine checks every one against the chains (arcade/trade.py).
"""

from __future__ import annotations

from typing import Any

from . import trade_messages as M
from .messaging import api as apilib
from .messaging.keys import fingerprint_of
from .messaging.sender import MessageSender, SendError, funded_address


def send(state: Any, chain: Any, to_pub: bytes, body: bytes) -> str:
    """One trade step to another trader's messaging key, as this node. Returns the txid.
    `body` is a `trade_messages.build(...)`: parsed again here, so nothing malformed
    ever leaves."""
    M.parse(body)
    identity = state.ensure_identity()
    payload = apilib.seal(identity, to_pub, body)
    with chain.rpc() as rpc:
        sender = MessageSender(rpc, chain.params)
        address = funded_address(rpc, prefer=state.derived_address)
        try:
            prepared = sender.prepare(address, payload)
        except SendError:
            # A message is funded from ordinary pay-to-pubkey-hash coins only (a reader
            # works the sender out of them); fall back to the address holding the most of
            # THOSE -- not the most coins, which on a node that mines is mined coins.
            other = _message_address(rpc)
            if not other or other == address:
                raise
            prepared = sender.prepare(other, payload)
        return sender.broadcast(prepared)


def _message_address(rpc: Any) -> str:
    """The wallet address holding the most coins a message can be paid from."""
    totals: dict[str, float] = {}
    for coin in rpc.call("listunspent", 1, 9_999_999) or []:
        script = str(coin.get("scriptPubKey", ""))
        if coin.get("address") and script.startswith("76a914") and script.endswith("88ac"):
            totals[coin["address"]] = totals.get(coin["address"], 0.0) + float(coin["amount"])
    return max(totals, key=totals.get) if totals else ""


def inbox(state: Any, chain: Any, after_id: int = 0, limit: int = 200) -> list[dict]:
    """Trade steps addressed to this node since `after_id`, oldest first:
    [{id, sender (hex messaging key), txid, height, step (the parsed dict)}].
    Anything that is not a well-formed trade step is skipped, not raised: the API
    channel carries other programs' traffic too."""
    identity = state.ensure_identity()
    out: list[dict] = []
    with state.store() as store:
        rows = store.api_messages(fingerprint_of(identity.public_bytes), chain.network,
                                  after_id=after_id, limit=limit)
    for row in rows:
        try:
            step = M.parse(bytes(row["body"]))
        except M.TradeMessageError:
            continue
        out.append({"id": row["id"], "sender": bytes(row["sender_pubkey"]).hex(),
                    "txid": row["txid"], "height": row["height"], "step": step})
    return out

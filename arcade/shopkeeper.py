"""The seller's side of a swap, answered without anybody pressing anything.

Why nobody is asked
-------------------
Every send this wallet makes for somebody else goes through the approvals
queue (approvals.py): the owner looks at the transaction and says yes. An
order at a shop is different in one way that matters -- the owner already
said yes, in writing, when they inscribed the shop. The listing names what is
handed over and what is taken for it; the shopkeeper adds nothing to that.
It offers exactly what the JSON says, checks the buyer's half of the
transaction against exactly what it offered (swap.countersign), and signs
only then. What it cannot do is sell anything the JSON does not list, sell
it for less, or sell from a shop this wallet did not create -- and the
engine on every node checks the transaction again when it lands.

What it does, once per tick
---------------------------
Runs from the block watcher's thread (web/watcher.py), a few seconds apart:

  1. expires offers nobody took up, unlocking their outputs;
  2. reads the node-to-node messages that arrived since it last looked, by
     its own cursor (swap.Offers.cursor);
  3. answers each `{"swap": "offer"}` with an offer or a refusal, and each
     `{"swap": "sign"}` by countersigning and broadcasting, or refusing;
  4. moves the cursor past what it read.

Every answer is a node-to-node message sealed to the key the question came
from, carrying `re` (the txid of the question) and the offer id, so the
buyer's page can match it (nodetalk.replies). A message that is not about a
swap is left for whoever it is for.

Testnet only: node-to-node messages are (D-010), and so, therefore, is this.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from . import swap as swaplib
from .messaging import api as apilib
from .messaging.keys import fingerprint_of
from .messaging.sender import MessageSender, SendError, funded_address

log = logging.getLogger(__name__)

#: How many inbox rows one tick looks at. An inbox that filled while the
#: wallet was off is worked through over a few ticks, not in one.
BATCH = 100


class Shopkeeper:
    """Answers orders for every shop this wallet created and still holds."""

    def __init__(self, state: Any):
        self.state = state
        self._warned = False

    def tick(self) -> int:
        """One pass; returns how many swap messages were answered.

        Never raises: the watcher must not die, and a node that is down is
        the next tick's problem.
        """
        state = self.state
        chain = state.messaging
        if chain.network == "main" or chain.params.swaps_from is None:
            return 0
        try:
            identity = state.ensure_identity()
        except Exception:
            return 0                # the node is not up, or has no wallet yet
        offers = state.offers
        answered = 0
        try:
            with chain.rpc() as rpc:
                swaplib.expire(rpc, offers, chain.network)
                index = state.token_index(chain)
                with state.store() as store:
                    last = offers.cursor(chain.network)
                    if last is None:
                        # First run on this chain: what arrived before there
                        # was a shopkeeper was not an order to it.
                        last = int(store.conn.execute(
                            "SELECT COALESCE(MAX(id), 0) FROM api_message").fetchone()[0])
                        offers.set_cursor(chain.network, last)
                        return 0
                    rows = store.api_messages(fingerprint_of(identity.public_bytes),
                                              chain.network, after_id=last, limit=BATCH)
                    handled: list[int] = []
                    for row in rows:
                        last = int(row["id"])
                        question = _swap_message(row)
                        if question is None:
                            continue
                        answer = self._answer(rpc, index, offers, chain, row, question)
                        try:
                            self._reply(rpc, chain, identity, bytes(row["sender_pubkey"]),
                                        answer)
                            answered += 1
                        except (SendError, apilib.ApiMessageError, ValueError) as exc:
                            log.warning("could not answer a swap message: %s", exc)
                        handled.append(int(row["id"]))
                    if handled:
                        store.mark_api_read(handled)
                    offers.set_cursor(chain.network, last)
        except Exception:
            log.debug("shopkeeper tick failed", exc_info=True)
        return answered

    # --- answering ---------------------------------------------------------

    def _answer(self, rpc: Any, index: Any, offers: Any, chain: Any, row: Any,
                question: dict) -> dict:
        state = self.state
        kind = question["swap"]
        reply: dict[str, Any] = {"swap": kind, "swapv": swaplib.PROTOCOL,
                                 "re": row["txid"], "ok": False,
                                 "version": state.running_version}
        if question.get("swapv") != swaplib.PROTOCOL:
            reply["error"] = (f"this shop speaks swap protocol {swaplib.PROTOCOL}, "
                              f"not {question.get('swapv')!r}; update one of us")
            return reply
        try:
            if kind == "offer":
                shop = index.inscription(str(question.get("shop") or ""))
                if shop is None:
                    raise swaplib.SwapError("no such inscription on this node")
                from .web.app import _ledger_addresses
                offer = swaplib.make_offer(
                    rpc, index, offers, chain.network, shop,
                    int(question.get("listing", -1)), str(question.get("buyer") or ""),
                    bytes(row["sender_pubkey"]).hex(), own=_ledger_addresses(rpc))
                reply.update(ok=True, offer=offer)
            else:
                offer = offers.get(str(question.get("offer") or ""))
                if offer is None:
                    raise swaplib.SwapError("no such offer")
                reply["offer"] = offer["id"]
                if offer["buyer_pubkey"] != bytes(row["sender_pubkey"]).hex():
                    raise swaplib.SwapError("that offer was made to somebody else")
                txid = swaplib.countersign(rpc, index, offers, offer,
                                           str(question.get("hex") or ""))
                reply.update(ok=True, txid=txid)
                state.bump_generation()
        except (swaplib.SwapError, ValueError) as exc:
            reply["error"] = str(exc)
        except Exception as exc:
            log.warning("swap %s failed: %s", kind, exc, exc_info=True)
            reply["error"] = f"the shop's node could not do it: {exc}"
        return reply

    def _reply(self, rpc: Any, chain: Any, identity: Any, to: bytes, body: dict) -> str:
        payload = apilib.seal(identity, to, json.dumps(body, separators=(",", ":")).encode())
        sender = MessageSender(rpc, chain.params)
        address = funded_address(rpc, prefer=self.state.derived_address)
        try:
            prepared = sender.prepare(address, payload)
        except SendError:
            other = funded_address(rpc)
            if other == address:
                raise
            prepared = sender.prepare(other, payload)
        return sender.broadcast(prepared)


def _swap_message(row: Any) -> dict | None:
    """The question in an inbox row, if it is a swap question at all."""
    # The stored body is the message after its stamp (scanner.py); the
    # protocol and API fingerprint are columns of their own.
    try:
        data = json.loads(bytes(row["body"]).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or data.get("swap") not in ("offer", "sign"):
        return None
    return data

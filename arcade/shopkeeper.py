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
                        if answer is None:
                            # Written down, and nobody to answer: an offer on
                            # something of ours waits for a person (D-038).
                            handled.append(int(row["id"]))
                            continue
                        try:
                            bid_id = answer.pop("bid", "")
                            fill_id = answer.pop("fill", "")
                            self._reply(rpc, chain, identity, bytes(row["sender_pubkey"]),
                                        answer)
                            # Marked once the half has actually gone, never
                            # before: a record that says "signed" for a reply
                            # that never went out is a lie about what this
                            # wallet did.
                            if bid_id:
                                offers.close_bid(bid_id, "signed")
                                state.bump_generation()
                            if fill_id:
                                offers.close_fill(fill_id, "signed",
                                                  offer_id=str(answer.get("offer") or ""))
                                state.bump_generation()
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
                question: dict) -> dict | None:
        state = self.state
        kind = question["swap"]
        reply: dict[str, Any] = {"swap": kind, "swapv": swaplib.PROTOCOL,
                                 "re": row["txid"], "ok": False,
                                 "version": state.running_version}
        if question.get("swapv") != swaplib.PROTOCOL:
            reply["error"] = (f"this shop speaks swap protocol {swaplib.PROTOCOL}, "
                              f"not {question.get('swapv')!r}; update one of us")
            return reply
        if kind == "bid":
            return self._bid(rpc, index, offers, chain, row, question)
        if kind == "fill" and "ok" in question:
            return self._fill(rpc, index, offers, chain, row, question)
        try:
            if kind == "fill":
                # Somebody taking a price off this wallet's book. What they
                # cannot work out alone is which of this wallet's outputs
                # carries the swap; the price comes from the order, never
                # from the question (D-063).
                from .web.app import _ledger_addresses
                offer = swaplib.offer_for_order(
                    rpc, index, offers, chain.network,
                    str(question.get("order") or ""), int(question.get("tokens", 0)),
                    str(question.get("buyer") or ""),
                    bytes(row["sender_pubkey"]).hex(), own=_ledger_addresses(rpc))
                reply.update(ok=True, offer=offer)
            elif kind == "offer":
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
                _write_it_down(state, chain, offer, txid)
                state.bump_generation()
        except (swaplib.SwapError, ValueError) as exc:
            reply["error"] = str(exc)
        except Exception as exc:
            log.warning("swap %s failed: %s", kind, exc, exc_info=True)
            reply["error"] = f"the shop's node could not do it: {exc}"
        return reply

    def _bid(self, rpc: Any, index: Any, offers: Any, chain: Any, row: Any,
             question: dict) -> dict | None:
        """An offer on an NFT, or the answer to one this wallet made.

        A bid is written down and left for a person: it is an offer on
        something of theirs, and nobody but them can say yes. The ANSWER to
        a bid is different -- the person already said what they would pay
        when they made it, so the wallet signs its half without asking
        again, and only if the terms are the ones it offered (D-038).
        """
        state = self.state
        bid_id = str(question.get("id") or "")
        if not bid_id:
            return None
        if "ok" not in question:
            # An offer ASKED for by message. Offers are said on the chain now
            # (D-042), where they reach a holder who never published a key --
            # so this is an older peer, and there is nothing to answer: the
            # Exchange shows what the chain holds, not what arrives here.
            return None

        mine = offers.get_bid(bid_id)
        if mine is None:
            # No note of it here, but an offer is on the chain and the chain
            # is what an answer is checked against. A wallet reinstalled
            # since it made the offer, or one whose note was never written
            # (it was not, for a day -- D-049), still knows what it asked
            # for.
            mine = _bid_from_chain(rpc, index, bid_id)
        if mine is None or mine["direction"] != "out" or mine["status"] != "open":
            return None
        if not question.get("ok"):
            offers.close_bid(bid_id, "refused",
                             error=str(question.get("error") or "refused"))
            state.bump_generation()
            return None
        reply = {"swap": "sign", "swapv": swaplib.PROTOCOL}
        try:
            offer = swaplib.check_offer(
                question.get("offer"), shop=mine["inscription"],
                own=_own_addresses(rpc), height=index.indexed_height(),
                params=chain.params)
            if offer["seller"] != mine["owner"]:
                raise swaplib.SwapError("that answer is not from the wallet that "
                                        "holds it")
            if offer["give"].get("txid") != mine["inscription"]:
                raise swaplib.SwapError("that is not the item that was offered for")
            if not _same_price(offer["take"], mine["take"]):
                raise swaplib.SwapError("that is not the price that was offered")
            built = swaplib.build(rpc, index, offer, own=_own_addresses(rpc))
        except (swaplib.SwapError, ValueError) as exc:
            offers.close_bid(bid_id, "failed", error=str(exc))
            state.bump_generation()
            return None
        # Marked when the answer is handed to the sender, not before: a bid
        # closed as "signed" whose reply never went out is a lie about what
        # this wallet did. The tick loop sends what is returned here, so the
        # bid is closed by the caller once it has.
        reply.update(offer=offer["id"], hex=built.hex, bid=bid_id)
        return reply

    def _fill(self, rpc: Any, index: Any, offers: Any, chain: Any, row: Any,
              question: dict) -> dict | None:
        """The answer to a fill this wallet asked for.

        The person already said what they would take and at what price when
        they pressed Take, so the wallet signs its half without asking again
        -- and only if the answer is the one it asked for (D-038). Checked
        against two things that cannot both be forged: the note this wallet
        wrote before the question went out, and the order as this node's own
        index reads it off the chain.
        """
        state = self.state
        fill_id = str(question.get("re") or "")
        mine = offers.get_fill(fill_id) if fill_id else None
        if mine is None or mine["status"] != "asked":
            return None                   # not ours, or already dealt with
        if not question.get("ok"):
            offers.close_fill(fill_id, "refused",
                              error=str(question.get("error") or "refused"))
            state.bump_generation()
            return None

        reply = {"swap": "sign", "swapv": swaplib.PROTOCOL}
        try:
            offer = swaplib.check_offer(
                question.get("offer"), shop="", own=_own_addresses(rpc),
                height=index.indexed_height(), params=chain.params)
            order = index.order(mine["order"])
            if order is None:
                raise swaplib.SwapError("that order is no longer on this node's book")
            if offer["seller"] != order["address"] or offer["seller"] != mine["maker"]:
                raise swaplib.SwapError("that answer is not from the wallet whose "
                                        "order this is")
            if offer.get("order") and offer["order"] != mine["order"]:
                raise swaplib.SwapError("that answer is for a different order")
            if offer["give"].get("kind") != "token" \
                    or int(offer["give"].get("propertyid") or 0) != order["sale_property"] \
                    or int(offer["give"].get("units") or 0) != int(mine["tokens"]):
                raise swaplib.SwapError("that is not the amount that was asked for")
            if offer["take"].get("kind") != "coins":
                raise swaplib.SwapError("an order is filled with coins")
            asked = int(offer["take"].get("sats") or 0)
            # Never more than this wallet worked out for itself from the order
            # on the chain. The maker names the price in its answer; this is
            # what stops the answer naming a different one.
            if asked > int(mine["coins"]):
                raise swaplib.SwapError(
                    f"that answer asks {asked} satoshis for what this wallet "
                    f"priced at {mine['coins']} from the order on the chain")
            built = swaplib.build(rpc, index, offer, own=_own_addresses(rpc),
                                  from_order=order)
        except (swaplib.SwapError, ValueError) as exc:
            offers.close_fill(fill_id, "failed", error=str(exc))
            state.bump_generation()
            return None
        reply.update(offer=offer["id"], hex=built.hex, fill=fill_id)
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


def _write_it_down(state: Any, chain: Any, offer: dict, txid: str) -> None:
    """A sale the shopkeeper made goes in the approvals book, already done.

    Nobody was asked -- the owner said yes when they inscribed the shop
    (D-024) -- but Approvals is where a person looks to see what this wallet
    signed for something other than a person, so a sale belongs there beside
    the sends a page asked for. It is written after the broadcast: a sale
    that happened and was not written down is worse than the other way round,
    and a book that cannot be written must not undo a sale (D-029).
    """
    try:
        state.approvals.record(
            chain.network, "swap", "shop", offer["buyer"], txid=txid,
            fromaddress=offer["seller"], offer=offer, page=offer["shop"],
            peer=offer.get("buyer_pubkey", ""))
    except Exception:
        log.warning("a sale was made but could not be written down", exc_info=True)


def _bid_from_chain(rpc: Any, index: Any, txid: str) -> dict | None:
    """An offer this wallet made, read back off the chain.

    Returns it in the shape a local bid row has, so the check that follows
    does not care where the terms came from -- only that they are the terms
    that were offered.
    """
    try:
        found = index.offer(txid)
    except Exception:
        return None
    if found is None or found["buyer"] not in _own_addresses(rpc):
        return None
    take = {"kind": {2: "token", 3: "coins"}.get(int(found["take_kind"]), "unknown"),
            "propertyid": found["take_property"] or 0}
    if take["kind"] == "token":
        take["units"] = int(found["take_amount"])
    else:
        take["sats"] = int(found["take_amount"])
    return {"id": txid, "direction": "out", "status": "open",
            "inscription": found["inscription"], "owner": found["owner"],
            "buyer": found["buyer"], "take": take, "peer_pubkey": ""}


def _own_addresses(rpc: Any) -> list[str]:
    from .web.app import _ledger_addresses
    return _ledger_addresses(rpc)


def _same_price(offered: dict, wanted: dict) -> bool:
    """Whether two legs name the same thing and the same amount.

    Compared on the numbers, never on the words: "2" and "2.00000000" are
    the same price, and a check that said otherwise would refuse a
    perfectly good answer over how it was written down.
    """
    for key in ("kind", "propertyid", "txid"):
        if str(offered.get(key) or "") != str(wanted.get(key) or ""):
            return False
    for key in ("sats", "units"):
        if int(offered.get(key) or 0) != int(wanted.get(key) or 0):
            return False
    return True


def _swap_message(row: Any) -> dict | None:
    """The question in an inbox row, if it is a swap question at all."""
    # The stored body is the message after its stamp (scanner.py); the
    # protocol and API fingerprint are columns of their own.
    try:
        data = json.loads(bytes(row["body"]).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or data.get("swap") not in ("offer", "sign", "bid", "fill"):
        return None
    if data.get("swap") in ("bid", "fill"):
        # A bid and its answer are both questions to the wallet that gets
        # them: one asks a person, the other asks the wallet to sign what
        # that person already agreed to. Neither can be answered by an
        # answer, so neither can loop.
        return data
    if "re" in data or "ok" in data:
        # An answer, not an order. Answers carry `re` (the txid they answer)
        # and `ok`; a question carries neither. Without this, two shopkeepers
        # answer each other's answers for ever -- each refusal is read as a
        # fresh order and refused in turn, a message a block out of each
        # wallet until somebody stops a node (D-027).
        return None
    return data

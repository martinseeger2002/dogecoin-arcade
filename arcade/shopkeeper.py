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
  4. answers an ANSWER to this wallet's own offer that arrives as a signed leg
     by finishing that trade out of its own coins (`_fill_a_leg`), or refuses it;
  5. moves the cursor past what it read.

Every answer is a node-to-node message sealed to the key the question came
from, carrying `re` (the txid of the question) and the offer id, so the
buyer's page can match it (nodetalk.replies). A message that is not about a
swap is left for whoever it is for.

Testnet only: node-to-node messages are (D-010), and so, therefore, is this.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from typing import Any

from . import fees
from . import funding as fundinglib
from . import listings as listingslib
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
        #: Orders already asked about; see `_asked_already` (D-119).
        self._tried: dict[tuple, float] = {}

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
                    newest = int(store.conn.execute(
                        "SELECT COALESCE(MAX(id), 0) FROM api_message").fetchone()[0])
                    if last is None:
                        # First run on this chain: what arrived before there
                        # was a shopkeeper was not an order to it.
                        offers.set_cursor(chain.network, newest)
                        return 0
                    if last > newest:
                        # The cursor is ahead of the store, so the store is not
                        # the one the cursor was counting: it has been moved
                        # aside and rebuilt -- which is what a chain reset asks
                        # every node to do (D-106). Left alone, the shopkeeper
                        # would answer nothing until the new store had grown
                        # past the old one's last id, deaf for thousands of
                        # messages and silent about it. Treated as first run.
                        log.info("swap cursor %d is past the store's %d; the "
                                 "message store was rebuilt", last, newest)
                        offers.set_cursor(chain.network, newest)
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

    # --- selling at a price already published --------------------------------

    def sell_at_asking_price(self) -> int:
        """Accept offers that meet a price this wallet has already asked.

        The same reason nothing is asked for a shop's order: the owner
        already said yes, in writing, where everyone can see it. An ask IS
        that yes -- it names the piece and the price, on the chain, signed by
        the address that holds it -- and this accepts exactly it and nothing
        else (D-101).

        What it will not do, each refusal being the thing somebody would
        worry about:

        * sell for less than was asked, or in a currency that was not asked
          for -- the comparison is on the numbers, per kind, and a token
          price and a coin price are not comparable at all;
        * sell a piece whose ask is not live: `ledger.asks` already drops
          one whose seller no longer holds the piece, or that has been
          withdrawn, or that a newer ask has replaced;
        * sell the same piece twice: `offer_for_bid` refuses while another
          buyer's offer is still reserved against it (D-049);
        * act on an offer that is only in the mempool. A block is what makes
          an offer a fact, and the few minutes cost nothing: the payment
          rides in the swap, not in the offer.

        Off by a switch on the Overview, like automatic updates, because
        somebody may want to look at every sale first -- but on by default,
        since a price said in public that the seller then ignores is worse
        than no price at all.
        """
        state = self.state
        chain = state.messaging
        if chain.network == "main" or chain.params.swaps_from is None:
            return 0
        if chain.params.asks_from is None:
            return 0
        if state.setting("auto_sell", True) is False:
            return 0
        sold = 0
        try:
            identity = state.ensure_identity()
            index = state.token_index(chain)
            with chain.rpc() as rpc:
                own = _own_addresses(rpc)
                mine = {ask["inscription"]: ask for ask in index.asks(limit=200)
                        if ask["seller"] in set(own)}
                if not mine:
                    return 0
                best: dict[str, dict] = {}
                for offer in index.offers_on(sorted(own)):
                    ask = mine.get(offer["inscription"])
                    if ask is None or not _meets_the_ask(ask, offer):
                        continue
                    # The best price wins, and at the same price the one that
                    # was made first: the same rule the token book follows,
                    # and the one anybody queueing behind somebody expects
                    # (D-083).
                    standing = best.get(offer["inscription"])
                    if standing is None or _better_offer(offer, standing):
                        best[offer["inscription"]] = offer
                for inscription, offer in best.items():
                    if self._accept(rpc, index, chain, identity, offer):
                        sold += 1
        except Exception:
            log.debug("selling at the asking price failed", exc_info=True)
        return sold

    def _accept(self, rpc: Any, index: Any, chain: Any, identity: Any,
                offer: dict) -> bool:
        """Answer one offer with this wallet's half of the swap."""
        state = self.state
        try:
            with state.store() as store:
                key = store.key_for(offer["buyer"])
            if key is None:
                # Nobody to answer. Their offer stands and a person can still
                # accept it by hand once they publish a key (D-042).
                return False
            peer = bytes(key["pubkey"])
            bid = {"inscription": offer["inscription"],
                   "take": _take_of(offer, index),
                   "buyer": offer["buyer"], "peer_pubkey": peer.hex()}
            from .web.app import _node_cut
            half = swaplib.offer_for_bid(rpc, index, state.offers, chain.network,
                                         bid, own=_own_addresses(rpc),
                                         cut=_node_cut(state))
            self._reply(rpc, chain, identity, peer,
                        {"swap": "bid", "swapv": swaplib.PROTOCOL,
                         "id": offer["txid"], "ok": True, "offer": half})
        except (swaplib.SwapError, SendError, apilib.ApiMessageError, ValueError) as exc:
            log.debug("could not sell %s at its asking price: %s",
                      offer.get("inscription"), exc)
            return False
        log.info("offered inscription %s at its asking price to %s",
                 offer.get("number"), offer.get("buyer"))
        # Not written down as a sale here: nothing has been sold yet. This is
        # the seller's half going out; the swap happens when the buyer signs
        # theirs and it comes back to be countersigned, and THAT is what goes
        # in the approvals book (D-029).
        state.bump_generation()
        return True

    # --- taking a price this wallet's own order crosses ------------------------

    def fill_what_crosses(self) -> int:
        """Take an ask off the book when this wallet's own bid crosses it.

        A book where a bid sits above an ask and nothing happens is not a
        market, it is two people waiting for each other. Somebody has to be
        the taker, and it can only be the bid: an ask can be taken (the
        maker's node answers with its half, which is the one thing a taker
        cannot work out alone -- D-063), and a bid cannot, because nothing
        holds the buyer's coins and the seller has nothing to ask them for.

        Both sides now. When a bid of this wallet's crosses somebody's ask,
        this takes the ask; when an ask of this wallet's is crossed by
        somebody's bid, this takes the bid -- the maker answers with coins
        and this wallet gives the tokens. Whichever side is not resting is
        the one that acts, and a crossing book empties itself from either end
        (D-118).

        A partial fill leaves the maker's order shrunk by exactly what was
        taken and still on the book: `state._fill_order` for an ask, and
        `state._reduce_bid` for a bid, which is the piece that had to exist
        before this was safe -- a book that goes on advertising what somebody
        has already bought is the state D-082 is about.

        One fill at a time per pair, because the second one would be decided
        on a book that the first has not landed in yet.
        """
        state = self.state
        chain = state.messaging
        if chain.network == "main" or chain.params.swaps_from is None:
            return 0
        if chain.params.fills_from is None:
            return 0
        if state.setting("auto_fill", True) is False:
            return 0
        try:
            identity = state.ensure_identity()
            index = state.token_index(chain)
            if (index.indexed_height() or 0) < chain.params.fills_from:
                return 0
            with chain.rpc() as rpc:
                own = _own_addresses(rpc)
                mine = [o for o in index.orders_of(own) if not o.get("pending")]
                busy = {f["order"] for f in state.offers.fills(chain.network, limit=50)
                        if f["status"] in ("asked", "signed")}
                for order in mine:
                    if order["sale_property"] == 0 and order["want_property"]:
                        if _twin(order, mine):
                            # Two bids at one price on one pair: cancelling by
                            # price would take both off and only one would come
                            # back. A person can still press Take.
                            continue
                        if self._fill_one(rpc, index, chain, identity, order,
                                          own, busy):
                            return 1
                    elif order["want_property"] == 0 and order["sale_property"]:
                        if self._sell_into_a_bid(rpc, index, chain, identity,
                                                 order, own, busy):
                            return 1
        except Exception:
            log.debug("filling what crosses failed", exc_info=True)
        return 0

    #: An order this node has already asked about, as it looked when asked:
    #: (txid, what it sells, what it wants) -> when. The book is built from
    #: blocks, so a crossing order stays crossing until one lands -- and
    #: without this the next tick sees the same untouched order and asks
    #: again. Eleven fill transactions went out in fifty seconds against two
    #: bids before this existed, each for the full amount (a test machine, D-119).
    #:
    #: Keyed on the order AS SEEN, so a PARTIAL fill -- which changes the
    #: amounts -- is a different key and may be acted on at once, while a
    #: refusal or a silence is not retried until the window passes.
    RETRY_AFTER = 600.0

    def _asked_already(self, order: dict) -> bool:
        """Whether this node has asked about this order, as it stands now."""
        key = (str(order["txid"]), int(order["sale_amount"]),
               int(order["want_amount"]))
        when = self._tried.get(key)
        now = time.time()
        if when is not None and now - when < self.RETRY_AFTER:
            return True
        # Written BEFORE anything is sent, never after: a record made after
        # the message is a record that is missing exactly when the send
        # failed, which is the case that loops.
        self._tried[key] = now
        if len(self._tried) > 500:
            for old_key in sorted(self._tried, key=self._tried.get)[:100]:
                del self._tried[old_key]
        return False

    def _fill_one(self, rpc: Any, index: Any, chain: Any, identity: Any,
                  bid: dict, own: list[str], busy: set) -> bool:
        from fractions import Fraction

        state = self.state
        want = int(bid["want_amount"])            # tokens this bid is for
        pays = int(bid["sale_amount"])            # coins it will pay for them
        if want <= 0 or pays <= 0:
            return False
        book = index.book(int(bid["want_property"]))
        for ask in book["asks"]:
            if ask.get("pending") or ask["address"] in set(own) or ask["txid"] in busy:
                continue
            if Fraction(ask["want_amount"], ask["sale_amount"]) > Fraction(pays, want):
                return False                      # sorted: nothing after is better
            with state.store() as store:
                key = store.key_for(ask["address"])
            if key is None:
                continue                          # nobody to ask; leave it be
            take = min(want, int(ask["sale_amount"]))
            if take <= 0:
                continue
            # Rounded up, which is what the engine's price guard requires of a
            # fill: the maker must not be paid less than their own price.
            coins = -(-int(ask["want_amount"]) * take // int(ask["sale_amount"]))
            if not _has_two_outputs(rpc, bid["address"]):
                # Two transactions from the buyer's side, two outputs (D-051).
                return False
            if self._asked_already(ask):
                continue
            self._reprice(rpc, chain, bid, want - take)
            sent = self._reply(rpc, chain, identity, bytes(key["pubkey"]),
                               {"swap": "fill", "swapv": swaplib.PROTOCOL,
                                "order": ask["txid"], "tokens": take,
                                "buyer": bid["address"]})
            now = time.time()
            state.offers.add_fill({
                "id": sent, "network": chain.network, "order": ask["txid"],
                "maker": ask["address"], "buyer": bid["address"], "tokens": take,
                "coins": coins, "created": now, "expires": now + swaplib.OFFER_TTL})
            log.info("taking %s of order %s at the price this wallet bid",
                     take, ask["txid"][:12])
            state.bump_generation()
            return True
        return False

    def _sell_into_a_bid(self, rpc: Any, index: Any, chain: Any, identity: Any,
                         ask: dict, own: list[str], busy: set) -> bool:
        """Take somebody's bid when this wallet's own ask is crossed by it.

        The mirror of `_fill_one`, and simpler in the one way that matters:
        the maker's bid IS reduced by the swap when it names it
        (state._reduce_bid), so there is nothing to withdraw and re-post
        afterwards. What this wallet gives is tokens, which its own resting
        ask holds in reserve -- and the engine takes them from that reserve
        rather than from the free balance, which is what `_fills_for` has
        done since fills existed.
        """
        from fractions import Fraction

        state = self.state
        selling = int(ask["sale_amount"])          # tokens this ask is for
        wants = int(ask["want_amount"])            # coins it wants for them
        if selling <= 0 or wants <= 0:
            return False
        book = index.book(int(ask["sale_property"]))
        for bid in book["bids"]:
            if bid.get("pending") or bid["address"] in set(own) or bid["txid"] in busy:
                continue
            # Their price per token must be at least this wallet's.
            if Fraction(bid["sale_amount"], bid["want_amount"]) \
                    < Fraction(wants, selling):
                return False                       # sorted dearest first
            with state.store() as store:
                key = store.key_for(bid["address"])
            if key is None:
                continue                           # nobody to ask; leave it be
            tokens = min(selling, int(bid["want_amount"]))
            if tokens <= 0:
                continue
            # What this wallet will accept, worked out from THEIR order, and
            # rounded down so the answer cannot underpay by a rounding.
            coins = int(bid["sale_amount"]) * tokens // int(bid["want_amount"])
            if coins <= 0:
                continue
            if not _has_two_outputs(rpc, ask["address"]):
                return False
            if self._asked_already(bid):
                continue
            sent = self._reply(rpc, chain, identity, bytes(key["pubkey"]),
                               {"swap": "fill", "swapv": swaplib.PROTOCOL,
                                "order": bid["txid"], "tokens": tokens,
                                "buyer": ask["address"]})
            now = time.time()
            state.offers.add_fill({
                "id": sent, "network": chain.network, "order": bid["txid"],
                "maker": bid["address"], "buyer": ask["address"],
                "tokens": tokens, "coins": coins,
                "created": now, "expires": now + swaplib.OFFER_TTL})
            log.info("selling %s into the bid %s crosses", tokens,
                     bid["txid"][:12])
            state.bump_generation()
            return True
        return False

    def _reprice(self, rpc: Any, chain: Any, bid: dict, left: int) -> None:
        """Withdraw this bid and put back what is still wanted.

        Cancelled at its own price, not by pair, so another bid of this
        wallet's at another price is left where it is.
        """
        from . import payload as P
        from . import tokens as tokenlib

        pid = int(bid["want_property"])
        sender = tokenlib.TokenSender(rpc, chain.params)
        cancel = P.MetaDExCancelPrice(
            property_id_for_sale=0, amount_for_sale=int(bid["sale_amount"]),
            property_id_desired=pid, amount_desired=int(bid["want_amount"]))
        sender.broadcast(sender.prepare(bid["address"], cancel.encode()))
        if left <= 0:
            return
        # The remainder at the same price, rounded the way the order was:
        # what is left of a bid must not become dearer than it was.
        coins = int(bid["sale_amount"]) * left // int(bid["want_amount"])
        if coins <= 0:
            return
        again = P.MetaDExTrade(property_id_for_sale=0, amount_for_sale=coins,
                               property_id_desired=pid, amount_desired=left)
        sender.broadcast(sender.prepare(bid["address"], again.encode()))

    # --- answering ---------------------------------------------------------

    def _answer(self, rpc: Any, index: Any, offers: Any, chain: Any, row: Any,
                question: dict) -> dict | None:
        state = self.state
        if question.get("ask"):
            return self._ask(rpc, index, row, question)
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
                from .web.app import _ledger_addresses, _node_cut
                offer = swaplib.offer_for_order(
                    rpc, index, offers, chain.network,
                    str(question.get("order") or ""), int(question.get("tokens", 0)),
                    str(question.get("buyer") or ""),
                    bytes(row["sender_pubkey"]).hex(), own=_ledger_addresses(rpc),
                    cut=_node_cut(state))
                reply.update(ok=True, offer=offer)
            elif kind == "offer":
                shop = index.inscription(str(question.get("shop") or ""))
                if shop is None:
                    raise swaplib.SwapError("no such inscription on this node")
                from .web.app import _ledger_addresses, _node_cut
                offer = swaplib.make_offer(
                    rpc, index, offers, chain.network, shop,
                    int(question.get("listing", -1)), str(question.get("buyer") or ""),
                    bytes(row["sender_pubkey"]).hex(), own=_ledger_addresses(rpc),
                    cut=_node_cut(state))
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

    def _ask(self, rpc: Any, index: Any, row: Any, question: dict) -> dict | None:
        """Answer a question about a piece this wallet holds.

        No person is asked and nothing is signed: a route is a public
        declaration inscribed by whoever made the piece, and answering one
        gives away only what that declaration already promises. It works with
        nobody at the screen, which is the point -- two pieces can interact
        while one of their owners is asleep (D-091).
        """
        from . import pageapi

        answer: dict[str, Any] = {"ask": str(question.get("ask") or ""),
                                  "route": str(question.get("route") or ""),
                                  "re": row["txid"], "ok": False}
        try:
            found = index.inscription(str(question.get("ask") or ""))
            if found is None:
                raise pageapi.ApiError("no such inscription on this node")
            if found["owner"] not in _own_addresses(rpc):
                raise pageapi.ApiError(
                    "that piece is not held by this wallet, so there is "
                    "nothing here to ask")
            items = self.state.pagestore.items(found["txid"])
            answer.update(ok=True,
                          answer=pageapi.answer(found, answer["route"], items))
        except pageapi.ApiError as exc:
            answer["error"] = str(exc)
        except Exception as exc:
            log.warning("ask failed: %s", exc, exc_info=True)
            answer["error"] = f"the holder's node could not answer: {exc}"
        return answer

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
        if isinstance(question.get("leg"), dict):
            # Answered with the holder's half already signed, rather than with an
            # offer this node would countersign. Different work, and the
            # difference is worth a method of its own.
            return self._fill_a_leg(rpc, index, offers, chain, row, question, mine)
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

    def _fill_a_leg(self, rpc: Any, index: Any, offers: Any, chain: Any,
                    row: Any, question: dict, mine: dict) -> dict | None:
        """Finish a trade whose other half was handed over instead of offered.

        The usual answer to an offer this wallet made is an OFFER: the holder
        signs nothing, and this node countersigns its own half, which it can
        because it holds the one key that half needs -- its own. A holder can also
        answer with a LEG: it signed the two SINGLE|ANYONECANPAY signatures
        itself and sent them, needing nothing from this wallet but its coins, and
        what finishes the trade is a signature this node was GIVEN and pastes in
        (`listings.wallet_completes`). That difference is the argument the whole
        multi-user design rests on, so it is a method of its own and not one more
        arm of the one above.

        Everything `_bid` checks of an offer it checks of this, from the same
        note: that the answer came from the wallet that holds the piece, that it
        is the piece that was asked about, and that it is the price that was
        offered. The price is the check an account's `/account/fill` cannot run --
        a node has a note of what it offered, where a tab has to remember for
        itself -- and it matters most here, because this branch finishes the trade
        and broadcasts it in one move with no person looking at anything in
        between. What it refuses on is what the note says, and nothing else.

        So the bid is closed HERE, with the txid, and the reply carries no `bid`
        for the caller to close afterwards. The money moves inside this method: a
        note left open over a piece that is already bought is the same lie as one
        reading "signed" over a reply that never went out, only backwards.
        """
        from .ledger import AmountError

        state = self.state
        bid_id = str(question.get("id") or "")
        said = question["leg"]
        try:
            owed = mine.get("take") or {}
            # The coins the leg pays out, read out of this wallet's own note and
            # never out of the answer's word. It matters in one direction only:
            # the engine refuses a coin leg that is paid LESS than it asks and
            # says nothing about being paid more, so a number taken from the
            # stranger would be funded by this wallet -- an answer that took N
            # and asked for 2N would buy the piece it offered for N at twice the
            # price. A price in coins is paid inside the finished transaction, so
            # it stands in the leg's own output and `register` refuses a leg whose
            # output does not close at exactly this figure; a price in a token pays
            # no coin at all, so the leg this answer carries is the same leg with
            # nothing in its payment, and the payload comparison below is the guard
            # (D-182, D-183).
            price = (int(owed.get("sats") or 0)
                     if str(owed.get("kind") or "coins") == "coins" else 0)
            listing = state.listings.register(
                rpc, raw=str(said.get("raw") or ""),
                signatures=[str(s) for s in (said.get("signatures") or [])],
                pubkey=bytes.fromhex(str(said.get("pubkey") or "")),
                network=chain.network, owner=str(said.get("seller") or ""),
                price=price, seconds=listingslib.ANSWERED_FOR, record=False)
            if listing["owner"] != mine["owner"]:
                raise swaplib.SwapError("that answer is not from the wallet that "
                                        "holds it")
            named = listingslib.named_swap(
                bytes.fromhex(str(listing.get("payload") or "")))
            if named is None or named.give.txid.hex() != mine["inscription"]:
                raise swaplib.SwapError("that is not the item that was offered for")
            if not _same_price(swaplib.leg_json(named.take, index), owed):
                raise swaplib.SwapError("that is not the price that was offered")
            with contextlib.closing(index.open()) as db:
                txid = listingslib.wallet_completes(
                    rpc, db, chain.params, listing, str(mine.get("buyer") or ""),
                    fees.MIN_FEE_PER_KB)
        except (listingslib.ListingError, fundinglib.FundingError, AmountError,
                swaplib.SwapError, ValueError) as exc:
            offers.close_bid(bid_id, "failed", error=str(exc))
            state.bump_generation()
            return None
        except Exception as exc:
            # The answer was on the terms and this wallet still could not pay:
            # its own coins, a locked output, a node that refused the broadcast.
            log.warning("could not finish an answered leg: %s", exc, exc_info=True)
            offers.close_bid(bid_id, "failed",
                             error=f"this wallet could not pay for it: {exc}")
            state.bump_generation()
            return None
        offers.close_bid(bid_id, "signed", txid=txid)
        state.bump_generation()
        return {"swap": "sign", "swapv": swaplib.PROTOCOL, "re": row["txid"],
                "ok": True, "txid": txid}

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
            if order["sale_property"] == 0 and order["want_property"]:
                # A BID being filled: the maker gives coins and takes tokens,
                # and this wallet is the one with the tokens (D-118). Every
                # check is the same one read from the other side.
                if offer["give"].get("kind") != "coins":
                    raise swaplib.SwapError("a bid is filled with coins")
                if offer["take"].get("kind") != "token" \
                        or int(offer["take"].get("propertyid") or 0) != order["want_property"] \
                        or int(offer["take"].get("units") or 0) != int(mine["tokens"]):
                    raise swaplib.SwapError("that is not the amount that was asked for")
                paid = int(offer["give"].get("sats") or 0)
                # Never LESS than this wallet worked out from the order on the
                # chain. On this side the maker's answer could underpay, which
                # is the mirror of it overcharging.
                if paid < int(mine["coins"]):
                    raise swaplib.SwapError(
                        f"that answer offers {paid} satoshis for what this wallet "
                        f"priced at {mine['coins']} from the order on the chain")
            else:
                if offer["give"].get("kind") != "token" \
                        or int(offer["give"].get("propertyid") or 0) != order["sale_property"] \
                        or int(offer["give"].get("units") or 0) != int(mine["tokens"]):
                    raise swaplib.SwapError("that is not the amount that was asked for")
                if offer["take"].get("kind") != "coins":
                    raise swaplib.SwapError("an order is filled with coins")
                asked = int(offer["take"].get("sats") or 0)
                # Never more than this wallet worked out for itself from the
                # order on the chain. The maker names the price in its answer;
                # this is what stops the answer naming a different one.
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


def _take_of(offer: dict, index: Any) -> dict:
    """An offer's price, in the shape a swap leg is written in."""
    from . import inscriptions as I

    leg = I.Leg(int(offer["take_kind"]),
                property_id=int(offer["take_property"] or 0),
                amount=int(offer["take_amount"] or 0))
    return swaplib.leg_json(leg, index)


def _meets_the_ask(ask: dict, offer: dict) -> bool:
    """Whether an offer is worth at least what was asked for the piece.

    Same currency or nothing: coins are not a bid for a token price, and one
    token is not another. Within a currency it is a number, and more than
    the asking price is still a yes -- refusing it would be refusing money
    for the piece at a price already agreed to in public.
    """
    if int(ask["take_kind"]) != int(offer["take_kind"]):
        return False
    if int(ask["take_property"] or 0) != int(offer["take_property"] or 0):
        return False
    return int(offer["take_amount"] or 0) >= int(ask["take_amount"] or 0)


def _better_offer(offer: dict, standing: dict) -> bool:
    """More money wins; at the same money, whoever asked first."""
    mine, theirs = int(offer["take_amount"] or 0), int(standing["take_amount"] or 0)
    if mine != theirs:
        return mine > theirs
    return ((int(offer["block_height"] or 0), int(offer["position"] or 0))
            < (int(standing["block_height"] or 0), int(standing["position"] or 0)))


def _twin(bid: dict, mine: list[dict]) -> bool:
    """Whether another order of this wallet's is the same pair at the same price."""
    from fractions import Fraction

    price = Fraction(int(bid["sale_amount"]), int(bid["want_amount"]))
    for other in mine:
        if other["txid"] == bid["txid"] or other["sale_property"] != 0:
            continue
        if other["want_property"] != bid["want_property"]:
            continue
        if Fraction(int(other["sale_amount"]), int(other["want_amount"])) == price:
            return True
    return False


def _has_two_outputs(rpc: Any, address: str) -> bool:
    """A buyer's side of a swap is two transactions and needs an output for
    each: the half they sign, and the message that carries it (D-051)."""
    try:
        outputs = [u for u in (rpc.call("listunspent", 1, 9_999_999, [address]) or [])
                   if u.get("spendable", True)]
    except Exception:
        return True                  # cannot tell; let the usual path decide
    return len(outputs) >= 2


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
    if isinstance(data, dict) and data.get("ask"):
        # A question about a piece this wallet holds, answered from what that
        # piece declares (D-091). An ANSWER carries `re`, so it is not one.
        return None if "re" in data else data
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

"""What an account may ask the node to build, and how it comes back signed.

One shape, used by everything an account ever does on the chain: claiming a
name today, sending coins and inscribing later. The node builds and
explains; the browser shows, asks and signs; the node checks what came back
is what it offered and broadcasts (docs/multi-user.md §5).

    POST /account/build   {what, ...}  -> an offer, with an id
    POST /account/sign    {id, signatures}  -> a txid

**The offer is remembered by the node, not carried by the browser.** If the
browser handed back the transaction to broadcast, a compromised page could
be shown one thing, sign another, and have the node publish the second. It
hands back signatures over bytes the node already has, and nothing else it
says is used.

**An offer is short-lived and single-use.** It names specific coins; two
offers spending the same coin would be one transaction the network takes
and one it refuses, and the refusal would arrive after somebody was told
both had gone.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any

#: Long enough to read what it says and press the button, short enough that
#: the coins it names are not held out of use by a tab somebody closed.
OFFER_SECONDS = 300


class OfferError(Exception):
    """Something the account asked for that cannot be done. Shown as written."""


@dataclass
class Offer:
    """One unsigned transaction, waiting for the browser that asked for it."""

    id: str
    pubkey: str
    network: str
    unsigned: Any
    what: str
    made: float = field(default_factory=time.time)
    #: Called with the txid once this offer has been broadcast and checked.
    #: Nothing else can do this job: a run's book has to hear that a piece
    #: went out at the moment it went out, and only `/account/sign` knows
    #: that. A route with somewhere to record passes a closure over its own
    #: book; a route with nowhere to record leaves it None.
    done: Any = None

    @property
    def stale(self) -> bool:
        return time.time() - self.made > OFFER_SECONDS


class Offers:
    """The offers in flight, in memory only.

    In memory because an offer that survived a restart would name coins
    chosen against an index that has moved, and the honest answer to "the
    node restarted while you were reading" is to build a new one.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, Offer] = {}

    def add(self, pubkey: str, network: str, unsigned, what: str,
            done: Any = None) -> Offer:
        self.sweep()
        offer = Offer(id=secrets.token_urlsafe(18), pubkey=pubkey.lower(),
                      network=network, unsigned=unsigned, what=what, done=done)
        self._by_id[offer.id] = offer
        return offer

    def take(self, offer_id: str, pubkey: str) -> Offer:
        """The offer, removed, or a refusal that says which.

        Removed on the way out so it cannot be signed twice: the second
        signing would spend coins the first already spent, and the network
        would refuse one of them after somebody was told both had gone.
        """
        self.sweep()
        offer = self._by_id.pop(offer_id or "", None)
        if offer is None:
            raise OfferError(
                "that offer is not one this node is holding. It may have been "
                "signed already, or it may have expired -- ask again and you "
                "will get a fresh one.")
        if offer.pubkey != (pubkey or "").lower():
            raise OfferError("that offer belongs to somebody else")
        return offer

    def sweep(self) -> None:
        for key in [k for k, v in self._by_id.items() if v.stale]:
            del self._by_id[key]

    def waiting(self, pubkey: str) -> list[Offer]:
        self.sweep()
        return [o for o in self._by_id.values()
                if o.pubkey == (pubkey or "").lower()]


# --- what is on its way but not in a block -------------------------------------
#
# The UTXO index knows what is in a BLOCK. Between a broadcast and its
# block it still shows the coin that was just spent, and does not show the
# change that came back -- so a second transaction built in that window
# picks the same coin and the node answers `txn-mempool-conflict`. That is
# exactly what happened the first time an account claimed a name and
# published a messaging key without waiting a block in between.
#
# So an account's own transactions are remembered here until the index
# catches up: what they spent, so it is not offered again, and what they
# paid back, so it can be spent. Nothing here is authoritative -- the index
# is -- and everything is forgotten once the index agrees or the wait is
# over.


#: Long enough for a block on a slow chain, short enough that a
#: transaction the network dropped stops being believed. A coin wrongly
#: excluded is a refusal somebody can retry; a coin wrongly OFFERED is a
#: transaction the network refuses.
IN_FLIGHT_SECONDS = 1800


@dataclass
class InFlight:
    """One broadcast transaction, until the index has seen it.

    `network` is not decoration. An account now holds coins on more than
    one chain, and a coin remembered without saying which chain it is on
    would be offered as an input on the other one -- where it does not
    exist. The index would never have made that mistake, because it is
    per-chain; this list sits in front of the index and has to be too.
    """

    txid: str
    pubkey: str
    network: str
    spent: tuple
    made: tuple
    at: float = field(default_factory=time.time)


class Flights:
    """Everything an account has broadcast and the index has not yet read."""

    def __init__(self) -> None:
        self._all: list[InFlight] = []

    def add(self, pubkey: str, txid: str, unsigned, address: str,
            network: str = "") -> None:
        spent = tuple((coin["txid"], coin["vout"]) for coin in unsigned.inputs)
        from ..txbuild import p2pkh_script

        ours = p2pkh_script(address)
        made = tuple(
            {"txid": txid, "vout": n, "address": address, "value": value,
             "height": 0}
            for n, (value, script) in enumerate(unsigned.outputs)
            if script == ours)
        self._all.append(InFlight(txid=txid, pubkey=pubkey.lower(),
                                  network=network, spent=spent, made=made))
        self.sweep()

    def sweep(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        self._all = [f for f in self._all if now - f.at < IN_FLIGHT_SECONDS]

    def _mine(self, pubkey: str, network: str):
        who = (pubkey or "").lower()
        for flight in self._all:
            if flight.pubkey != who:
                continue
            # An unnamed network matches everything, so a caller that has
            # not been told which chain it is on is no worse off than
            # before. A caller that HAS been told gets only that chain.
            if network and flight.network and flight.network != network:
                continue
            yield flight

    def spent_by(self, pubkey: str, network: str = "") -> frozenset:
        self.sweep()
        out: set = set()
        for flight in self._mine(pubkey, network):
            out.update(flight.spent)
        return frozenset(out)

    def change_for(self, pubkey: str, network: str = "") -> list:
        self.sweep()
        out: list = []
        for flight in self._mine(pubkey, network):
            out.extend(flight.made)
        return out

    def note_incoming(self, pubkey: str, txid: str, vout: int,
                      address: str, value: int, network: str = "") -> None:
        """A payment TO an account that the node made itself.

        The faucet's coins come out of this node's own wallet, so the node
        knows that output exists the moment it broadcasts it -- there is
        nothing to wait for. Without this an account waits a whole block
        before it can claim its own name, for a transaction this machine
        is holding in its own mempool.

        Spending it unconfirmed is ordinary: a chain of two in the pool is
        far inside the ancestor limit, and if the payment were ever dropped
        the child would be dropped with it rather than being wrong.
        """
        self._all.append(InFlight(
            txid=txid, pubkey=(pubkey or "").lower(), network=network,
            spent=(),
            made=({"txid": txid, "vout": int(vout), "address": address,
                   "value": int(value), "height": 0},)))
        self.sweep()

    def forget(self, txid: str) -> None:
        self._all = [f for f in self._all if f.txid != txid]

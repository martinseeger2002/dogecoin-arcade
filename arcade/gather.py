"""Bringing a wallet's things back to one address.

A Core wallet spreads itself out: a fresh address for every payment received,
another for every lot of change. That is good for privacy and bad for
everything this does. A token on an address with no coins cannot be moved
without funding that address first. An NFT on one address and a published key
on another cannot be offered for, because nobody can work out who to write to.
A balance in three piles is three transactions to spend (D-043).

So one address a chain holds everything, and what is elsewhere is walked home
(D-046). Coins first, because a token or a piece cannot move off an address
that cannot pay its own fee; the fee for that move comes out of the pile being
moved, so nothing is ever spent that was not already there.

Automatic only where a transaction costs nothing real. On mainnet the same
work is offered and waits to be confirmed, because sweeping somebody's coins
without asking is spending their money for them.
"""

from __future__ import annotations

import logging
from typing import Any

from .ledger import COIN, format_amount

log = logging.getLogger(__name__)

#: Below this a stray output is left alone: moving it costs more in fee than
#: it is worth, and a wallet that shuffles dust around for ever is worse than
#: one with a little dust in it. Measured against a real refusal: sweeping a
#: 0.01 output failed with "too small to send after the fee has been
#: deducted" on every pass, for ever.
MIN_SWEEP = 2 * COIN

#: What an address needs before it can move a token or a piece off itself.
#: Having *an* output is not enough -- a token send pays a dust output and a
#: fee, and an address with 0.01 was asked for 1.01 on every pass and told
#: the same thing every time.
CAN_PAY = 3 * COIN // 2

#: What one pass will do. A wallet with a hundred stray pieces walks them home
#: over a hundred blocks rather than filling a mempool in one go -- and each
#: pass is written down as it happens, so an interruption loses nothing.
PER_PASS = 4

#: Enough coins to pay a fee, sent to a stray address that cannot, so that
#: what it holds can move at all. Comfortably more than one send costs, so a
#: second thing on the same address does not need a second seeding.
SEED = 3 * COIN


def _holding_ours(rpc: Any, index: Any, home: str) -> list[str]:
    """Addresses the ledger shows holding tokens or NFTs this node can sign for."""
    candidates: set[str] = set()
    try:
        for row in index.token_holders() if hasattr(index, "token_holders") else []:
            candidates.add(row["address"])
    except Exception:
        pass
    try:
        for row in index.inscriptions(limit=200):
            candidates.add(row["owner"])
    except Exception:
        pass
    mine = []
    for address in sorted(candidates - {home}):
        try:
            if rpc.call("validateaddress", address).get("ismine"):
                mine.append(address)
        except Exception:
            continue
    return mine


def stray(rpc: Any, index: Any, home: str, own: list[str]) -> dict[str, list]:
    """What this wallet holds away from home, and what it would take to fetch.

    Read-only. `coins` are outputs worth moving, `tokens` are (address,
    property, units) and `pieces` are inscriptions; `needs_coins` are
    addresses holding something that cannot pay to move it.
    """
    found: dict[str, list] = {"coins": [], "tokens": [], "pieces": [],
                              "needs_coins": []}
    elsewhere = [a for a in own if a != home]
    # Plus anywhere the ledger says this wallet's own things are sitting,
    # even on an address the arcade did not file under its own account: a
    # token this application sent to a receiving address it made before it
    # kept one address is still its business, and the node can sign for it.
    # An address holding nothing but coins is NOT claimed this way -- coins
    # look the same whoever they belong to (D-046).
    elsewhere += [a for a in _holding_ours(rpc, index, home)
                  if a not in elsewhere and a != home]
    if not elsewhere:
        return found
    # `listunspent` leaves out what the node has locked, which is how an
    # output held for an open offer (swap.make_offer) stays out of this: the
    # gather never sees it, so it can never sweep the thing a sale is about
    # to spend.
    coins_at: dict[str, int] = {}
    for utxo in rpc.call("listunspent", 1, 9_999_999) or []:
        address = utxo.get("address")
        if address in elsewhere and utxo.get("spendable", True):
            sats = int(round(float(utxo["amount"]) * COIN))
            coins_at[address] = coins_at.get(address, 0) + sats
            if sats >= MIN_SWEEP:
                found["coins"].append({"address": address, "txid": utxo["txid"],
                                       "vout": int(utxo["vout"]), "sats": sats})
    # Able to pay, not merely holding something: an address with a hundredth
    # of a coin cannot move a token off itself, and telling it to try again
    # every pass is a loop, not a plan.
    funded = {a for a, sats in coins_at.items() if sats >= CAN_PAY}
    try:
        for row in index.balances(elsewhere):
            found["tokens"].append({"address": row["address"],
                                    "property_id": row["property_id"],
                                    "name": row["name"], "units": int(row["balance"]),
                                    "display": row["display"]})
    except Exception:                       # an index that is not up yet
        pass
    for address in elsewhere:
        try:
            found["pieces"].extend(index.inscriptions(owner=address, limit=50))
        except Exception:
            pass
    waiting = {t["address"] for t in found["tokens"]}
    waiting |= {row["owner"] for row in found["pieces"]}
    found["needs_coins"] = sorted(a for a in waiting if a not in funded)
    # Coins are swept only off an address with nothing left on it. Otherwise
    # the pass that seeded an address so its token could travel would sweep
    # the seed back on the next one, and the two would take turns for ever
    # -- which is what happened, once, at a block apiece.
    found["coins"] = [c for c in found["coins"] if c["address"] not in waiting]
    return found


def walk_home(rpc: Any, index: Any, home: str, own: list[str], *,
              send_coins: Any, send_token: Any, send_piece: Any,
              limit: int = PER_PASS) -> list[str]:
    """Move what is away from home, a few things at a time. Returns txids.

    The order is not arbitrary. Coins come first and seed the addresses that
    have none, because a token or a piece cannot move off an address that
    cannot pay for the move; the next pass then finds them able to travel.
    Each send is somebody else's function -- the wallet's, the token
    sender's, the inscription sender's -- so this decides what moves and
    nothing about how.
    """
    found = stray(rpc, index, home, own)
    done: list[str] = []

    for address in found["needs_coins"]:
        if len(done) >= limit:
            return done
        try:
            done.append(send_coins(home, address, SEED))
            log.info("gather: sent %s a fee to travel with", address)
        except Exception as exc:
            log.warning("gather: could not fund %s: %s", address, exc)
    if found["needs_coins"]:
        # Everything else on those addresses waits for the seed to confirm.
        # One pass, one purpose: the next finds them ready.
        return done

    for token in found["tokens"]:
        if len(done) >= limit:
            return done
        try:
            done.append(send_token(token["address"], home, token["property_id"],
                                   token["units"]))
            log.info("gather: %s of %s came home", token["display"], token["name"])
        except Exception as exc:
            log.warning("gather: %s stayed put: %s", token["name"], exc)

    for piece in found["pieces"]:
        if len(done) >= limit:
            return done
        try:
            done.append(send_piece(piece["owner"], home, piece["txid"]))
            log.info("gather: inscription #%s came home", piece["number"])
        except Exception as exc:
            log.warning("gather: inscription #%s stayed put: %s",
                        piece["number"], exc)

    # Coins last: a stray output may be the very fee a token move above just
    # spent, and sweeping it first would leave that move unfundable.
    for coin in found["coins"]:
        if len(done) >= limit:
            return done
        try:
            done.append(send_coins(coin["address"], home, coin["sats"],
                                   outpoint=coin))
            log.info("gather: %s coins came home from %s",
                     coin["sats"] / COIN, coin["address"])
        except Exception as exc:
            log.warning("gather: coins stayed on %s: %s", coin["address"], exc)
    return done


def describe(found: dict[str, list], divisible: bool = True) -> str:
    """One line for a person: what is out there, in plain words."""
    parts = []
    if found["coins"]:
        total = sum(c["sats"] for c in found["coins"]) / COIN
        parts.append(f"{total:.8f} coins on {len({c['address'] for c in found['coins']})} "
                     f"address(es)")
    if found["tokens"]:
        parts.append(f"{len(found['tokens'])} token balance(s)")
    if found["pieces"]:
        parts.append(f"{len(found['pieces'])} NFT(s)")
    return ", ".join(parts)

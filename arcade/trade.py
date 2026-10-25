"""The trade state machine: one swap, two traders, every step decided from the chains.

The plan is ~/arcade-work/trustless-trading-plan.md (2026-10-13: trades "100%
trustless with lock scripts"; a facilitating node "just like a regular lock script that
the client side does"). This file is the brain both kinds of trader run -- a browser
tab (ported to JS) and a facilitating node -- so the two can never disagree about
what is safe.

A trade is two LEGS: what the initiator gives, on its chain, and what the responder
gives, on its chain (the same chain or another). The INITIATOR makes the secret.

    initiator locks its leg     (lock time T1, the longer one)
    responder checks that lock, then locks its own leg   (T2, well before T1)
    initiator checks that lock, then CLAIMS it -- the secret is now on that chain
    responder reads the secret there and claims the initiator's lock before T1
    either side, if the other never moves, refunds its own lock after its own time

`next_step(trade, facts, now)` is a pure function: given what the chains show (the
`Facts`), it returns the one thing this trader should do next. Nothing in here
trusts a message from the other side -- a message only says where to look; every
decision is made on what is confirmed on-chain, checked against the agreed terms.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import htlc as H
from .config import NETWORKS

INITIATOR, RESPONDER = "initiator", "responder"

#: How long the initiator's lock stands, in seconds; the responder's stands half as long.
#: 2026-10-13: 2 h. Short, because nobody has to be online to finish a swap: each
#: side's node holds its pre-signed claim and refund (arcade/trade_watch.py), so the lock
#: time only bounds how long coins sit when the other side vanishes.
LOCK_SECONDS = {"mainnet": 2 * 3600, "testnet": 2 * 3600}
#: Confirmations a lock needs before the other side acts on it. 2026-10-13: one, and
#: three for a big one (reorg safety), "big" being a coin leg at or above BIG[network]; the
#: operator sets the real line (admin, swap_big_*). 2026-10-17: none below that line
#: ("Allow 0-conf for less than 1000 doge and 1000000 pep") -- a lock seen in the mempool, and
#: exactly the one the terms say, is enough, so a small swap is as quick as its messages. The
#: risk he accepted: whoever locks first could double-spend an unconfirmed lock, up to the line.
#: A token or NFT lock counts unconfirmed when the pool shows its own transaction moving exactly
#: that asset in (asset_at); a Doginal lock already reads from the coin it spent (doginal_at).
CONFIRMATIONS = 0
#: At or above the line, one confirmation (2026-10-17: "1 conf above the threshold").
CONFIRMATIONS_BIG = 1
BIG = {"main": 1_000_000 * 100_000_000, "doge": 1_000 * 100_000_000,
       # Litecoin (2026-10-17, SadFrog's side): 2 LTC, about the value of the Dogecoin line;
        # The operator moves it with swap_big:ltc like the others.
        "ltc": 2 * 100_000_000,
        # Bellscoin and Luckycoin (2026-10-22, SadFrog's side): set low on purpose, so a lock asks for its
        # confirmation early until the operator sets swap_big:bel / swap_big:lky to the coins' worth.
        "bel": 200 * 100_000_000, "lky": 20 * 100_000_000,
        # Kotia (2026-10-23, tbuuol's side): low on purpose too, until the operator sets swap_big:kot.
        "kot": 100 * 100_000_000,
        # Wojakcoin (2026-10-25): low on purpose too, until the operator sets swap_big:wjk.
        "wjk": 100 * 100_000_000}
#: Never claim the responder's lock when less than this is left on it: the responder
#: must still have time to see the secret and claim the initiator's lock before T1.
CLAIM_MARGIN = 20 * 60
#: Never start a lock with less than this left before the other lock's refund opens.
LOCK_MARGIN = 30 * 60


def confirmations_for(give: "Leg", get: "Leg", big: dict | None = None) -> int:
    """0 (the lock seen in the mempool), or 1 when a mainnet coin leg is at or above its BIG line."""
    big = BIG if big is None else big
    # A DRC-20 / PRC-20 lock counts only once a copy of the ord index has read its note (token20_swap.token20_seen):
    # never from the mempool.
    if "token20" in (give.kind, get.kind):
        return max(CONFIRMATIONS_BIG, 1)
    for leg in (give, get):
        line = big.get(leg.network)
        if leg.kind == "coin" and leg.tier() == "mainnet" and line and leg.amount >= int(line):
            return CONFIRMATIONS_BIG
    return CONFIRMATIONS

# The steps next_step can answer.
LOCK, WAIT, CLAIM, REFUND, DONE, ABORT = "lock", "wait", "claim", "refund", "done", "abort"


@dataclasses.dataclass
class Leg:
    """One side of a trade: what moves, on which chain, from whom to whom."""
    network: str                 # "test", "main", "doge", "regtest"...
    kind: str                    # "coin", "token", "nft", "doginal", "token20" (Dogecoin / Pepecoin mainnet)
    asset: str                   # "" for coin, property id for a token, txid for an NFT, inscription id for a Doginal
    amount: int                  # sats for coin, units for a token, 1 for an NFT or a Doginal
    giver_key: str               # hex pubkey of whoever locks it (refunds to them)
    taker_key: str               # hex pubkey of whoever claims it
    #: The locker's operator fee, held INSIDE this leg's lock and paid out by the claim (2026-10-21, below
    #: `operator_fee`): the rate in whole basis points and the address it is paid to, both the LOCKER's own arcade's.
    #: None: terms from an arcade before that day, which carry no fee fields -- rate 0, and the lock is checked the old
    #: way (at least the amount; the claim's network fee comes off it). 0: an updated arcade with its fee Off.
    fee_bps: int | None = None
    fee_to: str = ""

    def tier(self) -> str:
        # Any real-coin chain's leg is mainnet tier (2026-10-22): a chain added as Params (Bellscoin,
        # Luckycoin) must not trade at the testnet tier's zero confirmations.
        params = NETWORKS.get(self.network)
        return "mainnet" if self.network == "main" or (params is not None and params.real_coins) else "testnet"


@dataclasses.dataclass
class Trade:
    id: str
    role: str                    # INITIATOR or RESPONDER, from this trader's side
    give: Leg                    # what THIS trader locks
    get: Leg                     # what THIS trader claims
    secret_hash: str             # hex
    secret: str = ""             # hex; the initiator's from the start, the responder's once read
    initiator_locktime: int = 0
    responder_locktime: int = 0
    state: str = "proposed"
    note: str = ""
    created: float = dataclasses.field(default_factory=time.time)
    need: int = 0                # confirmations this trader waits for (0: confirmations_for)

    @property
    def my_locktime(self) -> int:
        return self.initiator_locktime if self.role == INITIATOR else self.responder_locktime

    @property
    def their_locktime(self) -> int:
        return self.responder_locktime if self.role == INITIATOR else self.initiator_locktime

    def my_redeem(self) -> bytes:
        return H.redeem_script(bytes.fromhex(self.secret_hash), bytes.fromhex(self.give.taker_key),
                               bytes.fromhex(self.give.giver_key), self.my_locktime)

    def their_redeem(self) -> bytes:
        return H.redeem_script(bytes.fromhex(self.secret_hash), bytes.fromhex(self.get.taker_key),
                               bytes.fromhex(self.get.giver_key), self.their_locktime)


#: A token or NFT lock carries this much coin in its lock output besides the asset: it
#: pays for the one transaction that moves the asset out again, claim or refund
#: (htlc.move_out), so neither side has to bring coin of its own to finish.
LOCK_RESERVE = H.SPEND_FEE + H.DUST

# --- who pays the fees of a lock (2026-10-21) ----------------------------------------
#
# The operator: "We need to have the sender of each lock script pay the fee so it doesn't come out of the amount of the coin
# received. We need to add an operator fee output to each lock script." His answers, the same day:
#
# * The NETWORK fee of the claim is the sender's: a coin lock holds the amount plus `claim_fee` for its chain, so the
#   claimer receives exactly the amount agreed. A refund gives back everything less the refund's own fee -- at least the
#   amount. A token, NFT or Doginal lock already carries its own reserve (LOCK_RESERVE, the Doginal's output 1).
# * The OPERATOR fee is held in the lock too ("Hold it in the lock"): the claim pays it out as an output of its own to
#   the locker's operator, a refund gives it back to the locker. The script cannot make a claim pay it (no covenants):
#   the claimer's arcade builds the claim honestly, which was accepted. An arcade from before this change claims the
#   whole lock less the network fee, so with it the operator fee goes to the claimer instead.
# * Whose fee: each locker's OWN arcade's operator, at its "cut of trades" rate (app.TRADE_CUT, swap.cut_bps), carried
#   in the terms for that leg (Leg.fee_bps, Leg.fee_to) so the other side -- possibly on another arcade, through the
#   shared order book -- knows what the lock must hold and what its claim must pay.
# * Its form: "percent with a floor". The rate times the coin amount of that lock, rounded DOWN to the satoshi, never
#   under the chain's dust line (an output under it does not relay). A token, NFT or Doginal lock takes its percent of
#   the other leg when that is coin on the SAME chain; against another chain (or a non-coin) only the floor. Rate 0
#   (Off): no operator output at all, and no floor.
#
# Both traders work every one of these numbers out for themselves, from the terms, in integers, here and in
# wallet_js.js (operatorFee, claimFee...; tests/test_trade_fees.py holds the two to the same satoshi).

#: The highest rate a leg may carry: all of it (swap.CUT_CEILING). A bigger number in a leg is read as this.
FEE_BPS_CEILING = 10_000


def claim_fee(network: str) -> int:
    """What the claim (or refund) of a COIN lock on `network` pays the network (htlc.spend_fee), from the chain's own
    published parameters (config.NETWORKS) -- never from whichever node is asked, so two arcades always agree."""
    return H.spend_fee(NETWORKS.get(network))


def fee_floor(network: str) -> int:
    """The least an operator fee output may be on `network`: its dust line (htlc.dust_of -- 0.01 on the Dogecoin family,
    its soft-dust line; 0.0001 LTC on Litecoin)."""
    return H.dust_of(NETWORKS.get(network))


def operator_fee(leg: Leg, other: Leg | None = None) -> int:
    """The operator fee `leg`'s lock holds and its claim pays to `leg.fee_to`, in that chain's base units: 0 with no rate
    or no payee (terms from before 2026-10-21 included); else rate x base // 10,000, at least `fee_floor`. The base is
    the leg's own amount for coin; for a token, NFT or Doginal it is `other`'s amount when `other` is coin on the same
    chain, else nothing (the floor alone)."""
    bps = min(int(leg.fee_bps or 0), FEE_BPS_CEILING)
    if bps <= 0 or not leg.fee_to:
        return 0
    if leg.kind == "coin":
        base = int(leg.amount)
    elif other is not None and other.kind == "coin" and other.network == leg.network:
        base = int(other.amount)
    else:
        base = 0
    return max(fee_floor(leg.network), base * bps // 10_000)


def payee(leg: Leg, other: Leg | None = None) -> tuple[int, str] | None:
    """(fee, address) of the operator output a claim of `leg`'s lock pays, or None when it pays none."""
    fee = operator_fee(leg, other)
    return (fee, leg.fee_to) if fee else None


def doginal_reserve(leg: Leg, other: Leg | None = None) -> int:
    """The coin locked beside a Doginal (output 1 of its lock): the spend's own reserve and the operator fee."""
    return H.DOGINAL_RESERVE + operator_fee(leg, other)


def lock_value(leg: Leg, other: Leg | None = None) -> int:
    """The least coin the lock output for `leg` must hold for the terms to be met (check_lock): the amount, the claim's
    network fee and the operator fee for coin; the reserve and the operator fee for a token or NFT. A leg on terms from
    before 2026-10-21 (fee_bps None) is held to the old rule, the amount alone (or the reserve), so an arcade not yet
    updated still trades. A Doginal's lock output holds its own coin, whatever that is worth; its reserve stands beside
    it as output 1 and is checked with the Doginal itself (doginal_at, check_lock)."""
    if leg.kind in ("doginal", "token20"):
        return 1
    if leg.kind != "coin":
        return LOCK_RESERVE + operator_fee(leg, other)
    if leg.fee_bps is None:
        return leg.amount
    return leg.amount + claim_fee(leg.network) + operator_fee(leg, other)


def fund_value(leg: Leg, other: Leg | None = None) -> int:
    """What this trader puts into its OWN lock for `leg`: lock_value as an updated arcade reads it, whatever terms the
    other side sent -- the network fee of the claim is always the sender's, so the claimer gets exactly the amount even
    when the claimer is an old arcade (which pays itself the lock less that fee). A Doginal: the reserve beside it."""
    if leg.kind in ("doginal", "token20"):
        return doginal_reserve(leg, other)
    if leg.fee_bps is None:
        leg = dataclasses.replace(leg, fee_bps=0, fee_to="")
    return lock_value(leg, other)


#: The version bytes an operator fee may be paid to: a pay-to-pubkey-hash address of any chain this software knows.
#: The key behind a hash holds the coins on every chain, so a regtest node standing in for Dogecoin (its own version
#: byte) is paid as well as a real one; a script hash is refused, nobody's arcade pays one.
PAYEE_VERSIONS = frozenset(p.pubkeyhash_version for p in NETWORKS.values())


def payee_ok(address: Any) -> bool:
    """Is `address` somewhere an operator fee can be paid: a pay-to-pubkey-hash address (PAYEE_VERSIONS)?"""
    from .script import b58check_decode
    if not isinstance(address, str) or not 26 <= len(address) <= 36:
        return False
    try:
        version, payload = b58check_decode(address)
    except Exception:                                      # noqa: BLE001 -- not base58check
        return False
    return version in PAYEE_VERSIONS and len(payload) == 20


def with_fee(leg: Leg, terms: dict | None) -> Leg:
    """`leg` carrying an arcade's lock-fee terms ({"bps", "to"}, as /account and node_trader.lock_fee give them; empty
    or None: Off, rate 0 -- still the new terms, so the lock is checked with its network fee on top)."""
    terms = terms or {}
    bps = int(terms.get("bps") or 0)
    to = str(terms.get("to") or "") if bps > 0 else ""
    return dataclasses.replace(leg, fee_bps=bps if to else 0, fee_to=to)


def move_data(leg: Leg) -> bytes:
    """The OP_RETURN data that moves a token or NFT leg out of its lock (claim or refund)."""
    from . import escrow as E
    if leg.kind == "token":
        return E.token_payload(int(leg.asset), leg.amount)
    if leg.kind == "nft":
        return E.nft_payload(leg.asset)
    raise ValueError("a coin leg moves no asset, and a Doginal moves by koinu order, not by a payload")


def asset_at(index: Any, leg: Leg, address: str, lock_height: int | None,
             lock_txid: str = "") -> bool | None:
    """Does the ledger show `leg`'s token or NFT at the lock `address`? None when the
    ledger has not yet read the lock's block (`lock_height`, None while unconfirmed):
    a lagging index must make a lock look not-yet-there, never wrong -- a wrong lock is
    walked away from (next_step ABORT).

    An unconfirmed lock (2026-10-17: the same 0-conf as coins, "for NFT's"): True when
    the lock transaction itself moves exactly this piece into the lock from its current owner
    with no other move of it in the pool, or at least this many tokens from an address whose
    confirmed balance covers them and every other pending send of that token; else None (wait
    for the block) -- never False, which would walk away from a lock that may yet be good."""
    if leg.kind == "coin":
        return True
    if lock_height is None:
        if not lock_txid or not hasattr(index, "pool_moves_of"):
            return None
        return _pool_asset_at(index, leg, address, lock_txid)
    with index.open() as db:
        tip = db.tip()
    if tip is None or int(tip["height"]) < int(lock_height):
        return None
    if leg.kind == "token":
        return int(index.balance(address, int(leg.asset))) >= leg.amount
    row = index.inscription(leg.asset)
    return bool(row) and row["owner"] == address


def _pool_asset_at(index: Any, leg: Leg, address: str, lock_txid: str) -> bool | None:
    try:
        rows = index.pool_moves_of(lock_txid)
        others = [m for m in index.pending_moves() if m.get("txid") != lock_txid]
    except Exception:
        return None
    if leg.kind == "nft":
        here = any(r["kind"] == "inscription" and r["inscription"] == leg.asset and r["to"] == address
                   for r in rows)
        racing = any(m["kind"] == "inscription" and m.get("inscription") == leg.asset for m in others)
        return True if here and not racing else None
    if leg.kind == "token":
        pid = int(leg.asset)
        sent = [r for r in rows if r["kind"] == "token" and r["property_id"] == pid and r["to"] == address
                and r.get("why") == "send"]
        if not sent or sum(r["units"] for r in sent) < leg.amount:
            return None
        sender = sent[0]["from"]
        also = sum(m["units"] for m in others if m["kind"] == "token" and m["property_id"] == pid
                   and m["from"] == sender and m.get("why") != "grant")
        covered = int(index.balance(sender, pid)) >= sum(r["units"] for r in sent) + also
        return True if covered else None
    return None


def doginal_at(doginals: Any, leg: Leg, redeem: bytes, lock_tx: dict, parent_tx: dict) -> bool | None:
    """Is `leg`'s Doginal at offset 0 of output 0 of `lock_tx` (verbose, as getrawtransaction
    gives it), with its reserve beside it? None while the chain cannot be read.

    [Claude 2026-10-15] Read from the coin the lock SPENT, not from the lock itself: the reading
    may not have indexed a minutes-old lock, but koinu order settles it -- when input 0 is a
    coin holding exactly this inscription at its first koinu (`parent_tx`'s output, which
    the reading has long seen) and output 0 takes that coin's whole value, the inscription is
    output 0's first koinu, whatever the other inputs carry (they fill output 1 and change)."""
    from . import doginals as D
    script = H.p2sh_script(redeem).hex()
    outs = lock_tx.get("vout") or []
    if len(outs) < 2 or outs[0]["scriptPubKey"]["hex"] != script or outs[1]["scriptPubKey"]["hex"] != script:
        return False
    if round(float(outs[1]["value"]) * 100_000_000) < H.DOGINAL_RESERVE:
        return False
    first = (lock_tx.get("vin") or [{}])[0]
    if first.get("txid") != parent_tx.get("txid"):
        return False
    spent = parent_tx["vout"][int(first["vout"])]
    if round(float(spent["value"]) * 100_000_000) != round(float(outs[0]["value"]) * 100_000_000):
        return False
    try:
        on = doginals.held(parent_tx["txid"]).get(int(first["vout"]))
    except D.Unknown:
        return None
    return not D.exactly(on, leg.asset)


@dataclasses.dataclass
class LockSeen:
    """A lock as a chain shows it: where, how much, how deep, and whether it was spent."""
    txid: str
    vout: int
    value: int                   # coin in the lock output (sats)
    script_hex: str              # the output's scriptPubKey
    confirmations: int
    asset_ok: bool = True        # the ledger shows the token/NFT at the lock address (coin: True)
    spent_by_script_sig: str = ""   # hex input script of the spend, if it was spent
    spent_by_me: bool = False       # the spend pays one of my addresses: shown only, next_step never decides on it
    reserve: int = 0             # a Doginal lock: the coin at output 1 that pays its spend


@dataclasses.dataclass
class Facts:
    """What this trader's chains show right now, for both locks."""
    mine: LockSeen | None = None
    theirs: LockSeen | None = None


def _tier(give: Leg, get: Leg) -> str:
    return "mainnet" if "mainnet" in (give.tier(), get.tier()) else "testnet"


def plan_locktimes(give: Leg, get: Leg, role: str, now: int) -> tuple[int, int]:
    """(initiator's T1, responder's T2) for a trade starting now. The tier of a cross
    chain trade is the stricter of the two (any mainnet leg -> mainnet times)."""
    tier = _tier(give, get)
    t1 = int(now) + LOCK_SECONDS[tier]
    t2 = int(now) + LOCK_SECONDS[tier] // 2
    return t1, t2


#: The least time a responder accepts between the two lock times (2026-10-25): after the initiator's claim shows the
#: secret -- as late as T2 -- the responder must still claim the initiator's lock before T1 opens its refund. Half the
#: lock duration, as plan_locktimes makes it, and never under an hour.
LOCK_GAP_FLOOR = 3600


def locktimes_fit(give: Leg, get: Leg, t1: Any, t2: Any, now: int) -> str:
    """"" when a proposal's lock times are ones a responder may lock under, else why not (2026-10-25). Checked
    wherever a proposal is accepted (node_trader._consider; wallet.js locktimesFit in answerProposals): T2 before T1
    by at least the gap plan_locktimes leaves (LOCK_GAP_FLOOR at least); T2 far enough off to lock in time
    (LOCK_MARGIN); and T2 no further off than twice the normal lock duration, so a responder's coins are never held
    for long by a counterparty that vanishes. The legs are either side's: the tier is the stricter of the two."""
    lock = LOCK_SECONDS[_tier(give, get)]
    if not (isinstance(t1, int) and isinstance(t2, int)) or isinstance(t1, bool) or isinstance(t2, bool):
        return "lock times are whole seconds"
    if t1 - t2 < max(LOCK_GAP_FLOOR, lock // 2):
        return "the two lock times are too close together to finish the swap safely"
    if t2 - LOCK_MARGIN <= int(now):
        return "too little time is left before the responder's lock time"
    if t2 - int(now) > 2 * lock:
        return "the responder's lock time is too far off"
    return ""


def check_lock(trade: Trade, seen: LockSeen, theirs: bool) -> str:
    """"" if `seen` is exactly the lock the terms say, else why not. "Holds at least": a lock that holds more passes,
    which is how an arcade from before 2026-10-21 (checking the amount alone) still accepts an updated one's lock."""
    leg = trade.get if theirs else trade.give
    other = trade.give if theirs else trade.get
    redeem = trade.their_redeem() if theirs else trade.my_redeem()
    if seen.script_hex != H.p2sh_script(redeem).hex():
        return "that lock's script is not the one agreed (hash, keys or lock time differ)"
    if leg.kind in ("doginal", "token20") and (seen.vout != 0 or not seen.asset_ok):
        return "that lock does not hold the agreed Doginal at the start of output 0, with its reserve beside it"
    if leg.kind in ("doginal", "token20") and operator_fee(leg, other) and seen.reserve < doginal_reserve(leg, other):
        return "the reserve beside that Doginal is too small to pay its spend and the operator fee agreed"
    if seen.value < lock_value(leg, other):
        if leg.kind == "coin":
            if leg.fee_bps is None:
                return f"that lock holds {seen.value}, less than the {leg.amount} agreed"
            return (f"that lock holds {seen.value}, less than the {leg.amount} agreed with its network fee "
                    f"({claim_fee(leg.network)}) and operator fee ({operator_fee(leg, other)}) on top")
        return "that lock holds too little coin to move its token or NFT out again"
    if leg.kind != "coin" and not seen.asset_ok:
        return "the ledger does not show the agreed token or NFT at that lock"
    return ""


def _branch(seen: LockSeen, redeem: bytes) -> str:
    """htlc.spend_branch of the spend `seen` shows, "" while unspent or unreadable."""
    try:
        return H.spend_branch(bytes.fromhex(seen.spent_by_script_sig), redeem) if seen.spent_by_script_sig else ""
    except ValueError:
        return ""


def next_step(trade: Trade, facts: Facts, now: int) -> tuple[str, str]:
    """(step, why). The one thing this trader should do next, from the chains alone."""
    need = trade.need or confirmations_for(trade.give, trade.get)
    mine, theirs = facts.mine, facts.theirs

    # Finished: I claimed theirs (the trade went through), or mine came back to me. Told by the BRANCH each spend
    # took -- the claim of their lock is my key's, the refund of mine is my key's -- never by who its outputs pay:
    # the other side's claim of mine (or refund of theirs) could pay me a crumb and pass for my own.
    if theirs is not None and _branch(theirs, trade.their_redeem()) == H.CLAIM:
        return DONE, "I claimed their lock"
    if mine is not None and _branch(mine, trade.my_redeem()) == H.REFUND:
        return DONE, "my lock came back to me (refunded)"

    # The other lock, if it is there, must be exactly what was agreed.
    if theirs is not None and not theirs.spent_by_script_sig:
        bad = check_lock(trade, theirs, theirs=True)
        if bad:
            if mine is not None and not mine.spent_by_script_sig:
                return (REFUND, bad) if now >= trade.my_locktime else (WAIT, bad + "; waiting to refund mine")
            return ABORT, bad

    if trade.role == INITIATOR:
        if mine is None:
            return LOCK, "I lock first"
        if theirs is None or theirs.confirmations < need:
            if now >= trade.my_locktime:
                return REFUND, "the other side never locked; my lock time has passed"
            return WAIT, "waiting for the other lock to confirm"
        if theirs.spent_by_script_sig:
            return WAIT if now < trade.my_locktime else REFUND, "their lock went back to them"
        if now < trade.their_locktime - CLAIM_MARGIN:
            return CLAIM, "their lock is confirmed and agreed: claim it (this reveals the secret)"
        if now >= trade.my_locktime:
            return REFUND, "too late to claim safely; my lock time has passed"
        return WAIT, "too close to their lock time to claim safely; waiting to refund"

    # RESPONDER
    if theirs is None or theirs.confirmations < need:
        if mine is not None and now >= trade.my_locktime:
            return REFUND, "my lock time has passed"
        return WAIT, "waiting for the initiator's lock to confirm"
    if mine is None:
        if now >= trade.their_locktime - LOCK_MARGIN or now >= trade.responder_locktime - LOCK_MARGIN:
            return ABORT, "too little time left to lock safely"
        return LOCK, "their lock is confirmed and agreed: lock mine"
    # Both locked. Has the initiator claimed mine (revealing the secret)?
    if mine.spent_by_script_sig:
        secret = H.secret_from_script_sig(bytes.fromhex(mine.spent_by_script_sig), trade.my_redeem())
        if secret is None:
            return WAIT, "my lock was spent without the secret?"
        trade.secret = secret.hex()
        if now < trade.their_locktime:
            return CLAIM, "the secret is on the chain: claim their lock"
        return ABORT, "the secret came too late to claim their lock"
    if now >= trade.my_locktime:
        return REFUND, "the initiator never claimed; my lock time has passed"
    return WAIT, "waiting for the initiator to claim (that reveals the secret)"


# --- persistence --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS trade (
    id      TEXT PRIMARY KEY,
    body    TEXT NOT NULL,
    state   TEXT NOT NULL,
    updated REAL NOT NULL
);
"""


class Book:
    """This trader's trades, kept so a restart never forgets a lock (or its secret)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._open() as conn:
            conn.executescript(SCHEMA)

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def save(self, trade: Trade) -> None:
        body = json.dumps(dataclasses.asdict(trade))
        with self._open() as conn:
            conn.execute("INSERT INTO trade(id, body, state, updated) VALUES(?,?,?,?) "
                         "ON CONFLICT(id) DO UPDATE SET body=excluded.body, "
                         "state=excluded.state, updated=excluded.updated",
                         (trade.id, body, trade.state, time.time()))

    def get(self, trade_id: str) -> Trade | None:
        with self._open() as conn:
            row = conn.execute("SELECT body FROM trade WHERE id=?", (trade_id,)).fetchone()
        return _load(row["body"]) if row else None

    def open_trades(self) -> list[Trade]:
        with self._open() as conn:
            rows = conn.execute("SELECT body FROM trade WHERE state NOT IN ('done','aborted')").fetchall()
        return [_load(r["body"]) for r in rows]


def _load(body: str) -> Trade:
    said: dict[str, Any] = json.loads(body)
    said["give"], said["get"] = Leg(**said["give"]), Leg(**said["get"])
    return Trade(**said)

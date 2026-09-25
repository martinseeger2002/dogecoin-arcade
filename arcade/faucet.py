"""A hundred test coins, once, so a new account can do something.

An account with no coins cannot claim its name, post, or inscribe
anything: every one of those is a transaction and every transaction needs
an input. So the first thing that happens to a new account is that it is
given enough to start.

**Testnet, unless somebody deliberately says otherwise.** Refused on a
mainnet chain by default, and enforced here rather than remembered by
whoever wires it up, because "the faucet paid out on mainnet" is a
sentence nobody should have to read by accident.

Not "for ever", which is what this said first and was not the operator's
position. A mainnet faucet is possible later **if the testnet one turns
out to be secure**, and it would give something like ten coins rather than
a hundred. So the refusal is a setting that is off, not a law: turning it
on means naming the chain and the amount explicitly, and the amount has
its own much lower ceiling (`MAINNET_MOST`). What would have to be true
first is written down in docs/multi-user.md §1b -- a faucet that has run
in the open on testnet without being farmed.

**Bounded by the account and the seat**: one gift per account, and a node
seats a fixed number of accounts, so a node seating a hundred gives at most a
hundred gifts, ever. The operator took out the other two limits on 2026-09-25: once
a connection a day turned away the second person in a house, an office or a
phone network (and anybody farming had a phone anyway), and a daily ceiling of
twenty would have left a launch day's crowd with an empty faucet. The ceiling
is still there for an operator who wants one (`faucet_daily`); the connection
is still RECORDED with each gift, so a pattern can be seen after the fact.

**What is NOT done here**: judging whether somebody deserves it. There is
no captcha, no email, no wait. A faucet that is hard to use is a faucet
that stops new people rather than farmers, who are the ones with scripts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

#: What a new account is given. Enough to claim a name, post a few times,
#: and inscribe something small -- so the first hour is spent using the
#: thing rather than working out how to get coins.
GIFT = 100_00000000

#: The ceiling, per day, across everybody. None: there is none by default
#: (2026-09-25) -- a launch day's crowd must not find the faucet dry
#: at breakfast. What bounds a node's faucet is one gift per account and the
#: seats: a node that seats a hundred can give at most a hundred gifts, ever.
#: An operator who wants a daily cap sets `faucet_daily` in settings.json.
DAILY_CEILING = None

#: The most a faucet may give on a chain where the coins are real, if one
#: is ever turned on. A tenth of the testnet gift, because the question a
#: mainnet faucet answers is "can this person pay one fee" and not "can
#: this person play for an afternoon".
MAINNET_MOST = 10_00000000

SCHEMA = """
CREATE TABLE IF NOT EXISTS faucet (
    pubkey  TEXT PRIMARY KEY,
    address TEXT NOT NULL,
    amount  INTEGER NOT NULL,
    txid    TEXT NOT NULL DEFAULT '',
    ip      TEXT NOT NULL DEFAULT '',
    at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS faucet_when ON faucet(at);
CREATE INDEX IF NOT EXISTS faucet_ip ON faucet(ip, at);
"""


class FaucetError(Exception):
    """A refusal, in words somebody can act on."""


class DryToday(FaucetError):
    """The ceiling is reached. Tomorrow, not never."""


@dataclass(frozen=True)
class Given:
    pubkey: str
    address: str
    amount: int
    txid: str
    at: int


class Faucet:
    """The record of what has been given, and the rules about giving more."""

    def __init__(self, accounts, gift: int = GIFT,
                 ceiling: int | None = DAILY_CEILING, real_coins: bool = False):
        self.conn = accounts.conn
        self.gift = int(gift)
        #: None is "no daily ceiling", which is the default (see DAILY_CEILING).
        self.ceiling = None if ceiling is None else int(ceiling)
        #: Whether this faucet may pay on a chain where the coins are real.
        #: Off unless an operator says otherwise, and capped much lower
        #: when it is on.
        self.real_coins = bool(real_coins)
        self.conn.executescript(SCHEMA)

    def given(self, pubkey: str) -> Given | None:
        row = self.conn.execute("SELECT * FROM faucet WHERE pubkey = ?",
                                ((pubkey or "").lower(),)).fetchone()
        if row is None:
            return None
        return Given(row["pubkey"], row["address"], row["amount"],
                     row["txid"], row["at"])

    def today(self, now: int | None = None) -> int:
        now = int(now if now is not None else time.time())
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM faucet WHERE at > ?",
            (now - 86400,)).fetchone()
        return int(row[0] or 0)

    def left_today(self, now: int | None = None) -> int:
        if self.ceiling is None:
            return 10 ** 18                   # no daily ceiling: never the reason
        return max(0, self.ceiling - self.today(now))

    def may(self, pubkey: str, ip: str = "", now: int | None = None) -> None:
        """Raise if this account may not be paid. Says which rule stopped it."""
        now = int(now if now is not None else time.time())
        if self.given(pubkey) is not None:
            raise FaucetError(
                "this account has already had its coins. They are testnet "
                "coins, so there is nothing to be gained by asking twice.")
        if self.left_today(now) < self.gift:
            raise DryToday(
                "the faucet is empty until tomorrow. It has a daily limit so "
                "that a bad day for it is never a bad day for the node that "
                "runs it.")
        # No per-connection rule any more (2026-09-25): see the module
        # docstring. `ip` is still taken, and recorded by record(), not judged.

    def record(self, pubkey: str, address: str, txid: str, ip: str = "",
               now: int | None = None) -> Given:
        now = int(now if now is not None else time.time())
        self.conn.execute(
            "INSERT INTO faucet (pubkey, address, amount, txid, ip, at) "
            "VALUES (?,?,?,?,?,?)",
            ((pubkey or "").lower(), address, self.gift, txid, ip, now))
        return self.given(pubkey)


def pour(chain, faucet: Faucet, pubkey: str, address: str, ip: str = "",
         now: int | None = None) -> Given:
    """Give one account its coins, and write down that it happened.

    The refusal comes BEFORE the node is asked for anything, so a rule that
    says no costs nothing. The record is written after the broadcast, with
    the txid in it: a record written first would claim coins that never
    went, and a coin that went with no record would go again.
    """
    if chain.is_mainnet and not faucet.real_coins:
        raise FaucetError(
            "this faucet gives testnet coins. Real ones are not given away "
            "here -- an operator who wants that has to turn it on and say "
            "how much.")
    if chain.is_mainnet and faucet.gift > MAINNET_MOST:
        raise FaucetError(
            f"a faucet on a real chain gives at most "
            f"{MAINNET_MOST / 100_000_000:g} coins, and this one is set to "
            f"{faucet.gift / 100_000_000:g}. The ceiling is deliberate: the "
            f"question a real faucet answers is whether somebody can pay one "
            f"fee, not whether they can play for an afternoon.")
    faucet.may(pubkey, ip=ip, now=now)
    with chain.rpc() as rpc:
        txid = rpc.call("sendtoaddress", address, faucet.gift / 100_000_000)
    return faucet.record(pubkey, address, txid, ip=ip, now=now)

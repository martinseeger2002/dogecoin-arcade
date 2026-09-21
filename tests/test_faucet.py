"""A hundred test coins, once, and the three limits on it.

An open faucet and open signup together are a farm. The rules exist so
that the worst case is "the faucet is empty until tomorrow" and never
"the operator's wallet is empty" (docs/multi-user.md §1b).
"""

import pathlib
import time

import pytest

from arcade import faucet as faucetlib
from arcade.accounts import Accounts
from arcade.faucet import DryToday, Faucet, FaucetError, GIFT


@pytest.fixture
def register(tmp_path):
    return Accounts(tmp_path / "accounts.sqlite")


@pytest.fixture
def faucet(register):
    return Faucet(register)


KEY = "ab" * 32
OTHER = "cd" * 32


class FakeChain:
    """A chain that records what it was asked to send."""

    def __init__(self, mainnet=False):
        self.is_mainnet = mainnet
        self.sent = []

    def rpc(self):
        import contextlib
        chain = self

        class Node:
            def call(self, method, *args):
                assert method == "sendtoaddress"
                chain.sent.append(args)
                return "ff" * 32

        return contextlib.nullcontext(Node())


# --- the three limits ----------------------------------------------------------

def test_an_account_is_paid_once(faucet):
    chain = FakeChain()
    faucetlib.pour(chain, faucet, KEY, "nAddr", ip="1.2.3.4")
    assert len(chain.sent) == 1
    assert chain.sent[0] == ("nAddr", GIFT / 100_000_000)

    with pytest.raises(FaucetError) as again:
        faucetlib.pour(chain, faucet, KEY, "nAddr", ip="5.6.7.8")
    assert "already had its coins" in str(again.value)
    assert len(chain.sent) == 1, "and nothing was sent the second time"


def test_one_connection_gets_one_wallet_a_day(faucet):
    """Not one ever: families, offices and phones on one network are real,
    and refusing the second person in a house refuses the wrong person."""
    chain = FakeChain()
    now = int(time.time())
    faucetlib.pour(chain, faucet, KEY, "nOne", ip="1.2.3.4", now=now)
    with pytest.raises(FaucetError) as soon:
        faucetlib.pour(chain, faucet, OTHER, "nTwo", ip="1.2.3.4", now=now + 60)
    assert "one wallet a day" in str(soon.value)

    faucetlib.pour(chain, faucet, OTHER, "nTwo", ip="1.2.3.4",
                   now=now + 86401)
    assert len(chain.sent) == 2, "tomorrow, yes"


def test_the_daily_ceiling_is_the_one_that_matters(register):
    """The first two limits are a nuisance to anybody with addresses and a
    phone. This one cannot be worked around by anybody."""
    faucet = Faucet(register, gift=GIFT, ceiling=2 * GIFT)
    chain = FakeChain()
    now = int(time.time())
    faucetlib.pour(chain, faucet, "11" * 32, "nA", now=now)
    faucetlib.pour(chain, faucet, "22" * 32, "nB", now=now)
    assert faucet.left_today(now) == 0

    with pytest.raises(DryToday) as dry:
        faucetlib.pour(chain, faucet, "33" * 32, "nC", now=now)
    assert "empty until tomorrow" in str(dry.value)
    assert "bad day for the node" in str(dry.value)
    assert len(chain.sent) == 2

    # And tomorrow it is full again.
    faucetlib.pour(chain, faucet, "33" * 32, "nC", now=now + 86401)
    assert len(chain.sent) == 3


def test_real_coins_are_not_given_away_unless_somebody_says_so(faucet):
    """Off by default and enforced rather than remembered. Not a law: a
    faucet on a real chain is possible later, if the testnet one turns out
    not to be farmed, and it would give a tenth as much."""
    chain = FakeChain(mainnet=True)
    with pytest.raises(FaucetError) as refused:
        faucetlib.pour(chain, faucet, KEY, "PReal")
    assert "not given away here" in str(refused.value)
    assert chain.sent == [], "nothing was asked of the node at all"


def test_turning_it_on_for_real_coins_takes_saying_how_much(register):
    """And a hundred is refused: the question a real faucet answers is
    whether somebody can pay one fee, not play for an afternoon."""
    generous = Faucet(register, gift=GIFT, real_coins=True)
    chain = FakeChain(mainnet=True)
    with pytest.raises(FaucetError) as too_much:
        faucetlib.pour(chain, generous, KEY, "PReal")
    assert "at most 10" in str(too_much.value)
    assert chain.sent == []

    modest = Faucet(register, gift=faucetlib.MAINNET_MOST, real_coins=True)
    faucetlib.pour(chain, modest, KEY, "PReal")
    assert chain.sent == [("PReal", 10.0)]


def test_a_testnet_faucet_is_unaffected_by_that_ceiling(faucet):
    """A hundred testnet coins is the point; the low ceiling is about the
    chain where they are worth something."""
    chain = FakeChain(mainnet=False)
    faucetlib.pour(chain, faucet, KEY, "nAddr")
    assert chain.sent == [("nAddr", 100.0)]


def test_a_refusal_costs_the_node_nothing(faucet):
    """The rules are checked before the node is asked for anything, so a
    faucet under attack is a table lookup rather than an RPC storm."""
    chain = FakeChain()
    faucetlib.pour(chain, faucet, KEY, "nAddr")
    for _ in range(20):
        with pytest.raises(FaucetError):
            faucetlib.pour(chain, faucet, KEY, "nAddr")
    assert len(chain.sent) == 1


# --- what is written down ------------------------------------------------------

def test_the_record_is_written_after_the_coins_go(faucet):
    """A record written first would claim coins that never went; a coin
    that went with no record would go again."""
    chain = FakeChain()
    given = faucetlib.pour(chain, faucet, KEY, "nAddr", ip="1.2.3.4")
    assert given.txid == "ff" * 32, "the record carries the txid"
    assert given.amount == GIFT
    assert faucet.given(KEY).address == "nAddr"


def test_a_node_that_refuses_to_send_leaves_no_record(faucet):
    """Otherwise the account is marked as paid and never is."""
    class Broken(FakeChain):
        def rpc(self):
            import contextlib

            class Node:
                def call(self, *a):
                    raise RuntimeError("no funds")

            return contextlib.nullcontext(Node())

    with pytest.raises(RuntimeError):
        faucetlib.pour(Broken(), faucet, KEY, "nAddr")
    assert faucet.given(KEY) is None, "so it can be tried again"


def test_an_operator_can_turn_it_off(register):
    faucet = Faucet(register, gift=GIFT, ceiling=0)
    with pytest.raises(DryToday):
        faucetlib.pour(FakeChain(), faucet, KEY, "nAddr")

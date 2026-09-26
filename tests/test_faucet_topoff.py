"""The faucet's daily top-off (2026-09-25): once a day every account is
brought back up to the gift -- 100 test coins -- with only the shortfall sent."""

import types

import pytest

from arcade import accounts as accountslib
from arcade import faucet as faucetlib

COIN = 100_000_000


class Chain:
    def __init__(self, mainnet=False):
        self.is_mainnet = mainnet
        self.sent = []

    def rpc(self):
        chain = self

        class RPC:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def call(self, method, address, amount):
                chain.sent.append((address, round(amount * COIN)))
                return "ab" * 32
        return RPC()


@pytest.fixture
def faucet(tmp_path):
    return faucetlib.Faucet(accountslib.Accounts(tmp_path / "a.sqlite"))


def test_only_the_shortfall_is_sent_and_only_once_a_day(faucet):
    chain = Chain()
    now = 1_790_000_000
    assert faucetlib.top_off(chain, faucet, "aa", "nA", 30 * COIN, now=now) == 70 * COIN
    assert chain.sent == [("nA", 70 * COIN)]
    assert faucetlib.top_off(chain, faucet, "aa", "nA", 10 * COIN, now=now + 60) == 0, \
        "once a day"
    assert faucetlib.top_off(chain, faucet, "aa", "nA", 10 * COIN, now=now + 86400) == 90 * COIN


def test_nothing_to_an_account_that_is_not_short(faucet):
    chain = Chain()
    assert faucetlib.top_off(chain, faucet, "bb", "nB", 100 * COIN) == 0
    assert faucetlib.top_off(chain, faucet, "bb", "nB", 150 * COIN) == 0
    assert faucetlib.top_off(chain, faucet, "bb", "nB", 100 * COIN - 5000) == 0, "dust"
    assert chain.sent == []


def test_never_on_a_real_chain_and_not_when_the_faucet_is_off(faucet, tmp_path):
    assert faucetlib.top_off(Chain(mainnet=True), faucet, "cc", "nC", 0) == 0
    off = faucetlib.Faucet(accountslib.Accounts(tmp_path / "b.sqlite"), gift=0)
    assert faucetlib.top_off(Chain(), off, "cc", "nC", 0) == 0


def test_the_daily_ceiling_covers_top_offs_too(tmp_path):
    f = faucetlib.Faucet(accountslib.Accounts(tmp_path / "c.sqlite"), ceiling=150 * COIN)
    chain, now = Chain(), 1_790_000_000
    assert faucetlib.top_off(chain, f, "d1", "nD1", 0, now=now) == 100 * COIN
    assert faucetlib.top_off(chain, f, "d2", "nD2", 0, now=now) == 0, "only 50 left today"
    assert f.today(now) == 100 * COIN

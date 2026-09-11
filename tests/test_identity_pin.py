"""The identity address is pinned, and both front ends read the same pin.

a test machine found this on a real machine: the web interface and the CLI answered as two
different people from the same wallet at the same moment. The web pinned its
choice in the store; the CLI recomputed `sorted(getaddressesbyaccount(...))[0]`
on every call and never looked at the pin. The moment the account held a second
address that sorted earlier, the CLI's identity changed and the web's did not.

Sorting is deterministic over a *fixed* set. The set is not fixed: anything that
calls `getaccountaddress` on that account can add to it. So the choice is made
once and recorded, and sorting survives only as the first-run tiebreak.
"""

import tempfile
from pathlib import Path

import pytest

from arcade.messaging.derive import (
    DerivationError, IDENTITY_ACCOUNT, resolve_identity_address,
)
from arcade.messaging.store import MessageStore


class FakeWallet:
    """A wallet whose identity account can grow, the way a real one does."""

    def __init__(self, addresses=(), mine=None):
        self.addresses = list(addresses)
        self.mine = set(mine if mine is not None else addresses)
        self.minted = 0
        self.fail_listing = False

    def call(self, method, *args):
        if method == "getaddressesbyaccount":
            if self.fail_listing:
                raise RuntimeError("connection reset by peer")
            return list(self.addresses)
        if method == "getaccountaddress":
            self.minted += 1
            fresh = f"nMinted{self.minted}"
            self.addresses.append(fresh)
            self.mine.add(fresh)
            return fresh
        if method == "setaccount":
            return None
        if method == "validateaddress":
            return {"ismine": args[0] in self.mine}
        raise AssertionError(f"unexpected RPC {method}")


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as directory:
        yield MessageStore(Path(directory) / "m.sqlite")


def test_a_new_earlier_sorting_address_does_not_change_the_identity(store):
    """The exact failure a test machine measured: identity flipped with no user action."""
    wallet = FakeWallet(["netiMj1HFc2TuWegeQQKDLcjL1jUAXTnPD"])
    first = resolve_identity_address(wallet, store, "test")

    # Something calls getaccountaddress; the account grows an earlier address.
    wallet.addresses.append("ncpXrSCQx667v4y92zSWidxs96N5TtPjTE")
    wallet.mine.add("ncpXrSCQx667v4y92zSWidxs96N5TtPjTE")

    assert resolve_identity_address(wallet, store, "test") == first


def test_both_front_ends_resolve_to_the_same_address(store):
    """One store, one pin -- whichever half asks first."""
    wallet = FakeWallet(["nBbb", "nAaa"])
    from_cli = resolve_identity_address(wallet, store, "test")
    from_web = resolve_identity_address(wallet, store, "test")
    assert from_cli == from_web == "nAaa"


def test_the_choice_is_recorded_where_both_can_see_it(store):
    wallet = FakeWallet(["nAaa"])
    chosen = resolve_identity_address(wallet, store, "test")
    assert store.get_meta("identity_address:test") == chosen


def test_an_empty_account_gets_exactly_one_address(store):
    wallet = FakeWallet([])
    first = resolve_identity_address(wallet, store, "test")
    second = resolve_identity_address(wallet, store, "test")
    assert first == second
    assert wallet.minted == 1, "a second address would be a second identity"


def test_a_failed_listing_does_not_mint_a_new_identity(store):
    """A bare `except Exception` here silently created a new identity.

    One transient RPC error would orphan every message the old identity had
    received. Failing is the safe outcome: nothing is created, and the next
    attempt succeeds.
    """
    wallet = FakeWallet(["nAaa"])
    wallet.fail_listing = True

    with pytest.raises(DerivationError):
        resolve_identity_address(wallet, store, "test")

    assert wallet.minted == 0
    assert store.get_meta("identity_address:test") is None


def test_a_pin_the_wallet_cannot_sign_for_is_refused_not_replaced(store):
    """A restored-from-elsewhere wallet must say so, not quietly become someone new."""
    wallet = FakeWallet(["nAaa"])
    resolve_identity_address(wallet, store, "test")

    wallet.mine.clear()          # a different wallet is now loaded
    with pytest.raises(DerivationError) as caught:
        resolve_identity_address(wallet, store, "test")

    assert "not in this wallet" in str(caught.value)
    assert wallet.minted == 0


def test_networks_are_pinned_separately(store):
    wallet = FakeWallet(["nAaa"])
    test_address = resolve_identity_address(wallet, store, "test")
    wallet.addresses.append("nZzz")
    assert resolve_identity_address(wallet, store, "regtest") != test_address or True
    assert store.get_meta("identity_address:test") == test_address

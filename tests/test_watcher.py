"""The block watcher: scan when a block lands, and never die doing it.

Messages only become visible once they are in a block, and the failure this
prevents is specific and bad -- a correspondent says "I sent it", the recipient
is looking straight at the conversation, and nothing is there because the
interface is not looking.
"""

import time

import pytest

from arcade.config import NETWORKS
from arcade.web.watcher import BlockWatcher


class FakeChain:
    def __init__(self, network="regtest", tip=100, fails=False):
        self.network = network
        self.params = NETWORKS[network]
        self.tip = tip
        self.fails = fails
        self.polls = 0

    def rpc(self):
        return self

    def __enter__(self):
        if self.fails:
            raise RuntimeError("connection refused")
        return self

    def __exit__(self, *exc):
        return False

    def get_block_count(self):
        self.polls += 1
        return self.tip


class FakeState:
    def __init__(self, messaging, ledger):
        self.messaging = messaging
        self.ledger = ledger
        self.tips = {}
        self.last_checked = {}
        self.generation = 0
        self.scans = []

    def bump_generation(self):
        self.generation += 1

    def ensure_identity(self):
        return None

    def store(self):
        raise AssertionError("no store expected in these tests")


def test_an_unchanged_tip_does_not_scan(monkeypatch):
    """A quiet chain must cost one RPC call and nothing else."""
    state = FakeState(FakeChain(tip=100), FakeChain("main", tip=5))
    watcher = BlockWatcher(state)

    scanned = []
    monkeypatch.setattr("arcade.web.watcher.Scanner",
                        lambda *a, **k: scanned.append(1))
    watcher._tick()
    first = len(scanned)
    watcher._tick()

    assert len(scanned) == first, "a tip that did not move must not rescan"


def test_a_node_that_is_down_does_not_raise(monkeypatch):
    """Nodes restart -- for a wallet switch, among other things."""
    state = FakeState(FakeChain(fails=True), FakeChain("main", fails=True))
    BlockWatcher(state)._tick()          # must simply return

    assert state.tips == {}


def test_a_failed_check_is_not_recorded_as_checked():
    """'Nothing new' and 'not looking' have to stay distinguishable."""
    state = FakeState(FakeChain(fails=True), FakeChain("main", fails=True))
    BlockWatcher(state)._tick()

    assert state.last_checked == {}


def test_a_successful_check_records_when(monkeypatch):
    state = FakeState(FakeChain(tip=100), FakeChain("main", tip=5))
    monkeypatch.setattr("arcade.web.watcher.Scanner", lambda *a, **k: None)
    before = time.time()
    BlockWatcher(state)._tick()

    assert state.last_checked["regtest"] >= before


def test_the_ledger_chain_is_scanned_public_only(monkeypatch):
    """Mainnet must never be trial-decrypted, whatever else changes here."""
    calls = []

    class Recorder:
        def __init__(self, rpc, params, store, identity=None, public_only=False):
            calls.append((params.name, public_only, identity))

        def scan(self, max_blocks=0):
            class R:
                opened = announcements = group_posts = 0
            return R()

    state = FakeState(FakeChain(tip=100), FakeChain("main", tip=5))
    state.store = lambda: _NullStore()
    monkeypatch.setattr("arcade.web.watcher.Scanner", Recorder)
    BlockWatcher(state)._tick()

    by_network = {name: public for name, public, _ in calls}
    assert by_network["main"] is True
    assert by_network["regtest"] is False


def test_an_empty_block_does_not_bump_the_generation(monkeypatch):
    """A block with nothing in it for us must not reload every open page."""
    class Empty:
        def __init__(self, *a, **k):
            pass

        def scan(self, max_blocks=0):
            class R:
                opened = announcements = group_posts = 0
            return R()

    state = FakeState(FakeChain(tip=100), FakeChain("main", tip=5))
    state.store = lambda: _NullStore()
    monkeypatch.setattr("arcade.web.watcher.Scanner", Empty)
    BlockWatcher(state)._tick()

    assert state.generation == 0


def test_finding_something_bumps_the_generation(monkeypatch):
    class Found:
        def __init__(self, *a, **k):
            pass

        def scan(self, max_blocks=0):
            class R:
                opened = 1
                announcements = 0
                group_posts = 0
            return R()

    state = FakeState(FakeChain(tip=100), FakeChain("main", tip=5))
    state.store = lambda: _NullStore()
    monkeypatch.setattr("arcade.web.watcher.Scanner", Found)
    BlockWatcher(state)._tick()

    assert state.generation > 0


def test_a_scan_that_raises_does_not_kill_the_watcher(monkeypatch):
    class Exploding:
        def __init__(self, *a, **k):
            raise RuntimeError("database is locked")

    state = FakeState(FakeChain(tip=100), FakeChain("main", tip=5))
    state.store = lambda: _NullStore()
    monkeypatch.setattr("arcade.web.watcher.Scanner", Exploding)

    BlockWatcher(state)._tick()          # must not raise
    assert state.generation == 0


class _NullStore:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

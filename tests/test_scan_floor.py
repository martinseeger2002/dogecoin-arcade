"""The scan must never crawl up from below the identity's creation height.

A real failure, found by sending a message to myself and watching the interface
report "5000 blocks, 0 candidates" over and over. The store held a scan cursor at
height 24,999 -- left by the old scan-from-zero behaviour -- against a tip of
1,482,779. `start_height()` was consulted only when there was *no* cursor, so the
fix for scanning from zero never applied to any store that had already scanned
from zero. At 5,000 blocks a press, reaching the tip would have taken about 290
presses, each one truthfully reporting that it found nothing.

The rule now: the identity's creation height is a floor on every resume, not just
a default for a fresh store. Skipping forward is safe precisely because nothing
written before a key existed can be addressed to it.
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from arcade.config import NETWORKS
from arcade.messaging.scanner import Scanner
from arcade.messaging.store import MessageStore


IDENTITY_HEIGHT = 1_482_745
TIP = 1_482_779


class FakeRpc:
    """Enough of a node to drive _resolve_fork. Records what was asked for."""

    def __init__(self, tip=TIP, known_hashes=True):
        self.tip = tip
        self.known_hashes = known_hashes
        self.scanned: list[int] = []

    def get_block_count(self):
        return self.tip

    def get_block_hash(self, height):
        if not self.known_hashes:
            raise RuntimeError("no such block")
        return f"hash-{height}"

    def get_block(self, block_hash, verbosity=2):
        self.scanned.append(int(block_hash.split("-")[1]))
        return {"tx": [], "time": 0}

    def call(self, *args, **kwargs):
        raise AssertionError(f"unexpected RPC {args}")


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as directory:
        s = MessageStore(Path(directory) / "m.sqlite")
        s.set_meta("identity_height:regtest", str(IDENTITY_HEIGHT))
        yield s


@pytest.fixture
def params():
    return NETWORKS["regtest"]


def test_a_stale_cursor_does_not_drag_the_scan_back_to_genesis(store, params):
    """The actual bug: a cursor at 24,999 against a tip 1.45 million higher."""
    store.set_scan_cursor("regtest", 24_999, "hash-24999")
    rpc = FakeRpc()

    result = Scanner(rpc, params, store).scan(max_blocks=5000)

    assert min(rpc.scanned) >= IDENTITY_HEIGHT, (
        f"scanned from {min(rpc.scanned)}, below the identity height"
    )
    assert result.blocks == TIP - IDENTITY_HEIGHT + 1
    assert result.blocks < 100, "should be tens of blocks, not thousands"


def test_a_fresh_store_starts_at_the_identity_height(store, params):
    rpc = FakeRpc()

    Scanner(rpc, params, store).scan(max_blocks=5000)

    assert min(rpc.scanned) == IDENTITY_HEIGHT


def test_a_cursor_above_the_floor_is_still_honoured(store, params):
    """Flooring must not re-scan blocks already done -- that would be as wrong."""
    store.set_scan_cursor("regtest", TIP - 5, f"hash-{TIP - 5}")
    rpc = FakeRpc()

    Scanner(rpc, params, store).scan(max_blocks=5000)

    assert rpc.scanned == list(range(TIP - 4, TIP + 1))


def test_nothing_is_scanned_when_the_cursor_is_at_the_tip(store, params):
    store.set_scan_cursor("regtest", TIP, f"hash-{TIP}")
    rpc = FakeRpc()

    assert Scanner(rpc, params, store).scan(max_blocks=5000).blocks == 0
    assert rpc.scanned == []


def test_a_reorg_rewind_still_respects_the_floor(store, params):
    """A rewind must not walk below the floor looking for agreement either."""
    store.set_scan_cursor("regtest", IDENTITY_HEIGHT + 3, "a-hash-that-no-longer-matches")
    rpc = FakeRpc()

    Scanner(rpc, params, store).scan(max_blocks=5000)

    assert rpc.scanned, "a rewind should still scan something"
    assert min(rpc.scanned) >= IDENTITY_HEIGHT

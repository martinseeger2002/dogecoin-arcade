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


# --- one starting height for everyone on a version ----------------------------
# Each machine used to begin at whatever height its own identity happened to be
# created, so two people on the same release saw different histories and neither
# could tell why -- one would see an announcement or a public post the other had
# simply never scanned.


def test_every_installation_starts_at_the_same_block(store, params):
    """No local record, so this is what a fresh install does."""
    from arcade.config import NETWORKS

    testnet = NETWORKS["test"]
    scanner = Scanner(FakeRpc(), testnet, store)
    assert scanner.start_height() == testnet.messaging_start_height


def test_a_local_record_cannot_start_earlier_than_everyone_else(store):
    """Otherwise one machine reads history the protocol says is not there."""
    from arcade.config import NETWORKS

    testnet = NETWORKS["test"]
    store.set_meta("identity_height:test", "1000")
    assert Scanner(FakeRpc(), testnet, store).start_height() == \
        testnet.messaging_start_height


def test_a_later_identity_may_start_later(store):
    """Nothing written before a key existed can be addressed to it."""
    from arcade.config import NETWORKS

    testnet = NETWORKS["test"]
    later = testnet.messaging_start_height + 5000
    store.set_meta("identity_height:test", str(later))
    assert Scanner(FakeRpc(), testnet, store).start_height() == later


def test_the_shared_height_is_a_release_decision_not_a_guess():
    """It is written down per network, not derived from whatever the tip is."""
    from arcade.config import NETWORKS

    assert NETWORKS["test"].messaging_start_height > 0
    # Chains with no declared start fall back to their activation height.
    assert NETWORKS["regtest"].messaging_start_height == 0


def test_the_mempool_is_read_before_a_block_arrives(tmp_path):
    """A message costs a block to arrive and a swap costs several. Reading
    the pool turns that into seconds, and the block promotes the row it
    already wrote rather than writing a second one (D-050)."""
    from arcade.config import NETWORKS
    from arcade.messaging.scanner import Scanner
    from arcade.messaging.store import MessageStore

    store = MessageStore(tmp_path / "m.sqlite")
    fingerprint = "aa" * 4

    # Seen in the pool: height 0, and it can be read at once.
    store.add_api_message("regtest", "tx1", 0, 1000, "nSender", b"\x01" * 32,
                          fingerprint, b"{}", protocol=1, fingerprint=b"\0" * 4)
    (row,) = store.api_messages(fingerprint, "regtest")
    assert row["height"] == 0, "not in a block yet, and readable anyway"
    first_id = row["id"]

    # The block arrives: the same row, promoted in place.
    store.add_api_message("regtest", "tx1", 500, 1200, "nSender", b"\x01" * 32,
                          fingerprint, b"{}", protocol=1, fingerprint=b"\0" * 4)
    rows = store.api_messages(fingerprint, "regtest")
    assert len(rows) == 1, "one message, not two"
    assert rows[0]["id"] == first_id, "the same row, so nothing acts on it twice"
    assert rows[0]["height"] == 500

    # A later pass must never push it back to unconfirmed.
    store.add_api_message("regtest", "tx1", 0, 9999, "nSender", b"\x01" * 32,
                          fingerprint, b"{}", protocol=1, fingerprint=b"\0" * 4)
    assert store.api_messages(fingerprint, "regtest")[0]["height"] == 500

    # The same for the sealed-message candidates.
    store.add_candidate("tx2", 0, 0, 1000, "nSender", b"body", 1, None, None)
    assert store.unopened_candidates()[0]["height"] == 0
    store.add_candidate("tx2", 501, 3, 1200, "nSender", b"body", 1, None, None)
    found = store.unopened_candidates()
    assert len(found) == 1 and found[0]["height"] == 501 and found[0]["position"] == 3


def test_a_scanner_reads_the_pool_once(tmp_path, monkeypatch):
    """A pool is looked at every few seconds and mostly does not change."""
    from arcade.config import NETWORKS
    from arcade.messaging.scanner import Scanner
    from arcade.messaging.store import MessageStore

    asked = []

    class Node:
        def call(self, method, *args):
            asked.append(method)
            if method == "getrawmempool":
                return ["a" * 64, "b" * 64]
            return {"txid": args[0], "vin": [], "vout": []}

        def get_block_count(self):
            return 1

    scanner = Scanner(Node(), NETWORKS["regtest"],
                      MessageStore(tmp_path / "m.sqlite"), public_only=True)
    scanner.scan_mempool()
    fetched = asked.count("getrawtransaction")
    assert fetched == 2, "both, the first time"
    asked.clear()
    scanner.scan_mempool()
    assert asked.count("getrawtransaction") == 0, "and neither, the second"


def test_an_index_below_a_raised_floor_moves_itself_aside(tmp_path, regtest):
    """Automate the move, never the delete (D-123).

    An index is derived from the chain, so keeping it is not the decision --
    but a program that discards on a config change discards on a mistaken
    one, and a floor a digit too high would wipe every updated node the
    moment it started. So the file is renamed after the floor that displaced
    it, and rebuilt.
    """
    import dataclasses

    from arcade.ledger import LedgerIndex

    path = tmp_path / "test-ledger.sqlite"
    regtest.generate(6)
    index = LedgerIndex(path, regtest.params, rpc_factory=lambda: regtest.rpc)
    index.sync()
    assert index.indexed_height() is not None
    was = index.indexed_height()

    floor = was + 500
    index.params = dataclasses.replace(regtest.params, activation_height=floor)
    index.sync()

    kept = tmp_path / f"test-ledger.sqlite.before-{floor}"
    assert kept.exists(), "the old index is kept, under the floor that displaced it"
    assert index.stopped is None, "and nothing is halted: this is a rebuild, not a fault"
    assert index.indexed_height() is None, "the new one starts empty"

    # A second time keeps both: a floor moved twice must not overwrite the
    # copy from the first move.
    index.sync()                                   # nothing to do; index is empty
    fresh = LedgerIndex(path, regtest.params, rpc_factory=lambda: regtest.rpc)
    fresh.sync()
    fresh.params = dataclasses.replace(regtest.params, activation_height=floor)
    fresh.sync()
    assert (tmp_path / f"test-ledger.sqlite.before-{floor}.2").exists()

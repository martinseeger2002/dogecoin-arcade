"""Chain following against a real regtest node, including a real reorg.

These tests deliberately use a live pepecoind rather than a mock. Reorg handling
is the one thing in M0 that must not be proven against a simulation, because the
failure mode -- silently keeping state from an orphaned block -- is invisible
until someone compares consensus hashes much later.
"""

import pytest

from arcade.chain import ChainFollower, ReorgTooDeep
from arcade.db import Database, StateDB, register_journalled_table

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_block (
    height INTEGER PRIMARY KEY,
    hash   TEXT NOT NULL
);
"""


class RecordingHandler:
    """Writes one journalled row per block, so rollback is observable."""

    def on_connect(self, state: StateDB, height: int, block: dict) -> None:
        state.insert("seen_block", {"height": height, "hash": block["hash"]})


@pytest.fixture
def follower(tmp_path, regtest):
    db = Database(tmp_path / "chain.sqlite")
    db.conn.executescript(SCHEMA)
    register_journalled_table("seen_block", ("height",))
    yield ChainFollower(regtest.rpc, db, regtest.params, handler=RecordingHandler())
    db.close()


def seen(db):
    return {r["height"]: r["hash"] for r in db.conn.execute("SELECT * FROM seen_block")}


def test_connects_blocks_from_activation(regtest, follower):
    regtest.generate(10)
    result = follower.sync_once()

    assert result.node_tip >= 10
    assert not result.reorged
    assert follower.db.tip()["height"] == result.node_tip
    # Every connected block was handed to the handler.
    assert len(seen(follower.db)) == len(result.connected)


def test_sync_is_idempotent(regtest, follower):
    regtest.generate(5)
    follower.sync_once()
    tip_before = follower.db.tip()["height"]

    second = follower.sync_once()
    assert second.connected == [], "a second pass with no new blocks must do nothing"
    assert follower.db.tip()["height"] == tip_before


def test_respects_max_blocks(regtest, follower):
    regtest.generate(20)
    result = follower.sync_once(max_blocks=5)
    assert len(result.connected) == 5


def test_real_reorg_is_detected_and_rolled_back(regtest, follower):
    """Mine, invalidate, mine a different longer chain, and re-sync."""
    regtest.generate(20)
    follower.sync_once()

    original_tip = follower.db.tip()
    original_height = original_tip["height"]

    # Fork three blocks below the tip.
    fork_height = original_height - 3
    doomed_hash = regtest.rpc.get_block_hash(fork_height + 1)
    hashes_before = seen(follower.db)

    regtest.invalidate(doomed_hash)
    # Build a strictly longer chain on the other side of the fork.
    regtest.generate(6)

    result = follower.sync_once()

    assert result.reorged, "the follower must notice the chain changed under it"
    assert result.fork_height == fork_height
    assert sorted(result.disconnected) == list(range(fork_height + 1, original_height + 1))

    new_tip = follower.db.tip()
    assert new_tip["height"] > original_height
    assert new_tip["hash"] == regtest.rpc.get_block_hash(new_tip["height"])

    # State from the orphaned blocks must be gone, not merely superseded.
    after = seen(follower.db)
    for height in range(fork_height + 1, original_height + 1):
        assert after.get(height) != hashes_before[height], (
            f"height {height} still holds the orphaned block's state"
        )

    # And our view must agree with the node at every height we hold.
    for height, block_hash in after.items():
        assert block_hash == regtest.rpc.get_block_hash(height)


def test_undo_journal_is_emptied_for_disconnected_blocks(regtest, follower):
    regtest.generate(10)
    follower.sync_once()
    height = follower.db.tip()["height"]

    regtest.invalidate(regtest.rpc.get_block_hash(height))
    regtest.generate(3)
    follower.sync_once()

    orphaned = follower.db.conn.execute(
        "SELECT COUNT(*) AS n FROM undo WHERE height = ?", (height,)
    ).fetchone()["n"]
    # The height is reoccupied by the new chain, so entries exist -- but they must
    # belong to exactly one block, not two chains' worth.
    assert orphaned == 1


def test_reorg_deeper_than_limit_refuses(regtest, follower):
    regtest.generate(10)
    follower.sync_once()
    follower.max_reorg_depth = 0

    # Corrupt our view so no height agrees with the node.
    follower.db.conn.execute("UPDATE block SET hash = 'deadbeef' WHERE height = (SELECT MAX(height) FROM block)")

    with pytest.raises(ReorgTooDeep):
        follower.sync_once()


def test_an_index_below_a_raised_floor_says_so(regtest, follower, monkeypatch):
    """Raising the floor above everything the index holds is not a reorg.

    It is what raising a floor means, and it happened three times in two days
    -- each time reported as "the chain reorganised further back than the
    index can unwind", with no hash compared and every hash in fact agreeing.
    A wrong diagnosis in an error message aims the investigation (D-123).
    """
    from arcade.chain import IndexBelowFloor

    regtest.generate(10)
    follower.sync_once()
    tip = follower.db.tip()["height"]

    # Through monkeypatch: the node's client is shared with every other test
    # in the session, and a replacement left on it is a defect handed to
    # whatever runs next.
    asked = []
    real = follower.rpc.get_block_hash
    monkeypatch.setattr(follower.rpc, "get_block_hash",
                        lambda h: (asked.append(h), real(h))[1])

    follower.params = follower.params.__class__(
        **{**follower.params.__dict__, "activation_height": tip + 100})
    with pytest.raises(IndexBelowFloor) as complaint:
        follower.find_fork_height()

    said = str(complaint.value)
    assert "older floor" in said and "Nothing is wrong with the chain" in said
    assert f"{tip + 100:,}" in said, "it says where the chain now starts"
    assert asked == [], "no hash is compared, because none of them is the question"


def test_a_node_that_cannot_be_asked_is_not_a_reorg(regtest, follower, monkeypatch):
    """A timeout, a refused connection or a bad password is evidence about
    the node and none at all about the chain. Walking on regardless is how a
    wallet that briefly loses its node reports a catastrophic reorg (D-123)."""
    from arcade.chain import NodeUnreachable
    from arcade.rpc import RpcError

    regtest.generate(5)
    follower.sync_once()
    follower.db.conn.execute(
        "UPDATE block SET hash = 'deadbeef' WHERE height = (SELECT MAX(height) FROM block)")

    def unreachable(height):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(follower.rpc, "get_block_hash", unreachable)
    with pytest.raises(NodeUnreachable):
        follower.find_fork_height()

    # An answer of "I do not have that height" is different: the node spoke,
    # and what it said is about the chain, so the walk goes on.
    monkeypatch.setattr(follower.rpc, "get_block_hash", lambda h: (_ for _ in ()).throw(
        RpcError(-8, "Block height out of range", "getblockhash")))
    follower.max_reorg_depth = 0
    with pytest.raises(ReorgTooDeep):
        follower.find_fork_height()

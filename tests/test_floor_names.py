"""@names survive a floor move (2026-09-25; docs/multi-user.md).

A floor move raises where the index reads tokens, inscriptions, shops, posts and
messages; `Params.names_from` stays where names began, and the blocks in between
are read for @names (tag claims and transfers, and messaging key announcements)
and nothing else. Every node derives the same owners from the chain alone.
"""

import dataclasses
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_tags import claim, engine, feed                        # noqa: F401,E402

from arcade import inscriptions as I                             # noqa: E402
from arcade import payload as P                                  # noqa: E402
from arcade.chain import IndexFromAnotherFloor                   # noqa: E402
from arcade.config import NETWORKS                               # noqa: E402
from arcade.messaging.store import MessageStore                  # noqa: E402
from arcade.state import Engine                                  # noqa: E402
from arcade.tx import ArcadeTransaction, EncodingClass           # noqa: E402

TEST = NETWORKS["test"]


def moved(to=1_600_000, names=1_496_133):
    """The testnet params after a launch floor move to `to`."""
    return dataclasses.replace(TEST, activation_height=to, messaging_start_height=to,
                               names_from=names)


# --- the heights -------------------------------------------------------------------

def test_before_a_move_nothing_changes():
    assert TEST.index_start == TEST.activation_height
    assert not TEST.names_only(TEST.activation_height)


def test_after_a_move_names_are_read_from_where_they_began():
    p = moved()
    assert p.index_start == 1_496_133
    assert p.names_only(1_500_000) and p.names_only(1_496_133)
    assert not p.names_only(1_600_000) and not p.names_only(1_496_132)


def test_no_names_from_means_the_floor_as_before():
    p = dataclasses.replace(TEST, names_from=None, activation_height=1_600_000)
    assert p.index_start == 1_600_000 and not p.names_only(1_500_000)


# --- the engine below the floor ----------------------------------------------------

def test_below_the_floor_a_name_is_claimed_and_nothing_else_happens(engine):
    eng, state, db = engine
    names_only = Engine(state, NETWORKS["regtest"], names_only=True)
    feed(names_only, state, [claim(0, "apple")])
    assert db.conn.execute("SELECT address FROM tag WHERE tag='apple'").fetchone()[0] == "nAlice"
    art = ArcadeTransaction(
        txid=f"{99:064x}", block_height=150, position=0, encoding_class=EncodingClass.C,
        sender="nAlice", reference=None,
        payload=P.AnyData(data=I.MAGIC + bytes([I.VERSION, 1]) + b"not a name").encode()
        if hasattr(I, "MAGIC") else P.AnyData(data=b"not a name at all").encode(), fee=0)
    with state.block_context(150, "h150", "p", 0, 1, 0):
        result = names_only.process(art)
    assert not result.valid and "only @names" in result.reason
    assert db.conn.execute("SELECT COUNT(*) FROM arcade_tx WHERE txid=?",
                           (f"{99:064x}",)).fetchone()[0] == 0, "passed over, not recorded"


def test_first_claim_still_wins_below_the_floor(engine):
    eng, state, db = engine
    names_only = Engine(state, NETWORKS["regtest"], names_only=True)
    feed(names_only, state, [claim(0, "apple"), claim(1, "apple", sender="nBob")])
    assert db.conn.execute("SELECT address FROM tag WHERE tag='apple'").fetchone()[0] == "nAlice"


# --- the index notices a move ------------------------------------------------------

class _Db:
    """Just what find_fork_height asks of the index."""
    def __init__(self, low, tip, built_for=None):
        import sqlite3
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE block(height INTEGER)")
        self.conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
        self.conn.executemany("INSERT INTO block VALUES(?)", [(low,), (tip,)])
        if built_for is not None:
            self.conn.execute("INSERT INTO meta VALUES('content_floor', ?)", (str(built_for),))
        self._tip = tip

    def tip(self):
        return {"height": self._tip}

    def get_meta(self, key):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key, value):
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, value))


def _follower(params, db):
    from arcade.chain import ChainFollower
    f = ChainFollower.__new__(ChainFollower)
    f.params, f.db, f.max_reorg_depth = params, db, 6
    f.rpc = None
    return f


def test_a_floor_move_sets_the_index_aside_even_though_names_start_where_they_did():
    db = _Db(low=1_496_133, tip=1_700_000, built_for=1_496_133)
    with pytest.raises(IndexFromAnotherFloor) as why:
        _follower(moved(), db).find_fork_height()
    assert "@names are read from 1,496,133" in str(why.value)


def test_an_index_from_before_the_record_adopts_its_floor_and_is_not_rebuilt():
    db = _Db(low=1_496_133, tip=1_500_000)
    try:
        _follower(TEST, db).find_fork_height()
    except IndexFromAnotherFloor:
        pytest.fail("an index built for the current floor must not be rebuilt")
    except Exception:
        pass                                   # the rpc part of the walk; not what this tests
    assert db.get_meta("content_floor") == str(TEST.activation_height)


# --- the messaging store -----------------------------------------------------------

def _stocked(tmp_path):
    store = MessageStore(tmp_path / "m.sqlite")
    store.add_key_announcement("k1", "nApple", b"\x01" * 32, "ff", 1_500_000, 1, stated=True, tag="apple")
    store.add_group_post("test", "", "p1", 1_500_000, 1, "nApple", "", "a post before launch")
    store.add_group_post("test", "", "p2", 1_600_010, 2, "nApple", "", "a post after launch")
    return store


def test_the_first_run_only_takes_note_of_the_floor(tmp_path):
    store = _stocked(tmp_path)
    assert store.clear_below_floor("test", 1_496_133) == {}
    assert store.conn.execute("SELECT COUNT(*) FROM group_post").fetchone()[0] == 2


def test_a_floor_move_clears_posts_below_it_and_keeps_every_name(tmp_path):
    store = _stocked(tmp_path)
    store.clear_below_floor("test", 1_496_133)          # before launch: noted
    gone = store.clear_below_floor("test", 1_600_000)   # the launch
    assert gone.get("group_post") == 1
    assert [r["txid"] for r in store.conn.execute("SELECT txid FROM group_post")] == ["p2"]
    assert store.key_for("nApple")["tag"] == "apple", "the name's key survives"
    assert store.clear_below_floor("test", 1_600_000) == {}, "once per floor"

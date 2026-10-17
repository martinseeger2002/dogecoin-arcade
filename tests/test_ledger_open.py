"""One connection per thread per index file, lent out by `LedgerIndex.open()`
(D-193). Until 2026-10-11 every call opened a fresh one, and the Exchange's
Tokens tab paid for sixty of them a page."""

import os
import sqlite3
import threading
from contextlib import closing

import pytest

from arcade.config import NETWORKS
from arcade.ledger import LedgerIndex


class _NoRpc:
    def __enter__(self):
        raise AssertionError("nothing here asks the node")

    def __exit__(self, *exc):
        return False


@pytest.fixture
def index(tmp_path):
    return LedgerIndex(tmp_path / "ledger.sqlite", NETWORKS["regtest"], lambda: _NoRpc())


def test_one_thread_gets_the_same_connection_and_another_gets_its_own(index):
    with index.open() as a:
        first = a.conn
    with index.open() as b:
        assert b.conn is first, "the same thread, the same connection"
        with index.open() as inner:
            assert inner.conn is first, "opened inside an open: still the same one"
    seen = []
    worker = threading.Thread(target=lambda: seen.append(index.open().conn))
    worker.start(); worker.join()
    assert seen and seen[0] is not first, "another thread has a connection of its own"


def test_handing_it_back_does_not_close_it(index):
    with closing(index.open()) as db:
        db.conn.execute("SELECT 1")
    with index.open() as db:
        assert db.conn.execute("SELECT 1").fetchone()[0] == 1, \
            "contextlib.closing hands it back; it still answers"
    db.close()
    assert index.open().conn.execute("SELECT 1").fetchone()[0] == 1


def test_a_transaction_a_crashed_caller_left_open_is_rolled_back(index):
    with index.open() as db:
        db.conn.execute("BEGIN IMMEDIATE")
        db.conn.execute("INSERT INTO meta(key, value) VALUES('left', 'open')")
        # ...and the caller dies here, without COMMIT or ROLLBACK.
    with index.open() as db:
        assert not db.conn.in_transaction
        assert db.get_meta("left") is None, "what it wrote went with it"


def test_a_connection_closed_outright_is_replaced(index):
    with index.open() as db:
        db.conn.close()
    with index.open() as db:
        assert db.conn.execute("SELECT 1").fetchone()[0] == 1


def test_the_connection_follows_the_file_not_the_path(index):
    """An index moved aside and rebuilt (`_start_again`) is a new file at the
    same path; the next open must not go on reading the old one."""
    with index.open() as db:
        db.set_meta("which", "old")
        old = db.conn
    moved = index.path.with_name("ledger.sqlite.before-1")
    for suffix in ("", "-wal", "-shm"):
        src = index.path.with_name(index.path.name + suffix)
        if src.exists():
            os.rename(src, str(moved) + suffix)
    with index.open() as db:
        assert db.conn is not old
        assert db.get_meta("which") is None, "a fresh file, not the one moved aside"
        db.set_meta("which", "new")
    with index.open() as db:
        assert db.get_meta("which") == "new"
    with pytest.raises(sqlite3.ProgrammingError):
        old.execute("SELECT 1")                    # the old connection was closed


def test_a_thread_keeps_only_so_many(tmp_path):
    from arcade import ledger as L
    first = LedgerIndex(tmp_path / "0.sqlite", NETWORKS["regtest"], lambda: _NoRpc())
    with first.open() as db:
        keep = db.conn
    for n in range(1, L.KEPT_PER_THREAD + 1):
        with LedgerIndex(tmp_path / f"{n}.sqlite", NETWORKS["regtest"], lambda: _NoRpc()).open():
            pass
    with pytest.raises(sqlite3.ProgrammingError):
        keep.execute("SELECT 1")                   # the oldest was let go
    with first.open() as db:
        assert db.conn.execute("SELECT 1").fetchone()[0] == 1, "and comes back when asked for"

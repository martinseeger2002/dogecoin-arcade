"""Local files belong to a chain era, not just to a network.

Moving a floor makes a new era inside one network. "Every row is keyed by
network" is true and answers a different question, which is how five files
full of another era's rows came to be described as fine (a test machine, D-126).
"""

import json
import sqlite3

import pytest

from arcade import era


def a_store(home, filename, table, rows, columns="network TEXT, what TEXT"):
    conn = sqlite3.connect(home / filename)
    conn.execute(f"CREATE TABLE IF NOT EXISTS {table} ({columns})")
    conn.executemany(f"INSERT INTO {table} VALUES (?, ?)", rows)
    conn.commit()
    conn.close()


def count(home, filename, table):
    conn = sqlite3.connect(home / filename)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_the_first_run_retires_what_nobody_recorded(tmp_path):
    """The rows that prompted this are the ones no floor move will catch:
    they were already there when the era was first written down."""
    a_store(tmp_path, "approvals.sqlite", "request",
            [("test", "property 3, Arcade Test"), ("main", "mainnet thing")])

    moved = era.retire_old_rows(tmp_path, "test", 1_495_420)

    assert moved == {"approvals.sqlite.request": 1}
    assert count(tmp_path, "approvals.sqlite", "request") == 1, \
        "the other network's row is not this network's business"
    assert era.recorded(tmp_path) == {"test": 1_495_420}


def test_nothing_is_deleted_without_a_copy_beside_it(tmp_path):
    a_store(tmp_path, "approvals.sqlite", "request", [("test", "a decision")])
    era.retire_old_rows(tmp_path, "test", 1_495_420)

    kept = tmp_path / "approvals.sqlite.before-1495420.json"
    assert kept.exists()
    saved = json.loads(kept.read_text())
    assert saved["network"] == "test" and saved["floor"] == 1_495_420
    assert saved["rows"] == [{"network": "test", "what": "a decision",
                              "_table": "request"}]


def test_a_second_start_on_the_same_floor_does_nothing(tmp_path):
    a_store(tmp_path, "approvals.sqlite", "request", [("test", "a decision")])
    era.retire_old_rows(tmp_path, "test", 1_495_420)
    a_store(tmp_path, "approvals.sqlite", "request", [("test", "a newer one")])

    assert era.retire_old_rows(tmp_path, "test", 1_495_420) == {}
    assert count(tmp_path, "approvals.sqlite", "request") == 1, \
        "what was written under this floor stays"


def test_a_moved_floor_retires_the_era_before_it(tmp_path):
    a_store(tmp_path, "approvals.sqlite", "request", [("test", "old era")])
    era.retire_old_rows(tmp_path, "test", 1_495_420)
    a_store(tmp_path, "approvals.sqlite", "request", [("test", "this era")])

    moved = era.retire_old_rows(tmp_path, "test", 1_500_000)
    assert moved == {"approvals.sqlite.request": 1}
    assert count(tmp_path, "approvals.sqlite", "request") == 0
    saved = json.loads((tmp_path / "approvals.sqlite.before-1500000.json").read_text())
    assert saved["was"] == 1_495_420, "which era these came from"


def test_a_chain_with_no_floor_is_left_alone(tmp_path):
    """Dogecoin mainnet is not indexed: there is no era to be wrong about."""
    a_store(tmp_path, "approvals.sqlite", "request", [("doge", "untouched")])
    assert era.retire_old_rows(tmp_path, "doge", None) == {}
    assert count(tmp_path, "approvals.sqlite", "request") == 1
    assert era.recorded(tmp_path) == {}


def test_one_network_moving_does_not_touch_another(tmp_path):
    a_store(tmp_path, "swaps.sqlite", "fill",
            [("test", "a fill"), ("main", "a mainnet fill")])
    a_store(tmp_path, "swaps.sqlite", "offer", [("test", "an offer")])
    era.retire_old_rows(tmp_path, "test", 1_495_420)
    assert count(tmp_path, "swaps.sqlite", "fill") == 1

    rows = [r for r in sqlite3.connect(tmp_path / "swaps.sqlite")
            .execute("SELECT network FROM fill")]
    assert rows == [("main",)]


def test_a_table_a_store_never_used_is_skipped_not_fatal(tmp_path):
    """A store can exist without every table in it. Aborting on that would
    abort on every start for ever, and an era never recorded is a sweep that
    never finishes."""
    a_store(tmp_path, "approvals.sqlite", "request", [("test", "a decision")])
    conn = sqlite3.connect(tmp_path / "swaps.sqlite")
    conn.execute("CREATE TABLE something_else (network TEXT)")
    conn.commit()
    conn.close()

    assert era.retire_old_rows(tmp_path, "test", 1_495_420) == \
        {"approvals.sqlite.request": 1}
    assert era.recorded(tmp_path) == {"test": 1_495_420}


def test_a_store_that_cannot_be_read_stops_the_sweep(tmp_path):
    """A lock or a corrupt page is not evidence that the era is clean, so
    nothing is moved and the era is not recorded: the next start tries the
    whole sweep again."""
    a_store(tmp_path, "approvals.sqlite", "request", [("test", "a decision")])
    (tmp_path / "swaps.sqlite").write_bytes(b"this is not a database")

    assert era.retire_old_rows(tmp_path, "test", 1_495_420) == {}
    assert count(tmp_path, "approvals.sqlite", "request") == 1, "untouched"
    assert era.recorded(tmp_path) == {}, "and the era is not claimed"


def test_every_table_named_holds_something_the_chain_assigns():
    """A list like this rots silently. The names are checked against the
    schemas that create them, so a renamed table is a failing test rather
    than a sweep that quietly stops sweeping."""
    from arcade import approvals, nodetalk, swap
    from arcade import collections as collectionlib

    schemas = {"approvals.sqlite": approvals.SCHEMA, "swaps.sqlite": swap.SCHEMA,
               "nodetalk.sqlite": nodetalk.SCHEMA,
               "collections.sqlite": collectionlib.SCHEMA}
    for filename, table, column in era.ERA_TABLES:
        schema = schemas[filename]
        assert f"CREATE TABLE IF NOT EXISTS {table}" in schema.replace(
            f"CREATE TABLE IF NOT EXISTS {table}(",
            f"CREATE TABLE IF NOT EXISTS {table} ("), \
            f"{filename} has no table called {table}"
        assert column in schema, f"{table} has no {column} column"


def test_a_runs_items_go_with_the_run(tmp_path):
    """Four collection runs carry four hundred item rows, and `item` has no
    network column at all. Retiring the run and leaving them behind makes
    them unreachable rather than gone, which is the state the audit was
    about (a test machine, D-126)."""
    conn = sqlite3.connect(tmp_path / "collections.sqlite")
    conn.execute("CREATE TABLE job (id TEXT, network TEXT, name TEXT)")
    conn.execute("CREATE TABLE item (job_id TEXT, edition INTEGER)")
    conn.executemany("INSERT INTO job VALUES (?,?,?)",
                     [("aaa", "test", "Pixel Skulls"), ("bbb", "main", "Elsewhere")])
    conn.executemany("INSERT INTO item VALUES (?,?)",
                     [("aaa", n) for n in range(100)] + [("bbb", 1)])
    conn.commit()
    conn.close()

    moved = era.retire_old_rows(tmp_path, "test", 1_495_900)

    assert moved == {"collections.sqlite.job": 1, "collections.sqlite.item": 100}
    assert count(tmp_path, "collections.sqlite", "item") == 1, \
        "the other network's run keeps its items"
    saved = json.loads((tmp_path / "collections.sqlite.before-1495900.json").read_text())
    assert sum(1 for r in saved["rows"] if r["_table"] == "item") == 100, \
        "and all of them are in the copy, not only the run"


def a_jobs_file(home, rows, items=()):
    conn = sqlite3.connect(home / "collections.sqlite")
    conn.execute("CREATE TABLE IF NOT EXISTS job "
                 "(id TEXT, network TEXT, name TEXT, status TEXT, floor INTEGER)")
    conn.execute("CREATE TABLE IF NOT EXISTS item (job_id TEXT, edition INTEGER)")
    conn.executemany("INSERT INTO job VALUES (?,?,?,?,?)", rows)
    conn.executemany("INSERT INTO item VALUES (?,?)", items)
    conn.commit()
    conn.close()


def test_a_run_from_this_floor_is_not_retired(tmp_path):
    """The first sweep was era-BLIND rather than era-accurate: "nothing
    recorded which floor these belong to" took rows that plainly belong to
    this one. It retired a finished 333-piece run seven minutes after it
    finished (a test machine, D-132)."""
    a_jobs_file(tmp_path,
                [("old", "test", "Before", "done", 1_495_420),
                 ("new", "test", "Pixel Skull", "done", 1_495_811),
                 ("unsaid", "test", "No floor recorded", "done", None)],
                items=[("old", 1), ("new", 1), ("new", 2), ("unsaid", 1)])

    moved = era.retire_old_rows(tmp_path, "test", 1_495_811)

    assert moved == {"collections.sqlite.job": 2, "collections.sqlite.item": 2}
    left = sqlite3.connect(tmp_path / "collections.sqlite")
    assert [r[0] for r in left.execute("SELECT id FROM job")] == ["new"]
    assert [r[0] for r in left.execute("SELECT job_id FROM item")] == ["new", "new"], \
        "and its items stay with it"


def test_nothing_is_swept_while_a_run_is_in_flight(tmp_path):
    """An update restarts the service and the sweep runs at start. A run that
    is paused or part-sent would lose its job and its items underneath it:
    pieces broadcast, nothing left to resume (D-132)."""
    a_store(tmp_path, "approvals.sqlite", "request", [("test", "a decision")])
    a_jobs_file(tmp_path, [("busy", "test", "Pixel Skulls", "paused", 1_495_420)])

    assert era.retire_old_rows(tmp_path, "test", 1_495_811) == {}
    assert count(tmp_path, "approvals.sqlite", "request") == 1, "nothing at all moved"
    assert era.recorded(tmp_path) == {}, "and the era is not claimed, so it tries again"
    assert "is paused" in era.a_run_in_flight(tmp_path, "test")

    # Once the run is finished the sweep goes ahead.
    conn = sqlite3.connect(tmp_path / "collections.sqlite")
    conn.execute("UPDATE job SET status = 'done'")
    conn.commit()
    conn.close()
    assert era.retire_old_rows(tmp_path, "test", 1_495_811)["approvals.sqlite.request"] == 1


def test_another_networks_run_does_not_hold_up_this_one(tmp_path):
    a_store(tmp_path, "approvals.sqlite", "request", [("test", "a decision")])
    a_jobs_file(tmp_path, [("busy", "main", "Elsewhere", "running", None)])
    assert era.retire_old_rows(tmp_path, "test", 1_495_811) == \
        {"approvals.sqlite.request": 1}


def test_a_store_written_before_the_floor_column_is_still_swept(tmp_path):
    """The sweep runs at startup, before anything migrates a file. Asking an
    older store for a column it has never had would abort it on every start,
    which is the livelock this already fails safe against elsewhere."""
    conn = sqlite3.connect(tmp_path / "collections.sqlite")
    conn.execute("CREATE TABLE job (id TEXT, network TEXT, name TEXT, status TEXT)")
    conn.execute("INSERT INTO job VALUES ('old', 'test', 'Before', 'done')")
    conn.commit()
    conn.close()

    assert era.retire_old_rows(tmp_path, "test", 1_495_811) == \
        {"collections.sqlite.job": 1}
    assert era.recorded(tmp_path) == {"test": 1_495_811}


def a_messaging_store(home, network="test"):
    """The store as the application makes it, with an era's worth in it."""
    from arcade.messaging.store import MessageStore

    store = MessageStore(home / f"{network}.sqlite")
    store.add_group_post(network, "", "a" * 64, 100, 1000, "nThem", "", "a post")
    store.add_feed_act(network, "b" * 64, 1, "a" * 64, "nThem", "", 101, 1010)
    store.add_key_announcement("c" * 64, "nThem", b"\x21" * 32, "ff", 102, 1020)
    store.save_contact(name="them", testnet_address="nThem")
    store.set_meta(f"identity_address:{network}", "nMe")
    store.set_meta(f"identity_height:{network}", "1495287")
    store.conn.commit()
    return store


def test_the_messaging_store_loses_the_era_and_keeps_the_work(tmp_path):
    """The ledger rebuilt itself and this file did not, so a node was left
    believing it was somebody it was in a dead era: the old era's posts, its
    key announcements, and a birth height under the chain's own floor. The
    address book is not era state and never goes (a test machine, D-140)."""
    store = a_messaging_store(tmp_path)
    store.close()

    moved = era.retire_old_rows(tmp_path, "test", 1_496_133)

    assert moved["test.sqlite.group_post"] == 1
    assert moved["test.sqlite.feed_act"] == 1
    assert moved["test.sqlite.key_announcement"] == 1

    conn = sqlite3.connect(tmp_path / "test.sqlite")
    try:
        assert conn.execute("SELECT COUNT(*) FROM group_post").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM key_announcement").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM contact").fetchone()[0] == 1, \
            "the address book is work, not era state"
        keys = {r[0] for r in conn.execute("SELECT key FROM meta")}
        assert "identity_height:test" not in keys, "a height below the floor"
        assert "identity_address:test" in keys, \
            "but not the identity itself: it still holds coins, and a floor " \
            "move is no reason to become somebody else"
    finally:
        conn.close()

    kept = json.loads((tmp_path / "test.sqlite.before-1496133.json").read_text())
    assert {row["_table"] for row in kept["rows"]} >= {"group_post", "feed_act",
                                                       "key_announcement"}


def test_a_message_somebody_received_is_never_swept(tmp_path):
    """A chain cannot give correspondence back."""
    store = a_messaging_store(tmp_path)
    for table in era.MESSAGING_KEPT:
        assert table not in [t for t, _ in era.MESSAGING_TABLES]
        assert table not in era.MESSAGING_WHOLE
    store.close()
    era.retire_old_rows(tmp_path, "test", 1_496_133)
    conn = sqlite3.connect(tmp_path / "test.sqlite")
    try:
        assert conn.execute("SELECT COUNT(*) FROM contact").fetchone()[0] == 1
    finally:
        conn.close()

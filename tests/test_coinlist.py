"""arcade/coinlist.py: coverage ranges, merging lists, the shared walk's segments (2026-10-25)."""
import sqlite3

from arcade import coinlist as cl
from arcade import utxos


class _Db:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(utxos.SCHEMA)


T1, T2, T3 = "a" * 64, "b" * 64, "c" * 64


def test_union_merges_touching_and_overlapping():
    assert cl.union([(5, 9), (1, 3), (4, 4), (20, 30), (25, 40)]) == [(1, 9), (20, 40)]
    assert cl.union([(5, 3)]) == []


def test_gaps():
    assert cl.gaps([(10, 20), (30, 40)], 0, 50) == [(0, 9), (21, 29), (41, 50)]
    assert cl.gaps([(0, 100)], 10, 20) == []
    assert cl.gaps([], 5, 6) == [(5, 6)]
    assert cl.covers([(0, 4), (5, 9)], 0, 9)


def test_coverage_is_watching_plus_covered():
    db = _Db()
    utxos.watch(db, "A", 100)
    assert cl.coverage(db, "A", 150) == [(100, 150)]
    cl.mark_covered(db, "A", [(10, 60)], "walk")
    cl.mark_covered(db, "A", [(61, 99)], "walk")
    assert cl.coverage(db, "A", 150) == [(10, 150)]
    assert cl.coverage(db, "B", 150) == []


def test_listing_shape_and_marks():
    db = _Db()
    utxos.watch(db, "A", 1)                    # 0 would be a watch begun on an empty index: covering nothing
    db.conn.execute("INSERT INTO utxo VALUES (?,?,?,?,?)", (T1, 0, "A", 5000, 7))
    db.conn.execute("INSERT INTO utxo VALUES (?,?,?,?,?)", (T2, 1, "A", 100000, 3))
    got = cl.listing(db, ["A"], 10, marks={(T1, 0): "ins:" + T1 + "i0"})
    assert got["A"]["coins"] == [[T2, 1, 100000, 3, ""], [T1, 0, 5000, 7, "ins:" + T1 + "i0"]]
    assert got["A"]["covered"] == [[1, 10]]


def test_merge_unions_coins_marks_and_coverage():
    a = {"A": {"coins": [[T1, 0, 500, 7, ""]], "covered": [[100, 200]]}}
    b = {"A": {"coins": [[T1.upper(), 0, 500, 7, "tok:42"], [T2, 3, 900, 150, ""]], "covered": [[0, 99]]},
         "B": {"coins": [], "covered": [[5, 6]]}}
    junk = {"A": {"coins": [["short", 0, 1, 1], [T3, 0, 0, 1], "x"], "covered": [["a", 1]]}}
    m = cl.merge([a, b, junk, "nonsense"])
    assert m["A"]["coins"] == [[T1, 0, 500, 7, "tok:42"], [T2, 3, 900, 150, ""]]
    assert m["A"]["covered"] == [[0, 200]]
    assert m["B"] == {"coins": [], "covered": [[5, 6]]}


def test_segments_every_block_read_twice_by_different_readers():
    jobs = cl.segments(0, 999, ["x", "y", "z", "w"])
    assert cl.double_read(jobs)
    assert len(jobs) == 8
    for seg in range(4):
        readers = [j["reader"] for j in jobs if j["segment"] == seg]
        assert len(readers) == 2 and len(set(readers)) == 2
    # the segments tile [0, 999] exactly
    tops = sorted({(j["bottom"], j["top"]) for j in jobs})
    assert tops[0][0] == 0 and tops[-1][1] == 999
    assert all(tops[i][1] + 1 == tops[i + 1][0] for i in range(len(tops) - 1))
    # staggered: each reader starts at the top of its own segment, highest reader first
    firsts = [j for j in jobs if j["segment"] == [k["segment"] for k in jobs if k["reader"] == j["reader"]][0]]
    assert [j["top"] for j in firsts][0] == 999


def test_segments_edge_cases():
    assert cl.segments(5, 4, ["x"]) == []
    one = cl.segments(0, 9, ["x"])
    assert one == [{"reader": "x", "top": 9, "bottom": 0, "segment": 0}] and cl.double_read(one)
    tiny = cl.segments(0, 1, ["x", "y", "z"])     # more readers than blocks: two segments, both read twice
    assert cl.double_read(tiny) and {j["reader"] for j in tiny} == {"x", "y"}


def test_agree_and_canaries():
    assert cl.agree([[T1, 0], [T2, 1]], [[T1.upper(), 0]]) == ({(T2, 1)}, set())
    assert cl.canaries_missing([[T1, 0]], [[T1, 0], [T3, 2]]) == [(T3, 2)]


def test_a_watch_begun_on_an_empty_index_covers_nothing(tmp_path):
    """2026-11-05: two Flopcoin imports watched while that index held no block said "covered 0..tip", no coins,
    and Admin called them whole; another arcade would have taken "nothing there, all of it known" from the list."""
    import sqlite3
    from arcade import coinlist as L

    class Db:
        conn = sqlite3.connect(":memory:")
    Db.conn.execute("CREATE TABLE watched (address TEXT PRIMARY KEY, since INTEGER)")
    Db.conn.execute("INSERT INTO watched VALUES ('Fempty', 0), ('Ffloor', 500)")
    assert L.coverage(Db, "Fempty", 900) == []
    assert L.coverage(Db, "Ffloor", 900) == [(500, 900)]

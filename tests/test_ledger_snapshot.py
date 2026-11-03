"""Index copies between arcades (arcade/ledger_snapshot.py, 2026-11-02: "copy on request", "majority of linked",
"a full confirmation of every index should be done as a background task after the index is complete").

A real regtest chain with a token and an @name on it; one index read from the chain makes a copy, an empty one fetches
it piece by piece, checks it, takes it, and confirms it later from its own node."""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import sqlite3
import threading
import time

import pytest

from arcade import payload as P
from arcade import tags as T
from arcade import ledger_snapshot as LS
from arcade.ledger import LedgerIndex
from arcade.regtest import RegtestNode
from test_end_to_end import send_class_c


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """Its own chain: a floor, a token and an @name above it, and enough blocks for a settled checkpoint."""
    with RegtestNode() as node:
        node.generate(200)
        alice = node.rpc.call("getnewaddress")
        node.rpc.call("sendtoaddress", alice, 1000)
        node.generate(1)
        floor = node.rpc.get_block_count() + 1
        params = dataclasses.replace(node.params, activation_height=floor,
                                     marker_address=node.rpc.call("getnewaddress"))
        send_class_c(node, alice, P.IssuanceFixed(
            ecosystem=1, property_type=2, previous_property_id=0, category="c", subcategory="s", name="Copied",
            url="", data="", amount=5 * 10**8).encode(), change_to=alice)
        node.generate(1)
        send_class_c(node, alice, P.AnyData(data=T.encode("copied", T.KIND_CLAIM)).encode(), change_to=alice)
        node.generate(130)                       # a checkpoint (every 100, 6 under the tip) above the floor
        home = tmp_path_factory.mktemp("copies")
        yield node, alice, params, home


def _index(node, params, path):
    return LedgerIndex(path, params, lambda: contextlib.nullcontext(node.rpc))


def _synced(node, params, path):
    index = _index(node, params, path)
    while (index.indexed_height() or 0) < node.rpc.get_block_count():
        index.sync(max_blocks=500)
    return index


class Share:
    """The mesh, as one in-process maker: what an arcade asks of it is answered at once from `maker`."""

    def __init__(self, maker, taker, tamper=None):
        self.maker, self.taker, self.tamper = maker, taker, tamper
        self.made_asked = []

    def ask_make(self, node, net):
        self.made_asked.append(node)

    def ask_manifest(self, node, net, sid):
        self.taker.got_manifest(node, sid, self.maker.manifest(sid))

    def ask_piece(self, node, net, sid, i):
        data = self.maker.piece(sid, i)
        if self.tamper is not None and i == 0:
            data = self.tamper(data)
        self.taker.got_piece(node, sid, i, data)


def _fetch(taker, share, tip, verify, monkeypatch, maker_snaps, node="m" * 64):
    monkeypatch.setattr(LS, "WORTH_BLOCKS", 1)
    taker.heard(node, maker_snaps.offer(), maker_snaps.indexed())
    for _ in range(200):
        taker.step(share, tip, verify=verify)
        if taker.fetch is None:
            return
        time.sleep(0.05)
    raise AssertionError("the fetch never finished")


def test_a_copy_leaves_out_this_arcades_own_addresses_and_says_what_it_is(world, tmp_path):
    node, alice, params, home = world
    index = _synced(node, params, tmp_path / "maker.sqlite")
    with index.open() as db:
        db.conn.execute("INSERT INTO watched (address, since, why) VALUES (?, 1, 'mine')", (alice,))
        db.conn.execute("CREATE TABLE IF NOT EXISTS covered (address TEXT, lo INTEGER, hi INTEGER, how TEXT, at REAL)")
        db.conn.execute("INSERT INTO covered VALUES (?, 1, 2, 'backfill', 0)", (alice,))
        db.conn.commit()
    snaps = LS.LedgerSnapshots(index, "regtest")
    made = snaps.make()
    m = made["manifest"]
    assert m["height"] == node.rpc.get_block_count() and m["hash"] == node.rpc.call("getblockhash", m["height"])
    assert m["cp"] is not None and m["cp"] >= params.activation_height and m["d"] and m["s"] and m["n"]
    copy = sqlite3.connect(made["path"])
    try:
        for table in LS.PRIVATE:
            assert copy.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, f"never this arcade's {table}"
        assert copy.execute("SELECT tag FROM tag").fetchall() == [("copied",)]
    finally:
        copy.close()
    assert snaps.offer()["have"] == "all" and LS.manifest_id(m) == made["id"]


def test_an_empty_index_takes_a_checked_copy_and_reads_on_from_its_block(world, tmp_path, monkeypatch):
    node, alice, params, home = world
    maker = LS.LedgerSnapshots(_synced(node, params, tmp_path / "maker.sqlite"), "regtest")
    maker.make()
    taker_index = _index(node, params, tmp_path / "taker.sqlite")
    taker = LS.LedgerSnapshots(taker_index, "regtest")
    d = maker.made["manifest"]["d"]
    tip = node.rpc.get_block_count()
    _fetch(taker, Share(maker, taker), tip, lambda m, mk: {"m" * 64: d, "o" * 64: d, "x" * 64: "something else"},
           monkeypatch, maker)
    assert taker.copy is not None, taker.said
    assert taker_index.indexed_height() == tip
    assert taker_index.tag_of(alice) == "copied" and taker_index.address_of("copied") == alice
    assert taker.copy["agreed"] == 2 and taker.copy["voters"] == 3 and taker.copy["state"] == "confirming"
    node.generate(2)
    taker_index.sync()
    assert taker_index.indexed_height() == tip + 2, "it reads on from the copy's block"


@pytest.mark.parametrize("votes, why", [
    (lambda d: {"m" * 64: d}, "only 1 of 1"),
    (lambda d: {"m" * 64: d, "a" * 64: "no", "b" * 64: "no"}, "only 1 of 3"),
    (lambda d: {"m" * 64: d, "a" * 64: d, "b" * 64: "no", "c" * 64: "no"}, "only 2 of 4"),
])
def test_a_copy_most_holders_do_not_agree_with_is_refused(world, tmp_path, monkeypatch, votes, why):
    node, alice, params, home = world
    maker = LS.LedgerSnapshots(_synced(node, params, tmp_path / "maker.sqlite"), "regtest")
    maker.make()
    taker_index = _index(node, params, tmp_path / "taker.sqlite")
    taker = LS.LedgerSnapshots(taker_index, "regtest")
    d = maker.made["manifest"]["d"]
    _fetch(taker, Share(maker, taker), node.rpc.get_block_count(), lambda m, mk: votes(d), monkeypatch, maker)
    assert taker.copy is None and why in taker.said
    assert taker_index.indexed_height() is None, "the index reads its node as before"


def test_a_wrong_piece_is_not_kept_and_its_sender_is_not_asked_again(world, tmp_path, monkeypatch):
    node, alice, params, home = world
    maker = LS.LedgerSnapshots(_synced(node, params, tmp_path / "maker.sqlite"), "regtest")
    maker.make()
    taker = LS.LedgerSnapshots(_index(node, params, tmp_path / "taker.sqlite"), "regtest")
    monkeypatch.setattr(LS, "WORTH_BLOCKS", 1)
    taker.heard("m" * 64, maker.offer(), maker.indexed())
    share = Share(maker, taker, tamper=lambda data: b"x" + data[1:])
    for _ in range(5):
        taker.step(share, node.rpc.get_block_count(), verify=lambda m, mk: {})
    assert 0 not in taker.fetch["have"] and "m" * 64 not in taker.offers


def test_a_copy_of_another_chain_is_refused(world, tmp_path, monkeypatch):
    node, alice, params, home = world
    maker = LS.LedgerSnapshots(_synced(node, params, tmp_path / "maker.sqlite"), "regtest")
    maker.make()
    taker_index = _index(node, params, tmp_path / "taker.sqlite")
    taker = LS.LedgerSnapshots(taker_index, "regtest")

    class OtherChain:
        def call(self, method, *args):
            return "00" * 32 if method == "getblockhash" else node.rpc.call(method, *args)
    taker_index._rpc = lambda: contextlib.nullcontext(OtherChain())
    d = maker.made["manifest"]["d"]
    _fetch(taker, Share(maker, taker), node.rpc.get_block_count(), lambda m, mk: {"m" * 64: d, "o" * 64: d},
           monkeypatch, maker)
    assert taker.copy is None and "not the one this arcade's node has" in taker.said


def test_a_copy_is_confirmed_from_this_arcades_own_node_then_the_second_index_is_deleted(world, tmp_path,
                                                                                            monkeypatch):
    node, alice, params, home = world
    maker = LS.LedgerSnapshots(_synced(node, params, tmp_path / "maker.sqlite"), "regtest")
    maker.make()
    taker_index = _index(node, params, tmp_path / "taker.sqlite")
    taker = LS.LedgerSnapshots(taker_index, "regtest")
    d = maker.made["manifest"]["d"]
    _fetch(taker, Share(maker, taker), node.rpc.get_block_count(), lambda m, mk: {"m" * 64: d, "o" * 64: d},
           monkeypatch, maker)
    conf = LS.Confirmation(taker, lambda path: _index(node, params, path))
    for _ in range(20):
        conf.one_pass()
        if taker.copy["state"] != "confirming":
            break
    assert taker.copy["state"] == "confirmed", taker.copy
    assert not conf.path.exists(), "the second index is deleted"
    assert LS.LedgerSnapshots(taker_index, "regtest").copy["state"] == "confirmed", "kept across a restart"


def test_a_copy_whose_names_differ_from_this_arcades_own_node_is_replaced_by_the_index_built_here(world, tmp_path,
                                                                                                   monkeypatch):
    """A copy that passed every check and was still wrong (here: one name moved by hand after it was installed) is
    found by the confirmation, and the index read from this arcade's own node takes its place."""
    node, alice, params, home = world
    maker = LS.LedgerSnapshots(_synced(node, params, tmp_path / "maker.sqlite"), "regtest")
    maker.make()
    taker_index = _index(node, params, tmp_path / "taker.sqlite")
    taker = LS.LedgerSnapshots(taker_index, "regtest")
    d = maker.made["manifest"]["d"]
    _fetch(taker, Share(maker, taker), node.rpc.get_block_count(), lambda m, mk: {"m" * 64: d, "o" * 64: d},
           monkeypatch, maker)
    with taker_index.open() as db:
        db.conn.execute("UPDATE tag SET address='nSomebodyElse' WHERE tag='copied'")
        db.conn.commit()
    conf = LS.Confirmation(taker, lambda path: _index(node, params, path))
    for _ in range(20):
        conf.one_pass()
        if taker.copy["state"] not in ("confirming", "differs"):
            break
    assert taker.copy["state"] == "rebuilt" and "names" in taker.copy["differs"]
    assert taker_index.address_of("copied") == alice, "the index built here is the one in use"
    assert not conf.path.exists()


def test_a_copy_is_made_only_when_asked_by_an_index_that_is_current_and_may_serve(world, tmp_path):
    node, alice, params, home = world
    index = _synced(node, params, tmp_path / "maker.sqlite")
    settings = {}
    snaps = LS.LedgerSnapshots(index, "regtest", setting=lambda k, default=None: settings.get(k, default))
    tip = node.rpc.get_block_count()
    assert not snaps.want_made(tip), "nobody asked"
    snaps.asked_to_make("b" * 64)
    assert snaps.want_made(tip)
    assert not snaps.want_made(tip + 1000), "not while this index is behind its own node"
    settings["serve_index_copies"] = False
    assert not snaps.want_made(tip) and snaps.offer() is None, "the operator turned serving off (a phone)"


def test_over_real_mesh_links_an_empty_arcade_asks_fetches_checks_and_takes_the_index(world, tmp_path, monkeypatch):
    """Three arcades on real links: two hold the index (a maker and a second holder), one starts empty. The empty one
    asks for a copy ("sq"), the holders make one, it fetches the pieces ("g"/"gb"), asks both holders' fingerprints
    (indexcheck) and takes it."""
    from arcade import index_digest
    from arcade.mesh.indexcheck import IndexCheck
    from arcade.mesh.ledgershare import LedgerShare
    from test_mesh import addr, mk, started

    node, alice, params, home = world
    monkeypatch.setattr(LS, "WORTH_BLOCKS", 1)
    monkeypatch.setattr(LS, "ASK_EVERY", 0)
    floor = params.index_start
    tip = node.rpc.get_block_count()
    arcades = {}
    for name in ("a", "c", "b"):
        index = _synced(node, params, tmp_path / f"{name}.sqlite") if name != "b" else \
            _index(node, params, tmp_path / "b.sqlite")
        snaps = LS.LedgerSnapshots(index, "regtest")
        fp = index_digest.Fingerprints(index, floor)
        arcades[name] = (index, snaps, IndexCheck(lambda fp=fp: {"regtest": fp}),
                         LedgerShare(lambda snaps=snaps: {"regtest": snaps}))

    async def go():
        nodes = {n: mk() for n in arcades}
        for n, (_i, _s, check, share) in arcades.items():
            nodes[n].add_extension(check)
            nodes[n].add_extension(share)
        await started(*nodes.values())
        try:
            nodes["b"].add_address(*addr(nodes["a"]))
            nodes["b"].add_address(*addr(nodes["c"]))
            b_index, b_snaps, b_check, b_share = arcades["b"]

            async def until(check, what, seconds=30):
                end = time.monotonic() + seconds
                while time.monotonic() < end:
                    if check():
                        return
                    await asyncio.sleep(0.05)
                raise AssertionError(f"timed out: {what} ({b_snaps.said})")
            await until(lambda: len(b_check.theirs) == 2 and len(b_snaps.heights) == 2, "b heard both holders")
            verify = lambda m, maker: b_check.votes("regtest", floor, int(m["cp"]))   # noqa: E731
            await asyncio.to_thread(b_snaps.step, b_share, tip, verify)                # far behind: asks them
            for n in ("a", "c"):
                await until(lambda n=n: arcades[n][1].asked_by, f"{n} was asked")
                _i, snaps, _c, share = arcades[n]
                assert snaps.make_in_background(tip)
                await until(lambda snaps=snaps: snaps.made is not None and not snaps.making, f"{n} made a copy")
                await share.rebalance()                                               # says it can serve it
            await until(lambda: any(o.get("have") == "all" for o in b_snaps.offers.values()), "b heard an offer")
            end = time.monotonic() + 60
            while b_snaps.copy is None and time.monotonic() < end:
                await asyncio.to_thread(b_snaps.step, b_share, tip, verify)
                await asyncio.sleep(0.05)
            assert b_snaps.copy is not None, b_snaps.said
            assert b_snaps.copy["agreed"] == 2 and b_snaps.copy["voters"] == 2
            assert b_index.indexed_height() == tip and b_index.address_of("copied") == alice
        finally:
            for n in nodes.values():
                await n.stop()
    asyncio.run(asyncio.wait_for(go(), 120))


def test_an_arcade_that_says_busy_is_left_alone_for_a_while_and_refusals_do_not_use_up_its_allowance(world, tmp_path,
                                                                                                      monkeypatch):
    """2026-11-02, live: refused asks counted against the allowance and were asked again at once, so a fetch sat at 97
    of 411 pieces. Only pieces sent count, and a busy arcade is not asked again for BUSY_PAUSE."""
    import asyncio as aio
    from arcade.mesh import ledgershare as LX
    node, alice, params, home = world
    maker = LS.LedgerSnapshots(_synced(node, params, tmp_path / "maker.sqlite"), "regtest")
    maker.make()
    taker = LS.LedgerSnapshots(_index(node, params, tmp_path / "taker.sqlite"), "regtest")
    monkeypatch.setattr(LS, "WORTH_BLOCKS", 1)
    taker.heard("m" * 64, maker.offer(), maker.indexed())
    asked = []

    class Busy(Share):
        def ask_piece(self, node, net, sid, i):
            asked.append(i)
            self.taker.piece_failed(node, sid, i, True)
    share = Busy(maker, taker)
    for _ in range(4):
        taker.step(share, node.rpc.get_block_count(), verify=lambda m, mk: {})
    assert len(asked) == 1, "asked once, then left alone"

    sent = []

    class Link:
        peer_id = "p" * 64

        async def send(self, header, blob=b""):
            sent.append(header)
    monkeypatch.setattr(LX, "PIECES_PER_MINUTE", 2)
    lx = LX.LedgerShare(lambda: {"regtest": maker})
    sid = maker.made["id"]

    async def go():
        for i in range(6):
            await lx._send(Link(), maker, {"id": i, "what": "piece", "snap": sid, "i": 0, "z": 0})
    aio.run(go())
    served = [h for h in sent if h.get("op") == "gb" and not h.get("err")]
    assert len({h["id"] for h in served}) == 2 and len(lx._asked["p" * 64]) == 2, "refusals did not count"

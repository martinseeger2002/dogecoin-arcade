"""A chain's Core data copied from a linked arcade (arcade/core_copy.py, arcade/mesh/coreboot.py; 2026-11-04:
"The bootstrap should work automatically from the coins and chains tab just like the installer does now", from "Any
linked arcade").

With a real Core (regtest pepecoind, Dogecoin 1.14's family): a copy taken with the node stopped for a moment, cut into
pieces, fetched into another node's folder, swapped in and checked with no peers -- its tip, its coin set's summary,
verifychain -- and the node's own data put back when anything is off. Never a wallet file.
"""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from arcade import core_copy as cc
from arcade.config import REGTEST
from arcade.regtest import _free_port
from arcade.rpc import RpcClient, RpcCredentials


class RegCore(cc.Core):
    """A regtest pepecoind in a datadir of its own, stopped and started the way core_copy does an installed Core."""

    def __init__(self, datadir: Path):
        self.datadir = Path(datadir)
        self.installed = {"datadir": str(self.datadir), "conf": str(self.datadir / "none.conf")}
        self.params = REGTEST
        self.port, self.p2p = _free_port(), _free_port()
        self.proc = None
        self.started_with = []

    def rpc(self, timeout: float = 120.0):
        user, _, pw = (self.datadir / "regtest" / ".cookie").read_text().partition(":")
        return RpcClient(RpcCredentials("127.0.0.1", self.port, user, pw), timeout=timeout)

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, extra=()) -> None:
        if self.running():
            return
        self.started_with.append(tuple(extra))
        (self.datadir / "regtest" / ".cookie").unlink(missing_ok=True)
        self.proc = subprocess.Popen(
            [shutil.which("pepecoind"), "-regtest", f"-datadir={self.datadir}", f"-rpcport={self.port}",
             f"-port={self.p2p}", "-server=1", "-listen=0", "-connect=0", "-dnsseed=0", "-txindex=1",
             "-fallbackfee=0.01", *extra], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        until = time.time() + 60
        while not (self.datadir / "regtest" / ".cookie").exists():
            assert time.time() < until and self.running(), "pepecoind did not start"
            time.sleep(0.1)

    def stop(self) -> None:
        if not self.running():
            return
        try:
            self.call("stop", timeout=30)
        except Exception:
            pass
        self.proc.wait(timeout=120)


@pytest.fixture
def cores():
    if not shutil.which("pepecoind"):
        pytest.skip("pepecoind not on PATH")
    made = []

    def make():
        c = RegCore(Path(tempfile.mkdtemp(prefix="arcade-regtest-copy-")))
        c.start()
        c.wait(60)
        made.append(c)
        return c
    yield make
    for c in made:
        try:
            c.stop()
        finally:
            shutil.rmtree(c.datadir, ignore_errors=True)


def _mine(core, n):
    core.call("generate", n, timeout=300)


def _fetch_all(snaps, sid, manifest, fetch):
    fetch.begin(sid, "maker", manifest)
    for n in fetch.missing():
        f, i = fetch.where(n)
        assert fetch.got(n, snaps.piece(sid, f, i))
    assert fetch.missing() == []


def test_a_copy_of_a_synced_node_is_fetched_checked_and_runs_in_another_node(cores):
    maker, taker = cores(), cores()
    _mine(maker, 120)
    to = maker.call("getnewaddress")
    maker.call("sendtoaddress", to, 12.5)
    _mine(maker, 3)
    want = maker.call("gettxoutsetinfo")
    snaps = cc.Snapshots("regtest", REGTEST, maker.installed, maker)
    manifest = snaps.make()
    sid = snaps.current()[0]
    assert maker.running() and maker.call("getblockcount") == 123, "the maker is back"
    assert manifest["height"] == 123 and manifest["hash"] == want["bestblock"]
    assert manifest["utxo"]["hash_serialized"] == want["hash_serialized"]
    names = [f["p"] for f in manifest["files"]]
    assert names and all(n.split("/")[0] in cc.FOLDERS for n in names)
    assert not any("wallet" in n.lower() or n.endswith((".cookie", "peers.dat", ".conf")) for n in names)
    assert snaps.offer()["id"] == sid and cc.manifest_id(manifest) == sid

    before = taker.call("getblockcount")
    fetch = cc.Fetch(cc.chain_dir(taker.installed, REGTEST))
    _fetch_all(snaps, sid, manifest, fetch)
    got = fetch.install(taker)
    assert got == {"height": 123, "hash": want["bestblock"]}
    assert taker.started_with[-2] == cc.ALONE and taker.started_with[-1] == (), "checked alone, then as usual"
    taker.wait(120)
    assert before == 0 and taker.call("getblockcount") == 123
    assert taker.call("gettxoutsetinfo")["hash_serialized"] == want["hash_serialized"]
    assert (taker.datadir / "regtest" / "wallet.dat").exists(), "the taker's own wallet is kept"
    assert not fetch.root.exists() and not (taker.datadir / "regtest.arcade-old").exists(), "nothing left behind"


def test_a_piece_that_is_not_the_manifest_s_is_refused(cores):
    maker = cores()
    _mine(maker, 10)
    snaps = cc.Snapshots("regtest", REGTEST, maker.installed, maker)
    manifest = snaps.make()
    sid = snaps.current()[0]
    fetch = cc.Fetch(Path(tempfile.mkdtemp()) / "regtest")
    fetch.begin(sid, "maker", manifest)
    f, i = fetch.where(0)
    good = snaps.piece(sid, f, i)
    assert not fetch.got(0, b"\0" + good[1:]) and not fetch.got(0, good + b"x")
    assert fetch.got(0, good) and 0 not in fetch.missing()
    shutil.rmtree(fetch.root.parent, ignore_errors=True)


def test_a_copy_whose_coin_set_is_not_the_manifest_s_is_refused_and_the_node_s_own_data_goes_back(cores):
    maker, taker = cores(), cores()
    _mine(maker, 30)
    _mine(taker, 7)
    own = taker.call("getbestblockhash")
    snaps = cc.Snapshots("regtest", REGTEST, maker.installed, maker)
    manifest = snaps.make()
    sid = snaps.current()[0]
    # A maker that lies about its coin set: the manifest says another summary (its id is of what it says).
    lied = json.loads(json.dumps(manifest))
    lied["utxo"]["hash_serialized"] = "00" * 32
    fetch = cc.Fetch(cc.chain_dir(taker.installed, REGTEST))
    fetch.begin(cc.manifest_id(lied), "maker", lied)
    for n in fetch.missing():
        f, i = fetch.where(n)
        assert fetch.got(n, snaps.piece(sid, f, i))
    with pytest.raises(cc.CopyError, match="coin set"):
        fetch.install(taker)
    taker.wait(120)
    assert taker.call("getbestblockhash") == own and taker.call("getblockcount") == 7, "its own chain is back"
    assert not fetch.root.exists() and not (taker.datadir / "regtest.arcade-old").exists()


def test_a_copy_file_that_changed_after_it_was_hashed_is_not_served(cores):
    maker = cores()
    _mine(maker, 5)
    snaps = cc.Snapshots("regtest", REGTEST, maker.installed, maker)
    manifest = snaps.make()
    sid = snaps.current()[0]
    p = snaps.root / sid / "data" / manifest["files"][0]["p"]
    with open(p, "ab") as fh:
        fh.write(b"more")
    assert snaps.piece(sid, 0, 0) is None and snaps.current() is None, "dropped, never served changed"


def test_an_unsynced_node_makes_no_copy():
    class Behind(cc.Core):
        def __init__(self):
            pass

        def call(self, method, *a, timeout=0):
            return {"blocks": 10, "headers": 5000, "verificationprogress": 0.01}
    with pytest.raises(cc.CopyError, match="not synced"):
        cc.take(Behind(), Path("/nonexistent"), Path("/nonexistent2"))


class Headers(cc.Core):
    """A node's header chain: heights 0..top, hash 'h<height>' (or a fork's above `fork`)."""

    def __init__(self, top, fork=None, version=1, genesis="h0"):
        self.top, self.fork, self.version, self.genesis = top, fork, version, genesis

    def _hash(self, h):
        return f"x{h}" if self.fork is not None and h > self.fork else f"h{h}"

    def call(self, method, *a, timeout=0):
        if method == "getblockhash":
            return self.genesis if a[0] == 0 else self._hash(a[0])
        if method == "getnetworkinfo":
            return {"version": self.version}
        if method == "getchaintips":
            return [{"height": self.top, "hash": self._hash(self.top), "status": "headers-only"},
                    {"height": 3, "hash": "h3", "status": "active"}]
        if method == "getblockheader":
            h = int(a[0][1:])
            return {"height": h, "previousblockhash": self._hash(h - 1)}
        raise AssertionError(method)


def test_a_copy_is_taken_only_when_its_tip_is_on_this_node_s_own_header_chain():
    m = {"genesis": "h0", "version": 1, "height": 100, "hash": "h100"}
    assert cc.header_check(Headers(120), m) is None
    assert "not the one on this node's header chain" in cc.header_check(Headers(120, fork=90), m)
    assert "waiting for its headers" in cc.header_check(Headers(60), m)
    assert "another chain" in cc.header_check(Headers(120, genesis="g"), m)
    assert "another Core version" in cc.header_check(Headers(120, version=2), m)


def test_a_manifest_naming_a_file_outside_the_chain_data_is_refused(tmp_path):
    fetch = cc.Fetch(tmp_path / "chain")
    for bad in ("../wallet.dat", "/etc/passwd", "wallets/x/wallet.dat", "blocks/../../x", "peers.dat",
                "chainstate/LOCK"):
        m = {"files": [{"p": bad, "n": 1, "s": ["00"]}], "piece": cc.PIECE}
        with pytest.raises(cc.CopyError):
            fetch.begin(cc.manifest_id(m), "maker", m)
    assert not (tmp_path / "x").exists()


def test_a_fetch_goes_on_where_it_stopped(tmp_path):
    data = os.urandom(cc.PIECE + 1000)
    m = {"files": [{"p": "blocks/blk00000.dat", "n": len(data),
                    "s": [hashlib.sha256(data[:cc.PIECE]).hexdigest(), hashlib.sha256(data[cc.PIECE:]).hexdigest()]}],
         "piece": cc.PIECE, "bytes": len(data)}
    sid = cc.manifest_id(m)
    f = cc.Fetch(tmp_path / "chain")
    f.begin(sid, "maker", m)
    assert f.got(1, data[cc.PIECE:])
    f.save(force=True)
    again = cc.Fetch(tmp_path / "chain")
    assert again.active and again.missing() == [0] and again.state["maker"] == "maker"
    assert again.got(0, data[:cc.PIECE])
    assert (again.data / "blocks/blk00000.dat").read_bytes() == data


def test_only_files_that_never_change_are_hard_linked():
    newest = {"blk": 7, "rev": 7}
    assert cc._linked(Path("chainstate/001234.ldb"), newest) and cc._linked(Path("indexes/txindex/000001.ldb"), newest)
    assert not cc._linked(Path("chainstate/000003.log"), newest) and not cc._linked(Path("chainstate/MANIFEST-000002"),
                                                                                    newest)
    assert cc._linked(Path("blocks/blk00005.dat"), newest) and not cc._linked(Path("blocks/blk00006.dat"), newest)
    assert not cc._linked(Path("blocks/rev00007.dat"), newest) and not cc._linked(Path("blocks/xor.dat"), newest)


# --- between two arcades, automatically (arcade/mesh/coreboot.py) ----------------------------------------------------

class _State:
    def __init__(self, installed):
        self._s = {"core_installed:regtest": installed, "core:regtest": "here"}

    def settings(self):
        return dict(self._s)

    def setting(self, k, default=None):
        return self._s.get(k, default)

    def set_setting(self, k, v):
        self._s[k] = v


class _Link:
    def __init__(self, peer_id, other, back):
        self.peer_id, self.other, self.back = peer_id, other, back

    async def send(self, header, blob=b""):
        await self.other.on_frame(self.back(), header, blob)


class _Node:
    def __init__(self):
        self.links = {}


def test_an_arcade_far_behind_copies_a_linked_arcade_s_chain_data_by_itself(cores, monkeypatch):
    """No click: the arcade behind asks, the synced one makes a copy, the pieces come over the link, the copy is
    checked and installed, and the Coins & chains tab says from whom."""
    import asyncio
    from arcade.mesh.coreboot import CoreBoot
    maker_core, taker_core = cores(), cores()
    _mine(maker_core, 40)
    for c in (maker_core, taker_core):
        Path(c.installed["conf"]).write_text("txindex=1\n")
    monkeypatch.setattr(cc, "WORTH_BLOCKS", 10)
    monkeypatch.setattr(cc, "header_check", lambda core, m: None)       # this node's headers: tested above
    nets = {"regtest": REGTEST}
    maker = CoreBoot(_State(maker_core.installed), core_of=lambda n: maker_core, networks=nets)
    taker = CoreBoot(_State(taker_core.installed), core_of=lambda n: taker_core, networks=nets)
    maker.node, taker.node = _Node(), _Node()
    maker.node.links["T"] = _Link("T", taker, lambda: taker.node.links["M"])
    taker.node.links["M"] = _Link("M", maker, lambda: maker.node.links["T"])

    async def go():
        for side in (maker, taker):
            side.loop = asyncio.get_running_loop()
        until = time.time() + 180
        while time.time() < until:
            await asyncio.to_thread(maker.step)
            await asyncio.to_thread(taker.step)
            await maker.rebalance()
            await taker.rebalance()
            if (taker.jobs.get("regtest") or {}).get("state") in ("done", "failed"):
                return
            await asyncio.sleep(0.05)
    asyncio.run(go())
    job = taker.jobs.get("regtest") or {}
    assert job.get("state") == "done", job
    assert "copied from" in job["step"] and "40" in job["step"]
    taker_core.wait(120)
    assert taker_core.call("getblockcount") == 40
    assert taker_core.call("getbestblockhash") == maker_core.call("getbestblockhash")
    assert taker.state.setting("core_copied:regtest")["from"] == "M"
    assert maker.status()["regtest"]["holds"]["h"] == 40, "the maker says it holds a copy"
    assert maker_core.running() and maker_core.call("getblockcount") == 40


def test_a_frame_from_a_relayed_link_is_ignored():
    import asyncio
    from arcade.mesh.coreboot import CoreBoot
    side = CoreBoot(_State({}), networks={"regtest": REGTEST})
    side.node = _Node()
    relayed = _Link("R", None, None)                 # reached through another arcade: not in node.links
    asyncio.run(side.on_frame(relayed, {"op": "hi", "nets": {"regtest": {"h": 5}}}, b""))
    assert side.heard == {}

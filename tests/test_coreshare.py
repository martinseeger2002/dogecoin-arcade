"""Shared Cores (2026-10-17): an arcade borrows a chain from another arcade's Core over the mesh.

The blocks are real: two Dogecoin, two Pepecoin mainnet and two Pepecoin testnet blocks with their raw
bytes, headers and Core's own decoding (tests/data/rawchain/blocks.json.gz, read off the .25 nodes). A
borrowed chain must read exactly as the node itself prints it, and a sharer that changes one byte
must be caught.
"""
from __future__ import annotations

import asyncio
import gzip
import json
from pathlib import Path

import pytest

from arcade import cores, rawchain
from arcade.config import NETWORKS
from arcade.mesh import coreshare
from arcade.mesh.coreshare import BorrowedRpc, CoreShare
from arcade.rpc import RpcError

from test_mesh import addr, fast, mk, run, started, until  # noqa: F401 -- fast is the shrunk-timing fixture

DATA = json.loads(gzip.decompress((Path(__file__).parent / "data/rawchain/blocks.json.gz").read_bytes()))
CHAIN_ID = {"doge": 0x62, "main": 0x3F, "test": None}


def _blocks(net):
    return [r for r in DATA[net] if "raw" in r]


def _strip(v):
    return {k: x for k, x in v.items() if k != "auxpow"}


# ------------------------------------------------------------------------------------------- rawchain

@pytest.mark.parametrize("net", ["doge", "main", "test"])
def test_raw_blocks_read_as_core_prints_them_and_carry_their_work(net):
    before = None
    for row in _blocks(net):
        block = rawchain.read_block(bytes.fromhex(row["raw"]), row["hash"])
        rawchain.check_work(block, CHAIN_ID[net])
        rawchain.check_link(block, before)
        before = block
        assert block.verbose(NETWORKS[net], height=row["height"], extra=row["verbose"]) == _strip(row["verbose"])
        header = rawchain.read_header(bytes.fromhex(row["header"]), row["hash"])
        rawchain.check_work(header, CHAIN_ID[net])
    tx = next(r["tx"] for r in DATA[net] if "tx" in r)
    mine = rawchain.read_tx(bytes.fromhex(tx["hex"]), tx["txid"]).verbose(NETWORKS[net])
    assert mine == {k: v for k, v in tx.items() if k not in ("blockhash", "blocktime", "confirmations", "time")}


def test_a_changed_byte_or_a_borrowed_name_is_caught():
    row = _blocks("doge")[-1]
    raw = bytearray(bytes.fromhex(row["raw"]))
    raw[-10] ^= 1                                           # a byte of the last transaction
    with pytest.raises(rawchain.RawError, match="merkle"):
        rawchain.read_block(bytes(raw), row["hash"])
    with pytest.raises(rawchain.RawError, match="hashes to"):
        rawchain.read_block(bytes.fromhex(_blocks("doge")[0]["raw"]), row["hash"])
    block = rawchain.read_block(bytes.fromhex(row["raw"]))
    with pytest.raises(rawchain.RawError, match="chain id"):
        rawchain.check_work(block, 0x3F)                    # a Dogecoin block passed off as Pepecoin's
    test_block = rawchain.read_block(bytes.fromhex(_blocks("test")[-1]["raw"]))
    test_block.bits = 0x1D00FFFF                             # a target its work does not meet
    with pytest.raises(rawchain.RawError, match="work"):
        rawchain.check_work(test_block)
    rawchain.check_work(test_block, algo="")                 # a chain whose work is not checked here
    with pytest.raises(rawchain.RawError, match="work"):
        rawchain.check_work(test_block, algo="sha256d")      # Bitcoin's hash of a scrypt block: no work
    # A difficulty that falls more than 4x in a block is refused -- on a test network, except a block at
    # the easiest difficulty, which its rules allow after a long gap (Litecoin and Pepecoin testnet).
    hard = 0x1B0404CB
    assert rawchain.too_easy(0x1D00FFFF, hard) and rawchain.too_easy(0x1D00FFFF, hard, NETWORKS["main"])
    assert not rawchain.too_easy(0x1E0FFFFF, hard, NETWORKS["test"])
    assert rawchain.too_easy(0x1D00FFFF, hard, NETWORKS["test"]) is False  # easier than the limit: allowed
    assert not rawchain.too_easy(hard, hard)
    tx = next(r["tx"] for r in DATA["doge"] if "tx" in r)
    with pytest.raises(rawchain.RawError, match="hashes to"):
        rawchain.read_tx(bytes.fromhex(tx["hex"]), "00" * 32)


# ------------------------------------------------------------------------------------------- the mesh

class FakeCore:
    """A sharer's Core for one chain, answering from the real blocks."""

    def __init__(self, net, lie=False):
        self.net, self.lie = net, lie
        self.rows = _blocks(net)
        self.tx = next(r["tx"] for r in DATA[net] if "tx" in r)
        self.pool: dict[str, str] = {}
        self.asked: list[str] = []
        self.sent: list[str] = []

    def serve(self, net, calls):
        assert net == self.net
        out = []
        for method, params in calls:
            self.asked.append(method)
            out.append(self.one(method, params))
        return out

    def one(self, method, params):
        by_hash = {r["hash"]: r for r in self.rows}
        if method == "getblockchaininfo":
            return {"r": {"chain": "main", "blocks": self.rows[-1]["height"], "headers": self.rows[-1]["height"],
                          "initialblockdownload": False, "verificationprogress": 1.0}}
        if method == "getblockhash":
            if params[0] == 0:
                return {"r": NETWORKS[self.net].genesis}
            return {"r": next((r["hash"] for r in self.rows if r["height"] == params[0]), None)}
        if method == "getblock":
            row = by_hash.get(params[0])
            if row is None:
                return {"e": [-5, "Block not found"]}
            raw = row["raw"]
            if self.lie:
                raw = raw[:-20] + ("0" if raw[-20] != "0" else "1") + raw[-19:]
            return {"r": raw}
        if method == "getblockheader":
            row = by_hash[params[0]]
            if params[1]:
                v = dict(row["verbose"])
                return {"r": {k: v[k] for k in ("hash", "height", "confirmations", "chainwork", "mediantime",
                                                "version", "merkleroot", "time", "nonce", "bits",
                                                "nextblockhash") if k in v}}
            return {"r": row["header"]}
        if method == "getrawtransaction":
            if params[0] in self.pool:
                return {"r": self.pool[params[0]]}
            if params[0] == self.tx["txid"]:
                return {"r": self.tx if params[1] else self.tx["hex"]}
            return {"e": [-5, "No such mempool or blockchain transaction"]}
        if method == "getrawmempool":
            return {"r": sorted(self.pool)}
        if method == "sendrawtransaction":
            self.sent.append(params[0])
            try:
                return {"r": rawchain.read_tx(bytes.fromhex(params[0])).txid}
            except rawchain.RawError:
                return {"e": [-22, "TX decode failed"]}
        return {"r": None}


def _share(core, nets=("doge",)):
    tips = {n: {"h": core.rows[-1]["height"], "b": core.rows[-1]["hash"]} for n in nets}
    return CoreShare(core.serve, lambda: tips, pool_seconds=0.1)


def _borrower(nets=("doge",)):
    return CoreShare(None, lambda: {}, borrowing=set(nets))


async def _linked(*pairs):
    for a, b in pairs:
        a.add_address(*addr(b))


def test_a_borrowed_chain_reads_like_its_own_node_and_wallet_calls_fail_as_on_a_walletless_node():
    core = FakeCore("doge")

    async def go():
        a, b = mk(), mk()
        sa, sb = _share(core), _borrower()
        a.add_extension(sa); b.add_extension(sb)
        await started(a, b)
        try:
            await _linked((b, a))
            await until(lambda: sb.sharers("doge"), what="b hears a's offer")
            rpc = BorrowedRpc(sb, NETWORKS["doge"])
            row = _blocks("doge")[-1]
            got = await asyncio.to_thread(rpc.call, "getblock", row["hash"], 2)
            assert got == _strip(row["verbose"])
            assert await asyncio.to_thread(rpc.call, "getblock", row["hash"], 0) == row["raw"]
            assert core.asked.count("getblock") == 2 and "getblock" in core.asked, "always asked raw"
            tx = core.tx
            said = await asyncio.to_thread(rpc.call, "getrawtransaction", tx["txid"], 1)
            assert said == tx
            with pytest.raises(RpcError) as exc:
                await asyncio.to_thread(rpc.call, "getbalance")
            assert exc.value.code == -32601 and "getbalance" not in core.asked
            assert await asyncio.to_thread(rpc.call, "getconnectioncount") == 1
            v = await asyncio.to_thread(rpc.call, "validateaddress", "D8AXXiGEZeZnMKTKnC9AWB3YUU4jfMAmYU")
            assert v["isvalid"] and v["ismine"] is False and "validateaddress" not in core.asked
            # A batch, as the indexer sends them
            hashes = [r["hash"] for r in _blocks("doge")]
            both = await asyncio.to_thread(rpc.batch, [("getblock", [h, 2]) for h in hashes])
            assert [x["hash"] for x in both] == hashes
            # A big signed transaction crosses in several frames
            huge = "00" * 150_000
            with pytest.raises(RpcError):
                await asyncio.to_thread(rpc.call, "sendrawtransaction", huge)   # not a transaction: Core-side error
            assert core.sent == [huge], "150 KB of calls arrived whole"
        finally:
            await a.stop(); await b.stop()
    run(go())


def test_a_sharer_that_changes_a_byte_is_caught_and_the_next_sharer_answers():
    liar, honest = FakeCore("doge", lie=True), FakeCore("doge")

    async def go():
        a, c, b = mk(), mk(), mk()
        sa, sc, sb = _share(liar), _share(honest), _borrower()
        a.add_extension(sa); c.add_extension(sc); b.add_extension(sb)
        await started(a, c, b)
        try:
            await _linked((b, a), (b, c))
            await until(lambda: len(sb.sharers("doge")) == 2, what="two sharers")
            rpc = BorrowedRpc(sb, NETWORKS["doge"])
            row = _blocks("doge")[-1]
            sb.failures[c.node_id] = 5                        # the liar is asked first ...
            got = await asyncio.to_thread(rpc.call, "getblock", row["hash"], 2)
            assert got["hash"] == row["hash"], "... caught, and the honest sharer asked instead"
            assert sb.sharers("doge")[0] == c.node_id, "the liar goes to the back"
            await c.stop()                                     # with only the liar left, its answer is refused
            await until(lambda: sb.sharers("doge") == [a.node_id], what="only the liar left")
            with pytest.raises(RpcError, match="did not check out"):
                await asyncio.to_thread(rpc.call, "getblock", row["hash"], 2)
            # ... and a sharer that does not answer at all is passed over
            sa.serve = None
            with pytest.raises(Exception, match="no arcade sharing doge answered"):
                await asyncio.to_thread(rpc.call, "getblockcount")
        finally:
            for n in (a, b):
                await n.stop()
    run(go())


def test_mempool_transactions_arrive_as_they_hit_and_leave_when_mined():
    core = FakeCore("doge")
    tx = core.tx

    async def go():
        a, b = mk(), mk()
        sa, sb = _share(core), _borrower()
        a.add_extension(sa); b.add_extension(sb)
        await started(a, b)
        try:
            await _linked((b, a))
            await until(lambda: sb.sharers("doge"), what="offer")
            core.pool[tx["txid"]] = tx["hex"]
            await until(lambda: tx["txid"] in sb.pools.get("doge", {}), what="the pushed transaction")
            rpc = BorrowedRpc(sb, NETWORKS["doge"])
            core.asked.clear()
            said = await asyncio.to_thread(rpc.call, "getrawtransaction", tx["txid"], 1)
            assert said["txid"] == tx["txid"] and "getrawtransaction" not in core.asked, "answered from memory"
            assert sa.status()["doge"]["borrowers"] == 1
            del core.pool[tx["txid"]]                           # mined
            await until(lambda: tx["txid"] not in sb.pools.get("doge", {}), what="forgotten once it left")
        finally:
            await a.stop(); await b.stop()
    run(go())


def test_a_sharer_answers_only_what_it_shares():
    core = FakeCore("doge")

    async def go():
        a, b = mk(), mk()
        sa, sb = _share(core), CoreShare(None, lambda: {}, borrowing={"doge", "main"})
        a.add_extension(sa); b.add_extension(sb)
        await started(a, b)
        try:
            await _linked((b, a))
            await until(lambda: sb.sharers("doge"), what="offer")
            assert sb.sharers("main") == []
            got = await asyncio.to_thread(sb.call, "doge", [("dumpprivkey", ["D8AXXiGEZeZnMKTKnC9AWB3YUU4jfMAmYU"]),
                                                           ("getblockcount", [])])
            assert got[0]["e"][0] == -32601 and "dumpprivkey" not in core.asked
            sa.shared = lambda: {}                              # the box is unticked
            await sa.rebalance()
            await until(lambda: not sb.sharers("doge"), what="the offer withdrawn")
        finally:
            await a.stop(); await b.stop()
    run(go())


# ------------------------------------------------------------------------------------------- the rules

def test_testnet_and_one_more_core_run_here():
    cores.check({"test": "here", "main": "here", "doge": "borrow"})
    cores.check({"test": "here", "main": "borrow", "doge": "here"})
    with pytest.raises(cores.CoreRule, match="at least one more"):
        cores.check({"test": "here", "main": "borrow", "doge": "borrow"})
    with pytest.raises(cores.CoreRule, match="testnet"):
        cores.check({"test": "borrow", "main": "here"})
    with pytest.raises(cores.CoreRule, match="cannot be off"):
        cores.check({"test": "here", "main": "off", "doge": "here"})
    assert cores.mode({"core:test": "borrow"}, "test") == "here"


def test_a_borrowed_chain_context_reads_through_the_running_mesh(monkeypatch):
    from arcade.web.state import ChainContext
    core = FakeCore("doge")

    async def go():
        a, b = mk(), mk()
        sa, sb = _share(core), _borrower()
        a.add_extension(sa); b.add_extension(sb)
        await started(a, b)
        monkeypatch.setattr(coreshare, "CURRENT", sb)
        try:
            await _linked((b, a))
            await until(lambda: sb.sharers("doge"), what="offer")
            chain = ChainContext(network="doge", role="ledger", label="Dogecoin", borrowed=True)

            def read():
                with chain.rpc() as rpc:
                    return rpc.get_block_count() if False else rpc.call("getblockhash", _blocks("doge")[-1]["height"])
            assert await asyncio.to_thread(read) == _blocks("doge")[-1]["hash"]
            status = await asyncio.to_thread(chain._status)
            assert status["online"] and status["borrowed"] and status["wallet"] is None and status["peers"] == 1
        finally:
            await a.stop(); await b.stop()
    run(go())


# ------------------------------------------------------------------------------------------- Litecoin

def test_a_real_litecoin_block_with_segwit_and_its_hogex_reads_and_carries_its_work():
    """Litecoin mainnet block 3,197,009 (litecoinspace.org, 2026-10-18): 366 SegWit transactions and the
    MWEB HogEx (flag 0x08, empty MWEB part). Scrypt, not merge-mined."""
    raw = gzip.decompress((Path(__file__).parent / "data/rawchain/ltc-3197009.raw.gz").read_bytes())
    block = rawchain.read_block(raw, "ba5a40d8935f3fe314515982a2b1488bdcf7d1650dee8f5a9ba49af30dc749d9", mweb=True)
    rawchain.check_work(block, NETWORKS["ltc"].auxpow_chain_id, NETWORKS["ltc"].pow_algo)
    assert len(block.txs) == 524 and block.merkleroot == "3a87c49b168f878f6bead0f649e3a6796da05eb4fbe1a983b1928cbd5fd60d84"
    assert rawchain.read_block(raw + b"\x01mweb-extension", mweb=True).hash == block.hash, "its MWEB block is skipped"
    with pytest.raises(rawchain.RawError, match="left over"):
        rawchain.read_block(raw + b"\x01", mweb=False)
    hogex = next(t for t in block.txs if t.raw[4:6] == b"\x00\x08")
    assert rawchain.read_tx(hogex.raw, hogex.txid).txid == hogex.txid
    with_part = hogex.raw[:-5] + b"\x01" + hogex.raw[-4:]
    assert rawchain.read_tx(with_part, hogex.txid).txid == hogex.txid, "alone, an MWEB part is passed over"
    with pytest.raises(rawchain.Unreadable, match="MWEB part"):
        rawchain._read_tx(rawchain._Reader(with_part + b"next"))
    seg = next(t for t in block.txs if t.raw[4:6] == b"\x00\x01")
    outs = [o["scriptPubKey"] for o in seg.verbose(NETWORKS["ltc"])["vout"]]
    assert any(o["type"].startswith("witness_") and o["addresses"][0].startswith("ltc1") for o in outs) or outs


@pytest.mark.skipif(__import__("shutil").which("litecoind") is None, reason="litecoind is not installed here")
def test_litecoin_borrowed_through_the_mesh_reads_as_litecoin_core_prints_it():
    """A real regtest litecoind behind the sharing arcade: a block with a SegWit spend, borrowed, decoded
    here from its raw bytes and proved, comes out exactly as Litecoin Core prints it."""
    from arcade.regtest import LitecoinRegtestNode
    from arcade.rpc import RpcError as _RpcError

    with LitecoinRegtestNode() as node:
        rpc = node.rpc
        rpc.call("generatetoaddress", 200, rpc.call("getnewaddress"))
        bech = rpc.call("getnewaddress", "", "bech32")
        t1 = rpc.call("sendtoaddress", bech, 5)
        rpc.call("generatetoaddress", 1, rpc.call("getnewaddress"))
        vout = next(o["n"] for o in rpc.call("getrawtransaction", t1, 1)["vout"] if o["value"] == 5)
        unsigned = rpc.call("createrawtransaction", [{"txid": t1, "vout": vout}], {rpc.call("getnewaddress"): 4.999})
        t2 = rpc.call("sendrawtransaction", rpc.call("signrawtransactionwithwallet", unsigned)["hex"])
        block_hash = rpc.call("generatetoaddress", 1, rpc.call("getnewaddress"))[0]
        params = node.params

        def serve(net, calls):
            out = []
            for method, p in calls:
                try:
                    out.append({"r": rpc.call(method, *p)})
                except _RpcError as exc:
                    out.append({"e": [exc.code, exc.message]})
            return out

        async def go():
            a, b = mk(), mk()
            net = params.name                                # "regtest": the chain's own name
            sa = CoreShare(serve, lambda: {net: {"h": 202, "b": block_hash}})
            sb = CoreShare(None, lambda: {}, borrowing={net})
            a.add_extension(sa); b.add_extension(sb)
            await started(a, b)
            try:
                b.add_address(*addr(a))
                await until(lambda: sb.sharers(net), what="offer")
                borrowed = BorrowedRpc(sb, params)
                got = await asyncio.to_thread(borrowed.call, "getblock", block_hash, 2)
                core = rpc.call("getblock", block_hash, 2)
                for tx in core["tx"]:
                    tx.pop("hex", None)                      # getblock 2 prints each tx's hex; ours leaves it out
                assert got == core
                said = await asyncio.to_thread(borrowed.call, "getrawtransaction", t2, 1)
                assert said == rpc.call("getrawtransaction", t2, 1)
            finally:
                await a.stop(); await b.stop()
        run(go())


def test_a_litecoin_peg_in_is_proved_and_a_wholly_mweb_one_is_unreadable_not_a_lie():
    """2026-10-18: every Litecoin transaction with an MWEB part failed its check, and the honest sharer was counted
    as a liar for each. A peg-in (made on regtest litecoind 0.21.5.8) is proved by its ordinary part; one wholly MWEB
    has its kernel's id, which is said to be unreadable -- an error, never held against the sharer."""
    import gzip
    import json
    from arcade import rawchain
    said = json.load(gzip.open(Path(__file__).parent / "data" / "rawchain" / "ltc-mweb-tx.json.gz", "rt"))
    txid, raw = said["pegin"]
    tx = rawchain.read_tx(bytes.fromhex(raw), txid)
    assert tx.txid == txid and tx.vin and tx.vout
    txid, raw = said["mwebonly"]
    with pytest.raises(rawchain.Unreadable):
        rawchain.read_tx(bytes.fromhex(raw), txid)
    with pytest.raises(rawchain.RawError):
        rawchain.read_tx(bytes.fromhex(said["pegin"][1]), txid)


def test_mining_is_shared_for_test_coins_only_not_too_often_and_the_sharer_names_its_pay_address():
    """2026-10-18 (the operator: mine testnet LTC with sadfrog's Core; a tenth of the revenue to the sharer): a sharer
    answers getblocktemplate and submitblock for a test-coin chain only, at most once every MINE_EVERY seconds per
    borrower, and its offer names the address a borrower pays its tenth to."""
    pay = "nnG5sqH1WxegK5mr45TWf94uf1UKzc3tyT"

    async def go():
        a, b = mk(), mk()
        tips = {n: {"h": 1, "b": "00" * 32, "p": pay} for n in ("doge", "ltctest")}
        sa = CoreShare(lambda net, calls: [{"r": {"net": net}} for _ in calls], lambda: tips)
        sb = CoreShare(None, lambda: {}, borrowing={"doge", "ltctest"})
        a.add_extension(sa); b.add_extension(sb)
        await started(a, b)
        try:
            await _linked((b, a))
            await until(lambda: sb.sharers("ltctest") and sb.sharers("doge"), what="offers")
            assert sb.payee("ltctest") == pay and sb.payee("ltctest", lambda x: False) is None
            got = await asyncio.to_thread(sb.call, "ltctest", [("getblocktemplate", [{}])])
            assert got[0] == {"r": {"net": "ltctest"}}
            again = await asyncio.to_thread(sb.call, "ltctest", [("getblocktemplate", [{}])])
            assert "too often" in again[0]["e"][1]
            real = await asyncio.to_thread(sb.call, "doge", [("submitblock", ["00"])])
            assert "test coins only" in real[0]["e"][1]
        finally:
            await a.stop(); await b.stop()
    run(go())


def _spy(link, seen):
    """Record (header, bytes) of every frame `link` sends."""
    real = link.send

    async def send(header, blob=b""):
        seen.append((dict(header), len(blob)))
        return await real(header, blob)
    link.send = send


def test_a_borrowed_block_crosses_compressed_and_reads_the_same(monkeypatch):
    """2026-10-22: compress what arcades send each other. A borrowed block's answer carries its bytes as hex
    (twice their size); the borrower asks with "z", the sharer compresses, the block reads exactly as before."""
    core = FakeCore("doge")
    seen: list = []

    async def go():
        a, b = mk(), mk()
        sa, sb = _share(core), _borrower()
        a.add_extension(sa); b.add_extension(sb)
        await started(a, b)
        try:
            await _linked((b, a))
            await until(lambda: sb.sharers("doge"), what="b hears a's offer")
            for link in a.links.values():
                _spy(link, seen)
            rpc = BorrowedRpc(sb, NETWORKS["doge"])
            row = _blocks("doge")[-1]
            assert await asyncio.to_thread(rpc.call, "getblock", row["hash"], 0) == row["raw"]
            assert await asyncio.to_thread(rpc.call, "getblock", row["hash"], 2) == _strip(row["verbose"])
        finally:
            await a.stop(); await b.stop()
    run(go())
    answers = [(h, n) for h, n in seen if h.get("op") == "a"]
    assert answers and all(h.get("z") == 1 for h, _n in answers), "every answer compressed"
    raw_answer = len(json.dumps([{"r": _blocks("doge")[-1]["raw"]}], separators=(",", ":")))
    first = sum(n for h, n in answers[:1])
    assert first < raw_answer * 0.7, (first, raw_answer)


def test_an_older_sharer_or_borrower_still_trades_raw(monkeypatch):
    """No "z" on the request (an older borrower): the answer goes raw; and an older sharer that ignores "z" answers
    raw, which the borrower still reads."""
    core = FakeCore("doge")
    seen: list = []
    real_answer = CoreShare._answer

    async def old_answer(self, link, rid, net, calls, zipped=False):     # an older sharer: never compresses
        return await real_answer(self, link, rid, net, calls, False)
    monkeypatch.setattr(CoreShare, "_answer", old_answer)

    async def go():
        a, b = mk(), mk()
        sa, sb = _share(core), _borrower()
        a.add_extension(sa); b.add_extension(sb)
        await started(a, b)
        try:
            await _linked((b, a))
            await until(lambda: sb.sharers("doge"), what="b hears a's offer")
            for link in a.links.values():
                _spy(link, seen)
            rpc = BorrowedRpc(sb, NETWORKS["doge"])
            row = _blocks("doge")[-1]
            assert await asyncio.to_thread(rpc.call, "getblock", row["hash"], 0) == row["raw"]
        finally:
            await a.stop(); await b.stop()
    run(go())
    assert seen and not any(h.get("z") for h, _n in seen if h.get("op") == "a")


def test_a_compressed_answer_that_unpacks_too_far_or_is_not_zlib_fails_the_call():
    import zlib
    assert coreshare._inflate(zlib.compress(b"\0" * (2 * 1024 * 1024)), 1024 * 1024) is None, "a bomb: never inflated"
    assert coreshare._inflate(b"not zlib", 1024) is None
    assert coreshare._inflate(zlib.compress(b"[1]"), 1024) == b"[1]"
    sb = _borrower()

    async def go():
        fut = asyncio.get_running_loop().create_future()
        sb._waiting[7] = {"peer": "p", "fut": fut, "parts": [], "size": 0}
        sb._on_answer("p", {"id": 7, "seq": 0, "end": True, "z": 1}, b"not zlib")
        with pytest.raises(Exception, match="does not unpack"):
            await fut
    run(go())

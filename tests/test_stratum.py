"""The stratum server for ASICs on this network (arcade/stratum.py): stratum v1 as an ASIC speaks it, against a node
that checks every block it is sent -- its merkle root from its own transactions, and its proof of work. A real
DigiByte Core takes them in test_stratum_regtest.py. And the Mining module: how each chain is mined (arcade/mining.py),
and its switch."""
import dataclasses
import json
import socket
import struct
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_web import app_state, client  # noqa: F401,E402

from arcade import blockminer, mining, modules as modulelib, stratum  # noqa: E402
from arcade.config import NETWORKS  # noqa: E402
from arcade.script import b58check_encode  # noqa: E402

LTC_REGTEST = dataclasses.replace(NETWORKS["ltctest"], name="ltcreg", label="Litecoin regtest", mweb=False)
PAY = b58check_encode(LTC_REGTEST.pubkeyhash_version, bytes(range(20)))
EASY = 0x207FFFFF                       # regtest's bits: about every other hash is a block


def _tx(n: int) -> dict:
    data = bytes([n]) * 60
    return {"data": data.hex(), "txid": blockminer.sha256d(data)[::-1].hex()}


class Node:
    """getblocktemplate and submitblock, the second checking the block as a node would."""
    def __init__(self, txs=3, bits=EASY, algo="scrypt"):
        self.height, self.prev, self.algo = 101, "11" * 32, algo
        self.txs, self.bits, self.blocks, self.asked = [_tx(i) for i in range(txs)], bits, [], []

    def call(self, method, params):
        self.asked.append((method, params))
        if method == "getblocktemplate":
            return {"version": 0x20000000, "previousblockhash": self.prev, "height": self.height,
                    "coinbasevalue": 50 * 10 ** 8, "bits": f"{self.bits:08x}", "curtime": 1_700_000_000,
                    "transactions": self.txs}
        if method == "submitblock":
            raw = bytes.fromhex(params[0])
            header, rest = raw[:80], raw[80:]
            assert rest[0] == 1 + len(self.txs)
            # The coinbase: version, 1 input, its script, 1 output, locktime (no witness: no commitment here).
            at = 1 + 4 + 1 + 36
            size = rest[at]
            at += 1 + size + 4
            outs = rest[at]
            at += 1
            for _ in range(outs):
                at += 8
                at += 1 + rest[at]
            at += 4
            coinbase = rest[1:at]
            assert rest[at:] == b"".join(bytes.fromhex(t["data"]) for t in self.txs), "the template's own"
            root = blockminer.merkle_root([blockminer.sha256d(coinbase)]
                                          + [bytes.fromhex(t["txid"])[::-1] for t in self.txs])
            assert header[36:68] == root, "the header commits to these transactions"
            assert header[4:36] == bytes.fromhex(self.prev)[::-1]
            assert blockminer.pow_hash(header, self.algo) <= blockminer.target_of(self.bits)
            self.blocks.append(raw)
            self.prev, self.height = blockminer.sha256d(header)[::-1].hex(), self.height + 1
            return None
        raise ValueError(method)


class Asic:
    """A stratum v1 client that hashes as an ASIC would, in Python."""
    def __init__(self, port: int, algo: str = "scrypt"):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.file, self.algo, self.n = self.sock.makefile("r"), algo, 0
        self.difficulty, self.job, self.extranonce1, self.told = 1.0, None, b"", False

    def ask(self, method, params):
        self.n += 1
        self.sock.sendall((json.dumps({"id": self.n, "method": method, "params": params}) + "\n").encode())
        while True:
            msg = json.loads(self.file.readline())
            if msg.get("id") == self.n:
                return msg
            self.heard(msg)

    def heard(self, msg):
        if msg.get("method") == "mining.set_difficulty":
            self.difficulty, self.told = msg["params"][0], True
        elif msg.get("method") == "mining.notify":
            self.job = msg["params"]

    def wait_job(self, seconds=10.0):
        self.sock.settimeout(seconds)
        while self.job is None or not self.told:           # work may come before the difficulty: wait for both
            self.heard(json.loads(self.file.readline()))

    def solve(self, en2: bytes, want_block: bool = True, start: int = 0):
        jid, prev, c1, c2, branch, version, bits, ntime, _ = self.job
        coinbase = bytes.fromhex(c1) + self.extranonce1 + en2 + bytes.fromhex(c2)
        root = stratum.fold(blockminer.sha256d(coinbase), [bytes.fromhex(b) for b in branch])
        prev_le = b"".join(bytes.fromhex(prev)[i:i + 4][::-1] for i in range(0, 32, 4))
        share = stratum.DIFF1[self.algo] / self.difficulty
        block = blockminer.target_of(int(bits, 16))
        for nonce in range(start, start + 100_000):
            header = (struct.pack("<I", int(version, 16)) + prev_le + root + struct.pack("<I", int(ntime, 16))
                      + struct.pack("<I", int(bits, 16)) + struct.pack("<I", nonce))
            value = blockminer.pow_hash(header, self.algo)
            if value <= share and (value <= block) == want_block:
                return jid, en2.hex(), ntime, f"{nonce:08x}"
        raise AssertionError("no share found")


@pytest.fixture
def served():
    node = Node()
    server = stratum.Server(node.call, LTC_REGTEST, "scrypt", PAY, port=0, host="127.0.0.1", difficulty=1e-9).start()
    port = server._server.sockets[0].getsockname()[1]
    for _ in range(100):
        if server.job is not None:
            break
        time.sleep(0.05)
    yield node, server, port
    server.stop()


def test_the_merkle_branch_folds_to_the_root_of_every_size_of_block():
    for n in range(0, 9):
        txids = [blockminer.sha256d(bytes([i])) for i in range(n)]
        coinbase = blockminer.sha256d(b"cb")
        assert stratum.fold(coinbase, stratum.merkle_branch(txids)) == blockminer.merkle_root([coinbase] + txids), n


def test_an_asic_mines_a_block_the_node_takes_over_stratum(served):
    node, server, port = served
    asic = Asic(port)
    said = asic.ask("mining.subscribe", ["test-asic/1.0"])["result"]
    asic.extranonce1, size = bytes.fromhex(said[1]), said[2]
    assert size == stratum.EXTRANONCE2_SIZE and len(asic.extranonce1) == 4
    assert asic.ask("mining.authorize", ["rig1.a", "x"])["result"] is True
    asic.wait_job()
    assert asic.difficulty == pytest.approx(1e-9)

    share = asic.solve(b"\x00\x00\x00\x01", want_block=False)
    assert asic.ask("mining.submit", ["rig1.a", *share])["result"] is True, "a share, not a block"
    assert asic.ask("mining.submit", ["rig1.a", *share])["error"][0] == 22, "the same share twice"
    assert node.blocks == []

    found = asic.solve(b"\x00\x00\x00\x02")
    assert asic.ask("mining.submit", ["rig1.a", *found])["result"] is True
    for _ in range(100):
        if node.blocks:
            break
        time.sleep(0.05)
    assert len(node.blocks) == 1 and server.blocks[0]["result"] == "accepted" and server.blocks[0]["worker"] == "rig1.a"
    coinbase_out = node.blocks[0][80 + 1:]
    assert blockminer.output_script(PAY, LTC_REGTEST) in coinbase_out, "the block pays the operator's address"

    stale = asic.ask("mining.submit", ["rig1.a", "ff", "00000003", found[2], found[3]])
    assert stale["error"][0] == 21, "a job that is gone"
    status = server.status()
    assert status["miners"][0]["worker"] == "rig1.a" and status["miners"][0]["shares"] == 2
    asic.sock.close()


def test_a_share_under_the_difficulty_is_refused(served):
    node, server, port = served
    asic = Asic(port)
    asic.extranonce1 = bytes.fromhex(asic.ask("mining.subscribe", [])["result"][1])
    asic.ask("mining.authorize", ["rig2", "x"])
    asic.wait_job()
    asic.ask("mining.suggest_difficulty", [1e12])           # far beyond what one hash of this nonce is worth
    jid, en2, ntime, _ = asic.job[0], "00000009", asic.job[7], None
    said = asic.ask("mining.submit", ["rig2", jid, en2, ntime, "00000000"])
    assert said["result"] is False and said["error"][0] == 23
    asic.sock.close()


def test_version_rolling_is_offered_within_bip310s_mask_and_checked():
    server = stratum.Server(Node().call, LTC_REGTEST, "scrypt", PAY, port=0, host="127.0.0.1")
    sent = []

    class Writer:
        def get_extra_info(self, _):
            return ("10.0.0.50", 4000)
        def write(self, data):
            sent.append(json.loads(data))
    miner = stratum.Miner(server, Writer(), 1)
    server._answer(miner, {"id": 1, "method": "mining.configure",
                           "params": [["version-rolling"], {"version-rolling.mask": "ffffffff"}]})
    assert sent[-1]["result"] == {"version-rolling": True, "version-rolling.mask": "1fffe000"}
    assert server._allowed("10.0.0.50") and server._allowed("10.0.0.2") and server._allowed("127.0.0.1")
    assert not server._allowed("8.8.8.8"), "a stranger on the internet is refused unless the operator says"


def test_a_chain_an_asic_cannot_mine_here_is_refused_with_why():
    with pytest.raises(stratum.StratumError, match="merge-mined"):
        stratum.Server(Node().call, NETWORKS["doge"], "scrypt", "x")
    with pytest.raises(stratum.StratumError, match="not mined with sha256d"):
        stratum.Server(Node().call, NETWORKS["ltc"], "sha256d", "x")
    assert stratum.asic_algos(NETWORKS["dgb"]) == ("sha256d", "scrypt")
    assert stratum.asic_algos(NETWORKS["dgbtest"]) == ("sha256d", "scrypt")
    assert stratum.asic_algos(NETWORKS["ltc"]) == ("scrypt",)


class Ctx:
    def __init__(self, net, borrowed=False):
        self.network, self.params, self.borrowed, self.label = net, NETWORKS[net], borrowed, NETWORKS[net].label


def test_each_chain_says_how_it_is_mined_here():
    dgbtest, dgb, doge, ltc = (mining.ways(Ctx(n)) for n in ("dgbtest", "dgb", "doge", "ltc"))
    assert dgbtest == {"cpu": {"by": "node", "algos": ["odo", "qubit", "skein"]},
                       "asic": {"algos": ["sha256d", "scrypt"]}}
    assert "real coins" in dgb["cpu"]["why"] and dgb["asic"] == {"algos": ["sha256d", "scrypt"]}
    assert "real coins" in doge["cpu"]["why"] and "merge-mined" in doge["asic"]["why"]
    assert ltc["asic"] == {"algos": ["scrypt"]}
    borrowed = mining.ways(Ctx("ltctest", borrowed=True))
    assert borrowed["cpu"] == {"by": "cpu", "algos": ["scrypt"]} and "another arcade" in borrowed["asic"]["why"]


def test_mining_is_a_module_off_in_lite_until_switched_on(app_state):
    assert modulelib.module_of("/admin/api/mining") == modulelib.module_of("/admin/api/mine") == "mining"
    assert modulelib.module_of("/admin/api/stratum") == modulelib.module_of("/rpc/work/ltctest") == "mining"
    assert "mining" in modulelib.enabled({}) and "mining" not in modulelib.enabled({"edition": "lite"})
    on = modulelib.switch({"edition": "lite"}, "mining", True)
    assert "mining" in modulelib.enabled({"edition": "lite", "modules": on})


def test_admin_says_how_each_chain_is_mined_and_runs_the_stratum_server(client, monkeypatch):
    app, state = client
    local, write = {"host": "127.0.0.1:8420"}, {"x-arcade-admin": "1"}
    said = app.get("/admin/api/mining", headers=local).json()
    assert {"chains", "cpu", "stratum"} <= set(said) and said["stratum"]["running"] is False
    refused = app.post("/admin/api/stratum", headers={**local, **write}, json={"on": True, "net": "nowhere"})
    assert refused.status_code == 400 and "no nowhere chain" in refused.json()["detail"]
    assert app.post("/admin/api/stratum", headers={**local, **write}, json={"port": 80}).status_code == 400
    state.set_setting("edition", "lite")
    state.apply_modules()
    try:
        assert app.get("/admin/api/mining", headers=local).status_code == 404, "off in Lite until switched on"
    finally:
        state.set_setting("edition", None)
        state.apply_modules()

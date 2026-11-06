"""The shared walk across arcades (2026-10-25): every segment read by two arcades, canaries in every job, a
segment two readers disagree on read again here, the arcade that was wrong left out, and only what this chain says
is real filed. The operator: "Twice + canaries" -- "the odds of a bug happening and the odds of a spot check catching the
bug would be extremely low"."""
import contextlib
import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

from arcade import coinlist, utxos
from arcade.script import b58check_encode
from arcade.web import app as appmod
from arcade.web.app import create_app
from arcade.web.state import AppState, ChainContext


def _addr(params, seed, sh=False):
    h = hashlib.new("ripemd160", hashlib.sha256(seed.encode()).digest()).digest()
    return b58check_encode(params.scripthash_version if sh else params.pubkeyhash_version, h)


def _tx(n):
    return hashlib.sha256(str(n).encode()).hexdigest()


def _arcade(tmp_path):
    (tmp_path / "here").mkdir()
    st = AppState(home=tmp_path / "here",
                  messaging=ChainContext(network="regtest", role="messaging", label="Testnet",
                                         datadir=Path("/nonexistent")),
                  ledger=ChainContext(network="main", role="ledger", label="Mainnet", datadir=Path("/nonexistent")))
    TestClient(create_app(st))
    return st


def test_a_walk_is_read_twice_wrong_readers_rest_and_only_real_coins_are_filed(tmp_path, no_nodes, monkeypatch):
    here = _arcade(tmp_path)
    chain = here.messaging
    P = chain.params
    W1, W2 = _addr(P, "imported one"), _addr(P, "imported two")
    CAN_PKH, CAN_SH = _addr(P, "canary"), _addr(P, "canary sh", sh=True)
    HI = 999
    # the chain: the wallets' outputs, and outputs to addresses this arcade already watches (the canaries' pool)
    chain_outputs = [[_tx(i), 0, 1000 + i, h, a] for i, (h, a) in enumerate(
        [(900, W1), (700, W2), (450, W1), (120, W2), (30, W1)])]
    known = [[_tx(f"{a}{h}"), 1, 5000, h, a] for h in range(10, HI, 60) for a in (CAN_PKH, CAN_SH)]
    spent = {_tx(3)}                                    # W2's coin at 120 was spent since
    with contextlib.closing(here.token_index(chain).open()) as db:
        for a in (CAN_PKH, CAN_SH):
            utxos.watch(db, a, 0, why="test")
        for txid, vout, sats, h, a in known:
            db.conn.execute("INSERT INTO utxo VALUES (?,?,?,?,?)", (txid, vout, a, sats, h))

    def honest(top, bottom, addresses):
        return [m for m in chain_outputs + known if bottom <= m[3] <= top and m[4] in addresses]

    behaviour = {
        "self": honest,
        "good.example.com": honest,
        "liar.example.com": lambda t, b, a: [m for m in honest(t, b, a) if m[0] != _tx(2)],   # drops W1's coin at 450
        "blind.example.com": lambda t, b, a: [m for m in honest(t, b, a) if m[4] != CAN_SH],  # misses P2SH outputs
    }
    readers = ["good.example.com", "liar.example.com", "blind.example.com"]
    monkeypatch.setattr(type(chain), "status", lambda self: {"online": True})

    class Rpc:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def batch(self, calls):
            out = []
            for _, (txid, vout, mempool) in calls:
                m = next((m for m in chain_outputs if m[0] == txid and m[1] == vout), None)
                out.append(None if m is None or txid in spent else
                           {"confirmations": HI - m[3] + 1, "value": m[2] / 1e8, "scriptPubKey": {"addresses": [m[4]]}})
            return out

        def get_block_count(self):
            return HI
    monkeypatch.setattr(type(chain), "rpc", lambda self: Rpc())
    from arcade import coinwalk
    monkeypatch.setattr(coinwalk.Reader, "can", lambda self, c: {"files": True, "rpc": True})
    monkeypatch.setattr(coinwalk.Reader, "read",
                        lambda self, c, top, bottom, addresses, progress=None: behaviour["self"](top, bottom, addresses))

    jobs = {}

    class Answer:
        def __init__(self, body, code=200):
            self.status_code, self._b = code, body

        def json(self):
            return self._b

    def ask(url, **kw):
        host = url.split("/")[2]
        if "/coins/walk/can" in url:
            return Answer({"files": True, "rpc": True})
        jid = url.rsplit("/", 1)[1]
        return Answer({"state": "done", "matches": jobs[jid]})

    def post(url, **kw):
        host, body = url.split("/")[2], kw["json"]
        jid = f"{host}-{len(jobs)}"
        jobs[jid] = behaviour[host](body["top"], body["bottom"], body["addresses"])
        return Answer({"id": jid})
    monkeypatch.setattr(appmod, "_ask_arcade", ask)
    monkeypatch.setattr(appmod, "_post_arcade", post)
    monkeypatch.setattr(appmod, "WALK_POLL", 0)
    with here.store() as store:
        for i, h in enumerate(readers):
            store.add_mesh_announcement(f"t{i}", here.messaging.network, f"n{i}", f"wss://{h}/mesh", 443, f"{i:02x}" * 32, 0, 0)

    got = here.shared_walk(chain, [W1, W2], 0, HI)
    assert set(got["readers"]) == {"self", *readers}
    assert got["verified"] == [[0, HI]], "every segment ends verified: agreed by two, or read again here"
    assert not got["failed"]
    assert got["filed"] == 4, "five coins found, one spent since: four filed, each checked on this chain"
    with contextlib.closing(here.token_index(chain).open()) as db:
        assert {c["txid"] for c in utxos.unspent(db, W1)} == {_tx(0), _tx(2), _tx(4)}, "the liar's dropped coin too"
        assert {c["txid"] for c in utxos.unspent(db, W2)} == {_tx(1)}
        assert coinlist.covers(coinlist.coverage(db, W1, HI), 0, HI)
    import threading
    import time
    until = time.time() + 10
    while time.time() < until and any(t.name.startswith("walk ") for t in threading.enumerate()):
        time.sleep(0.05)                                 # late reads land and are judged
    # Chunks go to whoever is free (2026-11-05), so which reads were compared depends on who finished first: every
    # reader whose read was wrong -- a canary missed, or coins other than the chain's -- rests; the honest one never.
    for r in here.walk_store.reads(got["walk"]):
        right = honest(r["top"], r["bottom"], {W1, W2})
        wrong = r["missing"] or sorted(map(tuple, r["found"])) != sorted(map(tuple, right))
        if wrong and r["reader"] != "self":
            assert r["reader"] in here.walk_resting, r
    assert "good.example.com" not in here.walk_resting, "the honest reader of a disagreement is not punished"
    assert here.walk_store.get(got["walk"])["outcome"] == "whole"


def test_segments_with_one_reader_are_read_once_with_canaries(tmp_path):
    jobs = coinlist.segments(0, 99, ["self"])
    assert jobs == [{"reader": "self", "top": 99, "bottom": 0, "segment": 0}]


# --- kept on disk, and the mesh's Cores read too (2026-11-05: "I thought that was how we designed the walk") ---

def _small_chunks(monkeypatch):
    from arcade import walkstore
    monkeypatch.setattr(walkstore, "CHUNK_MIN", 100)
    monkeypatch.setattr(walkstore, "CHUNKS_ABOUT", 10)


def test_a_walk_cut_short_goes_on_from_where_it_was(tmp_path, no_nodes, monkeypatch):
    """A walk is kept chunk by chunk: one cut short (a reader failing, the arcade restarting) is left unfinished with
    its good reads, and the next start reads only the chunks still missing."""
    _small_chunks(monkeypatch)
    here = _arcade(tmp_path)
    chain = here.messaging
    W = _addr(chain.params, "cut short")
    coins = [[_tx(f"c{h}"), 0, 777, h, W] for h in (950, 520, 130)]
    monkeypatch.setattr(type(chain), "status", lambda self: {"online": True})
    from arcade import coinwalk
    monkeypatch.setattr(coinwalk.Reader, "can", lambda self, c: {"files": True, "rpc": True})
    monkeypatch.setattr(appmod, "_ask_arcade", lambda url, **kw: (_ for _ in ()).throw(OSError("away")))
    asked, broken = [], {"below": 500}

    def read(self, c, top, bottom, addresses, progress=None):
        asked.append(top)
        if bottom < broken["below"]:
            raise OSError("the node went away")
        return [m for m in coins if bottom <= m[3] <= top and m[4] in addresses]
    monkeypatch.setattr(coinwalk.Reader, "read", read)
    monkeypatch.setattr(appmod, "_file_checked_coins", lambda st, ch, a, cs: len(cs))
    first = here.shared_walk(chain, [W], 0, 999)
    assert first["failed"], "the chunks below 500 could not be read"
    cut = here.walk_store.unfinished("regtest")
    assert [w["id"] for w in cut] == [first["walk"]], "left unfinished, to go on later"
    done_before = {r["top"] for r in here.walk_store.reads(first["walk"]) if r["ok"]}
    assert done_before and all(t >= 500 for t in done_before)
    asked.clear()
    broken["below"] = -1                                 # the node is back
    second = here.shared_walk(chain, [W], 0, 999, walk=cut[0])
    assert second["walk"] == first["walk"] and not second["failed"]
    assert second["verified"] == [[0, 999]]
    assert not (set(asked) & done_before), "no chunk read before is read again"
    assert here.walk_store.unfinished("regtest") == [] and here.walk_store.get(first["walk"])["outcome"] == "whole"


def test_the_mesh_s_shared_cores_read_too_and_an_arcade_counts_once(tmp_path, no_nodes, monkeypatch):
    """Every Core another arcade shares over the mesh is a reader of its own ("core:<node>"), read from here a batch
    of raw blocks at a time -- unless that arcade already reads at its web address."""
    _small_chunks(monkeypatch)
    here = _arcade(tmp_path)
    chain = here.messaging
    W = _addr(chain.params, "mesh read")
    coins = [[_tx(f"m{h}"), 0, 555, h, W] for h in (910, 410)]
    monkeypatch.setattr(type(chain), "status", lambda self: {"online": True})
    from arcade import coinwalk
    from arcade.mesh import coreshare
    TWIN, OWN = "ab" * 32, "cd" * 32
    monkeypatch.setattr(coreshare, "lenders", lambda net: [(TWIN, 1), (OWN, 1)])
    monkeypatch.setattr(coreshare, "lent_rpc", lambda peer, params: ("rpc-of", peer))
    over = []

    def read_over(self, rpc, params, top, bottom, addresses, progress=None, batch=50):
        over.append(rpc[1])
        return [m for m in coins if bottom <= m[3] <= top and m[4] in addresses]
    monkeypatch.setattr(coinwalk.Reader, "can", lambda self, c: {"files": True, "rpc": True})
    monkeypatch.setattr(coinwalk.Reader, "read", lambda self, c, top, bottom, addresses, progress=None:
                        [m for m in coins if bottom <= m[3] <= top and m[4] in addresses])
    monkeypatch.setattr(coinwalk.Reader, "read_over", read_over)

    class Answer:
        status_code = 200

        def __init__(self, body):
            self._b = body

        def json(self):
            return self._b
    # twin.example.com reads at its web address and is the arcade whose Core is TWIN: counted once, not twice
    monkeypatch.setattr(appmod, "_ask_arcade", lambda url, **kw: Answer({"files": True, "rpc": True, "node": TWIN})
                        if "/coins/walk/can" in url else Answer({"state": "done", "matches": []}))
    monkeypatch.setattr(appmod, "_post_arcade", lambda url, **kw: Answer({"id": "j"}))
    monkeypatch.setattr(appmod, "WALK_POLL", 0)
    with here.store() as store:
        store.add_mesh_announcement("t0", here.messaging.network, "n0", "wss://twin.example.com/mesh", 443, TWIN, 0, 0)
    monkeypatch.setattr(appmod, "_file_checked_coins", lambda st, ch, a, cs: len(cs))
    got = here.shared_walk(chain, [W], 0, 999)
    assert set(got["readers"]) == {"self", "twin.example.com", "core:" + OWN}
    assert "core:" + TWIN not in got["readers"], "an arcade read at its web address is not read again through its Core"
    assert over and set(over) == {OWN}, "the mesh's Core is read from here"
    assert got["verified"] == [[0, 999]]


def test_reading_over_a_mesh_core_asks_a_batch_of_blocks_at_a_time(tmp_path):
    """Reader.read_over: block hashes, then raw blocks, `batch` a round trip; the outputs paying the address found."""
    import hashlib as _h
    from arcade import blkscan, coinwalk
    from arcade.config import NETWORKS
    params = NETWORKS["regtest"]
    W = _addr(params, "over the mesh")
    from arcade.script import b58check_decode
    pkh = b58check_decode(W)[1]

    def block(paid: bool, h: int) -> bytes:
        script = bytes([0x76, 0xa9, 20]) + (pkh if paid else bytes(20)) + bytes([0x88, 0xac])
        tx = (b"\x01\x00\x00\x00" + b"\x01" + bytes(32) + b"\xff\xff\xff\xff" + b"\x01" + bytes([h % 250])
              + b"\xff\xff\xff\xff" + b"\x01" + (12345).to_bytes(8, "little") + bytes([len(script)]) + script
              + b"\x00\x00\x00\x00")
        header = (1).to_bytes(4, "little") + bytes(32) + _h.sha256(tx).digest() + bytes(12)
        return header + b"\x01" + tx

    calls = []

    class Rpc:
        def batch(self, items):
            calls.append((items[0][0], len(items)))
            if items[0][0] == "getblockhash":
                return [f"{h:064x}" for (_, (h,)) in items]
            return [block(int(bh, 16) in (7, 93), int(bh, 16)).hex() for (_, (bh, _raw)) in items]
    found = coinwalk.Reader(tmp_path).read_over(Rpc(), params, 99, 0, [W], {}, batch=40)
    assert sorted(m[3] for m in found) == [7, 93] and all(m[4] == W and m[2] == 12345 for m in found)
    assert calls == [("getblockhash", 40), ("getblock", 40), ("getblockhash", 40), ("getblock", 40),
                     ("getblockhash", 20), ("getblock", 20)], "100 blocks in three round trips of each kind"
    assert blkscan.targets([W], params)


def test_a_block_the_files_lack_is_read_over_rpc(tmp_path, monkeypatch):
    """pepecoinarcade.com 2026-11-05: its Flopcoin block files lacked block 80,597 and both its halves of a walk failed;
    the range is read over the node's RPC instead."""
    from arcade import blkscan, coinwalk
    r = coinwalk.Reader(tmp_path)

    class Files:
        def refresh(self):
            pass

        def chain(self, *a):
            pass

    class Rpc:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_block_count(self):
            return 10

        def get_block_hash(self, h):
            return "00" * 32

    class Chain:
        network = "flop"
        params = None

        def rpc(self):
            return Rpc()
    monkeypatch.setattr(r, "_block_files", lambda c: Files())
    monkeypatch.setattr(blkscan, "scan_parallel", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("block 6c16 at 80597 is not in the block files (refresh first)")))
    monkeypatch.setattr(r, "_read_rpc", lambda c, top, bottom, addresses, progress: [["t", 0, 1, top, "F"]])
    progress = {}
    assert r.read(Chain(), 10, 0, ["F"], progress) == [["t", 0, 1, 10, "F"]]
    assert progress["how"] == "rpc" and "not in the block files" in progress["fell_back"]


def test_a_reader_that_comes_after_the_walk_began_joins_it(tmp_path, no_nodes, monkeypatch):
    """2026-11-05: a walk resumed right after a restart began before the mesh's links were up -- two readers, never
    the three Flopcoin Cores that came a minute later. A running walk looks for readers again and they join."""
    import time as _time
    _small_chunks(monkeypatch)
    here = _arcade(tmp_path)
    chain = here.messaging
    W = _addr(chain.params, "late reader")
    coins = [[_tx(f"l{h}"), 0, 321, h, W] for h in (990, 15)]
    monkeypatch.setattr(type(chain), "status", lambda self: {"online": True})
    from arcade import coinwalk
    from arcade.mesh import coreshare
    LATE = "ef" * 32
    asked = {"n": 0}

    def lenders(net):
        asked["n"] += 1
        return [] if asked["n"] == 1 else [(LATE, 1)]
    monkeypatch.setattr(coreshare, "lenders", lenders)
    monkeypatch.setattr(coreshare, "lent_rpc", lambda peer, params: ("rpc-of", peer))
    monkeypatch.setattr(coinwalk.Reader, "can", lambda self, c: {"files": True, "rpc": True})

    def slow(self, c, top, bottom, addresses, progress=None):
        _time.sleep(0.4)
        return [m for m in coins if bottom <= m[3] <= top and m[4] in addresses]
    monkeypatch.setattr(coinwalk.Reader, "read", slow)
    late = []
    monkeypatch.setattr(coinwalk.Reader, "read_over", lambda self, rpc, params, top, bottom, addresses, progress=None,
                        batch=50: late.append(top) or [m for m in coins if bottom <= m[3] <= top and m[4] in addresses])
    monkeypatch.setattr(appmod, "_ask_arcade", lambda url, **kw: (_ for _ in ()).throw(OSError("away")))
    monkeypatch.setattr(appmod, "WALK_READERS_EVERY", 0)
    monkeypatch.setattr(appmod, "_file_checked_coins", lambda st, ch, a, cs: len(cs))
    got = here.shared_walk(chain, [W], 0, 999)
    assert "core:" + LATE in got["readers"], "the Core that came later joined"
    assert late, "and read chunks"
    assert got["verified"] == [[0, 999]] and got["filed"] == 2

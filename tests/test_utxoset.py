"""The whole unspent set of a small chain, by address, and its data-carrying blocks (arcade/utxoset.py, tbuuol
2026-10-25). Blocks here are made in Kotia's own shape -- nTime in every transaction, a block signature after them --
and read through rawchain, as the real ones are."""
import hashlib
import pathlib
import sys

import pytest

from arcade import rawchain, utxoset as U
from arcade.config import NETWORKS
from arcade.script import b58check_encode
from arcade.txbuild import op_return_script, p2pkh_script, varint

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

KOT = NETWORKS["kot"]
A, B, C = (b58check_encode(46, bytes([n]) * 20) for n in (1, 2, 3))
COIN = 100_000_000


def _sha(b: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


def tx(ins, outs, ntime=1_700_000_000):
    """A Kotia transaction: version, nTime, inputs ((txid, vout), or None for a coinbase), outputs."""
    raw = (1).to_bytes(4, "little") + ntime.to_bytes(4, "little") + varint(len(ins))
    for i in ins:
        if i is None:
            raw += b"\0" * 32 + b"\xff" * 4 + b"\x02\x01\x01" + b"\xff" * 4
        else:
            raw += bytes.fromhex(i[0])[::-1] + i[1].to_bytes(4, "little") + b"\x00" + b"\xff" * 4
    raw += varint(len(outs))
    for value, script in outs:
        raw += value.to_bytes(8, "little") + varint(len(script)) + script
    return raw + b"\0" * 4


def txid(raw):
    return _sha(raw)[::-1].hex()


def block(prev, txs, version=0x20000000, time=1_700_000_000):
    """(hash, raw) of a Kotia block: header, transactions, an empty block signature."""
    root = bytes.fromhex(rawchain.merkle_root([txid(t) for t in txs]))[::-1]
    header = (version.to_bytes(4, "little") + bytes.fromhex(prev)[::-1] + root + time.to_bytes(4, "little")
              + (0x1E0FFFFF).to_bytes(4, "little") + (0).to_bytes(4, "little"))
    named = (rawchain.scrypt_hash(header) if version < KOT.scrypt_headers_below else _sha(header))[::-1].hex()
    return named, header + varint(len(txs)) + b"".join(txs) + b"\x00"


def chain():
    """Block 0 pays A (never a coin); 1 pays A 5; 2 spends it: B 3, A 1.99 change, an OP_RETURN; 3 spends B's 3 into
    a coin for C and 0.99 back to B, and spends C's in the same block, back to C. Returns [(height, hash, raw)]."""
    out = []
    g = tx([None], [(50 * COIN, p2pkh_script(A))])
    h0, r0 = block("00" * 32, [g], version=1)              # an old header: named by scrypt, as Kotia's genesis is
    cb1 = tx([None], [(5 * COIN, p2pkh_script(A))])
    h1, r1 = block(h0, [cb1])
    spend = tx([(txid(cb1), 0)], [(3 * COIN, p2pkh_script(B)), (0, op_return_script(b"asset")),
                                  (199_000_000, p2pkh_script(A))])
    h2, r2 = block(h1, [tx([None], [(0, b"")]), spend])
    make = tx([(txid(spend), 0)], [(2 * COIN, p2pkh_script(C)), (99_000_000, p2pkh_script(B))])
    again = tx([(txid(make), 0)], [(19_000_000 * 10, p2pkh_script(C))])
    h3, r3 = block(h2, [tx([None], [(0, b"")]), make, again])
    out = [(0, h0, r0), (1, h1, r1), (2, h2, r2), (3, h3, r3)]
    return out, {"cb1": txid(cb1), "spend": txid(spend), "make": txid(make), "again": txid(again)}


def parsed(blocks):
    return [(h, bh, U._read(raw, bh, KOT)) for h, bh, raw in blocks]


def test_a_kotia_genesis_is_named_by_scrypt():
    blocks, _ = chain()
    h0, raw = blocks[0][1], blocks[0][2]
    assert rawchain.read_block(raw, h0, timed=True, signed=True, scrypt_below=7).hash == h0
    with pytest.raises(rawchain.RawError):
        rawchain.read_block(raw, h0, timed=True, signed=True)       # sha256d would name it otherwise


def test_build_keeps_only_what_is_unspent_by_address(tmp_path):
    blocks, ids = chain()
    s = U.UtxoSet(tmp_path / "u.sqlite", KOT)
    said = s.build(parsed(blocks), tip=3)
    assert s.balance(A) == 199_000_000                               # block 0's 50 is never a coin; 5 was spent
    assert s.unspent(B) == [{"txid": ids["make"], "vout": 1, "value": 99_000_000, "height": 3, "kind": U.WALLET}]
    assert s.unspent(C) == [{"txid": ids["again"], "vout": 0, "value": 190_000_000, "height": 3, "kind": U.WALLET}]
    assert said["coins"] == 3 and said["addresses"] == 3 and said["height"] == 3 and said["best"] == blocks[3][1]
    # Only the block whose transaction carries an OP_RETURN, as an array of its txids.
    assert s.data_blocks() == [(2, [ids["spend"]])]
    # 49 bytes a coin, nothing else per coin.
    assert len(s.conn.execute("SELECT utxos FROM coins WHERE address=?", (B,)).fetchone()[0]) == U.RECORD.size


def test_a_block_that_does_not_follow_is_refused(tmp_path):
    blocks, _ = chain()
    with pytest.raises(U.UtxoSetError):
        U.UtxoSet(tmp_path / "u.sqlite", KOT).build(parsed([blocks[0], blocks[2]]), tip=1)


def test_connect_and_disconnect_follow_the_chain(tmp_path):
    blocks, ids = chain()
    s = U.UtxoSet(tmp_path / "u.sqlite", KOT)
    s.build(parsed(blocks[:3]), tip=2)
    before = (s.balance(A), s.balance(B), s.balance(C), s.data_blocks())
    outputs = {(ids["spend"], 0): (p2pkh_script(B), 3 * COIN)}
    h3, bh3, b3 = parsed(blocks)[3]
    s.connect(h3, bh3, b3, lambda t, v: outputs[(t, v)])
    assert (s.balance(B), s.balance(C)) == (99_000_000, 190_000_000)
    assert s.height == 3 and s.best == bh3
    s.disconnect()                                                   # a reorg takes block 3 back off
    assert (s.balance(A), s.balance(B), s.balance(C), s.data_blocks()) == before
    assert s.height == 2 and s.best == blocks[2][1]
    with pytest.raises(U.UtxoSetError):                              # block 3 again, but not on top of 2
        s.connect(4, bh3, b3, lambda t, v: outputs[(t, v)])


def test_building_from_block_files_follows_the_chain_not_the_file_order(tmp_path):
    blocks, _ = chain()
    magic = U.MAGIC["kot"]
    orphan_hash, orphan = block(blocks[1][1], [tx([None], [(7 * COIN, p2pkh_script(C))])], time=1_700_000_999)
    records = [blocks[0], blocks[2], (None, orphan_hash, orphan), blocks[1], blocks[3]]   # out of order, a fork
    (tmp_path / "blk00000.dat").write_bytes(
        b"".join(magic + len(raw).to_bytes(4, "little") + raw for _h, _bh, raw in records[:3]))
    (tmp_path / "blk00001.dat").write_bytes(
        b"".join(magic + len(raw).to_bytes(4, "little") + raw for _h, _bh, raw in records[3:]) + magic + b"\xff\xff")
    got = list(U.file_blocks(tmp_path, KOT, magic, blocks[3][1], genesis=blocks[0][1]))
    assert [(h, bh) for h, bh, _b in got] == [(h, bh) for h, bh, _raw in blocks]
    s = U.UtxoSet(tmp_path / "u.sqlite", KOT)
    s.build(got, tip=3)
    assert s.balance(C) == 190_000_000                               # the orphan's 7 KOT for C is not counted
    with pytest.raises(U.UtxoSetError):
        list(U.file_blocks(tmp_path, KOT, magic, blocks[3][1], genesis="11" * 32))


def test_history_is_kept_for_the_addresses_asked_for(tmp_path):
    """utxos.activity's rows: received, spent, paid out, the first other address -- for the accounts only."""
    blocks, ids = chain()
    s = U.UtxoSet(tmp_path / "u.sqlite", KOT)
    s.build(parsed(blocks), tip=3, history_for={A, B})
    rows = {(r["txid"], r["address"]): r for r in s.history([A, B, C])}
    assert set(rows) == {(ids["cb1"], A), (ids["spend"], A), (ids["spend"], B), (ids["make"], B)}
    assert rows[(ids["cb1"], A)] == {"txid": ids["cb1"], "address": A, "height": 1, "received": 5 * COIN, "spent": 0,
                                     "paid_out": 0, "other": ""}
    spend = rows[(ids["spend"], A)]                     # 5 in, 1.99 back: 3 to B (and the data output's nothing)
    assert (spend["received"], spend["spent"], spend["paid_out"], spend["other"]) == (199_000_000, 5 * COIN, 3 * COIN, B)
    # B's 3 went on in block 3 (to C and 0.99 back), C is not asked for and keeps no rows.
    assert (rows[(ids["make"], B)]["spent"], rows[(ids["make"], B)]["received"]) == (3 * COIN, 99_000_000)
    assert s.history_for == {A, B}


def test_more_accounts_get_their_history_without_the_coins_being_built_again(tmp_path):
    blocks, ids = chain()
    s = U.UtxoSet(tmp_path / "u.sqlite", KOT)
    s.build(parsed(blocks), tip=3, history_for={A})
    s.build(parsed(blocks), tip=3, history_for={C}, coins=False)
    assert s.history_for == {A, C}
    assert {r["txid"] for r in s.history([C])} == {ids["make"], ids["again"]}
    assert s.balance(C) == 190_000_000                  # the coins were not touched


def test_a_staking_reward_is_a_coin_but_not_a_wallet_coin(tmp_path):
    """Kotia pays stakers to their public key (P2PK): the key's address holds it, the wallet cannot sign it."""
    blocks, _ = chain()
    pub = bytes.fromhex("02" + "11" * 32)
    stake = tx([None], [(0, b""), (20 * COIN, bytes([33]) + pub + b"\xac")])
    h4, r4 = block(blocks[3][1], [stake])
    s = U.UtxoSet(tmp_path / "u.sqlite", KOT)
    s.build(parsed(blocks + [(4, h4, r4)]), tip=4)
    key = b58check_encode(46, __import__("arcade.script", fromlist=["hash160"]).hash160(pub))
    assert s.balance(key) == 20 * COIN and s.unspent(key)[0]["kind"] == U.PUBKEY
    assert s.unspent(key, wallet_only=True) == []


def test_the_full_set_is_kotia_only():
    assert [n for n, p in NETWORKS.items() if p.full_utxo_set] == ["kot"]


def test_the_watcher_seeds_account_coins_and_history_into_the_wallet_index(tmp_path):
    """The set is what the wallet's index reads an account back from on Kotia: its wallet coins and its history,
    once per address, and only when the set is at or past what the index has read."""
    import contextlib
    import types

    from arcade import utxos
    from arcade.ledger import LedgerIndex
    from arcade.web.watcher import BlockWatcher

    blocks, ids = chain()
    s = U.UtxoSet(tmp_path / "kot-utxoset.sqlite", KOT)
    s.build(parsed(blocks), tip=3, history_for={A, B})
    index = LedgerIndex(tmp_path / "kot-ledger.sqlite", KOT, rpc_factory=lambda: None)
    with contextlib.closing(index.open()) as db:
        utxos.install(db)
        for address in (A, B):
            utxos.watch(db, address, 0, "account (derived)")
        db.conn.commit()
    settings, bumps = {}, []
    state = types.SimpleNamespace(token_index=lambda chain: index, setting=lambda k, d=None: settings.get(k, d),
                                  set_setting=settings.__setitem__, bump_generation=lambda: bumps.append(1))
    me = types.SimpleNamespace(state=state)
    kot = types.SimpleNamespace(network="kot", params=KOT)

    index.indexed_height = lambda: 9                                # the index is past the set: not yet
    BlockWatcher._utxoset_seed(me, kot, s)
    assert not bumps
    index.indexed_height = lambda: None                             # below the floor: nothing read yet
    BlockWatcher._utxoset_seed(me, kot, s)
    with contextlib.closing(index.open()) as db:
        assert utxos.balance(db, A) == 199_000_000 and utxos.balance(db, B) == 99_000_000
        rows = utxos.activity(db, [A])
        assert {r["txid"] for r in rows} == {ids["cb1"], ids["spend"]}
        assert next(r for r in rows if r["txid"] == ids["spend"])["net"] == 199_000_000 - 5 * COIN
    assert settings["utxoset_seeded:kot"] == sorted([A, B]) and bumps == [1]
    BlockWatcher._utxoset_seed(me, kot, s)                          # once per address
    assert bumps == [1]


class _Core:
    """A Kotia Core holding `chain()`'s blocks, as the watcher's pass asks it (getblockcount, getblockhash, raw
    blocks in batches)."""

    def __init__(self, blocks):
        self.blocks = blocks

    def call(self, method, *args):
        if method == "getblockcount":
            return len(self.blocks) - 1
        if method == "getblockhash":
            return self.blocks[args[0]][1]
        raise AssertionError(method)

    def batch(self, calls):
        by_hash = {bh: raw for _h, bh, raw in self.blocks}
        return [self.call(m, *p) if m != "getblock" else by_hash[p[0]].hex() for m, p in calls]


def test_the_watcher_builds_the_set_and_builds_it_again_when_asked(tmp_path, monkeypatch):
    import contextlib
    import types

    from arcade import utxos
    from arcade.ledger import LedgerIndex
    from arcade.web import watcher as W

    monkeypatch.setattr(W, "UTXOSET_REST", 0)
    blocks, _ids = chain()
    index = LedgerIndex(tmp_path / "kot-ledger.sqlite", KOT, rpc_factory=lambda: None)
    with contextlib.closing(index.open()) as db:
        utxos.install(db)
        utxos.watch(db, A, 0, "account (derived)")
        db.conn.commit()
    uset = U.UtxoSet(tmp_path / "kot-utxoset.sqlite", KOT)
    settings, progress, rebuild = {}, {}, set()
    state = types.SimpleNamespace(token_index=lambda c: index, setting=lambda k, d=None: settings.get(k, d),
                                  set_setting=settings.__setitem__, bump_generation=lambda: None,
                                  utxoset=lambda c: uset, utxoset_progress=progress, utxoset_rebuild=rebuild)
    kot = types.SimpleNamespace(network="kot", params=KOT, borrowed=False,
                                local_rpc=lambda: contextlib.nullcontext(_Core(blocks)))
    me = types.SimpleNamespace(state=state)
    me._utxoset_seed = lambda c, u: W.BlockWatcher._utxoset_seed(me, c, u)
    W.BlockWatcher._utxoset_pass(me, kot, set())
    assert progress["kot"]["phase"] == "ready" and uset.height == 3 and uset.balance(A) == 199_000_000
    assert settings["utxoset_seeded:kot"] == [A]
    uset.conn.execute("DELETE FROM coins")                          # something went wrong with it
    uset.conn.commit()
    rebuild.add("kot")                                               # Admin -> Chains -> Rebuild
    W.BlockWatcher._utxoset_pass(me, kot, set())
    assert not rebuild and uset.balance(A) == 199_000_000 and uset.height == 3
    assert settings["utxoset_seeded:kot"] == [A]                     # read back again


def test_admin_asks_for_a_rebuild_of_kotia_only(client, monkeypatch):
    import pathlib
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from test_admin import LOCAL, WRITE
    from arcade.web.state import ChainContext

    app, state = client
    kot = ChainContext(network="kot", role="ledger", label="Kotia", conf=pathlib.Path("/nonexistent/kotia.conf"))
    monkeypatch.setattr(state, "more", {"kot": kot}, raising=False)
    assert app.post("/admin/api/utxoset/rebuild", headers=LOCAL, json={"net": "kot"}).status_code in (401, 403)
    assert app.post("/admin/api/utxoset/rebuild", headers={**LOCAL, **WRITE}, json={"net": "doge"}).status_code == 400
    assert app.post("/admin/api/utxoset/rebuild", headers={**LOCAL, **WRITE}, json={"net": "kot"}).json()["ok"]
    assert "kot" in state.utxoset_rebuild and state.utxoset_progress["kot"]["phase"] == "queued"
    state.utxoset_progress["kot"] = {"phase": "building", "height": 5, "tip": 10}
    assert app.post("/admin/api/utxoset/rebuild", headers={**LOCAL, **WRITE}, json={"net": "kot"}).status_code == 409
    rows = {r["net"]: r for r in app.get("/admin/api/state", headers=LOCAL).json()["chains"]["rows"]}
    assert rows["kot"]["utxoset"]["phase"] == "building"

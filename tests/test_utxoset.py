"""The whole unspent set of a small chain, by address, and its data-carrying blocks (arcade/utxoset.py, tbuuol
2026-10-25). Blocks here are made in Kotia's own shape -- nTime in every transaction, a block signature after them --
and read through rawchain, as the real ones are."""
import hashlib

import pytest

from arcade import rawchain, utxoset as U
from arcade.config import NETWORKS
from arcade.script import b58check_encode
from arcade.txbuild import op_return_script, p2pkh_script, varint

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
    assert s.unspent(B) == [{"txid": ids["make"], "vout": 1, "value": 99_000_000, "height": 3}]   # its 3 went on
    assert s.unspent(C) == [{"txid": ids["again"], "vout": 0, "value": 190_000_000, "height": 3}]
    assert said["coins"] == 3 and said["addresses"] == 3 and said["height"] == 3 and said["best"] == blocks[3][1]
    # Only the block whose transaction carries an OP_RETURN, as an array of its txids.
    assert s.data_blocks() == [(2, [ids["spend"]])]
    # 48 bytes a coin, nothing else per coin.
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

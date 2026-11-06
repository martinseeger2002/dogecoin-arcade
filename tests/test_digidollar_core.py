"""arcade/digidollar.py, DigiDollar in the arcade's own wallet (2026-11-05): its addresses and its send are the
pilot module's (examples/packages/digidollar, itself checked against a real DigiByte Core in test_digidollar_regtest.py),
and the index follows a watched account's DigiDollar coins in and out of blocks, journalled."""
import importlib
import sqlite3
import sys
from pathlib import Path

import pytest

from arcade import digidollar as dd
from arcade import taproot

PILOT = Path(__file__).resolve().parent.parent / "examples" / "packages" / "digidollar"


def _pilot():
    sys.path.insert(0, str(PILOT))
    try:
        for name in ("lib.taproot", "lib", "module"):
            sys.modules.pop(name, None)
        return importlib.import_module("lib.taproot"), importlib.import_module("module")
    finally:
        sys.path.remove(str(PILOT))


def _key(n: int) -> bytes:
    x, y = taproot.mul(n)
    return bytes([2 + (y & 1)]) + x.to_bytes(32, "big")


ME, THEM = _key(7), _key(11)
P2PKH = bytes.fromhex("76a914" + "11" * 20 + "88ac")


def test_addresses_are_the_pilots_and_digibyte_cores():
    _, pilot = _pilot()
    for net in dd.NETS:
        assert dd.address(net, ME) == pilot.dd_address(net, ME[1:])
        assert dd.key_of(net, dd.address(net, THEM)) == dd.output_key(THEM)
    assert dd.address("dgb", ME).startswith("DD") and dd.address("dgbtest", ME).startswith("TD")
    with pytest.raises(ValueError, match="other chain"):
        dd.key_of("dgb", dd.address("dgbtest", ME))
    with pytest.raises(ValueError, match="not a DigiDollar address"):
        dd.key_of("dgb", "DGBnotanaddress")


def test_a_send_is_the_pilots_and_reads_back_as_its_dollars():
    _, pilot = _pilot()
    held = [{"txid": "dd" * 32, "vout": 1, "cents": 6000}]
    dgb = [{"txid": "aa" * 32, "vout": 0, "value": 150_000_000}]
    to = dd.address("dgbtest", THEM)
    ours = dd.prepare_send("dgbtest", ME, held, dgb, P2PKH, to, 4000)
    theirs = pilot.send(ME[1:], held, dgb, P2PKH, dd.key_of("dgbtest", to), 4000, "dgbtest", to)
    assert ours["outputs"] == theirs["outputs"] and ours["inputs"] == theirs["inputs"]
    assert ours["version"] == dd.SEND_VERSION
    tx = {"txid": "ee" * 32, "vout": [{"n": n, "value": v / 1e8, "scriptPubKey": {"hex": s}}
                                      for n, (v, s) in enumerate(ours["outputs"])]}
    assert dd.dollars_out(tx) == [(0, 4000, dd.output_key(THEM)), (1, 2000, dd.output_key(ME))]
    with pytest.raises(ValueError, match="you hold"):
        dd.prepare_send("dgbtest", ME, held, dgb, P2PKH, to, 7000)
    with pytest.raises(ValueError, match="under DigiDollar's \\$1 least"):
        dd.prepare_send("dgbtest", ME, held, dgb, P2PKH, to, 5950)
    with pytest.raises(ValueError, match="network fee"):
        dd.prepare_send("dgbtest", ME, held, [], P2PKH, to, 4000)


class _State:
    """The journalled writes on_block makes, straight to a table."""
    def __init__(self):
        self.db = type("Db", (), {})()
        self.db.conn = sqlite3.connect(":memory:")
        self.db.conn.executescript(dd.SCHEMA)

    def insert(self, table, row):
        self.db.conn.execute(f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                             tuple(row.values()))

    def delete(self, table, key):
        self.db.conn.execute(f"DELETE FROM {table} WHERE {' AND '.join(k + '=?' for k in key)}", tuple(key.values()))


def test_the_index_follows_a_watched_accounts_digidollars_in_and_out():
    st = _State()
    mine, theirs = dd.address("dgbtest", ME), dd.address("dgbtest", THEM)
    assert dd.watch(st.db, mine, 100) and not dd.watch(st.db, mine, 200)
    assert dd.watched_row(st.db, mine) == {"since": 100, "seeded": False}
    # Somebody pays this account $60 (a send of theirs: $60 to it, $5 back to them).
    record = bytes([dd.OP_RETURN]) + dd.push(b"DD") + dd.push(dd.num(2)) + dd.push(dd.num(6000)) + dd.push(dd.num(500))
    paid = {"txid": "a1" * 32, "vin": [{"txid": "f0" * 32, "vout": 0}], "vout": [
        {"n": 0, "value": 0, "scriptPubKey": {"hex": taproot.p2tr(dd.output_key(ME)).hex()}},
        {"n": 1, "value": 0, "scriptPubKey": {"hex": taproot.p2tr(dd.output_key(THEM)).hex()}},
        {"n": 2, "value": 0, "scriptPubKey": {"hex": record.hex()}}]}
    moved = dd.on_block(st, 101, {"tx": [paid]}, "dgbtest", dd.watching(st.db))
    assert moved == {"added": 1, "spent": 0}
    assert dd.coins(st.db, mine) == [{"txid": "a1" * 32, "vout": 0, "cents": 6000, "height": 101}]
    assert dd.coins(st.db, theirs) == [], "only watched addresses"
    # It sends them on: the coin is gone.
    spent = {"txid": "b2" * 32, "vin": [{"txid": "a1" * 32, "vout": 0}], "vout": []}
    assert dd.on_block(st, 102, {"tx": [spent]}, "dgbtest", dd.watching(st.db)) == {"added": 0, "spent": 1}
    assert dd.coins(st.db, mine) == []
    # A zero-value Taproot output with no "DD" record is nobody's dollars.
    plain = {"txid": "c3" * 32, "vin": [], "vout": paid["vout"][:2]}
    assert dd.on_block(st, 103, {"tx": [plain]}, "dgbtest", dd.watching(st.db))["added"] == 0
    # Another chain's blocks are not DigiByte's.
    assert dd.on_block(st, 104, {"tx": [paid]}, "doge", dd.watching(st.db))["added"] == 0

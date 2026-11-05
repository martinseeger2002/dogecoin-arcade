"""The Exchange's trades without reading every payload (2026-11-05, the token tab on .25: the trades read was a LIKE over
every type-200 payload -- 363 MB on Pepecoin testnet, minutes on a laptop disk -- to find 33 swaps, and again after
every block). Now the swaps found are kept (trade_seen) and each read looks only at rows it has not read yet and the
last few blocks; a reorg's new rows are found, and a take whose price could not be read is tried again."""
import contextlib

from arcade import config, inscriptions as I, ledger


def _swap_hex(amount: int) -> str:
    swap = I.Swap(give=I.Leg(I.LEG_TOKEN, property_id=7, amount=amount), take=I.Leg(I.LEG_COINS, amount=amount * 10))
    return "000000c8" + swap.encode().hex()


def _index(tmp_path):
    return ledger.LedgerIndex(tmp_path / "l.sqlite", config.REGTEST, lambda: contextlib.nullcontext(None))


def _block(index, height):
    with index.open() as db:
        db.conn.execute("INSERT OR REPLACE INTO block(height, hash, prev_hash, time, tx_count, processed_at) "
                        "VALUES (?,?,?,?,?,?)", (height, f"{height:064x}", f"{height - 1:064x}", 1_700_000_000 + height, 1, 0))
        db.conn.commit()


def _tx(index, txid, height, payload, position=0):
    with index.open() as db:
        db.conn.execute("INSERT OR REPLACE INTO arcade_tx(txid, block_height, position, encoding_class, message_type, "
                        "sender, payload_hex, valid) VALUES (?,?,?,?,?,?,?,1)",
                        (txid, height, position, "C", int(payload[:8], 16), "Dseller", payload))
        db.conn.commit()


def test_trades_are_found_once_then_only_new_rows_are_read(tmp_path):
    index = _index(tmp_path)
    for h in range(1, 4):
        _block(index, h)
    _tx(index, "aa" * 32, 1, _swap_hex(5))
    _tx(index, "bb" * 32, 2, "000000c8" + "00" * 40)                 # an inscription piece, not a swap
    assert [t["txid"] for t in index.trades()] == ["aa" * 32]
    with index.open() as db:
        mark = db.conn.execute("SELECT v FROM trade_scan WHERE k='rowid'").fetchone()[0]
        assert [r[0] for r in db.conn.execute("SELECT txid FROM trade_seen")] == ["aa" * 32]
    assert mark >= 2
    _tx(index, "cc" * 32, 3, _swap_hex(9))                           # a new block's swap
    got = index.trades()
    assert [t["txid"] for t in got] == ["cc" * 32, "aa" * 32] and got[0]["give"].amount == 9


def test_a_reorg_s_rows_are_found_even_with_old_row_ids(tmp_path):
    index = _index(tmp_path)
    for h in range(1, 4):
        _block(index, h)
    _tx(index, "aa" * 32, 1, _swap_hex(5))
    _tx(index, "dd" * 32, 3, "000000c8" + "00" * 40)
    index.trades()
    with index.open() as db:                                          # block 3 replaced: its rows go, a swap comes
        db.conn.execute("DELETE FROM arcade_tx WHERE block_height = 3")
        db.conn.execute("UPDATE block SET hash = ? WHERE height = 3", ("33" * 32,))
        db.conn.commit()
    _tx(index, "ee" * 32, 3, _swap_hex(4))                             # may take the deleted row's id
    assert [t["txid"] for t in index.trades()] == ["ee" * 32, "aa" * 32]
    with index.open() as db:                                          # and a reorged-away swap drops out
        db.conn.execute("DELETE FROM arcade_tx WHERE txid = ?", ("ee" * 32,))
        db.conn.commit()
    _block(index, 4)
    assert [t["txid"] for t in index.trades()] == ["aa" * 32]


def test_a_big_ledger_scans_first_in_the_background_and_answers_meanwhile(tmp_path, monkeypatch):
    index = _index(tmp_path)
    _block(index, 1)
    _tx(index, "aa" * 32, 1, _swap_hex(5))
    monkeypatch.setattr(ledger.LedgerIndex, "TRADE_FIRST_SCAN_BACKGROUND_BYTES", 0)
    first = index.trades()                                            # answered at once, not kept
    index._trade_first_thread.join(10)
    assert first in ([], [t for t in first if t["txid"] == "aa" * 32])
    assert [t["txid"] for t in index.trades()] == ["aa" * 32], "once the scan is done, the trades are there"


def test_a_take_not_written_is_tried_again(tmp_path, monkeypatch):
    index = _index(tmp_path)
    _block(index, 1)
    _tx(index, "ff" * 32, 1, "0000001d" + "00" * 20)
    tries = []
    monkeypatch.setattr(ledger.LedgerIndex, "_backfill_takes", lambda self, since_height=0, txids=None: tries.append(txids))
    index._trade_scan()
    index._trade_scan()
    assert tries == [["ff" * 32], ["ff" * 32]], "the node could not be asked: asked again on the next read"

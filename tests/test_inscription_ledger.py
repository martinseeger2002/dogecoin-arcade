"""Inscriptions in the ledger: created by chunks, moved by their owner, and
unwound by a reorg exactly like every other thing this engine holds."""

import pytest

from arcade import inscriptions as I
from arcade import payload as P
from arcade.config import NETWORKS
from arcade.db import Database
from arcade.state import Engine, StateDB, install_schema
from arcade.tx import ArcadeTransaction, EncodingClass


def hexid(n: int) -> str:
    return f"{n:064x}"


@pytest.fixture
def engine(tmp_path):
    db = Database(tmp_path / "ledger.sqlite")
    install_schema(db)
    state = StateDB(db)
    return Engine(state, NETWORKS["regtest"]), state, db


@pytest.fixture
def keeping(tmp_path):
    """An engine told to keep the bytes, for the guarantees that are about
    the blob column rather than about the default policy (D-113)."""
    db = Database(tmp_path / "kept.sqlite")
    install_schema(db)
    state = StateDB(db)
    return Engine(state, NETWORKS["regtest"],
                  keep_content=lambda address: True), state, db


def tx(n, payload, sender="nCreator", reference=None, height=None, position=0):
    return ArcadeTransaction(
        txid=hexid(n), block_height=height if height is not None else 100 + n,
        position=position, encoding_class=EncodingClass.B, sender=sender,
        reference=reference, payload=P.AnyData(data=payload).encode(), fee=0)


def feed(engine, state, transactions):
    for transaction in transactions:
        with state.block_context(transaction.block_height,
                                 f"hash{transaction.block_height}", "prev", 0, 1, 0):
            engine.process(transaction)


def test_chunks_become_an_inscription(engine):
    eng, state, db = engine
    data = bytes(range(256)) * 70
    bodies = I.plan(data, "image/png", '{"name":"Sunrise"}')
    feed(eng, state, [tx(n, body) for n, body in enumerate(bodies)])

    row = db.conn.execute("SELECT * FROM inscription").fetchone()
    assert row["number"] == 0
    assert row["creator"] == row["owner"] == "nCreator"
    assert row["content_type"] == "image/png"
    assert row["content_len"] == len(data)
    assert row["json"] == '{"name":"Sunrise"}'
    assert row["chunks"] == len(bodies)
    assert row["txid"] == hexid(0), "named by the piece that carried the manifest"
    assert db.conn.execute("SELECT COUNT(*) FROM inscription_chunk").fetchone()[0] == 0

    # The bytes are NOT kept: they are on the chain in full, and what is kept
    # is which transactions carry them, so the file can be read back and
    # proved against the sha256 above rather than trusted from a copy (D-113).
    assert row["content"] is None
    assert row["sha256"] and row["content_len"] == len(data)
    pieces = db.conn.execute(
        "SELECT countdown, txid FROM inscription_piece WHERE inscription=? "
        "ORDER BY countdown DESC", (row["txid"],)).fetchall()
    assert len(pieces) == len(bodies), "one row per transaction that carried it"
    assert [p["txid"] for p in pieces] == [hexid(n) for n in range(len(bodies))]


def test_it_does_not_exist_until_every_piece_is_here(engine):
    eng, state, db = engine
    bodies = I.plan(b"x" * 20_000, "text/plain")
    feed(eng, state, [tx(n, body) for n, body in enumerate(bodies[:-1])])

    assert db.conn.execute("SELECT COUNT(*) FROM inscription").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM inscription_chunk").fetchone()[0] \
        == len(bodies) - 1


def test_pieces_may_confirm_in_any_order(engine):
    """They are independent transactions; the miner takes them as it likes."""
    eng, state, db = engine
    data = b"\xff\xd8" * 9000
    bodies = I.plan(data, "image/jpeg")
    ordered = list(enumerate(bodies))
    feed(eng, state, [tx(n, body) for n, body in reversed(ordered)])

    row = db.conn.execute("SELECT * FROM inscription").fetchone()
    assert row["txid"] == hexid(0), "still named by the manifest piece"
    assert db.conn.execute(
        "SELECT COUNT(*) FROM inscription_piece WHERE inscription=?",
        (row["txid"],)).fetchone()[0] == len(bodies), \
        "and every piece is remembered, whatever order they arrived in"


def test_a_stranger_cannot_poison_a_set_in_progress(engine):
    """The id is in the clear on the chain, so anyone can publish a piece
    claiming it. Grouping by sender is what stops one injected piece making a
    real inscription permanently incomplete."""
    eng, state, db = engine
    data = b"z" * 20_000
    bodies = I.plan(data, "text/plain", inscription_id=b"\x22" * 8)

    feed(eng, state, [tx(0, bodies[0])])
    # Somebody else publishes rubbish under the same id and countdown.
    forged = I.Chunk(inscription_id=b"\x22" * 8, countdown=0, body=b"rubbish").encode()
    feed(eng, state, [tx(50, forged, sender="nStranger")])
    feed(eng, state, [tx(n, body) for n, body in enumerate(bodies[1:], start=1)])

    rows = db.conn.execute("SELECT creator, content_len FROM inscription").fetchall()
    assert [(r["creator"], r["content_len"]) for r in rows] == [("nCreator", 20_000)]


def test_the_owner_can_send_it_and_nobody_else_can(engine):
    eng, state, db = engine
    bodies = I.plan(b"a picture", "image/png")
    feed(eng, state, [tx(0, bodies[0])])
    txid = bytes.fromhex(hexid(0))

    feed(eng, state, [tx(1, I.Transfer(txid=txid).encode(), reference="nNewOwner")])
    assert db.conn.execute("SELECT owner FROM inscription").fetchone()[0] == "nNewOwner"

    feed(eng, state, [tx(2, I.Transfer(txid=txid).encode(),
                         sender="nCreator", reference="nThief")])
    assert db.conn.execute("SELECT owner FROM inscription").fetchone()[0] == "nNewOwner"
    assert "only the owner" in db.conn.execute(
        "SELECT invalid_reason FROM arcade_tx WHERE txid=?", (hexid(2),)).fetchone()[0]


def test_sending_one_that_does_not_exist_is_refused_not_ignored(engine):
    eng, state, db = engine
    feed(eng, state, [tx(0, I.Transfer(txid=bytes(32)).encode(), reference="nThem")])
    assert db.conn.execute(
        "SELECT invalid_reason FROM arcade_tx WHERE txid=?", (hexid(0),)
    ).fetchone()[0] == "no such inscription"


def test_a_transfer_needs_somewhere_to_go(engine):
    eng, state, db = engine
    feed(eng, state, [tx(0, I.plan(b"x", "text/plain")[0])])
    feed(eng, state, [tx(1, I.Transfer(txid=bytes.fromhex(hexid(0))).encode())])
    assert "reference address" in db.conn.execute(
        "SELECT invalid_reason FROM arcade_tx WHERE txid=?", (hexid(1),)).fetchone()[0]


def test_numbers_run_in_chain_order(engine):
    """Two nodes replaying the same chain agree without having to talk: the
    order is the chain's, not anybody's opinion."""
    eng, state, db = engine
    for n in range(3):
        feed(eng, state, [tx(n, I.plan(f"file {n}".encode(), "text/plain")[0])])
    rows = db.conn.execute("SELECT number, txid FROM inscription ORDER BY number").fetchall()
    assert [r["number"] for r in rows] == [0, 1, 2]
    assert [r["txid"] for r in rows] == [hexid(0), hexid(1), hexid(2)]


def test_a_reorg_takes_it_all_back(engine):
    eng, state, db = engine
    data = bytes(range(256)) * 70
    bodies = I.plan(data, "image/png")
    feed(eng, state, [tx(n, body) for n, body in enumerate(bodies)])
    txid = bytes.fromhex(hexid(0))
    feed(eng, state, [tx(9, I.Transfer(txid=txid).encode(),
                         reference="nNewOwner", height=200)])

    state.rollback_block(200)
    assert db.conn.execute("SELECT owner FROM inscription").fetchone()[0] == "nCreator"

    state.rollback_block(100 + len(bodies) - 1)
    assert db.conn.execute("SELECT COUNT(*) FROM inscription").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM inscription_chunk").fetchone()[0] \
        == len(bodies) - 1, "the earlier pieces come back, waiting for the last"


def test_content_bytes_survive_the_undo_journal(keeping):
    """The journal stores a row as JSON and JSON has no bytes. A blob column is
    an ordinary thing for a table to have; an inscription's content is one on
    a node that has been told to keep them."""
    eng, state, db = keeping
    data = b"\x00\xff binary \x00" * 200
    feed(eng, state, [tx(0, I.plan(data, "application/octet-stream")[0])])
    state.rollback_block(100)
    assert db.conn.execute("SELECT COUNT(*) FROM inscription").fetchone()[0] == 0

    feed(eng, state, [tx(1, I.plan(data, "application/octet-stream")[0])])
    assert bytes(db.conn.execute(
        "SELECT content FROM inscription").fetchone()[0]) == data


def test_a_node_can_describe_what_it_does_not_keep(tmp_path):
    """Keeping every megabyte a stranger ever wrote is a choice, not a
    requirement. The hash and the length are always kept, so a body that was
    not stored can be fetched back and proved to be the right one."""
    db = Database(tmp_path / "ledger.sqlite")
    install_schema(db)
    state = StateDB(db)
    eng = Engine(state, NETWORKS["regtest"],
                 keep_content=lambda address: address == "nMine")

    feed(eng, state, [tx(0, I.plan(b"mine", "text/plain")[0], sender="nMine")])
    feed(eng, state, [tx(1, I.plan(b"theirs", "text/plain")[0], sender="nTheirs")])

    rows = {r["creator"]: r for r in db.conn.execute("SELECT * FROM inscription")}
    assert bytes(rows["nMine"]["content"]) == b"mine"
    assert rows["nTheirs"]["content"] is None
    for row in rows.values():
        assert row["sha256"] and row["content_len"], "always described"


def test_a_malformed_payload_cannot_stop_the_index(engine):
    """A stranger's broken bytes must never halt anybody's node."""
    eng, state, db = engine
    feed(eng, state, [tx(0, I.MAGIC + b"\x01\x01" + b"short")])
    assert "malformed inscription" in db.conn.execute(
        "SELECT invalid_reason FROM arcade_tx WHERE txid=?", (hexid(0),)).fetchone()[0]


def test_anydata_that_is_not_an_inscription_is_still_a_no_op(engine):
    """Every messenger transaction is one of these."""
    eng, state, db = engine
    feed(eng, state, [tx(0, b"an ordinary payload")])
    row = db.conn.execute("SELECT valid FROM arcade_tx WHERE txid=?",
                          (hexid(0),)).fetchone()
    assert row["valid"] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM inscription").fetchone()[0] == 0


def test_the_same_piece_twice_is_refused(engine):
    eng, state, db = engine
    bodies = I.plan(b"y" * 20_000, "text/plain")
    feed(eng, state, [tx(0, bodies[0]), tx(1, bodies[0])])
    assert "already on chain" in db.conn.execute(
        "SELECT invalid_reason FROM arcade_tx WHERE txid=?", (hexid(1),)).fetchone()[0]


def test_where_a_piece_has_been(engine):
    """The inscription row holds the current owner and is overwritten on every
    transfer, so nothing remembered where a piece had been. "When did this
    leave the wallet that made it" -- the question people mean by a mint date
    when they ask about provenance -- had no answer on any node (D-071)."""
    eng, state, db = engine
    feed(eng, state, [tx(0, I.plan(b"a picture", "image/png")[0])])
    item = hexid(0)
    txid = bytes.fromhex(item)

    def moves():
        return [dict(r) for r in db.conn.execute(
            "SELECT * FROM inscription_move WHERE inscription=? "
            "ORDER BY block_height, position", (item,))]

    assert moves() == [], "made, never moved"

    feed(eng, state, [tx(1, I.Transfer(txid=txid).encode(), reference="nSecond")])
    feed(eng, state, [tx(2, I.Transfer(txid=txid).encode(), sender="nSecond",
                         reference="nThird")])

    where = moves()
    assert [(m["from_address"], m["to_address"]) for m in where] == [
        ("nCreator", "nSecond"), ("nSecond", "nThird")], "oldest first"
    assert all(m["how"] == "transfer" for m in where)
    assert where[0]["block_height"] < where[1]["block_height"]
    assert db.conn.execute("SELECT owner FROM inscription").fetchone()[0] == "nThird"


def test_a_refused_transfer_leaves_no_trace(engine):
    """Provenance must not record hands it never changed."""
    eng, state, db = engine
    feed(eng, state, [tx(0, I.plan(b"x", "text/plain")[0])])
    txid = bytes.fromhex(hexid(0))
    feed(eng, state, [tx(1, I.Transfer(txid=txid).encode(), sender="nThief",
                         reference="nThief")])
    assert db.conn.execute("SELECT COUNT(*) FROM inscription_move").fetchone()[0] == 0


def test_a_piece_that_comes_back_still_remembers_going(engine):
    """History, not current state: "did this ever leave" must not change
    because it came home."""
    eng, state, db = engine
    feed(eng, state, [tx(0, I.plan(b"x", "text/plain")[0])])
    txid = bytes.fromhex(hexid(0))
    feed(eng, state, [tx(1, I.Transfer(txid=txid).encode(), reference="nSecond")])
    feed(eng, state, [tx(2, I.Transfer(txid=txid).encode(), sender="nSecond",
                         reference="nCreator")])
    assert db.conn.execute("SELECT owner FROM inscription").fetchone()[0] == "nCreator"
    assert db.conn.execute("SELECT COUNT(*) FROM inscription_move").fetchone()[0] == 2


def test_history_is_rebuilt_from_what_is_already_indexed(engine, tmp_path):
    """Nothing is lost when a table arrives late: every transfer is on the
    chain as its own message and this node has already read and judged each
    one. So the history that was never written down is replayed from
    `arcade_tx`, with no rescan of blocks and nothing new to trust (D-071)."""
    from arcade.ledger import LedgerIndex
    from arcade.config import NETWORKS

    eng, state, db = engine
    feed(eng, state, [tx(0, I.plan(b"a picture", "image/png")[0])])
    item = hexid(0)
    txid = bytes.fromhex(item)
    feed(eng, state, [tx(1, I.Transfer(txid=txid).encode(), reference="nSecond")])
    feed(eng, state, [tx(2, I.Transfer(txid=txid).encode(), sender="nSecond",
                         reference="nThird")])

    # Lose the history, keep the chain: exactly the state an installation is
    # in the moment the table is added.
    db.conn.execute("DELETE FROM inscription_move")
    db.conn.commit()

    index = LedgerIndex(db.path, NETWORKS["regtest"], lambda: None)
    assert index.backfill_moves() == 2
    where = index.moves(item)
    assert [(m["from_address"], m["to_address"]) for m in where] == [
        ("nCreator", "nSecond"), ("nSecond", "nThird")], \
        "the owner at each step, which no single row records"
    assert index.left_the_creator(item)["to_address"] == "nSecond"

    # Once, not every pass.
    assert index.backfill_moves() == 0


def test_a_backfill_that_cannot_resolve_a_swap_says_so(engine, caplog):
    """It did not, and that was the bug rather than the skip.

    The first live run had a broken node factory. Every swap raised, a bare
    `except` swallowed it, nine moves were skipped and the run reported
    success: four written, no sign that nine were missing. For provenance a
    silent gap is not a missing answer, it is a wrong one -- a piece that was
    bought reads as a piece that never moved.
    """
    import logging
    from arcade.ledger import LedgerIndex
    from arcade.config import NETWORKS

    eng, state, db = engine
    feed(eng, state, [tx(0, I.plan(b"a picture", "image/png")[0])])
    item = hexid(0)
    # A swap the ledger has already accepted. Written straight into arcade_tx
    # because that is the only thing the backfill reads, and because the
    # point here is what happens when the NODE cannot be asked about it.
    from arcade import payload as P
    swap = I.Swap(give=I.Leg(I.LEG_INSCRIPTION, txid=bytes.fromhex(item)),
                  take=I.Leg(I.LEG_COINS, amount=10 ** 7))
    db.conn.execute(
        "INSERT INTO arcade_tx(txid, block_height, position, encoding_class, "
        "message_type, message_version, sender, reference, payload_hex, valid) "
        "VALUES(?,?,?,?,?,?,?,?,?,1)",
        (hexid(900), 300, 0, "C", 200, 0, "nCreator", "nBuyer",
         P.AnyData(data=swap.encode()).encode().hex()))
    db.conn.execute("DELETE FROM inscription_move")
    db.conn.commit()

    def no_node():
        raise OSError("connection refused")

    index = LedgerIndex(db.path, NETWORKS["regtest"], no_node)
    with caplog.at_level(logging.WARNING):
        index.backfill_moves()
    assert any("could not resolve" in r.message for r in caplog.records), \
        "a gap has to be said out loud"


def test_a_file_is_read_back_off_the_chain_and_proved(tmp_path, monkeypatch):
    """The bytes are not kept here, so serving one means fetching the
    transactions that carry it, reassembling, and checking the result against
    the manifest's own sha256 -- which is a stronger guarantee than a stored
    copy ever was: a stored blob is trusted, a reassembled one is proved
    (D-113)."""
    from arcade.config import NETWORKS
    from arcade.ledger import LedgerIndex

    db = Database(tmp_path / "ledger.sqlite")
    install_schema(db)
    state = StateDB(db)
    eng = Engine(state, NETWORKS["regtest"])
    data = bytes(range(256)) * 90
    bodies = I.plan(data, "image/png", '{"name":"Sunrise"}')
    sent = [tx(n, body) for n, body in enumerate(bodies)]
    feed(eng, state, sent)
    db.close()

    # A node that will hand back the transactions, as a real one does.
    raw = {t.txid: t for t in sent}

    class Node:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def call(self, method, *args):
            if method == "getrawtransaction":
                found = raw[args[0]]
                return {"txid": found.txid, "vin": [],
                        "vout": [{"n": 0, "value": 0, "scriptPubKey": {}}]}
            raise AssertionError(method)

    index = LedgerIndex(tmp_path / "ledger.sqlite", NETWORKS["regtest"],
                        rpc_factory=Node)

    # The fetch is stubbed at the payload boundary: what this proves is the
    # reassembly, the sha256 check and the cache, not bitcoin's serialisation.
    def payloads(row, pieces):
        assembly = I.Assembly(inscription_id=None)
        for piece in pieces:
            chunk = I.parse(P.decode(raw[piece["txid"]].payload).data)
            assembly.inscription_id = chunk.inscription_id
            assembly.pieces[chunk.countdown] = chunk.body
        manifest, content = assembly.join()
        from hashlib import sha256
        assert sha256(content).hexdigest() == row["sha256"], "proved, not trusted"
        return content

    monkeypatch.setattr(type(index), "_from_chain",
                        lambda self, row, pieces: payloads(row, pieces))
    found = index.inscription_content(hexid(0))
    assert found is not None
    assert found == ("image/png", data)

    # Asked twice, fetched once: a wall of a hundred tiles must not be a
    # hundred trips to the node for the same picture.
    monkeypatch.setattr(type(index), "_from_chain",
                        lambda self, row, pieces: (_ for _ in ()).throw(
                            AssertionError("should have been cached")))
    assert index.inscription_content(hexid(0)) == ("image/png", data)


def test_what_cannot_be_produced_is_not_pretended(tmp_path):
    """None means "this node cannot give you the file", never "there is no
    such inscription" -- the row still says how long it is and what it hashes
    to."""
    from arcade.config import NETWORKS
    from arcade.ledger import LedgerIndex

    db = Database(tmp_path / "ledger.sqlite")
    install_schema(db)
    state = StateDB(db)
    eng = Engine(state, NETWORKS["regtest"])
    feed(eng, state, [tx(0, I.plan(b"art" * 10, "text/plain")[0])])
    db.close()

    def no_node():
        raise OSError("the node is not answering")

    index = LedgerIndex(tmp_path / "ledger.sqlite", NETWORKS["regtest"],
                        rpc_factory=no_node)
    assert index.inscription_content(hexid(0)) is None
    row = index.inscription(hexid(0))
    assert row["content_len"] == 30 and row["sha256"], "it still knows what it is"
    assert row["held"], "and that it could produce it, from a node that answers"

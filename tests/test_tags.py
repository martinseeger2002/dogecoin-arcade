"""@tags: one name to an address, one address to a name, decided by the chain."""

import pytest

from arcade import payload as P
from arcade import tags as T
from arcade.config import NETWORKS
from arcade.db import Database
from arcade.inscriptions import InscriptionError
from arcade.state import Engine, StateDB, install_schema
from arcade.tx import ArcadeTransaction, EncodingClass


@pytest.fixture
def engine(tmp_path):
    db = Database(tmp_path / "ledger.sqlite")
    install_schema(db)
    state = StateDB(db)
    return Engine(state, NETWORKS["regtest"]), state, db


def claim(n, tag, sender="nAlice", kind=T.KIND_CLAIM, reference=None, height=None):
    return ArcadeTransaction(
        txid=f"{n:064x}", block_height=height if height is not None else 100 + n,
        position=0, encoding_class=EncodingClass.C, sender=sender,
        reference=reference, payload=P.AnyData(data=T.encode(tag, kind)).encode(),
        fee=0)


def feed(eng, state, transactions):
    for transaction in transactions:
        with state.block_context(transaction.block_height,
                                 f"h{transaction.block_height}", "p", 0, 1, 0):
            eng.process(transaction)


def reason(db, n):
    return db.conn.execute("SELECT invalid_reason FROM arcade_tx WHERE txid=?",
                           (f"{n:064x}",)).fetchone()[0]


# --- what a tag may be ---------------------------------------------------------


def test_a_tag_is_lower_case_and_has_no_at_sign():
    assert T.validate("@the operator") == "robin"
    assert T.validate("  @BIG_chief_99 ") == "big_chief_99"
    assert T.normalise("") == ""


@pytest.mark.parametrize("bad,why", [
    ("@a", "at least"),
    ("@" + "x" * 25, "at most"),
    ("@mar-tin", "a-z, 0-9 and _"),
    ("@robın", "a-z, 0-9 and _"),      # dotless i: looks like the real one
    ("@mar.tin", "a-z, 0-9 and _"),
    ("@admin", "reserved"),
])
def test_names_nobody_can_have(bad, why):
    with pytest.raises(T.TagError, match=why):
        T.validate(bad)


def test_a_tag_fits_the_cheapest_carriage():
    """Claiming a name should cost one small transaction, not a Class B one."""
    from arcade.encoding import max_class_c_payload

    payload = T.encode("x" * T.MAX_LENGTH)
    assert len(payload) + 4 <= max_class_c_payload()


def test_a_payload_naming_something_unclaimable_is_refused():
    with pytest.raises(InscriptionError):
        T.parse(T.MAGIC + bytes([T.VERSION, T.KIND_CLAIM, 3]) + b"A!x")


# --- who holds what ------------------------------------------------------------


def test_claiming_a_name_and_keeping_it(engine):
    eng, state, db = engine
    feed(eng, state, [claim(0, "@robin")])
    row = db.conn.execute("SELECT * FROM tag").fetchone()
    assert row["tag"] == "robin" and row["address"] == "nAlice"


def test_first_claim_wins(engine):
    eng, state, db = engine
    feed(eng, state, [claim(0, "robin"), claim(1, "robin", sender="nBob")])
    assert db.conn.execute("SELECT address FROM tag").fetchone()[0] == "nAlice"
    assert reason(db, 1) == "that tag is taken"


def test_claiming_your_own_again_changes_nothing(engine):
    eng, state, db = engine
    feed(eng, state, [claim(0, "robin"), claim(1, "robin")])
    assert reason(db, 1) == "that tag is already yours"


def test_changing_your_tag_frees_the_old_one(engine):
    """Holding names you no longer use is how a namespace fills with nothing."""
    eng, state, db = engine
    feed(eng, state, [claim(0, "robin"), claim(1, "bigchief")])
    rows = db.conn.execute("SELECT tag, address FROM tag").fetchall()
    assert [(r["tag"], r["address"]) for r in rows] == [("bigchief", "nAlice")]

    feed(eng, state, [claim(2, "robin", sender="nBob")])
    assert db.conn.execute(
        "SELECT address FROM tag WHERE tag='robin'").fetchone()[0] == "nBob"


def test_one_address_holds_one_name(engine):
    eng, state, db = engine
    feed(eng, state, [claim(0, "robin"), claim(1, "bigchief")])
    assert db.conn.execute("SELECT COUNT(*) FROM tag WHERE address='nAlice'"
                           ).fetchone()[0] == 1


def test_a_tag_can_be_handed_to_somebody_else(engine):
    eng, state, db = engine
    feed(eng, state, [claim(0, "robin")])
    feed(eng, state, [claim(1, "robin", kind=T.KIND_TRANSFER, reference="nBob")])
    assert db.conn.execute("SELECT address FROM tag").fetchone()[0] == "nBob"


def test_only_the_holder_can_send_a_tag(engine):
    eng, state, db = engine
    feed(eng, state, [claim(0, "robin")])
    feed(eng, state, [claim(1, "robin", sender="nThief", kind=T.KIND_TRANSFER,
                            reference="nThief")])
    assert reason(db, 1) == "that tag is not yours to send"
    assert db.conn.execute("SELECT address FROM tag").fetchone()[0] == "nAlice"


def test_a_tag_is_not_dropped_on_somebody_who_has_one(engine):
    """Quietly losing your name because a stranger sent you another is a way to
    lose a name without being asked."""
    eng, state, db = engine
    feed(eng, state, [claim(0, "robin"), claim(1, "bob", sender="nBob")])
    feed(eng, state, [claim(2, "robin", kind=T.KIND_TRANSFER, reference="nBob")])
    assert "already holds @bob" in reason(db, 2)
    assert db.conn.execute(
        "SELECT address FROM tag WHERE tag='robin'").fetchone()[0] == "nAlice"


def test_a_reorg_takes_a_name_back(engine):
    eng, state, db = engine
    feed(eng, state, [claim(0, "robin")])
    feed(eng, state, [claim(1, "robin", kind=T.KIND_TRANSFER, reference="nBob",
                            height=500)])
    assert db.conn.execute("SELECT address FROM tag").fetchone()[0] == "nBob"

    state.rollback_block(500)
    assert db.conn.execute("SELECT address FROM tag").fetchone()[0] == "nAlice"
    state.rollback_block(100)
    assert db.conn.execute("SELECT COUNT(*) FROM tag").fetchone()[0] == 0


def test_the_index_answers_both_ways(tmp_path):
    from arcade.ledger import LedgerIndex

    path = tmp_path / "l.sqlite"
    db = Database(path)
    install_schema(db)
    state = StateDB(db)
    eng = Engine(state, NETWORKS["regtest"])
    feed(eng, state, [claim(0, "robin"), claim(1, "boxa", sender="nBob")])
    db.close()

    index = LedgerIndex(path, NETWORKS["regtest"], rpc_factory=lambda: None)
    assert index.tag_of("nAlice") == "robin"
    assert index.address_of("@the operator") == "nAlice", "a name is case-insensitive"
    assert index.address_of("nobody") is None
    assert index.tags_for(["nAlice", "nBob", "nNobody"]) == {
        "nAlice": "robin", "nBob": "boxa"}


def test_what_to_show_for_somebody(engine):
    assert T.display("robin") == "@robin"
    assert T.display(None, "nYW2BPLENpu2nGa7WCExvzxD3hQYueULFa").endswith("…")
    assert T.display(None, "") == "anonymous"


def test_the_at_sign_is_punctuation_and_nothing_depends_on_it():
    """Nobody types it consistently. What tells a name from an address is the
    shape -- 24 characters of a-z0-9_ against 34 of mixed-case base58, so no
    string can be read as both (D-112)."""
    from arcade import tags as T

    for written in ("@boxa", "boxa", " @boxa ", "@a test machine"):
        assert T.looks_like_a_tag(written), written
        assert T.normalise(written) == "boxa"

    # A BARE name is read as one only if it is written as one. With the @ any
    # case is plainly a name; without it, "PoNotARealAddress" is somebody
    # mistyping base58 and deserves the checksum complaint, not "nobody holds
    # that name".
    assert not T.looks_like_a_tag("a test machine")
    assert not T.looks_like_a_tag("PoNotARealAddress")
    assert T.normalise("a test machine") == "boxa", "and it is still the same name"

    for other in ("nqW8nXSzigaSx1wTTTtUkMLYkbrYNJLRhz",
                  "PognhfhGxiSNPrYLQYUaT5bMsVbgumzc6i",
                  "arcade:test:9GYRWhff7rnQQGe9j7q7KS2yRAc8",
                  "", "  ", "a", "x" * 30, "no-hyphens", "no.dots"):
        assert not T.looks_like_a_tag(other), other


def test_a_search_puts_the_likelier_name_first(tmp_path):
    """Somebody typing "mar" means @robin far more often than @postmarket,
    so a prefix ranks above the middle of a word and an exact match wins
    outright. This is the ledger's rule; the address book only draws it."""
    from arcade.config import NETWORKS
    from arcade.db import Database
    from arcade.ledger import LedgerIndex
    from arcade.state import install_schema

    path = tmp_path / "ledger.sqlite"
    db = Database(path)
    install_schema(db)
    for n, tag in enumerate(("postmarket", "robin", "mar", "marigold")):
        db.conn.execute("INSERT INTO tag(tag,address,claimed_txid,block_height,"
                        "position) VALUES(?,?,?,?,?)",
                        (tag, f"address{n}", f"{n:064x}", 100 + n, 0))
    db.conn.commit()
    db.close()

    index = LedgerIndex(path, NETWORKS["regtest"], rpc_factory=lambda: None)
    assert [t["tag"] for t in index.search_tags("mar")] == [
        "mar", "robin", "marigold", "postmarket"], \
        "exact, then prefixes shortest first, then the middle of a word"
    # The @ changes nothing, and neither does case.
    assert index.search_tags("@MAR") == index.search_tags("mar")
    assert index.search_tags("  ") == []

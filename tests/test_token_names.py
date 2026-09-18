"""One name, one token.

The same name was issued twice on two machines and both tokens were real: a
hundred "Dogecoin Arcade" here and a hundred there, telling anybody reading a
balance or an order book two different things with one word (D-122).
"""

import pytest

from arcade import payload as P
from arcade.config import NETWORKS
from arcade.db import Database
from arcade.state import Engine, InvalidTransaction, StateDB, install_schema, name_key
from arcade.tx import ArcadeTransaction, EncodingClass


@pytest.fixture
def engine(tmp_path):
    db = Database(tmp_path / "ledger.sqlite")
    install_schema(db)
    state = StateDB(db)
    return Engine(state, NETWORKS["regtest"]), state, db


def issue(eng, state, n, name, sender="nMe", amount=100_000_000, managed=False):
    msg = (P.IssuanceManaged(ecosystem=2, property_type=2, previous_property_id=0,
                             category="", subcategory="", name=name, url="", data="")
           if managed else
           P.IssuanceFixed(ecosystem=2, property_type=2, previous_property_id=0,
                           category="", subcategory="", name=name, url="", data="",
                           amount=amount))
    rtx = ArcadeTransaction(
        txid=f"{n:064x}", block_height=100 + n, position=0,
        encoding_class=EncodingClass.B, sender=sender, reference=None,
        payload=msg.encode(), fee=0)
    with state.block_context(rtx.block_height, f"h{n}", "prev", 0, 1, 0):
        eng.process(rtx)
    return rtx.txid


def names(db):
    return [r["name"] for r in db.conn.execute(
        "SELECT name FROM property ORDER BY property_id")]


def test_the_second_token_of_a_name_is_not_created(engine):
    eng, state, db = engine
    issue(eng, state, 1, "Dogecoin Arcade")
    issue(eng, state, 2, "Dogecoin Arcade", sender="nSomebodyElse")
    assert names(db) == ["Dogecoin Arcade"]


def test_the_issuer_cannot_take_their_own_name_twice(engine):
    eng, state, db = engine
    issue(eng, state, 1, "Dogecoin Arcade")
    issue(eng, state, 2, "Dogecoin Arcade")
    assert names(db) == ["Dogecoin Arcade"]


def test_a_name_is_the_same_name_however_it_is_written(engine):
    """Case, spacing and punctuation are how a name is written, not which
    name it is -- otherwise the rule is stepped around with the space bar."""
    eng, state, db = engine
    issue(eng, state, 1, "Dogecoin Arcade")
    for n, spelling in enumerate(["dogecoin arcade", "DOGECOIN ARCADE",
                                  "Dogecoin-Arcade", "DogecoinArcade",
                                  "  Dogecoin   Arcade  ", "Dogecoin.Arcade!"], start=2):
        issue(eng, state, n, spelling)
    assert names(db) == ["Dogecoin Arcade"]


def test_a_different_name_is_still_free(engine):
    eng, state, db = engine
    issue(eng, state, 1, "Dogecoin Arcade")
    issue(eng, state, 2, "Dogecoin Arcade 2")
    issue(eng, state, 3, "Pepecoin Arcade")
    assert names(db) == ["Dogecoin Arcade", "Dogecoin Arcade 2", "Pepecoin Arcade"]


def test_managed_and_fixed_share_one_namespace(engine):
    """Two kinds of token, one set of names: a reader cannot see the
    difference and should not have to."""
    eng, state, db = engine
    issue(eng, state, 1, "Dogecoin Arcade", managed=True)
    issue(eng, state, 2, "dogecoin arcade")
    assert names(db) == ["Dogecoin Arcade"]


def test_the_first_in_the_block_wins(engine):
    """Two issuances in one block: the earlier position takes the name, which
    is the only ordering every node already agrees on."""
    eng, state, db = engine
    for position, sender in enumerate(("nFirst", "nSecond")):
        msg = P.IssuanceFixed(ecosystem=2, property_type=2, previous_property_id=0,
                              category="", subcategory="", name="Dogecoin Arcade",
                              url="", data="", amount=100)
        rtx = ArcadeTransaction(
            txid=f"{position + 1:064x}", block_height=200, position=position,
            encoding_class=EncodingClass.B, sender=sender, reference=None,
            payload=msg.encode(), fee=0)
        if position == 0:
            ctx = state.block_context(200, "h200", "prev", 0, 2, 0)
            ctx.__enter__()
        eng.process(rtx)
    ctx.__exit__(None, None, None)
    rows = [dict(r) for r in db.conn.execute("SELECT issuer FROM property")]
    assert [r["issuer"] for r in rows] == ["nFirst"]


def test_a_name_of_nothing_but_punctuation_is_refused(engine):
    """It has no folded form, so it cannot be compared with anything -- and a
    name nobody can say is not a name."""
    eng, state, db = engine
    issue(eng, state, 1, "???")
    assert names(db) == []


def test_what_the_fold_keeps():
    assert name_key("Dogecoin Arcade") == "dogecoinarcade"
    assert name_key("  DOGE-coin  ") == "dogecoin"
    assert name_key("") == "" and name_key("!!") == ""
    assert name_key("Token 2") == "token2", "digits are part of a name"


def test_an_invalid_issuance_changes_nothing_else(engine):
    """A refused issuance is refused whole: no property, no balance, and no
    id burned.

    The last of those was asserted before it was established -- the survivor
    keeping the number it took says nothing about what the NEXT token gets,
    which is the whole question (a test machine). It is the next id that has to be
    looked at, and nobody had looked.
    """
    eng, state, db = engine
    issue(eng, state, 1, "Dogecoin Arcade")
    first = db.conn.execute("SELECT property_id FROM property").fetchone()[0]

    issue(eng, state, 2, "dogecoin arcade", sender="nSomebodyElse")
    assert [r["property_id"] for r in db.conn.execute(
        "SELECT property_id FROM property")] == [first]
    held = db.conn.execute(
        "SELECT COUNT(*) AS n FROM balance WHERE address = 'nSomebodyElse'").fetchone()
    assert held["n"] == 0

    issue(eng, state, 3, "Something Else", sender="nSomebodyElse")
    ids = [r["property_id"] for r in db.conn.execute(
        "SELECT property_id FROM property ORDER BY property_id")]
    assert ids == [first, first + 1], \
        "the next token takes the next number: the refusal consumed nothing"
    assert len(names(db)) == 2


def test_a_lookalike_from_another_alphabet_is_refused_not_folded(engine):
    """The hole a fold cannot close.

    "Dogecoin" with a Cyrillic o renders identically to "Dogecoin" on every
    page either machine draws. Folded, it passes whichever way the fold is
    written: drop the character and the key is "dgecoin", keep it and the key
    carries a Cyrillic o -- a new name both times. So the character is
    refused instead, which cannot be gamed by adding more of them (a test machine,
    D-122).
    """
    eng, state, db = engine
    issue(eng, state, 1, "Dogecoin")
    issue(eng, state, 2, "Dоgecoin", sender="nImposter")     # Cyrillic о
    issue(eng, state, 3, "Ｄogecoin", sender="nImposter")     # full-width Ｄ
    issue(eng, state, 4, "Dogec₀in", sender="nImposter")     # subscript zero
    assert names(db) == ["Dogecoin"]


def test_what_a_name_may_contain_is_answered_about_the_name_alone(engine):
    """Separate from uniqueness and asked first: an empty chain refuses it
    just the same, so nobody learns the rule only when somebody else has
    already taken a name."""
    from arcade.state import name_complaint

    eng, state, db = engine
    issue(eng, state, 1, "Dоgecoin")
    assert names(db) == []
    assert name_complaint("Dogecoin Arcade") == ""
    assert "other alphabets" in name_complaint("Dоgecoin")
    assert "letter or digit" in name_complaint("!!!")
    assert "empty" in name_complaint("   ")
    assert name_complaint("A.1") == "" and name_complaint("Token #2") == ""


def test_punctuation_and_spacing_still_collide(engine):
    """The aggressive fold is kept: distinct tickers that differ only in
    spacing or punctuation are one name, and the second creator is told
    before paying rather than after."""
    eng, state, db = engine
    issue(eng, state, 1, "A.1")
    issue(eng, state, 2, "A1")
    issue(eng, state, 3, "Doge Coin")
    issue(eng, state, 4, "Dogecoin")
    assert names(db) == ["A.1", "Doge Coin"]


def test_a_name_claimed_in_the_mempool_is_claimed(tmp_path):
    """The gap the first version left open, and the one that cost money.

    A wallet knew what IT had broadcast and nothing about what anybody else
    had, so a name claimed on another machine two seconds ago was invisible
    until a block landed. The chain refuses the second issuance either way --
    it refuses it after the fee is spent (D-124).
    """
    from arcade import payload as P
    from arcade.config import NETWORKS
    from arcade.ledger import LedgerIndex

    params = NETWORKS["regtest"]
    issuance = P.IssuanceFixed(ecosystem=2, property_type=2,
                               previous_property_id=0, category="",
                               subcategory="", name="Dogecoin Arcade", url="",
                               data="", amount=100).encode()

    class Rpc:
        """A node whose mempool holds somebody else's issuance."""

        def __enter__(self): return self
        def __exit__(self, *_): return False

        def call(self, method, *args):
            if method == "getrawmempool":
                return ["aa" * 32]
            if method == "getrawtransaction":
                return {"txid": "aa" * 32, "vin": [], "vout": []}
            raise AssertionError(method)

    index = LedgerIndex(tmp_path / "ledger.sqlite", params, lambda: Rpc())

    # The transaction is read by the same extractor the indexer uses, so the
    # test stands in at that seam rather than faking a whole transaction.
    import arcade.tx as txlib

    class Fake:
        txid = "aa" * 32
        sender = "nSomebodyElse"
        payload = issuance

    original = txlib.extract
    txlib.extract = lambda *a, **k: Fake()
    try:
        rows = index.pending_names()
    finally:
        txlib.extract = original

    assert [r["name"] for r in rows] == ["Dogecoin Arcade"]
    assert rows[0]["sender"] == "nSomebodyElse", "whose it is, for the message"

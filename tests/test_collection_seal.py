"""What a collection will and will not admit.

A set is (creator, name). Everything here is about the two ways that was
not enough: the same build inscribed twice, which doubled a set that was
supposed to be a hundred pieces, and a set with no stated size, which can
never be finished because nothing says when it is (D-120).
"""

import json

import pytest

from arcade import collections as C
from arcade import inscriptions as I
from arcade import payload as P
from arcade.config import NETWORKS
from arcade.db import Database
from arcade.state import Engine, StateDB, install_schema
from arcade.tx import ArcadeTransaction, EncodingClass


@pytest.fixture
def engine(tmp_path):
    db = Database(tmp_path / "ledger.sqlite")
    install_schema(db)
    state = StateDB(db)
    return Engine(state, NETWORKS["regtest"]), state, db


class Chain:
    """Somewhere to inscribe pieces, one after another, in block order."""

    def __init__(self, engine):
        self.eng, self.state, self.db = engine
        self.n = 0

    def inscribe(self, meta: dict, sender: str = "nCreator") -> str:
        bodies = I.plan(b"a picture", "image/png", json.dumps(meta))
        first = None
        for body in bodies:
            self.n += 1
            rtx = ArcadeTransaction(
                txid=f"{self.n:064x}", block_height=100 + self.n, position=0,
                encoding_class=EncodingClass.B, sender=sender, reference=None,
                payload=P.AnyData(data=body).encode(), fee=0)
            first = first or rtx.txid
            with self.state.block_context(rtx.block_height, f"h{self.n}", "prev", 0, 1, 0):
                self.eng.process(rtx)
        return first

    def members(self, creator="nCreator", collection="Pixel Skulls"):
        return [dict(r) for r in self.db.conn.execute(
            "SELECT edition, txid FROM collection_item "
            "WHERE creator = ? AND collection = ? ORDER BY edition",
            (creator, collection)).fetchall()]

    def inscriptions(self) -> int:
        return self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM inscription").fetchone()["n"]


def item(edition: int, supply: int | None = None) -> dict:
    meta: dict = {"name": f"Pixel Skulls #{edition}", "edition": edition}
    if supply is not None:
        meta["collection"] = {"name": "Pixel Skulls", "supply": supply}
    return meta


def test_a_set_cannot_be_inscribed_twice(engine):
    """The report this exists for: a hundred pieces read as two hundred,
    every one of them present in duplicate."""
    chain = Chain(engine)
    for edition in range(1, 6):
        chain.inscribe(item(edition, supply=5 if edition == 1 else None))
    assert [m["edition"] for m in chain.members()] == [1, 2, 3, 4, 5]
    first = {m["edition"]: m["txid"] for m in chain.members()}

    for edition in range(1, 6):       # the whole build, sent a second time
        chain.inscribe(item(edition, supply=5 if edition == 1 else None))

    assert [m["edition"] for m in chain.members()] == [1, 2, 3, 4, 5]
    assert {m["edition"]: m["txid"] for m in chain.members()} == first, \
        "the pieces that were there first are the ones that stayed"
    assert chain.inscriptions() == 10, \
        "the second copies are still inscriptions -- they were paid for"


def test_an_edition_is_claimed_once_even_with_no_supply(engine):
    """A set that never says how big it is still cannot hold two #3s."""
    chain = Chain(engine)
    for edition in (1, 2, 3):
        chain.inscribe(item(edition))
    chain.inscribe(item(3))
    assert [m["edition"] for m in chain.members()] == [1, 2, 3]


def test_the_first_piece_says_how_many_there_are(engine):
    chain = Chain(engine)
    chain.inscribe(item(1, supply=3))
    for edition in (2, 3, 4):
        chain.inscribe(item(edition))
    assert [m["edition"] for m in chain.members()] == [1, 2, 3], \
        "a fourth piece does not join a three-piece set"


def test_a_number_beyond_the_supply_is_refused(engine):
    chain = Chain(engine)
    chain.inscribe(item(1, supply=3))
    chain.inscribe(item(99))
    assert [m["edition"] for m in chain.members()] == [1]


def test_an_unnumbered_piece_is_claimed_by_its_name(engine):
    chain = Chain(engine)
    for name in ("Dapper Cat", "Dapper Cat", "Bored Cat"):
        chain.inscribe({"name": name, "collection": "Cats"})
    assert len(chain.members(collection="Cats")) == 2


def test_nobody_else_can_inscribe_into_your_set(engine):
    """The creator is half of the key, so a stranger's piece opens a set of
    their own rather than landing in yours -- whatever its JSON says."""
    chain = Chain(engine)
    chain.inscribe(item(1, supply=3))
    chain.inscribe(item(2), sender="nStranger")

    assert [m["edition"] for m in chain.members()] == [1]
    assert [m["edition"] for m in chain.members(creator="nStranger")] == [2]


def test_a_full_set_is_full_for_everyone_including_its_creator(engine):
    chain = Chain(engine)
    chain.inscribe(item(1, supply=2))
    chain.inscribe(item(2))
    chain.inscribe(item(3))
    chain.inscribe(item(3), sender="nStranger")
    assert [m["edition"] for m in chain.members()] == [1, 2]


def test_a_refused_piece_still_belongs_to_whoever_made_it(engine):
    """Refusing membership is not confiscation: the inscription is there,
    numbered, owned, and sellable."""
    chain = Chain(engine)
    chain.inscribe(item(1, supply=1))
    txid = chain.inscribe(item(2))
    row = chain.db.conn.execute(
        "SELECT owner, number FROM inscription WHERE txid = ?", (txid,)).fetchone()
    assert row is not None and row["owner"] == "nCreator"


def test_the_wizard_stamps_the_size_on_the_first_piece(tmp_path):
    """Every set inscribed here says how big it is, whether or not the
    creator filled anything in."""
    build = C.Build(folder=tmp_path, collection="Pixel Skulls", items=[
        C.Item(edition=n, name=f"Pixel Skulls #{n}", image=f"{n}.png",
               content_type="image/png", size=10, json=json.dumps(
                   {"name": f"Pixel Skulls #{n}", "edition": n}))
        for n in (1, 2, 3)])

    said = C.with_details(build, {})
    assert I.collection_details(said.items[0].json)["supply"] == 3
    assert I.collection_of(said.items[0].json)[0] == "Pixel Skulls", \
        "saying how big it is does not move it out of its own set"
    assert said.items[1].json == build.items[1].json, "only the #1 carries it"

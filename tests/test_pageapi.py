"""Routes an inscription declares, and what they will and will not answer.

Two NFTs interacting is the point: a Pokemon-style piece asking another for
its power, a battle arena asking both whether they are ready. The answer has
to arrive whether or not the holder is sitting at the screen, which is why the
node answers on the inscription's behalf -- and why what it may answer is
declared in the inscription itself rather than decided by whoever holds it.

Declared, never executed. Everything here is a lookup.
"""

import json

import pytest

from arcade import pageapi


def piece(**meta):
    """An inscription row carrying this JSON."""
    return {"txid": "ab" * 32, "number": 7, "owner": "nHolder",
            "creator": "nMaker", "collection": "Sparks", "edition": 3,
            "json": json.dumps(meta)}


FIGHTER = piece(
    name="Sparky",
    stats={"power": 55, "hp": 120, "moves": ["spark", "bite"]},
    api={
        "power": {"json": "stats.power"},
        "hp": {"json": "stats.hp"},
        "firstmove": {"json": "stats.moves.0"},
        "ready": {"store": "ready"},
        "holder": {"owner": True},
        "number": {"number": True},
        "set": {"collection": True},
        "greeting": {"const": "hello"},
    })


def test_a_piece_answers_what_it_declares():
    assert pageapi.answer(FIGHTER, "power") == 55
    assert pageapi.answer(FIGHTER, "hp") == 120
    assert pageapi.answer(FIGHTER, "firstmove") == "spark", "a list index too"
    assert pageapi.answer(FIGHTER, "holder") == "nHolder"
    assert pageapi.answer(FIGHTER, "number") == 7
    assert pageapi.answer(FIGHTER, "set") == {"collection": "Sparks", "edition": 3}
    assert pageapi.answer(FIGHTER, "greeting") == "hello"


def test_a_toggle_comes_from_what_the_page_saved():
    """The battle case: an arena asking both fighters whether they are ready.
    The value lives in the wallet's per-page storage, which the page itself
    wrote through arcade.storage."""
    assert pageapi.answer(FIGHTER, "ready", {"ready": "yes"}) == "yes"
    assert pageapi.answer(FIGHTER, "ready", {}) is None, "nothing saved, nothing to say"


def test_it_answers_nothing_it_was_not_asked_to():
    """A route is an opt-in, inscribed by whoever made the piece. A holder
    cannot widen it, and a caller cannot read past it."""
    with pytest.raises(pageapi.ApiError, match="answers no route"):
        pageapi.answer(FIGHTER, "secret")
    # Storage the declaration does not mention stays private even though the
    # node has it in hand.
    assert pageapi.answer(FIGHTER, "ready", {"ready": "y", "password": "hunter2"}) == "y"


def test_a_path_that_is_not_there_is_nothing_rather_than_an_error():
    """A page asking about a field a piece does not have should be told
    nothing, not handed an exception to render."""
    thin = piece(api={"power": {"json": "stats.power"}})
    assert pageapi.answer(thin, "power") is None


def test_nonsense_declares_no_routes():
    """An inscription is permanent and may have been written by anyone."""
    for meta in ({"api": "not an object"}, {"api": ["nor", "this"]}, {},
                 {"api": {"bad": "spec"}}, {"api": {"two": {"json": "a", "owner": True}}},
                 {"api": {"": {"const": 1}}}, {"api": {"x" * 80: {"const": 1}}}):
        assert pageapi.declared(piece(**meta)) == {}, meta
    assert pageapi.declared({"json": "not json at all"}) == {}
    assert pageapi.declared({"json": ""}) == {}


def test_a_route_cannot_be_used_to_move_anything():
    """There are no writes here. A page that wants to send something files an
    approval and a person says yes."""
    for verb in ("send", "transfer", "sign", "spend", "approve"):
        assert verb not in pageapi.VERBS


def test_an_answer_too_big_to_carry_is_refused():
    """It travels in a node-to-node message, which is a chain transaction.
    Refused whole rather than truncated into something a caller misreads."""
    big = piece(blob="x" * 5000, api={"blob": {"json": "blob"}})
    with pytest.raises(pageapi.ApiError, match="over 2000 bytes"):
        pageapi.answer(big, "blob")


def test_the_declaration_is_readable_before_asking():
    """Public because it is inscribed: a caller can see what a route resolves
    to before spending a message on it."""
    routes = pageapi.declared(FIGHTER)
    assert routes["power"] == {"json": "stats.power"}
    assert routes["ready"] == {"store": "ready"}
    assert len(routes) == 8


"""The card watch (arcade/cardwatch.py; 2026-10-18: a clone of ASHVALE "spelt slightly different with two L's"
could drain a player's wallet -- "have our local model watch card publications, and flag any that appear as
duplications from a invalid user", "Hide until reviewed by operator. Send operator node account a notification.")."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from test_games import MAKER, OTHER, _inscribe                     # noqa: E402
from test_web import app_state, client                              # noqa: F401,E402

from arcade import cardwatch as cw                                  # noqa: E402


def test_lookalike_names_are_one_name():
    for copy in ("ASHVALLE", "Ashva1e", "ASHVA|E", "АSHVALE", "Ash Vale", "ashvale!", "ÁSHVALE", "A5HVALE"):
        assert cw.looks_like({"name": copy}, {"name": "ASHVALE"}), copy
    assert cw.looks_like({"name": "ASHVALEX"}, {"name": "ASHVALE"}), "one letter away"
    assert cw.looks_like({"name": "ASHVALE Remastered"}, {"name": "ASHVALE"}), "the name inside another"
    for own in ("RACE CONDITION", "Ashes", "Vale of Stars", "GHOST FLEET", "GEM DROP"):
        assert not cw.looks_like({"name": own}, {"name": "ASHVALE"}), own
    assert cw.looks_like({"name": "Totally Different", "cover": "ab" * 32}, {"name": "ASHVALE", "cover": "ab" * 32}), \
        "the same cover picture"


def card(txid, creator, name, height, **more):
    return {"txid": txid, "creator": creator, "name": name, "height": height, "net": "test", **more}


def test_the_later_lookalike_by_somebody_else_is_flagged_and_the_maker_never_is(tmp_path):
    w = cw.CardWatch(tmp_path / "cw.sqlite")
    cards = [card("a" * 64, MAKER, "ASHVALE", 100), card("b" * 64, MAKER, "ASHVALE", 120),
             card("c" * 64, OTHER, "ASHVALLE", 130), card("d" * 64, OTHER, "Gem Drop", 131)]
    flagged = w.check(cards)
    assert [c["txid"] for c in flagged] == ["c" * 64], "no model: a close lookalike is hidden on its own"
    assert w.hidden() == {"c" * 64}
    row = w.rows()[0]
    assert row["like_name"] == "ASHVALE" and row["like_creator"] == MAKER and row["by"] == "lookalike"
    assert w.check(cards) == [], "each card is looked at once"
    assert w.decide("c" * 64, cw.CLEARED) and w.hidden() == set(), "the operator clears it"


def test_a_trusted_publisher_is_never_hidden_even_when_later(tmp_path):
    w = cw.CardWatch(tmp_path / "cw.sqlite", trusted=lambda: {MAKER})
    w.check([card("c" * 64, OTHER, "ASHVALE", 90), card("a" * 64, MAKER, "ASHVALE", 100)])
    assert w.hidden() == {"c" * 64}, "the squatter who was first is still the copy of the trusted one"


def test_the_model_decides_the_close_ones(tmp_path, monkeypatch):
    model = {"url": "http://model/v1", "model": "m"}
    w = cw.CardWatch(tmp_path / "cw.sqlite", model=lambda: model)
    said = {}
    monkeypatch.setattr(w, "_ask", lambda new, old: said.setdefault(new["name"], None))
    said["ASHVALE Stories"] = (False, "a fan game with its own title")
    said["ASHVALLE"] = (True, "an extra L, the same description")
    cards = [card("a" * 64, MAKER, "ASHVALE", 100), card("b" * 64, OTHER, "ASHVALE Stories", 110),
             card("c" * 64, OTHER, "ASHVALLE", 120), card("e" * 64, OTHER, "ASHVAL3", 125)]
    w.check(cards)
    status = w.status()
    assert status["b" * 64] == cw.OK and status["c" * 64] == cw.FLAGGED
    assert "e" * 64 not in status, "the model did not answer: looked at again next time, not passed"
    assert any(r["by"] == "model" and "extra L" in r["why"] for r in w.rows())


def test_a_hidden_card_leaves_the_games_tab_and_the_operator_is_told(client):
    app, state = client
    real, copy = "a1" * 32, "c1" * 32
    _inscribe(state, real, 10, MAKER, {"game": {"name": "ASHVALE"}}, height=90)
    _inscribe(state, copy, 11, OTHER, {"game": {"name": "ASHVALLE"}}, height=95)
    from arcade.web import app as W  # noqa: F401
    games = app.get("/games").text
    assert copy in games, "not looked at yet"
    from arcade import cardwatch
    watch = cardwatch.CardWatch(pathlib.Path(state.home) / "cardwatch.sqlite")
    state._cardwatch = watch
    watch.check([card(real, MAKER, "ASHVALE", 90), card(copy, OTHER, "ASHVALLE", 95)])
    games = app.get("/games").text
    assert copy not in games and real in games
    assert app.get(f"/games/{copy}").status_code == 404, "its discussion and Play link go too"
    watch.decide(copy, cardwatch.CLEARED)
    assert copy in app.get("/games").text, "cleared: back on the tab"

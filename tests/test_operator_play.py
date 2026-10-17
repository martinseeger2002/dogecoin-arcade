"""The node plays games as itself (2026-10-05, the operator: on his own machine as the
operator a game said he had to log in). A seed, a pool-paid claim and a silent
send work for the node's own address with no sign-in; a silent send of anything
that is not the game's own asset back to the game fails instead of asking."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                                 # noqa: F401,E402


def test_the_node_is_not_told_to_sign_in_for_a_seed_or_a_claim(client, monkeypatch):
    app, state = client
    monkeypatch.setattr(type(state), "derived_address",
                        property(lambda self: "nNodePlayerAddressExample1111111111"))
    seed = app.post("/account/referee/seed", json={"game": "#999999"})
    assert seed.status_code != 403, "the node playing is not asked to sign in"
    assert "no such game" in seed.text or "refereed" in seed.text or seed.status_code == 400, seed.text
    take = app.post("/account/prize/take", json={"pool": "#999999", "lot": 0})
    assert take.status_code != 403, take.text


def test_a_stranger_from_outside_is_still_asked_to_sign_in(client):
    app, state = client
    was, state.public = state.public, True
    try:
        seed = app.post("/account/referee/seed", json={"game": "#1"},
                        headers={"host": "app.example", "cf-ray": "x"})
        assert seed.status_code in (403, 404), seed.text
    finally:
        state.public = was


def test_a_silent_send_that_is_not_the_games_own_fails_instead_of_asking(client):
    app, state = client
    asked = app.post("/r/send", json={"kind": "coins", "to": "nSomebodyElse11111111111111111111",
                                      "amount": "1", "silent": True},
                     headers={"referer": "http://testserver/content/" + "ab" * 32})
    said = asked.json()
    if asked.status_code == 202:
        assert said["status"] == "failed" and "say-so" in said["error"], said
        assert app.get("/r/send/" + said["id"]).json()["status"] == "failed"
    else:
        assert asked.status_code == 400, said        # refused before it was even filed

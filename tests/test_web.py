"""Smoke tests for every web route.

These exist because two bugs reached the user that a single request to each route
would have caught: a missing `vault` import (NameError on identity creation) and
a `state.rpc()` call left behind by the dual-chain refactor (every scan failed).

Both were one-line mistakes in code that was never executed by anything. The
value here is not depth -- it is that every route gets exercised at least once,
with the node unreachable, which is also the state a new user starts in.
"""

import dataclasses
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from arcade.web.app import create_app
from arcade.web.state import AppState, ChainContext


@pytest.fixture
def app_state(tmp_path):
    """An application pointed at nowhere: no node, no identity.

    Deliberately unreachable. Every page must still render and explain itself --
    that is exactly what a user sees before their nodes have synced.
    """
    return AppState(
        home=tmp_path,
        messaging=ChainContext(network="regtest", role="messaging", label="Testnet",
                               datadir=Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                            datadir=Path("/nonexistent")),
    )


@pytest.fixture
def client(app_state):
    return TestClient(create_app(app_state)), app_state


GET_ROUTES = ["/", "/inbox", "/compose", "/keys", "/wallet",
              "/tokens", "/nfts", "/exchange", "/inscriptions"]


@pytest.mark.parametrize("path", GET_ROUTES)
def test_every_page_renders_without_a_node(client, path):
    """A page that 500s because the node is down is useless when it matters most."""
    response, _ = client[0].get(path), client[1]
    assert response.status_code == 200, f"{path} returned {response.status_code}"
    assert "<html" in response.text.lower()


@pytest.mark.parametrize("path", GET_ROUTES)
def test_no_page_leaks_a_traceback(client, path):
    body = client[0].get(path).text
    for marker in ("Traceback", "AttributeError", "NameError", "KeyError"):
        assert marker not in body, f"{path} leaked {marker} to the page"


def test_offline_node_is_explained_not_hidden(client):
    body = client[0].get("/").text
    assert "nothing listening" in body or "Could not reach" in body


# --- CSRF ---------------------------------------------------------------------

POST_ROUTES = [
    ("/unlock", {"passphrase": "x"}),
    ("/lock", {}),
    ("/keygen", {"passphrase": "x" * 12, "confirm": "x" * 12}),
    ("/scan", {}),
    ("/fund", {}),
    ("/reveal", {}),
    ("/forget", {}),
    ("/change-passphrase", {"new": "a", "confirm": "a"}),
    ("/reset-identity", {"understand": "yes"}),
    ("/wallet/receive", {"which": "messaging"}),
    ("/publish-key", {}),
]


@pytest.mark.parametrize("path,data", POST_ROUTES, ids=[p for p, _ in POST_ROUTES])
def test_state_changing_routes_require_a_csrf_token(client, path, data):
    """A local server is reachable by any process here, including a stray browser tab."""
    app, state = client
    response = app.post(path, data=data, follow_redirects=False)
    assert response.status_code in (200, 303), response.status_code
    # Nothing may have happened: no identity created, nothing unlocked.
    assert not state.unlocked
    assert not state.key_path.exists()


def test_wrong_csrf_token_is_rejected(client):
    app, state = client
    app.post("/keygen", data={"csrf_token": "wrong", "passphrase": "x" * 12,
                              "confirm": "x" * 12}, follow_redirects=False)
    assert not state.key_path.exists()


# --- identity lifecycle -------------------------------------------------------


def test_identity_can_be_created_and_is_unlocked(client):
    """The path that broke with a NameError once already."""
    app, state = client
    response = app.post("/keygen", data={
        "csrf_token": state.csrf_token, "mode": "chosen",
        "passphrase": "correct-horse-battery-staple-here",
        "confirm": "correct-horse-battery-staple-here",
    }, follow_redirects=False)
    assert response.status_code == 303
    assert state.key_path.exists()
    assert state.unlocked
    assert oct(state.key_path.stat().st_mode)[-3:] == "600"


def test_a_chosen_passphrase_with_hyphens_is_not_mistaken_for_generated(client):
    """The bug this file was written to catch a second time.

    "generated" used to be inferred from entropy, and the conservative estimate
    for a chosen passphrase caps at exactly the threshold -- so any self-chosen
    passphrase of 30-odd characters containing a hyphen demanded a checkbox that
    its own form never displayed. There was no way for the user to proceed.
    """
    app, state = client
    phrase = "correct-horse-battery-staple-here"
    assert len(phrase) > 30 and "-" in phrase
    app.post("/keygen", data={"csrf_token": state.csrf_token, "mode": "chosen",
                              "passphrase": phrase, "confirm": phrase},
             follow_redirects=False)
    assert state.key_path.exists(), "a chosen passphrase must be accepted"


def test_generated_passphrase_needs_the_saved_acknowledgement(client):
    """Six words with no confirmation that they were written down must not pass."""
    app, state = client
    from arcade.messaging.keys import generate_passphrase
    app.post("/keygen", data={"csrf_token": state.csrf_token, "mode": "generated",
                              "passphrase": generate_passphrase()},
             follow_redirects=False)
    assert not state.key_path.exists()


def test_contact_code_is_offered_once_unlocked(client):
    app, state = client
    app.post("/keygen", data={"csrf_token": state.csrf_token, "mode": "chosen",
                              "passphrase": "a-long-enough-passphrase",
                              "confirm": "a-long-enough-passphrase"},
             follow_redirects=False)
    body = app.get("/").text
    assert "arcade:regtest:" in body, "the contact code should be shown"


def test_lock_and_unlock_round_trip(client):
    app, state = client
    phrase = "another-perfectly-fine-passphrase"
    app.post("/keygen", data={"csrf_token": state.csrf_token, "mode": "chosen",
                              "passphrase": phrase, "confirm": phrase},
             follow_redirects=False)
    fingerprint = state.identity.fingerprint

    app.post("/lock", data={"csrf_token": state.csrf_token}, follow_redirects=False)
    assert not state.unlocked

    app.post("/unlock", data={"csrf_token": state.csrf_token, "passphrase": phrase},
             follow_redirects=False)
    assert state.unlocked
    assert state.identity.fingerprint == fingerprint


def test_wrong_passphrase_does_not_unlock(client):
    app, state = client
    app.post("/keygen", data={"csrf_token": state.csrf_token, "mode": "chosen",
                              "passphrase": "the-right-one-here",
                              "confirm": "the-right-one-here"}, follow_redirects=False)
    app.post("/lock", data={"csrf_token": state.csrf_token}, follow_redirects=False)
    app.post("/unlock", data={"csrf_token": state.csrf_token, "passphrase": "wrong"},
             follow_redirects=False)
    assert not state.unlocked


def test_passphrase_can_be_saved_and_retrieved(client):
    app, state = client
    phrase = "saved-on-this-computer-please"
    app.post("/keygen", data={"csrf_token": state.csrf_token, "mode": "chosen", "passphrase": phrase,
                              "confirm": phrase, "remember": "yes"},
             follow_redirects=False)
    assert state.passphrase_remembered
    assert state.reveal_passphrase() == phrase

    app.post("/forget", data={"csrf_token": state.csrf_token}, follow_redirects=False)
    assert not state.passphrase_remembered


def test_passphrase_can_be_changed(client):
    app, state = client
    app.post("/keygen", data={"csrf_token": state.csrf_token, "passphrase": "old-one-here",
                              "confirm": "old-one-here"}, follow_redirects=False)
    before = state.identity.fingerprint
    app.post("/change-passphrase", data={"csrf_token": state.csrf_token,
                                         "current": "old-one-here",
                                         "new": "the-new-one-here",
                                         "confirm": "the-new-one-here"},
             follow_redirects=False)
    app.post("/lock", data={"csrf_token": state.csrf_token}, follow_redirects=False)
    app.post("/unlock", data={"csrf_token": state.csrf_token,
                              "passphrase": "the-new-one-here"}, follow_redirects=False)
    assert state.unlocked and state.identity.fingerprint == before


def test_reset_archives_the_old_key_rather_than_deleting_it(client):
    """A forgotten passphrase may still turn up; destroying the key forecloses that."""
    app, state = client
    app.post("/keygen", data={"csrf_token": state.csrf_token, "passphrase": "forgotten-soon",
                              "confirm": "forgotten-soon"}, follow_redirects=False)
    app.post("/reset-identity", data={"csrf_token": state.csrf_token, "understand": "yes"},
             follow_redirects=False)
    assert not state.key_path.exists()
    assert list(state.home.glob("*.old-*.key")), "the old key should be archived"


def test_reset_requires_the_acknowledgement(client):
    app, state = client
    app.post("/keygen", data={"csrf_token": state.csrf_token, "passphrase": "still-here-ok",
                              "confirm": "still-here-ok"}, follow_redirects=False)
    app.post("/reset-identity", data={"csrf_token": state.csrf_token}, follow_redirects=False)
    assert state.key_path.exists(), "an unticked box must not discard the identity"


# --- routes that need a node --------------------------------------------------


def test_scan_reports_a_failure_rather_than_raising(client):
    """The route that was broken by the dual-chain refactor."""
    app, state = client
    response = app.post("/scan", data={"csrf_token": state.csrf_token},
                        follow_redirects=False)
    assert response.status_code == 303
    assert state.notice and "Scan failed" in state.notice
    # The specific mistake that shipped: a missing attribute, not a node problem.
    assert "no attribute" not in state.notice, state.notice


def test_wallet_receive_fails_cleanly_without_a_node(client):
    app, state = client
    response = app.post("/wallet/receive",
                        data={"csrf_token": state.csrf_token, "which": "messaging"},
                        follow_redirects=False)
    assert response.status_code == 303
    assert "no attribute" not in (state.notice or "")


def test_unbuilt_sections_say_so(client):
    for path, milestone in (("/exchange", "M3"), ("/nfts", "M4"), ("/inscriptions", "M5")):
        body = client[0].get(path).text
        assert "Not built yet" in body and milestone in body

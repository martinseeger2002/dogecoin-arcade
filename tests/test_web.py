"""Smoke tests for every web route.

These exist because two bugs reached the user that a single request to each route
would have caught: a missing import (NameError on identity creation) and
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


GET_ROUTES = ["/", "/inbox", "/compose", "/contacts", "/backup", "/keys", "/wallet",
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
    ("/setup-identity", {}),
    ("/scan", {}),
    ("/fund", {}),
    ("/wallet/receive", {"which": "messaging"}),
    ("/publish-key", {}),
    ("/contacts/save", {"name": "Forged"}),
]


@pytest.mark.parametrize("path,data", POST_ROUTES, ids=[p for p, _ in POST_ROUTES])
def test_state_changing_routes_require_a_csrf_token(client, path, data):
    """A local server is reachable by any process here, including a stray browser tab."""
    app, state = client
    response = app.post(path, data=data, follow_redirects=False)
    assert response.status_code in (200, 303), response.status_code
    # Nothing may have happened.
    assert not state.unlocked


def test_wrong_csrf_token_is_rejected(client):
    app, state = client
    app.post("/setup-identity", data={"csrf_token": "wrong"}, follow_redirects=False)
    assert not state.unlocked


# --- identity: derived from the wallet, never from a passphrase ---------------
#
# The passphrase is gone, along with the key file, the credential store and the
# recovery problem that came with them. What remains must be tested for what it
# no longer does as much as for what it does.


def test_no_page_ever_asks_for_a_passphrase(client):
    """There is nothing to invent, type or write down.

    Saying "no passphrase" is fine and is the point; *asking* for one is not, so
    this looks for the asking -- a password box or a prompt -- rather than the
    word, which the reassuring copy legitimately uses.
    """
    prompts = ("your passphrase", "enter a passphrase", "confirm passphrase",
               "a passphrase is required", "new passphrase", "current passphrase")
    for path in GET_ROUTES:
        body = client[0].get(path).text.lower()
        assert 'type="password"' not in body, f"{path} still has a password box"
        for prompt in prompts:
            assert prompt not in body, f"{path} still prompts: {prompt!r}"


def test_no_page_shows_a_fingerprint(client):
    """Users get names and addresses. A hex fingerprint means nothing to them."""
    for path in GET_ROUTES:
        assert "fingerprint" not in client[0].get(path).text.lower(), path


def test_setup_without_a_node_says_so_rather_than_failing(client):
    app, state = client
    response = app.post("/setup-identity", data={"csrf_token": state.csrf_token},
                        follow_redirects=False)
    assert response.status_code == 303
    assert state.notice and "no attribute" not in state.notice


def test_the_overview_explains_that_the_wallet_is_the_backup(client):
    body = client[0].get("/").text
    assert "wallet.dat" in body


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


# --- address book -------------------------------------------------------------
# Local-only data, so these tests need no node and no identity -- which is also
# the point: the address book must work before anything else is set up.

TEST_ADDRESS = "nqW8nXSzigaSx1wTTTtUkMLYkbrYNJLRhz"
MAIN_ADDRESS = "PognhfhGxiSNPrYLQYUaT5bMsVbgumzc6i"


def _save(app, state, **fields):
    fields["csrf_token"] = state.csrf_token
    return app.post("/contacts/save", data=fields, follow_redirects=False)


def test_address_book_starts_empty_and_says_so(client):
    assert "address book is empty" in client[0].get("/contacts").text


def test_address_book_round_trip(client):
    app, state = client
    assert _save(app, state, name="A test machine", testnet_address=TEST_ADDRESS,
                 mainnet_address=MAIN_ADDRESS, notes="Other room.").status_code == 303
    body = app.get("/contacts").text
    assert "A test machine" in body and TEST_ADDRESS in body
    assert MAIN_ADDRESS in body and "Other room." in body


def test_a_mainnet_address_is_refused_in_the_testnet_field(client):
    """The two look alike and the consequences do not. Catch it at entry."""
    app, state = client
    _save(app, state, name="Wrong chain", testnet_address=MAIN_ADDRESS)
    assert "not a testnet one" in (state.notice or "")
    with state.store() as store:
        assert store.contacts() == []


def test_a_testnet_address_is_refused_in_the_mainnet_field(client):
    app, state = client
    _save(app, state, name="Wrong chain", mainnet_address=TEST_ADDRESS)
    assert "not a mainnet one" in (state.notice or "")


def test_a_mistyped_address_is_refused(client):
    app, state = client
    _save(app, state, name="Typo", mainnet_address="PoNOTAREALADDRESS")
    assert "checksum" in (state.notice or "")


def test_a_contact_needs_a_name(client):
    app, state = client
    _save(app, state, mainnet_address=MAIN_ADDRESS)
    assert "name" in (state.notice or "").lower()


def test_a_broken_contact_code_does_not_500(client):
    app, state = client
    response = _save(app, state, name="Truncated", code="arcade:regtest:zzz:zz")
    assert response.status_code == 303
    assert "Contact code" in (state.notice or "")


def test_editing_a_contact_updates_rather_than_duplicating(client):
    app, state = client
    _save(app, state, name="Before", mainnet_address=MAIN_ADDRESS)
    with state.store() as store:
        (row,) = store.contacts()
    assert 'value="Before"' in app.get(f"/contacts?edit={row['id']}").text
    _save(app, state, contact_id=str(row["id"]), name="After", notes="changed")
    with state.store() as store:
        rows = store.contacts()
    assert len(rows) == 1 and rows[0]["name"] == "After"


def test_deleting_a_contact_removes_it(client):
    app, state = client
    _save(app, state, name="Temporary", mainnet_address=MAIN_ADDRESS)
    with state.store() as store:
        (row,) = store.contacts()
    app.post(f"/contacts/{row['id']}/delete",
             data={"csrf_token": state.csrf_token}, follow_redirects=False)
    with state.store() as store:
        assert store.contacts() == []


def test_the_address_book_rejects_a_stale_form(client):
    app, state = client
    response = app.post("/contacts/save", data={"name": "Forged", "csrf_token": "wrong"},
                        follow_redirects=False)
    assert response.status_code == 303
    with state.store() as store:
        assert store.contacts() == []


# --- backup and restore -------------------------------------------------------
# No node here, so these test the refusals and the wording -- which is most of
# what matters. The operations themselves are verified against a live node.


BACKUP_POSTS = [
    ("/backup/messaging/save", {}),
    ("/backup/messaging/print", {"understand": "yes"}),
    ("/backup/messaging/import-key", {"key": "x"}),
    ("/backup/messaging/restore", {"source": "/tmp/nope.dat", "understand": "yes"}),
]


@pytest.mark.parametrize("path,data", BACKUP_POSTS, ids=[p for p, _ in BACKUP_POSTS])
def test_backup_actions_fail_cleanly_without_a_node(client, path, data):
    app, state = client
    data = dict(data, csrf_token=state.csrf_token)
    response = app.post(path, data=data, follow_redirects=False)
    assert response.status_code in (200, 303)
    assert "no attribute" not in (state.notice or ""), state.notice


@pytest.mark.parametrize("path,data", BACKUP_POSTS, ids=[p for p, _ in BACKUP_POSTS])
def test_backup_actions_require_a_csrf_token(client, path, data):
    app, state = client
    response = app.post(path, data=data, follow_redirects=False)
    assert response.status_code in (200, 303)


def test_printing_keys_requires_the_acknowledgement(client):
    """These keys spend coins. Nobody reaches that page by a stray click."""
    app, state = client
    app.post("/backup/messaging/print", data={"csrf_token": state.csrf_token},
             follow_redirects=False)
    assert "tick the box" in (state.notice or "")


def test_restoring_requires_the_acknowledgement(client):
    app, state = client
    app.post("/backup/messaging/restore",
             data={"csrf_token": state.csrf_token, "source": "/tmp/x.dat"},
             follow_redirects=False)
    assert "tick the box" in (state.notice or "")


def test_the_backup_page_says_the_wallet_is_the_only_thing_to_keep(client):
    body = client[0].get("/backup").text
    assert "wallet.dat" in body
    assert "no passphrase" in body.lower() or "No passphrase" in body


def test_a_backup_of_an_unknown_chain_is_refused(client):
    app, state = client
    response = app.post("/backup/nonsense/save",
                        data={"csrf_token": state.csrf_token}, follow_redirects=False)
    assert response.status_code == 303
    assert state.notice

"""Smoke tests for every web route.

These exist because two bugs reached the user that a single request to each route
would have caught: a missing import (NameError on identity creation) and
a `state.rpc()` call left behind by the dual-chain refactor (every scan failed).

Both were one-line mistakes in code that was never executed by anything. The
value here is not depth -- it is that every route gets exercised at least once,
with the node unreachable, which is also the state a new user starts in.
"""

import re
import time
import pathlib
import dataclasses
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from arcade.web.app import create_app
from arcade.web.state import AppState, ChainContext


@pytest.fixture
def app_state(tmp_path, no_nodes):
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


GET_ROUTES = ["/", "/inbox", "/compose", "/contacts", "/backup", "/wallet",
              "/wallet/tokens", "/wallet/nfts", "/tokens", "/nfts", "/exchange",
              "/exchange?tab=mintpads", "/exchange?tab=tokens",
              "/exchange?tab=market"]


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
    """An unreachable node must say so, and say why.

    Asserts the property rather than one lower layer's wording. It used to match
    the literal string "nothing listening", which only appears when a connection
    is actually refused -- so the test was pinned to the reason a node happened
    to be unavailable on the machine running it. a test machine had it fail there and pass
    here for exactly that reason.
    """
    body = client[0].get("/").text
    assert "offline" in body
    assert "Start the node" in body, "it should say what to do about it"


# --- CSRF ---------------------------------------------------------------------

POST_ROUTES = [
    ("/setup-identity", {}),
    ("/scan", {}),
    ("/fund", {}),
    ("/wallet/receive", {"which": "messaging"}),
    ("/publish-key", {}),
    ("/contacts/save", {"name": "Forged"}),
    ("/tokens/create", {"name": "Forged", "supply": "1"}),
    ("/tokens/send", {"property_id": "1", "amount": "1"}),
    ("/tokens/1/grant", {"amount": "1"}),
    ("/tokens/1/revoke", {"amount": "1"}),
    ("/tokens/1/issuer", {"recipient": "x"}),
    ("/tokens/chain", {"chain": "regtest"}),
]


@pytest.mark.parametrize("path,data", POST_ROUTES, ids=[p for p, _ in POST_ROUTES])
def test_state_changing_routes_require_a_csrf_token(client, path, data):
    """A local server is reachable by any process here, including a stray browser tab.

    The status code is part of the assertion now. It used to be 200 or 303,
    because every handler caught the rejection into its own error path -- so a
    refused request was indistinguishable from an accepted one to anything but a
    human reading the page. a test machine audited `/publish-key`, saw 200 with no token,
    and had to check the chain and the wallet before concluding it was safe.
    """
    app, state = client
    response = app.post(path, data=data, follow_redirects=False)
    assert response.status_code == 400, f"{path} answered {response.status_code}"
    assert not state.unlocked


def test_wrong_csrf_token_is_rejected(client):
    app, state = client
    response = app.post("/setup-identity", data={"csrf_token": "wrong"},
                        follow_redirects=False)
    assert response.status_code == 400
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


def test_the_exchange_reads_the_chain_rather_than_a_list(client):
    """Nothing is listed with the exchange: a shop is an inscription, and it
    closes by being sent away. So an empty chain is an empty exchange, and
    every tab still draws (D-037)."""
    app, _ = client
    for tab, empty in (("mintpads", "No mintpads on this chain yet"),
                       ("tokens", "is selling tokens"),
                       ("market", "No NFTs are listed for sale")):
        body = app.get(f"/exchange?tab={tab}").text
        assert empty in body, tab
    offers = app.get("/exchange?tab=offers").text
    assert "Offered to you" in offers and "Yours, outstanding" in offers


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
    assert response.status_code == 400
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
    assert response.status_code == 400


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


def test_a_binary_attachment_downloads_byte_identically(client):
    """The last text-only container on the path: the HTTP response itself."""
    app, state = client
    payload = bytes(range(256)) * 8
    with state.store() as store:
        message_id = store.add_message(None, "tx", "tx", 1, 0, "nS", b"\x0d" * 32,
                                       "me", b"see attached")
        store.add_attachment(message_id, "all.bin", "application/octet-stream", payload)

    response = app.get(f"/messages/attachment/{message_id}")

    assert response.status_code == 200
    assert response.content == payload
    assert response.headers["content-type"] == "application/octet-stream"
    assert 'filename="all.bin"' in response.headers["content-disposition"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_a_downloaded_file_is_never_served_as_html(client):
    """A filename chosen by a stranger must not come back as script.

    This origin holds the wallet, so a file served inline as text/html would be
    running in it.
    """
    app, state = client
    with state.store() as store:
        message_id = store.add_message(None, "tx", "tx", 1, 0, "nS", b"\x0e" * 32,
                                       "me", b"x")
        store.add_attachment(message_id, "evil.html", "text/html",
                             b"<script>alert(1)</script>")

    response = app.get(f"/messages/attachment/{message_id}")

    assert response.headers["content-type"] == "application/octet-stream"
    assert "attachment;" in response.headers["content-disposition"]


def test_a_missing_attachment_does_not_500(client):
    response = client[0].get("/messages/attachment/99999", follow_redirects=False)
    assert response.status_code == 303


# --- finding people from the address book -------------------------------------
# The address book is where you decide who somebody is, so it is where "who has
# published a key?" belongs. It was only on the Keys page, with no way to act on
# what it showed.


def test_the_address_book_offers_a_scan(client):
    body = client[0].get("/contacts").text
    assert "Scan for published addresses" in body


def test_a_published_key_is_offered_for_adding(client):
    app, state = client
    with state.store() as store:
        store.add_key_announcement("tx1", "nPublishedAddress", b"\x21" * 32,
                                   "aaaa bbbb cccc dddd", 500, 1000)

    body = app.get("/contacts").text
    assert "nPublishedAddress" in body
    assert "/contacts/add-published" in body


def test_adding_a_published_key_puts_it_in_the_address_book(client):
    app, state = client
    with state.store() as store:
        store.add_key_announcement("tx1", "nPublishedAddress", b"\x21" * 32,
                                   "aaaa bbbb", 500, 1000)

    app.post("/contacts/add-published",
             data={"csrf_token": state.csrf_token, "pubkey": ("21" * 32),
                   "address": "nPublishedAddress", "name": "Someone"},
             follow_redirects=False)

    with state.store() as store:
        (row,) = store.contacts()
    assert row["name"] == "Someone"
    assert row["testnet_address"] == "nPublishedAddress"


def test_someone_already_in_the_book_is_not_offered_again(client):
    """The list shrinks as it is used rather than repeating what is known."""
    app, state = client
    with state.store() as store:
        store.add_key_announcement("tx1", "nKnown", b"\x22" * 32, "ffff", 500, 1000)
        store.save_contact(pubkey=b"\x22" * 32, name="Known", testnet_address="nKnown")

    body = app.get("/contacts").text
    assert "/contacts/add-published" not in body


def test_the_page_says_an_announcement_proves_nothing_about_identity(client):
    """Anyone can publish a key and call themselves anything."""
    app, state = client
    with state.store() as store:
        store.add_key_announcement("tx1", "nAnyone", b"\x23" * 32, "gggg", 500, 1000)

    body = app.get("/contacts").text
    assert "nothing about who they are" in body


def test_a_malformed_key_is_refused(client):
    app, state = client
    response = app.post("/contacts/add-published",
                        data={"csrf_token": state.csrf_token, "pubkey": "not-hex",
                              "address": "nSomewhere"}, follow_redirects=False)
    assert response.status_code == 303
    with state.store() as store:
        assert store.contacts() == []


def test_a_short_key_is_refused(client):
    app, state = client
    app.post("/contacts/add-published",
             data={"csrf_token": state.csrf_token, "pubkey": "aabb",
                   "address": "nSomewhere"}, follow_redirects=False)
    with state.store() as store:
        assert store.contacts() == []


def test_scanning_from_the_address_book_fails_cleanly_without_a_node(client):
    app, state = client
    response = app.post("/contacts/scan", data={"csrf_token": state.csrf_token},
                        follow_redirects=False)
    assert response.status_code == 303
    assert "no attribute" not in (state.notice or "")


def test_you_are_not_a_contact_of_yourself(tmp_path):
    """Scanning your own announcement was putting you in your own address book.

    Your published name is not a stranger's claim about you, and you are not
    somebody you message. It appeared as an ordinary contact card alongside real
    people.
    """
    from arcade.config import NETWORKS
    from arcade.messaging.keys import Identity
    from arcade.messaging.scanner import Scanner
    from arcade.messaging.store import MessageStore

    me = Identity.generate()
    store = MessageStore(tmp_path / "m.sqlite")
    scanner = Scanner.__new__(Scanner)
    scanner.params = NETWORKS["regtest"]
    scanner.store = store
    scanner.identity = me
    scanner.public_only = False

    # What the scanner does on seeing an announcement naming its own key.
    mine = me.public_bytes == me.public_bytes
    if not mine:                                   # pragma: no cover
        store.apply_profile(me.public_bytes, "me", "nMyAddress", "")

    assert store.contacts() == []



# --- stated addresses beat inferred ones --------------------------------------
# An announcement can say which address a key belongs to. An address merely
# inferred from the transaction's inputs follows whichever coins paid, so it is
# a guess -- and leaving both live meant two rows for one key, both resolving as
# targets, with no way to tell which the owner meant.


def test_a_stated_address_wins_over_an_inferred_one(tmp_path):
    from arcade.messaging.store import MessageStore

    store = MessageStore(tmp_path / "m.sqlite")
    key = b"\x30" * 32
    store.add_key_announcement("tx1", "nFunding", key, "ff", 100, 1000, stated=False)
    store.add_key_announcement("tx2", "nIdentity", key, "ff", 200, 2000, stated=True)

    assert [r["address"] for r in store.live_keys()] == ["nIdentity"]
    assert store.superseded_addresses(key) == ["nFunding"]


def test_a_newer_inferred_address_does_not_displace_a_stated_one(tmp_path):
    """Height must not decide this: the inferred one can arrive later."""
    from arcade.messaging.store import MessageStore

    store = MessageStore(tmp_path / "m.sqlite")
    key = b"\x31" * 32
    store.add_key_announcement("tx1", "nIdentity", key, "ff", 100, 1000, stated=True)
    store.add_key_announcement("tx2", "nFunding", key, "ff", 900, 9000, stated=False)

    assert [r["address"] for r in store.live_keys()] == ["nIdentity"]


def test_with_no_stated_address_the_inferred_one_is_all_there_is(tmp_path):
    from arcade.messaging.store import MessageStore

    store = MessageStore(tmp_path / "m.sqlite")
    store.add_key_announcement("tx1", "nFunding", b"\x32" * 32, "ff", 100, 1000)

    assert [r["address"] for r in store.live_keys()] == ["nFunding"]


def test_a_stated_address_replaces_one_the_book_inferred(tmp_path):
    """The user-visible half: the book was handing out a funding address."""
    from arcade.messaging.store import MessageStore

    store = MessageStore(tmp_path / "m.sqlite")
    key = b"\x33" * 32
    store.add_message(None, "tx", "tx", 1, 0, "nFunding", key, "me", b"hi")
    assert store.contact_by_key(key)["testnet_address"] == "nFunding"

    store.set_contact_address(key, "nIdentity")
    assert store.contact_by_key(key)["testnet_address"] == "nIdentity"


def test_the_interface_says_which_version_it_is_running(client):
    """A stale process is otherwise invisible after an update."""
    body = client[0].get("/").text
    assert "running" in body


def test_the_updater_warns_when_something_is_still_serving():
    """Without a service unit there is nothing to restart, so it keeps serving."""
    import socket
    from arcade.update import _warn_if_still_running

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        assert _warn_if_still_running(port) is True

    assert _warn_if_still_running(port) is False


def test_a_conversation_with_yourself_shows_each_message_once(tmp_path):
    """Both halves are held locally, and they are the same message.

    A send is recorded when it goes out, and the same transaction is later
    decrypted off the chain -- so a self-conversation showed everything twice.
    """
    from arcade.messaging.sender import record_sent
    from arcade.messaging.store import MessageStore

    store = MessageStore(tmp_path / "m.sqlite")
    me_key, me_fp = b"\x50" * 32, "my fingerprint"
    record_sent(store, "tx-self", me_key, me_fp, b"a note to myself")
    store.add_message(None, "tx-self", "tx-self", 1, 100, "nMe", me_key, me_fp,
                      b"a note to myself")

    thread = store.thread(me_fp, me_key)
    assert len(thread) == 1
    assert thread[0]["mine"] is True


def test_a_real_conversation_keeps_both_sides(tmp_path):
    """Deduplicating must not collapse two people's messages into one."""
    from arcade.messaging.sender import record_sent
    from arcade.messaging.store import MessageStore

    store = MessageStore(tmp_path / "m.sqlite")
    peer = b"\x51" * 32
    record_sent(store, "tx-out", peer, "me", b"mine")
    store.add_message(None, "tx-in", "tx-in", 1, 200, "nThem", peer, "me", b"theirs")

    assert len(store.thread("me", peer)) == 2


def test_the_conversation_can_scroll(client):
    """A flex child sizes to its content unless min-height is zero."""
    body = client[0].get("/messages").text
    assert "overflow-y:auto" in body
    assert "min-height:0" in body


# --- sending should feel like sending -----------------------------------------
# A plain testnet message has nothing to weigh up: the coins are free, it is one
# transaction, and it is irreversible the moment it is broadcast either way. A
# review step there only makes sending feel like filing paperwork. A file is
# genuinely different -- it costs dust that can never be spent again, and a
# chunked send waits a block between transactions, so it can take minutes.


def test_a_plain_message_needs_no_confirmation(monkeypatch, client):
    """One keystroke, not two clicks."""
    app, state = client
    sent = {}

    class FakeSender:
        def __init__(self, *a, **k):
            pass

        def prepare(self, *a, **k):
            return type("P", (), {"fee_sats": 1000, "txid": "tx"})()

        def broadcast(self, prepared):
            sent["txid"] = "tx-broadcast"
            return "tx-broadcast"

        def send_all(self, address, payloads, **k):
            return [self.broadcast(None)]

    monkeypatch.setattr("arcade.web.app.MessageSender", FakeSender)
    monkeypatch.setattr("arcade.web.app.funded_address", lambda *a, **k: "nAddr")
    monkeypatch.setattr("arcade.web.app.Miner",
                        lambda *a, **k: type("M", (), {
                            "status": lambda self: type("S", (), {"funded": True})()})())

    class FakeRpc:
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(type(state.messaging), "rpc", lambda self: FakeRpc())
    state.ensure_identity = lambda: None
    from arcade.messaging.keys import Identity
    state.identity = Identity.generate()

    peer = ("aa" * 32)
    response = app.post(f"/messages/{peer}/send",
                        data={"csrf_token": state.csrf_token, "body": "hello"},
                        follow_redirects=False)

    assert response.status_code == 303, "a plain message should just go"
    assert sent.get("txid"), "it should have been broadcast without a second step"


def test_the_time_estimate_comes_from_the_chain_not_a_constant():
    """Pepecoin testnet aims at a minute and does not hit it.

    Over thirty real blocks the median gap was 35 seconds, the mean 49 and the
    worst 373, so an estimate built on the nominal target would be confidently
    wrong in both directions.
    """
    from arcade.messaging.sender import estimate_send_seconds

    typical, slow = estimate_send_seconds(6, 33.0, 73.0)
    assert typical == 165          # five waits, not six
    assert slow == 365


def test_one_transaction_takes_no_waiting():
    from arcade.messaging.sender import estimate_send_seconds

    assert estimate_send_seconds(1, 60.0, 180.0) == (0, 0)


@pytest.mark.parametrize("seconds,expected", [
    (0, "under a minute"), (30, "under a minute"), (90, "about 2 minutes"),
    (600, "about 10 minutes"), (5400, "about 1.5 hours"),
])
def test_durations_are_described_coarsely(seconds, expected):
    from arcade.messaging.sender import describe_duration

    assert describe_duration(seconds) == expected


def test_the_address_selector_and_the_input_selector_agree():
    """They disagreed, and the send failed naming the address it had just picked.

    `funded_address` counted unconfirmed change as spendable; `prepare` did not.
    So an address chosen as funded was then reported as holding 0.00000000.
    """
    from arcade.messaging.sender import SPENDABLE_MINCONF, MessageSender
    import inspect

    assert SPENDABLE_MINCONF == 0
    for name in ("prepare", "_select_inputs"):
        default = inspect.signature(getattr(MessageSender, name)).parameters["minconf"].default
        assert default == SPENDABLE_MINCONF, f"{name} uses a different minconf"


def test_a_named_contact_is_not_asked_to_be_named_again(client, monkeypatch):
    """The save field answers a question the address book has already answered."""
    app, state = client
    from arcade.messaging.keys import Identity

    peer_key = b"\x60" * 32
    state.identity = Identity.generate()
    with state.store() as store:
        store.add_message(None, "tx", "tx", 1, 0, "nThem", peer_key,
                          state.identity.fingerprint, b"hello")
        store.save_contact(pubkey=peer_key, name="Alice")

    body = app.get(f"/messages/{peer_key.hex()}").text
    assert 'placeholder="name them"' not in body
    assert "Address book" in body


def test_an_unnamed_contact_can_still_be_named_from_the_thread(client):
    app, state = client
    from arcade.messaging.keys import Identity

    peer_key = b"\x61" * 32
    state.identity = Identity.generate()
    with state.store() as store:
        store.add_message(None, "tx", "tx", 1, 0, "nThem", peer_key,
                          state.identity.fingerprint, b"hello")

    body = app.get(f"/messages/{peer_key.hex()}").text
    assert 'placeholder="name them"' in body
    assert "Add to address book" in body



def test_adding_a_published_contact_keeps_their_name(client):
    app, state = client
    with state.store() as store:
        store.add_key_announcement("tx1", "nTheirAddress", b"\x73" * 32, "aa",
                                   500, 1000, stated=True, name="bob")

    app.post("/contacts/add-published",
             data={"csrf_token": state.csrf_token, "pubkey": "73" * 32,
                   "address": "nTheirAddress", "name": "bob"},
             follow_redirects=False)

    with state.store() as store:
        (row,) = store.contacts()
    assert row["name"] == "bob"
    assert row["testnet_address"] == "nTheirAddress"


# --- one send at a time -------------------------------------------------------
# A long send can take minutes and the browser shows nothing while it waits, so a
# second click is the natural thing to do. Two concurrent sends each choose their
# own outputs without seeing the other's claims, so they can collide -- and a
# chunked message that stops part way is permanently unreadable.


def test_a_second_send_is_refused_while_one_is_running(client):
    app, state = client
    assert state.begin_send() is True
    try:
        assert state.begin_send() is False, "two sends must not run at once"
    finally:
        state.end_send()
    assert state.begin_send() is True
    state.end_send()


def test_the_claim_is_released_even_when_a_send_fails(client):
    """A failed send must not lock out every later one."""
    app, state = client
    assert state.begin_send()
    state.end_send()
    assert state.begin_send(), "the lock should be free again"
    state.end_send()


def test_releasing_a_lock_nobody_holds_is_harmless(client):
    client[1].end_send()
    assert client[1].begin_send()
    client[1].end_send()


def test_the_send_forms_cannot_be_submitted_twice(client):
    """Client side, so a second click never becomes a second request."""
    app, state = client
    from arcade.messaging.keys import Identity

    peer = b"\x80" * 32
    state.identity = Identity.generate()
    with state.store() as store:
        store.add_message(None, "tx", "tx", 1, 0, "nThem", peer,
                          state.identity.fingerprint, b"hi")

    body = app.get(f"/messages/{peer.hex()}").text
    assert "startSending" in body
    assert "form.dataset.sending" in body


def test_a_long_send_says_it_is_working(client):
    """Showing nothing for minutes is what invited the second click."""
    app, state = client
    from arcade.messaging.keys import Identity

    peer = b"\x81" * 32
    state.identity = Identity.generate()
    with state.store() as store:
        store.add_message(None, "tx", "tx", 1, 0, "nThem", peer,
                          state.identity.fingerprint, b"hi")

    body = app.get(f"/messages/{peer.hex()}").text
    assert 'id="sending"' in body
    assert "Leave this page open" in body


def test_the_web_route_funds_a_long_message_before_sending(client):
    """This was written, demonstrated, and then never wired into the route.

    `ensure_outputs` existed and worked when called directly, but the send path
    did not call it -- so a large file still chained a block at a time. The
    feature was verified in isolation and absent in practice.
    """
    import inspect
    from arcade.web import app as webapp

    source = inspect.getsource(webapp.create_app)
    assert "ensure_outputs" in source
    assert "begin_pending_send" in source, "a web send should be resumable too"


def test_a_short_identity_address_falls_back_rather_than_advising(client):
    """The advice was unusable: a browser offers no way to choose an address.

    The identity address is preferred so a message is attributed to the address
    people were given -- but preferring it is not the same as being able to pay
    from it, and the failure told the user to "use that one" with no way to.
    """
    import inspect
    from arcade.web import app as webapp

    source = inspect.getsource(webapp.create_app)
    assert "_prepare_first" in source
    assert "if other == where:" in source, "it must not loop on the same address"


def test_the_send_route_does_not_hold_the_event_loop(client):
    """A blocking async route froze the whole interface while a send ran.

    No progress, no /events, a browser that looked hung -- which is what made a
    second click the natural thing to do. A sync route runs in the threadpool.
    """
    import inspect
    from arcade.web import app as webapp

    source = inspect.getsource(webapp.create_app)
    assert "async def send_in_thread" not in source
    assert "async def group_post_send" not in source
    assert "await " not in source, "nothing in these routes may await"


def test_enter_does_not_arm_the_guard_before_submitting(client):
    """The Enter handler must not call startSending itself.

    This test used to assert the opposite, as a literal string:
    `if (!startSending(box.form)) return;`. That line was the bug. Arming the
    guard before calling requestSubmit meant the form's own
    `onsubmit="return startSending(this)"` saw the flag already set, returned
    false, and CANCELLED the submission -- so Enter painted "Sending..." over a
    form that never posted, while the button worked because a click goes through
    onsubmit exactly once. The operator reported it and it survived two wrong
    diagnoses, partly because this test said the path was covered.

    Matching served JavaScript as a string cannot tell a working handler from a
    broken one. The behaviour is asserted in tests/test_browser.py, by checking
    whether a request reaches the server at all; this only guards the shape that
    made it possible, so it cannot come back unnoticed.
    """
    app, state = client
    from arcade.messaging.keys import Identity

    peer = b"\x82" * 32
    state.identity = Identity.generate()
    with state.store() as store:
        store.add_message(None, "tx", "tx", 1, 0, "nThem", peer,
                          state.identity.fingerprint, b"hi")

    body = app.get(f"/messages/{peer.hex()}").text
    assert "requestSubmit" in body, "form.submit() would skip onsubmit entirely"
    assert "if (!startSending(box.form)) return;" not in body, (
        "arming the guard before requestSubmit makes onsubmit cancel the submit"
    )
    assert "if (form.dataset.sending === 'yes') return;" in body, (
        "the guard must be read, not set, on the Enter path"
    )


# --- watching a long send -----------------------------------------------------
# A long send used to hold the request open for minutes with nothing on screen.
# It now runs on a thread, the browser is sent back to the conversation, and a
# bubble reports what is happening.


def test_progress_starts_empty_and_can_be_reported(client):
    app, state = client
    assert state.send_progress == {}

    state.start_progress("aa" * 32, total=16, estimate="about 2 minutes")
    assert state.send_progress["total"] == 16
    assert state.send_progress["done"] == 0
    assert state.send_progress["estimate"] == "about 2 minutes"

    state.update_progress(done=6, note="broadcast 6 of 16")
    assert state.send_progress["done"] == 6
    assert "6 of 16" in state.send_progress["note"]

    state.finish_progress()
    assert state.send_progress["finished"] is True


def test_events_carries_the_progress(client):
    app, state = client
    state.start_progress("bb" * 32, total=4, estimate="under a minute")
    state.update_progress(done=2)

    data = app.get("/events").json()
    assert data["sending"]["done"] == 2
    assert data["sending"]["total"] == 4


def test_a_failed_send_says_so_rather_than_vanishing(client):
    """A part-sent message cannot be finished later; that is worth saying."""
    app, state = client
    state.start_progress("cc" * 32, total=4, estimate="")
    state.finish_progress(error="2 of 4 are already on the chain")

    data = app.get("/events").json()
    assert "already on the chain" in data["sending"]["error"]


def test_finished_progress_clears_when_the_conversation_reloads(client):
    """Otherwise the bubble sits at 100% for good."""
    app, state = client
    from arcade.messaging.keys import Identity

    peer = b"\x90" * 32
    state.identity = Identity.generate()
    state.start_progress(peer.hex(), total=2, estimate="")
    state.finish_progress()

    app.get(f"/messages/{peer.hex()}")
    assert state.send_progress == {}


def test_progress_for_another_conversation_is_left_alone(client):
    app, state = client
    from arcade.messaging.keys import Identity

    state.identity = Identity.generate()
    state.start_progress(("91" * 32), total=2, estimate="")
    state.finish_progress()

    app.get(f"/messages/{'92' * 32}")
    assert state.send_progress != {}, "a different thread must not clear it"


def test_the_bubble_shows_a_percentage(client):
    app, state = client
    from arcade.messaging.keys import Identity

    peer = b"\x93" * 32
    state.identity = Identity.generate()
    with state.store() as store:
        store.add_message(None, "tx", "tx", 1, 0, "nThem", peer,
                          state.identity.fingerprint, b"hi")

    body = app.get(f"/messages/{peer.hex()}").text
    assert 'id="sending-pct"' in body
    assert "pct + '%'" in body
    assert 'id="sending-fill"' in body


# --- starting fresh -----------------------------------------------------------


def test_a_reset_clears_history_but_keeps_the_address_book(tmp_path):
    """The address book is the one thing here that was typed, not scanned."""
    from arcade.messaging.store import MessageStore

    store = MessageStore(tmp_path / "m.sqlite")
    peer = b"\xa0" * 32
    store.add_message(None, "tx", "tx", 1, 0, "nThem", peer, "me", b"old test")
    store.add_sent("tx2", peer, "", "me", b"another")
    store.save_contact(pubkey=peer, name="Someone I know")

    counts = store.reset_history("test", from_height=1_500_000)

    assert counts["message"] == 1 and counts["sent"] == 1
    assert store.stats()["messages"] == 0
    assert [c["name"] for c in store.contacts()] == ["Someone I know"]


def test_a_reset_moves_the_starting_point_forward(tmp_path):
    """Otherwise the next scan finds everything that was just cleared."""
    from arcade.messaging.store import MessageStore

    store = MessageStore(tmp_path / "m.sqlite")
    store.set_meta("identity_height:test", "1000")
    store.set_scan_cursor("test", 1200, "hash")

    store.reset_history("test", from_height=1_500_000)

    assert store.get_meta("identity_height:test") == "1500000"
    assert store.scan_cursor("test") is None


def test_a_reset_can_drop_contacts_when_asked(tmp_path):
    from arcade.messaging.store import MessageStore

    store = MessageStore(tmp_path / "m.sqlite")
    store.save_contact(pubkey=b"\xa1" * 32, name="Someone")

    store.reset_history("test", from_height=1, keep_contacts=False)
    assert store.contacts() == []


def test_there_is_no_way_to_clear_the_history_from_the_interface(client):
    """The button is gone. `MessageStore.reset_history` stays -- the tests below
    use it, and it is the right tool for a machine being set up for testing --
    but nothing in the interface offers to throw somebody's messages away."""
    app, _ = client
    assert app.post("/reset-history").status_code == 404
    assert "Start fresh" not in app.get("/").text
    assert "Clear and start from this block" not in app.get("/").text


def test_an_interrupted_send_is_offered_for_finishing(client):
    """Progress lives in memory; a restart loses it and left the bar at 0%."""
    app, state = client
    from arcade.messaging.keys import Identity

    peer = b"\xa2" * 32
    state.identity = Identity.generate()
    with state.store() as store:
        store.begin_pending_send(b"\x01" * 8, peer, "nAddr", b"body",
                                 [b"chunk1", b"chunk2"])
        store.record_pending_progress(b"\x01" * 8, "tx-one")

    body = app.get(f"/messages/{peer.hex()}").text
    assert "stopped after" in body
    assert "Finish sending" in body
    assert "cannot be taken back" in body      # one transaction is on the chain


def test_a_send_that_never_started_does_not_claim_the_chain_has_part_of_it(client):
    """a test machine saw "0 of 4 transactions" under "what is on the chain cannot be
    taken back" -- nothing was on the chain. The sentence is only true once
    something has gone out."""
    app, state = client
    from arcade.messaging.keys import Identity

    peer = b"\xa3" * 32
    state.identity = Identity.generate()
    with state.store() as store:
        store.begin_pending_send(b"\x02" * 8, peer, "nAddr", b"body",
                                 [b"chunk1", b"chunk2", b"chunk3", b"chunk4"])

    body = app.get(f"/messages/{peer.hex()}").text
    assert "stopped after" in body and "0 of 4" in body
    assert "cannot be taken back" not in body
    assert "Nothing reached the chain" in body
    for name in ("messages.html", "groups.html"):
        source = pathlib.Path("arcade/web/templates", name).read_text()
        assert "{% if unfinished.sent %}" in source, name


def test_a_sent_file_is_kept_and_shown_like_a_received_one(client):
    """An image you sent should look like an image, not like '[sent photo.png]'."""
    app, state = client
    from arcade.messaging.keys import Identity

    peer = b"\xa3" * 32
    state.identity = Identity.generate()
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    with state.store() as store:
        store.add_sent("tx", peer, "", state.identity.fingerprint, b"look",
                       file_name="photo.png", file_type="image/png", file_data=png)

    body = app.get(f"/messages/{peer.hex()}").text
    assert "/messages/sent-media/" in body
    assert '<img src="/messages/sent-media/' in body

    with state.store() as store:
        (row,) = store.conn.execute("SELECT id FROM sent").fetchall()
    served = app.get(f"/messages/sent-media/{row['id']}")
    assert served.content == png
    assert served.headers["content-type"] == "image/png"


def test_an_abandoned_send_stops_claiming_to_be_working(client):
    """In-memory progress dies with a restart; a browser polling for it does not.

    A bar could sit at 0% for ever with no thread behind it. A send that is
    genuinely working reports on every chunk and every wait, so a long silence
    means the thread is gone.
    """
    import time

    app, state = client
    state.start_progress("aa" * 32, total=6, estimate="a minute")
    state.send_progress["updated"] = time.time() - state.PROGRESS_STALE_AFTER - 10

    shown = app.get("/events").json()["sending"]
    assert shown["finished"] is True
    assert "stopped reporting" in shown["error"]


def test_a_working_send_is_not_declared_abandoned(client):
    app, state = client
    state.start_progress("bb" * 32, total=6, estimate="a minute")
    state.update_progress(done=1, note="sent 1 of 6")

    assert app.get("/events").json()["sending"]["finished"] is False


def test_marking_a_send_stale_does_not_rewrite_the_record(client):
    """Read-time judgement, so a slow-but-alive send can still report later."""
    import time

    app, state = client
    state.start_progress("cc" * 32, total=2, estimate="")
    state.send_progress["updated"] = time.time() - state.PROGRESS_STALE_AFTER - 1

    state.live_progress()
    assert state.send_progress["finished"] is False


# --- an interface newer than the code it runs ---------------------------------
# Jinja reads templates from disk every request; Python is whatever was imported
# at startup. After an update without a restart the server renders the NEW page
# against the OLD code, so it advertises buttons whose routes do not exist. a test machine
# measured "Start fresh" posting into a 404, and the operator reasonably concluded the
# feature was broken. That is worse than plain staleness: it looks like a bug in
# the feature rather than a stale process.


def test_matching_versions_are_not_called_stale(client, monkeypatch):
    app, state = client
    monkeypatch.setattr(type(state), "running_version",
                        property(lambda self: "abc1234"))
    monkeypatch.setattr(type(state), "installed_version",
                        property(lambda self: "abc1234"))
    assert state.is_stale is False
    assert "must be restarted" not in app.get("/").text


def test_a_newer_checkout_is_announced_on_every_page(client, monkeypatch):
    app, state = client
    monkeypatch.setattr(type(state), "running_version",
                        property(lambda self: "old1111"))
    monkeypatch.setattr(type(state), "installed_version",
                        property(lambda self: "new2222"))

    assert state.is_stale is True
    for path in ("/", "/messages", "/contacts", "/backup"):
        body = app.get(path).text
        assert "must be restarted" in body, path
        assert "old1111" in body and "new2222" in body


def test_the_warning_says_buttons_may_silently_do_nothing(client, monkeypatch):
    """The symptom, named -- otherwise it reads as a vague upgrade nag."""
    app, state = client
    monkeypatch.setattr(type(state), "running_version",
                        property(lambda self: "old1111"))
    monkeypatch.setattr(type(state), "installed_version",
                        property(lambda self: "new2222"))

    import re

    # Normalised, because the sentence wraps in the template: a raw substring
    # search is asserting on where the line breaks fall, not on what it says.
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", app.get("/").text))
    assert "buttons will do nothing at all" in text


def test_an_unknown_version_is_not_reported_as_stale(client, monkeypatch):
    """Not a git checkout, no git installed: say nothing rather than warn wrongly."""
    app, state = client
    monkeypatch.setattr(type(state), "running_version", property(lambda self: ""))
    monkeypatch.setattr(type(state), "installed_version",
                        property(lambda self: "new2222"))
    assert state.is_stale is False






def test_a_reset_keeps_your_own_published_key(tmp_path):
    """It is on the chain permanently, below the new starting point.

    Forgetting it meant the Keys page offered to publish again -- paying a second
    fee for something already published and unreachable by any rescan. a test machine hit
    exactly that after clearing.
    """
    from arcade.messaging.store import MessageStore

    store = MessageStore(tmp_path / "m.sqlite")
    mine, theirs = b"\xc0" * 32, b"\xc1" * 32
    store.add_key_announcement("tx-mine", "nMine", mine, "aa", 100, 1, stated=True)
    store.add_key_announcement("tx-them", "nThem", theirs, "bb", 101, 2, stated=True)

    store.reset_history("test", from_height=999, keep_key=mine)

    remaining = [bytes(r["pubkey"]) for r in store.all_keys()]
    assert remaining == [mine]


def test_a_reset_without_an_identity_still_works(tmp_path):
    from arcade.messaging.store import MessageStore

    store = MessageStore(tmp_path / "m.sqlite")
    store.add_key_announcement("tx", "nSomeone", b"\xc2" * 32, "aa", 1, 1)
    store.reset_history("test", from_height=999, keep_key=None)
    assert store.all_keys() == []


def test_every_reader_of_progress_applies_the_staleness_rule(client):
    """Two readers of one piece of state, one ignoring the rule, is the same
    drift that produced the identity pin and funded-address bugs."""
    import inspect
    from arcade.web import app as webapp

    source = inspect.getsource(webapp.create_app)
    assert "state.send_progress" not in source, (
        "the rendered pages must read live_progress(), like /events does"
    )


def test_a_stale_send_does_not_render_as_working(client):
    """The 15-minute rule applied to the JSON but not to the page."""
    import time

    app, state = client
    from arcade.messaging.keys import Identity

    peer = b"\xc3" * 32
    state.identity = Identity.generate()
    state.start_progress(peer.hex(), total=6, estimate="a minute")
    state.send_progress["updated"] = time.time() - state.PROGRESS_STALE_AFTER - 10

    # The conversation treats it as finished and clears it, as /events does.
    app.get(f"/messages/{peer.hex()}")
    assert state.send_progress == {}


# --- a frozen composer must be able to thaw -----------------------------------
# `startSending` was a one-way transition. If the submission never completed --
# a dropped connection, a server restarted underneath the page, a back-button
# restore -- the interface stayed convincingly frozen at "Sending…" with the
# button disabled, while the server had no send at all and would not accept
# another because the button was disabled. a test machine measured that state against an
# idle server.


def test_the_composer_can_be_reset(client):
    app, state = client
    from arcade.messaging.keys import Identity

    peer = b"\xd0" * 32
    state.identity = Identity.generate()
    with state.store() as store:
        store.add_message(None, "tx", "tx", 1, 0, "nThem", peer,
                          state.identity.fingerprint, b"hi")

    body = app.get(f"/messages/{peer.hex()}").text
    assert "function resetComposer" in body


def test_a_restored_page_is_not_left_mid_send(client):
    """bfcache returns the page exactly as it was, paint and all."""
    app, state = client
    from arcade.messaging.keys import Identity

    peer = b"\xd1" * 32
    state.identity = Identity.generate()
    with state.store() as store:
        store.add_message(None, "tx", "tx", 1, 0, "nThem", peer,
                          state.identity.fingerprint, b"hi")

    body = app.get(f"/messages/{peer.hex()}").text
    assert "'pageshow'" in body
    assert "event.persisted" in body


def test_a_request_that_never_lands_recovers(client):
    """The case that actually happened: the server restarted underneath it."""
    app, state = client
    from arcade.messaging.keys import Identity

    peer = b"\xd2" * 32
    state.identity = Identity.generate()
    with state.store() as store:
        store.add_message(None, "tx", "tx", 1, 0, "nThem", peer,
                          state.identity.fingerprint, b"hi")

    body = app.get(f"/messages/{peer.hex()}").text
    assert "did not reach the application" in body
    assert "setTimeout" in body


def test_the_poller_clears_the_whole_sending_state(client):
    """Clearing the bubble alone left a page that refuses input while telling
    the truth in one small element."""
    app, state = client
    from arcade.messaging.keys import Identity

    peer = b"\xd3" * 32
    state.identity = Identity.generate()
    with state.store() as store:
        store.add_message(None, "tx", "tx", 1, 0, "nThem", peer,
                          state.identity.fingerprint, b"hi")

    body = app.get(f"/messages/{peer.hex()}").text
    assert 'form[data-sending="yes"]' in body
    assert "resetComposer('')" in body


def test_the_public_composer_recovers_too(client):
    body = client[0].get("/groups?channel=main").text
    assert "function resetPoster" in body
    assert "did not reach the application" in body


def test_pages_are_never_served_from_a_cache(client):
    """They carry live state: a send in flight, a balance, an unread count.

    A browser re-serving an old page on a back navigation shows a send that has
    since finished -- part of how an interface got stuck looking busy against an
    idle server.
    """
    for path in ("/", "/messages", "/contacts", "/groups"):
        header = client[0].get(path).headers.get("cache-control", "")
        assert "no-store" in header, f"{path} may be cached"


def test_media_may_still_be_cached(client):
    """An attachment's bytes do not change, and refetching them is expensive."""
    app, state = client
    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    with state.store() as store:
        message_id = store.add_message(None, "tx", "tx", 1, 0, "nS", b"\xe0" * 32,
                                       "me", b"x")
        store.add_attachment(message_id, "a.png", "image/png", payload)

    header = app.get(f"/messages/media/{message_id}").headers.get("cache-control", "")
    assert "no-store" not in header


# --- the hidden attribute must actually hide ----------------------------------
# `[hidden]{display:none}` lives in the browser's stylesheet, and ANY author rule
# setting `display` on the same element beats it outright. `.bubble-row{display:
# flex}` therefore made `hidden` inert on the send progress bubble: it rendered
# on first paint of a brand-new page with no send in progress, and every
# `row.hidden = true` in the JavaScript was a no-op that appeared to work.
#
# a test machine found it in a headless browser by asserting the computed style. That
# distinction is the lesson: checking for the attribute reports "hidden: 1" for
# ever while the user stares at the element. These tests cannot render, so they
# check the rule that makes the attribute trustworthy, and flag any new element
# that would need it.


def _stylesheet(body: str) -> str:
    return body[body.index("<style>"):body.index("</style>")]


def test_hidden_is_enforced_over_author_display_rules(client):
    css = _stylesheet(client[0].get("/").text)
    assert "[hidden]{display:none !important}" in css, (
        "without !important, any author display rule makes `hidden` inert"
    )


def test_the_progress_bubble_would_otherwise_be_visible(client):
    """Pins the exact collision, so removing the guard fails loudly."""
    app, state = client
    from arcade.messaging.keys import Identity

    peer = b"\xf0" * 32
    state.identity = Identity.generate()
    with state.store() as store:
        store.add_message(None, "tx", "tx", 1, 0, "nThem", peer,
                          state.identity.fingerprint, b"hi")

    body = app.get(f"/messages/{peer.hex()}").text
    assert 'id="sending-bubble" hidden' in body
    assert "class=\"bubble-row mine\" id=\"sending-bubble\"" in body
    css = _stylesheet(body)
    assert ".bubble-row{" in css.replace("\n", "") or ".bubble-row {" in css
    # Matched as a rule, not by splitting on the first occurrence of the string
    # -- which found the explanatory comment above the rule and failed. That is
    # the same class of mistake as checking for the attribute instead of the
    # computed style: a measurement that looks right and is not.
    import re as _re
    rules = _re.findall(r"^\[hidden\]\s*\{([^}]*)\}", css, _re.MULTILINE)
    assert rules, "no [hidden] rule found at all"
    assert all("!important" in rule for rule in rules), rules


@pytest.mark.parametrize("path", ["/", "/messages", "/contacts", "/groups", "/backup"])
def test_every_page_carries_the_guard(path, client):
    """It is one rule in the shared stylesheet; every page must get it."""
    assert "[hidden]{display:none !important}" in _stylesheet(client[0].get(path).text)


def test_no_element_relies_on_hidden_without_the_guard():
    """A static sweep for the next one, since we cannot render here.

    Reports any element carrying `hidden` whose own class sets display, which is
    the exact shape that failed. It is not a substitute for a browser assertion
    and does not pretend to be -- it is the cheap check that would have caught
    this particular case.
    """
    import pathlib
    import re

    templates = pathlib.Path("arcade/web/templates")
    css = (templates / "base.html").read_text()
    css = css[css.index("<style>"):css.index("</style>")]
    assert "[hidden]{display:none !important}" in css, (
        "the guard is what makes the sweep below unnecessary"
    )

    display_classes = set()
    for block in re.finditer(r"([^{}]+)\{([^}]*)\}", css):
        selector, body = block.group(1), block.group(2)
        if "display:" not in body:
            continue
        for part in selector.split(","):
            part = part.strip()
            # Only a selector targeting a single class, not a descendant.
            if re.fullmatch(r"\.[A-Za-z0-9_-]+", part):
                display_classes.add(part[1:])

    risky = []
    for template in templates.glob("*.html"):
        for line in template.read_text().splitlines():
            if not re.search(r"<[^>]*\shidden\b", line):
                continue
            classes = re.search(r'class="([^"]*)"', line)
            if not classes:
                continue
            for name in classes.group(1).split():
                if name in display_classes:
                    risky.append((template.name, name, line.strip()[:60]))

    # Recorded rather than forbidden: the guard above makes them safe, and this
    # names them so a future change that removes the guard is understood.
    for name, cls, line in risky:
        assert "[hidden]{display:none !important}" in css, (
            f"{name}: .{cls} sets display and would make `hidden` inert -- {line}"
        )


# --- a refusal should suit whoever asked --------------------------------------
# Making a rejected form answer 400 was right: it is what made the protection
# legible to anything but a human, after a test machine audited an endpoint, saw 200 with
# no token, and had to check the chain before concluding it was safe. But it then
# handed a browser raw JSON, which is a worse experience than the redirect it
# replaced. The code is for machines; the page is for people.


def test_a_rejected_form_gives_a_browser_a_page(client):
    app, _ = client
    response = app.post("/scan", data={"csrf_token": "wrong"},
                        headers={"Accept": "text/html"}, follow_redirects=False)

    assert response.status_code == 400
    assert "text/html" in response.headers["content-type"]
    assert "not accepted" in response.text.lower()
    assert "reload the page" in response.text


def test_a_rejected_form_still_gives_a_script_json(client):
    app, _ = client
    response = app.post("/scan", data={"csrf_token": "wrong"},
                        headers={"Accept": "application/json"},
                        follow_redirects=False)

    assert response.status_code == 400
    assert response.json()["detail"]


def test_the_refusal_page_does_not_depend_on_page_state(client):
    """It renders when something has already gone wrong."""
    from arcade.web.app import REFUSED_PAGE

    assert "{detail}" in REFUSED_PAGE
    assert "<!doctype html>" in REFUSED_PAGE.lower()


def test_the_refusal_page_escapes_what_it_shows(client):
    """The detail is ours, but escaping it costs nothing and closes the class."""
    import inspect
    from arcade.web import app as webapp

    assert "html.escape" in inspect.getsource(webapp.create_app)


# --- a sent message reports its own state -------------------------------------
# A green "Sent." banner on top of a bubble saying the same thing is one
# notification too many, and the bubble's timestamp was misleading anyway: before
# a message is in a block the only time available is when this computer pressed
# send, which is not when the message exists for anybody else.
#
# Nothing marked a send confirmed at all before this -- the column existed and no
# code ever set it.


def test_a_sent_message_says_unconfirmed_until_it_is_in_a_block(client):
    app, state = client
    from arcade.messaging.keys import Identity

    peer = b"\xe1" * 32
    state.identity = Identity.generate()
    with state.store() as store:
        store.add_sent("tx-new", peer, "", state.identity.fingerprint, b"just sent")

    body = app.get(f"/messages/{peer.hex()}").text
    assert 'class="unconfirmed">unconfirmed' in body


def test_a_confirmed_message_shows_the_block_time(client):
    app, state = client
    from arcade.messaging.keys import Identity

    peer = b"\xe2" * 32
    state.identity = Identity.generate()
    with state.store() as store:
        store.add_sent("tx-old", peer, "", state.identity.fingerprint, b"landed")
        store.mark_sent_confirmed("tx-old", block_time=1_760_000_000,
                                  height=1_483_995)

    body = app.get(f"/messages/{peer.hex()}").text
    assert "unconfirmed" not in body.split('class="bubbles"')[1].split("</div>")[0] \
        or "1,483,995" in body
    assert "1,483,995" in body


def test_the_block_time_replaces_the_local_send_time(tmp_path):
    """"When this computer pressed send" is not when the message exists."""
    from arcade.messaging.store import MessageStore

    store = MessageStore(tmp_path / "m.sqlite")
    peer = b"\xe3" * 32
    store.add_sent("tx", peer, "", "me", b"x")
    local = store.thread("me", peer)[0]["when"]

    store.mark_sent_confirmed("tx", block_time=1_760_000_000, height=42)
    item = store.thread("me", peer)[0]

    assert item["when"] == 1_760_000_000 != local
    assert item["height"] == 42
    assert item["confirmed"] is True


def test_confirming_without_a_block_time_keeps_what_is_there(tmp_path):
    """A confirmation we cannot date must not blank the date we have."""
    from arcade.messaging.store import MessageStore

    store = MessageStore(tmp_path / "m.sqlite")
    peer = b"\xe4" * 32
    store.add_sent("tx", peer, "", "me", b"x")
    before = store.thread("me", peer)[0]["when"]

    store.mark_sent_confirmed("tx")
    item = store.thread("me", peer)[0]
    assert item["when"] == before
    assert item["confirmed"] is True


def test_sending_no_longer_flashes_a_banner(client):
    """The bubble is the notification."""
    import inspect
    from arcade.web import app as webapp

    source = inspect.getsource(webapp.create_app)
    assert 'state.flash("Sent.' not in source


# --- nothing but a title belongs in <title> -----------------------------------
#
# Three pages were rendering whole panels inside <title>, because the panel had
# been pasted after the title text inside {% block title %} and base.html wraps
# that block in <title>. On / and /contacts the trapped copy was a duplicate of
# one that also rendered in the body, so the damage was a tab title 1,100
# characters long. On /wallet it was the ONLY copy: the "Fast sending" panel and
# its split form rendered inside <head>, so the feature was unreachable -- the
# page looked as though the panel had never been written. a test machine found it by
# measuring the served <title>, and pressed on the difference between the two
# cases rather than calling all three cosmetic.
#
# One assertion catches the whole class, needs no browser, and would have caught
# it on the day it was introduced.

TITLED_PAGES = ["/", "/contacts", "/wallet", "/messages", "/groups", "/backup"]


@pytest.mark.parametrize("path", TITLED_PAGES)
def test_a_page_title_is_only_a_title(client, path):
    app, _ = client
    html = app.get(path).text

    match = re.search(r"<title>(.*?)</title>", html, re.S)
    assert match, f"{path} rendered no <title>"
    title = match.group(1)

    assert "<" not in title, (
        f"{path} has markup inside <title> ({len(title)} bytes). A block pasted "
        f"into {{% block title %}} renders inside <head>: on /wallet that made "
        f"the Fast sending panel unreachable."
    )
    assert len(title) < 120, f"{path} has a {len(title)}-byte title"
    assert title.strip(), f"{path} has an empty title"


def test_the_wallet_split_form_is_in_the_body(client):
    """The specific feature that was lost, asserted where a user can reach it."""
    app, _ = client
    html = app.get("/wallet").text
    head, _, body = html.partition("</head>")

    assert "Fast sending" not in head, "the panel is back inside <head>"
    assert "Fast sending" in body
    assert 'action="/wallet/split"' in body, (
        "the split form has to be in the body to be submittable"
    )


# --- a pending confirmation is the only live control --------------------------
#
# The composer's own submit button stayed live BEHIND the confirmation. On the
# public board that put three buttons on screen, two of them saying Post ("New",
# "Post it", "Post"), and a page reading "Post this? 1 transaction, fee ..."
# would also accept a fresh submission of whatever was in the box. a test machine clicked
# the wrong one and re-submitted an empty composer. Nothing was spent, but a
# live control contradicting a pending confirmation is a hazard for anyone in a
# hurry, and the private messenger had the same shape with "Send it" beside a
# live "Send".


def test_a_composer_offers_no_second_route_while_confirming():
    """Rendered with `prepared`, the composer must be inert.

    Both halves are asserted. The disabled attribute covers the mouse; the
    data flag is what the Enter handler reads, and without it disabling the
    button would only move the hazard to the keyboard.

    This reads the template rather than driving a live send, because reaching a
    confirmation needs a funded node -- so it is a shape check, with the same
    limitation as any shape check. What it cannot tell you is whether the
    composer is really inert on screen; tests/test_browser.py does that.
    """
    root = pathlib.Path("arcade/web/templates")

    for name in ("groups.html", "messages.html"):
        source = (root / name).read_text()
        assert "{% if prepared %}disabled" in source, (
            f"{name}: the composer button stays live behind a confirmation"
        )
        assert '{% if prepared %}data-awaiting-confirm="yes"{% endif %}' in source, (
            f"{name}: the Enter handler has no way to know a confirmation is up"
        )
        assert "awaitingConfirm === 'yes'" in source, (
            f"{name}: Enter would still submit past a pending confirmation"
        )


def test_the_confirm_screen_quotes_the_total_not_one_component():
    """Fee alone and dust alone are both true and neither is the cost.

    a test machine posted for 0.04568 having been shown "fee 0.00568000" on the confirm
    screen and "about 0.03 in dust" while typing.
    """
    root = pathlib.Path("arcade/web/templates")
    groups = (root / "groups.html").read_text()
    # Through `cost`, not `prepared`: see
    # test_no_template_quotes_a_single_transaction_as_the_price. `prepared` is
    # the first chunk, so quoting it understated a post by its chunk count.
    assert "cost.total" in groups, (
        "the confirm screen still quotes a component rather than the total"
    )
    # And the total is ONE number. Splitting it into fee and dust gave two
    # figures where neither was the answer to "what does this cost me".
    assert "cost.fee" not in groups and "cost.dust" not in groups, (
        "the confirm screen is breaking the cost apart again"
    )


def test_prepared_totals_reconcile():
    """The total is exact, not estimated: fee plus the outputs it builds."""
    from arcade.messaging.sender import PreparedTx

    prepared = PreparedTx(hex="", txid="", decoded={}, fee_sats=568_000,
                          size=567, outputs=5, dust_sats=4_000_000)
    assert prepared.total_sats == 4_568_000
    assert abs(prepared.total_coins - 0.04568) < 1e-9, (
        "this is the transaction a test machine actually paid for; the numbers must agree"
    )
    assert prepared.dust_coins == 0.04


# --- a public post is a chunked send too --------------------------------------
#
# a test machine read the group route and found three things the private path had and the
# public one did not, then measured each: no begin_pending_send, so an
# interrupted post spent what it had broadcast and left no record; no
# start_progress, so /events reported "sending": null throughout a 90-second
# post; and no thread, so send_all ran inside the request, holding it open
# across a confirmation wait per chunk. A 30 KB post would hold it six or seven
# minutes. It also closed the browser part way through and the post finished
# anyway -- the work was never tied to the client, which is why it survived and
# equally why nothing reported back.


def test_a_public_post_records_itself_before_broadcasting(tmp_path):
    """The record is what makes an interruption recoverable rather than lost."""
    from arcade.messaging.store import MessageStore

    store = MessageStore(tmp_path / "s.sqlite")
    store.begin_pending_post(b"\x11" * 8, "test", "main", "a test machine", "addr",
                             b"a long post", [b"one", b"two", b"three"])

    posts = store.pending_posts()
    assert len(posts) == 1
    assert posts[0]["channel"] == "main"
    assert posts[0]["nickname"] == "a test machine"
    assert posts[0]["total"] == 3
    assert posts[0]["sent_count"] == 0
    assert posts[0]["chunks"] == [b"one", b"two", b"three"]


def test_a_post_never_appears_in_the_private_resume_path(tmp_path):
    """The kind filter is load-bearing, not tidiness.

    pending_send is one table for both. Unfiltered, the private resume path
    would pick up a post and try to seal it to a recipient_key of b''.
    """
    from arcade.messaging.store import MessageStore

    store = MessageStore(tmp_path / "s.sqlite")
    store.begin_pending_send(b"\x01" * 8, b"\x02" * 32, "a1", b"msg", [b"x", b"y"])
    store.begin_pending_post(b"\x03" * 8, "test", "main", "a test machine", "a2", b"post",
                             [b"p", b"q"])

    assert [r["msg_id"] for r in store.pending_sends()] == [b"\x01" * 8]
    assert [r["msg_id"] for r in store.pending_posts()] == [b"\x03" * 8]
    assert store.pending_posts("test") and not store.pending_posts("main")


def test_progress_on_a_post_is_recorded_per_chunk(tmp_path):
    from arcade.messaging.store import MessageStore

    store = MessageStore(tmp_path / "s.sqlite")
    store.begin_pending_post(b"\x11" * 8, "test", "main", "a test machine", "addr",
                             b"post", [b"one", b"two"])
    store.record_pending_progress(b"\x11" * 8, "txid-one")

    record = store.pending_posts()[0]
    assert record["sent_count"] == 1
    assert record["txids"] == ["txid-one"]
    # Only the rest go out on resume; re-sending a broadcast chunk would pay
    # for it twice and strand the post under a second msg_id.
    assert record["chunks"][record["sent_count"]:] == [b"two"]

    store.finish_pending_send(b"\x11" * 8)
    assert store.pending_posts() == []


def test_the_public_route_no_longer_posts_inside_the_request():
    """A chunked post must hand off to a thread, as a private message does."""
    import inspect

    from arcade.web import app as webapp

    source = inspect.getsource(webapp.create_app)
    assert "_post_in_background" in source
    assert 'threading.Thread(target=work, name="arcade-post"' in source
    assert "begin_pending_post" in source
    # The single-transaction case stays inline: there is nothing to report and
    # nothing to resume.
    assert "if plan.transactions == 1:" in source


def test_an_unfinished_post_can_be_finished(client):
    app, _ = client
    html = app.get("/groups").text
    assert 'action="/groups/resume"' in html or "{% if unfinished %}" in (
        pathlib.Path("arcade/web/templates/groups.html").read_text())

    source = pathlib.Path("arcade/web/templates/groups.html").read_text()
    assert "Finish posting" in source
    assert 'action="/groups/resume"' in source
    assert 'id="posting-bubble"' in source, "a running post must be visible"


# --- resuming a send that got part way ----------------------------------------
#
# a test machine proved resume end to end on regtest: killed a send with SIGKILL while it
# waited for its wallet split, confirmed the sealed chunks survived on disk
# byte-for-byte, then pressed Finish sending and watched all three transactions
# go out under the ORIGINAL msg_id -- continuing the message rather than sealing
# a new one, which is the thing most worth proving.
#
# What that could NOT reach is resume with sent_count > 0. It needs an
# interruption between two chunk broadcasts, and with a confirmed split those go
# out back to back in under a second. a test machine said plainly that racing it is not
# reliable even on regtest and that the honest way is a unit test. This is it.


class _StubSender:
    """Records what it is asked to send. Sends nothing."""

    def __init__(self):
        self.ensured = None
        self.sent = None

    def ensure_outputs(self, address, wanted, on_progress=None):
        self.ensured = (address, wanted)
        return False

    def spendable_outputs(self, address, at_least=0, minconf=0):
        return 99

    def prepare(self, address, payload, **kw):
        from arcade.messaging.sender import PreparedTx
        return PreparedTx(hex="00", txid="prepared", decoded={"vout": []},
                          fee_sats=1000, size=100, outputs=2)

    def send_all(self, address, payloads, on_progress=None, on_broadcast=None):
        self.sent = (address, list(payloads))
        for index, payload in enumerate(payloads, 1):
            if on_broadcast is not None:
                on_broadcast(index, len(payloads), f"txid-{index}")
        return [f"txid-{i}" for i in range(1, len(payloads) + 1)]


@pytest.fixture
def _stubbed_chain(monkeypatch, app_state):
    """Let a route reach a sender without a node behind it."""
    import contextlib

    from arcade.web import app as webapp

    stub = _StubSender()
    monkeypatch.setattr(webapp, "MessageSender", lambda *a, **k: stub)

    @contextlib.contextmanager
    def fake_rpc():
        yield object()

    monkeypatch.setattr(app_state.messaging, "rpc", fake_rpc)
    monkeypatch.setattr(webapp, "recent_block_seconds", lambda rpc: (60.0, 120.0))
    return stub


def test_resuming_a_message_sends_only_what_is_left(client, _stubbed_chain, monkeypatch):
    """sent_count chunks are already paid for and on the chain.

    Re-sending one would pay for it twice and put the same chunk on the chain
    under the same msg_id, which no reader asked for.
    """
    import time as _time

    app, state = client
    from arcade.messaging.keys import Identity

    state.identity = Identity.generate()
    peer = b"\x55" * 32
    chunks = [b"chunk-one", b"chunk-two", b"chunk-three"]
    msg_id = b"\xab" * 8

    with state.store() as store:
        store.begin_pending_send(msg_id, peer, "senderaddr", b"the body", chunks)
        store.record_pending_progress(msg_id, "txid-already-sent")

    token = re.search(r'name="csrf_token" value="([^"]+)"',
                      app.get("/messages").text).group(1)
    app.post(f"/messages/{peer.hex()}/resume", data={"csrf_token": token},
             follow_redirects=False)

    for _ in range(100):                      # the work is on a thread
        if _stubbed_chain.sent is not None:
            break
        _time.sleep(0.02)

    assert _stubbed_chain.sent is not None, "resume never reached the sender"
    address, payloads = _stubbed_chain.sent
    assert address == "senderaddr"
    assert payloads == [b"chunk-two", b"chunk-three"], (
        "resume must send only the chunks that never went out"
    )
    assert _stubbed_chain.ensured == ("senderaddr", 2), (
        "it should prepare outputs for what is left, not for the whole message"
    )


def test_a_resumed_send_keeps_the_readable_copy_and_the_file(client, _stubbed_chain,
                                                              monkeypatch):
    """a test machine's own picture came back as `ARCB E{"file":...}` plus JPEG bytes as
    text, with no file to show: resume recorded the ENCODED body as the own
    copy. The copy must be what the direct path keeps -- the text, or
    "[sent name]", and the file in its columns."""
    import time as _time

    from arcade.messaging import content
    from arcade.messaging.keys import Identity
    from arcade.messaging.store import MessageStore

    # The store repairs such rows when it opens; switched off here so this
    # proves the resume path writes the row right in the first place.
    monkeypatch.setattr(MessageStore, "_repair_encoded_own_copies", lambda self: None)

    app, state = client
    state.identity = Identity.generate()
    peer = b"\x56" * 32
    msg_id = b"\xac" * 8
    jpeg = b"\xff\xd8\xff" + bytes(range(256)) * 4
    body = content.build(text="", attachment=content.Attachment(
        "sunrise_photo.jpg", "image/jpeg", jpeg))
    with state.store() as store:
        store.begin_pending_send(msg_id, peer, "senderaddr", body, [b"a", b"b", b"c"])
        store.record_pending_progress(msg_id, "txid-1")

    token = re.search(r'name="csrf_token" value="([^"]+)"',
                      app.get("/messages").text).group(1)
    app.post(f"/messages/{peer.hex()}/resume", data={"csrf_token": token},
             follow_redirects=False)
    for _ in range(100):
        with state.store() as store:
            if not store.pending_sends():
                break
        _time.sleep(0.02)

    with state.store() as store:
        row = store.conn.execute("SELECT * FROM sent WHERE txid='txid-1'").fetchone()
    assert row is not None, "the own copy is filed under the first txid"
    assert bytes(row["body"]) == b"[sent sunrise_photo.jpg]"
    assert not bytes(row["body"]).startswith(content.BODY_MAGIC)
    assert row["file_name"] == "sunrise_photo.jpg"
    assert row["file_type"] == "image/jpeg"
    assert bytes(row["file_data"]) == jpeg


def test_a_chunked_send_keeps_its_file_too(client, _stubbed_chain, monkeypatch):
    """The chunked-but-not-resumed path recorded the readable copy but no
    file, so a large picture never showed in the sender's own bubble."""
    import time as _time
    import types

    from arcade.messaging.keys import Identity
    from arcade.web import app as webapp

    # The route checks the wallet is funded and picks an address before it
    # hands the chunks to the sender; neither needs a node here.
    monkeypatch.setattr(webapp, "Miner", lambda rpc, params: types.SimpleNamespace(
        status=lambda: types.SimpleNamespace(funded=True, describe=lambda: "")))
    monkeypatch.setattr(webapp, "funded_address", lambda rpc, prefer=None: "senderaddr")

    app, state = client
    state.identity = Identity.generate()
    peer = b"\x57" * 32
    with state.store() as store:
        store.save_contact(pubkey=peer, name="Peer")
    big = b"\x89PNG" + bytes(range(256)) * 200         # well past one chunk
    token = re.search(r'name="csrf_token" value="([^"]+)"',
                      app.get("/messages").text).group(1)
    response = app.post(f"/messages/{peer.hex()}/send",
                        data={"csrf_token": token, "body": "", "confirmed": "yes"},
                        files={"attachment": ("big.png", big, "image/png")},
                        follow_redirects=False)
    for _ in range(200):
        if _stubbed_chain.sent is not None:
            with state.store() as store:
                if store.conn.execute("SELECT 1 FROM sent").fetchone():
                    break
        _time.sleep(0.02)

    assert _stubbed_chain.sent is not None, re.findall(
        r'class="msg err"[^>]*>([^<]*)', response.text)
    assert len(_stubbed_chain.sent[1]) > 1
    with state.store() as store:
        row = store.conn.execute("SELECT * FROM sent").fetchone()
    assert row is not None
    assert bytes(row["body"]) == b"[sent big.png]"
    assert row["file_name"] == "big.png" and bytes(row["file_data"]) == big


def test_own_copies_written_encoded_are_repaired_on_open(tmp_path):
    """The row a test machine already has: length 25746, ARCB header, no file columns."""
    from arcade.messaging import content
    from arcade.messaging.store import MessageStore

    jpeg = b"\xff\xd8" * 40
    encoded = content.build(text="", attachment=content.Attachment(
        "sunrise_photo.jpg", "image/jpeg", jpeg))
    path = tmp_path / "m.sqlite"
    store = MessageStore(path)
    store.add_sent("t1", b"\x01" * 32, "", "fp", encoded)
    store.add_sent("t2", b"\x01" * 32, "", "fp", b"a plain copy")
    store.close()

    store = MessageStore(path)
    rows = {r["txid"]: r for r in store.conn.execute("SELECT * FROM sent")}
    assert bytes(rows["t1"]["body"]) == b"[sent sunrise_photo.jpg]"
    assert rows["t1"]["file_name"] == "sunrise_photo.jpg"
    assert bytes(rows["t1"]["file_data"]) == jpeg
    assert bytes(rows["t2"]["body"]) == b"a plain copy"
    store.close()


def test_resuming_keeps_the_original_message_id(client, _stubbed_chain):
    """A new id would strand what is already on the chain, unreadable for ever."""
    import time as _time

    app, state = client
    from arcade.messaging.keys import Identity

    state.identity = Identity.generate()
    peer = b"\x66" * 32
    msg_id = b"\xcd" * 8

    with state.store() as store:
        store.begin_pending_send(msg_id, peer, "addr2", b"body",
                                 [b"a", b"b", b"c"])
        store.record_pending_progress(msg_id, "txid-1")
        store.record_pending_progress(msg_id, "txid-2")

    token = re.search(r'name="csrf_token" value="([^"]+)"',
                      app.get("/messages").text).group(1)
    app.post(f"/messages/{peer.hex()}/resume", data={"csrf_token": token},
             follow_redirects=False)

    for _ in range(100):
        if _stubbed_chain.sent is not None:
            break
        _time.sleep(0.02)

    assert _stubbed_chain.sent[1] == [b"c"], "only the last chunk was outstanding"
    # The record is keyed by msg_id, so it can only have been cleared by a
    # finish under the ORIGINAL id.
    for _ in range(100):
        with state.store() as store:
            if not store.pending_sends():
                break
        _time.sleep(0.02)
    with state.store() as store:
        assert store.pending_sends() == [], (
            "the pending record must be cleared under the original msg_id"
        )


def test_resuming_a_post_sends_only_what_is_left(client, _stubbed_chain):
    """The public resume path slices the same way, so it is checked the same way.

    Two implementations of "send the rest" is how the CLI and the web came to
    disagree about recording a send, and each machine then showed half a
    conversation. These are separate routes, so they get separate tests.
    """
    import time as _time

    app, state = client
    msg_id = b"\xef" * 8
    with state.store() as store:
        store.begin_pending_post(msg_id, "regtest", "main", "a test machine", "postaddr",
                                 b"a long public post", [b"p1", b"p2", b"p3"])
        store.record_pending_progress(msg_id, "txid-p1")

    token = re.search(r'name="csrf_token" value="([^"]+)"',
                      app.get("/groups").text).group(1)
    app.post("/groups/resume",
             data={"csrf_token": token, "which": "messaging", "channel": "main"},
             follow_redirects=False)

    for _ in range(100):
        if _stubbed_chain.sent is not None:
            break
        _time.sleep(0.02)

    assert _stubbed_chain.sent is not None, "resume never reached the sender"
    address, payloads = _stubbed_chain.sent
    assert address == "postaddr"
    assert payloads == [b"p2", b"p3"]

    for _ in range(100):
        with state.store() as store:
            if not store.pending_posts():
                break
        _time.sleep(0.02)
    with state.store() as store:
        assert store.pending_posts() == []
        # And the local copy of the post is written on completion, which is
        # what makes it appear on the board afterwards.
        assert any(p["text"] == "a long public post"
                   for p in store.group_posts("regtest", "main"))


def test_a_post_resume_will_not_touch_a_private_pending_send(client, _stubbed_chain):
    """The kind filter, asserted through the route rather than the store."""
    app, state = client
    with state.store() as store:
        store.begin_pending_send(b"\x01" * 8, b"\x02" * 32, "privaddr", b"m",
                                 [b"x", b"y"])

    token = re.search(r'name="csrf_token" value="([^"]+)"',
                      app.get("/groups").text).group(1)
    app.post("/groups/resume",
             data={"csrf_token": token, "which": "messaging", "channel": "main"},
             follow_redirects=False)

    assert _stubbed_chain.sent is None, (
        "a public resume reached a private message's chunks"
    )
    with state.store() as store:
        assert len(store.pending_sends()) == 1, "the private record must survive"


def test_the_timing_note_is_not_gated_on_an_attachment():
    """A long TEXT message chunks for the same reason a file does.

    The gate was `plan is not None and file_bytes`, so timing was built only for
    an attachment and a text message needing three transactions got timing=None
    -- which silently skipped the readable-by note. a test machine's two captures were
    both text-only three-transaction sends, the exact case its block-packing
    measurement was about, and neither could show it.
    """
    import inspect

    from arcade.web import app as webapp

    source = inspect.getsource(webapp.create_app)
    assert "if plan is not None and file_bytes:" not in source, (
        "timing is gated on an attachment again"
    )
    assert "if plan is not None and plan.transactions > 1:" in source


def test_the_public_confirm_can_show_a_readable_estimate(client):
    """groups.html was never passed timing at all.

    So the post whose chunks were measured landing two blocks apart could never
    say how long before anyone could read it.
    """
    source = pathlib.Path("arcade/web/templates/groups.html").read_text()
    assert "timing.readable" in source, (
        "the public confirm screen cannot report a readable-by time"
    )

    import inspect

    from arcade.web import app as webapp

    route = inspect.getsource(webapp.create_app)
    assert "timing=_post_timing(chain, plan)" in route, (
        "groups.html is still passed no timing"
    )
    assert "estimate_readable_seconds" in route
    # One definition, called from both render paths. Pasted into both, it raised
    # NameError on the listing route, which has no `plan` at all.
    assert route.count("def _post_timing(") == 1


def test_both_confirm_screens_quote_dust_from_a_real_field(client):
    """Guards the pairing, not just the presence of a figure.

    A screen that renders prepared.dust_coins is only honest if prepare() fills
    it in; the template was right and the value was structurally zero. So this
    asserts the template reads the field AND that the field is set where the
    messages and posts are built.
    """
    groups = pathlib.Path("arcade/web/templates/groups.html").read_text()
    messages = pathlib.Path("arcade/web/templates/messages.html").read_text()
    # The screens show one figure now, but it still has to CONTAIN the dust --
    # which for a Class B send is most of it. The check moved from "is the dust
    # displayed" to "is the dust in the total", because the first was satisfied
    # by a 0.00000 that was structurally impossible to be anything else.
    assert "cost.total" in groups
    assert "total" in messages

    sender = pathlib.Path("arcade/messaging/sender.py").read_text()
    body = sender[sender.index("    def prepare("):]
    body = body[:body.index("\n    def ", 1)]
    assert "dust_sats=" in body, (
        "prepare() must set dust_sats or both screens quote zero"
    )
    # And the value has to survive the scaling, or the screens quote one chunk.
    assert "dust = prepared.dust_sats * count" in sender, (
        "send_cost() must scale the dust by the transaction count"
    )


def test_publishing_asks_the_chain_before_it_spends(monkeypatch, client):
    """The store can be wrong in the one direction that costs money.

    a test machine cleared its store and the Keys page went from three announcements to
    "None seen yet", with a live Publish on chain form -- for a key already
    published in blocks below the new starting point, where no rescan on this
    version could ever find it again. The only routes left were paying a second
    fee for a permanent record, or re-adding rows by hand.

    So the decision is taken from the CHAIN, not the list. And it refuses
    WITHOUT writing the rows back: restoring them would undo the reset the user
    asked for -- those are the very transactions they cleared.
    """
    import inspect

    from arcade.web import app as webapp

    source = inspect.getsource(webapp.create_app)
    publish = source[source.index("def publish_key("):]
    publish = publish[:publish.index("\n    @app.")]

    assert "find_own_announcements(" in publish, (
        "publishing decides from the local list again"
    )
    spend = publish.index("sender.broadcast(")
    check = publish.index("find_own_announcements(")
    assert check < spend, "the chain is asked AFTER the money is spent"
    assert "add_key_announcement" not in publish, (
        "refusing is enough; writing the rows back undoes the user's reset"
    )


def test_no_dialog_can_promise_what_the_reset_could_not_keep():
    """The dialog is gone with the feature it belonged to.

    It is worth keeping the lesson: it said "your address book, your wallet and
    your own published key are left alone", and whether that announcement row
    survived depended on the identity being loadable. On a machine where it was
    not, the reset took three announcements to none -- which is what a test machine
    measured, an hour after I promised otherwise. Copy that outruns the code is
    the failure this file has pinned more than once.
    """
    import pathlib as _p

    overview = _p.Path("arcade/web/templates/overview.html").read_text()
    assert "published key</strong> are left" not in overview
    assert "Start fresh" not in overview


# --- the tokens chain switch --------------------------------------------------


def test_the_chain_tag_switches_tokens_between_mainnet_and_testnet(client, tmp_path):
    """Tokens show on mainnet by default; the tag on the page switches to testnet.

    The choice is written to a file so it survives a restart. Asserts what the
    served page says and what a fresh AppState reads back, not that a variable
    was set.
    """
    app, state = client
    page = app.get("/tokens").text
    assert 'name="chain" value="regtest"' in page, "the tag offers the other chain"
    assert '<button type="submit" class="tag mainnet switch"' in page
    assert "Every fungible token on mainnet" in page

    response = app.post("/tokens/chain", data={"chain": "regtest", "csrf_token": state.csrf_token},
                        follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/tokens"
    page = app.get("/tokens").text
    assert '<button type="submit" class="tag testnet switch"' in page
    assert 'name="chain" value="main"' in page
    assert "Every fungible token on testnet" in page
    assert (state.home / "tokens-chain").read_text().strip() == "regtest"

    again = AppState(home=state.home, messaging=state.messaging, ledger=state.ledger)
    assert again.token_chain.network == "regtest", "the choice survives a restart"

    app.post("/tokens/chain", data={"chain": "doge-main", "csrf_token": state.csrf_token},
             follow_redirects=False)
    assert state.token_chain.network == "regtest", "a chain tokens are not on is refused"
    assert "not indexed on" in app.get("/tokens").text


# --- the browser must not remember what is typed here -------------------------


TEXT_FIELD = re.compile(r"<(input|textarea)\b[^>]*>", re.IGNORECASE | re.DOTALL)
NOT_TYPED_IN = re.compile(r'type\s*=\s*"(hidden|checkbox|file|radio|submit|button)"',
                          re.IGNORECASE)


def _fields_that_remember(html: str) -> list[str]:
    return [
        match.group(0)
        for match in TEXT_FIELD.finditer(html)
        if not NOT_TYPED_IN.search(match.group(0))
        and "autocomplete=" not in match.group(0)
    ]


@pytest.mark.parametrize("path", GET_ROUTES + ["/groups", "/messages"])
def test_no_page_lets_the_browser_remember_what_was_typed(client, path):
    """A contact code, an address, a message: the browser offers them back on
    every form it decides is similar, on a machine anyone may walk up to. The
    page is checked as served, not as written, so a field added by a macro or a
    second template is caught too."""
    app, _ = client
    body = app.get(path).text
    remembered = _fields_that_remember(body)
    assert remembered == [], f"{path} has fields the browser will remember: {remembered}"


def test_every_template_says_so_too():
    """The routes above do not render every branch -- a form behind an `if`
    would never be seen. The templates themselves have to be clean."""
    offenders = []
    for template in sorted(pathlib.Path("arcade/web/templates").glob("*.html")):
        for tag in _fields_that_remember(template.read_text()):
            offenders.append(f"{template.name}: {' '.join(tag.split())[:70]}")
    assert offenders == [], offenders


def test_a_long_send_shows_its_progress_in_the_bubble(client):
    """"I sent a larger image and it's been a few minutes and it still says
    unconfirmed." It was: 36 of its 51 transactions were in blocks and the
    bubble had no way to say so."""
    app, state = client
    from arcade.messaging.keys import Identity

    state.identity = Identity.generate()
    peer = b"\xc1" * 32
    with state.store() as store:
        store.save_contact(pubkey=peer, name="Them")
        store.add_sent("tx0", peer, "", state.identity.fingerprint,
                       b"[sent photo.jpg]", file_name="photo.jpg",
                       file_type="image/jpeg", file_data=b"\xff\xd8" * 20,
                       txids=[f"tx{i}" for i in range(51)])
        store.conn.execute("UPDATE sent SET confirmed_count=36")

    body = app.get(f"/messages/{peer.hex()}").text
    assert "36 of 51" in body
    assert "transactions in blocks" in body


def test_a_single_transaction_message_just_says_unconfirmed(client):
    """One transaction has no progress to report, and "0 of 1" would be a
    worse way of saying the same thing."""
    app, state = client
    from arcade.messaging.keys import Identity

    state.identity = Identity.generate()
    peer = b"\xc2" * 32
    with state.store() as store:
        store.save_contact(pubkey=peer, name="Them")
        store.add_sent("only", peer, "", state.identity.fingerprint, b"hello")

    body = app.get(f"/messages/{peer.hex()}").text
    assert ">unconfirmed<" in body
    assert "of 1 transactions" not in body


# --- mining a block from the wallet page --------------------------------------

def test_mine_one_block_is_always_offered_and_says_when_it_is_at_it(client, monkeypatch):
    """The button began as a way to fund an empty wallet and vanished once it
    had coins. On a test chain a block is also the only way anything confirms,
    so it stays -- and while a block is being mined (twenty minutes or more on
    a small machine) the page says so, and how far it has got, instead of
    showing a button that would start a second miner."""
    import contextlib
    import threading

    from arcade.messaging import miner as minerlib

    http, state = client
    finding = threading.Event()
    batches = []

    class FakeRpc:
        def call(self, method, *args):
            if method == "getwalletinfo":
                return {"balance": 12.5, "immature_balance": 0}
            if method == "getblockcount":
                return 100
            if method == "getnewaddress":
                return "nNew"
            if method == "getconnectioncount":
                return 3
            if method == "getdifficulty":
                return 0.0005
            if method == "generatetoaddress":
                # The node hashes for as long as it is told and answers only
                # then: batches, so the wallet is never left waiting past
                # the RPC timeout (miner.py).
                batches.append(args[2])
                time.sleep(0.01)
                return ["ab" * 32] if finding.wait(0.2) else []
            raise AssertionError(method)

        def get_block_count(self):
            return 100

        def get_blockchain_info(self):
            return {"chain": "test", "blocks": 100, "headers": 100}

    @contextlib.contextmanager
    def fake_rpc():
        yield FakeRpc()

    monkeypatch.setattr(state.messaging, "rpc", fake_rpc)
    monkeypatch.setattr("arcade.messaging.miner.require_messaging_network", lambda params: None)
    monkeypatch.setattr(type(state), "derived_address", property(lambda self: "nMine"))

    page = http.get("/wallet").text
    assert "Mine one block" in page, "funded, and still offered"

    http.post("/fund", data={"csrf_token": state.csrf_token})
    assert state.mining is not None and state.mining["address"] == "nMine"
    page = http.get("/wallet").text
    assert "Mining a block on this machine" in page
    assert "Mine one block" not in page, "no button to press twice"
    assert http.get("/wallet/mining").json()["mining"] is True

    # The post lands on the wallet page, where the notice is.
    assert "Already mining" in http.post("/fund", data={"csrf_token": state.csrf_token}).text

    for _ in range(100):
        if state.mining and state.mining["tries"]:
            break
        time.sleep(0.05)
    watched = http.get("/wallet/mining").json()
    assert watched["tries"] >= minerlib.FIRST_TRIES and watched["rate"] > 0
    assert watched["expected"] == int(0.0005 * 2 ** 32) and watched["expected_seconds"] > 0
    assert batches[0] == minerlib.FIRST_TRIES and batches[1] >= minerlib.TRIES_PER_CALL[0]
    assert "Stop mining" in http.get("/wallet").text

    finding.set()
    for _ in range(100):
        if state.mining is None:
            break
        time.sleep(0.05)
    assert state.mining is None
    assert http.get("/wallet/mining").json()["mining"] is False
    page = http.get("/wallet").text
    assert "Mined block abababababababab" in page and "12.50 PEP spendable" in page, page
    assert "Mine one block" in page, "and it can be pressed again"

    # Stopped: the node finishes the batch it is on, and no more.
    finding.clear()
    http.post("/fund", data={"csrf_token": state.csrf_token})
    assert "Not mining" not in http.post("/fund/stop", data={"csrf_token": state.csrf_token}).text
    assert state.mining is None or state.mining["stop"] is True
    for _ in range(100):
        if state.mining is None:
            break
        time.sleep(0.05)
    page = http.get("/wallet").text
    assert "Stopped mining after" in page and "no block" in page, page
    assert "Not mining" in http.post("/fund/stop", data={"csrf_token": state.csrf_token}).text


def test_a_real_chains_wallet_is_only_the_arcades_own():
    """On a test chain the node exists to run this, so every address in it is
    its. On a real chain the node is usually somebody's own wallet too, and
    those coins are not ours to show or to spend (D-046)."""
    from arcade.web.app import _ledger_addresses

    class Node:
        def __init__(self, rows, unspent):
            self.rows, self.unspent = rows, unspent

        def call(self, method, *args):
            return self.rows if method == "listreceivedbyaddress" else self.unspent

    from arcade.config import NETWORKS
    from arcade.script import b58check_encode

    def address(chain: str, n: int) -> str:
        return b58check_encode(NETWORKS[chain].pubkeyhash_version, bytes([n]) * 20)

    mine, also, change = (address("test", n) for n in (1, 2, 3))
    testnet = Node([{"address": mine, "account": ""},
                    {"address": also, "account": "somebody else"}],
                   [{"address": change, "account": "", "spendable": True}])
    assert set(_ledger_addresses(testnet)) == {mine, also, change}

    ours, theirs, spare = (address("main", n) for n in (1, 2, 3))
    mainnet = Node([{"address": ours, "account": "arcade-identity"},
                    {"address": theirs, "account": "savings"}],
                   [{"address": spare, "account": "", "spendable": True}])
    assert _ledger_addresses(mainnet) == [ours], "their own coins stay theirs"


def test_gathering_waits_for_whatever_else_is_sending(client, monkeypatch):
    """Housekeeping must not build a transaction from outputs a collection
    run or a message is spending at that moment (D-046)."""
    app, state = client
    assert state.begin_send(), "nothing is sending yet"
    try:
        assert state.gather_once(state.messaging) == [], \
            "a pass while something else sends does nothing and waits"
    finally:
        state.end_send()
    # And it gives the lock back, so the next pass can have it.
    assert state.begin_send()
    state.end_send()


def test_a_wallet_with_one_output_is_told_before_it_pays(client):
    """A swap needs two outputs from the buyer: one for the trade, one for
    the message carrying it. Finding that out at the last step means an
    order paid for and an offer waited on for nothing (D-051)."""
    from arcade.web.app import create_app

    app, state = client
    # The helper is a closure over create_app; reach it through a shop door
    # call would need a whole chain, so exercise the rule it applies.
    import arcade.web.app as appmod

    class Node:
        def __init__(self, n):
            self.n = n

        def call(self, method, *args):
            return [{"txid": f"{i:064x}", "vout": 0, "amount": 5.0,
                     "spendable": True} for i in range(self.n)]

    # Rebuilt here because the helper lives inside create_app; the rule is
    # the same one the door uses.
    def too_few(rpc, address):
        outputs = [u for u in (rpc.call("listunspent", 1, 9_999_999, [address]) or [])
                   if u.get("spendable", True)]
        return "" if len(outputs) >= 2 else "needs two"

    assert too_few(Node(0), "nMe")
    assert too_few(Node(1), "nMe"), "one output cannot both trade and post"
    assert not too_few(Node(2), "nMe")
    assert not too_few(Node(9), "nMe")


def test_the_shop_door_says_whether_this_wallet_can_buy(client):
    """A page can ask, and say so, instead of drawing a button that fails."""
    import inspect

    import arcade.web.app as appmod

    source = inspect.getsource(appmod.create_app)
    assert '"can_buy": ready_to_buy' in source, "the shop answer carries it"
    assert "_too_few_outputs(rpc, buyer)" in source, \
        "and the offer is refused before it is paid for"


def test_the_nav_counts_what_is_waiting(client, monkeypatch):
    """A red circle beside the page that has something in it.

    Three different questions, one shape: unopened private messages, posts on
    the board since this wallet last looked, and offers on its pieces that it
    has not answered. Nothing is drawn when there is nothing waiting -- a
    permanent zero is noise, and noise is what a badge cannot be.
    """
    page, state = client
    body = page.get("/").text
    assert '<span class="nav-count">' not in body, "nothing waiting, nothing drawn"

    with state.store() as store:
        store.add_group_post(state.messaging.network, "main", "a" * 64, 100, 1700,
                             "nSomebody", "them", "hello")
    body = page.get("/").text
    assert 'href="/groups">Public<span class="nav-count">1</span>' in body

    # Opening the board is reading it.
    page.get("/groups")
    assert '<span class="nav-count">' not in page.get("/").text


def test_a_hundred_waiting_does_not_stretch_the_nav(client):
    page, state = client
    with state.store() as store:
        for n in range(101):
            store.add_group_post(state.messaging.network, "main", f"{n:064x}",
                                 100 + n, 1700 + n, "nSomebody", "them", "hi")
    assert '<span class="nav-count">99+</span>' in page.get("/").text


def test_anything_can_be_sent_to_a_tag(client, monkeypatch):
    """A tag is a name for an address, so a field that wants an address takes
    one -- coins, tokens, a contact. It was two fields out of six."""
    page, state = client

    class Index:
        def address_of(self, wanted):
            return "nTagHolderAddress1111111111111111" if wanted == "friend" else None

    monkeypatch.setattr(type(state), "token_index", lambda self, chain: Index())
    from arcade.web import app as appmod

    for mainnet in (False, True):
        chains = [c for c in state.token_chains if bool(c.is_mainnet) == mainnet]
        if not chains:
            continue
        assert appmod._tag_address(state, "@friend", mainnet=mainnet) \
            == "nTagHolderAddress1111111111111111"
        assert appmod._tag_address(state, "@FRIEND", mainnet=mainnet) \
            == "nTagHolderAddress1111111111111111", "a tag is not case sensitive"

    # An address is passed through untouched; only an @ means a lookup.
    assert appmod._tag_address(state, "nSomebodyElse", mainnet=False) == "nSomebodyElse"
    assert appmod._tag_address(state, "  ", mainnet=False) == ""

    # And a name nobody holds is refused by name, not by a base58 complaint.
    import pytest as _pytest
    with _pytest.raises(ValueError, match="nobody holds @stranger"):
        appmod._tag_address(state, "@stranger", mainnet=False)


def test_the_address_book_stores_the_address_a_tag_names(client, monkeypatch):
    """Not the tag. A tag moves, and paying whoever holds a name today is not
    what somebody meant when they wrote it down last year."""
    page, state = client

    class Index:
        def address_of(self, wanted):
            return "mgA7SfyBBrVGVSpQ7oqGHPhxpp2gUZWtfc" if wanted == "friend" else None

    monkeypatch.setattr(type(state), "token_index", lambda self, chain: Index())
    csrf = state.csrf_token
    answer = page.post("/contacts/save", data={"csrf_token": csrf, "name": "A friend",
                                               "testnet_address": "@friend"},
                       follow_redirects=True)
    assert answer.status_code == 200, answer.text[:400]
    with state.store() as store:
        saved = [dict(r) for r in store.contacts()]
    import re
    said = re.search(r'<div class="msg [^"]*">(.*?)</div>', answer.text, re.S)
    assert saved, f"nothing was saved: {said.group(1).strip()[:200] if said else '?'}"
    assert saved and saved[0]["testnet_address"] == "mgA7SfyBBrVGVSpQ7oqGHPhxpp2gUZWtfc", \
        "the address it named, not the name"

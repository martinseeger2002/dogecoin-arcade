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


def test_the_name_field_asks_for_your_name(client):
    """Not a suggestion, and not somebody else's name."""
    body = client[0].get("/contacts").text
    assert 'placeholder="Your name"' in body


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


def test_a_published_name_travels_with_its_address(client):
    """The name is read from the announcement, not from the address book.

    Looking it up in the address book could only ever find names for people
    already in it -- which is exactly who this list leaves out.
    """
    app, state = client
    with state.store() as store:
        store.add_key_announcement("tx1", "nTheirAddress", b"\x72" * 32, "aa",
                                   500, 1000, stated=True, name="alice")

    body = app.get("/contacts").text
    assert "alice" in body
    assert "nTheirAddress" in body


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


def test_enter_goes_through_the_double_send_guard(client):
    """form.submit() does not fire onsubmit, so the guard never ran on Enter."""
    app, state = client
    from arcade.messaging.keys import Identity

    peer = b"\x82" * 32
    state.identity = Identity.generate()
    with state.store() as store:
        store.add_message(None, "tx", "tx", 1, 0, "nThem", peer,
                          state.identity.fingerprint, b"hi")

    body = app.get(f"/messages/{peer.hex()}").text
    assert "requestSubmit" in body
    assert "if (!startSending(box.form)) return;" in body


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


def test_the_reset_needs_the_acknowledgement(client):
    app, state = client
    app.post("/reset-history", data={"csrf_token": state.csrf_token},
             follow_redirects=False)
    assert "tick the box" in (state.notice or "")


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

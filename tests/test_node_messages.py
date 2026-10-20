"""The operator's Messages is the accounts' Messenger over the node's own opened messages (2026-10-19,
node-mail.js): both sides of a conversation as letters, and reading one marks it read."""

from test_web import client, app_state  # noqa: F401  (fixture re-export)

ADMIN = {"X-Arcade-Admin": "1"}


def test_the_node_hands_its_page_both_sides_as_letters(client):
    app, state = client
    from arcade.messaging.keys import Identity
    state.identity = Identity.generate()
    peer = b"\x66" * 32
    with state.store() as store:
        store.add_message(None, "tx-in", "tx-in", 1, 100, "nThem", peer, state.identity.fingerprint, b"hello node")
        store.add_sent("tx-out", peer, "nThem", state.identity.fingerprint, b"hello back")
    page = app.get("/messages").text
    assert '"/node-mail.js"' in page and 'href="/messages/more"' in page
    letters = app.get("/node/mail/letters").json()["letters"]
    assert {(l["mine"], bytes.fromhex(l["body"])) for l in letters} == {(False, b"hello node"), (True, b"hello back")}
    assert all(l["peer"] == peer.hex() for l in letters)
    assert next(l for l in letters if not l["mine"])["unread"] is True
    assert app.post("/node/mail/read", json={"peer": peer.hex()}, headers=ADMIN).status_code == 200
    assert next(l for l in app.get("/node/mail/letters").json()["letters"] if not l["mine"])["unread"] is False

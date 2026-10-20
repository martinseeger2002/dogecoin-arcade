"""The operator's Address book is the accounts' page over the node's contacts table (2026-10-19, node-mail.js)."""

from test_web import client, app_state  # noqa: F401  (fixture re-export)

ADMIN = {"X-Arcade-Admin": "1"}


def test_the_node_book_reads_and_edits_like_an_accounts(client):
    app, state = client
    page = app.get("/contacts").text
    assert '"/node-mail.js"' in page and "Address book" in page
    assert app.get("/contacts/more").status_code == 200, "the old page is kept"
    with state.store() as store:
        store.save_contact(pubkey=b"\x21" * 32, name="robin", testnet_address="nRobinAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    book = app.get("/node/contacts").json()["book"]
    assert [e["tag"] for e in book] == ["robin"] and book[0]["key"] == "21" * 32
    said = app.post("/node/contacts/update", json={"tag": "robin", "fav": True, "note": "met at the arcade"},
                    headers=ADMIN).json()
    assert said["fav"] is True and said["note"] == "met at the arcade"
    assert app.post("/node/contacts/remove", json={"tag": "@robin"}, headers=ADMIN).status_code == 200
    assert app.get("/node/contacts").json()["book"] == []

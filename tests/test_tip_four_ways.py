"""Tip in four ways, on the feed and on the Games tab (2026-10-13: "When a
user selects tip on the feed, it should give them the options of testnet PEP
mainnet PEP and testnet or mainnet token", then "There should be also added to
the Games tab for tipping")."""

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_account_tokens import TEST, SOMEBODY                   # noqa: E402
from test_me_page import _seat, LOCAL                            # noqa: E402

COIN = 100_000_000
TIPPER_MAIN, AUTHOR_MAIN = "PTipperMainAddressXXXXXXXXXXXXXXXX", "PAuthorMainAddressXXXXXXXXXXXXXXXX"


def _published(state, address, other, n):
    with state.store() as store:
        store.add_key_announcement(f"{n:064x}", address, bytes([n]) * 32, f"fp{n}",
                                   10, 100, stated=True, other_address=other)


def test_the_tip_picker_offers_both_coins_and_both_kinds_of_token(client):
    """2026-10-13: "The tip picker should start with a network picker and then have
    a coin or token picker and then a token if token is selected"."""
    app, state = client
    _seat(app)
    # The template helpers are bound per app on a module-level environment; bind
    # them to THIS state before rendering outside a request (parallel runs).
    from arcade.web.app import TEMPLATES, create_app
    create_app(state)
    html = TEMPLATES.env.from_string('{% import "_tip.html" as t with context %}{{ t.tip_pay() }}').render()
    nets = re.search(r'<select name="net"[^>]*>(.*?)</select>', html, re.S).group(1)
    whats = re.search(r'<select name="what"[^>]*>(.*?)</select>', html, re.S).group(1)
    assert re.findall(r'<option value="([^"]+)"', nets) == [state.messaging.network, state.ledger.network]
    assert [" ".join(x.split()) for x in re.findall(r">([^<]+)</option>", nets)] == ["Pepecoin (PEP) \u2013 Test", "Pepecoin (PEP) \u2013 Main"]
    assert re.findall(r'<option value="([^"]+)"', whats) == ["coin", "token"]
    assert ">PEP TEST</option>" in whats and ">a token</option>" in whats
    assert '<select name="token"' in html and "hidden" in html.split('<select name="token"')[1][:80]


def test_feed_and_games_draw_the_same_picker(client):
    root = pathlib.Path(__file__).resolve().parent.parent / "arcade" / "web" / "templates"
    for page in ("feed.html", "games.html"):
        text = (root / page).read_text()
        assert "tipui.tip_pay()" in text and "_tip.html\" as tipui with context" in text and "wallet.tip(held, form, txid, KINDS, arcadeAsk)" in text, page
        assert "wallet.tipPicker()" in text, page
    shared = (root / "wallet_js.js").read_text()
    assert "export async function tip(" in shared and "tip_of: txid" in shared


def test_a_mainnet_tip_to_somebody_with_no_mainnet_address_is_refused(client):
    from test_feed_web import a_post
    from arcade.messaging import feed
    app, state = client
    _seat(app)
    post = "a2" * 32
    a_post(state, post, text="tip me on mainnet", sender=SOMEBODY)
    said = app.post("/account/react", headers=LOCAL, json={
        "txid": post, "kind": feed.TIP, "amount": "1", "chain": state.ledger.network})
    assert said.status_code == 400, said.text
    assert "mainnet" in said.json()["detail"].lower(), said.text
    wrong = app.post("/account/react", headers=LOCAL, json={
        "txid": post, "kind": feed.TIP, "amount": "1", "chain": "nochain"})
    assert wrong.status_code == 400 and "does not run" in wrong.json()["detail"], wrong.text


def test_a_mainnet_token_tip_counts_at_the_addresses_they_published(client):
    """The note is on the tag chain; the send is on mainnet, between the two
    people's published mainnet addresses. It is named "(mainnet)" so it never
    adds up with a testnet token of the same name."""
    from test_feed_web import a_post, an_act
    from arcade import payload as P
    from arcade.messaging import feed

    app, state = client
    _published(state, TEST, TIPPER_MAIN, 1)
    _published(state, SOMEBODY, AUTHOR_MAIN, 2)
    index = state.token_index(state.ledger)
    post, real, wrong = "a3" * 32, "b3" * 32, "c3" * 32
    a_post(state, post, text="tip me a real token", sender=SOMEBODY)
    with index.open() as db:
        db.conn.execute(
            "INSERT OR REPLACE INTO property(property_id, ecosystem, property_type, issuer, name, "
            "total_tokens, creation_txid, creation_block) VALUES(?,?,?,?,?,?,?,?)",
            (77, 1, 2, TIPPER_MAIN, "Realcoin", 1_000_000 * COIN, "aa" * 32, 1))
        for txid, sender in ((real, TIPPER_MAIN), (wrong, "PSomebodyElseXXXXXXXXXXXXXXXXXXXXX")):
            db.conn.execute(
                "INSERT OR REPLACE INTO arcade_tx(txid, block_height, position, encoding_class, "
                "message_type, message_version, sender, reference, payload_hex, valid) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (txid, 10, 0, "C", 0, 0, sender, AUTHOR_MAIN,
                 P.SimpleSend(property_id=77, amount=3 * COIN).encode().hex(), 1))
        db.conn.commit()
    an_act(state, "d3" * 32, feed.TIP_TOKEN, post, author=TEST, text=real)
    an_act(state, "e3" * 32, feed.TIP_TOKEN, post, author=TEST, text=wrong)
    page = " ".join(app.get("/feed").text.split())
    assert "3 Realcoin (mainnet)" in page, "the mainnet send from the tipper's published address counts"
    assert "6 Realcoin" not in page, "a send from somebody else does not"

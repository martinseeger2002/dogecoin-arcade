"""A trade's own page (2026-10-17: "The recent trades should be clickable and show more
information like who was on both sides of the trade")."""
import contextlib
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from test_web import app_state, client                       # noqa: F401,E402

SELLER, BUYER, MAKER2 = "n" + "S" * 33, "n" + "B" * 33, "n" + "M" * 33
TAKE, MATCH = "a" * 64, "b" * 64


def _index(state):
    chain = state.token_chains[0]
    return chain, state.token_index(chain)


def _write(index):
    with contextlib.closing(index.open()) as db:
        for txid, sender in ((TAKE, BUYER), (MATCH, BUYER)):
            db.conn.execute("INSERT INTO arcade_tx (txid, block_height, position, encoding_class, sender,"
                            " payload_hex, valid) VALUES (?,?,?,?,?,?,1)", (txid, 120, 1, "C", sender, "00"))
        db.conn.execute("INSERT INTO take_trade (txid, property_id, tokens, coins, maker) VALUES (?,?,?,?,?)",
                        (TAKE, 14, 70 * 10 ** 8, 70 * 10 ** 6, SELLER))
        for seq, maker in ((0, SELLER), (1, MAKER2)):
            db.conn.execute("INSERT INTO pair_trade (txid, seq, block_height, position, taker, maker, maker_txid,"
                            " gave_property, gave, got_property, got) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (MATCH, seq, 120, 1, BUYER, maker, "c" * 64, 9, 5 * 10 ** 8, 14, 10 * 10 ** 8))
        db.conn.commit()


def test_a_take_names_its_seller_and_buyer(client):
    app, state = client
    chain, index = _index(state)
    _write(index)
    page = app.get(f"/exchange/trade/{TAKE}?chain={chain.network}").text
    assert "Taken from the book" in page
    assert SELLER in page and BUYER in page, "both sides of it"
    assert "gave 70 token #14" in page and f"gave 0.7 {chain.params.coin}" in page
    assert "Block 120" in page


def test_a_token_match_lists_every_maker_it_met(client):
    app, state = client
    chain, index = _index(state)
    _write(index)
    page = app.get(f"/exchange/trade/{MATCH}?chain={chain.network}").text
    assert page.count("Matched by the chain") == 2 and SELLER in page and MAKER2 in page


def test_an_unknown_trade_is_not_found(client):
    app, _ = client
    assert app.get("/exchange/trade/" + "e" * 64).status_code == 404
    assert app.get("/exchange/trade/nothex").status_code == 404


def test_the_public_site_opens_it():
    from arcade.web import door
    assert door.public_path("/exchange/trade/" + "a" * 64)

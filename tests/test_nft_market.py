"""The NFT marketplace: the list of collections, one collection opened, and
putting a piece of your own up for sale.

Everything here runs off a real ledger database and no node, because that is
what the marketplace is: collections, listings and offers are all read from
the chain this node has indexed. The node is only asked which addresses are
this wallet's, which is what tells a Make offer button from a List for sale
one.
"""

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                            # noqa: F401,E402
from test_collection_web import index_with_a_collection           # noqa: E402

#: The fixture's collection: five Doge Punks by nMe, editions 1..5, numbered
#: backwards (edition 1 is number 4), which is what makes "chronological" and
#: "edition order" tell each other apart.
CREATOR = "nMe"
PIECES = [f"{edition:064x}" for edition in range(1, 6)]
SHOP = "5" * 64


class Wallet:
    """A node that owns some addresses and knows nothing else."""

    def __init__(self, *mine: str):
        self.mine = list(mine)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def call(self, method, *args):
        if method == "listreceivedbyaddress":
            return [{"address": a, "account": "arcade-identity"} for a in self.mine]
        if method == "listunspent":
            return []
        if method == "getbalance":
            return 12.5
        raise AssertionError(f"unexpected rpc {method}")


def wallet_holding(state, monkeypatch, *addresses):
    """Make the wallet's node say these addresses are its own.

    A listing also needs this wallet's messaging key, because the shop's JSON
    names the node that answers for it -- given here rather than derived from
    a node that does not exist.
    """
    from arcade.messaging.keys import Identity

    node = Wallet(*addresses)
    monkeypatch.setattr(type(state.ledger), "rpc", lambda self: node)
    state.identity = Identity.generate()
    state.ensure_identity = lambda: state.identity
    return node


def sell(home, piece, price="5", shop=SHOP, seller=CREATOR, number=99):
    """A shop inscription selling one piece, as the seller's node wrote it."""
    from arcade.db import Database

    db = Database(home / "main-ledger.sqlite")
    db.conn.execute(
        "INSERT INTO inscription(txid,number,creator,owner,block_height,position,"
        "content_type,content_len,sha256,json,chunks,content) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (shop, number, seller, seller, 300, 0, "text/html", 20, "ef" * 32,
         json.dumps({"name": "for sale",
                     "shop": {"node": "arcade:test:abc",
                              "listings": [{"give": {"inscription": piece},
                                            "take": {"coins": price}}]}}),
         1, b"<html>"))
    db.conn.commit()
    db.close()


def offer_on(home, piece, buyer="nBuyer", txid=None):
    from arcade.db import Database

    db = Database(home / "main-ledger.sqlite")
    db.conn.execute(
        "INSERT INTO nft_offer(txid,block_height,position,inscription,buyer,"
        "take_kind,take_property,take_amount) VALUES(?,?,?,?,?,?,?,?)",
        (txid or ("d" * 63 + "1"), 310, 0, piece, buyer, 3, None, 700000000))
    db.conn.commit()
    db.close()


def tag(home, address, name):
    from arcade.db import Database

    db = Database(home / "main-ledger.sqlite")
    db.conn.execute("INSERT INTO tag(tag,address,claimed_txid,block_height,position) "
                    "VALUES(?,?,?,?,?)", (name, address, "e" * 64, 320, 0))
    db.conn.commit()
    db.close()


def order_of(body, *needles):
    """Where each needle first appears, so a page's order can be asserted."""
    found = []
    for needle in needles:
        at = body.find(needle)
        assert at >= 0, f"{needle} is not on the page"
        found.append(at)
    return found


def grid(body):
    """The wall of pieces alone.

    Sliced off the rest because the page says a piece's txid in more places
    than the grid -- the hero is #1 of the set, and the band above is what
    this wallet holds. An assertion about the ORDER of the collection has to
    be made about the collection.
    """
    at = body.index('<div class="tiles">')
    return body[at:]


# --- the list of collections ------------------------------------------------

def test_the_market_lists_collections_by_their_first_piece(client):
    app, state = client
    index_with_a_collection(state.home)
    tag(state.home, CREATOR, "punkmaker")

    body = app.get("/exchange?tab=market").text
    assert "Doge Punks" in body
    assert "/exchange/collection/nMe/Doge%20Punks" in body, "the row opens the collection"
    assert f"/content/{PIECES[0]}" in body, "the face of the row is #1, not a random piece"
    for other in PIECES[1:]:
        assert f"/content/{other}" not in body
    assert "@punkmaker" in body, "who made it, by name"


def test_a_collection_with_something_for_sale_is_listed_first(client):
    app, state = client
    index_with_a_collection(state.home)
    sell(state.home, PIECES[2])

    body = app.get("/exchange?tab=market").text
    # The table says how many of the set can be bought right now; the number
    # is what a marketplace is for.
    assert "For sale" in body and ">1<" in body


def test_the_market_survives_a_shop_that_is_nonsense(client):
    app, state = client
    index_with_a_collection(state.home)
    sell(state.home, "9" * 64)          # a shop selling a piece nobody has
    body = app.get("/exchange?tab=market").text
    assert "Doge Punks" in body and "Traceback" not in body


# --- one collection, opened -------------------------------------------------

def test_open_contracts_come_first_then_the_chain_order(client):
    app, state = client
    index_with_a_collection(state.home)
    sell(state.home, PIECES[4])          # edition 5, which is inscription #0
    offer_on(state.home, PIECES[1])      # edition 2, which is inscription #3

    body = grid(app.get("/exchange/collection/nMe/Doge%20Punks").text)
    for_sale, offered, first, second = order_of(
        body, PIECES[4], PIECES[1], PIECES[0], PIECES[3])
    assert for_sale < offered, "what can be bought comes before what is offered for"
    assert offered < first, "and both come before the rest of the collection"
    # The rest in the order they were inscribed: edition 1 is inscription #4
    # and edition 4 is #1, so chronological and edition order disagree here.
    assert second < first, "the rest is chronological, not in edition order"


def test_every_card_names_both_ends_and_offers_for_it(client):
    app, state = client
    index_with_a_collection(state.home)
    tag(state.home, CREATOR, "punkmaker")

    body = app.get("/exchange/collection/nMe/Doge%20Punks").text
    tiles = grid(body)
    assert tiles.count("by @punkmaker") >= 5, "who made each one"
    assert tiles.count("held by") >= 5, "and who holds it now"
    assert tiles.count("Make offer") >= 5, "every card offers for it"
    assert 'action="/exchange/offer"' in tiles


def test_a_piece_for_sale_says_its_price_and_a_way_to_buy(client):
    app, state = client
    index_with_a_collection(state.home)
    sell(state.home, PIECES[0], price="7")

    body = app.get("/exchange/collection/nMe/Doge%20Punks").text
    assert "for sale" in body and "7 coins" in body
    assert f"/inscriptions/{SHOP}/view" in body, "the listing is where it is bought"


def test_your_own_pieces_offer_to_be_listed_rather_than_offered_for(
        client, monkeypatch):
    app, state = client
    index_with_a_collection(state.home)
    wallet_holding(state, monkeypatch, CREATOR)

    body = app.get("/exchange/collection/nMe/Doge%20Punks").text
    assert f"/exchange/sell/{PIECES[0]}" in body, "your own piece can be listed"
    assert "Make offer" not in body, "and cannot be offered for by you"
    assert "<strong>you</strong>" in body


def test_the_collection_page_lists_what_you_hold_of_it(client, monkeypatch):
    """What a wallet can sell is the first thing it wants from a set of its
    own, and it must not depend on which page its pieces fell on."""
    app, state = client
    index_with_a_collection(state.home)
    sell(state.home, PIECES[1], price="9")
    wallet_holding(state, monkeypatch, CREATOR)

    body = app.get("/exchange/collection/nMe/Doge%20Punks").text
    band = body[body.index("Yours in this collection"):body.index('<div class="tiles">')]
    assert "Yours in this collection &mdash; 5" in body
    assert band.count("List for sale") == 4, "the four that are not listed yet"
    assert "9 coins" in band, "and the one that is says its price"
    assert "listed" in band


def test_a_collection_nobody_inscribed_says_so(client):
    app, state = client
    index_with_a_collection(state.home)
    assert app.get("/exchange/collection/nMe/Nope").status_code in (200, 303)


def describe_the_set(home, piece=None, **details):
    """Put collection-level details on the set's #1, where they belong."""
    from arcade.db import Database

    db = Database(home / "main-ledger.sqlite")
    item = {"name": "Doge Punks #1", "edition": 1,
            "attributes": [{"trait_type": "Background", "value": "Blue"}],
            "collection": {"name": "Doge Punks", **details}}
    db.conn.execute("UPDATE inscription SET json=? WHERE txid=?",
                    (json.dumps(item), piece or PIECES[0]))
    db.conn.commit()
    db.close()


def test_a_set_describes_itself_on_its_first_piece(client):
    """#1 is the piece a collection is known by, so it is where the set says
    what it is -- rather than on all five hundred of them."""
    app, state = client
    index_with_a_collection(state.home)
    describe_the_set(state.home, description="Five  hand drawn punks",
                     url="https://punks.example", twitter="@dogepunks",
                     discord="javascript:alert(1)", supply=5)

    body = app.get("/exchange/collection/nMe/Doge%20Punks").text
    assert "Five hand drawn punks" in body, "the description, whitespace tidied"
    assert 'href="https://punks.example"' in body
    assert "https://x.com/dogepunks" in body, "a handle becomes a link"
    assert "javascript:alert(1)" not in body, "a link that is not http(s) is not a link"
    # And it is still filed by its name: an object where a string was
    # expected must not move a piece out of its own collection.
    assert "Doge Punks" in body and app.get("/exchange?tab=market").text.count(
        "/exchange/collection/nMe/Doge%20Punks") >= 1


def test_the_strip_says_floor_owners_and_what_has_sold(client):
    app, state = client
    index_with_a_collection(state.home)
    sell(state.home, PIECES[2], price="4")
    sell(state.home, PIECES[3], price="11", shop="6" * 64, number=98)

    body = app.get("/exchange/collection/nMe/Doge%20Punks").text
    strip = body[body.index('class="statbar"'):body.index('<h2>')]
    assert "Floor" in strip and "4.0000" in strip, "the cheapest listed, not the dearest"
    assert "Owners" in strip and "Pieces" in strip
    # Cheapest first among what is for sale: the first tile of a collection
    # is its floor.
    tiles = grid(body)
    assert tiles.index(PIECES[2]) < tiles.index(PIECES[3])


def test_the_market_table_says_the_floor(client):
    app, state = client
    index_with_a_collection(state.home)
    sell(state.home, PIECES[2], price="4")

    body = app.get("/exchange?tab=market").text
    assert "Floor" in body and "4.0000" in body


def test_owners_are_counted_not_guessed_from_the_size(tmp_path):
    from arcade.config import NETWORKS
    from arcade.ledger import LedgerIndex
    from arcade.db import Database

    path = index_with_a_collection(tmp_path)
    index = LedgerIndex(path, NETWORKS["main"], rpc_factory=lambda: None)
    assert index.collection_owners(CREATOR, "Doge Punks") == 1
    db = Database(path)
    db.conn.execute("UPDATE inscription SET owner=? WHERE txid=?", ("nYou", PIECES[0]))
    db.conn.commit()
    db.close()
    assert index.collection_owners(CREATOR, "Doge Punks") == 2


# --- putting one up for sale ------------------------------------------------

def test_the_wallet_offers_to_list_what_it_holds(client, monkeypatch):
    app, state = client
    index_with_a_collection(state.home)
    wallet_holding(state, monkeypatch, CREATOR)

    body = app.get("/wallet/nfts").text
    assert f"/exchange/sell/{PIECES[0]}" in body


def test_a_piece_already_for_sale_says_so_instead(client, monkeypatch):
    app, state = client
    index_with_a_collection(state.home)
    sell(state.home, PIECES[0], price="7")
    wallet_holding(state, monkeypatch, CREATOR)

    body = app.get("/wallet/nfts").text
    assert "7 coins" in body
    assert f"/exchange/sell/{PIECES[0]}" not in body, "listing it twice sells it once"


def test_the_sell_form_opens_on_the_piece_it_is_about(client, monkeypatch):
    app, state = client
    index_with_a_collection(state.home)
    wallet_holding(state, monkeypatch, CREATOR)

    body = app.get(f"/exchange/sell/{PIECES[0]}").text
    assert "Doge Punks #1" in body and "held by nMe" in body
    assert 'name="amount"' in body, "it asks a price"
    # Mainnet reads no swaps, so a listing made there could be seen and not
    # bought. Said before the fee, not after it.
    assert "Swaps are not read on mainnet yet" in body


def test_an_inscription_this_node_has_never_seen_is_not_a_listing(client):
    app, state = client
    index_with_a_collection(state.home)
    assert app.get(f"/exchange/sell/{'9' * 64}").status_code in (200, 303)


def test_listing_prices_the_page_before_it_pays_for_it(client, monkeypatch):
    app, state = client
    index_with_a_collection(state.home)
    wallet_holding(state, monkeypatch, CREATOR)

    body = app.post("/exchange/sell",
                    data={"csrf_token": state.csrf_token, "inscription": PIECES[0],
                          "amount": "5", "kind": "coins"}).text
    assert "The page costs" in body, "the first press prices it"
    assert "5 coins" in body
    assert "Traceback" not in body


def test_only_the_wallet_holding_a_piece_may_list_it(client, monkeypatch):
    app, state = client
    index_with_a_collection(state.home)
    wallet_holding(state, monkeypatch, "nSomebodyElse")

    body = app.post("/exchange/sell",
                    data={"csrf_token": state.csrf_token, "inscription": PIECES[0],
                          "amount": "5", "kind": "coins"}).text
    assert "only the wallet holding a piece can list it" in body


def test_a_listing_says_what_it_sells_and_who_answers_for_it(tmp_path):
    """The JSON that goes on the chain, checked as a shop reads it."""
    from arcade import sellpage, swap as swaplib

    text = sellpage.sale_json("arcade:test:abc", PIECES[0].upper(),
                              {"coins": "5"}, "Doge Punks #1")
    shop = swaplib.shop_of({"json": text, "owner": CREATOR, "creator": CREATOR})
    assert shop["node"] == "arcade:test:abc"
    assert shop["listings"] == [{"give": {"inscription": PIECES[0]},
                                 "take": {"coins": "5"}}]
    page = sellpage.page(PIECES[0], "Doge Punks #1", "5 coins")
    assert PIECES[0].encode() in page and b"/r/swap.js" in page
    assert b"%%" not in page, "every placeholder is filled before it is paid for"


def test_a_name_with_html_in_it_cannot_write_the_page(tmp_path):
    from arcade import sellpage

    page = sellpage.page(PIECES[0], "<script>alert(1)</script>", "5 coins")
    assert b"<script>alert(1)" not in page
    assert b"&lt;script&gt;" in page

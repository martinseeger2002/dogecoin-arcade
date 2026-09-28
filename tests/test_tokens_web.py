"""The /tokens pages against a real regtest node.

test_web.py proves every token page renders with the node unreachable. This
proves the other half: that the forms build, show and broadcast a transaction
the index then reads back -- and that what the confirm screen says (fee, txid,
recipient) is what the node saw, not merely that a table was drawn.
"""

import base64
import dataclasses
import re as _r
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from arcade.config import NETWORKS
from arcade.rpc import RpcClient
from arcade.script import b58check_encode, hash160
from arcade.web.app import create_app
from arcade.web.state import AppState, ChainContext
from arcade.web.watcher import BlockWatcher
from test_end_to_end import chain  # noqa: F401  (fixture re-export)


@dataclasses.dataclass
class RegtestContext(ChainContext):
    """A ChainContext wired to the test node rather than a datadir on disk."""

    node: Any = None
    activation: int | None = None

    @property
    def params(self):
        return dataclasses.replace(super().params, activation_height=self.activation)

    def rpc(self) -> RpcClient:
        return RpcClient(self.node.rpc._creds)


@pytest.fixture
def web(tmp_path, chain):
    node, alice, bob = chain
    ledger = RegtestContext(network="regtest", role="ledger", label="Testnet",
                            node=node, activation=node.rpc.get_block_count() + 1,
                            marker=node.rpc.call("getnewaddress"))
    state = AppState(home=tmp_path, ledger=ledger,
                     messaging=RegtestContext(network="regtest", role="messaging",
                                              label="Testnet", node=node))
    return TestClient(create_app(state)), state, node, alice, bob


def mine_and_index(node, state):
    node.generate(1)
    BlockWatcher(state)._sync_ledgers()          # what the daemon thread would do
    assert state.token_index(state.ledger).stopped is None, state.token_index(state.ledger).stopped


def shown(page: str, label: str) -> str:
    """The value the confirm table shows beside `label`."""
    match = re.search(rf'<td class="muted"[^>]*>{label}</td>\s*<td[^>]*>(?:<strong>)?([^<\n]+)', page)
    assert match, f"{label!r} is not on the confirm page"
    return match.group(1).strip()


def test_two_prices_that_overlap_are_said_as_a_trade_and_not_a_sign(web):
    """A crossed book is the most actionable state a pair page can be in (a tester).

    `/exchange/pair/9` printed `-8.00000000 spread` and `/exchange/pair/14` printed
    `0.00000000`, which reads as a perfectly tight market. Neither page was wrong
    about the arithmetic; both were wrong about what it means. This node never
    matches two orders -- a fill is a swap both sides sign (D-048) -- so two prices
    that overlap sit there waiting for somebody to press, which is news a reader
    can act on and a decimal is not. Both shapes are asked here: the bid ABOVE the
    ask, and the bid exactly AT it, which is the one a check for a minus sign alone
    would have walked straight past.
    """
    app, state, node, alice, bob = web
    csrf = state.csrf_token
    form = dict(csrf_token=csrf, sender=alice, name="Cross Token", supply="1000",
                kind="fixed", units="divisible")
    txid = shown(app.post("/tokens/create", data=form).text, "txid")
    app.post("/tokens/create", data={**form, "confirmed": txid}, follow_redirects=False)
    mine_and_index(node, state)
    index = state.token_index(state.ledger)
    (prop,) = index.properties()
    pid = str(prop["property_id"])
    home = index.balances([alice])[0]["address"]

    import unittest.mock as mock

    def pair() -> str:
        # The page with its line breaks folded away. What is being asked below is
        # a sentence, and the template wraps sentences over several lines.
        return " ".join(app.get(f"/exchange/pair/{pid}").text.split())

    def file(side: str, price: str) -> str:
        """Put an order on the book and hand back what the node said about it.

        The notice is read-once -- the next page render clears it -- so it is
        caught here, at the moment the order goes in. That is also the moment the
        plan asks this route to speak (a tester).
        """
        app.post("/exchange/order", data=dict(csrf_token=csrf, property_id=pid,
                                             side=side, amount="10", price=price),
                 follow_redirects=False)
        said = state.notice or ""
        mine_and_index(node, state)
        return said

    def withdraw(side: str) -> None:
        app.post("/exchange/order/cancel", data=dict(csrf_token=csrf, property_id=pid,
                                                    side=side),
                 follow_redirects=False)
        mine_and_index(node, state)

    with mock.patch.object(type(state), "home_address", lambda self, chain: home):
        file("ask", "1")

        # A bid AT the ask. One-sided, a spread of zero would be the honest
        # figure; with both sides resting it is a market with a trade in it.
        said = file("bid", "1")
        assert "It is at or above the best ask" in said, said
        assert "you could buy what is on the book now" in said, said
        page = pair()
        assert '<strong class="mono">0.00000000</strong>' not in page, \
            "a zero with both sides on the book is a crossed book, not a tight one"
        assert "<strong>Crossed</strong>" in page

        # And a bid ABOVE it, the shape that used to print a minus sign.
        withdraw("bid")
        said = file("bid", "2")
        assert "It is at or above the best ask, 1 coins" in said, said
        page = pair()
        assert "-1.00000000" not in page, "the sign on a decimal is not the news"
        assert "Crossed" in page
        assert "your bid reaches the ask" in page, \
            "and which of the reader's own bids is the one that clears right now"
        assert ("A bid is an offer and not yet a trade: nothing on this node "
                "matches two orders, and a bid moves only when somebody buys "
                "into it (D-048).") in page, "bids do not fill themselves"

        # Withdraw it and put one back BELOW the ask: the figure comes back, and
        # so does the silence, so the page is not saying "Crossed" -- or offering
        # a trade -- wherever two prices happen to sit.
        withdraw("bid")
        said = file("bid", "0.5")
        assert "at or above the best ask" not in said, said
        page = pair()
        assert "Crossed" not in page
        assert '<strong class="mono">0.50000000</strong> <span class="muted">spread' in page, \
            "an honest spread between two prices that do not overlap"


def test_create_confirm_broadcast_and_read_back(web):
    app, state, node, alice, bob = web
    csrf = state.csrf_token

    page = app.get("/tokens").text
    assert "No tokens exist" in page
    assert f'value="{alice}"' in page, "the funded address must be offered as issuer"

    assert app.post("/tokens/create", data={"name": "Web Token"}).status_code == 400

    # Prepare: nothing broadcast, the transaction shown.
    form = dict(csrf_token=csrf, sender=alice, name="Web Token", supply="1000",
                kind="fixed", units="divisible")
    page = app.post("/tokens/create", data=form).text
    assert "Broadcast" in page and 'name="confirmed"' in page
    txid = shown(page, "txid")
    fee = shown(page, "Fee")
    assert shown(page, "From") == alice
    assert shown(page, "Total") == fee, "a creation has no recipient, so the fee is the whole cost"
    assert node.rpc.call("getrawmempool") == [], "prepare must not broadcast"

    # A confirmation for a transaction this server no longer holds (say it
    # restarted in between) is shown again, not sent.
    page = app.post("/tokens/create", data={**form, "confirmed": "0" * 64}).text
    assert "Confirm before sending" in page
    assert node.rpc.call("getrawmempool") == []

    # Confirm: it reaches the mempool with exactly the fee the page showed.
    response = app.post("/tokens/create", data={**form, "confirmed": txid},
                        follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/tokens"
    entry = node.rpc.call("getmempoolentry", txid)
    assert f"{float(entry['fee']):.8f}" == fee
    page = app.get("/tokens").text
    assert txid in page and "waiting for its block" in page

    mine_and_index(node, state)
    page = app.get("/tokens").text
    assert "waiting for its block" not in page
    assert "Web Token" in page and "1,000" in page
    (prop,) = state.token_index(state.ledger).properties()
    detail = app.get(f"/tokens/{prop['property_id']}").text
    assert 'class="pill ok">you</span>' in detail, "the issuer is in this wallet"
    assert "create (fixed supply)" in detail
    assert "Issuer controls" in detail
    assert f'href="/exchange/pair/{prop['property_id']}">Trade Web Token</a>' in detail, \
        "a token's page is a way into its market (a tester, 2026-09-27)"
    assert app.get("/tokens/999").status_code == 404

    # Send: refused before it costs anything, then shown, then read back.
    send = dict(csrf_token=csrf, sender=alice, property_id=str(prop["property_id"]))
    page = app.post("/tokens/send", data={**send, "amount": "5000", "recipient": bob}).text
    assert "holds 1,000 Web Token, not 5,000" in page
    mainnet_address = b58check_encode(NETWORKS["main"].pubkeyhash_version, bytes(20))
    page = app.post("/tokens/send", data={
        **send, "amount": "250", "recipient": mainnet_address}).text
    assert "that is a main address, not a testnet one" in page
    assert node.rpc.call("getrawmempool") == []

    page = app.post("/tokens/send", data={**send, "amount": "250", "recipient": bob}).text
    assert shown(page, "To") == bob
    txid = shown(page, "txid")
    app.post("/tokens/send", data={**send, "amount": "250", "recipient": bob,
                                   "confirmed": txid}, follow_redirects=False)
    assert txid in node.rpc.call("getrawmempool")
    mine_and_index(node, state)
    index = state.token_index(state.ledger)
    assert index.balance(alice, prop["property_id"]) == 750 * 10**8
    assert index.balance(bob, prop["property_id"]) == 250 * 10**8
    # What this wallet holds is the wallet's Tokens tab; /tokens is what exists.
    page = app.get("/wallet/tokens").text
    assert ">750<" in page and ">250<" in page, "both wallet addresses are 'yours'"


def test_one_balance_a_token_not_one_a_piece(web):
    """A wallet holding a token on two addresses holds one balance of it.

    The addresses are how the node keeps it; the page shows the total, and
    the send picks the address it comes out of (D-031).
    """
    app, state, node, alice, bob = web
    csrf = state.csrf_token
    form = dict(csrf_token=csrf, sender=alice, name="Web Token", supply="1000",
                kind="fixed", units="divisible")
    txid = shown(app.post("/tokens/create", data=form).text, "txid")
    app.post("/tokens/create", data={**form, "confirmed": txid}, follow_redirects=False)
    mine_and_index(node, state)
    (prop,) = state.token_index(state.ledger).properties()
    pid = str(prop["property_id"])

    # Move part of it to the wallet's other address: one token, two pieces.
    send = dict(csrf_token=csrf, sender=alice, property_id=pid)
    txid = shown(app.post("/tokens/send",
                          data={**send, "amount": "400", "recipient": bob}).text, "txid")
    app.post("/tokens/send", data={**send, "amount": "400", "recipient": bob,
                                   "confirmed": txid}, follow_redirects=False)
    mine_and_index(node, state)

    page = app.get("/wallet/tokens").text
    assert page.count(">Web Token</strong>") == 1, "one row, not one per address"
    assert ">1,000<" in page, "the total, not either piece"
    assert "on 2 addresses" in page
    assert alice in page and bob in page, "the pieces are still there to look at"

    # A send that names no address: the wallet finds one holding enough.
    page = app.post("/tokens/send", data=dict(csrf_token=csrf, property_id=pid,
                                              amount="600", recipient=bob)).text
    assert shown(page, "From") == alice, "the only address holding 600"

    # More than any one address holds is not refused: it is several
    # transactions, all shown before any of them goes (D-043). Sent to
    # somebody else, so both of this wallet's piles can pay.
    stranger = node.rpc.call("getnewaddress")
    # Both piles need coins of their own: every transaction pays its fee from
    # the address it comes out of, and change from earlier sends went to
    # addresses of the node's choosing rather than back to these.
    node.rpc.call("sendtoaddress", alice, 2)
    node.rpc.call("sendtoaddress", bob, 2)
    mine_and_index(node, state)
    page = app.post("/tokens/send", data=dict(csrf_token=csrf, property_id=pid,
                                              amount="900", recipient=stranger)).text
    assert "Confirm before sending &mdash; 2 transactions" in page
    assert "a token send comes out of one address at a time" in page
    assert alice in page and bob in page
    assert node.rpc.call("getrawmempool") == [], "showing them must not broadcast"

    # One yes sends both, and what goes out is what was shown.
    plan = _r.findall(r"([0-9a-f]{64})", page)[0]
    moved = app.post("/tokens/send",
                     data=dict(csrf_token=csrf, property_id=pid, amount="900",
                               recipient=stranger, confirmed=plan),
                     follow_redirects=False)
    assert moved.status_code == 303 and moved.headers["location"] == "/wallet/tokens"
    pool = node.rpc.call("getrawmempool")
    assert len(pool) == 2 and plan in pool
    mine_and_index(node, state)
    ledger = state.token_index(state.ledger)
    assert ledger.balance(stranger, prop["property_id"]) == 900 * 10**8
    assert ledger.balance(alice, prop["property_id"]) + \
        ledger.balance(bob, prop["property_id"]) == 100 * 10**8

    # More than the wallet holds altogether still is refused.
    page = app.post("/tokens/send", data=dict(csrf_token=csrf, property_id=pid,
                                              amount="1500", recipient=stranger)).text
    assert "holds 100 Web Token, not 1,500" in page, "what is left, after the 900"


def test_a_tag_is_claimed_by_one_press_and_read_back(web, monkeypatch):
    """Claiming @tag from the address book: shown, then broadcast, then indexed.

    The chain already decided who holds what (test_tags.py). This is the half
    that was missing until D-032: a way to claim one without a command line.
    """
    app, state, node, alice, bob = web
    csrf = state.csrf_token
    monkeypatch.setattr(type(state), "derived_address", property(lambda self: alice))

    # Refused before anything is broadcast.
    assert "not" in app.post("/publish", data=dict(csrf_token=csrf, tag="A B")).text
    assert "arcade" in app.post("/publish",
                                data=dict(csrf_token=csrf, tag="arcade")).text
    assert node.rpc.call("getrawmempool") == [], "a refusal must not spend"

    # One button: the claim goes out on the press, no second confirmation.
    app.post("/publish", data=dict(csrf_token=csrf, tag="robin"))
    pool = node.rpc.call("getrawmempool")
    assert pool, "pressing it claims the name"
    mine_and_index(node, state)

    index = state.token_index(state.ledger)
    assert index.tag_of(alice) == "robin"
    assert index.address_of("robin") == alice
    assert "@robin" in app.get("/contacts").text

    # Already yours, and somebody else's, are both refused.
    # Already yours is a no-op rather than an error -- the button also
    # publishes a key, and pressing it twice must not be a refusal.
    before = len(node.rpc.call("getrawmempool"))
    app.post("/publish", data=dict(csrf_token=csrf, tag="robin"))
    assert state.token_index(state.ledger).tag_of(alice) == "robin"
    monkeypatch.setattr(type(state), "derived_address", property(lambda self: bob))
    assert "taken" in app.post("/publish",
                               data=dict(csrf_token=csrf, tag="robin")).text


def test_a_message_can_be_addressed_to_a_tag(web, monkeypatch):
    """@tag names an address; the address's announcement names the key."""
    import pytest

    from arcade.web.app import _resolve_recipient

    app, state, node, alice, bob = web
    csrf = state.csrf_token
    monkeypatch.setattr(type(state), "derived_address", property(lambda self: alice))
    app.post("/publish", data=dict(csrf_token=csrf, tag="robin"))
    mine_and_index(node, state)

    # Nobody has announced a key at that address yet, so it says so about the
    # key -- not about the tag, which resolved.
    with pytest.raises(ValueError, match="no announced key"):
        _resolve_recipient(state, "@robin")
    with pytest.raises(ValueError, match="nobody holds @nobody"):
        _resolve_recipient(state, "@nobody")

    key = b"\x04" * 32
    with state.store() as store:
        store.add_key_announcement("aa" * 32, alice, key, "fp", 1, 0, stated=True)
    assert _resolve_recipient(state, "@robin") == key
    assert _resolve_recipient(state, "@ROBIN") == key, "a tag is not case sensitive"


def test_an_inscription_named_in_a_post_gets_a_card_not_its_content(web):
    """A post can name an inscription; the feed shows a card for it.

    Never the content: a post is written by a stranger and an inscription can
    be a page of scripts. The card is what this node's own index says, and
    the button opens the viewer, which has the sandbox (D-035).

    (The board became the feed, D-138. Same rule, same card, one page.)
    """
    app, state, node, alice, bob = web
    index = state.token_index(state.ledger)
    txid = "cd" * 32
    page = b"<b>hello</b>" * 20
    with index.open() as db:
        db.conn.execute(
            "INSERT INTO block(height, hash, prev_hash, time, tx_count, processed_at) "
            "VALUES(1,'h','p',0,1,0)")
        db.conn.execute(
            "INSERT INTO inscription(txid,number,creator,owner,block_height,position,"
            "content_type,content_len,sha256,json,chunks,content) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (txid, 108, alice, alice, 1, 0, "text/html", len(page), "ab" * 32,
             '{"name": "Goofball Mintpad"}', 1, page))
        db.conn.commit()

    with state.store() as store:
        store.add_group_post("regtest", "", "tx1", 1, 0, alice, "someone",
                             f"minting here: /content/{txid}", mine=False)
        store.add_group_post("regtest", "", "tx2", 2, 0, alice, "someone",
                             "no inscription in this one", mine=False)

    body = app.get("/feed").text

    # The board showed a CARD -- name, type, size -- and refused to render
    # the thing itself. The feed renders it, in the sandbox the viewer uses
    # (D-138). Same guarantee by a different route: the page's own HTML
    # never contains the inscription's, so a post cannot bring markup or a
    # script into this document.
    assert "&lt;b&gt;hello" not in body and "<b>hello</b>" not in body, \
        "the content itself is never inlined, escaped or otherwise"
    assert f'src="/content/{txid}"' in body, "it is framed, not inlined"
    frame = body[body.index(f'src="/content/{txid}"') - 300:
                 body.index(f'src="/content/{txid}"') + 200]
    assert "sandbox=" in frame, "and the frame is sandboxed"
    assert "allow-same-origin" not in frame, \
        "no same-origin, or it could reach this page and this wallet"

    # An inscription this node does not have is a link, not a frame, and
    # nothing breaks: a thing it cannot identify is a thing it should not
    # be drawing.
    unknown = "ab" * 32
    with state.store() as store:
        store.add_group_post("regtest", "", "tx3", 3, 0, alice, "someone",
                             "/content/" + unknown, mine=False)
    body = app.get("/feed").text
    assert app.get("/feed").status_code == 200
    assert f'src="/content/{unknown}"' not in body
    assert f"/inscriptions/{unknown}/view" in body, "a link to the viewer"


def test_an_offer_can_only_be_made_with_what_this_wallet_holds(web, monkeypatch):
    """The form lists your tokens; the door checks them again (D-040)."""
    app, state, node, alice, bob = web
    csrf = state.csrf_token
    index = state.token_index(state.ledger)
    txid = "ef" * 32
    with index.open() as db:
        db.conn.execute(
            "INSERT INTO block(height, hash, prev_hash, time, tx_count, processed_at) "
            "VALUES(1,'h','p',0,1,0)")
        db.conn.execute(
            "INSERT INTO inscription(txid,number,creator,owner,block_height,position,"
            "content_type,content_len,sha256,json,chunks) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (txid, 5, "nStranger", "nStranger", 1, 0, "image/png", 10, "ab" * 32,
             '{"name": "Theirs"}', 1))
        db.conn.commit()

    page = app.get(f"/inscriptions/{txid}/view").text
    assert "Make an offer" in page, "it is not ours, so it can be offered for"
    assert "Only what this wallet holds can be offered" in page
    assert "no tokens on this chain" not in page, "a token list is not offered at all"

    # A token that exists and belongs to somebody else is refused here,
    # before a fee is spent finding out from them.
    with index.open() as db:
        db.conn.execute(
            "INSERT INTO property(property_id,ecosystem,property_type,issuer,name,"
            "creation_txid,creation_block) VALUES(3,2,2,'nStranger','Theirs','ab',1)")
        db.conn.execute("INSERT INTO balance(address,property_id,balance) "
                        "VALUES('nStranger',3,100000000000)")
        db.conn.commit()
    app.post("/exchange/offer",
             data={"csrf_token": csrf, "inscription": txid, "amount": "10",
                   "kind": "token", "property_id": "3"}, follow_redirects=False)
    assert "no address in this wallet holds" in (state.notice or ""), state.notice

    # More coins than the wallet has, likewise.
    app.post("/exchange/offer",
             data={"csrf_token": csrf, "inscription": txid, "amount": "100000000",
                   "kind": "coins"}, follow_redirects=False)
    assert state.notice, "it says why rather than sending"
    assert state.offers.bids("regtest", "out") == [], "nothing was offered"

    # And the form no longer offers a token picker it cannot fill.
    page = app.get(f"/inscriptions/{txid}/view").text
    assert 'name="property_id"' not in page, "this wallet holds no tokens"


def test_an_order_goes_on_the_book_and_can_be_taken_off(web):
    """The book is on the chain: an order rests until it is cancelled, and
    what it sells is held back meanwhile (D-048)."""
    app, state, node, alice, bob = web
    csrf = state.csrf_token
    form = dict(csrf_token=csrf, sender=alice, name="Web Token", supply="1000",
                kind="fixed", units="divisible")
    txid = shown(app.post("/tokens/create", data=form).text, "txid")
    app.post("/tokens/create", data={**form, "confirmed": txid}, follow_redirects=False)
    mine_and_index(node, state)
    index = state.token_index(state.ledger)
    (prop,) = index.properties()
    pid = str(prop["property_id"])
    monkey = type(state)
    home = index.balances([alice])[0]["address"]

    # The wallet's home address is the one that holds it in these tests.
    import unittest.mock as mock
    with mock.patch.object(monkey, "home_address", lambda self, chain: home):
        page = app.post("/exchange/order",
                        data=dict(csrf_token=csrf, property_id=pid, side="ask",
                                  amount="100", price="0.5"),
                        follow_redirects=False)
        assert page.status_code == 303
        assert "Order on the book" in (state.notice or ""), state.notice
        mine_and_index(node, state)

        book = index.book(prop["property_id"])
        assert len(book["asks"]) == 1 and not book["bids"]
        (order,) = book["asks"]
        assert order["tokens"] == 100 * 10**8 and order["coins"] == 50 * 10**8
        assert float(order["price"]) == 0.5
        assert index.balance(home, prop["property_id"]) == 900 * 10**8, \
            "what is on the book is not also spendable"

        # It shows on the market page, and on the list of markets. Asserted
        # on what the page is FOR rather than on its headings: the book was
        # re-laid-out around a spread (a87df0b) and these assertions went on
        # naming an "Asks" heading and a "Place an order" button that had
        # both been renamed, so the suite was red on a working feature and
        # two releases went out over it (a test machine).
        body = app.get(f"/exchange/pair/{pid}").text
        assert "Order book" in body and "0.5" in body
        assert 'action="/exchange/order"' in body, "and the form that adds to it"
        assert "Web Token" in app.get("/exchange?tab=tokens").text

        # Selling more than is left, counting the order, is refused.
        app.post("/exchange/order",
                 data=dict(csrf_token=csrf, property_id=pid, side="ask",
                           amount="950", price="0.5"), follow_redirects=False)
        mine_and_index(node, state)
        assert len(index.book(prop["property_id"])["asks"]) == 1

        # A bid holds nothing back and needs no tokens at all.
        app.post("/exchange/order",
                 data=dict(csrf_token=csrf, property_id=pid, side="bid",
                           amount="10", price="0.4"), follow_redirects=False)
        mine_and_index(node, state)
        book = index.book(prop["property_id"])
        assert len(book["bids"]) == 1 and book["bids"][0]["reserved"] == 0

        # Cancelling gives the tokens back.
        app.post("/exchange/order/cancel",
                 data=dict(csrf_token=csrf, property_id=pid, side="ask"),
                 follow_redirects=False)
        mine_and_index(node, state)
        assert index.book(prop["property_id"])["asks"] == []
        assert index.balance(home, prop["property_id"]) == 1000 * 10**8


def test_a_cancel_with_a_price_gives_back_one_price_and_not_the_pair(web):
    """The same page's cancel, at one price, on the operator's own route.

    The account's route and this one used to file the identical message, so the
    finding is the same whichever door you come in at: two asks at two prices,
    one press, both gone (a tester). `price` says which of the two shapes
    the wire has is filed -- and here it is the flash line that carries the
    answer, because that is what this route can say at all. The control is the
    balance: an ask's tokens come back when the cancel's block lands, so the
    spendable figure is the supply less the 25 the surviving ask still holds.
    The 50 that was withdrawn is back in there, which is what makes this a
    check of the cancel and not of the book.
    """
    app, state, node, alice, bob = web
    csrf = state.csrf_token
    form = dict(csrf_token=csrf, sender=alice, name="Price Token",
                supply="1000", kind="fixed", units="divisible")
    txid = shown(app.post("/tokens/create", data=form).text, "txid")
    app.post("/tokens/create", data={**form, "confirmed": txid},
             follow_redirects=False)
    mine_and_index(node, state)
    index = state.token_index(state.ledger)
    (prop,) = index.properties()
    pid = str(prop["property_id"])
    home = index.balances([alice])[0]["address"]

    import unittest.mock as mock
    with mock.patch.object(type(state), "home_address",
                           lambda self, chain: home):
        for amount, price in (("50", "0.5"), ("25", "1")):
            app.post("/exchange/order",
                     data=dict(csrf_token=csrf, property_id=pid, side="ask",
                               amount=amount, price=price),
                     follow_redirects=False)
            mine_and_index(node, state)
        assert len(index.book(prop["property_id"])["asks"]) == 2

        page = app.post("/exchange/order/cancel",
                        data=dict(csrf_token=csrf, property_id=pid,
                                  side="ask", price="0.5"),
                        follow_redirects=False)
        assert page.status_code == 303
        assert "one price, not the pair" in (state.notice or ""), state.notice
        mine_and_index(node, state)

        book = index.book(prop["property_id"])
        assert len(book["asks"]) == 1, \
            "a cancel at one price took the other price with it"
        assert float(book["asks"][0]["price"]) == 1.0
        assert index.balance(home, prop["property_id"]) == 975 * 10**8, \
            "the withdrawn price's tokens came back and the standing one's did not"


def test_a_stranger_s_token_page_offers_no_issuer_controls(web):
    """The issuer panel is drawn by `is_issuer`, which asks only whether the
    issuer is one of this machine's addresses. For a token THIS node issued that
    is true of every visitor, so a stranger got hand-over and launchpad buttons
    whose POSTs the door answers "Not here", and a frame pointed at a page the
    door keeps in NEVER_PUBLIC -- plus a "you" pill telling them they were the
    operator (the door census, 2026-09-27).

    What hid it is what reads this test: the guard looked like it was about the
    reader and was about the machine. The account issuer's panel beside it is a
    true guard -- `account_issuer` is set only when this tab holds the key -- so
    that one keeps rendering for the account and never for a stranger.
    """
    app, state, node, alice, bob = web
    csrf = state.csrf_token
    form = dict(csrf_token=csrf, sender=alice, name="Door Token",
                supply="1000", kind="fixed", units="divisible")
    txid = shown(app.post("/tokens/create", data=form).text, "txid")
    app.post("/tokens/create", data={**form, "confirmed": txid},
             follow_redirects=False)
    mine_and_index(node, state)
    index = state.token_index(state.ledger)
    # By name, not by "the only property": the chain outlives this test, so a
    # second token issued somewhere else is not a contradiction here.
    (prop,) = [p for p in index.properties() if p["name"] == "Door Token"]
    pid = prop["property_id"]
    page = f"/tokens/{pid}"

    state.public = True
    try:
        out = app.get(page).text
        assert "Issuer controls" not in out, \
            "the panel acts from this machine's wallet, so it is not this reader's"
        assert f'action="/tokens/{pid}/issuer"' not in out
        assert f'action="/tokens/{pid}/launchpad"' not in out
        assert "/tokens/launchpad/preview" not in out, \
            "and no frame of a page the door keeps shut"
        assert 'class="pill ok">you</span>' not in out, \
            "'you' is a fact about who is looking, not about the token"
        assert "Nothing is selling" in out, "the honest sentence is still there"
    finally:
        state.public = False

    mine = app.get(page).text
    assert "Issuer controls" in mine and 'class="pill ok">you</span>' in mine, \
        "the operator's own page lost nothing"
    assert f'action="/tokens/{pid}/launchpad"' in mine


def test_the_you_pill_on_a_holders_row_names_whoever_is_looking(web):
    """The other half of the door census, which it left behind.

    `owned` is what THIS MACHINE holds, so the pill in the Holders table has
    always named the node rather than the reader. An account holding a lot of
    its own was told the node's row was theirs, and given nothing beside the one
    row that is about them -- the stranger's version of that was fixed at
    `cb22835`; nobody had looked at the signed-in one.

    So the account is asked first and the node answers only for a reader with no
    key in the tab, which is the shape of the thing: a reader holding a key is
    one person, and two lit rows says otherwise. Three readers, one page.
    """
    app, state, node, alice, bob = web
    csrf = state.csrf_token
    form = dict(csrf_token=csrf, sender=alice, name="Pill Token",
                supply="1000", kind="fixed", units="divisible")
    txid = shown(app.post("/tokens/create", data=form).text, "txid")
    app.post("/tokens/create", data={**form, "confirmed": txid},
             follow_redirects=False)
    mine_and_index(node, state)
    index = state.token_index(state.ledger)
    # By name, not by "the only property": the chain outlives this test.
    (prop,) = [p for p in index.properties() if p["name"] == "Pill Token"]
    pid = prop["property_id"]
    page = f"/tokens/{pid}"

    from nacl.signing import SigningKey

    from arcade import accounts

    key = SigningKey.generate()
    challenge = app.get("/auth/challenge").json()
    signature = key.sign(accounts.login_message(
        challenge["origin"], challenge["nonce"])).signature
    assert app.post("/auth/login", json={
        "pubkey": key.verify_key.encode().hex(), "nonce": challenge["nonce"],
        "signature": signature.hex(), "join": True}).status_code == 200
    theirs = b58check_encode(state.ledger.params.pubkeyhash_version,
                             hash160(key.verify_key.encode()))
    assert not node.rpc.call("validateaddress", theirs).get("ismine"), \
        "the node has no key for this address, which is the whole point"
    assert app.post("/account/address", json={"address": theirs}).status_code == 200

    send = dict(csrf_token=csrf, sender=alice, property_id=str(pid))
    txid = shown(app.post("/tokens/send", data={**send, "amount": "400",
                                                "recipient": theirs}).text, "txid")
    app.post("/tokens/send", data={**send, "amount": "400", "recipient": theirs,
                                   "confirmed": txid}, follow_redirects=False)
    mine_and_index(node, state)
    assert index.balance(theirs, pid) == 400 * 10**8, "the account is a holder"

    def yours(text: str) -> list[str]:
        """The Holders rows that rendering says belong to whoever is looking."""
        table = text.split("<h2>Holders</h2>")[1].split("</table>")[0]
        return [row for row in re.findall(r"<tr><td>(.*?)</td>", table, re.S)
                if 'class="pill ok">you</span>' in row]

    was, state.public = state.public, True
    try:
        account_rows = yours(app.get(page).text)
    finally:
        state.public = was
    assert len(account_rows) == 1, f"one reader, one row: {account_rows}"
    assert theirs in account_rows[0], "the pill is on somebody else's row"
    assert alice not in account_rows[0], "the node's lot is not this reader's"

    app.cookies.clear()                      # a tab with no key in it
    state.public = True
    try:
        assert yours(app.get(page).text) == [], "a stranger is told nothing is theirs"
    finally:
        state.public = was
    (operator,) = yours(app.get(page).text)
    assert alice in operator, "the operator's own page still names the node's row"


def test_the_pair_page_tells_a_stranger_nothing_about_this_wallet(web):
    """The hole that was on the public site, in a test, about a number.

    `/exchange/pair/<id>` stands in the door's public trees, and until now it
    had one reading of itself and that reading was the operator's: it asked the
    node what it held before it asked who was looking, so a stranger on the
    public host was answered with the operator's own balance in a tick beside a
    chart -- measured on app.dogecoinarcade.com on 2026-09-26, `190992.3622
    coins`. `test_public.py` could not catch this by walking routes: nothing was
    LINKED where the door would shut, and what crossed was a figure. So this
    test is about a figure, and it is asserted on the same page twice, once with
    the door open and once without it.

    The second half is the same mistake in the other direction, found reviewing
    this fix: a stranger was still drawn a `Take` button beside every ask, and
    `/exchange/fill` is not a public route, so the press answered "Not here" --
    a button drawn and refused, which is the thing this change says a page does
    not owe anybody.
    """
    app, state, node, alice, bob = web
    csrf = state.csrf_token
    form = dict(csrf_token=csrf, sender=alice, name="Book Token", supply="1000",
                kind="fixed", units="divisible")
    txid = shown(app.post("/tokens/create", data=form).text, "txid")
    app.post("/tokens/create", data={**form, "confirmed": txid},
             follow_redirects=False)
    mine_and_index(node, state)
    # What this node's wallet says it holds, and the figure the operator's copy
    # of the page prints. Read last, and off the page: the first so that no
    # block mined between the two moves the number out from under the
    # comparison, the second so the test cannot pass by matching a figure the
    # page stopped showing.
    index = state.token_index(state.ledger)
    (prop,) = index.properties()
    pid = str(prop["property_id"])
    # A resting order on the book first, so that the empty cell where a Take
    # button would be has a row to be about: on an empty book "no button" is
    # true of nothing. This one is the operator's own, and the reason it is the
    # row that matters is that `mine` is settled from the READER's addresses --
    # on a public copy nobody holds anything, so this order is not the
    # stranger's either, which is precisely how a stranger came to be handed a
    # button posting to a route the door shuts.
    import unittest.mock as mock
    home = index.balances([alice])[0]["address"]
    with mock.patch.object(type(state), "home_address", lambda self, chain: home):
        app.post("/exchange/order",
                 data=dict(csrf_token=csrf, property_id=pid, side="ask",
                           amount="100", price="0.5"), follow_redirects=False)
        mine_and_index(node, state)
    balance = float(node.rpc.call("getbalance"))
    assert balance > 0, "the node has coins, which is what makes this a test"
    body = app.get(f"/exchange/pair/{pid}").text
    figure = f"{balance:.4f} coins"
    assert figure in body, "and the operator is told what their own machine holds"
    assert 'action="/exchange/order"' in body
    assert "0.5" in body, "the ask is on the book the operator reads"

    state.public = True
    try:
        gone = app.get(f"/exchange/pair/{pid}").text
        assert "0.5" in gone, \
            "the same ask, on the public copy: a row this page could have offered"
        assert 'action="/exchange/fill"' not in gone, \
            "and it does not offer it with a button whose press the door shuts"
        assert figure not in gone, "the node's coins are not the market's news"
        assert "You hold" not in gone, \
            "a stranger has no holdings, and a page does not say otherwise"
        assert 'action="/exchange/order"' not in gone, \
            "and no form here stands a stranger's click behind this machine's coins"
        assert "Sign in to put an order" in gone, "which is what it says instead"
    finally:
        state.public = False
    # And nothing was lost on the way: read by the machine the page is about,
    # it is the same page it always was. A fix that gets here by blanking the
    # panel would pass every assertion above.
    back = app.get(f"/exchange/pair/{pid}").text
    assert figure in back and 'action="/exchange/order"' in back


def test_an_indivisible_price_is_read_in_coins_and_not_in_satoshis(web):
    """An ask of 2 coins each, printed as `200000000`, on every such pair page.

    `index.book` keeps the ratio of the two integers an order is made of, because
    a book sorted on floats orders itself in a way nobody can reproduce (D-048).
    For a divisible token both integers are scaled by COIN and their ratio is
    coins; an indivisible token's token count is never scaled, so its ratio is
    already satoshis. The writing side knows this -- `_cancel_one_or_pair` says
    "divide the wrong way and the cancel names a price a hundred million off" --
    and the reading side simply did not, so the row said what the engine means
    instead of what the person typed.

    Found by rehearsing a cancel of ONE price on the live site for a tester
    (S18, 2026-09-27): two asks on an indivisible pair, one withdrawn, and the
    anonymous page read back showed the surviving row as `200000000 2 4`. The
    cancel was right and the price was not, which is why the test is about the
    page rather than the arithmetic -- the arithmetic was never broken.

    The spread is asserted beside it because it is a difference of the same two
    figures, and a fix that stopped at the table would leave the sentence under
    the book saying the same nonsense.
    """
    import re as _re
    import unittest.mock as mock
    app, state, node, alice, bob = web
    csrf = state.csrf_token
    form = dict(csrf_token=csrf, sender=alice, name="Whole Token", supply="100",
                kind="fixed", units="indivisible")
    txid = shown(app.post("/tokens/create", data=form).text, "txid")
    app.post("/tokens/create", data={**form, "confirmed": txid},
             follow_redirects=False)
    mine_and_index(node, state)
    index = state.token_index(state.ledger)
    (prop,) = index.properties()
    pid = str(prop["property_id"])
    assert not prop["divisible"], "the whole point of the test is this one word"
    home = index.balances([alice])[0]["address"]
    with mock.patch.object(type(state), "home_address", lambda self, chain: home):
        # Two sides, so the spread exists: 2 coins each wanted, 1 coin each
        # offered. They do not cross, so both rest, and the gap is one coin.
        for side, amount, price in (("ask", "2", "2"), ("bid", "1", "1")):
            said = app.post("/exchange/order", data=dict(
                csrf_token=csrf, property_id=pid, side=side, amount=amount,
                price=price), follow_redirects=False)
            assert said.status_code in (200, 302, 303), said.text
        mine_and_index(node, state)
    body = app.get(f"/exchange/pair/{pid}").text
    prices = [_re.sub(r"<[^>]+>", "", c) for c in _re.findall(
        r'<tr class="(?:ask|bid)[^>]*>\s*<td[^>]*>([^<]*)</td>', body)]
    assert prices == ["2", "1"], \
        f"each row priced in the coins somebody typed, and the page said {prices}"
    assert "200000000" not in body and "100000000" not in body, \
        "the satoshi figure appears nowhere, neither in a row nor in the spread"
    assert ">1.00000000<" in body, "and the spread says one coin"


def test_the_pair_page_says_one_price_for_an_indivisible_trade(web, monkeypatch):
    """The chart and the book, on one page, about one order, in one unit.

    The fix above stopped at the rows. The 24h figures, the candles and the
    recent-trade table are built from SWAPS by `charts.token_prices`, which
    divided every leg by COIN -- and an indivisible token's leg is already whole
    tokens. So the sale that read as 2 tokens for 4 coins was read as a sale of
    0.00000002 tokens at 200,000,000 coins each, and the page put
    `200000000.00000000` in the tick beside the row that says `2`. The market
    table did the same thing from the other direction: its last price came off
    the trades and its best ask came off the book's raw ratio, in the same row.

    A real fill needs two nodes and a message that takes blocks, so this asks
    the page about a sale made of the two integers of an order that IS on the
    chain -- the same legs, one each way. What is asserted is that the page
    agrees with itself, which is the claim nobody had ever made in either
    direction.
    """
    import time
    import unittest.mock as mock
    from arcade import inscriptions as I
    from arcade.ledger import COIN
    app, state, node, alice, bob = web
    csrf = state.csrf_token
    form = dict(csrf_token=csrf, sender=alice, name="Whole Trade", supply="100",
                kind="fixed", units="indivisible")
    txid = shown(app.post("/tokens/create", data=form).text, "txid")
    app.post("/tokens/create", data={**form, "confirmed": txid},
             follow_redirects=False)
    mine_and_index(node, state)
    index = state.token_index(state.ledger)
    (prop,) = [p for p in index.properties() if p["name"] == "Whole Trade"]
    pid = prop["property_id"]
    assert not prop["divisible"], "the whole point of the test is this one word"
    home = index.balances([alice])[0]["address"]
    with mock.patch.object(type(state), "home_address", lambda self, chain: home):
        app.post("/exchange/order",
                 data=dict(csrf_token=csrf, property_id=str(pid), side="ask",
                           amount="2", price="2"), follow_redirects=False)
        mine_and_index(node, state)
    (order,) = index.book(pid)["asks"]
    assert (order["tokens"], order["coins"]) == (2, 4 * COIN)

    sold = {"txid": "f" * 64, "height": order["block_height"], "when": time.time(),
            "seller": home,
            "give": I.Leg(I.LEG_TOKEN, property_id=pid, amount=order["tokens"]),
            "take": I.Leg(I.LEG_COINS, amount=order["coins"])}
    monkeypatch.setattr(index, "trades", lambda *a, **k: [sold])

    def tick(page: str, label: str) -> str:
        """What one of the figures above the chart says, tags off."""
        for value, span in _r.findall(
                r'<div class="tick"><b[^>]*>(.*?)</b>\s*<span>(.*?)</span>',
                page, _r.S):
            if span.startswith(label):
                return _r.sub(r"<[^>]+>", "", value).strip()
        raise AssertionError(f"no {label!r} tick on the page")

    body = app.get(f"/exchange/pair/{pid}").text
    assert tick(body, "Last") == "2.00000000", \
        "the last price is coins per token, and this said satoshis per token"
    assert tick(body, "24h volume") == "4.0000", "and four coins changed hands"
    row = _r.search(r'<tr class="ask[^>]*>\s*<td[^>]*>([^<]*)</td>', body)
    assert row and float(row.group(1)) == float(tick(body, "Last")), \
        "one page, one price, two readings of it"
    # The sale itself, in the trade table: two tokens, not 0.00000002 of one.
    sold_row = _r.findall(r'<td class="mono">2\.00000000</td>\s*'
                          r'<td class="mono"[^>]*>([^<]*)</td>', body)
    assert sold_row == ["2"], f"what traded was two tokens, the page said {sold_row}"
    assert "200000000" not in body and "0.00000002" not in body

    table = app.get("/exchange?tab=tokens").text
    assert "200000000" not in table, "no market row says eight zeros for two"
    assert "2.00000000" in table, "its last price and its best ask, both in coins"


def test_a_sensitive_name_is_walled_on_the_pair_page_as_it_is_on_the_token_page(web,
                                                                               monkeypatch):
    """a tester, 2026-09-27: the wall stood on `/tokens/11` and not on
    `/exchange/pair/11`, and what leaked was the name a stranger is least
    prepared for -- in the tab, in the history, in the `<h1>` a screen reader
    says first.

    Measured that day on the public host: the token page answered `<title>A
    token · Tokens · DogecoinArcade` with the name behind covers, and the pair
    page answered `<title>bigblackcocks/TESTNET · Exchange · DogecoinArcade`
    with none. Nothing in `tests/` asked a page about the screening at all, so
    this is the first one, and it is written about both pages: the pair page is
    the one that had the hole, the token page is the one that shows what correct
    looks like.

    The assertion is not that the name is missing -- a walled name is still
    there to be uncovered, which is the whole design (2026-09-26: what
    goes behind a cover stays on the page). It is that the page draws nothing
    but the cover: every copy of the name is inside a `<template>`, which a
    browser leaves undrawn until the reader taps, or inside a script, which a
    browser never draws and which holds the working copies the panels need. A
    page that passed this by deleting the name would fail the token page's own
    tests; a page that passed it by blanking the panel fails the last line.

    The description is in here too, because S36 named it and left it (`face.about`
    is the longest piece of words a person chooses about a token, and it reached
    three pages by two different routes). It is seeded as a second verdict rather
    than as a second token, which is how a verdict actually works -- one text, one
    judgement, read out wherever that text appears -- and the market table is in
    the loop because it prints the description out of `_pairs`, not out of the
    token page's own context, so a wall added to one template proves nothing about
    it. The row is only on that table once the token has a price on the book, so
    one ask is filed first: an assertion that never saw a row is the failure this
    file has already been taught once (a test machine, S20).
    """
    from arcade import moderation as mod
    app, state, node, alice, bob = web
    csrf = state.csrf_token
    name = "Nude Yacht Club"          # a name, not a verdict: the screening decides
    about = "A description written for this token by the person who made it"
    form = dict(csrf_token=csrf, sender=alice, name=name, supply="1000",
                kind="fixed", units="divisible", data=about)
    txid = shown(app.post("/tokens/create", data=form).text, "txid")
    app.post("/tokens/create", data={**form, "confirmed": txid},
             follow_redirects=False)
    mine_and_index(node, state)
    index = state.token_index(state.ledger)
    (prop,) = [p for p in index.properties() if p["name"] == name]
    pid = str(prop["property_id"])

    # Screening switched on -- `_verdict_of` asks nothing of a node that has it
    # off -- with the name's verdict already kept, which is what a verdict is:
    # per content, once, whatever page it is read out on. Everything else the
    # page displays is answered "ok" without a model being stood up.
    monkeypatch.setattr(mod.Screen, "_ask",
                        lambda self, content: ("ok", "answered in this test"))
    state.set_setting("moderation", {"url": "http://127.0.0.1:9/v1",
                                     "model": "test-model"})
    screen = state.screen()
    screen._keep(mod.digest_of(name), "text", mod.SENSITIVE, "seeded here")
    screen._keep(mod.digest_of(about), "text", mod.SENSITIVE, "seeded here")
    assert screen.check_text(name) == mod.SENSITIVE
    assert screen.check_text(about) == mod.SENSITIVE

    # One ask, so the token is a market rather than a fact: `_pairs` skips what
    # has neither price points nor a book, and the market table is one of the
    # three pages this test is about. AFTER the verdicts, which matters: placing
    # an order says the token's name out loud in a flash, and a flash is one slot
    # on shared state that the next page rendered -- by anyone -- gets to show.
    home = index.balances([alice])[0]["address"]
    import unittest.mock as mock
    with mock.patch.object(type(state), "home_address", lambda self, chain: home):
        app.post("/exchange/order",
                 data=dict(csrf_token=csrf, property_id=pid, side="ask",
                           amount="100", price="0.5"), follow_redirects=False)
    mine_and_index(node, state)
    assert index.book(prop["property_id"])["asks"], "the market needs a price"

    def drawn(page: str) -> str:
        """The page as a browser draws it before anybody taps."""
        return _r.sub(r"<(script|template)[^>]*>.*?</\1>", "", page, flags=_r.S)

    state.public = True
    try:
        pair = app.get(f"/exchange/pair/{pid}").text
        token = app.get(f"/tokens/{pid}").text
        market = app.get("/exchange?tab=tokens").text
    finally:
        state.public = False

    assert f">{name}<" in market or "Sensitive token name" in market, \
        "the row this test is about did not render, so prove nothing"

    for label, page in (("the pair page", pair), ("the token page", token),
                        ("the market table", market)):
        for said, cover in ((name, "Sensitive token name"),
                            (about, "Sensitive description")):
            assert said in page, f"{label} removed {cover.lower()} instead of walling it"
            assert said not in drawn(page), \
                f"{label} draws {said!r} at a stranger who did not ask for it"
            assert cover in page, f"{label} hides it without saying what is behind"
        title = _r.search(r"<title>(.*?)</title>", page, _r.S).group(1)
        assert name not in title and about not in title, \
            f"{label} puts it in a tab and a history entry"

    # The two spots the finding named, asserted on their own: the heading and
    # the title. A wall anywhere else on the page would satisfy the loop above
    # and still leave the tab reading like the name.
    heading = _r.search(r"<h1[^>]*>(.*?)</h1>", pair, _r.S).group(1)
    assert "Sensitive token name" in heading, heading[:200]
    assert "A token/" in _r.search(r"<title>(.*?)</title>", pair, _r.S).group(1)
    # And the page is still the page.
    assert "Buy and sell this token" in drawn(pair)


def test_a_name_already_on_the_chain_is_refused_before_it_costs_anything(web):
    """One name, one token. The chain refuses the second issuance now, and a
    refused issuance still costs its fee -- so the wallet asks first (D-122)."""
    app, state, node, alice, bob = web
    csrf = state.csrf_token
    form = dict(csrf_token=csrf, sender=alice, name="Dogecoin Arcade", supply="100",
                kind="fixed", units="divisible")
    txid = shown(app.post("/tokens/create", data=form).text, "txid")
    app.post("/tokens/create", data={**form, "confirmed": txid}, follow_redirects=False)

    # Broadcast but not yet in a block: the name is claimed all the same, or a
    # wallet pays twice to lose a race with itself.
    page = app.post("/tokens/create", data={**form, "name": "dogecoin arcade"}).text
    assert "broadcast a token called" in page
    assert 'name="confirmed"' not in page, "nothing to confirm; nothing was built"

    # And now the case that actually cost money: the name was claimed by
    # somebody ELSE's node, so this wallet has no memory of it. Forgetting
    # what we broadcast is how the other machine sees it -- in the mempool
    # and nowhere else (D-124).
    state.pending_tokens = []
    page = app.post("/tokens/create", data={**form, "name": "DOGECOIN-ARCADE"}).text
    assert "claimed a moment ago by a transaction waiting for its block" in page
    assert 'name="confirmed"' not in page
    assert len(node.rpc.call("getrawmempool")) == 1, "and nothing was added to it"

    mine_and_index(node, state)
    for spelling in ("Dogecoin Arcade", "dogecoin arcade", "DOGECOIN-ARCADE",
                     "DogecoinArcade"):
        page = app.post("/tokens/create", data={**form, "name": spelling}).text
        assert "is already token #" in page, spelling
        assert 'name="confirmed"' not in page

    assert node.rpc.call("getrawmempool") == [], "nothing was sent to be refused"

    # A name of its own is still free.
    page = app.post("/tokens/create", data={**form, "name": "Dogecoin Arcade 2"}).text
    assert 'name="confirmed"' in page

    page = app.post("/tokens/create", data={**form, "name": "!!!"}).text
    assert "needs at least one letter or digit" in page


def test_a_pad_page_carries_one_line_the_node_writes_above_the_frame(web):
    """A mintpad that is somebody's HTML page still says what a mint costs (a tester).

    Both live pads turned out to be the author's own inscribed HTML, shown in the
    sandbox, so the price, what a press gives and any sold-out line are prose inside
    two different layouts -- and the only thing the node itself drew on the pad page
    was a Make offer button. The node has all three facts in its own book, so it
    says them above the frame, in the site's voice, with the same arithmetic the
    inscribed page runs in its own script: the seller's open sell orders, less what
    a take already in the pool is buying. Nothing is pressed here to find out what
    sold-out looks like -- it is read off the orders, which is also what a pad whose
    author never wrote a sold-out line needs.
    """
    import json
    import unittest.mock as mock

    app, state, node, alice, bob = web
    csrf = state.csrf_token
    form = dict(csrf_token=csrf, sender=alice, name="Lot Token", supply="1000",
                kind="fixed", units="divisible")
    txid = shown(app.post("/tokens/create", data=form).text, "txid")
    app.post("/tokens/create", data={**form, "confirmed": txid}, follow_redirects=False)
    mine_and_index(node, state)
    index = state.token_index(state.ledger)
    (prop,) = index.properties()
    pid = str(prop["property_id"])
    home = index.balances([alice])[0]["address"]

    # An ask of 10 Lot Tokens at 0.2 coins each, so a lot of 5 costs exactly one
    # coin and the book holds two lots of it. The pad page works those out in its
    # script from /r/book; the line above the frame is asked to reach the same
    # numbers, and to say "1 coin" rather than "1 coins" while doing it.
    pad = "e7" * 32
    with mock.patch.object(type(state), "home_address", lambda self, chain: home):
        app.post("/exchange/order", data=dict(csrf_token=csrf, property_id=pid,
                                             side="ask", amount="10", price="0.2"),
                 follow_redirects=False)
        mine_and_index(node, state)
        with index.open() as db:
            db.conn.execute(
                "INSERT OR REPLACE INTO inscription(txid,number,creator,owner,"
                "block_height,position,content_type,content_len,sha256,json,chunks,"
                "content) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (pad, 771, home, home, 100, 0, "text/html", 48, "ab" * 32,
                 json.dumps({"name": "Lot Token mintpad",
                             "tokenpad": {"creator": home, "property_id": int(pid),
                                          "lot": 5 * 10**8, "look": "counter"}}),
                 1, b"<html><body>what a mint costs is written in here</body></html>"))
            db.conn.commit()

    def page() -> str:
        return " ".join(app.get(f"/inscriptions/{pad}/view").text.split())

    said = page()
    assert '<span class="pill ok">mintpad</span>' in said, said
    assert "<strong>5 Lot Token</strong> for <strong>1 coin</strong> a mint" in said, said
    assert "<strong>2</strong> mints left" in said, said
    assert "not from what this page says" in said, "and it says where it came from"

    # The seller takes the ask away: the pad is out of lots, and that is news the
    # node can tell without anybody pressing the pad to find out.
    with mock.patch.object(type(state), "home_address", lambda self, chain: home):
        app.post("/exchange/order/cancel", data=dict(csrf_token=csrf, property_id=pid,
                                                     side="ask"),
                 follow_redirects=False)
        mine_and_index(node, state)
    gone = page()
    assert "<strong>sold out</strong> &mdash; no lot of it left on the book" in gone, gone
    assert "coins</strong> a mint" not in gone, "no price claimed from an order that is gone"


def test_the_mintpads_tab_carries_the_same_count(web):
    """What is left is one fact, so the tiles say it too (a tester addendum 2).

    The tab's token tiles carried lot and creator only, so it could be read as a
    list of what is on the chain and not as a what-is-left list -- while the number
    sat one click away on the pad's own page. It is drawn here from the same
    arithmetic with the pending pool read once for the whole list, so a tile and
    the page it links to cannot tell two stories about the same book.

    The tile count is pieces or mints and never supply: a token pad publishes no
    cap to count against (the fixed/managed split in S48's addendum), which is why
    the tab says which of the two lists is which instead of filling a column it
    cannot answer. And a pad with nothing left stays on the list -- dropping it
    would undo the a tester findability the list exists for.
    """
    import json
    import unittest.mock as mock

    app, state, node, alice, bob = web
    csrf = state.csrf_token
    form = dict(csrf_token=csrf, sender=alice, name="Tab Token", supply="1000",
                kind="fixed", units="divisible")
    txid = shown(app.post("/tokens/create", data=form).text, "txid")
    app.post("/tokens/create", data={**form, "confirmed": txid}, follow_redirects=False)
    mine_and_index(node, state)
    index = state.token_index(state.ledger)
    prop = next(p for p in index.properties() if p["name"] == "Tab Token")
    pid = str(prop["property_id"])
    home = index.balances([alice])[0]["address"]

    pad = "e9" * 32
    with index.open() as db:
        db.conn.execute(
            "INSERT OR REPLACE INTO inscription(txid,number,creator,owner,"
            "block_height,position,content_type,content_len,sha256,json,chunks,"
            "content) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (pad, 772, home, home, 100, 0, "text/html", 48, "ad" * 32,
             json.dumps({"name": "Tab Token mintpad",
                         "tokenpad": {"creator": home, "property_id": int(pid),
                                      "lot": 5 * 10**8, "look": "counter"}}),
             1, b"<html><body>the pad, on the tab</body></html>"))
        db.conn.commit()

    def tab() -> str:
        return " ".join(app.get("/exchange?tab=mintpads").text.split())

    listed = tab()
    assert f'href="/inscriptions/{pad}/view"' in listed, listed
    assert "sold out" in listed, "nothing on the book yet, and it says so rather than lie"
    assert "pieces for a collection, whole mints for a token" in listed, \
        "and it says which of its two lists counts what"

    # Two lots of the lot size on the book, so the tile says two -- the same two
    # the pad page prints above its frame, because it is the same read.
    with mock.patch.object(type(state), "home_address", lambda self, chain: home):
        app.post("/exchange/order", data=dict(csrf_token=csrf, property_id=pid,
                                             side="ask", amount="10", price="0.2"),
                 follow_redirects=False)
        mine_and_index(node, state)
    assert "5 per mint" in tab() and "2 mints left" in tab(), tab()
    assert "&middot; 2 mints left" in app.get("/exchange?tab=mintpads").text, \
        "the separator and the number have to be apart on the page, not just in here"
    assert "<strong>2</strong> mints left" in " ".join(
        app.get(f"/inscriptions/{pad}/view").text.split()), \
        "the tile and the page are counting different things"

    with mock.patch.object(type(state), "home_address", lambda self, chain: home):
        app.post("/exchange/order/cancel", data=dict(csrf_token=csrf, property_id=pid,
                                                     side="ask"),
                 follow_redirects=False)
        mine_and_index(node, state)
    gone = tab()
    assert "mints left" not in gone, gone
    assert f'href="/inscriptions/{pad}/view"' in gone, \
        "sold out is what the tile says, not a reason to hide the pad"

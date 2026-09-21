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
from arcade.script import b58check_encode
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

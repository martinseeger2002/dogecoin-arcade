"""Paying somebody from their profile (2026-11-02, SadFrog: "when visiting someones user profile ... there should be an
option to pay that person right there or send crypto").

The profile only picks the coin. The amount, the review and the signing stay the Wallet's own Send sheet, so these tests
are about what is offered and where it leads; test_profile_pay_browser.py walks a payment through that sheet.
"""

import html
import pathlib
import re
import sys
import urllib.parse

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                                 # noqa: F401,E402

from arcade.config import NETWORKS                                     # noqa: E402
from arcade.db import Database                                         # noqa: E402
from arcade.script import b58check_encode                              # noqa: E402
from arcade.state import install_schema                                # noqa: E402

KEY = bytes(range(32))
HASH = b"\x22" * 20
EDGE = {"host": "node.dogecoinarcade.com", "cf-ray": "abc-LHR"}
LOCAL = {"host": "127.0.0.1:8420"}


def _address(state):
    return b58check_encode(state.messaging.params.pubkeyhash_version, HASH)


def _claim(state, tag, address, table="tag"):
    db = Database(state.home / f"{state.messaging.network}-ledger.sqlite")
    install_schema(db)
    db.conn.execute(f"INSERT INTO {table}(tag,address,claimed_txid,block_height,position) VALUES(?,?,?,100,0)",
                    (tag, address, "t" * 64))
    db.conn.commit()
    db.close()


def _publish(state, address, **said):
    with state.store() as store:
        store.add_key_announcement("aa" * 32, address, KEY, "fp", 10, 1, stated=True, **said)


def _sheet(answer):
    """The Pay sheet of a profile, or "" when the page offers none."""
    assert answer.status_code == 200, answer.text[:300]
    return answer.text[answer.text.index('id="ppay"'):answer.text.index("<style>", answer.text.index('id="ppay"'))] \
        if 'id="ppay"' in answer.text else ""


def _rows(sheet):
    """{network: href} of every coin on a sheet, the links as a browser reads them."""
    return {net: html.unescape(href) for href, net in re.findall(r'<a href="([^"]+)" data-network="([^"]+)"', sheet)}


def _asks(href):
    path, _, query = href.partition("?")
    return path, dict(urllib.parse.parse_qsl(query))


@pytest.fixture
def public(client):
    app, state = client
    state.set_setting("public_hosts", ["node.dogecoinarcade.com"])
    yield app, state
    state.set_setting("public_hosts", [])


@pytest.fixture
def them(public):
    """@them, claimed on the chain, with a Pepecoin main address published."""
    app, state = public
    address = _address(state)
    _claim(state, "them", address)
    _publish(state, address, other_address=b58check_encode(NETWORKS["main"].pubkeyhash_version, HASH))
    return app, state, address


def _sign_in(app):
    from test_me_page import _seat
    return _seat(app)


def test_a_signed_in_account_picks_a_coin_and_lands_on_the_send_sheet_for_that_coin(them):
    app, state, address = them
    _sign_in(app)
    page = app.get("/u/them", headers=EDGE)
    sheet = _sheet(page)
    assert 'id="pay-them"' in page.text
    assert "Sign in to pay" not in sheet
    rows = _rows(sheet)
    assert set(rows) == {"main", state.messaging.network}
    assert list(rows)[0] == "main", "real coins first"
    for net, href in rows.items():
        path, asks = _asks(href)
        assert path == "/me/wallet"
        assert asks == {"to": "@them", "coin": net}, "the name, looked up again at Review as every send is"
    assert address[:8] in sheet and address[-6:] in sheet, "they can see what they are about to pick"


def test_only_a_coin_they_can_be_paid_in_is_offered(public):
    """No Pepecoin main address published: there is nowhere to pay them on main, so main is not on the sheet."""
    app, state = public
    address = _address(state)
    _claim(state, "them", address)
    _publish(state, address)
    _sign_in(app)
    assert set(_rows(_sheet(app.get("/u/them", headers=EDGE)))) == {state.messaging.network}


def test_a_published_address_the_send_would_refuse_is_not_offered(public):
    """What is published is what somebody said. The send holds it to the chain's address bytes, and so does the sheet."""
    app, state = public
    address = _address(state)
    _claim(state, "them", address)
    _publish(state, address, other_address="DThisIsNotAnAddressOnPepecoinMainAtAll")
    _sign_in(app)
    assert "main" not in _rows(_sheet(app.get("/u/them", headers=EDGE)))


def test_the_sheet_offers_what_the_send_would_pay_on_every_coin(them, monkeypatch):
    """Dogecoin's address is the same key's twin, and a syncing Dogecoin is not offered at all."""
    from test_dogecoin_network import _with_doge
    app, state, address = them
    _sign_in(app)
    _with_doge(state, monkeypatch, ready=False)
    assert "doge" not in _rows(_sheet(app.get("/u/them", headers=EDGE))), "nothing is paid on a chain still syncing"
    _with_doge(state, monkeypatch, ready=True)
    sheet = _sheet(app.get("/u/them", headers=EDGE))
    twin = b58check_encode(NETWORKS["doge"].pubkeyhash_version, HASH)
    assert "doge" in _rows(sheet)
    assert twin[:8] in sheet and twin[-6:] in sheet


def test_a_visitor_who_is_signed_out_is_asked_to_sign_in_and_comes_back_to_the_coin(them):
    app, state, address = them
    page = app.get("/u/them", headers=EDGE)
    sheet = _sheet(page)
    assert "Sign in to pay" in sheet
    href = _rows(sheet)["main"]
    answer = app.get(href, headers=EDGE, follow_redirects=False)
    assert answer.status_code == 303
    assert answer.headers["location"] == "/join?next=" + urllib.parse.quote(href, safe="")


def test_you_cannot_pay_yourself(public):
    app, state = public
    pubkey = _sign_in(app)
    address = _address(state)
    _claim(state, "me", address)
    state.set_setting(f"address:{pubkey}", address)
    page = app.get("/u/me", headers=EDGE)
    assert 'id="pay-them"' not in page.text and 'id="ppay"' not in page.text
    assert "Edit profile" in page.text


def test_a_name_still_waiting_for_its_block_is_not_offered(public, monkeypatch):
    """First claim wins, and this one has not yet: the page says "claiming", and nobody is paid under the name."""
    from arcade.ledger import LedgerIndex as TokenIndex
    app, state = public
    address = _address(state)
    monkeypatch.setattr(TokenIndex, "pending_tags", lambda self: [{"address": address, "tag": "soon"}])
    _sign_in(app)
    page = app.get("/u/soon", headers=EDGE)
    assert ">claiming<" in " ".join(page.text.split()).replace("> claiming <", ">claiming<")
    assert 'id="pay-them"' not in page.text and 'id="ppay"' not in page.text


def test_the_operator_pays_from_the_nodes_wallet_to_the_address_shown(them):
    """The node's own send reads an @name's claim on the chain being paid, which on main may be somebody else's or
    nobody's. So the operator's link carries the address the sheet shows, and it opens the node's Wallet."""
    app, state, address = them
    page = app.get("/u/them", headers=LOCAL)
    sheet = _sheet(page)
    assert "Sign in to pay" not in sheet
    rows = _rows(sheet)
    assert rows, sheet
    for net, href in rows.items():
        path, asks = _asks(href)
        assert path == "/wallet"
        assert asks["coin"] == net
        assert not asks["to"].startswith("@")


def test_the_wallet_never_opens_a_send_on_a_coin_nobody_picked():
    """A coin named on the link that is hidden or missing here falls back on another wallet; opening Send there would
    pay in something they did not choose."""
    page = (pathlib.Path(__file__).parent.parent / "arcade/web/templates/my_wallet.html").read_text()
    guard = page[page.index('const asked = new URLSearchParams'):page.index('document.body.dataset.walletReady = "yes"')]
    assert guard.index("coinAsked && chainNow().network !== coinAsked") < guard.index("openSend(asked")

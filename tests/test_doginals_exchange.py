"""The Exchange's Doginals tab and its routes (2026-10-14: "I want doginals to be incorporated into the exchange
on its own separate tab. So people can list doginals for doge."). Dogecoin and Doge Soft are faked: the chain answers
gettxout from a dict, and every coin's Doginals come from `test_doginal_market._guard`."""

import contextlib
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_claim import _sign_in                              # noqa: E402
from test_dogecoin_network import _with_doge                         # noqa: E402
from test_doginal_market import (BUYER, BUYER_KEY, CLEAN_TX, INS_TX, INSCRIPTION, OTHER_INS_TX,  # noqa: E402
                                 SELLER, SELLER_KEY, _guard)
from test_funding import _pubkey, _sign                              # noqa: E402
from test_web import app_state, client                               # noqa: F401,E402

from arcade import doginal_market as M, funding, utxos              # noqa: E402
from arcade.script import b58check_encode                            # noqa: E402
from arcade.txbuild import p2pkh_script                              # noqa: E402

COIN = 100_000_000


class _Doge:
    """Just enough of a Dogecoin node: gettxout from `coins`, and a sendrawtransaction that remembers."""

    def __init__(self):
        self.coins, self.sent = {}, []

    def call(self, method, *args):
        if method == "gettxout":
            got = self.coins.get((args[0], args[1]))
            return None if got is None else {"value": got[0] / COIN, "scriptPubKey": {"hex": got[1].hex()}}
        if method == "sendrawtransaction":
            self.sent.append(args[0])
            return "dd" * 32
        raise AssertionError(method)


@pytest.fixture
def doge(client, monkeypatch, tmp_path):
    app, state = client
    chain = _with_doge(state, monkeypatch, ready=True)
    node = _Doge()
    node.coins[(INS_TX, 0)] = (100_000, p2pkh_script(SELLER))
    monkeypatch.setattr(chain, "rpc", lambda: contextlib.nullcontext(node), raising=False)
    wallets = {SELLER: [{"txid": INS_TX, "vout": 0, "value": 100_000}],
               BUYER: [{"txid": CLEAN_TX, "vout": 0, "value": 2_000_000},
                       {"txid": CLEAN_TX, "vout": 1, "value": 3_000_000},
                       {"txid": CLEAN_TX, "vout": 2, "value": 500 * COIN},
                       {"txid": OTHER_INS_TX, "vout": 0, "value": 100_000}]}
    monkeypatch.setattr(utxos, "unspent", lambda db, address: [dict(c, address=address)
                                                         for c in wallets.get(address, [])])
    monkeypatch.setattr(funding, "GUARD", _guard(tmp_path, {INS_TX: [(0, INSCRIPTION, 0)],
                                                            OTHER_INS_TX: [(0, OTHER_INS_TX + "i0", 0)]}))
    return app, state, chain, node


def _seat(app, state, address):
    pubkey = _sign_in(app)
    state.set_setting(f"address:doge:{pubkey}", address)
    # A wallet on the tag chain too, so the pages are drawn for the account (app._account_view).
    state.set_setting(f"address:{pubkey}", b58check_encode(111, bytes.fromhex(pubkey)[:20]))
    return pubkey


def test_the_tab_is_grayed_until_dogecoin_syncs(client, monkeypatch):
    app, state = client
    _with_doge(state, monkeypatch, ready=False)
    page = app.get("/exchange?tab=tokens").text
    assert '<span class="chain-off" aria-disabled="true" title="Doginals open when the Dogecoin node has synced' in page
    assert "tab=doginals" not in page
    assert "Doginals open here when" in app.get("/exchange?tab=doginals").text


def test_no_dogecoin_no_tab(client):
    app, _ = client
    assert ">Doginals<" not in app.get("/exchange?tab=tokens").text


def test_list_show_and_buy_through_the_routes(doge):
    app, state, chain, node = doge
    _seat(app, state, SELLER)
    ask = app.post("/account/doginals/list", json={"txid": INS_TX, "vout": 0, "inscription": INSCRIPTION,
                                                   "amount": "5"})
    assert ask.status_code == 200, ask.text
    ask = ask.json()
    assert ask["at"] == 2 and ask["digest"] == M.listing_digest(SELLER, ask["coin"], 5 * COIN).hex()
    sig = _sign(SELLER_KEY, bytes.fromhex(ask["digest"]), funding.SINGLE_ANYONECANPAY).hex()
    bad = app.post("/account/doginals/list/sign", json={"txid": INS_TX, "vout": 0, "inscription": INSCRIPTION,
                                                        "amount": "6", "signature": sig,
                                                        "pubkey": _pubkey(SELLER_KEY).hex()})
    assert bad.status_code == 400, "a signature over 5 DOGE does not list at 6"
    filed = app.post("/account/doginals/list/sign", json={"txid": INS_TX, "vout": 0, "inscription": INSCRIPTION,
                                                          "amount": "5", "signature": sig,
                                                          "pubkey": _pubkey(SELLER_KEY).hex()})
    assert filed.status_code == 200, filed.text
    listing = filed.json()["listed"]

    page = app.get("/exchange?tab=doginals").text
    assert f'data-listing="{listing}"' in page and "5 DOGE" in page and 'data-dg-cancel="' in page
    shown = app.get("/doginals/listings").json()
    assert [d["id"] for d in shown["listings"]] == [listing] and "signature" not in shown["listings"][0]

    _seat(app, state, BUYER)
    assert f'data-dg-buy="{listing}"' in app.get("/exchange?tab=doginals").text
    offer = app.post("/account/doginals/buy", json={"listing": listing})
    assert offer.status_code == 200, offer.text
    offer = offer.json()
    assert offer["prepare"] is False and offer["buyer_inputs"] == [0, 1, 3]
    assert [(i["txid"], i["vout"]) for i in offer["inputs"]][:3] == [(CLEAN_TX, 0), (CLEAN_TX, 1), (INS_TX, 0)]
    assert all(i["txid"] != OTHER_INS_TX for i in offer["inputs"]), "the buyer's own Doginal is never spent"
    sigs = [_sign(BUYER_KEY, bytes.fromhex(h)).hex() for h in offer["sighashes"]]
    done = app.post("/account/doginals/buy/sign", json={"listing": listing, "raw": offer["raw"], "signatures": sigs,
                                                        "pubkey": _pubkey(BUYER_KEY).hex()})
    assert done.status_code == 200, done.text
    assert len(node.sent) == 1 and sig in node.sent[0], "the seller's signature is in what went out"
    assert state.doginal_book.get(listing)["status"] == "filled"
    del node.coins[(INS_TX, 0)]
    assert app.get("/doginals/listings").json()["listings"] == []


def test_a_buyer_without_small_coins_is_offered_the_prepare_split(doge, monkeypatch):
    app, state, chain, node = doge
    _seat(app, state, SELLER)
    ask = app.post("/account/doginals/list", json={"txid": INS_TX, "vout": 0, "inscription": INSCRIPTION,
                                                   "amount": "5"}).json()
    sig = _sign(SELLER_KEY, bytes.fromhex(ask["digest"]), funding.SINGLE_ANYONECANPAY).hex()
    listing = app.post("/account/doginals/list/sign", json={"txid": INS_TX, "vout": 0, "inscription": INSCRIPTION,
                                                            "amount": "5", "signature": sig,
                                                            "pubkey": _pubkey(SELLER_KEY).hex()}).json()["listed"]
    monkeypatch.setattr(utxos, "unspent", lambda db, address: [{"txid": CLEAN_TX, "vout": 2, "value": 500 * COIN,
                                                                 "address": BUYER}]
                        if address == BUYER else [])
    _seat(app, state, BUYER)
    offer = app.post("/account/doginals/buy", json={"listing": listing}).json()
    assert offer["prepare"] is True and offer["offer"]
    assert [o for o in offer["inputs"]] and offer["signed_from"] == 0


@pytest.mark.node_trades_default
def test_listing_and_buying_wait_for_node_trades(doge):
    app, state, chain, node = doge
    _seat(app, state, SELLER)
    assert app.post("/account/doginals/list", json={"txid": INS_TX, "vout": 0, "inscription": INSCRIPTION,
                                                    "amount": "5"}).status_code == 403
    assert "Listing and buying Doginals is off on this arcade" in app.get("/exchange?tab=doginals").text

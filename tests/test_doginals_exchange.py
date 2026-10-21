"""The Exchange's Doginals tab and its routes (2026-10-14: "I want doginals to be incorporated into the exchange
on its own separate tab. So people can list doginals for doge."). Dogecoin and the Doginals source are faked: the chain answers
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


def _collection(state, ids, name="Anti Doginal Dogs"):
    """A collection the registry has already settled (tests/test_collection_registry.py reads one off a chain)."""
    import json
    import sqlite3
    root = "ab" * 32
    reg = state.collection_registry()
    with sqlite3.connect(reg.path) as c:
        c.execute("INSERT INTO ext_root(txid, number, publisher, network, status, name, chain, supply, traits, cover) "
                  "VALUES(?, 1, 'nPublisher', 'test', 'active', ?, 'doge', ?, '[\"Fur\"]', ?)",
                  (root, name, len(ids), ids[0]))
        c.executemany("INSERT INTO ext_item(chain, id, root, number, traits) VALUES('doge', ?, ?, ?, ?)",
                      [(i, root, n + 1, json.dumps({"Fur": "Red"})) for n, i in enumerate(ids)])
    return root


def test_a_collection_json_gets_a_row_and_a_page_with_its_listings(doge):
    """2026-10-17: a Doginals collection JSON makes its collection appear with no manual step."""
    app, state, chain, node = doge
    _seat(app, state, SELLER)
    ask = app.post("/account/doginals/list", json={"txid": INS_TX, "vout": 0, "inscription": INSCRIPTION,
                                                   "amount": "5"}).json()
    sig = _sign(SELLER_KEY, bytes.fromhex(ask["digest"]), funding.SINGLE_ANYONECANPAY).hex()
    listing = app.post("/account/doginals/list/sign", json={"txid": INS_TX, "vout": 0, "inscription": INSCRIPTION,
                                                            "amount": "5", "signature": sig,
                                                            "pubkey": _pubkey(SELLER_KEY).hex()}).json()["listed"]
    others = [f"{k:064x}i0" for k in range(1, 70)]
    root = _collection(state, others[:2] + [INSCRIPTION] + others[2:])

    market = app.get("/exchange?tab=market").text
    assert f'href="/exchange/ord/{root}"' in market and "Anti Doginal Dogs" in market and "1 for sale" in market

    _seat(app, state, BUYER)
    page = app.get(f"/exchange/ord/{root}")
    assert page.status_code == 200, page.text
    page = page.text
    assert f'data-dg-buy="{listing}"' in page, "its listing, with the Doginals tab's own Buy"
    assert ">#3 <span" in page and f"/exchange/swap/doge-doginal-{INSCRIPTION}/main-coin" in page
    assert "Page 1 of 3" in page and ">#25<" not in page and ">#25<" in app.get(f"/exchange/ord/{root}?page=2").text
    assert ">#40<" in app.get(f"/exchange/ord/{root}?number=40").text
    assert app.get("/exchange/ord/" + "cd" * 32).status_code == 404


def test_a_game_reads_which_collection_pieces_an_address_holds(doge):
    """2026-10-17: a holder of a collection's Doginal gets something for it in a game. /r/doginals answers for
    the Dogecoin address, or for the same key's Pepecoin address (an account's)."""
    from arcade.script import b58check_decode
    app, state, chain, node = doge
    root = _collection(state, [f"{k:064x}i0" for k in range(1, 3)] + [INSCRIPTION])
    said = app.get(f"/r/doginals/{SELLER}").json()
    assert said["address"] == SELLER and said["unknown"] == 0
    assert said["items"] == [{"inscription": INSCRIPTION, "number": 3, "traits": {"Fur": "Red"},
                              "collection": {"root": root, "name": "Anti Doginal Dogs"}}]
    pubkey = _seat(app, state, SELLER)
    tag_address = state.setting(f"address:{pubkey}")
    assert app.get(f"/r/doginals/{tag_address}").json()["items"] == said["items"], "an account's Pepecoin address"
    stranger = b58check_encode(111, b58check_decode(SELLER)[1])
    assert app.get(f"/r/doginals/{stranger}").status_code == 404, "a testnet key is not the Dogecoin key"
    assert app.get(f"/r/doginals/{BUYER}").json()["items"] == []
    assert app.get(f"/r/collection-item/doge/{INSCRIPTION}").json()["number"] == 3
    assert app.get(f"/r/collection-item/doge/{'ef' * 32}i0").status_code == 404
    # Games read these from the sandboxed frame (origin "null"), like the other /r/ reads: CORS on every answer.
    for path in (f"/r/doginals/{SELLER}", f"/r/doginals/{stranger}", f"/r/collection-item/doge/{INSCRIPTION}",
                 f"/r/collection-item/doge/{'ef' * 32}i0"):
        assert app.get(path).headers.get("access-control-allow-origin") == "*", path


def test_every_account_s_dogecoin_address_is_watched_and_read_back(client, monkeypatch):
    """2026-10-17: only 11 of 97 accounts had told the node their Dogecoin address, so Doginals sent to the
    others were never filed. The watcher derives each from the account's Pepecoin MAINNET key and reads back once."""
    import contextlib as cl
    import threading
    from arcade import utxos
    from arcade.script import b58check_decode
    from arcade.web.watcher import BlockWatcher
    from test_dogecoin_network import _with_doge
    app, state = client
    doge = _with_doge(state, monkeypatch, ready=True)
    main = b58check_encode(56, bytes(range(20)))                 # an account's Pepecoin mainnet address
    state.set_setting("address:main:" + "ab" * 32, main)
    state.set_setting("address:" + "ab" * 32, b58check_encode(111, bytes(range(1, 21))))   # its testnet key: not it
    read = []
    monkeypatch.setattr(utxos, "backfill_many", lambda index, rpc, params, addresses, start, end:
                        read.append((set(addresses), start, end)) or 0)
    monkeypatch.setattr(doge, "rpc", lambda: cl.nullcontext(None), raising=False)
    monkeypatch.setattr(threading, "Thread", lambda target, **kw: type("T", (), {"start": lambda self: target()})())
    watcher = BlockWatcher(state)
    watcher._watch_doge_accounts()
    want = b58check_encode(30, b58check_decode(main)[1])
    with cl.closing(state.token_index(doge).open()) as db:
        assert want in utxos.watching(db)
    assert read and read[0][0] == {want}, "read back once, for the new address only"
    watcher._doge_watch_at = 0
    watcher._watch_doge_accounts()
    assert len(read) == 1, "an address already watched is not read back again"


def test_doginals_at_the_testnet_key_s_dogecoin_address_move_home(doge, monkeypatch):
    """2026-10-17: six ADDs went to @apple's testnet key's Dogecoin address. The wallet moves them home in one
    transaction, each inscribed coin whole to the account's real Dogecoin address, signed with the testnet coin key."""
    import contextlib as cl
    from arcade import utxos as U
    from arcade.script import b58check_decode
    from arcade.txbuild import p2pkh_script
    app, state, chain, node = doge
    pubkey = _seat(app, state, BUYER)                         # home: the account's own Dogecoin address
    state.set_setting(f"address:{pubkey}", b58check_encode(111, b58check_decode(SELLER)[1]))   # its testnet key
    with cl.closing(state.token_index(chain).open()) as db:
        U.watch(db, SELLER, 0, "test")
        db.conn.commit()
    coins = {SELLER: [{"txid": INS_TX, "vout": 0, "value": 100_000}]}
    monkeypatch.setattr(U, "unspent", lambda db, address: [dict(c, address=address) for c in coins.get(address, [])])
    root = _collection(state, [INSCRIPTION])

    said = app.get("/account/doge-stray").json()
    assert said["stray"] == SELLER and said["home"] == BUYER and said["fee_short"] > 0
    assert said["doginals"][0]["names"] == ["Anti Doginal Dogs #1"]
    assert app.post("/account/doge-stray/move", json={}).status_code == 402, "no DOGE there for the fee yet"

    coins[SELLER].append({"txid": CLEAN_TX, "vout": 5, "value": 5 * COIN})       # the top-up landed
    node.coins[(CLEAN_TX, 5)] = (5 * COIN, p2pkh_script(SELLER))
    move = app.post("/account/doge-stray/move", json={}).json()
    assert [(i["txid"], i["value"]) for i in move["inputs"]] == [(INS_TX, 100_000), (CLEAN_TX, 5 * COIN)]
    assert move["outputs"][0] == {"value": 100_000, "script": p2pkh_script(BUYER).hex()}, "the dog, whole, at offset 0"
    assert all(o["script"] == p2pkh_script(BUYER).hex() for o in move["outputs"])
    sigs = [_sign(SELLER_KEY, bytes.fromhex(h)).hex() for h in move["sighashes"]]
    wrong = app.post("/account/doge-stray/move/sign", json={"raw": move["raw"], "signatures": sigs,
                                                             "pubkey": _pubkey(BUYER_KEY).hex()})
    assert wrong.status_code == 400, "only the testnet key behind that address"
    done = app.post("/account/doge-stray/move/sign", json={"raw": move["raw"], "signatures": sigs,
                                                            "pubkey": _pubkey(SELLER_KEY).hex()})
    assert done.status_code == 200, done.text
    assert len(node.sent) == 1 and sigs[0] in node.sent[0] and done.json()["moved"] == 1


def test_a_collection_page_loads_its_pieces_one_at_a_time(doge):
    """2026-10-17: "The collection page should load one piece at a time": no frame has a src when the page
    arrives; the page's script gives them one after another."""
    import re
    app, state, chain, node = doge
    root = _collection(state, [f"{k:064x}i0" for k in range(1, 30)])
    page = app.get(f"/exchange/ord/{root}").text
    frames = re.findall(r"<iframe[^>]*>", page)
    assert len(frames) == 25 and all("data-src=" in f and " src=" not in f for f in frames), frames[:2]
    assert 'querySelectorAll("iframe[data-src]")' in page

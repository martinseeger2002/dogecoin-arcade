"""Luckinals, Wojakinals, Litecoin ords and Pepecoin testnet's Pepinals moved like Bellinals (2026-10-25, Arcade
Assets A4): sent by sat order, listed on each chain's own Exchange tab and bought in one transaction, each chain's
coins read by its own guard and priced at its own rate. Swapped where a lock can be made (Luckycoin, Litecoin, the two
testnets against each other); Wojakinals never (no CLTV).

The chains and guards are faked; the end-to-end test at the bottom runs on a real litecoind regtest
node (the listing digest, SINGLE|ANYONECANPAY, as the node checks it)."""

import contextlib
import dataclasses
import os
import pathlib
import shutil
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_claim import _sign_in                                                # noqa: E402
from test_bells_luckycoin import _with                                                 # noqa: E402
from test_doginal_market import CLEAN_TX, INS_TX, INSCRIPTION, OTHER_INS_TX, _guard, _where   # noqa: E402
from test_doginals_exchange import _Doge                                               # noqa: E402
from test_funding import _pubkey, _sign                                                # noqa: E402
from test_web import app_state, client                                                 # noqa: F401,E402

from arcade import doginal_market as M, fees, funding, ord_chains, trade_messages, trade_orders, utxos   # noqa: E402
from arcade.config import NETWORKS                                                     # noqa: E402
from arcade.script import b58check_decode, b58check_encode, hash160                    # noqa: E402
from arcade.txbuild import p2pkh_script                                                # noqa: E402

COIN = 100_000_000
SELLER_KEY, BUYER_KEY = 0x5E11E4, 0xB0B
NEW = ("lky", "wjk", "ltc", "ltctest")


def _addr(net, key):
    return b58check_encode(NETWORKS[net].pubkeyhash_version, hash160(_pubkey(key)))


# --- the shared definition ------------------------------------------------------------------------------------------

def test_every_ord_chain_is_moved_on_a_tab_of_its_own():
    assert set(ord_chains.MOVED) == set(ord_chains.WORDS) - {"kot"}
    tabs = ord_chains.tab_nets()
    assert len(tabs) == len(ord_chains.MOVED), "no two chains share an Exchange tab"
    assert (tabs["luckinals"], tabs["wojakinals"], tabs["ltcordinals"], tabs["ltctestordinals"],
            tabs["pepinals"], tabs["pepinalstest"]) == ("lky", "wjk", "ltc", "ltctest", "main", "test")
    assert all(t.isalnum() for t in tabs), "a tab is a plain URL word"
    assert ord_chains.word("ltc") == "LTC ordinal" and ord_chains.word("lky") == "Luckinal"


def test_wojakinals_are_never_swapped_and_testnets_price_against_testnets():
    assert {n: ord_chains.swappable(n) for n in NEW} == {"lky": True, "wjk": False, "ltc": True, "ltctest": True}
    assert (ord_chains.counter("ltctest"), ord_chains.counter("test")) == ("test", "ltctest")
    assert (ord_chains.counter("ltc"), ord_chains.counter("lky")) == ("doge", "doge")
    table = ord_chains.wallet_table()
    assert table["test"]["send"] and table["test"]["tab"] == "pepinalstest", "Pepecoin testnet's ords shown too"
    leg = {"kind": "doginal", "asset": INSCRIPTION, "amount": 1, "giver_key": "02" + "11" * 32,
           "taker_key": "03" + "22" * 32}
    for net in ("lky", "ltc"):
        assert trade_messages.leg_from({**leg, "network": net}).network == net
        assert trade_orders._side({**leg, "network": net})["network"] == net
    with pytest.raises(trade_messages.TradeMessageError):
        trade_messages.leg_from({**leg, "network": "wjk"})
    with pytest.raises(trade_orders.OrderError):
        trade_orders._side({**leg, "network": "wjk"})


# --- the builders, each chain at its own rate -----------------------------------------------------------------------

def _coins(tmp_path, monkeypatch, net):
    seller, buyer = _addr(net, SELLER_KEY), _addr(net, BUYER_KEY)
    wallets = {seller: [{"txid": INS_TX, "vout": 0, "value": 100_000}, {"txid": CLEAN_TX, "vout": 9, "value": 3 * COIN}],
               buyer: [{"txid": CLEAN_TX, "vout": 0, "value": 2_000_000}, {"txid": CLEAN_TX, "vout": 1, "value": 3_000_000},
                       {"txid": CLEAN_TX, "vout": 2, "value": 500 * COIN},
                       {"txid": OTHER_INS_TX, "vout": 1, "value": 1_500_000}]}
    monkeypatch.setattr(utxos, "unspent", lambda db, address: [dict(c, address=address) for c in wallets.get(address, [])])
    guard = _guard(tmp_path, {INS_TX: [(0, INSCRIPTION, 0)], OTHER_INS_TX: [(1, OTHER_INS_TX + "i1", 0)]})
    (tmp_path / "doge").mkdir(exist_ok=True)
    monkeypatch.setattr(funding, "GUARD", _guard(tmp_path / "doge", {}))
    version = NETWORKS[net].pubkeyhash_version
    monkeypatch.setattr(funding, "GUARDS", [(lambda a: b58check_decode(a)[0] == version, guard)])
    monkeypatch.setattr(funding, "GUARD_BY_NET", {"doge": funding.GUARD, net: guard})
    return seller, buyer


@pytest.mark.parametrize("net", NEW)
def test_a_send_and_a_purchase_are_sat_exact_at_the_chains_own_rate(net, tmp_path, monkeypatch):
    params = NETWORKS[net]
    seller, buyer = _coins(tmp_path, monkeypatch, net)
    send = funding.build_doginal_send(None, params, seller, {"txid": INS_TX, "vout": 0, "value": 100_000},
                                      INSCRIPTION, buyer, rate=fees.MIN_FEE_PER_KB)
    assert (send.inputs[0]["txid"], send.inputs[0]["vout"]) == (INS_TX, 0) and _where(send.inputs, send.outputs, 0) == (0, 0)
    assert [(c["txid"], c["vout"]) for c in send.inputs[1:]] == [(CLEAN_TX, 9)], "the fee from a clean coin only"
    assert send.fee == funding.price(len(send.inputs), send.outputs[:1], params.min_fee_per_kb, change=True,
                                     params=params)
    ask = M.build_ask(seller, {"txid": INS_TX, "vout": 0, "value": 100_000}, INSCRIPTION, 7 * COIN, params)
    assert ask.what == f"list {ord_chains.word(net)} {INSCRIPTION} for 7 {params.coin}"
    sig = _sign(SELLER_KEY, bytes.fromhex(ask.digest), funding.SINGLE_ANYONECANPAY)
    row = {"id": "L1", "seller": seller, "inscription": INSCRIPTION, "price": 7 * COIN, "coin": ask.coin,
           "signature": sig.hex(), "pubkey": _pubkey(SELLER_KEY).hex()}
    p = M.build_purchase(None, params, buyer, row, rate=fees.MIN_FEE_PER_KB)
    assert _where(p.inputs, p.outputs, 2) == (1, 0), "the ord lands at output 1, offset 0, the buyer's"
    assert all((i["txid"], i["vout"]) != (OTHER_INS_TX, 1) for i in p.inputs), "the buyer's own ord never pays"
    whole = funding.sighash(p.inputs, p.outputs, 2, p2pkh_script(seller), sighash_type=funding.SINGLE_ANYONECANPAY)
    assert whole.hex() == ask.digest


@pytest.mark.parametrize("net", NEW)
def test_an_ord_of_another_chain_is_never_checked_by_dogecoins_guard(net, tmp_path, monkeypatch):
    seller, buyer = _coins(tmp_path, monkeypatch, net)
    monkeypatch.setattr(funding, "GUARDS", [])                     # this node reads none of that chain's ords
    with pytest.raises(funding.FundingError, match="not checked on this chain"):
        funding.build_doginal_send(None, NETWORKS[net], seller, {"txid": INS_TX, "vout": 0, "value": 100_000},
                                   INSCRIPTION, buyer, rate=fees.MIN_FEE_PER_KB)


# --- the routes -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("net", NEW)
def test_sent_listed_shown_and_bought_through_the_routes(net, client, monkeypatch, tmp_path):
    app, state = client
    seller, buyer = _coins(tmp_path, monkeypatch, net)
    chain, = _with(state, monkeypatch, net)
    node = _Doge()
    node.coins[(INS_TX, 0)] = (100_000, p2pkh_script(seller))
    monkeypatch.setattr(chain, "rpc", lambda: contextlib.nullcontext(node), raising=False)
    coin, word, tab = NETWORKS[net].coin, ord_chains.word(net), ord_chains.tab(net)

    def seat(address):
        pubkey = _sign_in(app)
        state.set_setting(f"address:{net}:{pubkey}", address)
        state.set_setting(f"address:{pubkey}", b58check_encode(111, bytes.fromhex(pubkey)[:20]))

    seat(seller)
    mine = app.get(f"/account/doginals?chain={net}")
    assert mine.status_code == 200, mine.text
    assert [(d["txid"], d["listable"]) for d in mine.json()["doginals"]] == [(INS_TX, True)]
    sent = app.post("/account/doginal/send", json={"chain": net, "txid": INS_TX, "vout": 0,
                                                   "inscription": INSCRIPTION, "to": buyer})
    assert sent.status_code == 200, sent.text
    assert [(i["txid"], i["vout"]) for i in sent.json()["inputs"]] == [(INS_TX, 0), (CLEAN_TX, 9)]

    ask = app.post("/account/doginals/list", json={"chain": net, "txid": INS_TX, "vout": 0,
                                                   "inscription": INSCRIPTION, "amount": "5"})
    assert ask.status_code == 200, ask.text
    sig = _sign(SELLER_KEY, bytes.fromhex(ask.json()["digest"]), funding.SINGLE_ANYONECANPAY).hex()
    filed = app.post("/account/doginals/list/sign", json={"chain": net, "txid": INS_TX, "vout": 0,
                                                          "inscription": INSCRIPTION, "amount": "5", "signature": sig,
                                                          "pubkey": _pubkey(SELLER_KEY).hex()})
    assert filed.status_code == 200, filed.text
    listing = filed.json()["listed"]
    assert [d["id"] for d in app.get(f"/doginals/listings?chain={net}").json()["listings"]] == [listing]
    assert app.get("/doginals/listings?chain=doge").json()["listings"] == [], "Dogecoin's book is its own"
    assert f'href="/exchange?tab={tab}"' in app.get("/exchange?tab=tokens").text
    page = app.get(f"/exchange?tab={tab}").text
    assert f"{word}s listed for {coin}" in page and f'data-listing="{listing}"' in page
    assert f"const SWAP = {'true' if ord_chains.swappable(net) else 'false'}" in page

    seat(buyer)
    offer = app.post("/account/doginals/buy", json={"chain": net, "listing": listing})
    assert offer.status_code == 200, offer.text
    offer = offer.json()
    sigs = [_sign(BUYER_KEY, bytes.fromhex(h)).hex() for h in offer["sighashes"]]
    done = app.post("/account/doginals/buy/sign", json={"chain": net, "listing": listing, "raw": offer["raw"],
                                                        "signatures": sigs, "pubkey": _pubkey(BUYER_KEY).hex()})
    assert done.status_code == 200, done.text
    assert len(node.sent) == 1 and sig in node.sent[0]
    assert state.doginal_book.get(listing)["status"] == "filled"


# --- end to end, on real nodes ----------------------------------------------------------------------------------------

def _regtest(binary, cli, extra=()):
    from arcade.regtest import RegtestNode
    os.environ["PATH"] = str(pathlib.Path.home() / ".local/bin") + os.pathsep + os.environ.get("PATH", "")
    if shutil.which(binary) is None:
        pytest.skip(f"{binary} is not installed")
    node = RegtestNode(binary=binary, cli=cli, require_standard=True, extra_args=extra)
    try:
        node.start(timeout=90)
        if not node.rpc.call("listwallets"):
            node.rpc.call("createwallet", "arcade")
    except Exception as exc:                                            # noqa: BLE001
        node.stop()
        pytest.skip(f"{binary} regtest unavailable: {exc}")
    node.generate(110)                  # a few mature coinbases: Litecoin's are 50 coins each
    return node


# Litecoin only: Luckycoin 5's regtest takes minutes a block (2026-10-25), too slow for a suite, so Luckinals are
# proven with the fakes above (Luckycoin signs legacy inputs as Dogecoin does).
@pytest.fixture(scope="module", params=[("ltc", "litecoind", "litecoin-cli",
                                         ("-addresstype=legacy", "-changetype=legacy"))],
                ids=["litecoin"])
def regtest_chain(request):
    net, binary, cli, extra = request.param
    node = _regtest(binary, cli, extra)
    yield net, node
    node.stop()


def test_send_list_and_buy_an_ord_on_regtest(regtest_chain, tmp_path, monkeypatch):
    net, chain_node = regtest_chain
    rpc = chain_node.rpc
    version = b58check_decode(rpc.call("getnewaddress", "", "legacy"))[0]
    params = dataclasses.replace(NETWORKS[net], pubkeyhash_version=version)   # the chain's rules, regtest's byte
    seller, buyer, friend = (b58check_encode(version, hash160(_pubkey(k))) for k in (SELLER_KEY, BUYER_KEY, 0xF00D))
    known: dict[str, list] = {seller: [], buyer: [], friend: []}

    def watch(txid):
        for o in rpc.call("getrawtransaction", txid, True)["vout"]:
            got = o["scriptPubKey"].get("address") or (o["scriptPubKey"].get("addresses") or [""])[0]
            if got in known:
                known[got].append({"txid": txid, "vout": o["n"], "value": int(round(o["value"] * COIN)), "height": 1})

    def unspent(db, address):
        return [dict(c, address=address) for c in known.get(address, [])
                if rpc.call("gettxout", c["txid"], c["vout"], True)]
    monkeypatch.setattr(utxos, "unspent", unspent)
    for a, amount in ((seller, 10), (buyer, 50)):
        watch(rpc.call("sendtoaddress", a, amount))
    chain_node.generate(1)

    def signed(made, key):
        return funding.assemble(made, [_sign(key, bytes.fromhex(h)).hex() for h in made.sighashes], _pubkey(key))

    # The "inscribed" coin, read by a faked guard as one ord; then sent whole to a friend, its fee from clean coins.
    monkeypatch.setattr(funding, "GUARD", None)
    monkeypatch.setattr(funding, "GUARDS", [])
    made = funding.build(None, params, seller, [(100_000, p2pkh_script(seller))], rate=fees.MIN_FEE_PER_KB)
    ins_txid = rpc.call("sendrawtransaction", signed(made, SELLER_KEY))
    watch(ins_txid)
    chain_node.generate(1)
    inscription = ins_txid + "i0"
    guard = _guard(tmp_path, {ins_txid: [(0, inscription, 0)]})
    monkeypatch.setattr(funding, "GUARDS", [(lambda a: b58check_decode(a)[0] == version, guard)])
    monkeypatch.setattr(funding, "GUARD_BY_NET", {net: guard})
    send = funding.build_doginal_send(None, params, seller, {"txid": ins_txid, "vout": 0, "value": 100_000},
                                      inscription, friend, rate=fees.MIN_FEE_PER_KB)
    sent = rpc.call("sendrawtransaction", signed(send, SELLER_KEY))
    watch(sent)
    chain_node.generate(1)
    got = rpc.call("getrawtransaction", sent, True)
    assert (got["vin"][0]["txid"], got["vin"][0]["vout"]) == (ins_txid, 0)
    assert round(got["vout"][0]["value"] * COIN) == 100_000

    # Listed by the friend; the buyer prepares two dummies, then buys it in one transaction the node accepts.
    (tmp_path / "b").mkdir()
    guard = _guard(tmp_path / "b", {sent: [(0, inscription, 0)]})
    monkeypatch.setattr(funding, "GUARDS", [(lambda a: b58check_decode(a)[0] == version, guard)])
    coin = {"txid": sent, "vout": 0, "value": 100_000}
    ask = M.build_ask(friend, coin, inscription, 7 * COIN, params)
    sig = _sign(0xF00D, bytes.fromhex(ask.digest), funding.SINGLE_ANYONECANPAY)
    book = M.Book(tmp_path / "book.sqlite")
    row = book.register(rpc, network=net, seller=friend, coin=coin, inscription=inscription, price=7 * COIN,
                        signature=sig.hex(), pubkey=_pubkey(0xF00D), params=params)
    with pytest.raises(M.NeedDummies):
        M.build_purchase(None, params, buyer, row, rate=fees.MIN_FEE_PER_KB)
    prep = M.build_prepare(None, params, buyer, rate=fees.MIN_FEE_PER_KB)
    watch(rpc.call("sendrawtransaction", signed(prep, BUYER_KEY)))
    chain_node.generate(1)
    p = M.build_purchase(None, params, buyer, row, rate=fees.MIN_FEE_PER_KB)
    raw = M.assemble_purchase(p, row, [_sign(BUYER_KEY, bytes.fromhex(h)).hex() for h in p.sighashes],
                              _pubkey(BUYER_KEY))
    txid = rpc.call("sendrawtransaction", raw)
    chain_node.generate(1)
    got = rpc.call("getrawtransaction", txid, True)
    assert got["confirmations"] == 1 and (got["vin"][2]["txid"], got["vin"][2]["vout"]) == (sent, 0)
    pays = [o["scriptPubKey"].get("address") or (o["scriptPubKey"].get("addresses") or [""])[0] for o in got["vout"]]
    assert pays[1] == buyer and round(got["vout"][1]["value"] * COIN) == 100_000, "the ord, output 1, the buyer's"
    assert pays[2] == friend and round(got["vout"][2]["value"] * COIN) == 7 * COIN, "the price, to the seller"
    assert book.live(rpc, net) == []

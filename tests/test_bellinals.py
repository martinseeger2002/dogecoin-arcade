"""Bellinals (Bellscoin ords) moved like Doginals and Pepinals (2026-10-25): sent by koinu order, listed on the
Exchange's Bellinals tab for BEL and bought in one transaction, each Bellscoin coin read by Bellscoin's own guard and
priced at Bells' own fee rate. Not swapped: a swap lock is a P2SH address, and Bellscoin's P2SH byte is Dogecoin's "D"
(htlc.address_of refuses it), so no lock is offered there until that is lifted. Wojakcoin never: no CLTV.

The chain and its guard are faked here; the end-to-end test at the bottom runs on a real bellsd regtest node."""

import contextlib
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
BEL = NETWORKS["bel"]
SELLER_KEY, BUYER_KEY = 0x5E11E4, 0xB0B
SELLER = b58check_encode(25, hash160(_pubkey(SELLER_KEY)))      # "B..."
BUYER = b58check_encode(25, hash160(_pubkey(BUYER_KEY)))
DOGE_TWIN = b58check_encode(30, hash160(_pubkey(BUYER_KEY)))    # the same key's Dogecoin "D...", = a Bells P2SH string


def _bel_guard(tmp_path, monkeypatch, inscribed):
    """Bellscoin's guard, watching "B..." addresses only (as create_app sets it up), and an empty Dogecoin one."""
    guard = _guard(tmp_path, inscribed)
    (tmp_path / "doge").mkdir(exist_ok=True)
    monkeypatch.setattr(funding, "GUARD", _guard(tmp_path / "doge", {}))
    monkeypatch.setattr(funding, "GUARDS", [(lambda a: b58check_decode(a)[0] == 25, guard)])
    monkeypatch.setattr(funding, "GUARD_BY_NET", {"doge": funding.GUARD, "bel": guard})
    return guard


@pytest.fixture
def bel_coins(tmp_path, monkeypatch):
    wallets = {SELLER: [{"txid": INS_TX, "vout": 0, "value": 100_000}, {"txid": CLEAN_TX, "vout": 9, "value": 3 * COIN}],
               BUYER: [{"txid": CLEAN_TX, "vout": 0, "value": 2_000_000}, {"txid": CLEAN_TX, "vout": 1, "value": 3_000_000},
                       {"txid": CLEAN_TX, "vout": 2, "value": 500 * COIN},
                       {"txid": OTHER_INS_TX, "vout": 1, "value": 1_500_000}]}
    monkeypatch.setattr(utxos, "unspent", lambda db, address: [dict(c, address=address) for c in wallets.get(address, [])])
    _bel_guard(tmp_path, monkeypatch, {INS_TX: [(0, INSCRIPTION, 0)], OTHER_INS_TX: [(1, OTHER_INS_TX + "i1", 0)]})
    return wallets


# --- the shared definition ------------------------------------------------------------------------------------------

def test_bellinals_are_moved_and_not_swapped():
    assert ord_chains.moved("bel") and ord_chains.tab("bel") == "bellinals" and ord_chains.word("bel") == "Bellinal"
    assert BEL.cltv_enforced, "Bells enforces CHECKLOCKTIMEVERIFY"
    assert not ord_chains.swappable("bel"), "but its lock address cannot be made (P2SH byte 30 is Dogecoin's D)"
    table = ord_chains.wallet_table()
    assert (table["bel"]["send"], table["bel"]["list"], table["bel"]["swap"]) == (True, True, False)
    assert (table["wjk"]["send"], table["wjk"]["swap"]) == (True, False), "Wojakinals moved, never swapped (no CLTV)"


def test_a_bellinal_leg_waits_for_a_bells_lock_and_a_wojakinal_never_gets_one(monkeypatch):
    leg = {"kind": "doginal", "asset": INSCRIPTION, "amount": 1, "giver_key": "02" + "11" * 32,
           "taker_key": "03" + "22" * 32}
    for net in ("bel", "wjk"):
        with pytest.raises(trade_messages.TradeMessageError):
            trade_messages.leg_from({**leg, "network": net})
        with pytest.raises(trade_orders.OrderError):
            trade_orders._side({**leg, "network": net})
    # The day a Bells P2SH address can be told from a Dogecoin one (config.require_p2sh_payable lifted), the gate opens
    # by itself; Wojakcoin's stays shut, CLTV being a no-op there.
    monkeypatch.setattr(ord_chains, "unpayable_versions", lambda params: frozenset())
    assert trade_messages.leg_from({**leg, "network": "bel"}).network == "bel"
    assert trade_orders._side({**leg, "network": "bel"})["network"] == "bel"
    with pytest.raises(trade_messages.TradeMessageError):
        trade_messages.leg_from({**leg, "network": "wjk"})


# --- the builders -----------------------------------------------------------------------------------------------------

def test_a_bellinal_send_is_sat_exact_and_paid_from_clean_coins_at_bells_rate(bel_coins):
    send = funding.build_doginal_send(None, BEL, SELLER, {"txid": INS_TX, "vout": 0, "value": 100_000}, INSCRIPTION,
                                      BUYER, rate=fees.MIN_FEE_PER_KB)
    assert (send.inputs[0]["txid"], send.inputs[0]["vout"]) == (INS_TX, 0), "the inscribed coin is input 0"
    assert send.outputs[0] == (100_000, p2pkh_script(BUYER)), "whole, to the recipient, output 0"
    assert _where(send.inputs, send.outputs, 0) == (0, 0), "its first koinu is output 0's first koinu"
    assert [(c["txid"], c["vout"]) for c in send.inputs[1:]] == [(CLEAN_TX, 9)], "the fee from a clean coin only"
    assert send.outputs[-1] == (send.change, p2pkh_script(SELLER)), "change last, home"
    assert send.fee == funding.price(len(send.inputs), send.outputs[:1], BEL.min_fee_per_kb, change=True, params=BEL)
    assert send.fee < 10_000, "0.0001 BEL a kB, no soft-dust fee"


def test_a_bellinal_is_never_checked_by_dogecoins_guard(tmp_path, monkeypatch, bel_coins):
    monkeypatch.setattr(funding, "GUARDS", [])                     # this node reads no Bellscoin ords
    with pytest.raises(funding.FundingError, match="not checked on this chain"):
        funding.build_doginal_send(None, BEL, SELLER, {"txid": INS_TX, "vout": 0, "value": 100_000}, INSCRIPTION,
                                   BUYER, rate=fees.MIN_FEE_PER_KB)
    with pytest.raises(M.MarketError, match="not checked"):
        M.build_ask(SELLER, {"txid": INS_TX, "vout": 0, "value": 100_000}, INSCRIPTION, COIN, BEL)


def test_a_bells_address_finds_bells_guard_and_a_d_address_dogecoins(tmp_path, monkeypatch, bel_coins):
    assert funding.guard_for(SELLER) is funding.GUARD_BY_NET["bel"]
    assert funding.guard_for(DOGE_TWIN) is funding.GUARD, "a D... is Dogecoin's, never a Bells script hash"


def test_a_bellinal_is_listed_for_bel_and_bought_by_koinu_order(bel_coins):
    ask = M.build_ask(SELLER, {"txid": INS_TX, "vout": 0, "value": 100_000}, INSCRIPTION, 7 * COIN, BEL)
    assert ask.what == f"list Bellinal {INSCRIPTION} for 7 BEL"
    sig = _sign(SELLER_KEY, bytes.fromhex(ask.digest), funding.SINGLE_ANYONECANPAY)
    row = {"id": "L1", "seller": SELLER, "inscription": INSCRIPTION, "price": 7 * COIN, "coin": ask.coin,
           "signature": sig.hex(), "pubkey": _pubkey(SELLER_KEY).hex()}
    p = M.build_purchase(None, BEL, BUYER, row, rate=fees.MIN_FEE_PER_KB)
    assert p.what.endswith("7 BEL") and "Bellinal" in p.what
    assert _where(p.inputs, p.outputs, 2) == (1, 0), "the Bellinal lands at output 1, offset 0, the buyer's"
    assert p.outputs[2] == (7 * COIN, p2pkh_script(SELLER))
    assert all((i["txid"], i["vout"]) != (OTHER_INS_TX, 1) for i in p.inputs), "the buyer's own Bellinal never pays"
    assert p.fee == funding.price(len(p.inputs), p.outputs[:3], BEL.min_fee_per_kb, change=True, params=BEL)
    whole = funding.sighash(p.inputs, p.outputs, 2, p2pkh_script(SELLER), sighash_type=funding.SINGLE_ANYONECANPAY)
    assert whole.hex() == ask.digest
    raw = M.assemble_purchase(p, row, [_sign(BUYER_KEY, bytes.fromhex(h)).hex() for h in p.sighashes],
                              _pubkey(BUYER_KEY))
    assert row["signature"] in raw


# --- the routes -------------------------------------------------------------------------------------------------------

@pytest.fixture
def bel(client, monkeypatch, tmp_path, bel_coins):
    app, state = client
    chain, = _with(state, monkeypatch, "bel")
    node = _Doge()
    node.coins[(INS_TX, 0)] = (100_000, p2pkh_script(SELLER))
    monkeypatch.setattr(chain, "rpc", lambda: contextlib.nullcontext(node), raising=False)
    return app, state, chain, node


def _seat(app, state, address):
    pubkey = _sign_in(app)
    state.set_setting(f"address:bel:{pubkey}", address)
    state.set_setting(f"address:{pubkey}", b58check_encode(111, bytes.fromhex(pubkey)[:20]))
    return pubkey


def test_a_bellinal_is_sent_through_the_route(bel):
    app, state, _, _ = bel
    _seat(app, state, SELLER)
    mine = app.get("/account/doginals?chain=bel")
    assert mine.status_code == 200, mine.text
    assert [(d["txid"], d["listable"]) for d in mine.json()["doginals"]] == [(INS_TX, True)]
    got = app.post("/account/doginal/send", json={"chain": "bel", "txid": INS_TX, "vout": 0,
                                                  "inscription": INSCRIPTION, "to": BUYER})
    assert got.status_code == 200, got.text
    offer = got.json()
    assert offer["chain"] == "bel" and offer["amount"] == 100_000
    assert [(i["txid"], i["vout"]) for i in offer["inputs"]] == [(INS_TX, 0), (CLEAN_TX, 9)]
    refused = app.post("/account/doginal/send", json={"chain": "bel", "txid": INS_TX, "vout": 0,
                                                      "inscription": INSCRIPTION, "to": DOGE_TWIN})
    assert refused.status_code == 400 and "script (P2SH)" in refused.json()["detail"], \
        "a D... is never paid on Bellscoin"
    row = next(c for c in app.get("/account").json()["chains"] if c["network"] == "bel")
    assert 0 < row["doginal_send_need"] < 10_000, "what a send needs, at Bells' rate"


def test_a_bellinal_is_listed_shown_bought_and_taken_back_through_the_routes(bel):
    app, state, _, node = bel
    _seat(app, state, SELLER)
    ask = app.post("/account/doginals/list", json={"chain": "bel", "txid": INS_TX, "vout": 0,
                                                   "inscription": INSCRIPTION, "amount": "5"})
    assert ask.status_code == 200, ask.text
    ask = ask.json()
    assert ask["chain"] == "bel" and ask["digest"] == M.listing_digest(SELLER, ask["coin"], 5 * COIN).hex()
    sig = _sign(SELLER_KEY, bytes.fromhex(ask["digest"]), funding.SINGLE_ANYONECANPAY).hex()
    filed = app.post("/account/doginals/list/sign", json={"chain": "bel", "txid": INS_TX, "vout": 0,
                                                          "inscription": INSCRIPTION, "amount": "5", "signature": sig,
                                                          "pubkey": _pubkey(SELLER_KEY).hex()})
    assert filed.status_code == 200, filed.text
    listing = filed.json()["listed"]
    assert app.get("/doginals/listings?chain=doge").json()["listings"] == [], "Dogecoin's book is its own"
    assert [d["id"] for d in app.get("/doginals/listings?chain=bel").json()["listings"]] == [listing]

    tabs = app.get("/exchange?tab=tokens").text
    assert 'href="/exchange?tab=bellinals"' in tabs and ">Bellinals<" in tabs
    page = app.get("/exchange?tab=bellinals").text
    assert "Bellinals listed for BEL" in page and f'data-listing="{listing}"' in page and "5 BEL" in page
    assert "const SWAP = false" in page, "no lock-script pricing on Bellscoin"
    assert app.post("/account/doginal/send", json={"chain": "bel", "txid": INS_TX, "vout": 0,
                                                   "inscription": INSCRIPTION, "to": BUYER}).status_code == 400, \
        "held while it is listed"

    # Taken back: a send of it home.
    back = app.post("/account/doginals/cancel", json={"chain": "bel", "listing": listing})
    assert back.status_code == 200, back.text
    back = back.json()
    assert back["inputs"][0] == {"txid": INS_TX, "vout": 0, "value": 100_000, "address": SELLER}
    assert "Bellinal" in back["what"] and (p2pkh_script(SELLER).hex() in back["raw"])

    _seat(app, state, BUYER)
    offer = app.post("/account/doginals/buy", json={"chain": "bel", "listing": listing})
    assert offer.status_code == 200, offer.text
    offer = offer.json()
    assert offer["prepare"] is False and offer["chain"] == "bel"
    assert [(i["txid"], i["vout"]) for i in offer["inputs"]][:3] == [(CLEAN_TX, 0), (CLEAN_TX, 1), (INS_TX, 0)]
    sigs = [_sign(BUYER_KEY, bytes.fromhex(h)).hex() for h in offer["sighashes"]]
    done = app.post("/account/doginals/buy/sign", json={"chain": "bel", "listing": listing, "raw": offer["raw"],
                                                        "signatures": sigs, "pubkey": _pubkey(BUYER_KEY).hex()})
    assert done.status_code == 200, done.text
    assert len(node.sent) == 1 and sig in node.sent[0]
    assert state.doginal_book.get(listing)["status"] == "filled"


def test_the_wallet_offers_send_and_list_for_a_bellinal_and_no_swap(bel):
    app, state, _, _ = bel
    _seat(app, state, SELLER)
    page = app.get("/me/wallet").text
    assert '"bel": {"counter": "doge", "list": true, "send": true, "swap": false, "tab": "bellinals"' in page
    assert "const viewOnly = !can.send;" in page and "if (can.swap) {" in page


# --- end to end, on a real Bellscoin ------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def bellsd():
    import os

    from arcade.regtest import RegtestNode
    os.environ["PATH"] = str(pathlib.Path.home() / ".local/bin") + os.pathsep + os.environ.get("PATH", "")
    if shutil.which("bellsd") is None:
        pytest.skip("bellsd is not installed")
    # -acceptnonstdtxn=0: mainnet's relay rules. Legacy addresses: the arcade pays and signs P2PKH on Bellscoin.
    node = RegtestNode(binary="bellsd", cli="bells-cli", require_standard=True,
                       extra_args=("-addresstype=legacy", "-changetype=legacy"))
    try:
        node.start(timeout=90)
        if not node.rpc.call("listwallets"):
            node.rpc.call("createwallet", "arcade")
    except Exception as exc:                                            # noqa: BLE001
        node.stop()
        pytest.skip(f"bellsd regtest unavailable: {exc}")
    node.generate(101)
    yield node
    node.stop()


def test_send_list_and_buy_a_bellinal_on_bells_regtest(bellsd, tmp_path, monkeypatch):
    import dataclasses
    rpc = bellsd.rpc
    version = b58check_decode(rpc.call("getnewaddress", "", "legacy"))[0]
    params = dataclasses.replace(BEL, pubkeyhash_version=version)       # Bellscoin's rules, regtest's address byte
    seller = b58check_encode(version, hash160(_pubkey(SELLER_KEY)))
    buyer = b58check_encode(version, hash160(_pubkey(BUYER_KEY)))
    friend = b58check_encode(version, hash160(_pubkey(0xF00D)))
    known: dict[str, list] = {seller: [], buyer: [], friend: []}

    def watch(txid):
        """Note each output of `txid` that pays one of ours (the node's wallet does not hold these keys)."""
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
    bellsd.generate(1)

    # The "inscribed" coin: 100,000 koinu made by the seller, read by a faked Bellscoin guard as one Bellinal.
    monkeypatch.setattr(funding, "GUARD", None)
    monkeypatch.setattr(funding, "GUARDS", [])
    made = funding.build(None, params, seller, [(100_000, p2pkh_script(seller))], rate=fees.MIN_FEE_PER_KB)
    assert made.fee < 10_000, "Bells' own rate"
    ins_txid = rpc.call("sendrawtransaction", funding.assemble(
        made, [_sign(SELLER_KEY, bytes.fromhex(h)).hex() for h in made.sighashes], _pubkey(SELLER_KEY)))
    watch(ins_txid)
    bellsd.generate(1)
    inscription = ins_txid + "i0"
    guard = _guard(tmp_path, {ins_txid: [(0, inscription, 0)]})
    monkeypatch.setattr(funding, "GUARDS", [(lambda a: b58check_decode(a)[0] == version, guard)])
    monkeypatch.setattr(funding, "GUARD_BY_NET", {"bel": guard})

    # Sent to a friend: whole, first, its fee from the seller's clean coins.
    send = funding.build_doginal_send(None, params, seller, {"txid": ins_txid, "vout": 0, "value": 100_000},
                                      inscription, friend, rate=fees.MIN_FEE_PER_KB)
    sent = rpc.call("sendrawtransaction", funding.assemble(
        send, [_sign(SELLER_KEY, bytes.fromhex(h)).hex() for h in send.sighashes], _pubkey(SELLER_KEY)))
    watch(sent)
    bellsd.generate(1)
    got = rpc.call("getrawtransaction", sent, True)
    assert (got["vin"][0]["txid"], got["vin"][0]["vout"]) == (ins_txid, 0)
    assert round(got["vout"][0]["value"] * COIN) == 100_000 and _where(send.inputs, send.outputs, 0) == (0, 0)
    assert send.fee < 10_000

    # The friend lists it; the buyer prepares two dummies, then buys it in one transaction.
    (tmp_path / "b").mkdir()
    guard = _guard(tmp_path / "b", {sent: [(0, inscription, 0)]})
    monkeypatch.setattr(funding, "GUARDS", [(lambda a: b58check_decode(a)[0] == version, guard)])
    coin = {"txid": sent, "vout": 0, "value": 100_000}
    ask = M.build_ask(friend, coin, inscription, 7 * COIN, params)
    sig = _sign(0xF00D, bytes.fromhex(ask.digest), funding.SINGLE_ANYONECANPAY)
    book = M.Book(tmp_path / "book.sqlite")
    row = book.register(rpc, network="bel", seller=friend, coin=coin, inscription=inscription, price=7 * COIN,
                        signature=sig.hex(), pubkey=_pubkey(0xF00D), params=params)
    with pytest.raises(M.NeedDummies):
        M.build_purchase(None, params, buyer, row, rate=fees.MIN_FEE_PER_KB)
    prep = M.build_prepare(None, params, buyer, rate=fees.MIN_FEE_PER_KB)
    watch(rpc.call("sendrawtransaction", funding.assemble(
        prep, [_sign(BUYER_KEY, bytes.fromhex(h)).hex() for h in prep.sighashes], _pubkey(BUYER_KEY))))
    bellsd.generate(1)
    p = M.build_purchase(None, params, buyer, row, rate=fees.MIN_FEE_PER_KB)
    raw = M.assemble_purchase(p, row, [_sign(BUYER_KEY, bytes.fromhex(h)).hex() for h in p.sighashes],
                              _pubkey(BUYER_KEY))
    txid = rpc.call("sendrawtransaction", raw)
    bellsd.generate(1)
    got = rpc.call("getrawtransaction", txid, True)
    assert got["confirmations"] == 1 and (got["vin"][2]["txid"], got["vin"][2]["vout"]) == (sent, 0)
    outs = got["vout"]
    pays = [o["scriptPubKey"].get("address") or (o["scriptPubKey"].get("addresses") or [""])[0] for o in outs]
    assert pays[1] == buyer and round(outs[1]["value"] * COIN) == 100_000, "the Bellinal, output 1, the buyer's"
    assert pays[2] == friend and round(outs[2]["value"] * COIN) == 7 * COIN, "the price, to the seller"
    assert _where(p.inputs, p.outputs, 2) == (1, 0)
    assert p.fee < 20_000, "Bells' own rate"
    assert book.live(rpc, "bel") == []

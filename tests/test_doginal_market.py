"""Doginals for DOGE on the Exchange (2026-10-14: "I want doginals to be incorporated into the exchange on its own
separate tab. So people can list doginals for doge."). The builders in arcade/doginal_market.py, then one listing listed
and bought on a real dogecoind regtest node. Doge Soft is always faked."""

import pathlib
import shutil
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_funding import _pubkey, _sign                              # noqa: E402

from arcade import doginal_market as M, doginals, fees, funding, utxos   # noqa: E402
from arcade.config import NETWORKS                                   # noqa: E402
from arcade.script import b58check_encode, hash160                   # noqa: E402
from arcade.txbuild import p2pkh_script                              # noqa: E402

COIN = 100_000_000
SELLER_KEY, BUYER_KEY = 0x5E11E4, 0xB0B
SELLER = b58check_encode(30, hash160(_pubkey(SELLER_KEY)))
BUYER = b58check_encode(30, hash160(_pubkey(BUYER_KEY)))
INS_TX, OTHER_INS_TX = "aa" * 32, "bb" * 32
CLEAN_TX = "cc" * 32
INSCRIPTION = INS_TX + "i0"


def _guard(tmp_path, inscribed: dict):
    """A Doginals guard whose Doge Soft says `inscribed` ({txid: [(vout, id, offset)]}) and that every other
    transaction is clean."""
    def fetch(txid):
        return {"txid": txid, "inscriptions": [{"vout": v, "inscription_id": i, "sat_offset": o}
                                               for v, i, o in inscribed.get(txid, [])]}
    return doginals.Doginals(tmp_path / "doginals.sqlite", fetch=fetch)


@pytest.fixture
def market(tmp_path, monkeypatch):
    """A seller holding one Doginal; a buyer holding two small clean coins, a big clean coin, and two Doginals of its
    own (one on a small coin, one on a coin the size of a dummy) that must never be spent here."""
    wallets = {
        SELLER: [{"txid": INS_TX, "vout": 0, "value": 100_000}],
        BUYER: [{"txid": CLEAN_TX, "vout": 0, "value": 2_000_000},
                {"txid": CLEAN_TX, "vout": 1, "value": 3_000_000},
                {"txid": CLEAN_TX, "vout": 2, "value": 500 * COIN},
                {"txid": OTHER_INS_TX, "vout": 0, "value": 100_000},
                {"txid": OTHER_INS_TX, "vout": 1, "value": 1_500_000}],
    }
    monkeypatch.setattr(utxos, "unspent", lambda db, address: [dict(c) for c in wallets.get(address, [])])
    monkeypatch.setattr(funding, "GUARD", _guard(tmp_path, {
        INS_TX: [(0, INSCRIPTION, 0)],
        OTHER_INS_TX: [(0, OTHER_INS_TX + "i0", 0), (1, OTHER_INS_TX + "i1", 0)]}))
    return wallets


def _listing(price=5 * COIN):
    coin = {"txid": INS_TX, "vout": 0, "value": 100_000}
    ask = M.build_ask(SELLER, coin, INSCRIPTION, price)
    sig = _sign(SELLER_KEY, bytes.fromhex(ask.digest), funding.SINGLE_ANYONECANPAY)
    return ask, {"id": "L1", "seller": SELLER, "inscription": INSCRIPTION, "coin": coin, "price": price,
                 "signature": sig.hex(), "pubkey": _pubkey(SELLER_KEY).hex()}


def _where(inputs, outputs, n):
    """(output, offset) that input n's first koinu lands in, by koinu order."""
    at = sum(int(i["value"]) for i in inputs[:n])
    for k, (value, _) in enumerate(outputs):
        if at < value:
            return k, at
        at -= value
    return None, at


def test_the_purchase_layout_puts_the_doginal_at_output_1_offset_0(market):
    _, row = _listing()
    p = M.build_purchase(None, NETWORKS["doge"], BUYER, row, rate=fees.MIN_FEE_PER_KB)
    ins, outs = p.inputs, p.outputs
    assert (ins[2]["txid"], ins[2]["vout"]) == (INS_TX, 0) and ins[2]["address"] == SELLER
    assert all(i["address"] == BUYER for n, i in enumerate(ins) if n != 2)
    assert outs[0] == (ins[0]["value"] + ins[1]["value"], p2pkh_script(BUYER)), "the dummies come back whole"
    assert outs[1] == (100_000, p2pkh_script(BUYER)), "the inscribed coin's value, to the buyer"
    assert outs[2] == (5 * COIN, p2pkh_script(SELLER)), "the seller's price"
    assert len(outs) == 4 and outs[3][1] == p2pkh_script(BUYER) and outs[3][0] == p.change
    assert _where(ins, outs, 2) == (1, 0), "koinu order: the Doginal is the first koinu of output 1"
    assert all(v >= M.MIN_OUT for v, _ in outs)
    assert sum(i["value"] for i in ins) == sum(v for v, _ in outs) + p.fee
    assert p.fee >= funding.price(len(ins), outs[:3], fees.MIN_FEE_PER_KB, change=True)
    assert p.buyer_inputs == [n for n in range(len(ins)) if n != 2]


def test_doginal_coins_are_never_dummies_or_funding(market):
    _, row = _listing()
    p = M.build_purchase(None, NETWORKS["doge"], BUYER, row, rate=fees.MIN_FEE_PER_KB)
    spent = [i["txid"] for n, i in enumerate(p.inputs) if n != 2]
    assert OTHER_INS_TX not in spent, "the buyer's own Doginals stay put"
    assert [(i["txid"], i["vout"]) for i in p.inputs[:2]] == [(CLEAN_TX, 0), (CLEAN_TX, 1)], "the two smallest clean coins"


def test_the_sellers_digest_verifies_against_the_assembled_transaction(market):
    ask, row = _listing()
    p = M.build_purchase(None, NETWORKS["doge"], BUYER, row, rate=fees.MIN_FEE_PER_KB)
    whole = funding.sighash(p.inputs, p.outputs, 2, p2pkh_script(SELLER), sighash_type=funding.SINGLE_ANYONECANPAY)
    assert whole.hex() == ask.digest, "the 3-output template signs what the finished transaction asks"
    assert M.verify_signature(whole, bytes.fromhex(row["signature"])[:-1], _pubkey(SELLER_KEY))
    sigs = [_sign(BUYER_KEY, bytes.fromhex(h)).hex() for h in p.sighashes]
    raw = M.assemble_purchase(p, row, sigs, _pubkey(BUYER_KEY))
    assert raw.startswith("01000000") and row["signature"] in raw and len(sigs) == len(p.inputs) - 1


def test_a_changed_price_breaks_the_signature(market):
    ask, row = _listing()
    assert not M.verify_signature(M.listing_digest(SELLER, row["coin"], 4 * COIN),
                                  bytes.fromhex(row["signature"])[:-1], _pubkey(SELLER_KEY))
    p = M.build_purchase(None, NETWORKS["doge"], BUYER, dict(row, price=4 * COIN), rate=fees.MIN_FEE_PER_KB)
    with pytest.raises(M.MarketError):
        M.assemble_purchase(p, row, ["00"] * len(p.buyer_inputs), _pubkey(BUYER_KEY))


def test_a_buyer_without_two_small_coins_prepares_first(market):
    market[BUYER] = [c for c in market[BUYER] if c["value"] > M.DUMMY_MAX or c["txid"] == OTHER_INS_TX]
    _, row = _listing()
    with pytest.raises(M.NeedDummies):
        M.build_purchase(None, NETWORKS["doge"], BUYER, row, rate=fees.MIN_FEE_PER_KB)
    u = M.build_prepare(None, NETWORKS["doge"], BUYER, rate=fees.MIN_FEE_PER_KB)
    assert u.outputs[:2] == [(M.DUMMY_VALUE, p2pkh_script(BUYER))] * 2
    assert all(i["txid"] != OTHER_INS_TX for i in u.inputs), "never paid from a Doginal"


def test_a_listing_is_refused_unless_the_coin_holds_exactly_that_doginal_at_offset_0(tmp_path, monkeypatch):
    coin = {"txid": INS_TX, "vout": 0, "value": 100_000}
    for n, said in enumerate(([], [(0, INSCRIPTION, 0), (0, INS_TX + "i1", 5000)], [(0, INSCRIPTION, 500)])):
        (tmp_path / str(n)).mkdir()
        monkeypatch.setattr(funding, "GUARD", _guard(tmp_path / str(n), {INS_TX: said}))
        with pytest.raises(M.MarketError):
            M.build_ask(SELLER, coin, INSCRIPTION, 5 * COIN)


class _Chain:
    """gettxout for the book's checks."""

    def __init__(self, coins):
        self.coins = coins

    def call(self, method, *args):
        assert method == "gettxout"
        got = self.coins.get((args[0], args[1]))
        return None if got is None else {"value": got[0] / COIN, "scriptPubKey": {"hex": got[1].hex()}}


def test_the_book_files_only_a_signature_over_this_coin_and_price(market, tmp_path):
    ask, row = _listing()
    rpc = _Chain({(INS_TX, 0): (100_000, p2pkh_script(SELLER))})
    book = M.Book(tmp_path / "book.sqlite")
    with pytest.raises(M.MarketError):           # signed at 5, filed at 4
        book.register(rpc, network="doge", seller=SELLER, coin=row["coin"], inscription=INSCRIPTION, price=4 * COIN,
                      signature=row["signature"], pubkey=_pubkey(SELLER_KEY))
    with pytest.raises(M.MarketError):           # somebody else's key
        book.register(rpc, network="doge", seller=SELLER, coin=row["coin"], inscription=INSCRIPTION, price=5 * COIN,
                      signature=row["signature"], pubkey=_pubkey(BUYER_KEY))
    filed = book.register(rpc, network="doge", seller=SELLER, coin=row["coin"], inscription=INSCRIPTION,
                          price=5 * COIN, signature=row["signature"], pubkey=_pubkey(SELLER_KEY))
    assert [r["id"] for r in book.live(rpc, "doge")] == [filed["id"]]
    rpc.coins.clear()                            # the coin moved: the listing is gone from the page
    assert book.live(rpc, "doge") == [] and book.get(filed["id"])["status"] == "gone"


# --- end to end, on a real Dogecoin --------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def dogecoind():
    from arcade.regtest import RegtestNode
    import os
    os.environ["PATH"] = str(pathlib.Path.home() / ".local/bin") + os.pathsep + os.environ.get("PATH", "")
    if shutil.which("dogecoind") is None:
        pytest.skip("dogecoind is not installed")
    # -acceptnonstdtxn=0: mainnet's relay rules (dust, standardness), not regtest's looser defaults.
    node = RegtestNode(binary="dogecoind", cli="dogecoin-cli", require_standard=True)
    try:
        node.start(timeout=90)
    except RuntimeError as exc:
        pytest.skip(f"dogecoind regtest unavailable: {exc}")
    node.generate(101)
    yield node
    node.stop()


def test_list_and_buy_a_doginal_on_dogecoin_regtest(dogecoind, tmp_path, monkeypatch):
    rpc = dogecoind.rpc
    seller = b58check_encode(111, hash160(_pubkey(SELLER_KEY)))      # Dogecoin regtest pubkeyhash 111
    buyer = b58check_encode(111, hash160(_pubkey(BUYER_KEY)))
    for a in (seller, buyer):
        rpc.call("importaddress", a, "", False)
    rpc.call("sendtoaddress", seller, 10)
    rpc.call("sendtoaddress", buyer, 50)
    dogecoind.generate(1)

    def unspent(db, address):
        return [{"txid": u["txid"], "vout": int(u["vout"]), "value": int(round(u["amount"] * COIN)),
                 "address": address, "height": 1}
                for u in rpc.call("listunspent", 0, 9_999_999, [address])]
    monkeypatch.setattr(utxos, "unspent", unspent)
    params = NETWORKS["regtest"]
    # The "inscribed" coin: 100,000 koinu, the usual Doginal postage, made by the seller itself (the node's wallet
    # will not send so little).
    monkeypatch.setattr(funding, "GUARD", None)
    made = funding.build(None, params, seller, [(100_000, p2pkh_script(seller))], rate=fees.MIN_FEE_PER_KB)
    ins_txid = rpc.call("sendrawtransaction", funding.assemble(
        made, [_sign(SELLER_KEY, bytes.fromhex(h)).hex() for h in made.sighashes], _pubkey(SELLER_KEY)))
    dogecoind.generate(1)
    vout = 0
    inscription = ins_txid + "i0"
    monkeypatch.setattr(funding, "GUARD", _guard(tmp_path, {ins_txid: [(vout, inscription, 0)]}))

    # The seller signs its one input, and the book believes it only after checking it against the chain.
    ask = M.build_ask(seller, {"txid": ins_txid, "vout": vout, "value": 100_000}, inscription, 7 * COIN)
    sig = _sign(SELLER_KEY, bytes.fromhex(ask.digest), funding.SINGLE_ANYONECANPAY)
    book = M.Book(tmp_path / "book.sqlite")
    row = book.register(rpc, network="doge", seller=seller, coin=ask.coin, inscription=inscription, price=7 * COIN,
                        signature=sig.hex(), pubkey=_pubkey(SELLER_KEY))

    # The buyer has one big coin: it prepares two dummies first.
    with pytest.raises(M.NeedDummies):
        M.build_purchase(None, params, buyer, row, rate=fees.MIN_FEE_PER_KB)
    prep = M.build_prepare(None, params, buyer, rate=fees.MIN_FEE_PER_KB)
    raw = funding.assemble(prep, [_sign(BUYER_KEY, bytes.fromhex(h)).hex() for h in prep.sighashes],
                           _pubkey(BUYER_KEY))
    rpc.call("sendrawtransaction", raw)
    dogecoind.generate(1)

    p = M.build_purchase(None, params, buyer, row, rate=fees.MIN_FEE_PER_KB)
    raw = M.assemble_purchase(p, row, [_sign(BUYER_KEY, bytes.fromhex(h)).hex() for h in p.sighashes],
                              _pubkey(BUYER_KEY))
    txid = rpc.call("sendrawtransaction", raw)
    dogecoind.generate(1)

    got = rpc.call("getrawtransaction", txid, 1)
    assert got["confirmations"] == 1
    assert (got["vin"][2]["txid"], got["vin"][2]["vout"]) == (ins_txid, vout)
    outs = got["vout"]
    assert outs[1]["scriptPubKey"]["addresses"] == [buyer] and round(outs[1]["value"] * COIN) == 100_000
    assert outs[2]["scriptPubKey"]["addresses"] == [seller] and round(outs[2]["value"] * COIN) == 7 * COIN
    assert _where(p.inputs, p.outputs, 2) == (1, 0), "the Doginal is the first koinu of output 1"
    assert book.live(rpc, "doge") == [], "a sold listing leaves the page"

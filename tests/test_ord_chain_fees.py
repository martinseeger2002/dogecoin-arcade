"""Each ord chain's own fees, dust and least amounts (2026-10-25): an ord send, a listing and a purchase are priced on
the chain they are on, the way funding.build is (fees.rate_for / fees.dust_limit). Dogecoin's and Pepecoin's numbers are
exactly what they were; Bellscoin's are Bells 3's (0.0001 BEL a kB, no soft-dust fee, change from 0.0001 BEL), where
Dogecoin's 0.01 a kB overpaid a hundred times."""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_doginal_market import CLEAN_TX, INS_TX, INSCRIPTION, OTHER_INS_TX, _guard   # noqa: E402
from test_funding import _pubkey                                                      # noqa: E402
from test_web import app_state, client                                                # noqa: F401,E402

from arcade import doginal_market as M, fees, funding, utxos                          # noqa: E402
from arcade.config import NETWORKS                                                    # noqa: E402
from arcade.script import b58check_decode, b58check_encode, hash160                   # noqa: E402
from arcade.txbuild import p2pkh_script                                               # noqa: E402

COIN = 100_000_000


def _addresses(net):
    v = NETWORKS[net].pubkeyhash_version
    return b58check_encode(v, hash160(_pubkey(0x5E11E4))), b58check_encode(v, hash160(_pubkey(0xB0B)))


@pytest.fixture(params=["doge", "main", "bel"])
def chain(request, tmp_path, monkeypatch):
    """A seller with one ord on `net`, a buyer with two small clean coins, a big one and an ord of its own."""
    net = request.param
    seller, buyer = _addresses(net)
    wallets = {seller: [{"txid": INS_TX, "vout": 0, "value": 100_000}, {"txid": CLEAN_TX, "vout": 9, "value": 3 * COIN}],
               buyer: [{"txid": CLEAN_TX, "vout": 0, "value": 2_000_000}, {"txid": CLEAN_TX, "vout": 1, "value": 3_000_000},
                       {"txid": CLEAN_TX, "vout": 2, "value": 500 * COIN},
                       {"txid": OTHER_INS_TX, "vout": 1, "value": 1_500_000}]}
    monkeypatch.setattr(utxos, "unspent", lambda db, address: [dict(c) for c in wallets.get(address, [])])
    guard = _guard(tmp_path, {INS_TX: [(0, INSCRIPTION, 0)], OTHER_INS_TX: [(1, OTHER_INS_TX + "i1", 0)]})
    version = NETWORKS[net].pubkeyhash_version
    monkeypatch.setattr(funding, "GUARD", guard if net == "doge" else None)
    monkeypatch.setattr(funding, "GUARDS", [] if net == "doge" else [(lambda a: b58check_decode(a)[0] == version, guard)])
    return net, NETWORKS[net], seller, buyer


def _old_price(inputs, outputs):
    """What every ord transaction was priced at before: Dogecoin's rate and soft dust, whatever the chain."""
    return funding.price(inputs, outputs, fees.MIN_FEE_PER_KB, change=True)


def test_a_send_is_priced_on_its_own_chain(chain):
    net, params, seller, buyer = chain
    send = funding.build_doginal_send(None, params, seller, {"txid": INS_TX, "vout": 0, "value": 100_000},
                                      INSCRIPTION, buyer, rate=fees.MIN_FEE_PER_KB)
    assert send.inputs[0]["txid"] == INS_TX and send.outputs[0] == (100_000, p2pkh_script(buyer))
    assert send.outputs[-1] == (send.change, p2pkh_script(seller))
    assert sum(c["value"] for c in send.inputs) == sum(v for v, _ in send.outputs) + send.fee
    if net in ("doge", "main"):
        assert send.fee == _old_price(len(send.inputs), send.outputs[:1]), "exactly as before"
    else:
        own = funding.price(len(send.inputs), send.outputs[:1], 10_000, change=True, params=params)
        assert send.fee == own and own * 50 < _old_price(len(send.inputs), send.outputs[:1]), \
            "Bells' own rate, and no soft-dust fee for the 0.001 BEL output"


def test_a_purchase_and_its_prepare_are_priced_on_their_own_chain(chain):
    net, params, seller, buyer = chain
    row = {"id": "L1", "seller": seller, "inscription": INSCRIPTION, "price": 7 * COIN,
           "coin": {"txid": INS_TX, "vout": 0, "value": 100_000}}
    p = M.build_purchase(None, params, buyer, row, rate=fees.MIN_FEE_PER_KB)
    assert sum(i["value"] for i in p.inputs) == sum(v for v, _ in p.outputs) + p.fee
    word, coin = M.names(buyer, params)
    assert p.what.startswith(f"buy {word} ") and p.what.endswith(f"7 {coin}")
    prep = M.build_prepare(None, params, buyer, rate=fees.MIN_FEE_PER_KB)
    if net in ("doge", "main"):
        assert p.fee == _old_price(len(p.inputs), p.outputs[:3]), "exactly as before"
        assert prep.outputs[0][0] == M.DUMMY_VALUE == 2_000_000
        assert (word, coin) == (("Doginal", "DOGE") if net == "doge" else ("Pepinal", "PEP"))
    else:
        assert (word, coin) == ("Bellinal", "BEL")
        assert p.fee == funding.price(len(p.inputs), p.outputs[:3], 10_000, change=True, params=params)
        assert p.fee * 50 < _old_price(len(p.inputs), p.outputs[:3])
        assert prep.fee == funding.price(len(prep.inputs), prep.outputs[:2], 10_000, change=True, params=params)


def test_the_least_price_and_coin_are_the_chains_own(chain):
    net, params, seller, _ = chain
    coin = {"txid": INS_TX, "vout": 0, "value": 100_000}
    if net in ("doge", "main"):
        assert (M.min_out(params), M.dummy_value(params), M.dummy_max(params), M.least_price(params)) == \
            (M.MIN_OUT, M.DUMMY_VALUE, M.DUMMY_MAX, fees.DUST_LIMIT) == (100_000, 2_000_000, 10_000_000, 1_000_000)
        with pytest.raises(M.MarketError, match="smallest price is 0.01"):
            M.build_ask(seller, coin, INSCRIPTION, 10_000, params)
        with pytest.raises(M.MarketError, match="under the 100000"):
            M.build_ask(seller, dict(coin, value=50_000), INSCRIPTION, COIN, params)
    else:
        assert (M.min_out(params), M.least_price(params)) == (10_000, 10_000)
        assert M.dummy_value(params) == 2_000_000, "still over doginals.POSTAGE_MAX, which is counted in coins"
        ask = M.build_ask(seller, coin, INSCRIPTION, 10_000, params)
        assert ask.what == f"list Bellinal {INSCRIPTION} for 0.0001 BEL"
        with pytest.raises(M.MarketError, match="smallest price is 0.0001 BEL"):
            M.build_ask(seller, coin, INSCRIPTION, 9_999, params)


def test_the_wallets_fee_needs_on_dogecoin_are_as_before(client, monkeypatch, tmp_path):
    from arcade import htlc
    from test_doginal_wallet import _doge_account
    app, state = client
    _doge_account(app, state, monkeypatch, tmp_path)
    row = next(c for c in app.get("/account").json()["chains"] if c["network"] == "doge")
    assert row["doginal_send_need"] == _old_price(2, [(100_000, b"\x00" * 25)])
    assert row["doginal_lock_need"] == htlc.DOGINAL_RESERVE + _old_price(
        2, [(100_000, b"\x00" * 23), (htlc.DOGINAL_RESERVE, b"\x00" * 23)])

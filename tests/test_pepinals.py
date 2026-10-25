"""Pepinals (2026-10-17: "Add pepinals support"): the same ord inscriptions as Doginals, on Pepecoin mainnet,
listed for PEP, bought, and sent the way Doginals are -- each chain's coins checked by that chain's own guard."""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_doginal_market import CLEAN_TX, INS_TX, INSCRIPTION, OTHER_INS_TX, _guard, _where   # noqa: E402
from test_funding import _pubkey                                                              # noqa: E402
from test_web import app_state, client                                                        # noqa: F401,E402

from arcade import doginal_market as M, fees, funding, utxos                                  # noqa: E402
from arcade.config import NETWORKS                                                            # noqa: E402
from arcade.script import b58check_decode, b58check_encode, hash160                           # noqa: E402
from arcade.txbuild import p2pkh_script                                                       # noqa: E402

COIN = 100_000_000
PEP = NETWORKS["main"].pubkeyhash_version
SELLER = b58check_encode(PEP, hash160(_pubkey(0x5E11E4)))
BUYER = b58check_encode(PEP, hash160(_pubkey(0xB0B)))


@pytest.fixture
def pep_market(tmp_path, monkeypatch):
    wallets = {
        SELLER: [{"txid": INS_TX, "vout": 0, "value": 100_000}],
        BUYER: [{"txid": CLEAN_TX, "vout": 0, "value": 2_000_000}, {"txid": CLEAN_TX, "vout": 1, "value": 3_000_000},
                {"txid": CLEAN_TX, "vout": 2, "value": 500 * COIN},
                {"txid": OTHER_INS_TX, "vout": 1, "value": 1_500_000}],
    }
    monkeypatch.setattr(utxos, "unspent", lambda db, address: [dict(c) for c in wallets.get(address, [])])
    pep_guard = _guard(tmp_path, {INS_TX: [(0, INSCRIPTION, 0)], OTHER_INS_TX: [(1, OTHER_INS_TX + "i1", 0)]})
    # Dogecoin's guard knows nothing here: a Pepinal is read by the Pepecoin mainnet guard alone.
    (tmp_path / "doge").mkdir()
    monkeypatch.setattr(funding, "GUARD", _guard(tmp_path / "doge", {}))
    monkeypatch.setattr(funding, "GUARDS", [(lambda a: b58check_decode(a)[0] == PEP, pep_guard)])
    return wallets


def test_a_pepinal_is_listed_for_pep_and_bought_by_koinu_order(pep_market):
    ask = M.build_ask(SELLER, {"txid": INS_TX, "vout": 0, "value": 100_000}, INSCRIPTION, 7 * COIN)
    assert ask.what == f"list Pepinal {INSCRIPTION} for 7 PEP"
    row = {"id": "L1", "seller": SELLER, "inscription": INSCRIPTION, "price": 7 * COIN,
           "coin": {"txid": INS_TX, "vout": 0, "value": 100_000}}
    p = M.build_purchase(None, NETWORKS["main"], BUYER, row, rate=fees.MIN_FEE_PER_KB)
    assert "Pepinal" in p.what and p.what.endswith("7 PEP")
    assert _where(p.inputs, p.outputs, 2) == (1, 0), "the Pepinal lands at output 1, offset 0, the buyer's"
    assert p.outputs[2] == (7 * COIN, p2pkh_script(SELLER))
    assert all((i["txid"], i["vout"]) != (OTHER_INS_TX, 1) for i in p.inputs), "the buyer's own Pepinal never pays"


def test_a_coin_that_holds_no_pepinal_is_not_listed(pep_market):
    with pytest.raises(M.MarketError, match="does not hold exactly that Pepinal"):
        M.build_ask(SELLER, {"txid": CLEAN_TX, "vout": 0, "value": 100_000}, INSCRIPTION, COIN)


def test_a_pepinal_send_is_checked_by_the_pepecoin_guard(pep_market):
    send = funding.build_doginal_send(None, NETWORKS["main"], SELLER, {"txid": INS_TX, "vout": 0, "value": 100_000},
                                      INSCRIPTION, BUYER, rate=fees.MIN_FEE_PER_KB, extra=[
                                          {"txid": CLEAN_TX, "vout": 3, "value": 5 * COIN}])
    assert (send.inputs[0]["txid"], send.outputs[0]) == (INS_TX, (100_000, p2pkh_script(BUYER)))


def test_the_pepinals_tab_lists_for_pep(client):
    app, _ = client
    assert 'href="/exchange?tab=pepinals"' in app.get("/exchange?tab=tokens").text, "a tab of its own beside Doginals"
    tab = app.get("/exchange?tab=pepinals").text
    assert "Pepinals listed for PEP" in tab and "No Pepinals are listed yet" in tab
    assert "Doginal" not in tab.split("<main", 1)[1].split("</main>", 1)[0].replace("doginal", "")


def test_pepecoin_testnet_coins_are_guarded_by_their_own(app_state, monkeypatch):
    """Pepecoin testnet's ords are indexed and inscribed here too (2026-10-25), so an ordinary send there keeps an
    inscribed coin out like mainnet's: its own guard, found by its key byte (113) alone."""
    from fastapi.testclient import TestClient
    from arcade.web.app import create_app
    from arcade.web.state import ChainContext
    monkeypatch.setattr(funding, "GUARD", None)
    monkeypatch.setattr(funding, "GUARDS", [])
    monkeypatch.setattr(funding, "GUARD_BY_NET", {})
    monkeypatch.setattr(app_state, "messaging", ChainContext(network="test", role="messaging", label="Testnet",
                                                             datadir=pathlib.Path("/nonexistent")))
    TestClient(create_app(app_state))
    main, test = funding.guard_on("main"), funding.guard_on("test")
    assert main is not None and test is not None and main is not test
    tpep = b58check_encode(NETWORKS["test"].pubkeyhash_version, hash160(_pubkey(0x7E57)))
    assert funding.guard_for(tpep) is test and funding.guard_for(SELLER) is main
    for other in (30, 111, 196, 48):            # Dogecoin, L test / regtest keys, P2SH 196, Litecoin
        assert funding.guard_for(b58check_encode(other, bytes(range(20)))) is not test
    # The installed guard reads a fake index: the small inscribed coin stays put, the clean one pays.
    fake = _guard(pathlib.Path(app_state.home), {INS_TX: [(0, INSCRIPTION, 0)]})
    test.index, test.rpc_factory, test.fetch = None, None, fake.fetch
    coins = [{"txid": INS_TX, "vout": 0, "value": 100_000}, {"txid": CLEAN_TX, "vout": 0, "value": 200_000}]
    monkeypatch.setattr(utxos, "unspent", lambda db, address: [dict(c) for c in coins])
    got = funding.choose(None, tpep, 150_000)
    assert [(c["txid"], c["vout"]) for c in got] == [(CLEAN_TX, 0)], "the inscribed coin is never chosen"
    with pytest.raises(funding.FundingError):
        funding.choose(None, tpep, 250_000)     # only the inscribed coin could cover it: refused, not spent

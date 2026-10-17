"""Litecoin as an add-on network (SadFrog's side, 2026-10-17; LITECOIN-PLAN phase 2): attached at
start-up when the operator hosts it, shown grayed out until its node has synced, never used before,
and its coins guarded like Dogecoin's and Pepecoin's -- a coin carrying an inscription is never
handed to an ordinary send."""
import argparse
import json
import pathlib
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

from arcade import chains as chainlib, funding                   # noqa: E402
from arcade.config import NETWORKS                               # noqa: E402
from arcade.script import b58check_encode                        # noqa: E402

LTC_L = b58check_encode(48, bytes(range(20)))
LTC_M = b58check_encode(50, bytes(range(20)))
PEP_MAIN = b58check_encode(56, bytes(range(20)))
DOGE = b58check_encode(30, bytes(range(20)))


def _with_ltc(state, monkeypatch, ready: bool):
    from arcade.web.state import ChainContext
    monkeypatch.setattr(state, "ltc", ChainContext(
        network="ltc", role="ledger", label="Litecoin",
        conf=pathlib.Path("/nonexistent/litecoin.conf")), raising=False)
    said = {"ready": ready, "progress": 1.0 if ready else 0.9123, "online": True,
            "blocks": 100 if ready else 91, "headers": 100}
    real = type(state).chain_sync
    monkeypatch.setattr(type(state), "chain_sync",
                        lambda self, chain: said if chain.is_ltc else real(self, chain))
    return state.ltc


def test_litecoin_is_a_network_of_its_own():
    ltc = NETWORKS["ltc"]
    assert ltc.coin == "LTC" and ltc.label == "Litecoin"
    assert (ltc.pubkeyhash_version, ltc.scripthash_version) == (48, 50)
    assert ltc.activation_height == ltc.messaging_start_height == 3_198_000
    assert ltc.genesis == "12a765e31ffd4059bada1e25190f6e98c99d9714d334efa41a195a7e7e04bfe2"
    assert ltc.marker.startswith("L"), "its own Class B marker, from MARKER_SEED"
    assert ltc.min_fee_per_kb == 10_000 and NETWORKS["main"].min_fee_per_kb == 1_000_000


def test_the_chain_says_what_it_is():
    from arcade.web.state import ChainContext
    c = ChainContext(network="ltc", role="ledger", label="Litecoin")
    assert c.is_ltc and c.is_addon and c.is_mainnet and not c.is_doge
    assert (c.family, c.letter, c.title, c.net) == ("LTC", "L", "L main", "litecoin")
    pep = ChainContext(network="main", role="ledger", label="Pepecoin")
    assert (pep.family, pep.letter, pep.net) == ("PEP", "P", "mainnet") and not pep.is_addon


def test_a_syncing_litecoin_is_shown_and_never_used(client, monkeypatch):
    app, state = client
    ltc = _with_ltc(state, monkeypatch, ready=False)
    assert ltc in state.all_chains, "shown"
    assert ltc not in state.token_chains, "but nothing works on it yet"
    assert state.rpc_chain("ltc") is None
    page = app.get("/").text
    assert "Litecoin" in page and "Opens when its node has synced: 91.2% so far." in page


def test_a_synced_litecoin_is_a_network_like_the_others(client, monkeypatch):
    _, state = client
    ltc = _with_ltc(state, monkeypatch, ready=True)
    assert ltc in state.token_chains and state.rpc_chain("ltc") is ltc


def test_litecoin_is_attached_only_when_hosted(tmp_path, monkeypatch):
    from arcade.web import __main__ as launcher
    conf = tmp_path / "litecoin.conf"
    conf.write_text("server=1\n")
    args = argparse.Namespace(no_ltc=False, ltc_conf=str(conf), ltc_datadir=None, home=str(tmp_path))
    assert launcher._ltc_context(args) is None, "not hosted: never attached"
    (tmp_path / "settings.json").write_text(json.dumps({"chains:ltc": True}))
    ctx = launcher._ltc_context(args)
    assert ctx is not None and ctx.network == "ltc" and ctx.conf == conf
    assert launcher._ltc_context(argparse.Namespace(**{**vars(args), "no_ltc": True})) is None


def test_the_panel_asks_for_a_restart_when_the_network_does_not_match():
    hosted = {"chains:ltc": True}
    node = {"installed": True, "service": "active"}
    rows = lambda settings, attached: {r["key"]: r for r in chainlib.overview(
        settings, doge_running=False, ltc=node, doge_available=False, ltc_attached=attached)}
    assert rows(hosted, False)["ltc"]["restart_needed"], "hosted, node running, network not read yet"
    assert not rows(hosted, True)["ltc"]["restart_needed"]
    assert rows({}, True)["ltc"]["restart_needed"], "switched off, still attached"


def test_litecoin_coins_are_guarded_like_the_others(app_state, monkeypatch):
    _with_ltc(app_state, monkeypatch, ready=True)
    TestClient(__import__("arcade.web.app", fromlist=["create_app"]).create_app(app_state))
    for address in (LTC_L, LTC_M):
        assert funding.guard_for(address) is not None, f"{address} is watched"
    ltc_guard = funding.guard_for(LTC_L)
    assert funding.guard_for(PEP_MAIN) is not None and funding.guard_for(PEP_MAIN) is not ltc_guard, \
        "Pepecoin keeps its own guard"
    assert funding.guard_for(DOGE) is None, "no Dogecoin node here, so no Dogecoin guard"

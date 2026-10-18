"""Litecoin testnet, "L test" (SadFrog's side, 2026-10-17; plan B, the arcade's own hello world): its
own add-on chain beside L main, as P test sits beside P main -- its own node, switch, guard and
twin, keyed on coin type 1 like every test chain, and never mistaken for mainnet."""
import argparse
import json
import pathlib
import subprocess
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

from arcade import config, funding, litecoin as ltclib            # noqa: E402
from arcade.config import NETWORKS                               # noqa: E402
from arcade.script import b58check_decode, b58check_encode       # noqa: E402

HASH = bytes(range(20))
LTC_TEST = b58check_encode(111, HASH)
LTC_TEST_Q = b58check_encode(58, HASH)
LTC_MAIN = b58check_encode(48, HASH)
PEP_TEST = b58check_encode(113, HASH)
PEP_TEST_P2SH = b58check_encode(196, HASH)
PEP_MAIN = b58check_encode(56, HASH)


def _with_ltc_test(state, monkeypatch, ready: bool):
    from arcade.web.state import ChainContext
    monkeypatch.setattr(state, "ltc_test", ChainContext(
        network="ltctest", role="ledger", label="Litecoin testnet",
        conf=pathlib.Path("/nonexistent/litecoin.conf")), raising=False)
    said = {"ready": ready, "progress": 1.0 if ready else 0.42, "online": True,
            "blocks": 100 if ready else 42, "headers": 100}
    real = type(state).chain_sync
    monkeypatch.setattr(type(state), "chain_sync",
                        lambda self, chain: said if chain.is_ltc_test else real(self, chain))
    return state.ltc_test


def test_litecoin_testnet_is_a_network_of_its_own():
    t = NETWORKS["ltctest"]
    assert (t.coin, t.label, t.rpc_port, t.p2p_port) == ("LTC TEST", "Litecoin testnet", 19332, 19335)
    assert (t.pubkeyhash_version, t.scripthash_version, t.datadir_subdir) == (111, 58, "testnet4")
    assert t.activation_height == t.messaging_start_height == config.LTC_TESTNET_FLOOR
    assert t.genesis == "4966625a4b2851d9fdee139e56211a0d88575f59ed816ff5e6a63deb4e3e29a0"
    assert t.marker[0] in "mn", "its own Class B marker, from MARKER_SEED"
    assert t.min_fee_per_kb == NETWORKS["ltc"].min_fee_per_kb
    assert config.CHAIN_NAMES["ltctest"] == "test"
    assert "ltctest" not in config.MESSAGING_NETWORKS


def test_the_node_must_be_litecoin_testnet_not_another_test_chain():
    class Fake:
        def __init__(self, genesis):
            self.genesis = genesis

        def call(self, method, *args):
            return {"chain": "test"} if method == "getblockchaininfo" else self.genesis
    config.verify_connected_chain(Fake(NETWORKS["ltctest"].genesis), NETWORKS["ltctest"])
    try:
        config.verify_connected_chain(Fake("00" * 32), NETWORKS["ltctest"])   # a Pepecoin testnet, say
    except config.WrongChain:
        pass
    else:
        raise AssertionError("a test chain with another block 0 was taken for Litecoin testnet")


def test_the_chain_says_what_it_is():
    from arcade.web.state import ChainContext
    c = ChainContext(network="ltctest", role="ledger", label="Litecoin testnet")
    assert c.is_ltc_test and c.is_litecoin and c.is_addon
    assert not c.is_mainnet and not c.is_ltc and not c.is_doge, "test LTC is never real LTC"
    assert (c.family, c.letter, c.title, c.net) == ("LTC", "L", "L test", "litecoin")
    main = ChainContext(network="ltc", role="ledger", label="Litecoin")
    assert main.is_litecoin and not main.is_ltc_test and main.title == "L main"


def test_an_l_test_address_is_the_pepecoin_testnet_keys_twin():
    from arcade.web.app import _ltc_test_twin
    twin = _ltc_test_twin(PEP_TEST)
    assert twin[0] in "mn" and b58check_decode(twin) == (111, HASH)
    assert _ltc_test_twin(PEP_MAIN) == "" and _ltc_test_twin("not an address") == "", \
        "only a test key: a mainnet key is never turned into a test address"


def test_l_test_coins_have_their_own_guard(app_state, monkeypatch):
    _with_ltc_test(app_state, monkeypatch, ready=True)
    TestClient(__import__("arcade.web.app", fromlist=["create_app"]).create_app(app_state))
    guard = funding.guard_on("ltctest")
    assert guard is not None
    assert funding.guard_for(LTC_TEST) is guard and funding.guard_for(LTC_TEST_Q) is guard
    assert funding.guard_for(PEP_TEST_P2SH) is not guard, "196 is Pepecoin testnet's P2SH too"
    assert funding.guard_for(LTC_MAIN) is None, "no L main node here"


def test_a_syncing_l_test_is_shown_and_never_used(client, monkeypatch):
    _app, state = client
    t = _with_ltc_test(state, monkeypatch, ready=False)
    assert t in state.all_chains and t not in state.token_chains
    monkeypatch.setattr(type(state), "chain_sync", lambda self, chain: {"ready": True, "progress": 1.0})
    assert t in state.token_chains


def test_an_at_tag_pays_their_testnet_twin_on_l_test(app_state, monkeypatch):
    from arcade.web import app as applib
    t = _with_ltc_test(app_state, monkeypatch, ready=True)
    monkeypatch.setattr(applib.taglib, "looks_like_a_tag", lambda d: d.startswith("@"))
    monkeypatch.setattr(applib.taglib, "normalise", lambda d: d.lstrip("@"))
    real = applib._tag_address
    monkeypatch.setattr(applib, "_tag_address",
                        lambda state, d, *, mainnet: PEP_TEST if mainnet is False else real(state, d, mainnet=mainnet))
    assert applib._tag_address(app_state, "@frog", mainnet=t) == applib._ltc_test_twin(PEP_TEST)


def test_l_test_runs_here_when_its_node_is_there_and_finds_its_folder(tmp_path, monkeypatch):
    from arcade.web import __main__ as launcher
    monkeypatch.setenv("HOME", str(tmp_path))
    args = argparse.Namespace(no_ltctest=False, ltctest_conf=None, ltctest_datadir=None, home=str(tmp_path))
    assert launcher._addon_context(args, NETWORKS["ltctest"], "") is None, "no node here, no choice: off"
    (tmp_path / ".litecoin-testnet").mkdir()
    (tmp_path / ".litecoin-testnet" / "litecoin.conf").write_text("testnet=1\n")
    ctx = launcher._addon_context(args, NETWORKS["ltctest"], "")
    assert ctx.network == "ltctest" and ctx.datadir == tmp_path / ".litecoin-testnet"
    assert launcher._addon_context(argparse.Namespace(**{**vars(args), "no_ltctest": True}),
                                   NETWORKS["ltctest"], "") is None
    assert launcher._addon_context(args, NETWORKS["ltctest"], "borrow").borrowed


def test_l_test_is_its_own_chain_in_the_panel_rules():
    from arcade import cores
    assert {"doge", "ltc", "ltctest"} <= cores.OPTIONAL
    cores.check({"test": "here", "main": "borrow", "doge": "off", "ltc": "off", "ltctest": "here"})
    assert NETWORKS["ltctest"].min_difficulty_blocks and NETWORKS["ltctest"].auxpow_chain_id is None


def test_its_service_is_its_own(monkeypatch, tmp_path):
    monkeypatch.setattr(ltclib, "TEST_DATADIR", tmp_path)         # no .cookie here
    calls = []

    def fake(*args):
        calls.append(args)
        out = "loaded" if args[:1] == ("show",) else ""
        return subprocess.CompletedProcess(args, 0, out, "")
    monkeypatch.setattr(ltclib, "_systemctl", fake)
    said = ltclib.status("ltctest")
    assert said["unit"] == "litecoind-testnet" and said["answering"] is False
    ltclib.set_running(True, "ltctest")
    assert ("--no-block", "enable", "--now", "litecoind-testnet") in calls
    assert not any(ltclib.UNIT in c for c in calls), "L main's node is never touched"


def test_the_chain_switch_says_test_for_l_test(client, monkeypatch):
    app, state = client
    _with_ltc_test(state, monkeypatch, ready=True)
    # An imported macro keeps the globals of its first render, an earlier test's app's chain_menu
    # (one app per process outside the tests), so this page is drawn from fresh templates.
    from arcade.web.app import TEMPLATES
    if TEMPLATES.env.cache is not None:
        TEMPLATES.env.cache.clear()
    page = app.get("/wallet").text
    assert 'title="Switch to L test">test<' in page, "L test's net is litecoin, but it is a test chain"
    assert '>main<' not in page.split('title="Switch to L test"')[1][:10]


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

from arcade import config, funding                                 # noqa: E402
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
    assert ltc.activation_height == ltc.messaging_start_height == config.LTC_MAINNET_FLOOR == 3_197_300
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
    page = app.get("/overview").text
    assert "Litecoin" in page and "Opens when its node has synced: 91.2% so far." in page


def test_a_synced_litecoin_is_a_network_like_the_others(client, monkeypatch):
    _, state = client
    ltc = _with_ltc(state, monkeypatch, ready=True)
    assert ltc in state.token_chains and state.rpc_chain("ltc") is ltc


def test_litecoin_runs_here_when_its_node_is_there_or_borrowed_or_off_as_chosen(tmp_path, monkeypatch):
    """Admin -> Chains (arcade/cores.py, 2026-10-18): one setting per chain, core:ltc = here | borrow | off."""
    from arcade.web import __main__ as launcher
    monkeypatch.setenv("HOME", str(tmp_path))
    conf = tmp_path / "litecoin.conf"
    conf.write_text("server=1\n")
    args = argparse.Namespace(no_ltc=False, ltc_conf=str(conf), ltc_datadir=None, home=str(tmp_path))
    ctx = launcher._addon_context(args, NETWORKS["ltc"], "")
    assert ctx is not None and ctx.network == "ltc" and ctx.conf == conf and not ctx.borrowed
    assert launcher._addon_context(args, NETWORKS["ltc"], "off") is None
    borrowed = launcher._addon_context(args, NETWORKS["ltc"], "borrow")
    assert borrowed.borrowed and borrowed.label == "Litecoin"
    assert launcher._addon_context(argparse.Namespace(**{**vars(args), "no_ltc": True}), NETWORKS["ltc"], "") is None
    none_here = argparse.Namespace(no_ltc=False, ltc_conf=None, ltc_datadir=None, home=str(tmp_path))
    # No node, no choice: borrowed from an arcade that shares it (2026-10-19: "Using borrowed nodes should be
    # on by default"). Still off when the operator says so.
    got = launcher._addon_context(none_here, NETWORKS["ltc"], "")
    assert got is not None and got.borrowed, "no node, no choice: borrowed"


def test_litecoin_coins_are_guarded_like_the_others(app_state, monkeypatch):
    # Guards are module state: an earlier test's Dogecoin guard must not answer for this app (it lingered
    # in combined runs and this failed only there).
    monkeypatch.setattr(funding, "GUARD", None)
    monkeypatch.setattr(funding, "GUARDS", [])
    monkeypatch.setattr(funding, "GUARD_BY_NET", {})
    _with_ltc(app_state, monkeypatch, ready=True)
    TestClient(__import__("arcade.web.app", fromlist=["create_app"]).create_app(app_state))
    for address in (LTC_L, LTC_M):
        assert funding.guard_for(address) is not None, f"{address} is watched"
    ltc_guard = funding.guard_for(LTC_L)
    assert funding.guard_for(PEP_MAIN) is not None and funding.guard_for(PEP_MAIN) is not ltc_guard, \
        "Pepecoin keeps its own guard"
    assert funding.guard_for(DOGE) is None, "no Dogecoin node here, so no Dogecoin guard"


# --- the wallet: one key for PEP, DOGE and LTC (2026-10-17, SadFrog's choice) -------------------

def test_a_litecoin_address_is_the_pepecoin_keys_twin():
    from arcade.script import b58check_decode
    from arcade.web.app import _ltc_twin
    twin = _ltc_twin(PEP_MAIN)
    assert twin.startswith("L") and b58check_decode(twin) == (48, bytes(range(20)))
    assert _ltc_twin(DOGE) == "" and _ltc_twin("not an address") == "", "only a Pepecoin mainnet key"


def test_litecoin_joins_the_accounts_chains_only_once_synced(client, monkeypatch):
    app, state = client
    _with_ltc(state, monkeypatch, ready=False)
    chains = app.get("/account", headers={"Accept": "application/json"})
    names = [c.get("network") for c in (chains.json().get("chains") or [])] if chains.status_code == 200 else []
    assert "ltc" not in names, "nothing may be paid, held or signed there while it syncs"


def test_an_at_tag_pays_their_twin_on_litecoin(app_state, monkeypatch):
    from arcade.web import app as applib
    ltc = _with_ltc(app_state, monkeypatch, ready=True)
    monkeypatch.setattr(applib.taglib, "looks_like_a_tag", lambda d: d.startswith("@"))
    monkeypatch.setattr(applib.taglib, "normalise", lambda d: d.lstrip("@"))
    real = applib._tag_address
    monkeypatch.setattr(applib, "_tag_address",
                        lambda state, d, *, mainnet: PEP_MAIN if mainnet is True else real(state, d, mainnet=mainnet))
    assert applib._tag_address(app_state, "@frog", mainnet=ltc) == applib._ltc_twin(PEP_MAIN)


def test_the_backup_page_explains_the_path_and_hands_out_the_litecoin_key():
    from arcade.web.app import TEMPLATE_DIR
    page = (TEMPLATE_DIR / "my_backup.html").read_text()
    for said in ("Your Litecoin key, for other Litecoin wallets", "m/44'/3'/0'/0/0", "m/44'/2'/",
                 "importprivkey", "never paste it into a website", "coins.wif(ltc.key, coins.LTC_WIF)"):
        assert said in page, said
    coins = (TEMPLATE_DIR / "coins.js").read_text()
    assert "ltc: 3" in coins and "LTC_WIF = 0xb0" in coins


def test_the_browser_derives_the_same_key_and_a_wif_that_decodes_to_it(tmp_path):
    """The real coins.js, run in Node: Litecoin's key is the Pepecoin mainnet key; its WIF (0xB0,
    compressed, "T...") decodes to that key; its L-address is that key's (checked in Python)."""
    import shutil, subprocess
    if shutil.which("node") is None:
        import pytest
        pytest.skip("node is not installed here")
    from arcade.web.app import TEMPLATE_DIR
    from arcade.script import b58check_decode, b58check_encode, hash160
    from test_funding import _pubkey
    web = TEMPLATE_DIR.parent
    shutil.copytree(web / "vendor", tmp_path / "vendor")
    (tmp_path / "package.json").write_text('{"type":"module"}')
    (tmp_path / "coins.js").write_text((TEMPLATE_DIR / "coins.js").read_text().replace('"/vendor/', '"./vendor/'))
    (tmp_path / "run.js").write_text('''import * as coins from "./coins.js";
const seed = new Uint8Array(64).map((_, i) => (i * 7 + 3) & 255);
const ltc = await coins.coinKey(seed, "ltc"), pep = await coins.coinKey(seed, "main");
const hex = (b) => Buffer.from(b).toString("hex");
console.log(JSON.stringify({same: hex(ltc.key) === hex(pep.key), key: hex(ltc.key),
  wif: await coins.wif(ltc.key, coins.LTC_WIF), ltc: await coins.address(ltc.pubkey, 48)}));''')
    said = json.loads(subprocess.run(["node", "run.js"], cwd=tmp_path, capture_output=True, text=True,
                                     timeout=60, check=True).stdout)
    assert said["same"], "one key for PEP and LTC"
    version, body = b58check_decode(said["wif"])
    assert version == 0xB0 and body[32:] == b"\x01" and said["wif"].startswith("T")
    assert body[:32].hex() == said["key"]
    assert b58check_encode(48, hash160(_pubkey(int.from_bytes(body[:32], "big")))) == said["ltc"]


# --- the Exchange (2026-10-17) ---------------------------------------------------------------------

def test_litecoin_trades_are_mainnet_with_their_own_big_line():
    from arcade import trade, trade_orders
    from arcade.node_trader import swap_big
    leg = trade.Leg("ltc", "coin", "", 1, "", "")
    assert leg.tier() == "mainnet", "real coins: the second confirmation, no faucet"
    assert trade.BIG["ltc"] == 2 * 100_000_000
    assert trade_orders.RANK["main"] < trade_orders.RANK["doge"] < trade_orders.RANK["ltc"], \
        "pairs read PEP/LTC and DOGE/LTC"

    class S:
        def setting(self, key, default=None):
            return default
    assert swap_big(S())["ltc"] == 2 * 100_000_000, "the operator's line, defaulting to trade.BIG"


def test_the_exchange_offers_an_l_main_chip(client, monkeypatch):
    app, state = client
    _with_ltc(state, monkeypatch, ready=False)
    page = app.get("/exchange").text
    assert 'title="L main opens when its node has synced (91.2% so far)"' in page, "grayed while syncing"
    _with_ltc(state, monkeypatch, ready=True)
    page = app.get("/exchange").text
    assert "show=litecoin" in page and 'src="/coin-ltc.png" alt="LTC"' in page
    assert app.get("/exchange?show=litecoin").status_code == 200


def test_a_found_config_brings_its_folder_for_the_cookie(tmp_path, monkeypatch):
    """The installer's configs use cookie authentication: no rpcuser/rpcpassword, the cookie in the
    data folder. Passing the config alone failed every call -- the chain read "syncing 0%" forever
    (Dogecoin's too, 2026-10-17) -- so a found config brings its folder."""
    from arcade.web import __main__ as launcher
    monkeypatch.setenv("HOME", str(tmp_path))
    for name, conf in ((".litecoin", "litecoin.conf"), (".dogecoin", "dogecoin.conf")):
        (tmp_path / name).mkdir()
        (tmp_path / name / conf).write_text("server=1\n")
    args = argparse.Namespace(no_ltc=False, ltc_conf=None, ltc_datadir=None, no_doge=False,
                              doge_conf=None, doge_datadir=None, home=str(tmp_path))
    ltc = launcher._addon_context(args, NETWORKS["ltc"], "here")
    doge = launcher._addon_context(args, NETWORKS["doge"], "")
    assert ltc.datadir == tmp_path / ".litecoin" and ltc.conf == tmp_path / ".litecoin" / "litecoin.conf"
    assert doge.datadir == tmp_path / ".dogecoin"


def test_the_wallet_names_litecoin_testnet_test_and_mines_it_here(client, monkeypatch):
    """2026-10-18: the operator's Wallet showed "L MAIN" twice and no way to mine test LTC. The chain switch
    reads a chain's coins, not its word (Litecoin testnet's word is "litecoin"), and each add-on chain with no Core
    wallet here has its own section: the node's key address, its balance, and Mine on a test-coin chain."""
    from arcade.web.state import ChainContext
    from arcade import keywallet
    app, state = client
    ltc = ChainContext(network="ltc", role="ledger", label="Litecoin", conf=pathlib.Path("/x"), borrowed=True)
    ltctest = ChainContext(network="ltctest", role="ledger", label="Litecoin testnet", conf=pathlib.Path("/x"),
                           borrowed=True)
    monkeypatch.setattr(state, "ltc", ltc, raising=False)
    monkeypatch.setattr(state, "ltc_test", ltctest, raising=False)
    real = type(state).chain_sync
    monkeypatch.setattr(type(state), "chain_sync", lambda self, chain: {"ready": True, "progress": 1.0, "online": True}
                        if chain.is_litecoin else real(self, chain))
    monkeypatch.setattr(keywallet, "balance", lambda st, c: {"spendable": "0.5", "balance": "0.5",
                                                            "address": f"addr-{c.network}"})
    # An imported macro template keeps the globals of the app that first rendered it (Jinja caches the module);
    # a fresh one sees this app's chain_menu.
    from arcade.web import app as appmod
    appmod.TEMPLATES.env.cache.clear()
    page = app.get("/wallet/tools").text
    # Since 2026-10-19 the Wallet's chains are picked by the page's own picker (one section per chain label), not the
    # node-global token chain switch, which moved every non-account page of the node at once.
    assert 'class="chainswitch"' not in page.split("</h1>", 1)[0]
    assert 'data-label="L test"' in page and page.count('data-label="L main"') == 1, \
        "one picker button each (L test's key section and its Mine section share one label)"
    assert 'class="keyedsend" data-which="key:ltctest"' in page, "sent from the Wallet, not only from Admin"
    assert "addr-ltctest" in page and 'data-net="ltctest"' in page, "mined here"
    assert 'data-net="ltc"' not in page, "real coins are never mined"


def test_admin_shows_a_borrowed_chain_once_with_its_address(client, monkeypatch):
    """2026-10-19: Admin's Overview read "offline" for L main and L test, and again 0 beside them. A borrowed
    chain has no Core wallet here, so it is only its key card, and that card names the address."""
    from arcade.web.state import ChainContext
    from arcade import keywallet
    app, state = client
    ltc = ChainContext(network="ltc", role="ledger", label="Litecoin", conf=pathlib.Path("/x"), borrowed=True)
    ltctest = ChainContext(network="ltctest", role="ledger", label="Litecoin testnet", conf=pathlib.Path("/x"),
                           borrowed=True)
    monkeypatch.setattr(state, "ltc", ltc, raising=False)
    monkeypatch.setattr(state, "ltc_test", ltctest, raising=False)
    real = type(state).chain_sync
    monkeypatch.setattr(type(state), "chain_sync", lambda self, chain: {"ready": True, "progress": 1.0, "online": True}
                        if chain.is_litecoin else real(self, chain))
    monkeypatch.setattr(keywallet, "balance", lambda st, c: {"spendable": "0", "balance": "0",
                                                            "address": f"addr-{c.network}"})
    wallets = app.get("/admin/api/state").json()["wallets"]
    lit = [w for w in wallets if w["network"] in ("ltc", "ltctest")]
    assert sorted(w["which"] for w in lit) == ["key:ltc", "key:ltctest"], "no Core wallet card for a borrowed chain"
    assert all(w["address"] == f"addr-{w['network']}" and "error" not in w for w in lit)


def test_a_page_reads_the_tag_chain_whatever_the_operators_view(client, monkeypatch):
    """2026-10-18, playing ASHVALE as @apple: "None of my inventory is showing up". The operator had tapped
    L main in the Wallet, which moves the node's own view, and a game's frame (no cookie) read @apple's GOLD and gear
    from the Litecoin index. The page API stays on the tag chain, and an address's balances are read on its chain."""
    app, state = client
    ltc = _with_ltc(state, monkeypatch, ready=True)
    state.switch_token_chain("ltc")
    assert state.token_chain is ltc
    asked = []
    real = state.token_index
    monkeypatch.setattr(state, "token_index", lambda chain: (asked.append(chain.network), real(chain))[1])
    home = b58check_encode(state.messaging.params.pubkeyhash_version, bytes(range(20)))
    assert app.get(f"/r/balances/{home}").status_code == 200
    assert asked and set(asked) == {state.messaging.network}, asked
    asked.clear()
    app.get(f"/r/balances/{LTC_L}")
    assert "ltc" in asked, "a Litecoin address is read on Litecoin"


def test_a_page_never_waits_for_a_borrowed_chains_answer(client, monkeypatch):
    """2026-10-19: pepecoinarcade.com borrows Dogecoin and Litecoin over the mesh, and a page drawing its chain menu
    waited for the far arcade's answer (Exchange 25 s, feed 3-7 s). chain_sync answers from what is known, at once,
    and asks on a thread of its own."""
    import threading
    import time as _t
    from arcade.web.state import ChainContext
    app, state = client
    ltc = ChainContext(network="ltc", role="ledger", label="Litecoin", conf=pathlib.Path("/x"), borrowed=True)
    gate = threading.Event()

    class Slow:
        def __enter__(self):
            gate.wait(5)
            return self

        def __exit__(self, *a):
            return False

        def get_blockchain_info(self):
            return {"blocks": 10, "headers": 10, "verificationprogress": 1.0, "initialblockdownload": False}
    monkeypatch.setattr(ChainContext, "rpc", lambda self: Slow())
    monkeypatch.setattr(type(state), "FIRST_SYNC_WAIT", 0.3)
    t = _t.time()
    first = state.chain_sync(ltc)
    assert _t.time() - t < 1.0 and first["ready"] is False, "a short wait for the first answer, then not ready"
    t = _t.time()
    state.chain_sync(ltc)
    assert _t.time() - t < 0.5, "and no wait at all while that answer is still on its way"
    gate.set()
    end = _t.time() + 5
    while _t.time() < end and not state.chain_sync(ltc).get("ready"):
        _t.sleep(0.05)
    assert state.chain_sync(ltc)["ready"] is True, "and ready once it has answered"

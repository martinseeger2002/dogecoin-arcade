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
    # Drawn as its coin since 2026-10-18 (the operator: "Use the icons ... rather than text badges"): L test's own, the
    # purple one, never L main's.
    after = page.split('title="Switch to L test"')[1][:200]
    assert 'src="/coin-ltctest.png" alt="LTC TEST"' in after, "L test's net is litecoin, but it is a test chain"
    assert 'coin-ltc.png' not in after



# --- mining one L test block to an account (2026-10-18, SadFrog's side: "mine on my account") -----

def test_the_miner_mines_test_add_ons_and_never_real_coins():
    from arcade.messaging.miner import Miner
    Miner(None, NETWORKS["ltctest"])
    Miner(None, NETWORKS["regtest"])
    for real in ("main", "doge", "ltc"):
        try:
            Miner(None, NETWORKS[real])
        except config.MainnetRefused:
            continue
        raise AssertionError(f"would mine on {real}")


def _signed_in_as(state, monkeypatch, pubkey: str):
    import types
    real = state.accounts

    class Accounts:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def session(self, cookie):
            return types.SimpleNamespace(pubkey=pubkey, tag="frog", name="frog")
    monkeypatch.setattr(state, "accounts", lambda: Accounts(real()))


def test_one_l_test_block_is_mined_to_the_nodes_faucet_by_the_operator_only(client, monkeypatch):
    import contextlib
    import time
    app, state = client
    t = _with_ltc_test(state, monkeypatch, ready=True)
    monkeypatch.setattr(type(state), "chain_ready", lambda self, chain: True)
    pubkey = "02" + "11" * 32
    state.set_setting(f"address:{pubkey}", PEP_TEST)          # their tag chain's address
    calls = []

    class Rpc:
        def call(self, method, *args):
            calls.append((method, *args))
            if method == "getdifficulty":
                return 0.000244
            if method == "generatetoaddress":
                return ["ab" * 32]
            if method == "getnewaddress":
                return LTC_TEST                          # the node's own L test wallet
            raise AssertionError(method)

    @contextlib.contextmanager
    def rpc():
        yield Rpc()
    monkeypatch.setattr(t, "rpc", rpc)

    assert app.post("/fund/ltctest", data={"csrf_token": state.csrf_token}, follow_redirects=False).status_code == 303
    assert state.mining is None and not any(c[0] == "generatetoaddress" for c in calls), "nobody signed in"

    _signed_in_as(state, monkeypatch, pubkey)
    state.set_setting("operator", "02" + "22" * 32)                  # somebody else runs this node
    assert "Only this node" in app.get("/wallet").text
    app.post("/fund/ltctest", data={"csrf_token": state.csrf_token}, follow_redirects=False)
    assert state.mining is None and not any(c[0] == "generatetoaddress" for c in calls), "not the operator"

    state.set_setting("operator", pubkey)
    from arcade.web.app import _ltc_test_twin
    page = app.get("/wallet").text
    assert "Mine one L test block" in page and _ltc_test_twin(PEP_TEST) not in page
    app.post("/fund/ltctest", data={"csrf_token": state.csrf_token}, follow_redirects=False)
    for _ in range(100):
        if state.mining is None:
            break
        time.sleep(0.05)
    mined = [c for c in calls if c[0] == "generatetoaddress"]
    assert mined and mined[0][2] == LTC_TEST, "the node's faucet, never the operator's own account"
    assert state.setting("faucet_address:ltctest") == LTC_TEST
    assert "Mined L test block abababababababab" in app.get("/wallet").text


def test_every_chains_key_is_handed_out_in_its_own_text(tmp_path):
    """The real coins.js, run in Node: each chain's WIF prefix is the one that chain's Core accepted
    (checked with signmessagewithprivkey, 2026-10-18), and L test's key is the Pepecoin testnet key."""
    import shutil
    import pytest
    from arcade.web.app import TEMPLATE_DIR
    if shutil.which("node") is None:
        pytest.skip("node is not installed here")
    page = (TEMPLATE_DIR / "my_backup.html").read_text()
    assert 'id="chain-keys"' in page and "coins.WIF[network]" in page
    shutil.copytree(TEMPLATE_DIR.parent / "vendor", tmp_path / "vendor")
    (tmp_path / "package.json").write_text('{"type":"module"}')
    (tmp_path / "coins.js").write_text((TEMPLATE_DIR / "coins.js").read_text().replace('"/vendor/', '"./vendor/'))
    (tmp_path / "run.js").write_text('''import * as coins from "./coins.js";
const seed = new Uint8Array(64).fill(7);
const out = {};
for (const n of ["main", "doge", "ltc", "test", "ltctest"]) {
  const k = await coins.coinKey(seed, n);
  out[n] = {wif: await coins.wif(k.key, coins.WIF[n]), pub: Array.from(k.pubkey).join(",")};
}
console.log(JSON.stringify(out));''')
    said = json.loads(subprocess.run(["node", "run.js"], cwd=tmp_path, capture_output=True, text=True,
                                     check=True).stdout)
    versions = {n: b58check_decode(v["wif"])[0] for n, v in said.items()}
    assert versions == {"main": 158, "doge": 158, "ltc": 176, "test": 241, "ltctest": 239}
    assert said["main"]["pub"] == said["doge"]["pub"] == said["ltc"]["pub"], "one mainnet key"
    assert said["ltctest"]["pub"] == said["test"]["pub"] != said["main"]["pub"], "L test is the testnet key"


# --- each test chain's faucet, set in Admin -> Chains (2026-10-18, SadFrog's side: "give every new
#     account a set amount to top off every day ... give that option under all chain options") -------

LOCAL = {"host": "127.0.0.1:8420"}
WRITE = {"x-arcade-admin": "1"}


class _FaucetRpc:
    """An L test node: a wallet that pays, and blocks found at once."""

    def __init__(self):
        self.calls = []

    def call(self, method, *args):
        self.calls.append((method, *args))
        return {"getdifficulty": 0.000244, "generatetoaddress": ["cd" * 32], "sendtoaddress": "ef" * 32,
                "getnewaddress": LTC_TEST}.get(method)


def _l_test_node(state, monkeypatch, wallet=None):
    import contextlib
    t = _with_ltc_test(state, monkeypatch, ready=True)
    monkeypatch.setattr(type(state), "chain_ready", lambda self, chain: True)
    rpc = _FaucetRpc()

    @contextlib.contextmanager
    def connect():
        yield rpc
    monkeypatch.setattr(t, "rpc", connect)
    monkeypatch.setattr(t, "status", lambda: {"online": True, "wallet": wallet})
    return t, rpc


def test_each_test_chain_has_its_own_faucet_and_real_coins_have_none(app_state, monkeypatch):
    from arcade.faucet import GIFT
    from arcade.web.state import ChainContext
    t = _with_ltc_test(app_state, monkeypatch, ready=True)
    assert app_state.faucet_conf(app_state.messaging) == (GIFT, "button"), "P test keeps its own"
    assert app_state.faucet_conf(t) == (10_000_000, "button"), "0.1 LTC TEST a day until set"
    app_state.set_setting("faucet:ltctest", 25_000_000)
    app_state.set_setting("faucet_supply:ltctest", "auto")
    assert app_state.faucet_conf(t) == (25_000_000, "auto")
    for real in ("ltc", "doge", "main"):
        assert app_state.faucet_conf(ChainContext(network=real, role="ledger", label=real))[0] == 0


def test_admin_sets_a_test_chains_faucet_and_refuses_one_on_real_coins(client, monkeypatch):
    app, state = client
    _l_test_node(state, monkeypatch, wallet={"balance": 1.5, "immature": 0.0})
    rows = {r["net"]: r for r in app.get("/admin/api/state", headers=LOCAL).json()["chains"]["rows"]}
    assert rows["ltctest"]["faucet"]["amount"] == 0.1 and rows["ltctest"]["faucet"]["coin"] == "LTC TEST"
    assert rows["main"]["faucet"] is None, "real coins: no faucet"
    said = app.post("/admin/api/cores", headers={**LOCAL, **WRITE},
                    json={"faucet": {"ltctest": {"amount": 0.25, "supply": "auto"}}})
    assert said.status_code == 200, said.text
    assert state.setting("faucet:ltctest") == 25_000_000 and state.setting("faucet_supply:ltctest") == "auto"
    bad = app.post("/admin/api/cores", headers={**LOCAL, **WRITE}, json={"faucet": {"main": {"amount": 1}}})
    assert bad.status_code == 400 and state.setting("faucet:main") is None
    assert "faucetCell" in app.get("/admin", headers=LOCAL).text


def test_every_account_is_topped_up_once_a_day_on_l_test(client, monkeypatch):
    from arcade.web.app import _ltc_test_twin
    _app, state = client
    t, rpc = _l_test_node(state, monkeypatch, wallet={"balance": 5.0, "immature": 0.0})
    state.set_setting("address:" + "02" + "33" * 32, PEP_TEST)
    state.keep_faucets()
    sent = [c for c in rpc.calls if c[0] == "sendtoaddress"]
    assert sent == [("sendtoaddress", _ltc_test_twin(PEP_TEST), 0.1)], "its testnet key's twin, up to 0.1"
    state.keep_faucets()
    assert len([c for c in rpc.calls if c[0] == "sendtoaddress"]) == 1, "once a UTC day"
    assert not any(c[0] == "generatetoaddress" for c in rpc.calls), "stocked by button: nothing mined"


def test_an_auto_faucet_mines_here_when_it_runs_low(client, monkeypatch):
    import time
    _app, state = client
    t, rpc = _l_test_node(state, monkeypatch, wallet={"balance": 0.0, "immature": 0.0})
    state.set_setting("faucet_supply:ltctest", "auto")
    state.set_setting("faucet:ltctest", 0)                     # nobody topped up: only the stock matters
    state.keep_faucets()
    assert not any(c[0] == "generatetoaddress" for c in rpc.calls), "a faucet of 0 is off"
    state.set_setting("faucet:ltctest", 10_000_000)
    state.keep_faucets()
    for _ in range(100):
        if state.mining is None:
            break
        time.sleep(0.05)
    mined = [c for c in rpc.calls if c[0] == "generatetoaddress"]
    assert mined and mined[0][2] == LTC_TEST, "to the node's own faucet address"
    assert state.setting("faucet_address:ltctest") == LTC_TEST, "made once and kept"


def test_admin_lists_every_chains_node_wallet_and_big_swap_line(client, monkeypatch):
    app, state = client
    t, rpc = _l_test_node(state, monkeypatch, wallet={"balance": 0.0, "immature": 0.0})
    from arcade.web import app as applib
    monkeypatch.setattr(applib.walletlib, "balance",
                        lambda rpc: {"spendable": 1, "immature": 0, "unconfirmed": 0})
    d = app.get("/admin/api/state", headers=LOCAL).json()
    assert "ltctest" in [w["which"] for w in d["wallets"]], "L test's node wallet is listed"
    assert set(d["swap_big"]) >= {"main", "doge", "ltc"}
    page = app.get("/admin", headers=LOCAL).text
    assert "swapbiglines" in page and "swapbigdoge" not in page, "drawn from the setting, every chain"
    assert 'id="faucet_mine"' not in page and 'id="faucet_topoff"' not in page, "no switch that does nothing"


def test_a_test_chains_daily_top_up_can_be_switched_off(client, monkeypatch):
    app, state = client
    t, rpc = _l_test_node(state, monkeypatch, wallet={"balance": 5.0, "immature": 0.0})
    state.set_setting("address:" + "02" + "44" * 32, PEP_TEST)
    said = app.post("/admin/api/cores", headers={**LOCAL, **WRITE}, json={"faucet": {"ltctest": {"topoff": False}}})
    assert said.status_code == 200 and state.faucet_topoff(t) is False
    state.keep_faucets()
    assert not any(c[0] == "sendtoaddress" for c in rpc.calls)


def test_the_backup_page_has_dogecoins_node_wallet_too(client, monkeypatch, tmp_path):
    import contextlib
    from arcade import backup
    from arcade.web.state import ChainContext
    app, state = client
    doge = ChainContext(network="doge", role="ledger", label="Dogecoin", datadir=tmp_path)

    @contextlib.contextmanager
    def rpc():
        yield object()
    monkeypatch.setattr(doge, "rpc", rpc)
    monkeypatch.setattr(state, "doge", doge, raising=False)
    monkeypatch.setattr(type(state), "chain_ready", lambda self, chain: True)
    monkeypatch.setattr(backup, "wallet_summary", lambda rpc: {"balance": 0, "addresses": 0, "keys": 0})
    page = app.get("/backup", headers=LOCAL).text
    assert "/backup/doge/" in page, "Dogecoin's node wallet: save, switch, make new"


class _NamedWalletRpc:
    """Litecoin Core 0.21's wallet RPCs, as far as the Backup page uses them: wallets live in folders,
    loaded and unloaded by name, and `load_on_startup` is kept in settings."""

    def __init__(self, folder, loaded=("arcade",), on_disk=("arcade", "old"), fail_load=()):
        self.folder = folder
        self.loaded = list(loaded)
        self.on_disk = list(on_disk)
        self.startup = set(loaded)
        self.fail_load = set(fail_load)
        self.calls = []

    def call(self, method, *args):
        self.calls.append((method, *args))
        if method == "listwallets":
            return list(self.loaded)
        if method == "listwalletdir":
            return {"wallets": [{"name": n} for n in self.on_disk]}
        if method == "getwalletinfo":
            if len(self.loaded) != 1:
                raise RuntimeError("Wallet file not specified (must request wallet RPC through "
                                   "/wallet/<filename> uri-path).")
            return {"walletname": self.loaded[0], "balance": 0.5, "txcount": 2}
        if method == "createwallet":
            name, startup = args[0], args[6]
            assert args[5] is False, "a legacy wallet, for importprivkey and dumpprivkey"
            self.on_disk.append(name)
            self.loaded.append(name)
            if startup:
                self.startup.add(name)
            return {"name": name, "warning": ""}
        if method == "loadwallet":
            if args[0] in self.fail_load:
                raise RuntimeError("Wallet file verification failed.")
            if args[0] in self.loaded:
                raise RuntimeError("Wallet is already loaded.")
            self.loaded.append(args[0])
            if args[1]:
                self.startup.add(args[0])
            return {"name": args[0], "warning": ""}
        if method == "unloadwallet":
            self.loaded.remove(args[0])
            if args[1] is False:
                self.startup.discard(args[0])
            return {"warning": ""}
        if method == "backupwallet":
            pathlib.Path(args[0]).write_bytes(b"\x00" * 16)
            return None
        if method in ("listaddressgroupings", "listlabels"):
            return []
        raise RuntimeError(f"Method not found: {method}")


def _litecoin_nodes(state, monkeypatch, tmp_path, **kw):
    import contextlib
    from arcade import backup
    from arcade.web.state import ChainContext
    backup._KNOWN.clear()
    nodes = {}
    for which, label in (("ltc", "Litecoin"), ("ltctest", "Litecoin testnet")):
        chain = ChainContext(network=which, role="ledger", label=label, datadir=tmp_path / which)
        rpc = _NamedWalletRpc(tmp_path, **kw)

        @contextlib.contextmanager
        def connect(rpc=rpc):
            yield rpc
        monkeypatch.setattr(chain, "rpc", connect)
        monkeypatch.setattr(state, "ltc" if which == "ltc" else "ltc_test", chain, raising=False)
        nodes[which] = rpc
    monkeypatch.setattr(type(state), "chain_ready", lambda self, chain: True)
    return nodes


def test_the_backup_page_has_both_litecoin_node_wallets(client, monkeypatch, tmp_path):
    import contextlib
    app, state = client
    _litecoin_nodes(state, monkeypatch, tmp_path)

    class Pepecoin:
        def call(self, method, *args):
            return {"balance": 1} if method == "getwalletinfo" else []

    @contextlib.contextmanager
    def pepecoin():
        yield Pepecoin()
    monkeypatch.setattr(state.messaging, "rpc", pepecoin)
    page = app.get("/backup", headers=LOCAL).text
    for which in ("ltc", "ltctest"):
        assert f"/backup/{which}/save" in page and f"/backup/{which}/wallet/new" in page
        assert f"/backup/{which}/wallet/switch" in page, "the wallet not in use: Use this one"
        assert f"/backup/{which}/wallet/remove" not in page, "nothing to remove: unloaded is put aside"
        assert f"/backup/{which}/restore" not in page, "a Litecoin wallet is a folder, not a wallet.dat"
    assert "/backup/messaging/restore" in page, "Pepecoin's card is as before"


def test_a_borrowed_litecoin_has_no_backup_card(client, monkeypatch, tmp_path):
    app, state = client
    _litecoin_nodes(state, monkeypatch, tmp_path)
    monkeypatch.setattr(state.ltc, "borrowed", True)
    page = app.get("/backup", headers=LOCAL).text
    assert "/backup/ltc/" not in page and "/backup/ltctest/" in page
    app.post("/backup/ltc/save", data={"csrf_token": state.csrf_token}, follow_redirects=False)
    assert "unknown chain" in (state.notice or "")


def test_a_litecoin_wallet_is_saved_by_the_node_where_asked(client, monkeypatch, tmp_path):
    app, state = client
    nodes = _litecoin_nodes(state, monkeypatch, tmp_path)
    folder = tmp_path / "copies"
    app.post("/backup/ltctest/save", data={"csrf_token": state.csrf_token, "folder": str(folder)},
             follow_redirects=False)
    [written] = list(folder.iterdir())
    assert written.name.startswith("arcade-") and written.suffix == ".dat", "named for the wallet"
    assert ("backupwallet", str(written)) in nodes["ltctest"].calls, "the whole path, to the node"
    assert "Saved a copy" in (state.notice or "")


def test_a_new_litecoin_wallet_is_made_and_the_old_one_put_aside(client, monkeypatch, tmp_path):
    app, state = client
    nodes = _litecoin_nodes(state, monkeypatch, tmp_path)
    rpc = nodes["ltc"]
    app.post("/backup/ltc/wallet/new", data={"csrf_token": state.csrf_token, "name": "savings"},
             follow_redirects=False)
    assert rpc.loaded == ["savings"], "one loaded, so the arcade reads it at the node's root"
    assert rpc.startup == {"savings"}, "and a restart brings back the same one"
    assert "arcade" in rpc.on_disk, "the old one is unloaded, never deleted"
    made = [c[0] for c in rpc.calls if c[0] in ("createwallet", "unloadwallet")]
    assert made == ["createwallet", "unloadwallet"]
    assert "savings" in (state.notice or "") and "not deleted" in state.notice
    assert nodes["ltctest"].loaded == ["arcade"], "L test's node is its own"
    app.post("/backup/ltc/wallet/new", data={"csrf_token": state.csrf_token, "name": "Arcade"},
             follow_redirects=False)
    assert "already a wallet" in (state.notice or "") and rpc.loaded == ["savings"]


def test_use_this_one_unloads_then_loads_and_keeps_the_old_one_if_it_cannot(client, monkeypatch, tmp_path):
    app, state = client
    nodes = _litecoin_nodes(state, monkeypatch, tmp_path, fail_load=("broken",),
                            on_disk=("arcade", "old", "broken"))
    rpc = nodes["ltctest"]
    app.post("/backup/ltctest/wallet/switch", data={"csrf_token": state.csrf_token, "name": "old"},
             follow_redirects=False)
    assert rpc.loaded == ["old"] and rpc.startup == {"old"}
    moved = [c for c in rpc.calls if c[0] in ("loadwallet", "unloadwallet")]
    assert moved == [("unloadwallet", "arcade", False), ("loadwallet", "old", True)]
    assert "Now using old" in (state.notice or "")
    app.post("/backup/ltctest/wallet/switch", data={"csrf_token": state.csrf_token, "name": "broken"},
             follow_redirects=False)
    assert rpc.loaded == ["old"] and rpc.startup == {"old"}, "a failed load puts the old one back"
    assert "unchanged" in (state.notice or "")
    app.post("/backup/ltctest/wallet/switch", data={"csrf_token": state.csrf_token, "name": "nope"},
             follow_redirects=False)
    assert "no wallet called" in (state.notice or "")


def test_litecoin_wallets_are_never_removed_or_overwritten_here(client, monkeypatch, tmp_path):
    app, state = client
    nodes = _litecoin_nodes(state, monkeypatch, tmp_path)
    app.post("/backup/ltc/wallet/remove", data={"csrf_token": state.csrf_token, "name": "old"},
             follow_redirects=False)
    assert "put aside" in (state.notice or "")
    app.post("/backup/ltc/restore", data={"csrf_token": state.csrf_token, "understand": "yes",
                                          "source": str(tmp_path / "x.dat")}, follow_redirects=False)
    assert "folder of its own" in (state.notice or "")
    assert [c[0] for c in nodes["ltc"].calls if c[0] not in ("listwallets", "listwalletdir")] == []


# --- mining from Admin -> Chains (2026-10-18, SadFrog: "add the mine a block button, and add a mine
#     for x amount of hours button on the admin panel") --------------------------------------------

def _until_idle(state, seconds=5.0):
    import time
    deadline = time.time() + seconds
    while state.mining is not None and time.time() < deadline:
        time.sleep(0.02)
    assert state.mining is None, "the mining thread ended"


def test_admin_mines_one_test_block_to_the_faucet(client, monkeypatch):
    app, state = client
    _t, rpc = _l_test_node(state, monkeypatch, wallet={"balance": 0.0, "immature": 0.0})
    assert "mineControls" in app.get("/admin", headers=LOCAL).text
    said = app.post("/admin/api/faucet/mine", headers={**LOCAL, **WRITE}, json={"net": "ltctest"})
    assert said.status_code == 200 and said.json()["until"] is None, said.text
    _until_idle(state)
    mined = [c for c in rpc.calls if c[0] == "generatetoaddress"]
    assert len(mined) == 1 and mined[0][2] == LTC_TEST, "one block, to the node's faucet"
    assert "Mined L test block" in (state.notice or "")


def test_admin_mines_for_hours_and_stops_at_the_time_or_when_told(client, monkeypatch):
    import time
    app, state = client
    _t, rpc = _l_test_node(state, monkeypatch, wallet={"balance": 0.0, "immature": 0.0})
    said = app.post("/admin/api/faucet/mine", headers={**LOCAL, **WRITE}, json={"net": "ltctest", "hours": 0.0002})
    assert said.status_code == 200 and said.json()["until"] > time.time() - 1
    _until_idle(state)
    found = len([c for c in rpc.calls if c[0] == "generatetoaddress"])
    assert found > 1, "block after block until the time is up"
    assert f"Done mining L test: {found} blocks" in (state.notice or "")

    app.post("/admin/api/faucet/mine", headers={**LOCAL, **WRITE}, json={"net": "ltctest", "hours": 2})
    mining = app.get("/admin/api/state", headers=LOCAL).json()["chains"]["mining"]
    assert mining["mining"] and mining["net"] == "ltctest" and mining["until"] > time.time() + 7000
    busy = app.post("/admin/api/faucet/mine", headers={**LOCAL, **WRITE}, json={"net": "ltctest"})
    assert busy.status_code == 409, "one mining thread"
    assert app.post("/admin/api/faucet/mine/stop", headers={**LOCAL, **WRITE}).status_code == 200
    _until_idle(state)
    assert "Stopped mining L test" in (state.notice or "")


def test_admin_mining_refuses_real_coins_and_too_many_hours(client, monkeypatch):
    app, state = client
    _l_test_node(state, monkeypatch, wallet={"balance": 0.0, "immature": 0.0})
    for body in ({"net": "ltc"}, {"net": "main"}, {"net": "doge"}, {"net": "ltctest", "hours": 73},
                 {"net": "ltctest", "hours": -1}, {"net": "ltctest", "hours": "lots"}):
        said = app.post("/admin/api/faucet/mine", headers={**LOCAL, **WRITE}, json=body)
        assert said.status_code == 400, body
    assert state.mining is None


def test_admin_says_how_the_last_mining_run_ended_and_whether_its_block_stuck(client, monkeypatch):
    """2026-10-18: a block found and lost to another miner's, with the row simply going back to its
    buttons, read as "the button does nothing"."""
    import time
    app, state = client
    t, rpc = _l_test_node(state, monkeypatch, wallet={"balance": 0.0, "immature": 6.25})
    fate = {"confirmations": -1}
    real = rpc.call

    def call(method, *args):
        if method == "getblockheader":
            rpc.calls.append((method, *args))
            return {"height": 4_926_457, **fate}
        return real(method, *args)
    monkeypatch.setattr(rpc, "call", call)
    said = app.post("/admin/api/faucet/mine", headers={**LOCAL, **WRITE}, json={"net": "ltctest", "hours": 0})
    assert said.status_code == 200, said.text
    for _ in range(100):
        if state.mining is None:
            break
        time.sleep(0.05)
    assert state.last_mined["outcome"] == "found" and state.last_mined["blocks"] == ["cd" * 32]
    rows = {r["net"]: r for r in app.get("/admin/api/state", headers=LOCAL).json()["chains"]["rows"]}
    last = rows["ltctest"]["faucet"]["last"]
    assert last["blocks"][0]["confirmations"] == -1 and last["blocks"][0]["height"] == 4_926_457
    fate["confirmations"] = 12
    rows = {r["net"]: r for r in app.get("/admin/api/state", headers=LOCAL).json()["chains"]["rows"]}
    assert rows["ltctest"]["faucet"]["last"]["blocks"][0]["confirmations"] == 12
    page = app.get("/admin", headers=LOCAL).text
    assert "lost to another miner" in page and "mined, ripening" in page and "minesaid" in page


def test_a_test_add_on_chain_is_mined_in_short_batches_so_a_new_tip_is_taken_up_quickly():
    from arcade.messaging import miner as M

    class Rpc:
        def __init__(self):
            self.batches = []

        def call(self, method, *args):
            self.batches.append(args[2])
            return [] if len(self.batches) < 3 else ["ab" * 32]

    def batches(params):
        rpc = Rpc()
        m = M.Miner(rpc, params)
        clock = iter(range(0, 1000, 1))                     # each call takes one "second"
        real = M.time.monotonic
        M.time.monotonic = lambda: next(clock)
        try:
            m.mine_one("x")
        finally:
            M.time.monotonic = real
        return rpc.batches
    ltc, pep = batches(NETWORKS["ltctest"]), batches(NETWORKS["regtest"])
    assert ltc[1] < pep[1], f"L test {ltc[1]} vs P regtest {pep[1]} tries a call"
    assert pep[1] == min(max(int(20_000 * M.SECONDS_PER_CALL), M.TRIES_PER_CALL[0]), M.TRIES_PER_CALL[1])

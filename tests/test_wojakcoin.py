"""Wojakcoin (WJK) mainnet as an add-on chain, the same shape as Bellscoin and Luckycoin.

Params come from WojakCoin 1.21.3 chainparams.cpp and chainparamsbase.cpp, and were checked against
this chain's node: block 0, a W address (version 73), and a WIF (version 201) that signmessagewithprivkey
accepted. tests/data/rawchain/wjk-blocks.json.gz holds a real mainnet block so the reader and the
sha256d proof of work are held to what Wojak Core itself mined.
"""
import gzip
import json
import pathlib
import re

from arcade import config, funding, rawchain, trade as T
from arcade.config import NETWORKS
from arcade.script import b58check_decode, b58check_encode

DATA = pathlib.Path(__file__).resolve().parent / "data" / "rawchain" / "wjk-blocks.json.gz"
FIX = json.loads(gzip.open(DATA, "rt").read())
WJK = NETWORKS["wjk"]
TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "arcade" / "web" / "templates"
GENESIS = "000000004536a4f8fa9d88f0001ca9f9825f8d9fd3ba6383a2f030c0427bf085"


def test_wojakcoin_params_are_its_chainparams():
    assert WJK.addon and WJK.real_coins and not WJK.tx_time and not WJK.block_sig
    assert (WJK.pubkeyhash_version, WJK.scripthash_version) == (73, 5)
    assert (WJK.rpc_port, WJK.p2p_port) == (20760, 20759)
    assert WJK.genesis == GENESIS
    assert WJK.auxpow_chain_id is None and WJK.pow_algo == "sha256d"
    assert WJK.pow_limit_bits == 0x1D00FFFF
    assert not WJK.bech32_hrp and not WJK.mweb and not WJK.trust_progress
    assert (WJK.min_fee_per_kb, WJK.soft_dust, WJK.dust_limit) == (10_000, False, 10_000)
    assert (WJK.letter, WJK.family, WJK.net_word, WJK.node_dir, WJK.node_conf) == (
        "W", "WJK", "wojakcoin", ".wojakcoin", "wojakcoin.conf")
    assert config.CHAIN_NAMES["wjk"] == "main"
    assert WJK.activation_height == WJK.messaging_start_height == config.WJK_MAINNET_FLOOR == 228_000
    assert WJK.swaps_from is None and WJK.take_from is None
    assert "wjk" in __import__("arcade.cores", fromlist=["OPTIONAL"]).OPTIONAL


def test_its_script_byte_is_not_another_chains_key():
    """Version 5 is Luckycoin's and Litecoin's legacy script byte, and nobody's ordinary address."""
    assert config.unpayable_versions(WJK) == frozenset()
    others = {p.pubkeyhash_version for n, p in NETWORKS.items() if n != "wjk"}
    assert 73 not in others and 5 not in others
    assert 73 in T.PAYEE_VERSIONS
    assert T.claim_fee("wjk") == 10_000 and T.fee_floor("wjk") == 10_000


def test_an_accounts_wojakcoin_address_is_its_pepecoin_mainnet_key():
    from arcade.web.app import _chain_twin
    body = bytes(range(20))
    assert b58check_decode(_chain_twin(b58check_encode(56, body), WJK)) == (73, body)
    assert _chain_twin(b58check_encode(113, body), WJK) == ""


def test_a_real_wojakcoin_block_is_read_and_its_work_is_sha256d():
    block = FIX["blocks"][0]
    raw = bytes.fromhex(block["raw"])
    got = rawchain.read_block(raw, block["hash"])
    assert [t.txid for t in got.txs] == block["tx"]
    assert got.version == block["version"]
    rawchain.check_work(got, algo="sha256d")
    try:
        rawchain.check_work(got, algo="scrypt")
    except rawchain.RawError as exc:
        assert "work does not meet" in str(exc)
    else:
        raise AssertionError("a Wojakcoin block's work is sha256d, not scrypt")


def test_the_browser_knows_wojakcoins_key_and_its_fee():
    coins = (TEMPLATES / "coins.js").read_text()
    assert re.search(r"COIN_TYPE = \{[^}]*\bwjk: 3\b", coins)
    assert re.search(r"WIF = \{[^}]*\bwjk: 0xc9\b", coins, re.S)
    wallet = (TEMPLATES / "wallet_js.js").read_text()
    assert '"wjk"' in re.search(r"REAL_COINS = new Set\(\[[^\]]*\]\)", wallet).group(0)
    assert "wjk: 10_000" in wallet and "wjk: 5" in wallet
    assert (TEMPLATES / "coin-wjk.png").read_bytes()[:4] == b"\x89PNG"


def test_a_caught_up_node_is_ready_even_though_its_progress_estimate_never_finishes():
    """chainTxData says 100 tx/s, so verificationprogress stays under 0.9999 at the tip.
    A node out of IBD and level with its headers is the one to use. A chain whose estimate
    is real still waits for 0.9999, and a node whose headers are ahead is still syncing."""
    from arcade.web.state import AppState

    class Rpc:
        def __init__(self, info):
            self.info = info
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def get_blockchain_info(self):
            return self.info

    class Chain:
        def __init__(self, params, info):
            self.network = params.name
            self.params = params
            self._rpc = Rpc(info)
        def rpc(self):
            return self._rpc

    tip = {"blocks": 226544, "headers": 226544, "verificationprogress": 0.90,
           "initialblockdownload": False}
    state = AppState.__new__(AppState)
    state._sync = {}
    ready = state._sync_now(Chain(WJK, tip))
    assert ready["online"] is True and ready["ready"] is True and ready["progress"] == 0.90
    behind = dict(tip, blocks=1000, headers=226544, verificationprogress=0.01)
    assert state._sync_now(Chain(WJK, behind))["ready"] is False
    other = state._sync_now(Chain(NETWORKS["lky"], tip))
    assert other["ready"] is False, "a chain with a real estimate still waits for 0.9999"


def test_a_core_without_accounts_keeps_one_legacy_address():
    """Wojak Core 0.21 has no getaccountaddress. The wallet page drops a chain whose address comes back empty,
    which is why the coins were in the node and missing from the page."""
    from arcade.rpc import RpcError
    from arcade.wallet import kept_address

    class Rpc:
        def __init__(self):
            self.labels = {}
            self.made = []
        def call(self, method, *args):
            if method == "getaccountaddress":
                raise RpcError(-32601, "Method not found", method)
            if method == "getaddressesbylabel":
                found = self.labels.get(args[0])
                if not found:
                    raise RpcError(-11, "No addresses with label", method)
                return found
            if method == "getaddressinfo":
                addr = str(args[0])
                return {"iswitness": addr.startswith("wj"), "isscript": False,
                        "timestamp": 200 if addr.startswith("Wlater") else 100}
            if method == "getnewaddress":
                self.made.append(args)
                addr = "WkeptAddress11111111111111111111"
                self.labels[args[0]] = {addr: {"purpose": "receive"}}
                return addr
            raise AssertionError(method)

    rpc = Rpc()
    first = kept_address(rpc, legacy=True)
    # A later address that sorts first must not take the wallet's place.
    rpc.labels["arcade-identity"] = {
        "WlaterAddressSortsFirst111111111111": {"purpose": "receive"},
        "WkeptAddress11111111111111111111": {"purpose": "receive"},
    }
    second = kept_address(rpc, legacy=True)
    assert first == second == "WkeptAddress11111111111111111111"
    assert rpc.made == [("arcade-identity", "legacy")]


def test_the_ord_index_starts_where_ord_wojakcoin_does_and_is_not_taproot():
    from arcade import doginal_index, ord_inscribe, ord_rules
    assert doginal_index.STARTS["wjk"] == 1
    assert "wjk" not in doginal_index.TAPROOT
    assert ord_rules.dialect_of("wjk") == "dogecoin"
    assert ord_inscribe.rules_of("wjk").label == "Wojakinal"
    assert "wjk" in ord_inscribe.CHAINS

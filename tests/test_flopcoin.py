"""Flopcoin (FLOP) mainnet as an add-on chain, the same shape as Dogecoin and Luckycoin (2026-11-03, the operator's side).

Params come from Flopcoin Core v2.0.0.0 (github.com/Flopcoin/Flopcoin, tag v2.0.0.0) src/chainparams.cpp,
chainparamsbase.cpp, policy/policy.h and script/standard.h. Checked against that chain's own node (2026-11-03,
Flopcoin Core v2.0.0.0 from the release archive pinned in core_install): block 0's hash, and a peer's version
message (/Floptoshi:2.0.0/, protocol 70016) on port 32552 with message-start bytes fa bf b5 da.
Also on that node: validateaddress on an F address (35) and a version-22 address made by b58check_encode, both valid,
the 22 one isscript; a Dogecoin D address (30) refused; and signmessagewithprivkey accepting a WIF of version 158.
"""
import pathlib
import re

from arcade import config, core_install, htlc, trade as T, txbuild
from arcade.config import NETWORKS
from arcade.script import b58check_decode, b58check_encode, hash160

FLOP = NETWORKS["flop"]
TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "arcade" / "web" / "templates"
GENESIS = "1f1037762db00882d3353b8d9dcfd52fc93cbc2d694c62f1ecd6a209cf9d30d0"


def test_flopcoin_params_are_its_chainparams():
    assert FLOP.addon and FLOP.real_coins and not FLOP.tx_time and not FLOP.block_sig
    assert (FLOP.pubkeyhash_version, FLOP.scripthash_version) == (35, 22)
    assert (FLOP.rpc_port, FLOP.p2p_port) == (32551, 32552)
    assert FLOP.genesis == GENESIS
    assert FLOP.auxpow_chain_id == 0xA3, "chainparams.cpp nAuxpowChainId 0x00A3, fStrictChainId"
    assert FLOP.pow_algo == "scrypt" and FLOP.pow_limit_bits == 0x1E0FFFFF, "Dogecoin's powLimit, ~uint256(0) >> 20"
    assert FLOP.cltv_enforced, "BIP65 from V3ForkHeight 111,000: locks refund as on Dogecoin"
    doge = NETWORKS["doge"]
    assert (FLOP.min_fee_per_kb, FLOP.soft_dust, FLOP.dust_limit) == (
        doge.min_fee_per_kb, doge.soft_dust, doge.dust_limit), "Dogecoin 1.14's policy.h, unchanged"
    assert (FLOP.letter, FLOP.family, FLOP.net_word, FLOP.node_dir, FLOP.node_conf) == (
        "F", "FLOP", "flopcoin", ".flopcoin", "flopcoin.conf")
    assert config.CHAIN_NAMES["flop"] == "main"
    assert FLOP.activation_height == FLOP.messaging_start_height == config.FLOP_MAINNET_FLOOR == 914_000
    assert FLOP.swaps_from is None and FLOP.take_from is None
    assert "flop" in __import__("arcade.cores", fromlist=["OPTIONAL"]).OPTIONAL
    assert all(p.letter != "F" for n, p in NETWORKS.items() if n != "flop"), "its badge letter is its own"


def test_its_f_is_nobodys_and_its_script_byte_is_dogecoins():
    """35 is no other chain's byte of either kind. 22 is Dogecoin's and Pepecoin's script byte, never a key hash,
    so a Flopcoin script address is payable and nothing is refused for it."""
    others = [p for n, p in NETWORKS.items() if n != "flop"]
    assert 35 not in {p.pubkeyhash_version for p in others} | {p.scripthash_version for p in others}
    assert NETWORKS["doge"].scripthash_version == NETWORKS["main"].scripthash_version == FLOP.scripthash_version
    assert config.unpayable_versions(FLOP) == frozenset()
    assert 22 in txbuild.SCRIPTHASH_VERSIONS
    assert 35 in T.PAYEE_VERSIONS
    assert T.claim_fee("flop") == T.claim_fee("doge") and T.fee_floor("flop") == T.fee_floor("doge")
    assert T.BIG["flop"] == 1_000 * 100_000_000


def test_payees_on_a_flopcoin_send():
    from arcade.web.app import _check_address

    class Chain:
        network, params, label = "flop", FLOP, "Flopcoin"
    body = bytes(range(20))
    f_addr = b58check_encode(35, body)
    assert f_addr.startswith("F")
    assert _check_address(f_addr, mainnet=Chain()) is None
    assert _check_address(b58check_encode(22, body), mainnet=Chain()) is None
    assert _check_address(b58check_encode(30, body), mainnet=Chain()), "a Dogecoin D address is not Flopcoin's"
    assert _check_address(b58check_encode(56, body), mainnet=Chain()), "a Pepecoin address is not Flopcoin's"
    assert txbuild.p2pkh_script(f_addr) == b"\x76\xa9\x14" + body + b"\x88\xac"


def test_an_accounts_flopcoin_address_is_its_pepecoin_mainnet_key():
    from arcade.web.app import _chain_twin
    body = bytes(range(20))
    assert b58check_decode(_chain_twin(b58check_encode(56, body), FLOP)) == (35, body)
    assert _chain_twin(b58check_encode(113, body), FLOP) == ""


def test_a_swap_lock_on_flopcoin_is_a_script_address():
    """CLTV is enforced, the script byte is payable and transactions carry no time: all three lock checks pass."""
    config.require_cltv(FLOP)
    config.require_p2sh_payable(FLOP)
    config.require_untimed(FLOP)
    address = htlc.address_of(bytes(40), FLOP)
    assert b58check_decode(address)[0] == 22
    assert txbuild.p2pkh_script(address)[:2] == b"\xa9\x14"


def test_the_node_installs_from_the_pinned_release():
    rel = core_install.RELEASES["flop"]
    assert (rel.family, rel.version) == ("flopcoin", "2.0.0.0")
    assert rel.url == "https://github.com/Flopcoin/Flopcoin/releases/download/v2.0.0.0/"
    assert rel.assets == {"x86_64": ("flopcoin-v2.0.0.0-linux.tar.gz",
                                     "7e78da4a8e8c993a90f855c43cbce186bf84269d139c3695ad15606ae3d9905e", 29600108)}
    assert (rel.daemon, rel.cli) == ("flopcoind", "flopcoin-cli")
    assert core_install.offer("flop", "x86_64")["available"]
    assert not core_install.offer("flop", "aarch64")["available"], "the project publishes no ARM Linux build"
    lines = core_install._conf_lines(FLOP, rel)
    for want in ("server=1", "txindex=1", "prune=0", "listen=1", "rpcbind=127.0.0.1", "rpcallowip=127.0.0.1"):
        assert want in lines


def test_the_browser_knows_flopcoins_key_fee_and_lock_byte():
    coins = (TEMPLATES / "coins.js").read_text()
    assert re.search(r"COIN_TYPE = \{[^}]*\bflop: 3\b", coins)
    assert re.search(r"WIF = \{[^}]*\bflop: 0x9e\b", coins, re.S), "SECRET_KEY 158: Dogecoin's byte"
    assert re.search(r"WIF_CHAIN = \{[^}]*\bflop: \"Flopcoin mainnet\"", coins, re.S)
    wallet = (TEMPLATES / "wallet_js.js").read_text()
    assert '"flop"' in re.search(r"REAL_COINS = new Set\(\[[^\]]*\]\)", wallet).group(0)
    assert '"flop"' in re.search(r"MAINNETS = new Set\(\[[^\]]*\]\)", wallet).group(0)
    assert re.search(r"CLAIM_FEE: \{[^}]*\bflop: 1_000_000\b", wallet, re.S)
    assert re.search(r"FEE_FLOOR: \{[^}]*\bflop: 1_000_000\b", wallet, re.S)
    assert re.search(r"BIG: \{[^}]*\bflop: 1_000 \* 100_000_000\b", wallet, re.S)
    assert re.search(r"P2SH_VERSION = \{[^}]*\bflop: 22\b", wallet, re.S)
    assert re.search(r"COIN_WORD = \{[^}]*\bflop: \"FLOP\"", wallet, re.S)
    payees = re.search(r"PAYEE_VERSIONS: \[([^\]]*)\]", wallet).group(1)
    assert sorted(int(v) for v in payees.split(",")) == sorted(T.PAYEE_VERSIONS)
    assert "flop" not in re.search(r"NO_CLTV = new Set\(\[[^\]]*\]\)", wallet).group(0)
    assert "flop" not in re.search(r"NO_LOCK = new Set\(\[[^\]]*\]\)", wallet).group(0)
    assert (TEMPLATES / "coin-flop.png").read_bytes()[:4] == b"\x89PNG", "its own logo (src/qt/res/icons/flopcoin.png)"


def test_its_docs_page_is_listed():
    from arcade.web import guide
    assert any(page == "flopcoin.md" for page, *_ in guide.ORDER)
    text = (pathlib.Path(guide.__file__).resolve().parent / "docs" / "flopcoin.md").read_text()
    assert "v2.0.0.0" in text and "txindex=1" in text and "216.45.61.204" in text
    assert "## Flopinals" in text and "18,295" in text


def test_flopinals_are_indexed_from_the_floor_and_move_like_doginals():
    """2026-11-03: "we can incorporate them right now". A scan of the whole chain from block 0 (the ThinkPad's
    node, 2026-11-03) found the first Doginals-style envelope at 18,295, well below the floor: the index starts there,
    as every chain's does below its floor. Dogecoin 1.14's code with no SegWit: the Doginals kind, 240-byte parts, a
    reinscription not numbered (as DigiByte's)."""
    from arcade import doginal_index, ord_chains, ord_inscribe, ord_rules
    assert doginal_index.STARTS["flop"] == 18_295 < FLOP.activation_height, "the first envelope, below the floor"
    assert "flop" in doginal_index.SOURCES
    rules = ord_inscribe.rules_of("flop")
    assert (rules.label, rules.dialect, rules.part, rules.empty_ok) == ("Flopinal", "dogecoin", 240, False), "Doginals' own"
    assert "flop" in ord_inscribe.CHAINS
    assert ord_chains.word("flop") == "Flopinal"
    assert ord_chains.moved("flop") and ord_chains.swappable("flop"), "CLTV is enforced: listed, bought and swapped"
    assert "flop" in ord_chains.EXPLORE_ORDER
    assert "flop" not in ord_rules.NUMBERS_REINSCRIPTIONS and "flop" not in ord_rules.EVERY_INPUT
    wallet = (TEMPLATES / "my_wallet.html").read_text()
    assert 'c.network === "flop" ? "Flopinals"' in wallet


def test_dogefeedx_pills_say_flop():
    from arcade.web.dogefeedx import chains
    body = bytes(range(20))
    assert chains.chain_label(b58check_encode(35, body)) == "FLOP", "an F address"
    assert chains.chain_label("", "flopcoin") == "FLOP"
    pills = (TEMPLATES / "dogefeedx" / "_chain.js.html").read_text()
    assert "F: 'FLOP'" in pills and "flopcoin: 'FLOP'" in pills and ".dfx-chain-flop" in pills


# --- blocks borrowed over the mesh are checked as Flopcoin's own (arcade/rawchain.py) ----------------------------

def _raw_data():
    import gzip
    import json
    path = pathlib.Path(__file__).parent / "data/rawchain/flop-blocks.json.gz"
    return json.loads(gzip.decompress(path.read_bytes()))["flop"]


def test_real_flopcoin_blocks_read_as_core_prints_them_across_auxpow_and_the_fork():
    """Real blocks from Flopcoin Core v2.0.0.0 (the ThinkPad's node, 2026-11-04): 110,101-110,103 around the first AuxPoW
    block (110,102, between two legacy ones), and 124,998-125,000 across the hard fork (125,000: the first block after
    it, with the FlopFund output). Each run links on its own; the work is checked under chain id 0xA3 and an AuxPoW
    block passed off as Dogecoin's (0x62) is refused. 110,102's coinbase pays a P2PK script, which Core prints with its
    F address: rawchain dropped that address for every P2PK output until 2026-11-03."""
    from arcade import rawchain
    data = _raw_data()
    assert [r["height"] for r in data["runs"]["auxpow_start"]] == [110_101, 110_102, 110_103]
    assert [r["height"] for r in data["runs"]["hard_fork"]] == [124_998, 124_999, 125_000]
    auxpow = []
    for run in data["runs"].values():
        before = None
        for row in run:
            block = rawchain.read_block(bytes.fromhex(row["raw"]), row["hash"])
            rawchain.check_work(block, FLOP.auxpow_chain_id, FLOP.pow_algo, FLOP.auxpow_chain_mask)
            rawchain.check_link(block, before)
            before = block
            mine = block.verbose(FLOP, height=row["height"], extra=row["verbose"])
            assert mine == {k: v for k, v in row["verbose"].items() if k != "auxpow"}, row["height"]
            header = rawchain.read_header(bytes.fromhex(row["header"]), row["hash"])
            rawchain.check_work(header, FLOP.auxpow_chain_id, FLOP.pow_algo, FLOP.auxpow_chain_mask)
            if "auxpow" in row["verbose"]:
                auxpow.append(row["height"])
                try:
                    rawchain.check_work(block, 0x62, FLOP.pow_algo, FLOP.auxpow_chain_mask)
                except rawchain.RawError as exc:
                    assert "chain id" in str(exc)
                else:
                    raise AssertionError(f"{row['height']} passed as Dogecoin's")
    assert auxpow == [110_102, 124_998, 124_999, 125_000], "110,101 and 110,103 are legacy"
    p2pk = [o for o in data["runs"]["auxpow_start"][1]["verbose"]["tx"][0]["vout"]
            if o["scriptPubKey"]["type"] == "pubkey"]
    assert p2pk and p2pk[0]["scriptPubKey"]["addresses"][0].startswith("F"), "Core names the P2PK output's F address"
    tx = data["tx"]
    mine = rawchain.read_tx(bytes.fromhex(tx["hex"]), tx["txid"]).verbose(FLOP)
    assert mine == {k: v for k, v in tx.items() if k not in ("blockhash", "blocktime", "confirmations", "time")}


def test_a_caught_up_flopcoin_node_is_ready_though_its_estimate_stops_short_and_it_says_no_initial_download():
    """2026-11-04: a Flopcoin node at the tip says verificationprogress 0.99988 and, as a 1.14 Core, no
    initialblockdownload; it went in and out of "ready", and Flopcoin off the Wallet's coin list. Level with its
    headers it is ready; behind them it is not."""
    from arcade.web.state import AppState

    class Chain:
        def __init__(self, info):
            self.network, self.params, self.info = "flop", FLOP, info

        def rpc(self):
            outer = self

            class R:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def get_blockchain_info(self):
                    return outer.info
            return R()

    state = AppState.__new__(AppState)
    state._sync = {}
    tip = {"blocks": 914807, "headers": 914807, "verificationprogress": 0.9998831745255763}
    assert state._sync_now(Chain(tip))["ready"] is True
    assert state._sync_now(Chain(dict(tip, blocks=900000, verificationprogress=0.98)))["ready"] is False

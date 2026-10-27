"""DigiByte (DGB) mainnet as an add-on chain, the same shape as Bellscoin, Luckycoin and Wojakcoin.

Params come from DigiByte Core 9.26.7 src/kernel/chainparams.cpp and chainparamsbase.cpp, and were checked against
that chain's own node (2026-10-25): block 0; the soft forks' heights (getdeploymentinfo); a D address (version 30),
an S address (version 63), a dgb1q and a dgb1p address made by this arcade's encoders, all valid; a "3" address
(version 5, still listed in chainparams as SCRIPT_ADDRESS_OLD) refused by the node; and a WIF (version 128) that
signmessagewithprivkey accepted, its signature verified against the arcade's D address.
"""
import re
import pathlib

from arcade import config, htlc, rawchain, trade as T, txbuild
from arcade.config import NETWORKS
from arcade.script import b58check_decode, b58check_encode, hash160

DGB = NETWORKS["dgb"]
TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "arcade" / "web" / "templates"
GENESIS = "7497ea1b465eb39f1c8f507bc877078fe016d6fcb6dfad3a64c98dcc6e1e8496"


def test_digibyte_params_are_its_chainparams():
    assert DGB.addon and DGB.real_coins and not DGB.tx_time and not DGB.block_sig
    assert (DGB.pubkeyhash_version, DGB.scripthash_version) == (30, 63)
    assert (DGB.rpc_port, DGB.p2p_port) == (14022, 12024)
    assert DGB.genesis == GENESIS
    assert DGB.auxpow_chain_id is None
    assert DGB.pow_algo == "", "five algorithms: the work is the sharer's word, as on any chain it cannot check"
    assert DGB.bech32_hrp == "dgb" and not DGB.mweb and DGB.trust_progress
    assert DGB.cltv_enforced, "BIP65 is buried at 4,394,880: locks refund as on Litecoin"
    assert (DGB.min_fee_per_kb, DGB.soft_dust, DGB.dust_limit) == (1_000_000, False, 10_000)
    assert (DGB.letter, DGB.family, DGB.net_word, DGB.node_dir, DGB.node_conf) == (
        "G", "DGB", "digibyte", ".digibyte", "digibyte.conf")
    assert config.CHAIN_NAMES["dgb"] == "main"
    assert DGB.activation_height == DGB.messaging_start_height == config.DGB_MAINNET_FLOOR == 24_462_000
    assert DGB.swaps_from is None and DGB.take_from is None
    assert "dgb" in __import__("arcade.cores", fromlist=["OPTIONAL"]).OPTIONAL
    assert all(p.letter != "G" for n, p in NETWORKS.items() if n != "dgb"), "its badge letter is its own"


def test_its_d_is_dogecoins_d_and_its_s_is_nobodys():
    """Version 30 is Dogecoin's ordinary address too: the same key gives the same D string on both chains, and a coin
    sent to it is that key's on either, so nothing is refused for it. 63 is no chain's key-hash byte."""
    assert NETWORKS["doge"].pubkeyhash_version == DGB.pubkeyhash_version == 30
    assert config.unpayable_versions(DGB) == frozenset()
    assert 63 not in {p.pubkeyhash_version for p in NETWORKS.values()}
    assert 30 in config.unpayable_versions(NETWORKS["bel"]), "Bellscoin still refuses its D script addresses"
    assert 30 in T.PAYEE_VERSIONS
    assert T.claim_fee("dgb") == 400_000 and T.fee_floor("dgb") == 10_000


def test_a_send_to_an_s_address_pays_a_script_hash():
    """Without 63 in txbuild.SCRIPTHASH_VERSIONS an S address got a key-hash script and the coins were lost."""
    body = hash160(b"\x51")
    s_addr = b58check_encode(63, body)
    assert s_addr.startswith("S")
    assert txbuild.p2pkh_script(s_addr) == b"\xa9\x14" + body + b"\x87"
    d_addr = b58check_encode(30, body)
    assert txbuild.p2pkh_script(d_addr) == b"\x76\xa9\x14" + body + b"\x88\xac"


def test_payees_on_a_digibyte_send():
    """D and S are paid. A "3" address is refused: DigiByte Core 9.26.7 refuses one too, whatever chainparams list."""
    from arcade.web.app import _check_address

    class Chain:
        network, params, label = "dgb", DGB, "DigiByte"
    body = bytes(range(20))
    assert _check_address(b58check_encode(30, body), mainnet=Chain()) is None
    assert _check_address(b58check_encode(63, body), mainnet=Chain()) is None
    assert _check_address(b58check_encode(5, body), mainnet=Chain()), "a 3 address is not DigiByte's"
    assert _check_address(b58check_encode(56, body), mainnet=Chain()), "a Pepecoin address is not DigiByte's"
    assert "SegWit" in _check_address("dgb1qsafft33gf4rtn5dnxncjrnswcglm0mqdywyd8s", mainnet=Chain())


def test_an_accounts_digibyte_address_is_its_pepecoin_mainnet_key():
    from arcade.web.app import _chain_twin
    body = bytes(range(20))
    assert b58check_decode(_chain_twin(b58check_encode(56, body), DGB)) == (30, body)
    assert _chain_twin(b58check_encode(113, body), DGB) == ""


def test_a_swap_lock_on_digibyte_is_an_s_address():
    """CLTV is enforced, so the lock builders make DigiByte locks, at its own script byte."""
    config.require_cltv(DGB)
    address = htlc.address_of(bytes(40), DGB)
    assert b58check_decode(address)[0] == 63 and address.startswith("S")


def test_the_browser_knows_digibytes_key_fee_and_lock_byte():
    coins = (TEMPLATES / "coins.js").read_text()
    assert re.search(r"COIN_TYPE = \{[^}]*\bdgb: 3\b", coins)
    assert re.search(r"WIF = \{[^}]*\bdgb: 0x80\b", coins, re.S)
    wallet = (TEMPLATES / "wallet_js.js").read_text()
    assert '"dgb"' in re.search(r"REAL_COINS = new Set\(\[[^\]]*\]\)", wallet).group(0)
    assert '"dgb"' in re.search(r"MAINNETS = new Set\(\[[^\]]*\]\)", wallet).group(0)
    assert re.search(r"CLAIM_FEE: \{[^}]*\bdgb: 400_000\b", wallet, re.S)
    assert re.search(r"FEE_FLOOR: \{[^}]*\bdgb: 10_000\b", wallet, re.S)
    assert re.search(r"P2SH_VERSION = \{[^}]*\bdgb: 63\b", wallet, re.S)
    assert 'const NO_CLTV = new Set(["wjk"]);' in wallet, "DigiByte enforces CLTV: its swaps stay open"
    assert (TEMPLATES / "coin-dgb.png").read_bytes()[:4] == b"\x89PNG"


def test_its_docs_page_is_listed():
    from arcade.web import guide
    assert any(page == "digibyte.md" for page, *_ in guide.ORDER)
    text = (pathlib.Path(guide.__file__).resolve().parent / "docs" / "digibyte.md").read_text()
    assert "9.26.7" in text and "sharer's word" in text and "24,462,000" in text


def test_a_caught_up_node_with_a_real_estimate_waits_for_it():
    """DigiByte's chainTxData is real, so its node is ready at 0.9999 like Litecoin's, not by headers alone."""
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
            self.network, self.params, self._rpc = params.name, params, Rpc(info)
        def rpc(self):
            return self._rpc

    state = AppState.__new__(AppState)
    state._sync = {}
    tip = {"blocks": 24_449_834, "headers": 24_449_834, "verificationprogress": 0.99999, "initialblockdownload": False}
    assert state._sync_now(Chain(DGB, tip))["ready"] is True
    early = dict(tip, verificationprogress=0.95)
    assert state._sync_now(Chain(DGB, early))["ready"] is False


def test_digibyte_ordinals_follow_wonky_ord_digibyte():
    """reallyshadydev/wonky-ord-digibyte (chain.rs first_inscription_height 0, inscription.rs reading the first input's
    script, 0 parts not an inscription, a new one looked for only when none sits at offset 0) and digibyteordinals.js
    (240-byte parts): the Doginals kind, numbered as wonky-ord does, so reinscriptions are not numbered."""
    from arcade import collection_json, doginal_index, ord_chains, ord_inscribe, ord_rules
    assert doginal_index.STARTS["dgb"] == 0 and "wonky-ord-digibyte" in doginal_index.SOURCES["dgb"]
    assert "dgb" not in doginal_index.TAPROOT
    assert ord_rules.dialect_of("dgb") == "dogecoin" and "dgb" not in ord_rules.NUMBERS_REINSCRIPTIONS
    rules = ord_inscribe.rules_of("dgb")
    assert (rules.label, rules.dialect, rules.part, rules.empty_ok) == ("DigiByte ordinal", "dogecoin", 240, False)
    assert "dgb" in ord_inscribe.CHAINS and "dgb" in collection_json.CHAINS
    assert ord_chains.word("dgb") == "DigiByte ordinal" and ord_chains.tab("dgb") == "dgbordinals"
    assert ord_chains.moved("dgb"), "sent, listed and bought like Doginals"
    assert ord_chains.swappable("dgb"), "CLTV is enforced and S is payable: a DigiByte ordinal can be swapped"


def test_a_real_digibyte_block_is_read_and_its_work_is_not_judged_by_one_algorithm():
    """tests/data/rawchain/dgb-blocks.json.gz: block 24,450,197, fetched from a peer by DigiByte Core 9.26.7
    (getblockfrompeer) on 2026-10-25, mined with sha256d, one of DigiByte's five algorithms. The reader gives Core's own
    txids. One algorithm's check would refuse every block mined with the other four, so the chain's work is not judged:
    pow_algo "" checks nothing, and a borrowed block is still proved by hash, merkle root and linkage."""
    import gzip
    import json
    fix = json.loads(gzip.open(pathlib.Path(__file__).resolve().parent / "data" / "rawchain" / "dgb-blocks.json.gz",
                               "rt").read())
    block = fix["blocks"][0]
    got = rawchain.read_block(bytes.fromhex(block["raw"]), block["hash"])
    assert [t.txid for t in got.txs] == block["tx"]
    assert got.version == block["version"] and block["pow_algo"] == "sha256d"
    rawchain.check_work(got, algo=DGB.pow_algo)
    rawchain.check_work(got, algo="sha256d")
    try:
        rawchain.check_work(got, algo="scrypt")
    except rawchain.RawError as exc:
        assert "work does not meet" in str(exc)
    else:
        raise AssertionError("a sha256d DigiByte block does not carry scrypt work: one algorithm cannot judge them all")


def test_every_chains_script_address_is_paid_as_a_script_or_refused():
    """Each chain's P2SH byte is either one txbuild pays as a script hash, or one the payee check refuses because it is
    another chain's key-hash byte (config.unpayable_versions: Bellscoin's 30). Anything else is a send that _check_address
    lets through and txbuild turns into a key-hash script, and the coins are lost. Kotia's 45 was that until 2026-10-25."""
    from arcade.web.app import _check_address
    body = bytes(range(20))
    for name, p in NETWORKS.items():
        v = p.scripthash_version
        if v in config.unpayable_versions(p):
            continue
        assert v in txbuild.SCRIPTHASH_VERSIONS, f"{name}: a {v} address would get a key-hash script"
        assert txbuild.p2pkh_script(b58check_encode(v, body)) == b"\xa9\x14" + body + b"\x87", name
    key_hashes = {p.pubkeyhash_version for p in NETWORKS.values()}
    assert not (txbuild.SCRIPTHASH_VERSIONS & key_hashes), "a script byte read the same on every chain is nobody's key"

    class Kotia:
        network, params, label = "kot", NETWORKS["kot"], "Kotia"
    assert _check_address(b58check_encode(45, body), mainnet=Kotia()) is None, "a Kotia script address is paid"


def test_digibyte_tokens_are_dgb_20():
    """wonky-ord-digibyte reads DRC-20's rules under "p":"dgb-20" (src/drc20/params.rs PROTOCOL_LITERAL): a "drc-20"
    inscription on DigiByte is no token there, and a "dgb-20" one is."""
    from arcade import ord_rules
    assert ord_rules.token_protocol("dgb") == "dgb-20"
    body = b'{"p":"dgb-20","op":"deploy","tick":"digi","max":"21000000","lim":"1000"}'
    assert ord_rules.token_op("dgb-20", b"text/plain", body)["op"] == "deploy"
    assert ord_rules.token_op("dgb-20", b"text/plain", body.replace(b"dgb-20", b"drc-20")) is None

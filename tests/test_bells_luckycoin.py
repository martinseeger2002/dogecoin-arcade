"""Bellscoin (BEL) and Luckycoin (LKY) as add-on chains (SadFrog's side, 2026-10-22: "integrate them as fully as
Dogecoin"). Both are Params alone, in AppState.more: they show grayed out until their node has synced, then an
account holds them on its one key (coin type 3), they trade at the mainnet tier, and their coins are guarded.

Bellscoin's P2SH byte is Dogecoin's P2PKH "D" (30), so a "D..." payee on Bellscoin is refused and the arcade makes
no P2SH lock there, rather than fund a script hash as a key hash."""
import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

from arcade import config, funding, htlc, trade as T             # noqa: E402
from arcade.config import NETWORKS                               # noqa: E402
from arcade.script import b58check_decode, b58check_encode       # noqa: E402

HASH = bytes(range(20))
PEP_MAIN = b58check_encode(56, HASH)
DOGE = b58check_encode(30, HASH)
BEL_B = b58check_encode(25, HASH)
BEL_P2SH = b58check_encode(30, HASH)       # the same string as DOGE
LKY_K = b58check_encode(47, HASH)
LTC_L = b58check_encode(48, HASH)


def _with(state, monkeypatch, *nets, ready=True):
    from arcade.web.state import ChainContext
    more = {n: ChainContext(network=n, role="ledger", label=NETWORKS[n].label,
                            conf=pathlib.Path(f"/nonexistent/{NETWORKS[n].node_conf}")) for n in nets}
    monkeypatch.setattr(state, "more", more, raising=False)
    said = {"ready": ready, "progress": 1.0 if ready else 0.5, "online": True,
            "blocks": 100 if ready else 50, "headers": 100}
    real = type(state).chain_sync
    monkeypatch.setattr(type(state), "chain_sync",
                        lambda self, chain: said if chain.network in nets else real(self, chain))
    return [more[n] for n in nets]


def test_each_is_a_network_of_its_own():
    bel, lky = NETWORKS["bel"], NETWORKS["lky"]
    assert (bel.pubkeyhash_version, bel.scripthash_version, bel.rpc_port, bel.p2p_port) == (25, 30, 19918, 19919)
    assert (lky.pubkeyhash_version, lky.scripthash_version, lky.rpc_port, lky.p2p_port) == (47, 5, 9918, 9917)
    assert bel.genesis == "e5be24df57c43a82d15c2f06bda961296948f8f8eb48501bed1efb929afe0698"
    assert lky.genesis == "9b7bce58999062b63bfb18586813c42491fa32f4591d8d3043cb4fa9e551541b"
    assert config.CHAIN_NAMES["bel"] == config.CHAIN_NAMES["lky"] == "main", "genesis tells them from Pepecoin"
    assert bel.activation_height == bel.messaging_start_height == config.BEL_MAINNET_FLOOR
    assert lky.activation_height == lky.messaging_start_height == config.LKY_MAINNET_FLOOR
    assert (bel.auxpow_chain_id, bel.auxpow_chain_mask) == (16, 0x1F)
    assert (lky.auxpow_chain_id, lky.auxpow_chain_mask) == (0x2013, 0xFFFF)
    # Relay: Bells 3 is Bitcoin-shaped (no soft dust), Luckycoin is Dogecoin's.
    assert (bel.min_fee_per_kb, bel.soft_dust, bel.dust_limit) == (10_000, False, 10_000)
    assert (lky.min_fee_per_kb, lky.soft_dust, lky.dust_limit) == (1_000_000, True, 1_000_000)
    for p in (bel, lky):
        assert p.addon and p.real_coins and p.swaps_from is None and p.take_from is None, \
            "real coins: no swaps, takes or the rest until both sides agree on them"
    from arcade import cores
    assert {"bel", "lky"} <= cores.OPTIONAL


def test_the_chains_say_what_they_are():
    from arcade.web.state import ChainContext
    bel = ChainContext(network="bel", role="ledger", label="Bellscoin")
    lky = ChainContext(network="lky", role="ledger", label="Luckycoin")
    assert (bel.family, bel.letter, bel.title, bel.net) == ("BEL", "B", "Bellscoin (BEL) – Main", "bellscoin")
    assert (lky.family, lky.letter, lky.title, lky.net) == ("LKY", "K", "Luckycoin (LKY) – Main", "luckycoin")
    for c in (bel, lky):
        assert c.is_addon and c.is_mainnet and not c.is_doge and not c.is_litecoin


def test_a_syncing_chain_is_shown_and_never_used(client, monkeypatch):
    app, state = client
    bel, lky = _with(state, monkeypatch, "bel", "lky", ready=False)
    assert bel in state.all_chains and lky in state.all_chains, "shown"
    assert bel not in state.token_chains and lky not in state.token_chains, "not indexed or offered"


def test_only_bellscoin_has_a_script_byte_another_chain_uses_for_keys():
    assert {n: sorted(config.unpayable_versions(p)) for n, p in NETWORKS.items() if config.unpayable_versions(p)} \
        == {"bel": [30]}
    with pytest.raises(ValueError, match="P2SH"):
        htlc.address_of(b"\x51", NETWORKS["bel"])
    assert htlc.address_of(b"\x51", NETWORKS["lky"]).startswith("3")
    assert htlc.address_of(b"\x51", NETWORKS["doge"]).startswith("A")


def test_a_bellscoin_d_address_is_refused_and_a_dogecoin_one_still_paid():
    from arcade.web.app import _check_address
    from arcade.web.state import ChainContext
    bel = ChainContext(network="bel", role="ledger", label="Bellscoin")
    doge = ChainContext(network="doge", role="ledger", label="Dogecoin")
    lky = ChainContext(network="lky", role="ledger", label="Luckycoin")
    assert _check_address(BEL_B, mainnet=bel) is None
    assert "script (P2SH)" in (_check_address(BEL_P2SH, mainnet=bel) or "")
    assert _check_address(DOGE, mainnet=doge) is None, "Dogecoin's D... is untouched"
    assert _check_address(LKY_K, mainnet=lky) is None
    assert "Litecoin" in (_check_address(LTC_L, mainnet=lky) or ""), "an L... of Litecoin is not a Luckycoin one"
    assert "Bellscoin" in (_check_address(BEL_B, mainnet=doge) or "")


def test_each_address_is_the_pepecoin_keys_twin():
    from arcade.web.app import _chain_twin
    assert b58check_decode(_chain_twin(PEP_MAIN, NETWORKS["bel"])) == (25, HASH)
    assert b58check_decode(_chain_twin(PEP_MAIN, NETWORKS["lky"])) == (47, HASH)
    assert _chain_twin(DOGE, NETWORKS["bel"]) == "" and _chain_twin("nope", NETWORKS["lky"]) == ""


def test_an_at_tag_pays_their_twin_on_each(app_state, monkeypatch):
    from arcade.web import app as applib
    bel, lky = _with(app_state, monkeypatch, "bel", "lky")
    monkeypatch.setattr(applib.taglib, "looks_like_a_tag", lambda d: d.startswith("@"))
    monkeypatch.setattr(applib.taglib, "normalise", lambda d: d.lstrip("@"))
    real = applib._tag_address
    monkeypatch.setattr(applib, "_tag_address",
                        lambda state, d, *, mainnet: PEP_MAIN if mainnet is True else real(state, d, mainnet=mainnet))
    assert applib._tag_address(app_state, "@frog", mainnet=bel) == BEL_B
    assert applib._tag_address(app_state, "@frog", mainnet=lky) == LKY_K


def test_their_coins_are_guarded_each_by_its_own(app_state, monkeypatch):
    monkeypatch.setattr(funding, "GUARD", None)
    monkeypatch.setattr(funding, "GUARDS", [])
    monkeypatch.setattr(funding, "GUARD_BY_NET", {})
    _with(app_state, monkeypatch, "bel", "lky")
    TestClient(__import__("arcade.web.app", fromlist=["create_app"]).create_app(app_state))
    bel, lky = funding.guard_on("bel"), funding.guard_on("lky")
    assert bel is not None and lky is not None and bel is not lky
    assert funding.guard_for(BEL_B) is bel and funding.guard_for(LKY_K) is lky
    assert funding.guard_for(DOGE) is not bel, "a D... is never read as a Bellscoin script"
    assert funding.guard_for(b58check_encode(5, HASH)) is not lky, "nor a 3... as a Luckycoin one"


def test_they_trade_at_the_mainnet_tier_with_their_own_lines():
    for net in ("bel", "lky"):
        assert T.Leg(network=net, kind="coin", asset="", amount=1, giver_key="", taker_key="").tier() == "mainnet"
        assert net in T.BIG
    assert T.claim_fee("bel") == 10_000 and T.claim_fee("lky") == 1_000_000


def test_the_browser_has_their_keys_and_tables():
    root = pathlib.Path(__file__).resolve().parent.parent / "arcade/web/templates"
    coins = (root / "coins.js").read_text()
    assert "bel: 3, lky: 3, kot: 3, wjk: 3}" in coins, "one key for every real-coin chain"
    assert "bel: 0x99, lky: 0xaf, kot: 0xad, wjk: 0xc9}" in coins, "each Core's own WIF byte (SECRET_KEY 153 and 175)"
    wallet = (root / "wallet_js.js").read_text()
    assert 'REAL_COINS = new Set(["main", "doge", "ltc", "bel", "lky", "kot", "wjk"])' in wallet
    for icon in ("coin-bel.png", "coin-lky.png"):
        assert (root / icon).read_bytes()[:4] == b"\x89PNG"


def test_the_ord_index_reads_bellscoin_from_nintondos_height():
    from arcade import doginal_index, ord_rules
    assert doginal_index.STARTS["bel"] == 22_470 and "bel" in doginal_index.TAPROOT
    assert doginal_index.STARTS["lky"] == 88_554 and "lky" not in doginal_index.TAPROOT
    assert ord_rules.dialect_of("bel") == ord_rules.dialect_of("lky") == "dogecoin"


# --- blocks borrowed over the mesh are checked as each chain's own (arcade/rawchain.py) ------------------------

def _raw_data():
    import gzip
    import json
    path = pathlib.Path(__file__).parent / "data/rawchain/bel-lky-blocks.json.gz"
    return json.loads(gzip.decompress(path.read_bytes()))


@pytest.mark.parametrize("net", sorted(_raw_data()))
def test_raw_blocks_read_as_core_prints_them_and_carry_their_work(net):
    from arcade import rawchain
    params, before = NETWORKS[net], None
    rows = [r for r in _raw_data()[net] if "raw" in r]
    for row in rows:
        block = rawchain.read_block(bytes.fromhex(row["raw"]), row["hash"])
        rawchain.check_work(block, params.auxpow_chain_id, params.pow_algo, params.auxpow_chain_mask)
        rawchain.check_link(block, before)
        before = block
        mine = block.verbose(params, height=row["height"], extra=row["verbose"])
        assert mine == {k: v for k, v in row["verbose"].items() if k != "auxpow"}
        header = rawchain.read_header(bytes.fromhex(row["header"]), row["hash"])
        rawchain.check_work(header, params.auxpow_chain_id, params.pow_algo, params.auxpow_chain_mask)
    block = rawchain.read_block(bytes.fromhex(rows[-1]["raw"]))
    with pytest.raises(rawchain.RawError, match="chain id"):
        rawchain.check_work(block, 0x62, params.pow_algo, params.auxpow_chain_mask)    # passed off as Dogecoin's
    tx = next(r["tx"] for r in _raw_data()[net] if "tx" in r)
    mine = rawchain.read_tx(bytes.fromhex(tx["hex"]), tx["txid"]).verbose(params)
    assert mine == {k: v for k, v in tx.items() if k not in ("blockhash", "blocktime", "confirmations", "time")}


# --- Bellinals and Luckinals: Admin -> Ords opt-ins, and the wallet's view (2026-10-22) ---------------------------

def test_admin_ords_offers_each_chain_off_until_turned_on(client, monkeypatch):
    app, state = client
    _with(state, monkeypatch, "bel", "lky")
    state.set_setting("public_hosts", ["node.dogecoinarcade.com"])
    local, write = {"host": "127.0.0.1:8420"}, {"x-arcade-admin": "1"}
    try:
        rows = {r["net"]: r for r in app.get("/admin/api/ords", headers=local).json()["rows"]}
        assert rows["bel"]["base"] == 22_470 and rows["lky"]["base"] == 88_554
        assert "Nintondo" in rows["bel"]["source"] and rows["bel"]["mode"] == rows["lky"]["mode"] == "off"
        for net in ("bel", "lky"):
            assert app.post("/admin/api/ords", headers={**local, **write}, json={"net": net, "mode": "walk"}).json()["ok"]
            assert state.ord_mode(net) == "walk"
    finally:
        state.set_setting("public_hosts", [])


def test_the_wallet_moves_bellinals_and_shows_luckinals():
    from arcade import ord_chains
    page = (pathlib.Path(__file__).resolve().parent.parent / "arcade/web/templates/my_wallet.html").read_text()
    table = ord_chains.wallet_table()
    assert table["bel"]["words"] == ["Bellinal", "BEL", "Bellscoin"] and table["lky"]["words"] == ["Luckinal", "LKY", "Luckycoin"]
    # Bellinals are sent and listed (2026-10-25) but not swapped: no lock is made on Bellscoin (its P2SH byte, above).
    assert (table["bel"]["send"], table["bel"]["list"], table["bel"]["swap"]) == (True, True, False)
    assert not any(table[n][k] for n in ("lky", "wjk") for k in ("send", "list", "swap"))
    assert all(table[n]["send"] and table[n]["list"] and table[n]["swap"] for n in ("doge", "main"))
    assert "const ORD = {{ ord_wallet()|tojson }};" in page
    assert "!ORD_WORDS[c.network]" in page and "const viewOnly = !can.send;" in page

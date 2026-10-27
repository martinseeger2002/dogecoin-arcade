"""Kotia (KOT) as an add-on chain (tbuuol's side, 2026-10-23: large game assets in OP_RETURN). Params alone, like
Bellscoin and Luckycoin, plus what no chain here had before: Peercoin-style transactions with an `nTime` after the
version (Params.tx_time), and staked blocks that end with a block signature (Params.block_sig).

tests/data/rawchain/kot-blocks.json.gz holds real Kotia blocks and a transaction signed by Kotia's own Core with a
throwaway key, so the reading and the sighash are held to what Kotia itself does."""
import dataclasses
import gzip
import json
import pathlib
import re

import pytest

from arcade import config, funding, rawchain, trade as T
from arcade.config import NETWORKS
from arcade.doginal_market import verify_signature
from arcade.script import b58check_decode, b58check_encode
from arcade.txbuild import build_raw_tx

DATA = pathlib.Path(__file__).resolve().parent / "data" / "rawchain" / "kot-blocks.json.gz"
FIX = json.loads(gzip.open(DATA, "rt").read())
KOT = NETWORKS["kot"]
TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "arcade" / "web" / "templates"


def test_kotia_params_are_its_chainparams():
    assert KOT.addon and KOT.real_coins and KOT.tx_time and KOT.block_sig
    assert (KOT.pubkeyhash_version, KOT.scripthash_version) == (46, 45)
    assert (KOT.rpc_port, KOT.p2p_port) == (46173, 46174)
    assert KOT.genesis == "000008e4586479cd9dc5832b262414a60f152381e371cf7940ffe9f5aa4b80a0"
    assert KOT.auxpow_chain_id is None and KOT.pow_algo == "" and not KOT.bech32_hrp and not KOT.mweb
    assert (KOT.min_fee_per_kb, KOT.soft_dust, KOT.dust_limit) == (10_000, False, 100_000)
    assert config.CHAIN_NAMES["kot"] == "main"
    assert KOT.activation_height == KOT.messaging_start_height == config.KOT_MAINNET_FLOOR
    # Swaps and the rest stay closed, as on every mainnet.
    assert KOT.swaps_from is None and KOT.take_from is None


def test_only_kotia_is_timed():
    """Every other chain keeps its bytes: no nTime, no block signature."""
    assert [n for n, p in NETWORKS.items() if p.tx_time or p.block_sig] == ["kot"]


def test_its_address_bytes_clash_with_no_other_chain():
    assert config.unpayable_versions(KOT) == frozenset()
    others = {v for n, p in NETWORKS.items() if n != "kot" for v in (p.pubkeyhash_version, p.scripthash_version)}
    assert not {46, 45} & others
    assert 46 in T.PAYEE_VERSIONS


def test_an_accounts_kotia_address_is_its_pepecoin_mainnet_key():
    from arcade.web.app import _chain_twin
    # @liteworldsquest's two public addresses (2026-10-23), derived in coins.js on coin type 3.
    assert _chain_twin("PuWjvTp4cMnHiysjL1HmX32x2ZuWzTCkn4", KOT) == "Kt8i5NqBWZ9XYeUs5oxafnK5jXL5tEp3Kn"
    assert _chain_twin(b58check_encode(113, bytes(20)), KOT) == ""          # a testnet key is not a KOT key


@pytest.mark.parametrize("block", FIX["blocks"], ids=lambda b: str(b["height"]))
def test_real_kotia_blocks_are_read_and_proved(block):
    raw = bytes.fromhex(block["raw"])
    got = rawchain.read_block(raw, block["hash"], timed=True, signed=True)
    assert [t.txid for t in got.txs] == block["tx"]
    assert got.time == block["time"] and all(t.time is not None and t.time <= got.time for t in got.txs)
    for t in got.txs:
        assert rawchain.read_tx(t.raw, t.txid, timed=True).time == t.time


def test_a_kotia_block_read_untimed_is_refused():
    block = FIX["blocks"][0]
    with pytest.raises(rawchain.RawError):
        rawchain.read_block(bytes.fromhex(block["raw"]), block["hash"])


def _tx():
    t = FIX["tx"]
    prev = dict(t["prev"])
    outputs = [(v, bytes.fromhex(s)) for v, s in t["outputs"]]
    return t, prev, outputs


def test_the_unsigned_transaction_is_what_kotia_decodes():
    t, prev, outputs = _tx()
    raw = build_raw_tx([(prev["txid"], prev["vout"])], outputs, ntime=t["ntime"])
    assert raw == t["unsigned"]
    tx = rawchain.read_tx(bytes.fromhex(raw), t["decoded_txid"], timed=True)
    assert tx.time == t["ntime"]


def test_our_sighash_is_the_one_kotias_core_signed():
    """Kotia's Core signed this transaction; its signature verifies against the digest built here only if that
    digest is Kotia's own -- with the nTime after the version."""
    t, prev, outputs = _tx()
    signed = rawchain.read_tx(bytes.fromhex(t["signed_by_kotiad"]), timed=True)
    script_sig = signed.vin[0]["script"]
    sig = script_sig[1:1 + script_sig[0]]
    assert sig[-1] == funding.SIGHASH_ALL
    coin = {"txid": prev["txid"], "vout": prev["vout"]}
    script = bytes.fromhex(prev["scriptPubKey"])
    digest = funding.sighash([coin], outputs, 0, script, ntime=t["ntime"])
    assert verify_signature(digest, sig[:-1], bytes.fromhex(t["pubkey"]))
    # Without the time it is a different digest, and the same signature is worth nothing.
    assert not verify_signature(funding.sighash([coin], outputs, 0, script), sig[:-1], bytes.fromhex(t["pubkey"]))


def test_assemble_puts_the_time_back():
    t, prev, outputs = _tx()
    signed = rawchain.read_tx(bytes.fromhex(t["signed_by_kotiad"]), timed=True)
    script_sig = signed.vin[0]["script"]
    sig = script_sig[1:1 + script_sig[0]]
    unsigned = funding.Unsigned(raw=t["unsigned"], inputs=[{**prev, "address": t["address"]}], outputs=outputs,
                                ntime=t["ntime"])
    assert funding.assemble(unsigned, [sig.hex()], bytes.fromhex(t["pubkey"])) == t["signed_by_kotiad"]


def test_build_writes_the_time_on_kotia_only(monkeypatch):
    monkeypatch.setattr(funding.time, "time", lambda: 1_800_000_000.5)
    assert funding.tx_time(KOT) == 1_800_000_000
    assert funding.tx_time(NETWORKS["lky"]) is None and funding.tx_time(NETWORKS["main"]) is None
    # A KOT transaction is four bytes bigger, and priced so.
    untimed = dataclasses.replace(KOT, tx_time=False)
    assert funding.price(1, [], 10_000, True, KOT) == funding.price(1, [], 10_000, True, untimed) + 40


def test_untimed_builders_are_refused_on_kotia_alone():
    with pytest.raises(funding.FundingError, match="not open on Kotia"):
        funding.untimed(KOT, "trade")
    funding.untimed(NETWORKS["lky"], "trade")
    with pytest.raises(funding.FundingError, match="not open on Kotia"):
        funding.build_sweep(None, KOT, "Kt8i5NqBWZ9XYeUs5oxafnK5jXL5tEp3Kn", [], 10_000)


def test_the_browser_knows_kotias_key_and_its_timed_transactions():
    coins = (TEMPLATES / "coins.js").read_text()
    assert re.search(r"COIN_TYPE = \{[^}]*\bkot: 3\b", coins)
    assert re.search(r"WIF = \{[^}]*\bkot: 0xad\b", coins, re.S)
    assert 'TX_TIME = new Set(["kot"])' in coins
    # The shape is the browser's own choice, from the key it signs with -- not from the offer alone.
    assert "parseTx(offer.raw, TX_TIME.has(keys.network || offer.chain))" in coins
    assert coins.count("...timeField(tx)") == 3
    wallet = (TEMPLATES / "wallet_js.js").read_text()
    assert '"kot"' in re.search(r"REAL_COINS = new Set\(\[[^\]]*\]\)", wallet).group(0)
    assert (TEMPLATES / "coin-kot.png").is_file()


def test_a_wif_decodes_as_kotias_secret_key():
    version, _payload = b58check_decode(FIX["tx"]["wif"])
    assert version == 173


def test_a_synced_kotia_is_ready_though_its_core_never_says_initialblockdownload():
    """Kotia's getblockchaininfo (Bitcoin 0.14) has no `initialblockdownload`; its progress decides (2026-10-25: the
    chain stayed grayed out in Admin -> Chains while its node was synced)."""
    import contextlib
    import types
    from arcade.web.state import AppState

    def chain(info):
        rpc = types.SimpleNamespace(get_blockchain_info=lambda: info)
        return types.SimpleNamespace(network="kot", rpc=lambda: contextlib.nullcontext(rpc))

    def ready(info):
        me = types.SimpleNamespace(_sync={}, SYNC_SECONDS=60)
        return AppState._sync_now(me, chain(info))["ready"]

    kotia = {"chain": "main", "blocks": 1_529_499, "headers": 1_529_499, "verificationprogress": 1,
             "difficulty": {"proof-of-work": 59.95, "proof-of-stake": 869273.09}}
    assert ready(kotia)
    assert not ready({**kotia, "verificationprogress": 0.42})
    assert not ready({**kotia, "headers": kotia["blocks"] + 50})
    # A Core that does say it still decides by it.
    assert not ready({**kotia, "initialblockdownload": True})
    assert ready({**kotia, "initialblockdownload": False})


def test_getblock_with_a_number_is_answered_on_a_core_that_takes_a_boolean():
    """Kotia 1.0.0 (Bitcoin 0.14) refuses getblock's numeric verbosity: "JSON value is not a boolean as expected"
    (2026-10-25: its index never read a block, so no balance or history). The client learns it once and answers
    0, 1 and 2 from `getblock h false|true` and getrawtransaction."""
    from arcade.config import RpcCredentials
    from arcade.rpc import RpcClient
    block = FIX["blocks"][0]
    asked = []

    class Kotia(RpcClient):
        def _post(self, payload):
            if isinstance(payload, list):
                asked.append(("batch", len(payload)))
                return [{"id": p["id"], "error": None,
                         "result": {"txid": p["params"][0], "vout": [], "hex": "", "blockhash": block["hash"],
                                    "confirmations": 9, "time": 1, "blocktime": 1}} for p in payload]
            method, params = payload["method"], payload["params"]
            asked.append((method, params[1:]))
            if method == "getblock" and len(params) > 1 and not isinstance(params[1], bool):
                return {"id": payload["id"], "result": None,
                        "error": {"code": -1, "message": "JSON value is not a boolean as expected"}}
            if method == "getblock":
                return {"id": payload["id"], "error": None,
                        "result": {"hash": block["hash"], "tx": block["tx"]} if params[1] else block["raw"]}
            raise AssertionError(method)

    fields = RpcCredentials.__dataclass_fields__
    creds = RpcCredentials(**{k: v for k, v in {"host": "127.0.0.1", "port": 1, "user": "u", "password": "p"}.items()
                              if k in fields})
    rpc = Kotia(creds)
    assert rpc.get_block_hex(block["hash"]) == block["raw"]
    assert asked[:2] == [("getblock", [0]), ("getblock", [False])]
    asked.clear()
    v2 = rpc.get_block(block["hash"], 2)                  # learned: no numeric ask any more
    assert asked[0] == ("getblock", [True]) and asked[1] == ("batch", len(block["tx"]))
    assert [t["txid"] for t in v2["tx"]] == block["tx"]
    assert all("blockhash" not in t and "confirmations" not in t for t in v2["tx"])
    assert rpc.call("getblock", block["hash"], True)["tx"] == block["tx"]      # a boolean passes straight through


def test_the_node_keys_own_send_carries_the_time(monkeypatch):
    """2026-10-27: the operator's send from the node key (keywallet.prepare) wrote no nTime, so Kotia's Core would
    refuse it. Its digest is now Kotia's own (the one Kotia's Core signed above), and its bytes read back timed."""
    from types import SimpleNamespace
    from arcade import escrow, keywallet as K
    from arcade.node_trader import HeldKey
    from arcade.script import hash160
    t, prev, outputs = _tx()
    coin = {"txid": prev["txid"], "vout": prev["vout"]}
    script = bytes.fromhex(prev["scriptPubKey"])
    assert escrow.sighash([coin], outputs, 0, script, ntime=t["ntime"]) == \
        funding.sighash([coin], outputs, 0, script, ntime=t["ntime"])
    key = HeldKey("11" * 32)
    mine = b58check_encode(NETWORKS["kot"].pubkeyhash_version, hash160(key.pubkey))
    chain = SimpleNamespace(params=NETWORKS["kot"], network="kot")
    monkeypatch.setattr(K, "coins", lambda st, c: [{"txid": "ab" * 32, "vout": 0, "value": 5 * 10 ** 8}])
    monkeypatch.setattr(K.time, "time", lambda: 1_800_000_000)
    prepared = K.prepare(SimpleNamespace(key_address=lambda c: mine), chain, mine, 10 ** 8, key=key)
    tx = rawchain.read_tx(bytes.fromhex(prepared.raw), timed=True)
    assert tx.time == 1_800_000_000 and len(tx.vin) == 1
    script_sig = tx.vin[0]["script"]
    sig = script_sig[1:1 + script_sig[0]]
    paid = [(int(v), bytes(s)) for v, s in tx.vout]
    digest = funding.sighash([{"txid": "ab" * 32, "vout": 0}], paid, 0, bytes.fromhex(
        "76a914" + hash160(key.pubkey).hex() + "88ac"), ntime=1_800_000_000)
    assert verify_signature(digest, sig[:-1], key.pubkey), "signed over Kotia's timed digest"


def test_no_swap_lock_or_escrow_is_made_where_it_could_not_settle():
    """2026-10-27: a Kotia cross-chain order could be listed and its lock funded, but the lock's claim and refund are
    built without Kotia's nTime, so its Core would refuse both and the coins would stay locked. Bellscoin orders were
    listed and failed only when taken (its script addresses share Dogecoin's "D"). Both are refused in every
    cross-chain order, in the server's lock builders, and in the browser before anything is locked."""
    import time
    from arcade import escrow, htlc, trade_orders
    from arcade.config import MAINNET
    with pytest.raises(ValueError, match="carry a time"):
        config.require_untimed(KOT)
    config.require_untimed(MAINNET)
    with pytest.raises(ValueError, match="carry a time"):
        htlc.address_of(bytes(40), KOT)
    with pytest.raises(ValueError, match="carry a time"):
        escrow.address_of(bytes(40), KOT)
    order = {"id": "kot-order-1", "trade_key": "02" + "11" * 32, "msg_key": "22" * 32, "node": "33" * 32,
             "expires": int(time.time()) + 3600,
             "give": {"network": "kot", "kind": "coin", "asset": "", "amount": 100_000_000},
             "get": {"network": "main", "kind": "coin", "asset": "", "amount": 100_000_000}}
    for give in ("kot", "bel"):
        with pytest.raises(trade_orders.OrderError):
            trade_orders.check_order({**order, "give": {**order["give"], "network": give}})
        with pytest.raises(trade_orders.OrderError):
            trade_orders.check_order({**order, "give": order["get"], "get": {**order["give"], "network": give}})
    assert trade_orders.check_order({**order, "give": {**order["give"], "network": "doge"}})
    wallet = (TEMPLATES / "wallet_js.js").read_text()
    assert 'const NO_LOCK = new Set(["bel", "kot"]);' in wallet
    body = wallet[wallet.index("async function redeemsOf(trade) {"):]
    assert body.index("NO_LOCK.has(leg.network)") < body.index("coins.htlcRedeem("), "refused before a lock is made"


def test_every_chains_script_address_is_paid_as_a_script_or_refused():
    """2026-10-27: Kotia's P2SH byte 45 was in neither txbuild.SCRIPTHASH_VERSIONS nor unpayable_versions, so a send
    to a "K..." script address paid its hash as a key hash and the coins were lost. Every chain's script byte is now
    paid as a script, or refused as a payee where it is another chain's key byte."""
    from arcade import txbuild
    from arcade.script import b58check_encode
    for name, params in NETWORKS.items():
        version = params.scripthash_version
        if version in config.unpayable_versions(params):
            continue
        address = b58check_encode(version, bytes(range(20)))
        assert txbuild.p2pkh_script(address) == bytes([0xA9, 0x14]) + bytes(range(20)) + bytes([0x87]), name

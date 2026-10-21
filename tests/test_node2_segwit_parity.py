"""Node 2's parity + litecoind check for team/ltctest-electrum-paths (PR #4), asked by tbuuol's side.

1. Browser vs Python: coins.js electrumWallet (legacy m/44', P2SH-segwit m/49', native m/84') gives the
   same keys and addresses as arcade/seed.py + arcade/script.py, and sighashWitness gives the same
   32 bytes as funding.sighash_witness, for SIGHASH_ALL and ALL|ANYONECANPAY.
2. litecoind (the live testnet node, read-only RPC): each WIF the browser shows derives, in Litecoin
   Core's own descriptor code, to the very address the browser shows.
3. litecoind regtest: a P2WPKH coin spent with a BIP143 signature, signed once by Python and once by
   the browser, is accepted (testmempoolaccept) and mined.
"""
import dataclasses
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_coins_browser import browser, served, loaded                # noqa: E402,F401
from test_funding import N, _pubkey, _sign                           # noqa: E402
import hashlib, hmac                                                 # noqa: E402

from arcade import fees, funding, script, seed                       # noqa: E402
from arcade.config import NETWORKS                                   # noqa: E402

P = NETWORKS["ltctest"]
H = 0x80000000
PHRASE = ("abandon abandon abandon abandon abandon abandon "
          "abandon abandon abandon abandon abandon about")
SEED = seed.to_seed(PHRASE)
V = {"pubkeyhash": P.pubkeyhash_version, "scripthash": P.scripthash_version,
     "wif": 0xef, "bech32Hrp": P.bech32_hrp}
KINDS = {"legacy": 44, "p2shSegwit": 49, "nativeSegwit": 84}


def _bip32(seed_bytes: bytes, *path: int) -> bytes:
    """Plain BIP32 (seed.path hardens every step on purpose, so it is not this)."""
    I = hmac.new(b"Bitcoin seed", seed_bytes, hashlib.sha512).digest()
    k, c = int.from_bytes(I[:32], "big"), I[32:]
    for i in path:
        data = (b"\x00" + k.to_bytes(32, "big")) if i >= H else _pubkey(k)
        I = hmac.new(c, data + i.to_bytes(4, "big"), hashlib.sha512).digest()
        k, c = (int.from_bytes(I[:32], "big") + k) % N, I[32:]
    return k.to_bytes(32, "big")


def test_the_reference_is_plain_bip32_by_bip84_s_own_vector():
    key = _bip32(SEED, 84 + H, 0 + H, 0 + H, 0, 0)
    kh = script.hash160(_pubkey(int.from_bytes(key, "big")))
    assert script.segwit_encode("bc", 0, kh) == "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"


def _python_wallet(index: int) -> dict:
    out = {}
    for kind, purpose in KINDS.items():
        key = _bip32(SEED, purpose + H, 1 + H, 0 + H, 0, index)
        pub = _pubkey(int.from_bytes(key, "big"))
        kh = script.hash160(pub)
        address = {"legacy": script.b58check_encode(P.pubkeyhash_version, kh),
                   "p2shSegwit": script.p2sh_p2wpkh_address(P, kh),
                   "nativeSegwit": script.p2wpkh_address(P, kh)}[kind]
        out[kind] = {"path": f"m/{purpose}'/1'/0'/0/{index}", "address": address,
                     "wif": script.b58check_encode(0xef, key + b"\x01")}
    return out


ELECTRUM = """
const done = arguments[arguments.length - 1];
(async () => {
  const c = window.coins;
  const out = [];
  for (const i of arguments[1]) out.push(await c.electrumWallet(c.unhex(arguments[0]), "ltctest", arguments[2], i));
  done(out);
})().catch((e) => done(String(e)));
"""


@pytest.fixture(scope="module")
def browser_wallets(loaded):
    driver, _, _ = loaded
    got = driver.execute_async_script(ELECTRUM, SEED.hex(), [0, 1, 7], V)
    assert isinstance(got, list), got
    return dict(zip([0, 1, 7], got))


@pytest.mark.parametrize("index", [0, 1, 7])
def test_browser_electrum_wallet_matches_python(browser_wallets, index):
    want = _python_wallet(index)
    have = browser_wallets[index]
    for kind in KINDS:
        assert have[kind]["path"] == want[kind]["path"], kind
        assert have[kind]["address"] == want[kind]["address"], kind
        assert have[kind]["wif"] == want[kind]["wif"], kind
    assert have["nativeSegwit"]["address"].startswith("tltc1q")
    assert have["p2shSegwit"]["address"][0] == "Q"


def _testnet_cli(*args):
    cli = shutil.which("litecoin-cli") or str(Path.home() / ".local/bin/litecoin-cli")
    done = subprocess.run([cli, f"-datadir={Path.home() / '.litecoin-testnet'}", "-testnet", *args],
                          capture_output=True, text=True, timeout=30)
    if done.returncode != 0:
        pytest.skip(f"testnet litecoind not answering: {done.stderr.strip()[:200]}")
    return json.loads(done.stdout)


@pytest.mark.parametrize("index", [0, 7])
def test_litecoind_derives_the_browser_s_address_from_the_browser_s_wif(browser_wallets, index):
    have = browser_wallets[index]
    shape = {"legacy": "pkh({})", "p2shSegwit": "sh(wpkh({}))", "nativeSegwit": "wpkh({})"}
    for kind, desc in shape.items():
        info = _testnet_cli("getdescriptorinfo", desc.format(have[kind]["wif"]))
        with_sum = desc.format(have[kind]["wif"]) + "#" + info["checksum"]
        derived = _testnet_cli("deriveaddresses", with_sum)
        assert derived == [have[kind]["address"]], (kind, derived, have[kind]["address"])
        assert _testnet_cli("validateaddress", have[kind]["address"])["isvalid"] is True


def _raw_unsigned(inputs, outputs, version=1, locktime=0) -> str:
    def le(n, k): return n.to_bytes(k, "little")
    def vi(n): return bytes([n]) if n < 0xfd else b"\xfd" + le(n, 2)
    raw = le(version, 4) + vi(len(inputs))
    for i in inputs:
        raw += bytes.fromhex(i["txid"])[::-1] + le(i["vout"], 4) + b"\x00" + le(i.get("sequence", 0xffffffff), 4)
    raw += vi(len(outputs))
    for value, spk in outputs:
        raw += le(value, 8) + vi(len(spk)) + spk
    return (raw + le(locktime, 4)).hex()


SIGHASH = """
const done = arguments[arguments.length - 1];
(async () => {
  const c = window.coins;
  const tx = c.parseTx(arguments[0]);
  const out = [];
  for (const [i, amount, type] of arguments[2]) out.push(c.hex(await c.sighashWitness(tx, i, c.unhex(arguments[1]), amount, type)));
  done(out);
})().catch((e) => done(String(e)));
"""


def test_browser_sighash_witness_matches_python(loaded):
    driver, _, _ = loaded
    kh = bytes.fromhex("751e76e8199196d454941c45d1b3a323f1433bd6")
    code = funding.witness_scriptcode(kh)
    inputs = [{"txid": "11" * 32, "vout": 0, "value": 500_000},
              {"txid": "22" * 32, "vout": 3, "value": 1_234_567, "sequence": 0xfffffffe}]
    outputs = [(100_000, script.p2wpkh_script(b"\x22" * 20)), (1_600_000, script.p2wpkh_script(kh))]
    raw = _raw_unsigned(inputs, outputs, locktime=17)
    cases = [(0, 500_000, 1), (1, 1_234_567, 1), (0, 500_000, 0x81), (1, 1_234_567, 0x81)]
    got = driver.execute_async_script(SIGHASH, raw, code.hex(), cases)
    assert isinstance(got, list), got
    for (i, amount, typ), have in zip(cases, got):
        want = funding.sighash_witness(inputs, outputs, i, code, amount, locktime=17,
                                       sighash_type=typ).hex()
        assert have == want, (i, typ)


SIGN = """
const done = arguments[arguments.length - 1];
(async () => {
  const c = window.coins;
  done(c.hex(await c.signInput(c.unhex(arguments[0]), c.unhex(arguments[1]), 1)));
})().catch((e) => done(String(e)));
"""


@pytest.mark.skipif(shutil.which("litecoind") is None and not (Path.home() / ".local/bin/litecoind").exists(),
                    reason="no litecoind")
@pytest.mark.parametrize("signer", ["python", "browser"])
def test_regtest_litecoind_accepts_a_bip143_p2wpkh_spend(loaded, signer):
    from arcade.regtest import LitecoinRegtestNode
    driver, _, _ = loaded
    key = _bip32(SEED, 84 + H, 1 + H, 0 + H, 0, 3)
    secret = int.from_bytes(key, "big")
    pub = _pubkey(secret)
    kh = script.hash160(pub)
    with LitecoinRegtestNode() as node:
        node.generate(150)
        params = node.params
        address = script.p2wpkh_address(params, kh)            # rltc1q...
        txid = node.rpc.call("sendtoaddress", address, 2)
        node.generate(1)
        tx = node.rpc.call("getrawtransaction", txid, 1)
        vout = next(o["n"] for o in tx["vout"] if o["scriptPubKey"]["hex"] == script.p2wpkh_script(kh).hex())
        value = 2 * 100_000_000
        dest = script.p2wpkh_script(b"\x33" * 20)
        inputs = [{"txid": txid, "vout": vout, "value": value, "address": address, "segwit": "p2wpkh"}]
        outputs = [(value - 20_000, dest)]
        digest = funding.sighash_witness(inputs, outputs, 0, funding.witness_scriptcode(kh), value)
        if signer == "python":
            sig = _sign(secret, digest)                         # DER + SIGHASH_ALL
        else:
            sig = bytes.fromhex(driver.execute_async_script(SIGN, key.hex(), digest.hex()))
        unsigned = funding.Unsigned(raw="", inputs=inputs, outputs=outputs,
                                    sighashes=[digest.hex()], signed_from=0)
        raw = funding.assemble(unsigned, [sig.hex()], pub)
        verdict = node.rpc.call("testmempoolaccept", [raw])[0]
        assert verdict["allowed"] is True, verdict
        sent = node.rpc.call("sendrawtransaction", raw)
        node.generate(1)
        assert node.rpc.call("getrawtransaction", sent, 1)["confirmations"] >= 1


# --- team/segwit-send-wiring: a node-built send, checked and signed by verifyOffer ------------

VERIFY_AND_SIGN = """
const done = arguments[arguments.length - 1];
(async () => {
  const c = window.coins;
  const priv = c.unhex(arguments[1]);
  const pubkey = c.publicKey(priv);
  const out = await c.verifyOffer(JSON.parse(arguments[0]), {pubkey, address: arguments[2], version: arguments[3]});
  const sigs = [];
  for (const h of out.hashes) sigs.push(c.hex(await c.signInput(priv, c.unhex(h), 1)));
  done({hashes: out.hashes, sigs, fee: Number(out.fee), change: Number(out.change),
        pays: out.pays.map((p) => [p.to || null, Number(p.value), !!p.mine])});
})().catch((e) => done({error: String(e && e.message || e)}));
"""


@pytest.mark.skipif(shutil.which("litecoind") is None and not (Path.home() / ".local/bin/litecoind").exists(),
                    reason="no litecoind")
def test_verify_offer_signs_a_node_built_p2wpkh_send_litecoind_accepts(loaded, tmp_path):
    from arcade import utxos
    from arcade.db import Database
    from arcade.regtest import LitecoinRegtestNode
    from arcade.state import install_schema
    driver, _, _ = loaded
    key = _bip32(SEED, 84 + H, 1 + H, 0 + H, 0, 5)
    pub = _pubkey(int.from_bytes(key, "big"))
    kh = script.hash160(pub)
    with LitecoinRegtestNode() as node:
        node.generate(150)
        params = node.params
        own = script.p2wpkh_address(params, kh)                       # rltc1q...
        coins = []
        for amount in (1.5, 0.7):                                       # two inputs, so n > 0 is covered
            txid = node.rpc.call("sendtoaddress", own, amount)
            coins.append(txid)
        node.generate(1)
        db = Database(tmp_path / "index.sqlite")
        install_schema(db)
        utxos.watch(db, own, 0)
        for txid in coins:
            tx = node.rpc.call("getrawtransaction", txid, 1)
            o = next(o for o in tx["vout"] if o["scriptPubKey"]["hex"] == script.p2wpkh_script(kh).hex())
            db.conn.execute("INSERT INTO utxo (txid, vout, address, value, height) VALUES (?,?,?,?,?)",
                            (txid, o["n"], own, round(o["value"] * 100_000_000), 1))
        dest_kh = bytes(range(1, 21))
        dest = script.p2wpkh_script(dest_kh)
        unsigned = funding.build(db, params, own, [(200_000_000, dest)], rate=fees.MIN_FEE_PER_KB, what="a segwit test send")
        assert all(i.get("segwit") == "p2wpkh" for i in unsigned.inputs)
        assert len(unsigned.inputs) == 2

        shown = driver.execute_async_script(VERIFY_AND_SIGN, json.dumps(unsigned.as_json()), key.hex(), own, params.pubkeyhash_version)
        assert "error" not in shown, shown
        assert shown["hashes"] == unsigned.sighashes, "the browser's BIP143 digests are not the node's"
        dest_addr = script.p2wpkh_address(params, dest_kh)
        assert [dest_addr, 200_000_000, False] in shown["pays"], shown["pays"]
        assert any(p[2] and p[0] == own for p in shown["pays"]), "the segwit change is seen as ours"

        raw = funding.assemble(unsigned, shown["sigs"], pub)
        verdict = node.rpc.call("testmempoolaccept", [raw])[0]
        assert verdict["allowed"] is True, verdict
        sent = node.rpc.call("sendrawtransaction", raw)
        node.generate(1)
        assert node.rpc.call("getrawtransaction", sent, 1)["confirmations"] >= 1


def test_verify_offer_refuses_a_segwit_input_whose_value_was_changed(loaded, tmp_path):
    """BIP143 commits to the amount: an offer that lies about a coin's value must not be signed."""
    from arcade import utxos
    from arcade.db import Database
    from arcade.state import install_schema
    driver, _, _ = loaded
    key = _bip32(SEED, 84 + H, 1 + H, 0 + H, 0, 6)
    pub = _pubkey(int.from_bytes(key, "big"))
    kh = script.hash160(pub)
    own = script.p2wpkh_address(P, kh)
    db = Database(tmp_path / "index.sqlite")
    install_schema(db)
    utxos.watch(db, own, 0)
    db.conn.execute("INSERT INTO utxo (txid, vout, address, value, height) VALUES (?,?,?,?,?)",
                    ("44" * 32, 0, own, 50_000_000, 1))
    unsigned = funding.build(db, P, own, [(10_000_000, script.p2wpkh_script(b"\x55" * 20))], rate=fees.MIN_FEE_PER_KB)
    offer = unsigned.as_json()
    offer["inputs"][0]["value"] = 5_000_000_000                       # claims a bigger coin
    shown = driver.execute_async_script(VERIFY_AND_SIGN, json.dumps(offer), key.hex(), own, P.pubkeyhash_version)
    assert "error" in shown, "an offer over a different amount was signed"
    assert "different bytes" in shown["error"], shown["error"]     # refused for the amount, not for anything else

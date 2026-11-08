"""Taproot wallets on Litecoin and DigiByte (2026-11-07, SadFrog: "all litecoin addresses send and receive like we just
did with digibyte", Taproot on both).

The Wallet's Taproot address type is BIP-86: the key at m/86'/<coin>'/0'/0/0, its key-path output (ltc1p.../dgb1p...).
A send pays one (OP_1 <key>); a send from one is built over BIP-341's digest (funding.build, SIGHASH_DEFAULT) and signed
in the browser with Schnorr by the tweaked key (coins.js verifyOffer works the digest out itself). Held here to
BIP-341's wallet vectors (tests/bip341-wallet-vectors.json, bitcoin/bips bip-0341/wallet-test-vectors.json) and
BIP-86's, in Python and in the browser's code alike; a real litecoind takes the spend in test_taproot_regtest.py."""
import contextlib
import json
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_me_page import _seat                                   # noqa: E402
from test_litecoin_network import _with_ltc                      # noqa: E402
from test_litecoin_testnet import _with_ltc_test                  # noqa: E402
from test_extension import H, _bip32, _pubkey                    # noqa: E402
from test_taproot import schnorr                                 # noqa: E402

from arcade import blkscan, funding, rawchain, script, seed, taproot, txbuild, utxos, wallet_imports  # noqa: E402
from arcade.config import NETWORKS                               # noqa: E402
from arcade.db import Database                                   # noqa: E402
from arcade.script import b58check_encode, segwit_decode, segwit_encode  # noqa: E402
from arcade.state import install_schema                          # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
VECTORS = json.loads((ROOT / "tests" / "bip341-wallet-vectors.json").read_text())
#: The public BIP39 test phrase: nobody's account. BIP-86's own vectors are its m/86'/0'/0'.
PHRASE = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
#: BIP-86's test vectors (bitcoin/bips bip-0086.mediawiki): path, internal key, output key, address.
BIP86 = [
    ((86 | H, 0 | H, H, 0, 0), "cc8a4bc64d897bddc5fbc2f670f7a8ba0b386779106cf1223c6fc5d7cd6fc115",
     "a60869f0dbcf1dc659c9cecbaf8050135ea9e8cdc487053f1dc6880949dc684c",
     "bc1p5cyxnuxmeuwuvkwfem96lqzszd02n6xdcjrs20cac6yqjjwudpxqkedrcr"),
    ((86 | H, 0 | H, H, 0, 1), "83dfe85a3151d2517290da461fe2815591ef69f2b18a2ce63f01697a8b313145",
     "a82f29944d65b86ae6b5e5cc75e294ead6c59391a1edc5e016e3498c67fc7bbb",
     "bc1p4qhjn9zdvkux4e44uhx8tc55attvtyu358kutcqkudyccelu0was9fqzwh"),
    ((86 | H, 0 | H, H, 1, 0), "399f1b2f4393f29a18c937859c5dd8a77350103157eb880f02e8c08214277cef",
     "882d74e5d0572d5a816cef0041a96b6c1de832f6f9676d9605c44d5e9a97d3dc",
     "bc1p3qkhfews2uk44qtvauqyr2ttdsw7svhkl9nkm9s9c3x4ax5h60wqwruhk7"),
]
NETS = ("ltc", "ltctest", "dgb", "dgbtest")
#: What every coin send asks for: the chain's own minimum (fees.rate_for).
RATE = funding.fees.MIN_FEE_PER_KB


def _secret(n: int) -> bytes:
    return n.to_bytes(32, "big")


def _tweaked(secret: int, root: bytes = b"") -> int:
    """BIP-341 taproot_tweak_seckey."""
    point = taproot.mul(secret)
    d = secret if point[1] % 2 == 0 else taproot.N - secret
    return (d + int.from_bytes(taproot.tagged("TapTweak", point[0].to_bytes(32, "big") + root), "big")) % taproot.N


# --- BIP-341 and BIP-86, in Python -----------------------------------------------------------------------------------

def test_bip341s_tweak_and_address_vectors():
    for v in VECTORS["scriptPubKey"]:
        internal = bytes.fromhex(v["given"]["internalPubkey"])
        key, _ = taproot.tweak(internal, bytes.fromhex(v["intermediary"]["merkleRoot"] or ""))
        assert key.hex() == v["intermediary"]["tweakedPubkey"]
        assert taproot.p2tr(key).hex() == v["expected"]["scriptPubKey"]
        assert segwit_encode("bc", 1, key) == v["expected"]["bip350Address"]
        assert segwit_decode("bc", v["expected"]["bip350Address"]) == (1, key)


def _key_path_vector():
    """BIP-341's key-path transaction, its spent coins, and its SIGHASH_DEFAULT input (the kind a wallet here signs)."""
    spend = VECTORS["keyPathSpending"][0]
    tx = rawchain.read_tx(bytes.fromhex(spend["given"]["rawUnsignedTx"]))
    rows = [{**i, "value": u["amountSats"], "spent": bytes.fromhex(u["scriptPubKey"])}
            for i, u in zip(tx.vin, spend["given"]["utxosSpent"])]
    one = next(s for s in spend["inputSpending"] if s["given"]["hashType"] == 0)
    return spend, tx, rows, one


def test_bip341s_key_path_digest_tweaked_key_and_signature():
    spend, tx, rows, one = _key_path_vector()
    n = one["given"]["txinIndex"]
    digest = taproot.sighash(rows, list(tx.vout), n, version=tx.version, locktime=tx.locktime)
    assert digest.hex() == one["intermediary"]["sigHash"]
    secret = int(one["given"]["internalPrivkey"], 16)
    root = bytes.fromhex(one["given"]["merkleRoot"] or "")
    assert _tweaked(secret, root) == int(one["intermediary"]["tweakedPrivkey"], 16)
    sig = schnorr(_tweaked(secret, root), digest)                # BIP-341's vectors sign with aux_rand = 0
    assert sig.hex() == one["expected"]["witness"][0]
    assert taproot.verify(rows[n]["spent"][2:], digest, sig)
    assert len(rows) == len(spend["given"]["utxosSpent"]) == 9


def test_bip86s_vectors_from_the_words():
    s = seed.to_seed(PHRASE)
    for path, internal, output, address in BIP86:
        pub = _pubkey(_bip32(s, path))
        assert taproot.xonly(pub).hex() == internal
        assert taproot.tweak(taproot.xonly(pub))[0].hex() == output
        assert taproot.key_path_script(pub) == bytes.fromhex("5120" + output)
        assert segwit_encode("bc", 1, bytes.fromhex(output)) == address


def test_the_arcades_taproot_address_on_each_chain():
    pub = _pubkey(_bip32(seed.to_seed(PHRASE), (86 | H, 3 | H, H, 0, 0)))
    prefix = {"ltc": "ltc1p", "ltctest": "tltc1p", "dgb": "dgb1p", "dgbtest": "dgbt1p"}
    for net in NETS:
        made = taproot.address(NETWORKS[net], pub)
        assert made.startswith(prefix[net]) and segwit_decode(NETWORKS[net].bech32_hrp, made)[0] == 1
    for net in ("bel", "main", "doge"):
        with pytest.raises(ValueError):
            taproot.address(NETWORKS[net], pub)


# --- the browser's code agrees ---------------------------------------------------------------------------------------

def _ltc_p2tr_offer(secret: int, net: str = "ltc", coins=((5 * 10 ** 7, "aa"), (3 * 10 ** 7, "bb"))):
    """An offer funding.build makes from a Taproot address: (unsigned, address, pubkey)."""
    import tempfile
    params = NETWORKS[net]
    pub = _pubkey(_secret(secret))
    address = taproot.address(params, pub)
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(pathlib.Path(tmp) / "index.sqlite")
        install_schema(db)
        for value, txid in coins:
            db.conn.execute("INSERT INTO utxo (txid, vout, address, value, height) VALUES (?,?,?,?,?)",
                            (txid * 32, 1, address, value, 5))
        pay = b58check_encode(params.pubkeyhash_version, bytes(range(20)))
        unsigned = funding.build(db, params, address, [(6 * 10 ** 7, txbuild.p2pkh_script(pay))], rate=RATE)
        db.close()
    return unsigned, address, pub


def _ltc_pays_three():
    """(an offer from a legacy L... address paying a "3..." one, that address, the signer's keys)."""
    import tempfile
    params = NETWORKS["ltc"]
    pub = _pubkey(_secret(0x3333))
    own = b58check_encode(48, script.hash160(pub))
    three = b58check_encode(5, bytes(range(40, 60)))
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(pathlib.Path(tmp) / "index.sqlite")
        install_schema(db)
        db.conn.execute("INSERT INTO utxo (txid, vout, address, value, height) VALUES (?,?,?,?,?)",
                        ("cc" * 32, 0, own, 5 * 10 ** 8, 5))
        unsigned = funding.build(db, params, own, [(10 ** 8, txbuild.p2pkh_script(three))], rate=RATE)
        db.close()
    keys = {"key": _secret(0x3333).hex(), "pubkey": pub.hex(), "address": own, "version": 48, "network": "ltc"}
    return {**unsigned.as_json(), "chain": "ltc"}, three, keys


def _js(asked: dict) -> dict:
    out = subprocess.run(["node", "--experimental-default-type=module", "--import", "./extension/test/register.mjs",
                          str(ROOT / "tests" / "js" / "taproot.mjs"), json.dumps(asked)],
                         capture_output=True, text=True, timeout=180, check=True, cwd=ROOT)
    return json.loads(out.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_the_browsers_taproot_is_the_vectors_and_the_arcades():
    spend, tx, rows, one = _key_path_vector()
    secret = _tweaked(int(one["given"]["internalPrivkey"], 16), bytes.fromhex(one["given"]["merkleRoot"] or ""))
    unsigned, address, pub = _ltc_p2tr_offer(0x5EED)
    offer = {**unsigned.as_json(), "chain": "ltc"}
    keys = {"key": _secret(0x5EED).hex(), "pubkey": pub.hex(), "address": address,
            "version": NETWORKS["ltc"].pubkeyhash_version, "network": "ltc"}
    other = _pubkey(_secret(0xB0B))
    # A legacy L... wallet paying Litecoin's old "3..." (2026-11-07): the review names it as the send does.
    three_offer, three, legacy_keys = _ltc_pays_three()
    # The node lies about a coin's value: the browser's digest is then not the node's, and nothing is signed.
    lied = json.loads(json.dumps(offer))
    lied["inputs"][0]["value"] += 1
    # Or names another key's Taproot coin as this one's.
    theirs = json.loads(json.dumps(offer))
    theirs["inputs"][0]["spent"] = taproot.key_path_script(other).hex()
    got = _js({
        "tweaks": [{"internal": v["given"]["internalPubkey"], "root": v["intermediary"]["merkleRoot"] or ""}
                   for v in VECTORS["scriptPubKey"]],
        "spend": {"raw": spend["given"]["rawUnsignedTx"], "index": one["given"]["txinIndex"],
                  "spent": [{"script": u["scriptPubKey"], "value": u["amountSats"]}
                            for u in spend["given"]["utxosSpent"]],
                  "secret": f"{secret:064x}", "aux": "00" * 32},
        "bip86": {"seed": seed.to_seed(PHRASE).hex(), "paths": [list(p) for p, _, _, _ in BIP86]},
        "offers": [{"offer": offer, "keys": keys},
                   {"offer": offer, "keys": {**keys, "key": _secret(0xB0B).hex(), "pubkey": other.hex()}},
                   {"offer": lied, "keys": keys}, {"offer": theirs, "keys": keys},
                   {"offer": {**three_offer, "to": three}, "keys": legacy_keys},
                   {"offer": three_offer, "keys": legacy_keys}],
    })
    assert got["tweaks"] == [v["intermediary"]["tweakedPubkey"] for v in VECTORS["scriptPubKey"]]
    assert got["spend"] == {"digest": one["intermediary"]["sigHash"], "sig": one["expected"]["witness"][0]}
    assert [(b["pubkey"][2:], b["address"]) for b in got["bip86"]] == [(i, a) for _, i, _, a in BIP86]
    s = seed.to_seed(PHRASE)
    for net in NETS:
        coin = 3 if NETWORKS[net].real_coins else 1
        assert got["kinds"][net] == _pubkey(_bip32(s, (86 | H, coin | H, H, 0, 0))).hex(), net

    mine, stranger, lie, not_mine, said_three, said_m = got["offers"]
    hash_ = bytes(range(40, 60))
    assert said_three["pays"] == [[three, 10 ** 8]]
    assert said_m["pays"] == [[b58check_encode(50, hash_), 10 ** 8]], "the same script, said as M... otherwise"
    assert mine["hashes"] == unsigned.sighashes, "the browser's BIP-341 digests are the arcade's"
    assert mine["signers"] == ["key"] * len(unsigned.inputs)
    output_key = taproot.tweak(taproot.xonly(pub))[0]
    for h, sig in zip(unsigned.sighashes, mine["sigs"]):
        assert taproot.verify(output_key, bytes.fromhex(h), bytes.fromhex(sig))
    assert mine["change"] == unsigned.change > 0, "the change back to the Taproot address is said as change"
    assert mine["pays"] == [[b58check_encode(48, bytes(range(20))), 6 * 10 ** 7]]
    assert "back to you as change" in mine["says"]
    assert "Nothing was signed" in stranger["error"]
    assert "different bytes" in lie["error"]
    assert "does not hold" in not_mine["error"]


# --- what is paid, and the address kinds ------------------------------------------------------------------------------

def _chain(net):
    from types import SimpleNamespace
    return SimpleNamespace(network=net, params=NETWORKS[net], label=NETWORKS[net].label)


def test_a_coin_send_pays_taproot_where_the_chain_has_it():
    from arcade.web.app import _check_address
    key = taproot.tweak(taproot.xonly(_pubkey(_secret(7))))[0]
    off_curve = next(bytes([n]) * 32 for n in range(1, 256) if not taproot.payable(bytes([n]) * 32))
    for net in NETS:
        hrp = NETWORKS[net].bech32_hrp
        assert _check_address(segwit_encode(hrp, 1, key), mainnet=_chain(net), segwit_ok=True) is None, net
        assert _check_address(segwit_encode(hrp, 1, key), mainnet=_chain(net)), "only the coin send builds it"
        assert "never be spent" in _check_address(segwit_encode(hrp, 1, off_curve), mainnet=_chain(net),
                                                  segwit_ok=True)
        assert "witness version 2" in _check_address(segwit_encode(hrp, 2, key), mainnet=_chain(net),
                                                     segwit_ok=True)
    assert "Taproot" in _check_address(segwit_encode("bel", 1, key), mainnet=_chain("bel"), segwit_ok=True)
    assert _check_address(segwit_encode("tltc", 1, key), mainnet=_chain("ltc"), segwit_ok=True), "another chain's"


def test_litecoin_pays_the_old_3_and_digibyte_still_does_not():
    from arcade.web.app import _check_address
    three = b58check_encode(5, bytes(range(20)))
    assert three.startswith("3")
    assert _check_address(three, mainnet=_chain("ltc"), segwit_ok=True) is None
    assert _check_address(three, mainnet=_chain("ltc")) is None
    assert _check_address(three, mainnet=_chain("dgb"), segwit_ok=True), "DigiByte Core refuses a 3 address"
    assert _check_address(three, mainnet=_chain("ltctest"), segwit_ok=True), "Litecoin testnet's old P2SH is 2..."
    assert txbuild.p2pkh_script(three) == b"\xa9\x14" + bytes(range(20)) + b"\x87"


def _kinds():
    page = (ROOT / "arcade" / "web" / "templates" / "wallet_js.js").read_text()
    block = re.search(r"export const ADDRESS_KINDS = \{(.*?)\n\};", page, re.S).group(1)
    return {name: [json.loads(re.sub(r"(\w+):", r'"\1":', row)) for row in re.findall(r"\{[^}]*\}", rows)]
            for name, rows in re.findall(r"\n  (\w+): \[(.*?)\n  \],", block, re.S)}


def test_taproot_is_the_last_kind_on_litecoin_and_digibyte_only():
    kinds = _kinds()
    for net, rows in kinds.items():
        tap = [k for k in rows if k["id"] == "p2tr"]
        if net not in NETS:
            assert not tap, net
            continue
        coin = 3 if NETWORKS[net].real_coins else 1
        assert [k["id"] for k in rows] == ["legacy", "p2sh-p2wpkh", "p2wpkh", "p2tr"], net
        assert tap == [{"id": "p2tr", "label": "Taproot", "hrp": NETWORKS[net].bech32_hrp,
                        "path": f"m/86'/{coin}'/0'/0/0"}], net
    assert set(NETS) <= set(kinds) and set(NETS) == taproot.CHAINS


def test_lite_takes_the_row_from_the_same_table():
    page = (ROOT / "arcade" / "lite" / "templates" / "_wallet.html").read_text()
    assert "wallet.addressKinds(c.network)" in page and "wallet.asKind(open, c.network, k.id)" in page
    assert "pubkey: Array.from(mine.pubkey" in page, "a Taproot address is registered with its key"
    wallet = (ROOT / "arcade" / "web" / "templates" / "wallet_js.js").read_text()
    assert 'kind.id === "p2tr"' in wallet and "coins.taprootKey" in wallet and "coins.taprootAddress" in wallet


def test_imported_taproot_wallets():
    pub = _pubkey(_secret(9))
    assert wallet_imports.address_of(NETWORKS["ltc"], pub, "p2tr") == taproot.address(NETWORKS["ltc"], pub)
    assert wallet_imports.address_of(NETWORKS["dgb"], pub, "p2tr").startswith("dgb1p")
    for net in ("bel", "main", "doge"):
        with pytest.raises(wallet_imports.ImportRefused):
            wallet_imports.address_of(NETWORKS[net], pub, "p2tr")
    assert wallet_imports.entries([{"a": "x", "k": "p2tr"}]) == [{"a": "x", "k": "p2tr", "p": "", "t": 0}]
    from test_wallet_imports import _key, _sign
    priv, pub = _key(0x1234)
    address = wallet_imports.address_of(NETWORKS["ltc"], pub, "p2tr")
    sig = _sign(priv, wallet_imports.proof_digest("ab" * 32, "ltc", address, pub.hex()))
    assert wallet_imports.check_proof(NETWORKS["ltc"], "ab" * 32, "ltc", address, pub.hex(), "p2tr",
                                      sig.hex()) == pub


# --- building a spend from a Taproot address --------------------------------------------------------------------------

def test_a_taproot_send_is_built_over_bip341_with_its_change_back_and_priced_at_its_size():
    unsigned, address, pub = _ltc_p2tr_offer(0x5EED)
    script_ = taproot.key_path_script(pub)
    assert all(c["taproot"] == {"key": True} and c["spent"] == script_.hex() for c in unsigned.inputs)
    rows = [{**c, "spent": script_} for c in unsigned.inputs]
    assert unsigned.sighashes == [taproot.sighash(rows, unsigned.outputs, n, version=1, locktime=0).hex()
                                  for n in range(len(rows))]
    assert unsigned.outputs[-1] == (unsigned.change, script_), "change back to the same Taproot address"
    shown = unsigned.as_json()["inputs"][0]
    assert shown["taproot"] == {"key": True} and shown["spent"] == script_.hex()
    # Signed, its virtual size is what it was priced at (57.5 vbytes an input, not a legacy input's 148).
    secret = _tweaked(0x5EED)
    raw = bytes.fromhex(funding.assemble(unsigned, [schnorr(secret, bytes.fromhex(h)).hex()
                                                    for h in unsigned.sighashes], pub))
    stripped = bytes.fromhex(funding.serialize_segwit(
        [{"txid": c["txid"], "vout": c["vout"]} for c in unsigned.inputs], unsigned.outputs))
    vsize = -(-(len(stripped) * 3 + len(raw)) // 4)
    params = NETWORKS["ltc"]
    rate = funding.fees.rate_for(params, RATE)
    want = funding.price(len(unsigned.inputs), unsigned.outputs[:-1], rate, change=True, params=params, kind="p2tr")
    assert unsigned.fee == want == funding.fees.fee_for(vsize, 0, rate), (unsigned.fee, vsize)
    legacy = funding.price(len(unsigned.inputs), unsigned.outputs[:-1], rate, change=True, params=params)
    assert unsigned.fee < legacy
    # the witness is one 64-byte signature per input, and the scriptSigs are empty
    tx = rawchain.read_tx(raw)
    assert all(v["script"] == b"" and len(v["witness"]) == 1 and len(v["witness"][0]) == 64 for v in tx.vin)
    with pytest.raises(funding.FundingError):
        funding.assemble(unsigned, ["00" * 71] * len(unsigned.inputs), pub)


def test_spend_for_reads_a_taproot_address_and_refuses_other_witness_versions():
    pub = _pubkey(_secret(5))
    for net in NETS:
        rule = funding.spend_for(taproot.address(NETWORKS[net], pub), NETWORKS[net])
        assert rule["kind"] == "p2tr" and rule["change_script"] == taproot.key_path_script(pub)
    with pytest.raises(funding.FundingError):
        funding.spend_for(segwit_encode("ltc", 2, bytes(32)), NETWORKS["ltc"])


def test_the_index_files_a_watched_taproot_addresss_coins():
    assert script.OutputType.WITNESS_V1_TAPROOT in utxos.SPENDABLE
    pub = _pubkey(_secret(11))
    address = taproot.address(NETWORKS["dgb"], pub)
    spk = taproot.key_path_script(pub)
    parsed = script.parse_output(spk.hex(), 0, NETWORKS["dgb"])
    assert parsed.type in utxos.SPENDABLE and parsed.address == address
    want = blkscan.targets([address], NETWORKS["dgb"])
    assert want == {("tr", spk[2:]): address} and blkscan._kind(spk) == ("tr", spk[2:])


# --- inscriptions stay put on a Taproot address -----------------------------------------------------------------------

class _Guard:
    """An inscription guard: keeps out the coins it holds inscribed, by outpoint, as doginals.Doginals.spendable."""

    def __init__(self, inscribed):
        self.inscribed, self.asked = set(inscribed), []

    def spendable(self, coins):
        self.asked.append([(c["txid"], c["vout"]) for c in coins])
        return [c for c in coins if (c["txid"], c["vout"]) not in self.inscribed]


def test_a_taproot_send_never_spends_an_inscribed_coin(tmp_path, monkeypatch):
    params = NETWORKS["dgb"]
    pub = _pubkey(_secret(0xD6B))
    address = taproot.address(params, pub)
    guard = _Guard({("aa" * 32, 0)})
    monkeypatch.setattr(funding, "GUARD_BY_NET", {"dgb": guard})
    assert funding.guard_for(address, params.name) is guard, "a dgb1p address gets DigiByte's guard"
    assert funding.guard_for(address) is None, "by address alone, as before: no version byte to watch"
    assert funding.guard_for(segwit_encode("dgb", 0, bytes(20)), params.name) is guard, "dgb1q too"
    db = Database(tmp_path / "index.sqlite")
    install_schema(db)
    for txid, value in (("aa" * 32, 10 ** 9), ("bb" * 32, 3 * 10 ** 8)):
        db.conn.execute("INSERT INTO utxo (txid, vout, address, value, height) VALUES (?,?,?,?,?)",
                        (txid, 0, address, value, 5))
    pay = b58check_encode(30, bytes(range(20)))
    unsigned = funding.build(db, params, address, [(10 ** 8, txbuild.p2pkh_script(pay))], rate=RATE)
    assert [c["txid"] for c in unsigned.inputs] == ["bb" * 32], "the inscribed (and larger) coin stays put"
    assert guard.asked and ("aa" * 32, 0) in guard.asked[0]
    with pytest.raises(funding.FundingError):
        funding.build(db, params, address, [(5 * 10 ** 8, txbuild.p2pkh_script(pay))], rate=RATE)
    db.close()


# --- the routes, on real Litecoin and DigiByte ------------------------------------------------------------------------

def _ready_ltc(client, monkeypatch, net="ltc"):
    app, state = client
    chain = _with_ltc(state, monkeypatch, ready=True) if net == "ltc" else _with_ltc_test(state, monkeypatch, True)
    _seat(app)
    params = NETWORKS[net]
    legacy = b58check_encode(params.pubkeyhash_version, script.hash160(_pubkey(_secret(0x36))))
    r = app.post("/account/address", json={"address": legacy, "chain": net})
    assert r.status_code == 200, r.text
    return app, state, chain, legacy


def _fund(state, chain, address, txid="cd", value=3 * 10 ** 8):
    with contextlib.closing(state.token_index(chain).open()) as db:
        db.conn.execute("INSERT OR REPLACE INTO utxo(txid,vout,address,value,height) VALUES (?,?,?,?,?)",
                        (txid * 32, 1, address, value, 1))
        db.conn.commit()


def test_register_balance_and_send_from_a_taproot_address_on_real_litecoin(client, monkeypatch):
    app, state, chain, legacy = _ready_ltc(client, monkeypatch)
    pub = _pubkey(_secret(0x86))
    mine = taproot.address(NETWORKS["ltc"], pub)
    assert mine.startswith("ltc1p")
    # registered with the key it was made from, never by the address alone
    r = app.post("/account/segwit/register", json={"chain": "ltc", "address": mine})
    assert r.status_code == 400 and "public key" in r.json()["detail"], r.text
    r = app.post("/account/segwit/register", json={"chain": "ltc", "address": mine,
                                                   "pubkey": _pubkey(_secret(0x87)).hex()})
    assert r.status_code == 400, r.text
    r = app.post("/account/segwit/register", json={"chain": "ltc", "address": mine, "pubkey": pub.hex()})
    assert r.status_code == 200 and r.json()["kind"] == "p2tr", r.text
    assert app.post("/account/segwit/register", json={"chain": "ltc", "address": mine}).status_code == 200, \
        "the same address again needs no key"
    _fund(state, chain, mine)
    bal = app.get("/account/segwit/balance", params={"address": mine, "network": "ltc"}).json()
    assert bal["watched"] is True and bal["balance"] == 3 * 10 ** 8

    r = app.post("/account/send", json={"to": legacy, "amount": "1", "chain": "ltc", "from": mine})
    assert r.status_code == 200, r.text
    offer = r.json()
    assert all(i["taproot"] == {"key": True} and i["address"] == mine for i in offer["inputs"]), offer["inputs"]
    tx = rawchain.read_tx(bytes.fromhex(offer["raw"]))
    outs = list(tx.vout)
    spk = taproot.key_path_script(pub)
    rows = [{**i, "value": c["value"], "spent": spk} for i, c in zip(tx.vin, offer["inputs"])]
    assert offer["sighashes"] == [taproot.sighash(rows, outs, n, version=tx.version, locktime=tx.locktime).hex()
                                  for n in range(len(rows))]
    assert outs[0] == (10 ** 8, txbuild.p2pkh_script(legacy)) and outs[-1][1] == spk, "change back to ltc1p"

    # Another account's (or nobody's) Taproot address is not this one's to spend from.
    other = taproot.address(NETWORKS["ltc"], _pubkey(_secret(0x99)))
    r = app.post("/account/send", json={"to": legacy, "amount": "1", "chain": "ltc", "from": other})
    assert r.status_code == 400 and "register" in r.json()["detail"], r.text


def test_every_kind_on_real_litecoin_pays_taproot_and_3(client, monkeypatch):
    app, state, chain, legacy = _ready_ltc(client, monkeypatch)
    _fund(state, chain, legacy)
    key = taproot.tweak(taproot.xonly(_pubkey(_secret(0x77))))[0]
    three = b58check_encode(5, bytes(range(40, 60)))
    for to, want in ((segwit_encode("ltc", 1, key), b"\x51\x20" + key),
                     (three, b"\xa9\x14" + bytes(range(40, 60)) + b"\x87")):
        r = app.post("/account/send", json={"to": to, "amount": "1", "chain": "ltc"})
        assert r.status_code == 200, (to, r.text)
        assert r.json()["to"] == to
        assert list(rawchain.read_tx(bytes.fromhex(r.json()["raw"])).vout)[0] == (10 ** 8, want), to
    r = app.post("/account/send", json={"to": segwit_encode("ltc", 2, key), "amount": "1", "chain": "ltc"})
    assert r.status_code == 400 and "witness version 2" in r.json()["detail"], r.text


def test_litecoin_testnet_sends_from_and_to_taproot_too(client, monkeypatch):
    app, state, chain, legacy = _ready_ltc(client, monkeypatch, "ltctest")
    pub = _pubkey(_secret(0x4C))
    mine = taproot.address(NETWORKS["ltctest"], pub)
    assert mine.startswith("tltc1p")
    assert app.post("/account/segwit/register",
                    json={"chain": "ltctest", "address": mine, "pubkey": pub.hex()}).status_code == 200
    _fund(state, chain, mine)
    r = app.post("/account/send", json={"to": mine, "amount": "1", "chain": "ltctest", "from": mine})
    assert r.status_code == 200, r.text
    outs = list(rawchain.read_tx(bytes.fromhex(r.json()["raw"])).vout)
    assert [s for _, s in outs] == [taproot.key_path_script(pub)] * 2, "paid to itself, and its change"


def test_a_taproot_send_on_digibyte_keeps_inscribed_coins_out(client, monkeypatch):
    """The route's coin choice puts a dgb1p address through DigiByte's guard (keyed by outpoint), like a D... one."""
    from arcade.web.state import ChainContext
    app, state = client
    chain = ChainContext(network="dgb", role="ledger", label="DigiByte", conf=pathlib.Path("/nonexistent/d.conf"))
    monkeypatch.setattr(state, "more", {"dgb": chain}, raising=False)
    monkeypatch.setattr(type(state), "chain_ready", lambda self, c: True)
    _seat(app)
    legacy = b58check_encode(30, script.hash160(_pubkey(_secret(0x36))))
    assert app.post("/account/address", json={"address": legacy, "chain": "dgb"}).status_code == 200
    pub = _pubkey(_secret(0xD6))
    mine = taproot.address(NETWORKS["dgb"], pub)
    assert app.post("/account/segwit/register",
                    json={"chain": "dgb", "address": mine, "pubkey": pub.hex()}).status_code == 200
    _fund(state, chain, mine, "aa", 10 ** 9)
    _fund(state, chain, mine, "bb", 3 * 10 ** 8)
    guard = _Guard({("aa" * 32, 1)})
    monkeypatch.setattr(funding, "GUARD_BY_NET", {"dgb": guard})
    r = app.post("/account/send", json={"to": legacy, "amount": "1", "chain": "dgb", "from": mine})
    assert r.status_code == 200, r.text
    assert [i["txid"] for i in r.json()["inputs"]] == ["bb" * 32]
    r = app.post("/account/send", json={"to": legacy, "amount": "5", "chain": "dgb", "from": mine})
    assert r.status_code == 400 and "not enough" in r.json()["detail"], r.text

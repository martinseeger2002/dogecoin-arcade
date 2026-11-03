"""The Arcade Wallet extension (extension/) makes the same keys as the arcade, byte for byte.

Its keys.js derives the account key from the same words as arcade/seed.py, signs the same login message the node
checks, and opens what it seals. Run with Node; skipped where there is none."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from nacl.signing import VerifyKey

from arcade import accounts, config, script, seed
from arcade.llm import _G, _mul      # the arcade's own secp256k1 arithmetic (signature recovery), for a public key

ROOT = Path(__file__).parent.parent
PHRASES = [
    "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
    "legal winner thank year wave sausage worth useful legal winner thank yellow",
    "zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo vote",
]
ORIGIN, NONCE = "https://arcade.example.com", "ab" * 32

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")


def node(script_name: str, asked: dict) -> dict | list:
    out = subprocess.run(["node", "--experimental-default-type=module", "--import", "./extension/test/register.mjs",
                          str(ROOT / "extension" / "test" / script_name), json.dumps(asked)],
                         capture_output=True, text=True, timeout=180, check=True, cwd=ROOT)
    return json.loads(out.stdout)


@pytest.fixture(scope="module")
def derived():
    out = subprocess.run(["node", "--experimental-default-type=module", str(ROOT / "extension" / "test" / "derive.mjs"),
                          json.dumps({"phrases": PHRASES, "origin": ORIGIN, "nonce": NONCE})],
                         capture_output=True, text=True, timeout=120, check=True)
    return json.loads(out.stdout)


def test_the_extension_derives_the_arcades_account_key(derived):
    for phrase, got in zip(PHRASES, derived):
        assert got["pubkey"] == seed.login_pubkey(phrase), phrase
        assert got["complaint"] == ""


def test_its_sign_in_is_what_the_node_checks(derived):
    for got in derived[:len(PHRASES)]:
        VerifyKey(bytes.fromhex(got["pubkey"])).verify(accounts.login_message(ORIGIN, NONCE),
                                                       bytes.fromhex(got["signature"]))


def test_it_opens_what_it_seals_and_refuses_a_bad_checksum(derived):
    for phrase, got in zip(PHRASES, derived):
        assert got["reopened"] == seed.normalise(phrase)
    assert derived[-1]["badChecksum"], "a reversed phrase must not pass its checksum"


def test_the_manifest_asks_only_for_what_it_uses():
    manifest = json.loads((ROOT / "extension" / "manifest.json").read_text())
    assert manifest["manifest_version"] == 3
    assert sorted(manifest["permissions"]) == ["alarms", "storage"]
    assert manifest["content_scripts"][0]["all_frames"] is False, "inscribed apps in frames get no wallet"
    assert "unsafe" not in manifest["content_security_policy"]["extension_pages"]
    for name in ("background.js", "content.js", "inpage.js", "keys.js", "popup.html", "approve.html",
                 "bip39-english.txt"):
        assert (ROOT / "extension" / name).is_file(), name
    assert (ROOT / "extension" / "bip39-english.txt").read_text() == (ROOT / "arcade" / "bip39-english.txt").read_text()


# --- coin keys: every chain, against the Python ------------------------------------------------------------------------

H, N = 0x80000000, 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def _pubkey(key: bytes) -> bytes:
    x, y = _mul(int.from_bytes(key, "big"), _G)
    return bytes([2 + (y & 1)]) + x.to_bytes(32, "big")


def _bip32(seed_bytes: bytes, path) -> bytes:
    import hashlib
    import hmac
    import struct
    i = hmac.new(b"Bitcoin seed", seed_bytes, hashlib.sha512).digest()
    key, chain = i[:32], i[32:]
    for idx in path:
        data = (b"\x00" + key if idx & H else _pubkey(key)) + struct.pack(">I", idx)
        i = hmac.new(chain, data, hashlib.sha512).digest()
        key, chain = ((int.from_bytes(i[:32], "big") + int.from_bytes(key, "big")) % N).to_bytes(32, "big"), i[32:]
    return key


@pytest.fixture(scope="module")
def addressed():
    return node("coins.mjs", {"phrases": PHRASES})


def test_every_chain_of_the_arcade_is_in_the_extensions_table_with_its_own_bytes(addressed):
    table = addressed["chains"]
    assert sorted(table) == sorted(config.NETWORKS), "a chain added to config.NETWORKS needs a row in chains.js"
    for name, p in config.NETWORKS.items():
        row = table[name]
        assert (row["pubkeyhash"], row["scripthash"], row["coin"], row["real"]) == \
            (p.pubkeyhash_version, p.scripthash_version, p.coin, p.real_coins), name
        assert row.get("hrp", "") == p.bech32_hrp, name


def test_every_chains_address_is_the_arcades_for_the_same_words(addressed):
    """m/44'/3'/0'/0/0 on every real-coin chain and m/44'/1'/0'/0/0 on the test chains (coins.js COIN_TYPE), in that
    chain's own version byte: worked out here in Python, from the words, with nothing taken from the extension."""
    for phrase, got in zip(PHRASES, addressed["addresses"]):
        s = seed.to_seed(phrase)
        for name, p in config.NETWORKS.items():
            pub = _pubkey(_bip32(s, (44 | H, (3 if p.real_coins else 1) | H, 0 | H, 0, 0)))
            assert got[name]["pubkey"] == pub.hex(), (phrase, name)
            assert got[name]["address"] == script.b58check_encode(p.pubkeyhash_version, script.hash160(pub)), name
    first = addressed["addresses"][0]
    assert first["doge"]["address"] == "DBus3bamQjgJULBJtYXpEzDWQRwF5iwxgC"     # the published BIP44 Dogecoin vector
    assert first["regtest"]["address"] == "mkpZhYtJu2r87Js3pDiWJDmPte2NRZ8bJV"  # and the testnet one
    assert addressed["coins"] == ["0", "1.23456789", "1", "0.00000001"]


def test_the_extensions_coin_code_is_the_arcades_own_file():
    """Not a port: coins.js and the two libraries it imports are the web wallet's files, byte for byte, so what the
    arcade's tests prove of them (tests/test_coins_browser.py, test_bip143.py, test_vendor.py) holds here."""
    assert (ROOT / "extension" / "coins.js").read_bytes() == (ROOT / "arcade" / "web" / "templates" / "coins.js").read_bytes()
    vendored = sorted(p.relative_to(ROOT / "extension" / "vendor") for p in (ROOT / "extension" / "vendor").rglob("*.js"))
    assert vendored, "extension/vendor is empty"
    for rel in vendored:
        assert (ROOT / "extension" / "vendor" / rel).read_bytes() == (ROOT / "arcade" / "web" / "vendor" / rel).read_bytes(), rel

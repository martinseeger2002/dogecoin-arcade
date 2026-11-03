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


# --- messages: each chain's Core words, and a verifier for outside sites -------------------------------------------

#: A throwaway key (sha256 of a sentence) and the signature each chain's own Core made with it, 2026-11-03:
#: `<chain>-cli signmessagewithprivkey <its WIF> "arcade wallet probe"`. The extension must make the very same bytes.
PROBE_KEY = "87bd27b8eb03a9a6dd0cf45e0e9c15e6dda8e9f4a6a808d58a1ea93b2d4b934d"
PROBE_TEXT = "arcade wallet probe"
CORE_SAID = {
    "main": "H9xWUPXuwOupHYk3ms4hMbOJ/XJBlFAT+TfAUd4R2RHafmf87lFoCNPomKYhvdCpXauO5nxHzimH4cGRNSfTsuA=",   # pepecoind
    "test": "H9xWUPXuwOupHYk3ms4hMbOJ/XJBlFAT+TfAUd4R2RHafmf87lFoCNPomKYhvdCpXauO5nxHzimH4cGRNSfTsuA=",   # its testnet
    "doge": "HzeMpEhm+lv/tX/YbHrMF2rs112H0PLUewYzRI09NqTxHeTS6HKI+eD/zX/pL2DSMKyD4rKBFsg+fzKllnfWRTw=",   # dogecoind
    "ltc": "IH9ZFd53ydLWEL8PSrDqGqCnWruh2GRrugBdIFFjxUTUVi6IjPKYTT8V5dgGj3UN7gZXc++t/AjN/w5UvQkV+3o=",    # litecoind
    "ltctest": "IH9ZFd53ydLWEL8PSrDqGqCnWruh2GRrugBdIFFjxUTUVi6IjPKYTT8V5dgGj3UN7gZXc++t/AjN/w5UvQkV+3o=",
    "lky": "IPSadvZtMmDUdzNwiozAUon9uFo9sxjYvlhtOKQFvYzWJlObW4Q/79zmo24vwKGHYa211CwkaMEbD9WU2IaAG7A=",    # luckycoind
    "bel": "HzZJDNuWttSoRJjFwM8x2eSTdvWaYh+3tObnvE0BeuxHBWVpJ4Dh2wsJz0Hsc0Fy2NYpyGT2L+2IlGWBHKB/NCE=",    # bellsd
}
ARCADE_WORDS = [f"arcade-llm\n{'ab' * 32}\nPehYeRLFsRj5jboZXTC6rFHxmYdmV9RdfR\n1900000000", "arcade-realtime\nanything",
                "DogecoinArcade login\nhttps://x.example\n" + "ab" * 32]


@pytest.fixture(scope="module")
def signed():
    return node("messages.mjs", {"key": PROBE_KEY, "text": PROBE_TEXT, "phrase": PHRASES[0], "arcadeWords": ARCADE_WORDS})


def test_a_message_is_signed_as_each_chains_own_core_signs_it(signed):
    for network, core in CORE_SAID.items():
        assert signed["probe"][network] == core, network
    for network, row in signed["tables"]["chains"].items():
        assert (network in signed["tables"]["verify"]) == bool(row["magic"]), network
        if row["magic"]:
            assert signed["tables"]["verify"][network] == [row["pubkeyhash"], row["magic"]], network
    for network in ("dgb", "kot", "wjk"):         # no message words without a Core that was checked
        assert "not signed here yet" in signed["refused"][network], network
    assert sorted(signed["mine"]) == sorted([*CORE_SAID, "regtest"])


def test_the_arcades_own_words_are_never_signed_for_a_site(signed):
    for text in ARCADE_WORDS:
        assert "an arcade signs for itself" in signed["refused"][text], text


def test_the_verifiers_say_yes_to_the_wallets_signature_and_no_to_anything_changed(signed):
    import importlib.util
    from arcade import llm
    spec = importlib.util.spec_from_file_location("arcade_verify", ROOT / "extension" / "verify.py")
    verify = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verify)
    assert verify.CHAINS == {n: tuple(v) for n, v in signed["tables"]["verify"].items()}
    for network, got in signed["mine"].items():
        assert signed["verified"][network] == {"right": True, "otherText": False, "otherChain": False}, network
        assert verify.verify_message(network, got["address"], PROBE_TEXT, got["signature"]), network
        assert not verify.verify_message(network, got["address"], PROBE_TEXT + ".", got["signature"]), network
        assert llm.recover_hash160(got["signature"], PROBE_TEXT, verify.CHAINS[network][1]) == \
            script.b58check_decode(got["address"])[1], network          # and the arcade's own recovery agrees
    s = signed["signIn"]
    assert s["right"] and not s["otherOrigin"]
    assert verify.verify_sign_in(s["origin"], s["nonce"], s["pubkey"], s["signature"])
    assert not verify.verify_sign_in("https://evil.example", s["nonce"], s["pubkey"], s["signature"])
    assert s["pubkey"] == seed.login_pubkey(PHRASES[0])

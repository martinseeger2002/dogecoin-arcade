"""The Arcade Wallet extension (extension/) makes the same keys as the arcade, byte for byte.

Its keys.js derives the account key from the same words as arcade/seed.py, signs the same login message the node
checks, and opens what it seals. Run with Node; skipped where there is none."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from nacl.signing import VerifyKey

from arcade import accounts, seed

ROOT = Path(__file__).parent.parent
PHRASES = [
    "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
    "legal winner thank year wave sausage worth useful legal winner thank yellow",
    "zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo vote",
]
ORIGIN, NONCE = "https://arcade.example.com", "ab" * 32

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")


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

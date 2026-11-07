"""Restoring without an arcade (arcade/restore_kit.py, coins.js restoreKit, /recover; 2026-11-07, SadFrog's side).

SadFrog: "we want it to be easy for someone to be able to spin back up their wallets from any core if arcade goes down
as well as any arcade." What is checked here: the BIP380 checksum against the BIP's vector, the real Cores' answers and
Bitcoin Core's own Python; BIP32 serialization against BIP32's test vector 1; every chain's address, key and descriptors
against arcade/seed.py and a BIP32 worked out here in Python; the descriptors against what DigiByte Core 9.26.7,
Litecoin Core 0.21.5.8 and Bells Core 3.0.0-rc2 derived from them on 2026-11-07; the offline page's inlined code against
the real coins.js; and that /recover can fetch nothing from anywhere. Run with Node; skipped where there is none.
"""
import base64
import hashlib
import hmac
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_extension import H, _bip32, _pubkey                   # noqa: E402
from test_web import app_state, client                         # noqa: F401,E402

from arcade import config, core_install, ddaddress, restore_kit, seed, taproot  # noqa: E402
from arcade.script import b58check_encode, b58decode, hash160, segwit_encode     # noqa: E402

ROOT = Path(__file__).parent.parent
#: The public BIP39 test phrase: nobody's account. Its m/44'/3'/0'/0/0 is the published BIP44 Dogecoin vector.
PHRASE = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
BAD = "about abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon"

#: What the real Cores answered on 2026-11-07 for PHRASE (getdescriptorinfo / deriveaddresses, index 0):
#: DigiByte Core 9.26.7, Litecoin Core 0.21.5.8 (mainnet and testnet4) and Bells Core 3.0.0-rc2, from the full private
#: descriptors this kit makes (xprv/tprv/dgpv) and from the account xpubs alike.
CORE_SAID = {
    "dgb": {"pkh": "DBus3bamQjgJULBJtYXpEzDWQRwF5iwxgC", "sh": "STr4c2jQpSeRXCiU6kHLVfknD2reoa22DP",
            "wpkh": "dgb1q78ehfacf2d0s7nylpxd5a5wl0lf26nm694rtak",
            "tr": "dgb1pf77txrpzt8vrjylmgf4aaxheuvxmwr07vgx6sehgfzm3l5sh6vnspzuv93"},
    "ltc": {"pkh": "LRzimYwxBz25C8gsL6XYyF7fjWaDnSRa4k", "wpkh": "ltc1q78ehfacf2d0s7nylpxd5a5wl0lf26nm6kjahag"},
    # The Wallet's Taproot address type (BIP-86), 2026-11-07: DigiByte Core 9.26.7 (testnet26) derived it from
    # tr(tprv.../86h/1h/0h/0/0); Litecoin Core 0.21.5.8 has no tr() ("not a valid descriptor function") but its
    # validateaddress takes the ltc1p one as witness_version 1 with this program.
    "dgbtest": {"tr86": "dgbt1p8wpt9v4frpf3tkn0srd97pksgsxc5hs52lafxwru9kgeephvs7rq7658xf"},
    "ltctest": {"pkh": "mkpZhYtJu2r87Js3pDiWJDmPte2NRZ8bJV", "sh": "QRHtkDQdVvNNwrVjEdeCGviCw7Ny3SNNiA",
                "wpkh": "tltc1q6rz28mcfaxtmd6v789l9rrlrusdprr9pesrjxk"},
    "bel": {"pkh": "BBDr846KrqMvPAUsmSsDpMraFueXWBWgih", "sh": "DBhA7StuPVMWXu7cHuHpVXmpSPMXNffVnm",
            "wpkh": "bel1q78ehfacf2d0s7nylpxd5a5wl0lf26nm6uzrmng"},
}
#: Checksums the Cores computed themselves (getdescriptorinfo), beside BIP380's own vector.
CORE_CHECKSUMS = {
    "raw(deadbeef)": "89f8spxm",
    "pkh(xpub6Bxse8AT19u9HExKtP1EAudLi9CpLxPpxDvanL2fFtM7UFE2Q7TTWRg4bnMnmT4KcyN6GQkSgZmPWDtyUywSii3MDpMNfXSTuzH7gvZywLU"
    "/0/*)": "gr8xs3s8",
    "pkh(dgub8rUhDtD3YFGZTUphBfpBbzvFxSMKQXYLzg87Me2ta78r2SdVLmypBUkkxrrn9RTnchsyiJSkHZyLWxD13ibBiXtuFWktBoDaGaZjQUBLNLs"
    "/0/*)": "yxf82sjr",
    "tr(xpub6Bxse8AT19u9HExKtP1EAudLi9CpLxPpxDvanL2fFtM7UFE2Q7TTWRg4bnMnmT4KcyN6GQkSgZmPWDtyUywSii3MDpMNfXSTuzH7gvZywLU"
    "/0/*)": "wyqxgrnk",
}
#: BIP32 test vector 1.
V1_SEED = "000102030405060708090a0b0c0d0e0f"
V1 = [([], "xprv9s21ZrQH143K3QTDL4LXw2F7HEK3wJUD2nW2nRk4stbPy6cq3jPPqjiChkVvvNKmPGJxWUtg6LnF5kejMRNNU3TGtRBeJgk33yuGBxrMPHi",
       "xpub661MyMwAqRbcFtXgS5sYJABqqG9YLmC4Q1Rdap9gSE8NqtwybGhePY2gZ29ESFjqJoCu1Rupje8YtGqsefD265TMg7usUDFdp6W1EGMcet8"),
      ([H, 1, 2 | H],
       "xprv9z4pot5VBttmtdRTWfWQmoH1taj2axGVzFqSb8C9xaxKymcFzXBDptWmT7FwuEzG3ryjH4ktypQSAewRiNMjANTtpgP4mLTj34bhnZX7UiM",
       "xpub6D4BDPcP2GT577Vvch3R8wDkScZWzQzMMUm3PWbmWvVJrZwQY4VUNgqFJPMM3No2dFDFGTsxxpG5uJh7n7epu4trkrX7x7DogT5Uv6fcLW5")]

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")


# Bitcoin Core's test framework (test/functional/test_framework/descriptors.py), for a second opinion in Python.
_INPUT = "0123456789()[],'/*abcdefgh@:$%{}IJKLMNOPQRSTUVWXYZ&+-.;<=>?!^_|~ijklmnopqrstuvwxyzABCDEFGH`#\"\\ "
_GEN = [0xf5dee51989, 0xa9fdca3312, 0x1bab10e32d, 0x3706b1677a, 0x644d626ffd]


def descsum(s: str) -> str:
    symbols, groups = [], []
    for c in s:
        v = _INPUT.find(c)
        symbols.append(v & 31)
        groups.append(v >> 5)
        if len(groups) == 3:
            symbols.append(groups[0] * 9 + groups[1] * 3 + groups[2])
            groups = []
    if len(groups) == 1:
        symbols.append(groups[0])
    elif len(groups) == 2:
        symbols.append(groups[0] * 3 + groups[1])
    chk = 1
    for value in symbols + [0] * 8:
        top = chk >> 35
        chk = (chk & 0x7ffffffff) << 5 ^ value
        for i in range(5):
            chk ^= _GEN[i] if (top >> i) & 1 else 0
    chk ^= 1
    return "".join("qpzry9x8gf2tvdw0s3jn54khce6mua7l"[(chk >> (5 * (7 - i))) & 31] for i in range(8))


def _serialize(version: int, depth: int, parent: bytes, number: int, chain: bytes, key: bytes) -> str:
    body = version.to_bytes(4, "big") + bytes([depth]) + parent + number.to_bytes(4, "big") + chain + key
    from arcade.script import b58encode
    return b58encode(body + hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4])


@pytest.fixture(scope="module")
def made(tmp_path_factory):
    bundle = tmp_path_factory.mktemp("restore") / "bundle.js"
    bundle.write_text(restore_kit.bundle())
    chains = restore_kit.chains()
    asked = {"bundle": str(bundle), "words": str(ROOT / "arcade" / "bip39-english.txt"), "phrase": PHRASE, "bad": BAD,
             "chains": chains, "descriptors": list(CORE_CHECKSUMS),
             "bip32": [{"seed": V1_SEED, "path": path} for path, _, _ in V1]}
    out = subprocess.run(["node", "--experimental-default-type=module", "--import", "./extension/test/register.mjs",
                          str(ROOT / "tests" / "js" / "restore_kit.mjs"), json.dumps(asked)],
                         capture_output=True, text=True, timeout=180, check=True, cwd=ROOT)
    return chains, json.loads(out.stdout)


def test_the_descriptor_checksum_is_bip380s_and_the_cores_own(made):
    _, got = made
    want = list(CORE_CHECKSUMS.values())
    assert got["checksums"] == want and got["bundledChecksums"] == want
    assert [descsum(d) for d in CORE_CHECKSUMS] == want


def test_extended_keys_are_bip32_test_vector_1(made):
    _, got = made
    assert [(g["xprv"], g["xpub"]) for g in got["bip32"]] == [(prv, pub) for _, prv, pub in V1]


def test_the_offline_pages_words_make_the_arcades_seed_and_refuse_a_bad_checksum(made):
    _, got = made
    assert got["seed"] == got["seedPlain"] == seed.to_seed(PHRASE).hex()
    assert "not in an order" in got["refused"]
    assert got["drawn"] == "function"


def test_every_chain_has_a_recipe_its_core_can_run():
    """Descriptors only on the Cores whose releases have descriptor wallets (Bells 3, DigiByte 8+); Litecoin 0.21's are
    built without SQLite (createwallet refused one on node 2's 0.21.5.8 and on 0.21.3, 2026-11-07), so it gets a legacy
    wallet, each kind's key and one rescan from a height; importprivkey on the rest; SegWit exactly where the chain has a
    bech32 prefix; each since before the chain's floor."""
    rows = {r["network"]: r for r in restore_kit.chains()}
    assert set(rows) == set(config.NETWORKS) - {"regtest"}, "a chain added to config.NETWORKS needs a row in RECIPES"
    descriptor_cores = {"bells": (3, 0), "digibyte": (8, 0)}
    for name, row in rows.items():
        release = core_install.RELEASES.get(name)
        if row["recipe"] == "descriptors":
            assert release and tuple(int(x) for x in release.version.split(".")[:2]) >= descriptor_cores[release.family]
            assert 0 < row["since"] <= restore_kit._day(restore_kit.RECIPES[name]["day"]), name
        elif row["recipe"] == "legacy-keys":
            assert release.family == "litecoin" and row["height"] > 0 and row["since"] > 0, name
        else:
            assert row["since"] == 0
        assert row["hrp"] == config.NETWORKS[name].bech32_hrp
    assert rows["bel"]["xkey"] == {"prv": 0x02FAC398, "pub": 0x02FACAFD}, "Bells 3 reads dgpv/dgub, not xprv/xpub"
    assert rows["dgb"]["taproot"] and rows["dgbtest"]["taproot"] and not rows["ltc"]["taproot"]
    assert {n for n, r in rows.items() if r["bip86"]} == {"ltc", "ltctest", "dgb", "dgbtest"} == taproot.CHAINS
    # An account this arcade knew before the floor's week rescans from a week before it knew the account.
    early = restore_kit._day("2026-09-01")
    assert {r["network"]: r for r in restore_kit.chains(early)}["dgb"]["since"] == early - restore_kit.WEEK
    # ...and, with no height known for then, Litecoin rescans its whole chain.
    assert {r["network"]: r for r in restore_kit.chains(early)}["ltc"]["height"] == 0
    assert {r["network"] for r in restore_kit.chains() if r["recipe"] == "legacy-keys"} == {"ltc", "ltctest"}


def _xprv_master(text: str) -> tuple[int, bytes, bytes]:
    raw = b58decode(text)
    body, check = raw[:-4], raw[-4:]
    assert hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4] == check
    assert body[4] == 0 and body[5:13] == bytes(8) and body[45] == 0
    return int.from_bytes(body[:4], "big"), body[13:45], body[46:78]


def test_every_chains_kit_is_the_arcades_own_keys(made):
    """Each chain's address, key and descriptors, from the real coins.js and from the offline page's bundle alike,
    against the words worked out here in Python, and against what the real Cores derived."""
    chains, got = made
    assert got["kits"] == got["bundledKits"], "the offline page's inlined code says something the real coins.js does not"
    s = seed.to_seed(PHRASE)
    master = hmac.new(b"Bitcoin seed", s, hashlib.sha512).digest()
    for row, kit in zip(chains, got["kits"]):
        name, p = row["network"], config.NETWORKS[row["network"]]
        coin = 3 if p.real_coins else 1
        key = _bip32(s, (44 | H, coin | H, H, 0, 0))
        pub = _pubkey(key)
        assert kit["address"] == b58check_encode(p.pubkeyhash_version, hash160(pub)), name
        assert kit["path"] == f"m/44'/{coin}'/0'/0/0"
        assert b58decode(kit["wif"])[1:34] == key + b"\x01", name
        if row["recipe"] == "descriptors":
            versions = row["xkey"] or ({"prv": 0x04358394, "pub": 0x043587CF} if coin == 1
                                       else {"prv": 0x0488ADE4, "pub": 0x0488B21E})
            kinds = {(d["kind"], d["branch"]) for d in kit["descriptors"]}
            want = {("legacy", 0), ("legacy", 1)}
            if p.bech32_hrp:
                want |= {(k, b) for k in ("p2sh-segwit", "bech32") for b in (0, 1)}
            if row["taproot"]:
                want |= {("bech32m", 0), ("bech32m", 1)}
            if row["taproot"] and row["bip86"]:
                want |= {("bip86", 0), ("bip86", 1)}
            assert kinds == want, name
            for d in kit["descriptors"]:
                body, _, check = d["desc"].partition("#")
                assert check == descsum(body), d["desc"]
                xprv = re.search(r"\(([1-9A-HJ-NP-Za-km-z]{100,})/", body).group(1)
                assert _xprv_master(xprv) == (versions["prv"], master[32:], master[:32]), name
                purpose = {"legacy": 44, "p2sh-segwit": 49, "bech32": 84, "bech32m": 44, "bip86": 86}[d["kind"]]
                assert body.endswith(f"/{purpose}h/{coin}h/0h/{d['branch']}/*)" + (")" if "sh(" in body else ""))
            asks = json.loads(kit["commands"][1].split(" importdescriptors '", 1)[1][:-1])
            assert [a["desc"] for a in asks] == [d["desc"] for d in kit["descriptors"]]
            assert all(a["range"] == [0, 20] and a["timestamp"] == row["since"] and a["active"] for a in asks)
            assert kit["commands"][0] == f'{row["cli"]}{" " + row["flags"] if row["flags"] else ""} createwallet ' \
                                         '"arcade" false true "" false true'
            node = _bip32(s, (44 | H, coin | H, H))
            assert kit["xpub"] == _serialize(versions["pub"], 3, hash160(_pubkey(_bip32(s, (44 | H, coin | H))))[:4],
                                             H, _chain_code(s, (44 | H, coin | H, H)), _pubkey(node))
        elif row["recipe"] == "legacy-keys":
            cli = f'{row["cli"]}{" " + row["flags"] if row["flags"] else ""}'
            purposes = (44, 49, 84) if p.bech32_hrp else (44,)
            assert [k["path"] for k in kit["keys"]] == [f"m/{n}'/{coin}'/0'/0/0" for n in purposes], name
            for k, n in zip(kit["keys"], purposes):
                assert b58decode(k["wif"])[1:34] == _bip32(s, (n | H, coin | H, H, 0, 0)) + b"\x01", (name, n)
            assert kit["commands"] == [f'{cli} createwallet "arcade"'] + [
                f'{cli} -rpcwallet=arcade importprivkey "{k["wif"]}" "arcade" false' for k in kit["keys"]] + [
                f'{cli} -rpcwallet=arcade rescanblockchain {row["height"]}']
            assert not kit["descriptors"]
        else:
            assert kit["commands"][-1].endswith(f'importprivkey "{kit["wif"]}" "arcade" true')
            assert not kit["descriptors"]
        if row["bip86"]:
            tap = _pubkey(_bip32(s, (86 | H, coin | H, H, 0, 0)))
            assert kit["segwit"][2] == {"path": f"m/86'/{coin}'/0'/0/0", "address": taproot.address(p, tap),
                                        "kind": "taproot", "imported": row["taproot"]}, name
        else:
            assert len(kit["segwit"]) == (2 if p.bech32_hrp else 0), name
        if row["taproot"]:
            out_key = taproot.tweak(taproot.xonly(pub))[0]
            assert kit["taproot"]["address"] == segwit_encode(p.bech32_hrp, 1, out_key)
            assert kit["taproot"]["digidollar"] == ddaddress.address(name, pub)
    by = {row["network"]: kit for row, kit in zip(chains, got["kits"])}
    for name, said in CORE_SAID.items():
        kit = by[name]
        if "tr86" in said:
            assert kit["segwit"][2]["address"] == said["tr86"], name
        if "pkh" not in said:
            continue
        assert kit["address"] == said["pkh"], name
        assert kit["segwit"][1]["address"] == said["wpkh"], name
        if "sh" in said:
            assert kit["segwit"][0]["address"] == said["sh"]
        if "tr" in said:
            assert kit["taproot"]["address"] == said["tr"]
    assert got["whole"] == 0, "the whole-chain switch rescans from block 0"


def _chain_code(s: bytes, path) -> bytes:
    """The chain code at `path` (test_extension's _bip32 keeps only the key)."""
    i = hmac.new(b"Bitcoin seed", s, hashlib.sha512).digest()
    key, chain = i[:32], i[32:]
    n = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
    for idx in path:
        data = (b"\x00" + key if idx & H else _pubkey(key)) + idx.to_bytes(4, "big")
        i = hmac.new(chain, data, hashlib.sha512).digest()
        key, chain = ((int.from_bytes(i[:32], "big") + int.from_bytes(key, "big")) % n).to_bytes(32, "big"), i[32:]
    return chain


def test_recover_is_served_with_nothing_from_anywhere(client):
    """The page to save: one file, its script and style allowed by their hashes alone, nothing fetched."""
    app, _ = client
    response = app.get("/recover")
    assert response.status_code == 200
    page, header = response.text, response.headers["content-security-policy"]
    meta = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]+)">', page).group(1)
    assert header == meta + "; frame-ancestors 'none'"
    rules = dict(part.strip().split(" ", 1) for part in meta.split(";"))
    assert rules["default-src"] == "'none'" and rules["connect-src"] == "'none'"
    for name in ("script-src", "style-src"):
        assert re.fullmatch(r"'sha256-[A-Za-z0-9+/=]{44}'", rules[name]), rules[name]
    assert "http" not in meta and "*" not in meta
    scripts = re.findall(r"<script([^>]*)>(.*?)</script>", page, re.S)
    assert len(scripts) == 1 and scripts[0][0] == ""
    digest = base64.b64encode(hashlib.sha256(scripts[0][1].encode()).digest()).decode()
    assert rules["script-src"] == f"'sha256-{digest}'"
    style = re.search(r"<style>(.*?)</style>", page, re.S).group(1)
    assert rules["style-src"] == "'sha256-" + base64.b64encode(hashlib.sha256(style.encode()).digest()).decode() + "'"
    for never in ("fetch(", "XMLHttpRequest", "WebSocket", "EventSource", "sendBeacon", "import(", "<link", " src=",
                  "<iframe", "@import", "url("):
        assert never not in page, never
    assert "Open this page offline" in page and "Never type your words into a website you reached by a link" in page
    js = app.get("/restore-kit.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert "export async function kitsFor" in js.text


def test_the_backup_page_offers_it_and_hands_the_table(client):
    """Signed in: the page links the offline page as a download and carries every chain's row, its rescan starting a
    week before this arcade first knew the account (just now) or the chain's floor, whichever is earlier."""
    from test_me_page import _seat
    app, _ = client
    _seat(app)
    page = app.get("/me/backup").text
    assert 'href="/recover"\n     download="recover-coins.html">Save this page and open it offline</a>' in page
    assert 'import("/restore-kit.js")' in page and 'id="restore-kits"' in page
    rows = json.loads(re.search(r"const RESTORE = (\[.*?\]);\n", page).group(1))
    assert rows == restore_kit.chains(), "a fresh account: every since is the chain's floor week"


def test_the_extensions_coins_js_is_the_arcades():
    assert (ROOT / "extension" / "coins.js").read_bytes() == (ROOT / "arcade" / "web" / "templates" / "coins.js").read_bytes()


def test_recover_and_its_script_are_public_and_claimed():
    from arcade import modules as modulelib
    from arcade.web import door
    for path in ("/recover", "/restore-kit.js"):
        assert door.public_path(path), path
        assert modulelib.module_of(path) == "wallet", path

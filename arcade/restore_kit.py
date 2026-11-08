"""Restoring an account's coins without an arcade: each chain's Core recipe, and the offline /recover page (2026-11-07).

SadFrog: "we want it to be easy for someone to be able to spin back up their wallets from any core if arcade goes down
as well as any arcade." Another arcade already restores from the twelve words (/restore, /join). This is the other half:
a chain's own Core, with no arcade at all.

The keys are standard BIP39/BIP32 on m/44'/<coin>'/0'/0/i (coins.js coinPath), but <coin> is 3 on every mainnet and 1 on
every test chain, so a wallet that looks on a chain's registered type (DigiByte 20, Litecoin 2) finds nothing from the
words alone. Each chain's Core is told the keys in the form it reads:

  descriptors     a blank descriptor wallet, then importdescriptors with pkh(), and sh(wpkh())/wpkh() where the chain
                  has SegWit (config.py bech32_hrp), each with its BIP380 checksum: Bells 3, DigiByte 9
  legacy-keys     createwallet (a legacy wallet), importprivkey for each address type's key with no rescan, then one
                  rescanblockchain from a block height: Litecoin 0.21, whose releases are built without SQLite and so
                  refuse descriptor wallets (0.21.3 and 0.21.5.8 both said "Descriptor wallets not supported",
                  2026-11-07, restoring @arcadedemo on node 2's Litecoin Core)
  privkey         importprivkey "<WIF>" "arcade" true into the one wallet.dat: Dogecoin 1.14 and its forks (Pepecoin,
                  Luckycoin, Flopcoin) and Kotia (Bitcoin 0.14's code), none of which has descriptors or createwallet
  privkey-wallet  createwallet first, then importprivkey: Wojakcoin 1.21.3 (Bitcoin Core 0.21's code, whose wallets
                  are legacy by default and which never activates SegWit)

`chains()` is the table the backup page and the offline page are handed; the commands themselves are built in the
browser by coins.js `restoreKit`, from the words, so no key ever reaches this side. `page()` is the offline page: one
self-contained HTML file, every module inlined from the real files (coins.js, restore_kit.js and the vendored
noble-secp256k1 and RIPEMD-160), no network, a strict CSP. tests/test_restore_kit.py keeps all of it honest.
"""
from __future__ import annotations

import base64
import calendar
import hashlib
import json
import posixpath
import re
from pathlib import Path

from . import config, core_install

WEB = Path(__file__).resolve().parent / "web"
TEMPLATES = WEB / "templates"

#: Each chain's Core and how it restores. `day` is roughly when the arcade began reading the chain (its floor, config.py):
#: the rescan starts a week before it, or a week before this arcade first knew the account if that is earlier. Only
#: the descriptor Cores take a timestamp; importprivkey rescans everything. `height` (legacy-keys) is the chain's first
#: block on or after that week-early day, read from node 2's Litecoin Cores; rescanblockchain takes a height, not a time.
#: Checked against the real Cores on 2026-11-07 (getdescriptorinfo / deriveaddresses): Litecoin 0.21.5.8 reads xpub and
#: tpub; Bells 3.0.0-rc2 reads Dogecoin's dgpv/dgub and refuses an xpub; DigiByte 9.26.7 reads xpub and gave the
#: arcade's own DGB address at index 0.
RECIPES = {
    "main": dict(recipe="privkey", cli="pepecoin-cli", core="Pepecoin Core (Dogecoin 1.14's code)"),
    "doge": dict(recipe="privkey"),
    "ltc": dict(recipe="legacy-keys", day="2026-10-17", height=3_192_404),
    "bel": dict(recipe="descriptors", day="2026-10-22", xkey={"prv": 0x02FAC398, "pub": 0x02FACAFD}),
    "lky": dict(recipe="privkey"),
    "kot": dict(recipe="privkey", cli="kotia-cli", core="Kotia 1.0.0 (Bitcoin 0.14's code)"),
    "wjk": dict(recipe="privkey-wallet"),
    "dgb": dict(recipe="descriptors", day="2026-10-25", taproot=True),
    "flop": dict(recipe="privkey"),
    "test": dict(recipe="privkey", cli="pepecoin-cli", flags="-testnet", core="Pepecoin Core (Dogecoin 1.14's code)"),
    "ltctest": dict(recipe="legacy-keys", flags="-testnet", day="2026-10-18", height=4_912_330),
    "dgbtest": dict(recipe="descriptors", flags="-testnet", day="2026-11-04", taproot=True),
}

WEEK = 7 * 86400


def _day(text: str) -> int:
    return calendar.timegm((int(text[:4]), int(text[5:7]), int(text[8:10]), 0, 0, 0))


def _cli(network: str) -> tuple[str, str]:
    """(the command-line client's name, the Core's name and version) from the installer's pinned release."""
    release = core_install.RELEASES[network]
    cli = Path(release.cli).name.replace("-linux-{m}", "")
    name = {"digibyte": "DigiByte", "wojakcoin": "WojakCoin"}.get(release.family, release.family.capitalize())
    return cli, f"{name} Core {release.version}"


def chains(known_since: int | None = None) -> list[dict]:
    """Every chain an account holds a key on, real coins first, as the browser's restoreKit takes them.

    `known_since` is when this arcade first knew the account (unix seconds), if it does: the rescan then starts a week
    before that or a week before the chain's floor, whichever is earlier."""
    out = []
    for network, how in RECIPES.items():
        p = config.NETWORKS[network]
        cli, core = (how["cli"], how["core"]) if "cli" in how else _cli(network)
        since = height = 0
        if how.get("day"):
            since = _day(how["day"]) - WEEK
            if known_since:
                since = min(since, int(known_since) - WEEK)
            since = max(0, since - since % 86400)
            # An account known before the floor's week has no height here to start from: the whole chain, then.
            height = how.get("height", 0) if since >= _day(how["day"]) - WEEK else 0
        out.append({"network": network, "label": p.label, "coin": p.coin, "real": p.real_coins,
                    "pubkeyhash": p.pubkeyhash_version, "scripthash": p.scripthash_version, "hrp": p.bech32_hrp,
                    "recipe": how["recipe"], "cli": cli, "flags": how.get("flags", ""), "core": core,
                    "since": since, "taproot": bool(how.get("taproot")), "xkey": how.get("xkey"),
                    "height": height})
    return out


# --- the offline page -----------------------------------------------------------------------------------------------

#: Where each module the page needs is, by the path the browser imports it from.
SOURCES = {"/coins.js": TEMPLATES / "coins.js", "/restore-kit.js": TEMPLATES / "restore_kit.js"}
IMPORT = re.compile(r"^import\s+(.+?)\s+from\s+[\"']([^\"']+)[\"'];?[ \t]*$", re.M)
EXPORT_LIST = re.compile(r"^export\s*\{([^}]*)\}\s*;?", re.M)
EXPORT_DEFAULT = re.compile(r"^export\s+default\s+([A-Za-z_$][\w$]*)\s*;?", re.M)
EXPORT_DECL = re.compile(r"^export\s+((?:async\s+)?function\*?|class|const|let|var)\s+([A-Za-z_$][\w$]*)", re.M)


def _file(path: str) -> Path:
    if path in SOURCES:
        return SOURCES[path]
    if path.startswith("/vendor/"):
        return WEB / "vendor" / path[len("/vendor/"):]
    raise ValueError(f"the offline page cannot inline {path}")


def _module(path: str, text: str) -> tuple[str, list[str]]:
    """One ES module as the body of a function: its imports read from the modules already loaded, its exports returned.
    Returns (that body, the paths it imports)."""
    deps = []

    def imported(match: re.Match) -> str:
        what, spec = match.group(1).strip(), match.group(2)
        dep = posixpath.normpath(posixpath.join(posixpath.dirname(path), spec)) if spec.startswith(".") else spec
        deps.append(dep)
        ref = f"M[{json.dumps(dep)}]"
        if what.startswith("* as "):
            return f"const {what[5:].strip()} = {ref};"
        if what.startswith("{"):
            names = [n.strip() for n in what.strip("{} ").split(",") if n.strip()]
            return "const {" + ", ".join(n.replace(" as ", ": ") for n in names) + "} = " + ref + ";"
        return f"const {what} = {ref}.default;"

    text = IMPORT.sub(imported, text)
    exports: list[tuple[str, str]] = []

    def listed(match: re.Match) -> str:
        body = re.sub(r"//[^\n]*", "", match.group(1))
        for part in (p.strip() for p in body.split(",")):
            if part:
                local, _, name = part.partition(" as ")
                exports.append(((name or local).strip(), local.strip()))
        return ""

    text = EXPORT_LIST.sub(listed, text)

    def default(match: re.Match) -> str:
        exports.append(("default", match.group(1)))
        return ""

    text = EXPORT_DEFAULT.sub(default, text)

    def declared(match: re.Match) -> str:
        exports.append((match.group(2), match.group(2)))
        return f"{match.group(1)} {match.group(2)}"

    text = EXPORT_DECL.sub(declared, text)
    if re.search(r"^\s*(?:import|export)\b[^(]", text, re.M):
        raise ValueError(f"{path}: an import or export the offline page's bundler does not read")
    returned = ", ".join(f"{json.dumps(name)}: {local}" for name, local in exports)
    return f'"use strict";\n{text}\nreturn {{{returned}}};', deps


def bundle(entry: str = "/restore-kit.js") -> str:
    """Every module `entry` needs, in the order they load, as one classic script that fills `M` (path -> exports)."""
    done: dict[str, str] = {}

    def load(path: str, stack: tuple = ()) -> None:
        if path in done:
            return
        if path in stack:
            raise ValueError(f"a cycle through {path}")
        body, deps = _module(path, _file(path).read_text())
        for dep in deps:
            load(dep, stack + (path,))
        done[path] = body

    load(entry)
    parts = [f"M[{json.dumps(path)}] = await (async () => {{\n{body}\n}})();" for path, body in done.items()]
    return "\n".join(parts)


def _hash(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode() + "'"


def page() -> str:
    """The offline recovery page: one HTML file, nothing fetched, everything it runs inside it."""
    return build()[0]


def build() -> tuple[str, str]:
    """(the offline page, its Content-Security-Policy), the policy also in the page's own meta tag for a saved copy."""
    html = (TEMPLATES / "recover.html").read_text()
    words = (Path(__file__).resolve().parent / "bip39-english.txt").read_text().split()
    script = (html.split("<script>", 1)[1].split("</script>", 1)[0]
              .replace("/*@BUNDLE@*/", bundle())
              .replace("/*@CHAINS@*/null", json.dumps(chains()))
              .replace("/*@WORDS@*/null", json.dumps(words)))
    style = html.split("<style>", 1)[1].split("</style>", 1)[0]
    head, rest = html.split("<script>", 1)
    html = head + "<script>" + script + "</script>" + rest.split("</script>", 1)[1]
    policy = csp(script, style)
    return html.replace("@CSP@", policy), policy


def csp(script: str, style: str) -> str:
    """Nothing from anywhere: the one inline script and the one style block, by their hashes."""
    return (f"default-src 'none'; script-src {_hash(script)}; style-src {_hash(style)}; img-src data:; "
            "connect-src 'none'; form-action 'none'; base-uri 'none'; frame-src 'none'; object-src 'none'")


if __name__ == "__main__":                                    # python -m arcade.restore_kit > recover.html
    print(page(), end="")

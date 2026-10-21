"""Leaked passwords refused at sign-up and restore (2026-10-20, answering SadFrog's review). wallet.js's
`leaked()` is run here under Node, against a fake answer: only the first 5 hex characters of the SHA-1 go out, the
endings that come back are compared locally, and an unreachable list lets the password through (null)."""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

WALLET = pathlib.Path(__file__).resolve().parent.parent / "arcade" / "web" / "templates" / "wallet_js.js"
NODE = shutil.which("node") or shutil.which("nodejs")


def _leaked_source() -> str:
    src = WALLET.read_text()
    start = src.index("export async function leaked(password)")
    end = src.index("\n}\n", start) + 3
    helpers = re.search(r"const enc = new TextEncoder\(\);\nconst hex = [^\n]*\n[^\n]*\n", src)
    return helpers.group(0) + "\n" + src[start:end].replace("export async function", "async function")


@pytest.mark.skipif(NODE is None, reason="needs Node")
def test_only_a_hash_prefix_leaves_and_a_leaked_password_is_counted():
    # SHA-1("password") = 5BAA6 1E4C9B93F3F0682250B6CF8331B7EE68FD8
    script = _leaked_source() + r'''
const LEAKED_URL = "https://api.pwnedpasswords.com/range/";   // no ARCADE_LEAKED_URL here: the built-in list
const asked = [];
let mode = "ok";
globalThis.fetch = async (url, opts) => {
  asked.push([url, opts.headers["Add-Padding"], opts.credentials]);
  if (mode === "down") throw new Error("offline");
  return {ok: true, text: async () =>
    "0018A45C4D1DEF81644B54AB7F969B88D65:1\r\n1E4C9B93F3F0682250B6CF8331B7EE68FD8:9659365\r\n1E4C9B93F3F0682250B6CF8331B7EE68FD9:0\r\n"};
};
(async () => {
  const out = {};
  out.leaked = await leaked("password");
  out.clean = await leaked("a quite unusual sentence nobody has used");
  out.empty = await leaked("");
  mode = "down";
  out.down = await leaked("password");
  out.asked = asked;
  console.log(JSON.stringify(out));
})();
'''
    got = json.loads(subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True).stdout)
    assert got["leaked"] == 9659365
    assert got["clean"] == 0 and got["empty"] == 0
    assert got["down"] is None, "the list away: let through, never block a sign-up on another server"
    url, padding, creds = got["asked"][0]
    assert url == "https://api.pwnedpasswords.com/range/5BAA6", "only five hex characters of the hash go out"
    assert padding == "true" and creds == "omit"


def test_sign_up_and_restore_refuse_a_leaked_password_before_sealing():
    """Every place a wallet is sealed with a NEW password (sign-up, restore) asks the list first."""
    src = WALLET.read_text()
    calls = [m.start() for m in re.finditer(r"const blob = await seal\(", src)]
    assert len(calls) == 2, "sign-up and restore"
    for at in calls:
        before = src[src.rindex("\n", 0, src.rindex("\n", 0, at)):at]
        assert "await refuseLeaked(password);" in before, src[at - 120:at + 40]


@pytest.mark.skipif(NODE is None, reason="needs Node")
def test_an_empty_list_address_turns_the_check_off():
    script = _leaked_source() + r'''
const LEAKED_URL = "https://api.pwnedpasswords.com/range/";
globalThis.ARCADE_LEAKED_URL = "";
globalThis.fetch = async () => { throw new Error("asked anyway"); };
leaked("password").then((n) => console.log(JSON.stringify(n)));
'''
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "null", "off: nothing asked, the password let through"


def test_every_page_says_where_the_list_is():
    base = (WALLET.parent / "base.html").read_text()
    assert "var ARCADE_LEAKED_URL = {{ leaked_url()|tojson }};" in base

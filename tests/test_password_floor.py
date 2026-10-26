"""Twelve characters at least for every new password (issue #12).

2026-10-25, SadFrog: "12-character minimum for new passwords, and existing short-password users are asked to change".
An account's sealed wallet is handed to anybody who asks for the name, so it can be guessed at offline; until this a
1-character password was let through (D-157). The node never sees an account's password, so the floor for accounts
lives in the browser code (signin.js / wallet.js) and these tests read it there, and run its logic under Node. The
operator's own password reaches the node and is refused there too (test_password, test_admin). A sign-in with a
password from before the floor is let in and asked, dismissibly, to change it (base.html's bar).
"""
import json
import pathlib
import re
import shutil
import subprocess
import sys

import pytest
from nacl.signing import SigningKey

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_auth_web import LOCAL, _sign_in                       # noqa: E402

from arcade import accounts                                      # noqa: E402

TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "arcade" / "web" / "templates"
WALLET = TEMPLATES / "wallet_js.js"
SIGNIN = TEMPLATES / "signin.js"
APP = TEMPLATES.parent / "app.py"
NODE = shutil.which("node") or shutil.which("nodejs")


def _body(src: str, head: str) -> str:
    """One top-level function's source, from its first line to the closing brace at the start of a line."""
    start = src.index(head)
    return src[start:src.index("\n}\n", start) + 3]


# --- the one number --------------------------------------------------------------------------------------------

def test_the_floor_is_twelve_in_the_node_and_in_the_browser():
    assert accounts.MIN_PASSWORD == 12
    assert "export const MIN_PASSWORD = 12;" in SIGNIN.read_text()
    assert "export const MIN_PASSWORD = signer.MIN_PASSWORD;" in WALLET.read_text(), "one number, not two"
    assert "export const MIN_PASSWORD = 1;" not in WALLET.read_text()


# --- every place a new password is chosen ----------------------------------------------------------------------

def test_every_wallet_sealed_under_a_new_password_is_checked_first():
    """Sign-up and restore are the two places a wallet is sealed under a password somebody just chose. Each refuses
    one below the floor before anything else happens; a third `seal(` would need the same and fails this first."""
    src = WALLET.read_text()
    assert len(re.findall(r"await seal\(", src)) == 2, "a new sealing path: give it the floor too"
    for head in ("async function _signUp(", "async function _restore("):
        body = _body(src, head)
        assert "tooShort(password)" in body, head
        assert body.index("tooShort(password)") < body.index("await seal("), head
    sign_up = _body(src, "async function _signUp(")
    assert sign_up.index("tooShort(password)") < sign_up.index("signer.generate()"), "before any words are made"
    restore = _body(src, "async function _restore(")
    assert restore.index("tooShort(password)") < restore.index("seatWith("), "before the words sign in"


def test_the_copy_kept_in_this_browser_has_the_same_floor():
    src = SIGNIN.read_text()
    keep = _body(src, "export async function keep(")
    assert "tooShort(password)" in keep and "length < 8" not in keep
    join = (TEMPLATES / "join.html").read_text()
    at = join.index('signer.keep(phrase, $("keep-password").value)')
    assert 'signer.tooShort($("keep-password").value)' in join[:at]


def test_the_node_s_own_password_is_checked_on_the_page_and_by_the_node():
    join = (TEMPLATES / "join.html").read_text()
    save = join[join.index('$("password-save").onclick'):]
    assert save.index('signer.tooShort($("new-password").value)') < save.index('"/auth/set-password"')
    app = APP.read_text()
    assert "len(password) < 10" not in app
    assert "len(password) < accountslib.MIN_PASSWORD" in app, "the admin password"
    assert "(12+ characters)" in (TEMPLATES / "admin.html").read_text()


def test_every_new_password_box_says_twelve(client):
    app, _ = client
    for path in ("/join", "/restore"):
        body = app.get(path, headers=LOCAL).text
        assert 'placeholder="at least twelve characters"' in body, path
    assert "at least twelve characters" in (TEMPLATES / "game_door.html").read_text()
    keys = app.get("/join/keys", headers=LOCAL).text
    assert "at least twelve characters" in keys
    assert app.get("/auth/door", headers=LOCAL).json()["min_password"] == 12


def test_the_meters_colour_a_short_password_as_bad():
    for name in ("signup.html", "game_door.html", "restore.html"):
        page = (TEMPLATES / name).read_text()
        assert "said.short ?" in page and 'said.level === "instantly"' not in page, name


# --- the browser logic, run under Node ---------------------------------------------------------------------------

def _logic() -> str:
    """signin.js's floor and wallet.js's meter and short-password marks, without the rest of either module."""
    signin = SIGNIN.read_text()
    start = signin.index("export const MIN_PASSWORD = 12;")
    floor = signin[start:signin.index("\n}\n", signin.index("export function tooShort(")) + 3]
    wallet = WALLET.read_text()
    meter = wallet[wallet.index("const GUESSES_A_SECOND"):wallet.index("\n}\n", wallet.index("export function strength(")) + 3]
    marks = wallet[wallet.index("export const SHORT_PASSWORD"):
                   wallet.index("\n}\n", wallet.index("export function dismissShort(")) + 3]
    signer = floor.replace("export ", "")
    return ("const signer = (() => {\n" + signer + "\nreturn {MIN_PASSWORD, characters, tooShort};\n})();\n"
            + "const MIN_PASSWORD = signer.MIN_PASSWORD, tooShort = signer.tooShort;\n"
            + (meter + "\n" + marks).replace("export ", "") + r'''
const store = new Map();
globalThis.sessionStorage = {getItem: (k) => store.has(k) ? store.get(k) : null,
                             setItem: (k, v) => store.set(k, String(v)), removeItem: (k) => store.delete(k)};
''')


@pytest.mark.skipif(NODE is None, reason="needs Node")
def test_the_floor_counts_characters_as_a_person_does():
    script = _logic() + r'''
console.log(JSON.stringify({
  empty: tooShort(""), eleven: tooShort("x".repeat(11)), twelve: tooShort("x".repeat(12)),
  emoji11: tooShort("\u{1F600}".repeat(11)), emoji12: tooShort("\u{1F600}".repeat(12)),
  meter: strength("1234"), meter12: strength("123456789012"),
}));
'''
    got = json.loads(subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True).stdout)
    assert "at least 12 characters" in got["empty"] and "this one has" not in got["empty"]
    assert "(this one has 11)" in got["eleven"]
    assert got["twelve"] == "" and got["emoji12"] == ""
    assert "(this one has 11)" in got["emoji11"], "an emoji is one character, as Python's len counts it"
    assert got["meter"]["short"] is True and got["meter"]["ok"] is False and "(8 more)" in got["meter"]["says"]
    assert not got["meter12"].get("short") and got["meter12"]["level"] == "in a while"


@pytest.mark.skipif(NODE is None, reason="needs Node")
def test_a_short_sign_in_is_asked_about_until_not_now():
    """Asked, not forced. A sign-in with a short password marks the tab; "Not now" stops the asking in this tab;
    a long password (or a new one from restore) clears it. Only the mark is kept, never the password."""
    script = _logic() + r'''
const out = {};
out.long = [noteShort("a long enough password"), shortPassword()];
out.short = [noteShort("1234"), shortPassword(), [...store.values()]];
noteShort("a long enough password");
out.cleared = shortPassword();
noteShort("1234", "operator");
out.operator = shortPassword();
dismissShort();
out.dismissed = shortPassword();
noteShort("1234");
out.again = shortPassword();
store.clear();
noteShort("1234");
dismissShort({changed: true});
noteShort("1234");
out.changed = shortPassword();
console.log(JSON.stringify(out));
'''
    got = json.loads(subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True).stdout)
    assert got["long"] == [False, ""]
    assert got["short"][:2] == [True, "account"]
    assert "1234" not in json.dumps(got["short"][2]), "the password is never kept"
    assert got["cleared"] == ""
    assert got["operator"] == "operator"
    assert got["dismissed"] == "" and got["again"] == "", "not now holds for this tab"
    assert got["changed"] == "account", "a new password is not a not-now"


def test_every_sign_in_path_marks_a_short_password():
    src = WALLET.read_text()
    for head in ("async function _signIn(", "async function bringHere(", "async function _openFile(",
                 "async function _restore("):
        assert "noteShort(password)" in _body(src, head), head
    assert "sessionStorage.removeItem(SHORT_PASSWORD)" in _body(src, "export async function signOut(")
    join = (TEMPLATES / "join.html").read_text()
    assert 'noteShort($("password").value, "operator")' in join, "the node's own sign-in"
    assert "w.noteShort(typed, 'operator')" in (TEMPLATES / "admin_login.html").read_text()


def test_the_bar_is_on_every_signed_in_page_and_reads_wallet_js_s_marks(client):
    src = WALLET.read_text()
    mark = re.search(r'export const SHORT_PASSWORD = "([^"]+)"', src).group(1)
    not_now = re.search(r'const SHORT_DISMISSED = "([^"]+)"', src).group(1)
    app, _ = client
    assert _sign_in(app, SigningKey.generate()).status_code == 200
    page = app.get("/me", headers=LOCAL).text
    assert 'id="shortpwbar"' in page and 'id="shortpw-later"' in page
    bar = page[page.index('id="shortpwbar"'):]
    assert f'sessionStorage.getItem("{mark}")' in bar and f'"{not_now}"' in bar
    assert '"/restore?next="' in bar and "shorter than 12 characters" in bar
    assert 'id="shortpwbar"' not in app.get("/restore", headers=LOCAL).text, "not on the page that changes it"

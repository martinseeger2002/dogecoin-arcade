"""Every script a page hands to a browser still parses.

Templates hot-reload, and a page whose JavaScript is broken renders exactly as
it did before: status 200, the right markup, the right text -- and a browser
that runs nothing at all. Not one server-side test can see that, because the
failure happens after the response leaves the process. It is the shape of bug
that a one-character slip in `base.html` produces, which takes the JavaScript
off every page on the site in one go.

So this asks the one question that survives the rendering: can the browser
parse what it was given. Pages are fetched through the node-less app, so what
gets checked is the real output of the template, not a guess at it.

`node` is the only JavaScript parser on the box, and this is the only place the
suite uses it. Without it the file does nothing -- which is why it says so out
loud rather than passing quietly.
"""

import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                                       # noqa: F402,E402

NODE = shutil.which("node")

#: Every page the app answers with HTML and a script, taken from the routes
#: rather than from memory: a page that needs a path parameter is not here, and
#: neither is anything that answers JSON. The list is the claim, so a new page
#: belongs in it -- which is the same bargain test_public makes about its own.
PAGES = [
    "/", "/admin", "/admin/login", "/approvals", "/backup", "/clone", "/collections",
    "/compose", "/contacts", "/create", "/docs", "/exchange", "/feed", "/games",
    "/guide", "/inbox", "/inscriptions", "/inscriptions/collection", "/instances",
    "/join", "/join/keys", "/launch", "/launches", "/listings", "/me", "/me/accounts",
    "/me/backup", "/me/contacts", "/me/messages", "/me/nfts", "/me/notifications",
    "/me/runs", "/me/wallet", "/me/wallet/tokens", "/messages", "/mintpad/new",
    "/nfts", "/restore", "/tokens", "/tokens/launchpad/preview", "/wallet",
    "/wallet/nfts", "/wallet/tokens",
]

#: The JavaScript that arrives as a response of its own rather than inside a
#: page. Read from the routes, not from the templates folder, because some of
#: these are not the file a programmer wrote: `/sw.js` is rendered, and
#: `/signin.js` and `/r/storage.js` are wrapped by the route that serves them.
#: A check that read the files would skip the Jinja ones and mis-read the
#: wrapped ones, and the thing that has to parse is the response.
SCRIPTS = [
    "/coins.js", "/messaging.js", "/r/escrow.js", "/r/node.js", "/r/owner.js",
    "/r/state.js", "/r/storage.js", "/r/swap.js", "/signin.js", "/sw.js",
    "/wallet.js",
]

SCRIPT = re.compile(r"(<script(?![^>]*\bsrc=)[^>]*>)(.*?)</script>", re.S)
MODULE = re.compile(r"^\s*(?:import|export)\b", re.M)


def parses(source: str, module: bool) -> str:
    """What node says about `source`, or "" when it is fine."""
    if not source.strip():
        return ""
    suffix = ".mjs" if module else ".js"
    with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False) as fh:
        fh.write(source)
        name = fh.name
    try:
        out = subprocess.run([NODE, "--check", name], capture_output=True, text=True)
    finally:
        pathlib.Path(name).unlink(missing_ok=True)
    if out.returncode == 0:
        return ""
    lines = [line.strip() for line in out.stderr.splitlines() if line.strip()]
    return " | ".join(lines[:4])


def scripts_in(html: str):
    for tag, body in SCRIPT.findall(html):
        yield body, "type=\"module\"" in tag or "type='module'" in tag


@pytest.fixture(scope="module")
def js_parser():
    """`node`, or a skip. Named so it cannot be mistaken for the chain's node."""
    if not NODE:
        pytest.skip("no node to parse the pages' JavaScript with")
    return NODE


def test_every_script_on_every_page_parses(client, js_parser):
    """A page whose JavaScript cannot be parsed is a page that does nothing."""
    app, _ = client
    broken, seen = [], 0
    for path in PAGES:
        response = app.get(path)
        assert response.status_code == 200, f"{path} returned {response.status_code}"
        page = response.text.lower()
        assert "<html" in page or "<!doctype html" in page, f"{path} is not a page"
        for n, (source, module) in enumerate(scripts_in(response.text)):
            seen += 1
            why = parses(source, module)
            if why:
                broken.append(f"{path} script #{n}: {why}")
    assert seen > 200, f"only {seen} scripts checked; the pages are not being rendered"
    assert not broken, "\n".join(broken)


def test_the_scripts_served_under_their_own_names_parse(client, js_parser):
    """The JavaScript a browser is sent as a response of its own."""
    app, _ = client
    broken = []
    for path in SCRIPTS:
        response = app.get(path)
        assert response.status_code == 200, f"{path} returned {response.status_code}"
        why = parses(response.text, bool(MODULE.search(response.text)))
        if why:
            broken.append(f"{path}: {why}")
    assert not broken, "\n".join(broken)

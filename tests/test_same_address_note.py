"""The same address on Dogecoin and DigiByte, and the words' coin type on Backup (2026-11-09, security review).

Every real coin takes its key from coin type 3 (coins.js COIN_TYPE: m/44'/3'/..., m/49'/3', m/84'/3'), and Dogecoin and
DigiByte both write a legacy key-hash address with version byte 30, so an account's legacy "D..." address is the same
string on both chains. Receive says so beside such an address (wallet.js sameAddressNote, drawn by ArcadeCore's Receive
tab and the website Wallet's Receive sheet), and Backup says the words use coin type 3, so a wallet that expects DigiByte's
20 or Litecoin's 2 shows an empty wallet. The note's own answers, address by address, are in a real browser
(test_same_address_note_browser.py)."""

import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from nacl.signing import SigningKey

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_web import app_state  # noqa: F401,E402
from test_auth_web import _sign_in  # noqa: E402

from arcade import config  # noqa: E402
from arcade.web.app import create_app  # noqa: E402

TEMPLATES = Path(__file__).resolve().parent.parent / "arcade" / "web" / "templates"
LITE = Path(__file__).resolve().parent.parent / "arcade" / "lite" / "templates"
OUTSIDE = {"host": "arcade.example.com", "cf-connecting-ip": "198.51.100.9"}
NOTE = "This address is the same on Dogecoin and DigiByte. Check the chain before sending."


@pytest.fixture
def app(app_state):  # noqa: F811
    app_state.set_setting("public_hosts", ["arcade.example.com"])                 # as test_lite signs in from outside
    yield TestClient(create_app(app_state))
    app_state.set_setting("public_hosts", [])


def _coin_types():
    said = re.search(r"export const COIN_TYPE = \{(.*?)\};", (TEMPLATES / "coins.js").read_text()).group(1)
    return {name: int(n) for name, n in re.findall(r"(\w+): (\d+)", said)}


def test_dogecoin_and_digibyte_are_the_only_chains_whose_legacy_address_is_the_same_string():
    """Two chains make the same legacy address from one account when they share both the coin type and the key-hash
    byte. Among every chain but regtest (a test harness's own chain, never public) only Dogecoin and DigiByte do, and
    that pair is exactly what wallet.js names."""
    types = _coin_types()
    shared = {}
    for p in (p for p in config.NETWORKS.values() if p.name != "regtest"):
        shared.setdefault((types.get(p.name, 1), p.pubkeyhash_version), set()).add(p.name)
    twins = [names for names in shared.values() if len(names) > 1]
    assert twins == [{"doge", "dgb"}], twins
    wallet = (TEMPLATES / "wallet_js.js").read_text()
    assert 'export const SAME_LEGACY_ADDRESS = {doge: "dgb", dgb: "doge"};' in wallet
    assert f'export const SAME_ADDRESS_NOTE = "{NOTE}";' in wallet


def test_both_receive_views_draw_the_note_and_the_node_wallet_draws_none():
    """ArcadeCore's Receive tab has its line and fills it from wallet.js for the address it shows (not a DigiDollar
    address); the website Wallet's Receive sheet and its address chip do the same. /node-wallet.js has no such call,
    so the node's own wallet guards it rather than throwing."""
    lite = (LITE / "_wallet.html").read_text()
    receive = lite.split('<section data-panel="receive"', 1)[1].split("</section>", 1)[0]
    assert '<p class="warn" id="w-same" hidden></p>' in receive
    assert 'const same = c.dd || !wallet.sameAddressNote ? "" : wallet.sameAddressNote(c.network, address);' in lite
    assert '$("w-same").textContent = same; $("w-same").hidden = !same;' in lite
    site = (TEMPLATES / "my_wallet.html").read_text()
    sheet = site.split("function openReceive() {", 1)[1].split("\n}\n", 1)[0]
    assert "wallet.sameAddressNote(c.network, c.address)" in sheet and 'twin.id = "receive-same"' in sheet
    assert "wallet.sameAddressNote(c.network, shownAddr)" in site
    assert "sameAddressNote" not in (TEMPLATES / "node_wallet_js.js").read_text()


def _says_coin_type_3(html, id_):
    line = re.search(rf'<p class="muted" id="{id_}"[^>]*>(.*?)</p>', html, re.S)
    assert line, f"#{id_} is on the page"
    text = re.sub(r"\s+", " ", line.group(1))
    assert "These words use <b>coin type 3</b> for every coin" in text
    assert "(DigiByte 20, Litecoin 2) shows an empty wallet until it is pointed at coin type 3" in text
    assert '<a href="/recover" download="recover-coins.html">recovery page</a>' in text


def test_the_websites_backup_says_the_words_use_coin_type_3_and_links_the_recovery_page(app):
    """Beside the words, and in step with the Litecoin key's own note on the same page (m/44'/2' in most Litecoin
    wallets, so the words alone show an empty wallet there)."""
    from test_me_page import _seat
    _seat(app)
    page = app.get("/me/backup").text
    _says_coin_type_3(page, "coin-type-note")
    assert page.index('id="words-list"') < page.index('id="coin-type-note"') < page.index('id="ltc-key-box"')
    assert "Most Litecoin wallets look on a different path (<code>m/44'/2'/&hellip;</code>)" in page


def test_arcadecores_backup_says_the_words_use_coin_type_3_and_links_the_recovery_page(app, app_state):  # noqa: F811
    assert _sign_in(app, SigningKey.generate(), headers=OUTSIDE).status_code == 200
    app_state.set_setting("edition", "lite")
    try:
        page = app.get("/me/backup", headers=OUTSIDE).text
    finally:
        app_state.set_setting("edition", None)
    _says_coin_type_3(page, "k-coin-type")
    assert page.index('id="k-words"') < page.index('id="k-coin-type"') < page.index('id="k-hide"')

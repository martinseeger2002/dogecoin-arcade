"""The same address on Dogecoin and DigiByte (2026-11-09, security review).

Every real coin takes its key from coin type 3 (coins.js COIN_TYPE: m/44'/3'/..., m/49'/3', m/84'/3'), and Dogecoin and
DigiByte both write a legacy key-hash address with version byte 30, so an account's legacy "D..." address is the same
string on both chains. Receive says so beside such an address (wallet.js sameAddressNote, drawn by ArcadeCore's Receive
tab and the website Wallet's Receive sheet). The note's own answers, address by address, are in a real browser
(test_same_address_note_browser.py)."""

import re
from pathlib import Path

from arcade import config

TEMPLATES = Path(__file__).resolve().parent.parent / "arcade" / "web" / "templates"
LITE = Path(__file__).resolve().parent.parent / "arcade" / "lite" / "templates"
NOTE = "This address is the same on Dogecoin and DigiByte. Check the chain before sending."


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


"""Dogecoin as the arcade's third network (2026-10-13: "Incorporate Dogecoin
with every page of both the public facing Dogecoin arcade and the admin facing
Dogecoin arcade. Have the Doge options grayed out until the node is fully synced",
and "use blue for pep testnet indicators green for main pep ... yellow for doge")."""

import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

from arcade.config import NETWORKS                               # noqa: E402
from arcade.script import b58check_decode, b58check_encode       # noqa: E402

PEP_MAIN = b58check_encode(56, bytes(range(20)))
DOGE = b58check_encode(30, bytes(range(20)))


def _with_doge(state, monkeypatch, ready: bool):
    from arcade.web.state import ChainContext
    monkeypatch.setattr(state, "doge", ChainContext(
        network="doge", role="ledger", label="Dogecoin",
        conf=pathlib.Path("/nonexistent/dogecoin.conf")), raising=False)
    said = {"ready": ready, "progress": 1.0 if ready else 0.4231, "online": True,
            "blocks": 100 if ready else 42, "headers": 100}
    real = type(state).chain_sync
    monkeypatch.setattr(type(state), "chain_sync",
                        lambda self, chain: said if chain.is_doge else real(self, chain))
    return state.doge


def test_dogecoin_is_a_network_of_its_own():
    doge = NETWORKS["doge"]
    assert doge.coin == "DOGE" and doge.label == "Dogecoin" and doge.pubkeyhash_version == 30
    assert doge.activation_height == 6_415_000 and doge.messaging_start_height == 6_415_000
    assert doge.marker.startswith("D")
    assert NETWORKS["main"].coin == "PEP" and NETWORKS["test"].coin == "PEP TEST"


def test_a_syncing_dogecoin_is_shown_and_never_used(client, monkeypatch):
    app, state = client
    doge = _with_doge(state, monkeypatch, ready=False)
    assert doge in state.all_chains and doge not in state.token_chains, \
        "shown on the pages, kept out of everything that does work"
    assert doge.is_mainnet and doge.net == "dogecoin"
    page = app.get("/exchange?tab=tokens").text
    chip = re.search(r'<span class="ex-chip chain-off"[^>]*><span class="netbadge icon"[^>]*><img class="neticon" src="/coin-doge.png"', page)
    assert chip and "42.3% so far" in chip.group(0), "grayed out, with how far it has got"
    assert "show=dogecoin" not in page
    for tab in ("market", "mintpads"):
        assert "chain-off" in app.get(f"/exchange?tab={tab}").text, tab
    assert app.get("/exchange?tab=tokens&show=dogecoin").status_code == 200


def test_a_synced_dogecoin_opens(client, monkeypatch):
    app, state = client
    doge = _with_doge(state, monkeypatch, ready=True)
    assert doge in state.token_chains
    page = app.get("/exchange?tab=tokens").text
    assert 'href="/exchange?tab=tokens&show=dogecoin"' in page and 'src="/coin-doge.png" alt="DOGE"></span></a>' in page


def test_the_tip_picker_grays_dogecoin_until_it_syncs(client, monkeypatch):
    app, state = client
    _with_doge(state, monkeypatch, ready=False)
    from arcade.web.app import TEMPLATES, create_app
    create_app(state)
    html = TEMPLATES.env.from_string('{% import "_tip.html" as t with context %}{{ t.tip_pay() }}').render()
    doge = re.search(r'<option value="doge"[^>]*>[^<]*</option>', html)
    assert doge and " disabled" in doge.group(0) and "D main &mdash; syncing 42.3%" in doge.group(0)


def test_the_operator_wallet_and_admin_show_the_sync(client, monkeypatch):
    app, state = client
    _with_doge(state, monkeypatch, ready=False)
    page = app.get("/wallet/tools").text
    assert "D main opens here when its node has finished syncing" in page and "42.3%" in page
    from test_admin import LOCAL
    wallets = app.get("/admin/api/state", headers=LOCAL).json()["wallets"]
    doge = [w for w in wallets if w["which"] == "doge"]
    assert doge and doge[0]["syncing"] and doge[0]["progress"] == 42.3


def test_a_dogecoin_send_takes_a_dogecoin_address_and_nothing_else(client, monkeypatch):
    from arcade.web.app import _check_address, _doge_twin
    app, state = client
    doge = _with_doge(state, monkeypatch, ready=True)
    assert _check_address(DOGE, mainnet=doge) is None
    assert "not a" in (_check_address(PEP_MAIN, mainnet=doge) or ""), "a Pepecoin address on Dogecoin"
    assert "Dogecoin" in (_check_address(DOGE, mainnet=state.ledger) or ""), "and the other way round"
    assert _check_address(PEP_MAIN, mainnet=state.ledger) is None
    assert _check_address(PEP_MAIN, mainnet=True) is None, "a bool keeps its old meaning"
    # The same key on Dogecoin as on Pepecoin mainnet (BIP44 coin type 3).
    assert _doge_twin(PEP_MAIN) == DOGE and _doge_twin(DOGE) == ""


def test_the_network_colors(client):
    app, _ = client
    css = app.get("/exchange?tab=tokens").text
    assert "--net-test:#22a35a" in css and "--net-main:#22a35a" in css and "--net-doge:#d4a106" in css, \
        "PEP green on both its networks, DOGE yellow (2026-10-13)"
    assert ".netbadge.test{background:color-mix(in srgb,var(--net-test)" in css
    assert ".tag.dogecoin" in css



def test_every_network_word_says_which_coin(client, monkeypatch):
    """2026-10-13: "Wherever it says testnet or mainnet it should also
    indicate pep or doge Maybe with just an uppercase P or D in the correct color"."""
    app, state = client
    _with_doge(state, monkeypatch, ready=True)
    from arcade.web.app import TEMPLATES
    badge = TEMPLATES.env.globals["net_badge"]
    # As the chain's coin now (2026-10-18: "Use the icons ... rather than text badges"), named on hover.
    assert 'src="/coin-pep-test.png" alt="PEP TEST"' in badge(False) and "Pepecoin testnet" in badge(False)
    assert 'src="/coin-pep.png" alt="PEP"' in badge(True)
    assert 'src="/coin-doge.png"' in badge("doge") and 'src="/coin-doge.png"' in badge(state.doge)
    page = app.get("/exchange?tab=tokens").text
    assert '.tag.testnet::before,.tag.mainnet::before{content:"P"' in page
    assert '.tag.dogecoin::before{content:"D"' in page
    assert 'alt="PEP TEST"></span></a>' in page and 'alt="PEP"></span></a>' in page


def test_one_naming_convention(client, monkeypatch):
    """2026-10-13: "Pep and doge should have the same naming conventions for
    mainnet": PEP Testnet, PEP Mainnet, DOGE Mainnet."""
    app, state = client
    doge = _with_doge(state, monkeypatch, ready=False)
    assert (state.messaging.title, state.ledger.title, doge.title) == ("P test", "P main", "D main"), \
        'the operator: "make it say only P test or P main or D main in all of the labels"'
    page = app.get("/wallet/tools").text
    for letter, word in (("P", "test"), ("P", "main"), ("D", "main")):
        assert f">{letter}</b> {word}" in page, (letter, word)

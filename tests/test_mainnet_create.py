"""Making things on mainnet from the Create tab, and what it costs.

2026-10-09: "enable the ability for people to create tokens NFT's and NFT
collections on main net. It should be an option on the create tab, but make sure
any main net transactions are verified by the user", with an operator's fee of
".5% same as our exchange fee".

What the node decides, and so what is tested here: an offer is on mainnet only
when the request says so (no chain is the tag chain, testnet); a mainnet creation
carries the operator's fee as an output the tab can read off the bytes, never
below the dust limit; testnet never pays it; and the chain an account picks is its
own browser's, never the node's switch. The prompt itself is the tab's
(wallet.js `mainnetOk`, base.html `arcadeMainnetReview`), checked by its source.
"""

import base64
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_account_real_coins import _fund, THEIRS_MAIN         # noqa: E402
from test_account_mainnet import TEST                            # noqa: E402
from test_me_page import _seat                                   # noqa: E402
from test_funding import _pubkey                                 # noqa: E402

from arcade.script import b58check_encode, hash160               # noqa: E402

from arcade import fees                                          # noqa: E402

OPERATOR = THEIRS_MAIN       # stands in for the node's own mainnet address


#: A mainnet address with its coin key, which a Class B payload needs.
KEY = _pubkey(int("33" * 32, 16))
MAIN = b58check_encode(56, hash160(KEY))


def _open_both(app):
    _seat(app)
    app.post("/account/address", json={"address": TEST})
    assert app.post("/account/address", json={
        "address": MAIN, "chain": "main", "coin_pubkey": KEY.hex()}).status_code == 200


def _operated(state, monkeypatch):
    real = state.home_address
    monkeypatch.setattr(state, "home_address",
                        lambda chain: OPERATOR if chain is state.ledger else real(chain))


def _inscribe(app, chain=None):
    body = {"content": base64.b64encode(b"a piece made on the chain that is money").decode(),
            "content_type": "text/plain; charset=utf-8"}
    if chain:
        body["chain"] = chain
    return app.post("/account/inscribe", json=body)


def test_a_mainnet_inscription_pays_the_operator_and_says_so(client, monkeypatch):
    app, state = client
    _open_both(app)
    _fund(app, state, MAIN)
    _operated(state, monkeypatch)
    offer = _inscribe(app, "main")
    assert offer.status_code == 200, offer.text
    said = offer.json()
    assert said["chain"] == "main"
    fee = said["operator_fee"]
    assert fee["address"] == OPERATOR and fee["permille"] == 5
    # 0.5% of a small piece is far under the dust limit, so it is the minimum.
    assert fee["value"] == fees.DUST_LIMIT and fee["at_minimum"] is True
    from arcade.txbuild import p2pkh_script
    out = fee["value"].to_bytes(8, "little") + bytes([25]) + p2pkh_script(OPERATOR)
    assert out.hex() in said["raw"], "an output the tab can read off the bytes"


def test_testnet_never_pays_it(client, monkeypatch):
    app, state = client
    _open_both(app)
    _operated(state, monkeypatch)
    said = _inscribe(app).json()                      # no chain: the tag chain
    assert said.get("chain") != "main"
    assert said.get("operator_fee") is None, said


def test_the_rate_is_a_setting(client, monkeypatch):
    app, state = client
    _open_both(app)
    _fund(app, state, MAIN)
    _operated(state, monkeypatch)
    state.set_setting("mainnet_create_fee_permille", 0)
    assert _inscribe(app, "main").json()["operator_fee"] is None, "0 switches it off"


def test_the_chain_is_this_browsers_not_the_nodes(client):
    app, state = client
    _open_both(app)
    before = state.token_chain.network
    went = app.get("/create/chain?to=main&back=/create", follow_redirects=False)
    assert went.status_code == 303 and went.headers["location"] == "/create"
    assert "arcade_chain=main" in went.headers.get("set-cookie", "")
    assert state.token_chain.network == before, "the operator's switch is untouched"
    away = app.get("/create/chain?to=main&back=//elsewhere.example", follow_redirects=False)
    assert away.headers["location"] == "/create", "only a path of ours"


def test_the_prompt_is_in_the_one_place_everything_signs():
    wallet = pathlib.Path("arcade/web/templates/wallet_js.js").read_text()
    sign = wallet.split("async function signOffer(", 1)[1].split("\n}\n", 1)[0]
    assert "await mainnetOk(offer, shown, network, options)" in sign
    base = pathlib.Path("arcade/web/templates/base.html").read_text()
    review = base.split("function arcadeMainnetReview(", 1)[1].split("\n}\n", 1)[0]
    for line in ("Network fee", "Operator fee", "Total leaving your wallet",
                 "cannot be undone"):
        assert line in review, line



def test_the_pages_follow_the_browsers_choice(client):
    """The cookie reaches the page through the request's own chain, and only
    for an account's view."""
    app, state = client
    _open_both(app)
    assert "PEP TEST: test coins" in app.get("/tokens").text
    app.cookies.set("arcade_chain", "main")
    page = app.get("/tokens").text
    assert "Real PEP. Every transaction here shows its full cost" in page
    assert state.token_chain.network != "main", "and the node's own switch is where it was"
    assert "Launchpads are testnet only for now" in app.get("/mintpad/new").text

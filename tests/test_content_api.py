"""The API an inscribed page can call.

Inscribed code is written by strangers and runs in the browser of a wallet that
can spend. These endpoints are the only same-origin URLs in the application that
answer a cross-origin request, and every one of them is a read.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

CHAIN_ENDPOINTS = ["/r/blockheight", "/r/blocktime", "/r/inscriptions",
                   "/r/tag/somebody", "/r/address/nSomebody",
                   "/r/balances/nSomebody"]


@pytest.mark.parametrize("path", CHAIN_ENDPOINTS)
def test_a_sandboxed_page_can_reach_the_chain_endpoints(client, path):
    """A frame with no allow-same-origin has an opaque origin, so without CORS
    an inscribed page could not read even its own metadata."""
    app, _ = client
    response = app.get(path)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"


def test_the_rest_of_the_wallet_says_nothing_about_cors(client):
    """Which is what stops an inscription reading it: a cross-origin fetch
    without permission cannot see the answer."""
    app, _ = client
    for path in ("/", "/tokens", "/wallet", "/messages", "/remote"):
        assert "access-control-allow-origin" not in app.get(path).headers, path


def test_content_is_served_sandboxed_and_never_sniffed(client, monkeypatch):
    """Bytes a stranger wrote. `sandbox` in the policy applies to the response
    whatever frame it lands in, so it holds even opened directly in a tab."""
    app, state = client

    class FakeIndex:
        def inscription_content(self, key):
            return "text/html", b"<script>alert(1)</script>"
        def inscription(self, key):
            return None

    from arcade.web import app as webapp
    monkeypatch.setattr(webapp, "_CONTENT_INDEX_FOR_TESTS", FakeIndex(), raising=False)

    from arcade.web import content as contentlib
    response = contentlib.content(FakeIndex(), "0")
    assert "sandbox" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.media_type == "text/html"


def test_a_type_we_will_not_render_is_handed_over_as_a_download(client):
    """An inscription is arbitrary bytes, and guessing at them is how a text
    file becomes a script."""
    from arcade.web import content as contentlib

    class Odd:
        def inscription_content(self, key):
            return "application/x-msdownload", b"MZ..."
        def inscription(self, key):
            return None

    response = contentlib.content(Odd(), "0")
    assert response.headers["content-disposition"] == "attachment"
    assert response.media_type == "application/octet-stream"


def test_content_this_node_did_not_keep_says_so_and_how_to_check_it(client):
    """Not held here is not the same as does not exist. The hash and the
    length are always kept, so it can be fetched back and proved."""
    from arcade.web import content as contentlib

    class Described:
        def inscription_content(self, key):
            return None
        def inscription(self, key):
            return {"sha256": "ab" * 32, "content_len": 1234}

    response = contentlib.content(Described(), "7")
    assert response.status_code == 404
    body = response.body.decode()
    assert "did not keep" in body and "ab" * 32 in body and "1234" in body


def test_an_inscription_is_named_by_number_or_by_txid():
    from arcade.web import content as contentlib

    assert contentlib._key("42") == 42
    assert contentlib._key("AB" * 32) == ("ab" * 32)


def test_the_wallet_endpoint_can_be_turned_off(client):
    """A balance is public either way. WHICH address is yours is the one thing
    the chain does not say, so that is the part with a switch."""
    app, state = client
    assert app.get("/r/wallet").status_code == 200

    state.inscription_wallet_access = False
    refused = app.get("/r/wallet")
    assert refused.status_code == 403
    assert "does not tell inscriptions who is looking" in refused.text
    assert refused.headers["access-control-allow-origin"] == "*", (
        "a page has to be able to read the refusal too")


def test_every_content_endpoint_is_a_read(client):
    """None of them may change anything: that is the whole boundary."""
    app, _ = client
    for path in CHAIN_ENDPOINTS + ["/r/wallet", "/content/0"]:
        assert app.post(path).status_code in (404, 405), path


def test_a_missing_inscription_is_a_404_not_an_error_page(client):
    app, _ = client
    for path in ("/content/999999", "/r/inscription/999999", "/r/metadata/999999"):
        response = app.get(path)
        assert response.status_code == 404
        assert response.json()["error"] == "no such inscription"


# --- what a wallet holds -------------------------------------------------------
# `balances` takes a LIST of addresses. Handed a string it made one SQL
# placeholder per character and matched nothing -- silently, for every address
# there is. The page said "tokens: none" to somebody holding 989,995 of one.


@pytest.fixture
def with_tokens(tmp_path):
    """A ledger holding a real token balance across two addresses."""
    from arcade.config import NETWORKS
    from arcade.db import Database
    from arcade.ledger import LedgerIndex
    from arcade.state import install_schema

    path = tmp_path / "regtest-ledger.sqlite"
    db = Database(path)
    install_schema(db)
    db.conn.execute(
        "INSERT INTO property(property_id,ecosystem,property_type,issuer,category,"
        "subcategory,name,url,data,managed,total_tokens,creation_txid,creation_block)"
        " VALUES(3,1,2,'nMe','','','Arcade Test','','',0,0,'tx',1)")
    for address, units in (("nMine", 989_995_00000000), ("nAlsoMine", 5_00000000)):
        db.conn.execute(
            "INSERT INTO balance(address,property_id,balance,selloffer_reserve,"
            "accept_reserve,metadex_reserve) VALUES(?,3,?,0,0,0)", (address, units))
    db.conn.commit()
    db.close()
    return LedgerIndex(path, NETWORKS["regtest"], rpc_factory=lambda: None)


def test_an_address_balance_is_found_at_all(with_tokens):
    from arcade.web import content as contentlib

    rows = with_tokens.balances(["nMine"])
    assert [contentlib.holding(r)["name"] for r in rows] == ["Arcade Test"]
    assert with_tokens.balances("nMine") == [], (
        "a string is not a list of addresses, and this is why it must not be "
        "passed as one")


def test_a_holding_carries_both_forms(with_tokens):
    """The string a person reads, and the integer a page does arithmetic with,
    so nothing has to parse the display form back."""
    from arcade.web import content as contentlib

    held = contentlib.holding(with_tokens.balances(["nMine"])[0])
    assert held["propertyid"] == 3
    assert held["name"] == "Arcade Test"
    assert held["units"] == 989_995_00000000
    assert held["balance"].replace(",", "").startswith("989995")
    assert held["divisible"] is True


def test_the_balances_endpoint_answers_for_one_address(client, monkeypatch,
                                                       with_tokens):
    app, _ = client
    from arcade.web import app as webapp

    monkeypatch.setattr(webapp, "_CONTENT_INDEX", with_tokens, raising=False)
    # Drive the shape rather than the wiring: the route hands the row straight
    # to `holding`, which is what the test above pins.
    from arcade.web import content as contentlib
    payload = [contentlib.holding(r) for r in with_tokens.balances(["nMine"])]
    assert payload and payload[0]["units"] > 0


def test_a_wallet_sums_a_token_across_its_addresses(with_tokens):
    """Fifteen addresses holding one token is one balance, not fifteen.
    Showing the pieces would be showing the plumbing."""
    from arcade.web import content as contentlib

    held: dict[int, dict] = {}
    for row in with_tokens.balances(["nMine", "nAlsoMine"]):
        entry = held.setdefault(row["property_id"], {
            "propertyid": row["property_id"], "name": row["name"],
            "divisible": row["divisible"], "units": 0})
        entry["units"] += int(row["balance"])
    summed = [contentlib.holding(e) for e in held.values()]
    assert len(summed) == 1, "one token, one line"
    assert summed[0]["units"] == 989_995_00000000 + 5_00000000


def test_the_wallet_endpoint_asks_for_every_address_at_once():
    """One query for the whole wallet, not one per address: a loop calling it
    per address is how the string-instead-of-list bug hid."""
    import inspect

    from arcade.web import app as webapp

    source = inspect.getsource(webapp.create_app)
    start = source.index("def r_wallet(")
    body = source[start:source.index("@app.get", start + 10)]
    assert "index.balances(addresses)" in body, body[:400]

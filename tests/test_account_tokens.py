"""What an account holds in tokens, and sending some of it.

An account's Wallet page now has a Tokens tab, the same as the operator's
own -- what this differs on is that the transaction is a genuine Omni
Simple Send (type 0), built the way `TokenSender` builds one and NOT
wrapped in AnyData: it is not an arcade payload piggybacking on the chain,
it IS the transaction the token engine reads a balance change out of.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_me_page import _seat, public, EDGE            # noqa: F401,E402

TEST = "mqxyzWHvgSMmDYPg9aWpcmXWnkouLUDbWg"        # version 111, regtest
SOMEBODY = "mpK9VHfd3ZkKZXMoWPy1kDf65RLzzbP1Bd"      # version 111, regtest

ECOSYSTEM_MAIN = 1
PROPERTY_DIVISIBLE = 2
PROPERTY_INDIVISIBLE = 1


def _token(state, *, property_id=100, name="Testcoin", divisible=True,
          issuer=TEST):
    chain = state.messaging
    index = state.token_index(chain)
    with index.open() as db:
        db.conn.execute(
            "INSERT OR REPLACE INTO property"
            "(property_id, ecosystem, property_type, issuer, name, "
            " total_tokens, creation_txid, creation_block) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (property_id, ECOSYSTEM_MAIN,
             PROPERTY_DIVISIBLE if divisible else PROPERTY_INDIVISIBLE,
             issuer, name, 1_000_000_00000000, "aa" * 32, 1))
        db.conn.commit()
    return property_id


def _balance(state, address, property_id, amount):
    chain = state.messaging
    index = state.token_index(chain)
    with index.open() as db:
        db.conn.execute(
            "INSERT OR REPLACE INTO balance(address, property_id, balance) "
            "VALUES(?,?,?)", (address, property_id, amount))
        db.conn.commit()


def test_an_account_sees_what_it_holds(client):
    app, state = client
    _seat(app)
    app.post("/account/address", json={"address": TEST})
    pid = _token(state)
    _balance(state, TEST, pid, 5_00000000)
    said = app.get("/account/tokens").json()
    tokens = [t for chain in said["chains"] for t in chain["tokens"]]
    assert len(tokens) == 1
    assert tokens[0]["property_id"] == pid
    assert tokens[0]["name"] == "Testcoin"
    assert tokens[0]["balance"] == 5_00000000


def test_it_does_not_see_a_zero_balance(client):
    app, state = client
    _seat(app)
    app.post("/account/address", json={"address": TEST})
    pid = _token(state, issuer=SOMEBODY)
    _balance(state, TEST, pid, 0)
    said = app.get("/account/tokens").json()
    tokens = [t for chain in said["chains"] for t in chain["tokens"]]
    assert tokens == []


def test_sending_more_than_you_hold_is_refused(client):
    app, state = client
    _seat(app)
    app.post("/account/address", json={"address": TEST})
    pid = _token(state)
    _balance(state, TEST, pid, 1_00000000)
    answer = app.post("/account/token/send",
                      json={"property_id": pid, "to": SOMEBODY,
                            "amount": "2"})
    assert answer.status_code == 400
    assert "only" in answer.json()["detail"]


def test_a_token_that_does_not_exist_is_refused(client):
    app, _ = client
    _seat(app)
    app.post("/account/address", json={"address": TEST})
    answer = app.post("/account/token/send",
                      json={"property_id": 99999, "to": SOMEBODY,
                            "amount": "1"})
    assert answer.status_code == 400
    assert "no such" in answer.json()["detail"].lower() \
        or "no token" in answer.json()["detail"].lower() \
        or "99999" in answer.json()["detail"]


def test_it_will_not_send_to_itself(client):
    app, state = client
    _seat(app)
    app.post("/account/address", json={"address": TEST})
    pid = _token(state)
    _balance(state, TEST, pid, 5_00000000)
    answer = app.post("/account/token/send",
                      json={"property_id": pid, "to": TEST, "amount": "1"})
    assert answer.status_code == 400
    assert "own address" in answer.json()["detail"]


def test_a_payload_that_gets_this_far_is_not_wrapped_in_anydata():
    """The one distinction that matters: a token send must produce a
    genuine Omni message, not an arcade AnyData blob, or the token engine
    would credit nobody and the coins would vanish into an opaque type-200
    output -- paid for and invisible, the same failure inscribe.py's own
    history records."""
    from arcade import payload as P
    from arcade import tokens as tokenlib

    body = tokenlib.send_payload(100, 5_00000000)
    decoded = P.decode(body)
    assert isinstance(decoded, P.SimpleSend)
    assert decoded.property_id == 100
    assert decoded.amount == 5_00000000
    # And AnyData would have decoded to something else entirely -- proving
    # the two are not interchangeable is the whole point of `wrap=False`.
    wrapped = P.AnyData(data=body).encode()
    assert P.decode(wrapped).TYPE != P.SimpleSend.TYPE


def test_the_page_is_there_and_in_the_wallet_tabs(public):
    app, _ = public
    _seat(app)
    body = app.get("/me/wallet/tokens", headers=EDGE).text
    assert "<h1>Wallet</h1>" in body
    assert 'href="/me/wallet/tokens"' in body
    assert 'href="/me/wallet"' in body and 'href="/me/nfts"' in body


def test_a_stranger_is_sent_to_sign_up(client):
    app, _ = client
    answer = app.get("/me/wallet/tokens", follow_redirects=False)
    assert answer.status_code == 303
    assert answer.headers["location"].split("?")[0] == "/join"


def test_the_tokens_page_never_renders_a_name_as_markup(client):
    page = pathlib.Path("arcade/web/templates/my_wallet_tokens.html").read_text()
    uses = [line.strip() for line in page.splitlines()
            if "innerHTML" in line and not line.strip().startswith("//")]
    assert uses == ['$("chains").innerHTML = "";'], uses


def _managed(state, property_id):
    index = state.token_index(state.messaging)
    with index.open() as db:
        db.conn.execute("UPDATE property SET managed = 1, total_tokens = 0 "
                        "WHERE property_id = ?", (property_id,))
        db.conn.commit()


def test_a_token_you_issue_is_listed_even_at_zero(client):
    """A managed token starts at nothing; its issuer has to find it to grant
    any (a tester, 2026-09-26: "stuck at zero forever")."""
    app, state = client
    _seat(app)
    app.post("/account/address", json={"address": TEST})
    pid = _token(state, property_id=101, name="Skull Points", divisible=False)
    _managed(state, pid)
    said = app.get("/account/tokens").json()
    tokens = [t for chain in said["chains"] for t in chain["tokens"]]
    mine = [t for t in tokens if t["property_id"] == pid]
    assert mine and mine[0]["issuer_is_me"] and mine[0]["balance"] == 0


def test_only_the_issuer_manages_a_token(client):
    app, state = client
    _seat(app)
    app.post("/account/address", json={"address": TEST})
    pid = _token(state, property_id=102, issuer=SOMEBODY)
    _managed(state, pid)
    for action in ("grant", "revoke", "issuer"):
        answer = app.post("/account/token/manage",
                          json={"property_id": pid, "action": action,
                                "amount": "1", "to": TEST})
        assert answer.status_code == 400, action
        assert "issuer" in answer.json()["detail"], action


def test_a_fixed_supply_cannot_be_granted_or_revoked(client):
    app, state = client
    _seat(app)
    app.post("/account/address", json={"address": TEST})
    pid = _token(state, property_id=103)
    for action in ("grant", "revoke"):
        answer = app.post("/account/token/manage",
                          json={"property_id": pid, "action": action, "amount": "1"})
        assert answer.status_code == 400
        assert "fixed supply" in answer.json()["detail"]


def test_revoking_more_than_you_hold_and_handing_to_yourself_are_refused(client):
    app, state = client
    _seat(app)
    app.post("/account/address", json={"address": TEST})
    pid = _token(state, property_id=104)
    _managed(state, pid)
    _balance(state, TEST, pid, 1_00000000)
    answer = app.post("/account/token/manage",
                      json={"property_id": pid, "action": "revoke", "amount": "2"})
    assert answer.status_code == 400 and "all it can revoke" in answer.json()["detail"]
    answer = app.post("/account/token/manage",
                      json={"property_id": pid, "action": "issuer", "to": TEST})
    assert answer.status_code == 400 and "already the issuer" in answer.json()["detail"]
    answer = app.post("/account/token/manage",
                      json={"property_id": pid, "action": "melt"})
    assert answer.status_code == 400


def test_the_issuer_sees_its_controls_on_the_token_page(client):
    app, state = client
    _seat(app)
    app.post("/account/address", json={"address": TEST})
    pid = _token(state, property_id=105, name="Skull Points")
    _managed(state, pid)
    page = app.get(f"/tokens/{pid}").text
    assert 'id="issuer-panel"' in page and 'id="grant-go"' in page
    assert "Hand over the issuer role" in page
    other = _token(state, property_id=106, issuer=SOMEBODY)
    assert 'id="issuer-panel"' not in app.get(f"/tokens/{other}").text


def test_a_token_tip_counts_only_when_the_chain_shows_the_send(client):
    """2026-09-27: "Tip a post with a token you hold, not only with
    coins, and see it counted on the post". The tip is the token send plus an
    act naming it (feed.TIP_TOKEN); the page counts it only when the token
    index has that txid as a valid send from the act's author to the post's
    author -- a claim naming somebody else's send counts for nothing."""
    from test_feed_web import a_post, an_act
    from arcade import payload as P
    from arcade.messaging import feed

    app, state = client
    pid = _token(state, property_id=101, name="Tipcoin")
    post, real, fake = "a1" * 32, "b1" * 32, "c1" * 32
    a_post(state, post, text="tip me", sender=SOMEBODY)
    index = state.token_index(state.messaging)
    with index.open() as db:
        for txid, to in ((real, SOMEBODY), (fake, TEST)):
            db.conn.execute(
                "INSERT OR REPLACE INTO arcade_tx(txid, block_height, position, "
                "encoding_class, message_type, message_version, sender, reference, "
                "payload_hex, valid) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (txid, 10, 0, "C", 0, 0, TEST, to,
                 P.SimpleSend(property_id=pid, amount=5 * 100_000_000).encode().hex(), 1))
        db.conn.commit()
    an_act(state, "d1" * 32, feed.TIP_TOKEN, post, author=TEST, text=real)
    an_act(state, "e1" * 32, feed.TIP_TOKEN, post, author=TEST, text=fake)
    page = " ".join(app.get("/feed").text.split())
    assert "&#9670; 5 Tipcoin" in page or "◆ 5 Tipcoin" in page, "the real send counts"
    assert "10 Tipcoin" not in page, "the send to somebody else does not"


def test_a_token_tip_act_carries_the_send_it_names():
    from arcade.messaging import feed
    note = feed.build(feed.TIP_TOKEN, "aa" * 32, "bb" * 32)
    act = feed.parse(note)
    assert act.kind == feed.TIP_TOKEN and act.text == "bb" * 32

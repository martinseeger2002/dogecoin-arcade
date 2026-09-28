"""An account creates a token, with a key this node never had.

The bytes are the bytes the operator's own form makes -- one `issuance_payload`
called by both, which is the point: a token issued from a browser wallet is
the same type 50 transaction, and anything else would be a second token
protocol nobody else reads. What differs is who the chain credits. An
issuance credits the owner of the transaction's first input, and this node
picks that input out of the account's own address without ever being able to
sign for it, so the answer has to be checked where it is actually decided.

Against a real regtest node, for the reason every account test before this one
gives. The part that can be wrong here is the part that looks right on paper:
an issuance the chain accepts, that pays its fee, sits in a block, and credits
nobody -- which is what happens if the payload is wrapped in AnyData on the
way, since the engine then sees an opaque type 200 and a property that was
never created. `test_tokens.py` catches that class of mistake for the node's
own wallet; nothing here would notice it without a chain to read back from.

The name-collision tests spell the same name differently on purpose. One name
is one token (D-122) and a refused issuance still costs its fee, so the
account's path has to ask the same question the operator's asks -- and it has
to ask it about the chain the request named, not whichever chain the node's
own switch happens to be sitting on.
"""

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_claim import _catch_up                            # noqa: E402
from test_account_inscribe import arcade, seated                    # noqa: E402,F401
from test_funding import _sign                                      # noqa: E402
from test_web import app_state, client                              # noqa: E402,F401

COIN = 100_000_000
SECRET = 0x5e5e5e5e0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c

#: An inscription id that exists nowhere, for the tests that are only about
#: how an id is CARRIED. The chain does not check that an icon resolves --
#: nothing on any chain can -- so what the page's preview is for is saying so
#: before the fee; here the question is only whether the payload survived.
SOME_ICON = "ab" * 32


def _ask(app, **fields):
    return app.post("/account/token/create", json=fields)


def _sign_and_send(app, pubkey, offer):
    signatures = [_sign(SECRET, bytes.fromhex(digest)).hex()
                  for digest in offer["sighashes"]]
    return app.post("/account/sign", json={
        "offer": offer["offer"], "signatures": signatures,
        "pubkey": pubkey.hex()})


def _mine(state, rpc):
    rpc.call("generate", 1)
    _catch_up(state, rpc)


def _token_for(state, txid):
    """The property this transaction created, read back off the chain."""
    index = state.token_index(state.messaging)
    found = [row for row in index.properties()
             if str(row["creation_txid"]) == txid]
    assert found, f"nothing was created by {txid}"
    return found[0], index


def test_an_account_creates_a_token_the_chain_credits_to_it(seated):
    app, state, rpc, pubkey, mine = seated
    offered = _ask(app, name="Browser Wallet Token", supply="1000",
                   kind="fixed", units="divisible")
    assert offered.status_code == 200, offered.text
    offer = offered.json()
    assert offer["what"] == "create the token Browser Wallet Token"
    assert offer["class"] == "C", "short name, no icon: one OP_RETURN"
    assert offer["raw"] and offer["signed_from"] == 0
    assert len(offer["inputs"]) == len(offer["sighashes"])

    sent = _sign_and_send(app, pubkey, offer)
    assert sent.status_code == 200, sent.text
    txid = sent.json()["txid"]
    _mine(state, rpc)

    token, index = _token_for(state, txid)
    assert token["name"] == "Browser Wallet Token"
    assert token["issuer"] == mine, (
        "the chain credits the owner of the first input, and this node was "
        "the only one who could have named it -- so an issuer that is not "
        "this account means the account paid for a token that is not its own")
    assert index.balance(mine, token["property_id"]) == 1_000 * COIN, (
        "a fixed supply arrives entirely at the issuer. If it credited "
        "nobody, the payload came wrapped and the engine saw type 200")


def test_a_managed_issuance_creates_no_supply_to_arrive_anywhere(seated):
    app, state, rpc, pubkey, mine = seated
    offered = _ask(app, name="Managed Browser Token", kind="managed",
                   units="indivisible")
    assert offered.status_code == 200, offered.text
    sent = _sign_and_send(app, pubkey, offered.json())
    assert sent.status_code == 200, sent.text
    _mine(state, rpc)
    token, index = _token_for(state, sent.json()["txid"])
    assert token["managed"], "granted later by the issuer, which is now an account"
    assert index.balance(mine, token["property_id"]) == 0


def test_an_icon_rides_inside_the_description_and_the_token_wears_it(seated):
    """The payload detail that bites late, checked where it lands.

    An Omni issuance has five strings and no sixth, so an icon has to go
    inside `data`, and a 64-character id does not fit the 76 bytes a Class C
    payload allows for the WHOLE issuance. So the same token costs a marker
    output and some sweepable dust instead of one OP_RETURN -- and the only
    proof that the id arrived intact is a token that reads its own face back.
    """
    from arcade import tokens as tokenlib

    app, state, rpc, pubkey, mine = seated
    offered = _ask(app, name="Icon Browser Token", supply="1",
                   icon=SOME_ICON)
    assert offered.status_code == 200, offered.text
    offer = offered.json()
    assert offer["class"] == "B", "an id cannot fit one OP_RETURN, name included"

    sent = _sign_and_send(app, pubkey, offer)
    assert sent.status_code == 200, sent.text
    _mine(state, rpc)
    token, _ = _token_for(state, sent.json()["txid"])
    assert tokenlib.details(token)["icon"] == SOME_ICON, (
        "the id was written somewhere other than where it is read")


def test_the_same_name_spelled_differently_is_refused_before_it_costs(seated):
    app, state, rpc, pubkey, mine = seated
    first = _sign_and_send(app, pubkey, _ask(app, name="Twice Token",
                                            supply="5").json())
    assert first.status_code == 200, first.text
    _mine(state, rpc)

    again = _ask(app, name="  TWICE   TOKEN  ", supply="900")
    assert again.status_code == 400, again.text
    assert "already token" in again.json()["detail"], again.json()
    assert rpc.call("getrawmempool") == [], "a refusal built nothing to send"


def test_a_name_in_the_mempool_is_taken_too(seated):
    """Two presses, one block. The chain keeps the first and the second is a
    fee for nothing, so the mempool counts as taken even though no index has
    seen it yet.

    This is the mempool branch and not the `pending_tokens` one, because
    nothing on the account's path appends to `pending_tokens` -- that list is
    the node's memory of what its own wallet broadcast, and this node never
    broadcast anything. So the only way the second ask can know is by reading
    the mempool, which is read live and needs no index pass.
    """
    app, state, rpc, pubkey, mine = seated
    first = _ask(app, name="Raced Token", supply="5")
    assert first.status_code == 200, first.text
    assert _sign_and_send(app, pubkey, first.json()).status_code == 200
    second = _ask(app, name="Raced Token", supply="5")
    assert second.status_code == 400, second.text
    assert "waiting for its block" in second.json()["detail"], second.json()
    assert len(rpc.call("getrawmempool")) == 1, "the second was never built to send"


def test_a_picture_from_elsewhere_is_refused(seated):
    app, *_ = seated
    answer = _ask(app, name="Linked Token", supply="5",
                  icon="https://example.com/pic.png")
    assert answer.status_code == 400, answer.text
    assert "inscription on this chain" in answer.json()["detail"]


def test_the_chain_named_by_the_request_is_the_chain_it_is_offered_on(seated):
    """The page's chain is a switch on the node, an account's is named by its
    request, and there is a third notion in `_account_chains`. Asking for a
    chain this account never registered an address on has to be a refusal:
    checking the free name on one chain and spending on the other is how
    somebody pays a fee for a token they cannot hold."""
    app, *_ = seated
    answer = _ask(app, name="Wrong Chain Token", supply="5", chain="main")
    assert answer.status_code == 400, answer.text
    assert "no mainnet address" in answer.json()["detail"], answer.json()


def test_the_issue_dial_closes_this_door(seated):
    """The operator's number, enforced where the work is done and said in the
    refusal, rather than a 403 that leaves the account guessing."""
    app, state = seated[0], seated[1]
    state.set_setting("quota:issue", 0)
    answer = _ask(app, name="Dialled Token", supply="5")
    assert answer.status_code == 400, answer.text
    assert "token issuance" in answer.json()["detail"].lower(), answer.json()


def test_an_issuance_is_counted_apart_from_inscriptions(seated):
    """Two dials, not one, and this is the only test that can tell.

    Closing the inscription dial has to leave a token issuance standing, and
    the other way round: one number for both would mean a run of a hundred
    pieces costs somebody their one go at a name, which is the reason the
    second dial exists at all. So the inscription dial is shut and both doors
    are knocked on -- the inscription refused, the token not.
    """
    app, state = seated[0], seated[1]
    state.set_setting("quota:inscribe", 1)
    first = app.post("/account/inscribe", json={
        "content": "aGk=", "content_type": "text/plain"})
    assert first.status_code == 200, first.text
    # A different file, not the same one asked twice. `7d0034e` made a repeat
    # of an offer nobody signed one gesture asked again rather than a second
    # one -- right for a dismissed confirmation, and it means asking with the
    # same bytes is no longer a way to knock on the closed door.
    twice = app.post("/account/inscribe", json={
        "content": "aGkgdGhlcmU=", "content_type": "text/plain"})
    assert twice.status_code == 400, twice.text
    assert "inscriptions" in twice.json()["detail"], twice.json()
    answer = _ask(app, name="After Inscribing Token", supply="5")
    assert answer.status_code == 200, (
        "the inscription dial spent this account's go at a token name: "
        + answer.json().get("detail", ""))


def test_the_public_page_shows_the_account_a_way_in_and_not_the_node(seated):
    """The leak this page had. `/tokens` is a public page and used to render
    the operator's issuance form on it, whose "Issued from" chooser printed
    every one of the node's funded addresses beside a coin balance -- on a
    public instance, to strangers.

    So two things are asserted: that the node's own address does not appear
    anywhere in what a stranger is served, and that what they are served
    instead is their own address, named as the issuer, because an account
    that cannot tell which address it is issuing from is guessing.

    The seat is not the operator (`seated` only joins; nobody claims the
    node), which is what makes `public` apply to a signed-in account at all.
    """
    app, state, rpc, pubkey, mine = seated
    one = rpc.call("listunspent", 1)[0]["address"]
    assert state.operator == "", "an operator is served their own page, not this one"
    # Something this node's own wallet broadcast and no index has read yet. The
    # page that shows it says "your", so on a stranger's page it is a lie as
    # well as a disclosure.
    node_tx = "cd" * 32
    state.pending_tokens.append({"txid": node_tx, "what": "inscription #7",
                                 "at": time.time(),
                                 "network": state.messaging.network})
    state.public = True
    try:
        body = app.get("/tokens").text
    finally:
        state.public = False
    assert one not in body, "the node's wallet is still being listed to strangers"
    assert node_tx not in body, "the node's own unconfirmed transactions are shown"
    assert "Issued from" not in body, "the operator's chooser is still rendered"
    assert mine in body
    assert "/account/token/create" in body
    assert 'data-token-create-ready' in body or 'tokenCreateReady' in body


def test_a_stranger_gets_the_list_and_the_reason_not_a_form(seated):
    """No form rather than a form whose next button says "sign in first"."""
    app, state = seated[0], seated[1]
    app.cookies.clear()
    state.public = True
    try:
        body = app.get("/tokens").text
    finally:
        state.public = False
    assert "/account/token/create" not in body
    assert "cannot sign on your behalf" in body


def test_an_account_the_node_has_no_address_for_is_told_that(seated):
    """The other way this page can have no form, and the one that must not
    answer it with "sign in first".

    The chain a visitor sees is the node's switch; the address belongs to a
    chain, so an account can be signed in here and still have nothing to sign
    ON. It is told so, and pointed at the page that fixes it, rather than at a
    door it already came through.
    """
    app, state, rpc, pubkey, mine = seated
    # The key names the account's own identity key, not the coin key the test
    # signs with, and it carries the chain it belongs to -- unqualified when
    # that is the chain the tag lives on, named when it is not. This node's
    # token chain is the same one its tags do live on, so both spellings are
    # cleared. What is being tested is "this account has told this node no
    # address", and that means none on any chain, not none on one.
    identity = app.get("/account").json()["pubkey"]
    state.set_setting(f"address:{identity}", "")
    for chain in state.token_chains:
        state.set_setting(f"address:{chain.network}:{identity}", "")
    state.public = True
    try:
        body = app.get("/tokens").text
    finally:
        state.public = False
    assert "/account/token/create" not in body
    assert "cannot sign on your behalf" not in body
    assert "has not told this node an address" in body
    assert "every chain it runs" in body

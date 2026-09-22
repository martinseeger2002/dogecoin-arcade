"""An account's second wallet: the same words, on the chain that is money.

The operator: "Add mainnet wallets to the accounts on Doge arcade Web."

One set of twelve words, a key on each chain at that chain's own coin type
-- m/44'/1' on testnet, m/44'/3' on mainnet -- and nothing extra for
anybody to write down. What the node holds is still only addresses: which
one belongs to which chain, so it knows what to watch and what to fund
from, and it can derive neither.

The thing worth testing here is the separation. Two chains whose addresses
differ by one version byte, two indexes, two sets of coins, and one
account -- every place that forgets which chain it is on is a place where
real coins go somewhere they cannot be got back from.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_me_page import _seat                                   # noqa: E402

LOCAL = {"host": "127.0.0.1:8420"}

#: A well-formed address on each chain, made with this repository's own
#: encoder rather than typed: an address with a bad checksum is refused
#: before any of the chain logic this file is about ever runs.
MAIN = "PiNoqXu5dWsaEhiuoWCHwsZFB1e2CJBNmD"        # version 56
TEST = "mqxyzWHvgSMmDYPg9aWpcmXWnkouLUDbWg"        # version 111, regtest


def _chains(app):
    return {row["network"]: row for row in app.get("/account").json()["chains"]}


def test_a_signed_out_browser_is_told_which_chains_there_are(client):
    """Before anybody signs up, because the browser derives a key on each
    from the same words and hands both addresses over at once."""
    app, _ = client
    said = app.get("/account").json()
    assert said["pubkey"] is None
    networks = [row["network"] for row in said["chains"]]
    assert "regtest" in networks or "test" in networks
    assert "main" in networks, "the chain that is money is one of them"
    # And exactly one of them is where tags live.
    assert sum(1 for row in said["chains"] if row["tags"]) == 1


def test_an_account_has_a_wallet_on_each_chain(client):
    app, _ = client
    pubkey = _seat(app)
    app.post("/account/address", json={"address": TEST})
    app.post("/account/address", json={"address": MAIN, "chain": "main"})
    chains = _chains(app)
    assert chains["main"]["address"] == MAIN
    assert chains["regtest"]["address"] == TEST
    assert chains["main"]["mainnet"] is True
    assert chains["regtest"]["mainnet"] is False


def test_an_address_from_the_wrong_chain_is_refused(client):
    """One version byte apart, and one of them is money."""
    app, _ = client
    _seat(app)
    wrong = app.post("/account/address", json={"address": MAIN})
    assert wrong.status_code == 400
    assert "not a testnet one" in wrong.text, wrong.text
    other = app.post("/account/address",
                     json={"address": TEST, "chain": "main"})
    assert other.status_code == 400
    # And neither was written down.
    chains = _chains(app)
    assert chains["main"]["address"] == ""
    assert chains["regtest"]["address"] == ""


def test_a_chain_this_node_does_not_run_is_refused(client):
    """Rather than quietly serving the other one, which on these routes
    would mean building a payment on the wrong chain."""
    app, _ = client
    _seat(app)
    answer = app.post("/account/address",
                      json={"address": TEST, "chain": "litecoin"})
    assert answer.status_code == 400
    assert "litecoin" in answer.text


def test_sending_on_a_chain_with_no_address_says_which(client):
    app, _ = client
    _seat(app)
    app.post("/account/address", json={"address": TEST})
    answer = app.post("/account/send",
                      json={"to": MAIN, "amount": "1", "chain": "main"})
    assert answer.status_code == 400
    assert "mainnet" in answer.json()["detail"].lower()


def test_a_mainnet_send_will_not_take_a_testnet_address(client):
    app, _ = client
    _seat(app)
    app.post("/account/address", json={"address": MAIN, "chain": "main"})
    answer = app.post("/account/send",
                      json={"to": TEST, "amount": "1", "chain": "main"})
    assert answer.status_code == 400


def test_the_coins_in_flight_on_one_chain_are_not_offered_on_the_other():
    """The list that sits in front of the index has to be per-chain too.

    The index never made this mistake, because there is one of it per
    chain. This list is one object for every account and every chain, and
    a coin remembered without saying where it lives would be offered as an
    input on a chain it does not exist on.
    """
    from arcade.web.account import Flights

    class Unsigned:
        inputs = [{"txid": "aa" * 32, "vout": 0}]
        outputs = []

    flights = Flights()
    flights.add("abcd", "11" * 32, Unsigned(), TEST, network="regtest")
    assert flights.spent_by("abcd", "regtest")
    assert not flights.spent_by("abcd", "main")
    # And asking without naming a chain still sees everything, so a caller
    # that has not been told is no worse off than before.
    assert flights.spent_by("abcd")


def test_a_tag_is_paid_on_whichever_chain_it_is_asked_about(client):
    """Tags are claimed on one chain. A mainnet payment to @somebody uses
    the other address they published with their key -- and when they have
    not published one, it says so rather than paying the wrong chain."""
    app, state = client
    _seat(app)
    app.post("/account/address", json={"address": MAIN, "chain": "main"})
    answer = app.post("/account/send",
                      json={"to": "@nobodyatall", "amount": "1",
                            "chain": "main"})
    assert answer.status_code == 400
    assert "nobody holds @nobodyatall" in answer.json()["detail"]


def test_the_browser_derives_a_different_key_on_each_chain():
    """Different coin type, different key, different address -- and the
    page must pick by the offer's own chain rather than by whatever it had
    to hand: signing with the wrong key produces a signature that verifies
    against nothing and a refusal with no clue why."""
    wallet = pathlib.Path("arcade/web/templates/wallet_js.js").read_text()
    assert "export async function everyChain" in wallet
    assert "keysOn(wallet, offer.chain" in wallet, \
        "the signer picks its key by the offer's chain"
    coins = pathlib.Path("arcade/web/templates/coins.js").read_text()
    assert "COIN_TYPE = {main: 3, test: 1, regtest: 1}" in coins

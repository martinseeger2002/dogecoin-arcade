"""Four strangers living on one node, doing things to each other.

Every other account test asks one page to do one thing and checks the answer.
This puts four people on the same node and lets them behave like people who
found each other: names claimed, keys published, letters both ways, a post
answered by somebody else, a piece handed on, coins and tokens moved. That is
the point of the file. The sealed, unaddressed design (D-155) says a message
is unreadable by the node and by everyone but its recipient, and the sentence
is only actually tested when the one who wrote it and the one who reads it are
two different browsers on one machine. Every single-user page test has the node
holding both halves, so none of them can reach it.

What is here is what an account can already do, and the list has gone the way
of the ones before it. `door.py` refuses every POST that would spend the NODE's
wallet, and what is left behind that door is the open exchange and the offers --
so two sentences are still missing from this file, and they are missing because
the routes are: nobody here places an order or answers an offer. A mintpad is a
shop whose listing picks a random item, so its door came with the shop's and has
still not been bought from here -- a gap in the testing rather than a missing
route. Inscribe a piece, run a whole collection, issue a token, buy out of
somebody else's listing and buy out of a shop all came off that list, and each
brought its half of this file with it rather than going somewhere else to live.

A swap needs two accounts, and that is a rule rather than a convenience. A
seller signs a leg alone -- one input of theirs and one output paying them --
and what finishes the trade is the buyer's signature pasted onto it, a
transaction this node could not have made and does not sign. An account cannot
buy from its own shop, so a file with one person in it could only assert the
refusal. Both halves are here, in that order.

Two conventions, said before they surprise anybody:

* Login keys come from a real phrase, because the phrase IS the account
  (`seed.login_pubkey` is what the seat register stores), so signing up here is
  signing up in a browser. The messaging identities are generated rather than
  derived from those same words at their own branch, which `test_accounts.py`
  pins down; nothing here turns on which of the two it is.
* Coin keys are fixed scalars like the ones the other account tests sign with,
  not BIP32 keys out of the phrase. `coins.js` does the real derivation and
  `test_coins_browser.py` proves it; a scalar is enough to sign a transaction
  with, and the node never sees either.

The four in `crowd` are made once for the file, because a person who has not
claimed a name cannot be written to, and a file that rebuilt its cast for every
test would have to fake that rather than show it. Anything a test needs that is
not already there, it makes for itself.
"""

import base64
import contextlib
import dataclasses
import json
import pathlib
import sys
import time
from typing import Any

import pytest
from nacl.signing import SigningKey
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_funding import _pubkey, _sign                     # noqa: E402
from test_inscription_e2e import _put                       # noqa: E402
from test_tokens_web import (RegtestContext, mine_and_index,  # noqa: E402
                             shown)

from arcade import accounts as accountslib                  # noqa: E402
from arcade import funding, inscribe, seed, utxos           # noqa: E402
from arcade import inscriptions as pieces                   # noqa: E402
from arcade import payload as protocol                      # noqa: E402
from arcade import swap as swaplib                          # noqa: E402
from arcade import tokens as tokenlib                       # noqa: E402
from arcade.messaging import api as apilib                  # noqa: E402
from arcade.messaging import envelope                       # noqa: E402
from arcade.messaging import feed as feedlib                # noqa: E402
from arcade.messaging.keys import Identity                  # noqa: E402
from arcade.messaging.scanner import Scanner                # noqa: E402
from arcade.messaging.sender import funded_address          # noqa: E402
from arcade.script import b58check_encode, hash160          # noqa: E402
from arcade.shopkeeper import Shopkeeper                    # noqa: E402
from arcade.tokens import TokenSender                       # noqa: E402
from arcade.web.app import create_app                       # noqa: E402
from arcade.web.state import AppState                       # noqa: E402
from arcade.web.watcher import BlockWatcher                 # noqa: E402

COIN = 100_000_000

#: The cast, and their coin keys. The first scalar is the one the other account
#: tests sign with; the rest are its neighbours so one grep finds all of them.
SECRETS = (0x5151515151515151515151515151515151515151515151515151515151515151,
           0x5252525252525252525252525252525252525252525252525252525252525252,
           0x5353535353535353535353535353535353535353535353535353535353535353,
           0x5454545454545454545454545454545454545454545454545454545454545454)
NAMES = ("maple", "ferns", "osier", "quill")

#: Two more people, arriving later in the file in order to be refused things.
LATE = (0x5555555555555555555555555555555555555555555555555555555555555555,
        0x5656565656565656565656565656565656565656565656565656565656565656)


@dataclasses.dataclass
class Person:
    """One account, and the only machine that can say what its keys are."""
    client: Any
    words: str
    login: str
    tag: str
    address: str
    identity: Identity
    secret: int
    pubkey: bytes

    @property
    def key(self) -> bytes:
        return self.identity.public_bytes


@pytest.fixture(scope="module")
def node(tmp_path_factory, regtest):
    """One node, both of its roles on the same regtest chain.

    One network for both roles is the shape `_account_chains` already describes:
    a set of words, one chain, and the account pages saying so once instead of
    every caller having to. The activation height is the tip when this file
    starts, which keeps these people's traffic the only arcade state in this
    node's index -- the regtest daemon is shared across the session and earlier
    files left their own transactions on it.
    """
    regtest.generate(200)                       # past coinbase maturity
    height = regtest.rpc.get_block_count() + 1
    state = AppState(
        home=tmp_path_factory.mktemp("many-accounts"),
        messaging=RegtestContext(network="regtest", role="messaging",
                                 label="Testnet", node=regtest,
                                 activation=height),
        ledger=RegtestContext(network="regtest", role="ledger",
                              label="Testnet", node=regtest, activation=height,
                              marker=regtest.rpc.call("getnewaddress")),
    )
    return regtest, state, create_app(state)


def _settle(*node, blocks=1):
    """Mine what is waiting and read it back, as the watcher thread would.

    The scan runs to the tip rather than one bounded pass: a cursor already in
    the store makes that cheap after the first call, and one pass is not the tip
    on a chain as long as the one a full suite leaves.
    """
    daemon, state = node[0], node[1]
    daemon.generate(blocks)
    BlockWatcher(state)._sync_ledgers()
    with state.messaging.rpc() as rpc, state.store() as store:
        for _ in range(50):
            if Scanner(rpc, state.messaging.params, store,
                       identity=None).scan().blocks == 0:
                break


def _offer(person, where, body):
    """Ask the node to build something this account will pay for.

    What comes back is an offer: the inputs the node picked, the digests to
    sign, and whatever it has to say out loud about the transaction -- who is
    paid, how much, what it costs. Nothing is broadcast by asking.
    """
    return person.client.post(where, json=body)


def _complete(person, offered):
    """Sign what was offered, exactly as offered, and hand it back.

    The signatures are made over the digests in the offer and nothing else,
    which is the arrangement §5 draws: the node declares the inputs, this signs
    those, and something that comes back changed is refused rather than
    broadcast.
    """
    if offered.status_code != 200:
        return offered
    offer = offered.json()
    return person.client.post("/account/sign", json={
        "offer": offer["offer"], "pubkey": person.pubkey.hex(),
        "signatures": [_sign(person.secret, bytes.fromhex(h)).hex()
                       for h in offer["sighashes"]]})


def _do(person, where, body):
    """Both halves, for the times when nothing interesting is in between."""
    return _complete(person, _offer(person, where, body))


def _arrive(*node, secret):
    """Somebody who has just found this node, and takes a seat.

    The phrase is made rather than fixed, because two accounts from the same
    words would be the same person, and four people is the file.
    """
    daemon, state, app = node
    words = seed.generate()
    client = TestClient(app)
    login = SigningKey(seed.login_key(words))
    assert login.verify_key.encode().hex() == seed.login_pubkey(words), \
        "the seat is the phrase's, here exactly as it is in a browser"
    challenge = client.get("/auth/challenge").json()
    signature = login.sign(accountslib.login_message(
        challenge["origin"], challenge["nonce"])).signature
    seated = client.post("/auth/login", json={
        "pubkey": login.verify_key.encode().hex(), "nonce": challenge["nonce"],
        "signature": signature.hex(), "join": True})
    assert seated.status_code == 200, seated.text

    pubkey = _pubkey(secret)
    address = b58check_encode(state.messaging.params.pubkeyhash_version,
                              hash160(pubkey))
    told = client.post("/account/address",
                       json={"address": address, "coin_pubkey": pubkey.hex()})
    assert told.status_code == 200, told.text
    who = Person(client=client, words=words,
                 login=login.verify_key.encode().hex(), tag="",
                 address=address, identity=Identity.generate(),
                 secret=secret, pubkey=pubkey)
    daemon.rpc.call("sendtoaddress", address, 20.0)
    _settle(*node)
    return who


def _claim(*node, who, tag):
    """A name, which is what turns an address into somebody."""
    said = _do(who, "/account/claim", {"tag": tag})
    if said.status_code == 200:
        who.tag = tag
        _settle(*node)
    return said


@pytest.fixture(scope="module")
def crowd(node):
    """Four people already living here: a seat, coins, a name, a published key."""
    cast = [_arrive(*node, secret=secret) for secret in SECRETS]
    for one, name in zip(cast, NAMES):
        assert _claim(*node, who=one, tag=name).status_code == 200
    for one in cast:
        said = _do(one, "/account/announce",
                   {"key": one.key.hex(), "tag": one.tag})
        assert said.status_code == 200, said.text
    _settle(*node)
    return cast


def _write(from_person, to_person, text):
    """Seal it here; the node is handed ciphertext and an address."""
    sealed = envelope.seal_ciphertext(
        from_person.identity, to_person.key,
        envelope.Header(type=envelope.TYPE_SINGLE), text.encode())
    return _do(from_person, "/account/write",
               {"sealed": sealed.hex(), "to": to_person.address})


def _letters(person, after=0):
    """What the node has seen since `after`, and what this one can open."""
    said = person.client.get(f"/account/messages?after={after}")
    assert said.status_code == 200, said.text
    got = said.json()
    opened, refused = [], 0
    for row in got["candidates"]:
        try:
            sender, plain, _ = envelope.open_message(
                person.identity, bytes.fromhex(row["payload"]))
        except envelope.EnvelopeError:
            refused += 1
            continue
        opened.append({"sender": sender.hex(), "text": plain.decode(),
                       "txid": row["txid"], "cursor": row["cursor"],
                       "type": row["type"]})
    return {"cursor": got["cursor"], "opened": opened, "refused": refused,
            "seen": len(got["candidates"])}


def _feed(person):
    said = person.client.get("/account/feed")
    assert said.status_code == 200, said.text
    return said.json()


def _post(person, text):
    said = _do(person, "/account/post", {"text": text})
    assert said.status_code == 200, said.text
    return said.json()["txid"]


def _balance(state, address):
    """What this account can spend, out of the node's own watched-coin index.

    Not `listunspent`: that answers about the daemon's wallet, and these four
    addresses are precisely the ones it cannot spend -- and this chain has no
    `scantxoutset` to ask the chain directly. So the question goes to §4's
    index, which is the honest place anyway: it is how the node knows enough to
    build an offer for coins it holds no key for.
    """
    index = state.token_index(state.messaging)
    with contextlib.closing(index.open()) as db:
        return utxos.balance(db, address)


def _funded(*node, who, amount: float = 5.0) -> int:
    """Give a person spendable money, from this node's own wallet.

    Two ordinary things in this file leave an account holding a lot and being
    able to spend very little, and both are the design rather than a leak. A
    listing keeps two of the seller's coins unspent until a buyer spends one,
    because spending one is the only way to cancel a listing (`paste_leg`
    refuses the leg over a coin that went away). And an announcement that
    carries a bio and a link does not fit one OP_RETURN, so it goes out as
    Class B dust outputs -- about 0.05 apiece. So a test that arrives after
    both and asks for a profile is asking with money that is committed
    elsewhere, and the honest answer from the node is the numbers, refused.
    This file's rule is that a test makes what it needs.
    """
    daemon, state = node[0], node[1]
    daemon.rpc.call("sendtoaddress", who.address, amount)
    _settle(*node)
    return _balance(state, who.address)


def _tokens(person):
    said = person.client.get("/account/tokens").json()
    return {str(token["property_id"]): token
            for chain in said["chains"] for token in chain["tokens"]}


def _pieces(person):
    said = person.client.get("/account/nfts").json()
    return [piece for chain in said["chains"] for piece in chain["pieces"]]


def _inscribed(*node, owner, name, content):
    """One real inscription on the chain, ending up owned by `owner`.

    There are two ways to have a piece in this file, and the difference is what
    the test is about. An account that means to prove it can inscribe does it
    itself, at `/account/inscribe`, and is asked to sign its own transaction --
    that is the last section. This helper is for the tests where the inscription
    is scenery: it is put on chain with the node's wallet and handed over with
    `TokenSender`, which is the transfer the interface itself makes, so nothing
    about ownership is being smuggled in through the way the piece appeared.
    """
    daemon, state = node[0], node[1]
    keeper = daemon.rpc.call("getnewaddress")
    daemon.rpc.call("sendtoaddress", keeper, 5.0)
    _settle(*node)

    plan = inscribe.plan(content, "image/png", '{"name": "%s"}' % name)
    assert plan.chunks == 1, "these tests are about ownership, not reassembly"
    piece = _put(daemon, state.messaging.params, keeper, plan.payloads)[0]
    _settle(*node, blocks=2)

    moved = protocol.AnyData(
        data=pieces.Transfer(txid=bytes.fromhex(piece)).encode()).encode()
    sender = TokenSender(daemon.rpc, state.messaging.params)
    sender.broadcast(sender.prepare(keeper, moved, owner))
    _settle(*node)

    index = state.token_index(state.messaging)
    assert index.inscription(piece)["owner"] == owner, \
        "the piece is in the account's hands before the test starts"
    return piece


# --- who is on the node -------------------------------------------------------

def test_four_people_take_a_seat_and_the_node_holds_no_key_of_theirs(node, crowd):
    """The whole arrangement in one assertion set: seats, and no keys.

    A node that could sign for any of these four is a node whose coins their
    coins are. So this is not a sentence in a template -- it is the daemon's own
    list of addresses it can spend, crossed with theirs.
    """
    daemon, state = node[0], node[1]
    for one in crowd:
        who = one.client.get("/auth/who").json()
        assert who["pubkey"] == one.login, "each sees themselves, not the last one"
        assert who["operator"] is False

    # A name is never in the seat table (`test_no_name_is_kept_here`), so the
    # question "is this person their name" has exactly one honest answer, and it
    # is the chain's. `/auth/who` answers it with an empty string on purpose.
    index = state.token_index(state.messaging)
    for one in crowd:
        assert index.address_of(one.tag) == one.address

    assert state.accounts().taken() == len(crowd)
    ours = {one.address for one in crowd}
    spendable = {entry[0] for group in daemon.rpc.call("listaddressgroupings")
                 for entry in group}
    assert not (ours & spendable), "the node's wallet holds none of these addresses"


def test_a_newcomer_cannot_take_a_name_that_is_already_claimed(node, crowd):
    """First claim wins, and the loser is told in plain words and picks again.

    The names live on the chain, so this is a rule of the chain being
    demonstrated rather than a rule of this node -- which is how the refusal has
    to read, since the same collision happens between two different nodes.
    """
    state = node[1]
    index = state.token_index(state.messaging)
    latecomer = _arrive(*node, secret=LATE[0])
    tried = _claim(*node, who=latecomer, tag=crowd[0].tag)
    assert tried.status_code == 400, tried.text
    assert index.address_of(crowd[0].tag) == crowd[0].address, \
        "a refused claim takes nothing off the person who held it"

    assert _claim(*node, who=latecomer, tag="rowan").status_code == 200
    assert index.address_of("rowan") == latecomer.address
    assert state.accounts().taken() == len(crowd) + 1


def test_nobody_can_be_written_to_before_they_publish_a_key(node, crowd):
    """A name is not somewhere to send something. The key is."""
    latecomer = _arrive(*node, secret=LATE[1])
    assert _claim(*node, who=latecomer, tag="elder").status_code == 200

    asked = crowd[0].client.get("/account/who/elder")
    assert asked.status_code == 404, asked.text
    assert "publish" in asked.json()["detail"], asked.json()

    published = _do(latecomer, "/account/announce", {"key": latecomer.key.hex()})
    assert published.status_code == 200, published.text
    _settle(*node)

    found = crowd[0].client.get("/account/who/elder").json()
    assert found["address"] == latecomer.address
    assert bytes.fromhex(found["key"]) == latecomer.key

    missing = crowd[0].client.get("/account/who/nobodyhere")
    assert missing.status_code == 404
    assert "nobody holds" in missing.json()["detail"]


# --- writing to each other ----------------------------------------------------

def test_a_letter_goes_one_way_and_the_answer_comes_back(node, crowd):
    """The case no single-user test can reach: two browsers, one node.

    Maple seals to Ferns's published key, the node carries the bytes, and all it
    can show afterwards is an address it paid and a payload it could not read.
    Ferns answers. Both halves are checked from the reader's end, because the
    sender's page is not the one that would notice a lie.
    """
    maple, ferns = crowd[0], crowd[1]

    sent = _write(maple, ferns, "the machine works on the first try")
    assert sent.status_code == 200, sent.text
    _settle(*node)

    read = _letters(ferns)
    assert any(letter["text"] == "the machine works on the first try"
               for letter in read["opened"]), \
        f"nothing of maple's arrived: {read['seen']} candidates, " \
        f"{read['refused']} of them somebody else's"
    from_maple = [letter for letter in read["opened"]
                  if letter["text"].startswith("the machine")][0]
    assert from_maple["sender"] == maple.key.hex(), \
        "the sender is inside the ciphertext, and it is the right one"

    back = _write(ferns, maple, "agreed, though the fee surprised me")
    assert back.status_code == 200, back.text
    _settle(*node)
    assert any(letter["text"] == "agreed, though the fee surprised me"
               and letter["sender"] == ferns.key.hex()
               for letter in _letters(maple)["opened"])


def test_what_is_not_addressed_to_you_stays_closed(node, crowd):
    """Three people read the same list of envelopes and two of them learn nothing.

    This is D-155's cost and its point at once: the node hands over every
    candidate since the cursor and each browser finds out by trying. So the
    check is not that Osier's list is short -- it is that his list contains
    Maple's letter to Ferns, as bytes, and that no key he owns opens it.
    """
    state = node[1]
    maple, ferns, osier = crowd[0], crowd[1], crowd[2]
    assert _write(maple, ferns,
                   "a private word about the exchange").status_code == 200
    _settle(*node)

    theirs = [letter for letter in _letters(ferns)["opened"]
              if letter["sender"] == maple.key.hex()]
    assert theirs, "there has to be a letter before there is one to refuse"
    txid = theirs[-1]["txid"]

    handed = osier.client.get("/account/messages").json()
    same = [row for row in handed["candidates"] if row["txid"] == txid]
    assert same, "the same bytes are handed to him, as they are to everybody"
    for row in same:
        with pytest.raises(envelope.EnvelopeError):
            envelope.open_message(osier.identity, bytes.fromhex(row["payload"]))

    # And the node itself, which has no identity on this chain and so opens
    # nothing at all. `identity=None` is not a convenience here, it is the whole
    # arrangement.
    with state.store() as store:
        assert store.candidates_for_others(after=0, limit=500), \
            "the envelopes are on the node, closed"


# --- the feed between them ----------------------------------------------------

def test_a_post_is_answered_liked_and_tipped_by_somebody_else(node, crowd):
    """One post, three other people, and every action paid by its own author.

    The tip is the one worth the ceremony. Everywhere else it has been tested
    with the author and the payer being the same account, so the coins came home
    and proved only that an output was built. Here they do not come home.
    """
    daemon = node[0]
    maple, ferns, osier, quill = crowd
    target = _post(maple, "the faucet is generous today")
    _settle(*node)

    paid = _do(ferns, "/account/react",
               {"txid": target, "kind": feedlib.REPLY,
                "text": "until somebody drains it"})
    assert paid.status_code == 200, paid.text
    liked = _do(quill, "/account/react", {"txid": target, "kind": feedlib.LIKE})
    assert liked.status_code == 200, liked.text
    offered = _offer(osier, "/account/react",
                     {"txid": target, "kind": feedlib.TIP, "amount": "2"})
    assert offered.status_code == 200, offered.text
    # The offer says who is being paid and how much, before anybody signs.
    assert offered.json()["paid"] == 2 * COIN
    tipped = _complete(osier, offered)
    assert tipped.status_code == 200, tipped.text
    _settle(*node)

    raw = daemon.rpc.call("getrawtransaction", tipped.json()["txid"])
    decoded = daemon.rpc.call("decoderawtransaction", raw)
    where = [out for out in decoded["vout"]
             if abs(float(out["value"]) - 2.0) < 1e-8]
    assert where, "the tip output is in the transaction"
    key = where[0]["scriptPubKey"]
    address = key.get("address") or key["addresses"][0]
    assert address == maple.address, "it pays the author, who is somebody else"

    posts = [row for row in _feed(quill)["posts"]
             if row["text"] == "the faucet is generous today"]
    assert posts, [row["text"] for row in _feed(quill)["posts"]]
    assert posts[0]["author"] == maple.address
    assert posts[0]["likes"] >= 1 and posts[0]["replies"] >= 1 \
        and posts[0]["tips"] >= 1


def test_whoever_does_the_thing_pays_for_it(node, crowd):
    """Cheap to state and easy to get wrong: both addresses are in the
    transaction either way, and only one of them is paying."""
    state = node[1]
    maple, quill = crowd[0], crowd[3]
    target = _post(maple, "a thing worth liking")
    _settle(*node)

    before = _balance(state, quill.address)
    maple_before = _balance(state, maple.address)
    liked = _do(quill, "/account/react", {"txid": target, "kind": feedlib.LIKE})
    assert liked.status_code == 200, liked.text
    _settle(*node)
    assert _balance(state, quill.address) < before, "quill paid for the like"
    assert _balance(state, maple.address) == maple_before, \
        "and maple spent nothing to be liked"


def test_a_tip_with_nowhere_to_go_is_refused(node, crowd):
    """Nothing is half-done: no address to pay means no transaction, not a fee."""
    refused = crowd[3].client.post("/account/react", json={
        "txid": "cd" * 32, "kind": feedlib.TIP, "amount": "1"})
    assert refused.status_code == 400, refused.text
    assert "nowhere to send it" in refused.json()["detail"]


# --- coins, tokens and pieces -------------------------------------------------

def test_coins_move_between_two_of_them_and_the_name_resolves(node, crowd):
    """Paying @ferns means showing the address first, and then paying that one."""
    state = node[1]
    maple, ferns = crowd[0], crowd[1]
    before = _balance(state, ferns.address)

    offered = _offer(maple, "/account/send",
                     {"to": f"@{ferns.tag}", "amount": "3"})
    assert offered.status_code == 200, offered.text
    assert offered.json()["to"] == ferns.address, "the name resolved, in the open"
    done = _complete(maple, offered)
    assert done.status_code == 200, done.text
    _settle(*node)
    assert _balance(state, ferns.address) >= before + 3 * COIN

    own = maple.client.post("/account/send",
                            json={"to": maple.address, "amount": "1"})
    assert own.status_code == 400
    assert "own address" in own.json()["detail"]


def test_a_token_hands_from_one_to_another(node, crowd):
    """A token that came from somewhere else, and then moved without help.

    The issuance is on the operator's form here, which is a choice and not a
    limit -- an account issues its own token, with its own money, two sections
    from here. What this test is for is the shape a token has when it arrives
    from outside an account and only afterwards belongs to one of them: created
    by whoever could pay for it, handed in at an address, and from that moment
    moving between these four with nobody's help. So the creation stays where
    the outside is, and everything after it is theirs.
    """
    daemon, state, made = node
    app = TestClient(made)                    # the operator, on this very node
    maple, quill = crowd[0], crowd[3]
    issuer = daemon.rpc.call("getnewaddress")
    daemon.rpc.call("sendtoaddress", issuer, 5.0)
    mine_and_index(daemon, state)

    form = dict(csrf_token=state.csrf_token, sender=issuer, name="Four Coins",
                supply="1000", kind="fixed", units="divisible")
    txid = shown(app.post("/tokens/create", data=form).text, "txid")
    app.post("/tokens/create", data={**form, "confirmed": txid},
             follow_redirects=False)
    mine_and_index(daemon, state)
    properties = state.token_index(state.messaging).properties()
    assert properties, "the creation is in the index"
    pid = str(properties[-1]["property_id"])

    send = dict(csrf_token=state.csrf_token, sender=issuer, property_id=pid)
    txid = shown(app.post("/tokens/send",
                          data={**send, "amount": "400",
                                "recipient": maple.address}).text, "txid")
    app.post("/tokens/send", data={**send, "amount": "400",
                                   "recipient": maple.address,
                                   "confirmed": txid},
             follow_redirects=False)
    mine_and_index(daemon, state)
    assert _tokens(maple)[pid]["balance"] == 400 * COIN, _tokens(maple)

    handed = _do(maple, "/account/token/send",
                 {"property_id": int(pid), "to": f"@{quill.tag}",
                  "amount": "25"})
    assert handed.status_code == 200, handed.text
    _settle(*node)
    assert _tokens(quill)[pid]["balance"] == 25 * COIN, _tokens(quill)
    assert _tokens(maple)[pid]["balance"] == 375 * COIN, _tokens(maple)


def test_a_piece_handed_on_stops_belonging_to_the_one_who_handed_it(node, crowd):
    """A transfer is the one send that cannot be undone by sending it back.

    The piece is put on the chain by this file, because inscribing is still the
    node's action. From there, every move is coins and signatures belonging to
    the two of them.
    """
    maple, ferns = crowd[0], crowd[1]
    piece = _inscribed(*node, owner=maple.address, name="First piece",
                       content=bytes(range(256)) * 2)

    mine = _pieces(maple)
    assert [p["txid"] for p in mine] == [piece], mine
    assert mine[0]["content_type"] == "image/png"

    moved = _do(maple, "/account/nft/send",
                {"piece": piece, "to": ferns.address})
    assert moved.status_code == 200, moved.text
    _settle(*node, blocks=2)

    assert _pieces(ferns) and _pieces(ferns)[0]["txid"] == piece, _pieces(ferns)
    assert _pieces(maple) == [], "it left maple's hands"
    again = _do(maple, "/account/nft/send",
                {"piece": piece, "to": maple.address})
    assert again.status_code == 400
    assert "not this account's to send" in again.json()["detail"]


def test_an_ask_is_public_and_only_the_holder_prices_it(node, crowd):
    """An ask is not an escrow: the piece stays where it is, and the number on
    it is honoured only while its owner still holds it.

    Buying is the other half of a swap, and that half is still the node's, so
    what is proved here is the listing -- that a stranger can see it, and that
    nobody but the holder can put a price on it.
    """
    state = node[1]
    maple, ferns = crowd[0], crowd[1]
    piece = _inscribed(*node, owner=maple.address, name="Priced piece",
                       content=b"arcade" * 64)

    priced = _do(maple, "/account/nft/sell", {"piece": piece, "amount": "6"})
    assert priced.status_code == 200, priced.text
    _settle(*node, blocks=2)

    ask = state.token_index(state.messaging).ask_on(piece)
    assert ask, "the price is on the open market, not in maple's page"
    assert ask["seller"] == maple.address

    too = _do(ferns, "/account/nft/sell", {"piece": piece, "amount": "1"})
    assert too.status_code == 400
    assert "only whoever holds" in too.json()["detail"]


# --- buying from each other ---------------------------------------------------

def _listed(person, piece: str, amount: str):
    """Put a piece on the shelf: the leg the node built, twice signed, filed.

    Two requests with nothing kept between them, so this helper makes both
    halves and the row has to stand on its own arithmetic. The two signatures
    are why a listing needs two of the seller's coins -- one reaches the bytes
    naming the piece, the other the payment -- and neither half of it is a
    broadcast: a listing is a signature this node holds, not a transaction it
    makes.
    """
    asked = _offer(person, "/account/list",
                   {"piece": piece, "amount": amount})
    if asked.status_code != 200:
        return asked
    leg = asked.json()
    return person.client.post("/account/list/sign", json={
        "raw": leg["raw"], "amount": amount, "pubkey": person.pubkey.hex(),
        "signatures": [_sign(person.secret, bytes.fromhex(digest),
                             funding.SINGLE_ANYONECANPAY).hex()
                       for digest in leg["sighashes"]]})


def _bought(person, listing: str):
    """Both halves of a buy, signing only the half that is the buyer's.

    Not `_do`, and the count of digests is the reason: a swap is not an offer
    this node signs afterwards. The listing's coins lead the input list already
    signed, and `signed_from` is where this account's own inputs start.
    """
    asked = _offer(person, "/account/buy", {"listing": listing})
    if asked.status_code != 200:
        return asked
    said = asked.json()
    return person.client.post("/account/buy/sign", json={
        "raw": said["raw"], "listing": listing, "pubkey": person.pubkey.hex(),
        "signatures": [_sign(person.secret, bytes.fromhex(digest)).hex()
                       for digest in said["sighashes"]]})


def test_an_account_buys_a_piece_out_of_somebody_elses_shop(node, crowd):
    """The purchase this file said it could not have, with two wallets in it.

    Maple signs a leg for a piece it still holds -- nothing is escrowed, the
    piece stays where it is until a block spends it -- and Ferns's half is built
    by the same node that holds the key of neither, then finished by pasting
    rather than by countersigning. So every assertion below needs two people to
    be true: the piece changes hands in the index, the seller nets its price to
    the satoshi and nothing else, and the row on the node's shelf is closed by
    the transaction that did it.

    One thing here surprises anybody who has only read the transfer tests: the
    finished swap spends no coin of the piece's. Owning an inscription is the
    engine's reading of a payload and a reference output and not of a coin --
    `Engine._check_leg` asks only whose name the index holds -- and what a leg
    has to pin down is the seller's FIRST input, because Class C makes that
    input's address the sender of the swap and `inscriptions.Swap` has no room
    to name a buyer.
    """
    daemon, state = node[0], node[1]
    maple, ferns = crowd[0], crowd[1]
    piece = _inscribed(*node, owner=maple.address, name="Bought piece",
                       content=b"arcade" * 48)
    priced = _listed(maple, piece, "6")
    assert priced.status_code == 200, priced.text

    row = state.listings.get(priced.json()["listed"])
    assert row["status"] == "open" and row["owner"] == maple.address
    assert int(row["price"]) == 6 * COIN, "a row prices itself off its own leg"
    index = state.token_index(state.messaging)
    assert index.inscription(piece)["owner"] == maple.address, \
        "a listing holds nothing: this is still maple's piece"

    # The piece page is where a buyer meets the listing: a button for anybody
    # else, and none for the account that signed it. Served publicly, as a
    # stranger reading this node would be, since that is who the button is for.
    def _page(person):
        was, state.public = state.public, True
        try:
            return person.client.get(f"/inscriptions/{piece}/view")
        finally:
            state.public = was
    page = _page(ferns)
    assert page.status_code == 200, page.text
    assert "Buy it for 6" in page.text and row["id"] in page.text, \
        "a listing in the book is offered on the piece's own page"
    assert "Buy it for" not in _page(maple).text, \
        "nobody is offered their own listing"

    sold = _balance(state, maple.address)
    had = _balance(state, ferns.address)
    asked = _offer(ferns, "/account/buy", {"listing": row["id"]})
    assert asked.status_code == 200, asked.text
    said = asked.json()
    assert said["seller"] == maple.address and int(said["price"]) == 6 * COIN
    assert said["what"].startswith("buy "), said["what"]
    assert said["signed_from"] == 2, \
        "the listing's two coins lead the inputs, signed by maple and nobody else"
    assert [coin["txid"] for coin in said["inputs"][:2]] == \
        [row["input"]["txid"], row["coin"]["txid"]], said["inputs"]
    assert len(said["sighashes"]) == len(said["inputs"]) - said["signed_from"], \
        "not one digest is offered for a coin this account does not hold"

    done = _bought(ferns, row["id"])
    assert done.status_code == 200, done.text
    assert done.json()["seller"] == maple.address
    _settle(*node)

    assert index.inscription(piece)["owner"] == ferns.address, "the piece moved"
    assert "Buy it for" not in _page(ferns).text, \
        "a filled listing is not offered again"
    assert piece in [p["txid"] for p in _pieces(ferns)], _pieces(ferns)
    assert piece not in [p["txid"] for p in _pieces(maple)], "and it left maple"

    raw = daemon.rpc.call("getrawtransaction", done.json()["txid"])
    decoded = daemon.rpc.call("decoderawtransaction", raw)
    pays = [out for out in decoded["vout"]
            if int(round(float(out["value"]) * COIN))
            == int(row["output"]["value"])]
    assert len(pays) == 1, "the signed payment is in there, unsigned and whole"
    key = pays[0]["scriptPubKey"]
    assert (key.get("address") or key["addresses"][0]) == maple.address

    filed = state.listings.get(row["id"])
    assert filed["status"] == "filled"
    assert filed["spent_by"] == done.json()["txid"], "the row says what closed it"
    assert daemon.rpc.call("getrawmempool") == []

    # What the seller NETS is the price, to the satoshi -- the engine's own test
    # of a coin leg (`tx.paid_to` is outputs minus inputs), and the reason the
    # leg's fee reservation is handed back rather than kept: an output fixed at
    # signing time cannot also be what a block turns out to cost.
    sold_by = _balance(state, maple.address) - sold
    assert sold_by == 6 * COIN, f"maple netted {sold_by} on a listing at 6"
    # And the buyer pays that price plus the whole of the fee, because it is the
    # one asking -- the §1d rule `swap.build` has always followed for a swap the
    # node signs, now honoured by a swap the node only pastes together.
    paid = had - _balance(state, ferns.address)
    assert 6 * COIN < paid < 6 * COIN + COIN // 10, \
        f"ferns paid {paid} for a piece listed at 6"


def test_an_account_cannot_buy_from_its_own_shop(node, crowd):
    """A listing is a promise made to a stranger, and that is the whole rule.

    [2026-09-22: "An account should not be able to buy from its own
    shop, you'll have to make a separate account to buy from the shop."] Filling
    your own listing is two transactions that move nothing while printing a
    price and a volume nobody paid on a public page -- and it would fail by
    itself anyway, because `paste_leg` refuses a buyer input that pays to the
    seller and in a self-fill every buyer input does. So the refusal goes at the
    front, at the point where the trade is only still an intention, and it uses
    the sentence `swap.countersign` has used for the operator's wallet since
    before there were accounts.
    """
    daemon, state = node[0], node[1]
    maple = crowd[0]
    piece = _inscribed(*node, owner=maple.address, name="Shop piece",
                       content=b"arcade" * 32)
    priced = _listed(maple, piece, "6")
    assert priced.status_code == 200, priced.text
    row = state.listings.get(priced.json()["listed"])

    shown = maple.client.post("/account/buy", json={"listing": row["id"]})
    assert shown.status_code == 400, shown.text
    assert shown.json()["detail"] == "a wallet cannot fill its own order"

    # The other half takes the same route through the row, so a signature posted
    # straight at it meets the same answer instead of a building error.
    signed = maple.client.post("/account/buy/sign", json={"listing": row["id"]})
    assert signed.status_code == 400, signed.text
    assert signed.json()["detail"] == "a wallet cannot fill its own order"

    assert daemon.rpc.call("getrawmempool") == [], "a refusal spends nothing"
    assert state.listings.get(row["id"])["status"] == "open", \
        "and costs the listing nothing: a stranger can still buy it"
    assert state.token_index(state.messaging).inscription(piece)["owner"] \
        == maple.address


# --- buying from a shop, which is a different animal --------------------------

def _a_shop_on_the_node(*node, name: str, price: str) -> tuple[str, str, str]:
    """A shop on the chain, whose seller is this node's own wallet.

    Two inscriptions from one fresh address in the daemon's wallet: the thing
    for sale, and the page whose JSON sells it. The shop has to belong to a
    wallet rather than to an account because of a rule in `swap.make_offer` --
    a shop answers only when its `creator` still holds it and the wallet
    answering holds that address. That rule is why the shopkeeper can be trusted
    with nobody at the keyboard, so an account-run shop is not a thing this
    harness can build; the test after this one reaches the same refusal from the
    other side, by handing an account a shop it happens to own.

    The `node` in the shop's JSON is this node's own messaging key, which is what
    makes what follows a round trip instead of a loopback: the order leaves
    sealed under maple's key, and is opened on the same machine by the one
    program allowed to answer it -- a program that can spend maple's coins and
    cannot see maple's key.
    """
    daemon, state = node[0], node[1]
    keeper = daemon.rpc.call("getnewaddress")
    daemon.rpc.call("sendtoaddress", keeper, 12.0)
    _settle(*node, blocks=2)

    stock = inscribe.plan(b"arcade" * 40, "image/png", '{"name": "%s stock"}' % name)
    assert stock.chunks == 1, "one transaction, so the shop below has one to look at"
    piece = _put(daemon, state.messaging.params, keeper, stock.payloads)[0]
    _settle(*node, blocks=2)

    terms = json.dumps({"name": name,
                        "shop": {"node": state.ensure_identity().public_bytes.hex(),
                                 "listings": [{"give": {"inscription": piece},
                                               "take": {"coins": price}}]}},
                       separators=(",", ":"))
    # A shop is a page that carries its terms in the JSON field, not a JSON
    # inscription -- an empty page is nothing to inscribe. This is the shape
    # `mintpad.page` and `tokenpad.page` make, with their template left out.
    page = inscribe.plan(("<html><body>%s</body></html>" % name).encode("utf-8"),
                         "text/html", terms)
    assert page.chunks == 1, page.estimate.describe()
    shop = _put(daemon, state.messaging.params, keeper, page.payloads)[0]
    _settle(*node, blocks=2)

    index = state.token_index(state.messaging)
    assert index.inscription(piece)["owner"] == keeper
    assert swaplib.shop_of(index.inscription(shop))["listings"][0]["give"] \
        == {"inscription": piece}, "the shop says what it sells, on the chain"
    assert index.inscription(shop)["creator"] == keeper == \
        index.inscription(shop)["owner"], "make_offer insists on both halves of that"

    # The answer to an order is paid for out of this node's own wallet, and the
    # coins it picks there decide whether anyone can read what it sent. `generate`
    # mines to the wallet as pay-to-pubkey, and `tx.extract` refuses a pubkey
    # input outright (rules.cpp:416-430), so a message funded from a coinbase is
    # invisible to every scanner -- including the one on the machine that made it
    # -- and an order at this shop would sit unanswered as far as maple can see.
    # `funded_address` wants one address that can pay and takes the pile holding
    # the most, which after 200 mined blocks is the mining address and its
    # coinbase; `_select_inputs` then takes the smallest of that address's
    # outputs that still covers the target. Ordinary sends are what produce such
    # an output, and one per answer a buy costs -- the change of the first may
    # land elsewhere, so the second is not left hunting.
    answerer = funded_address(daemon.rpc, prefer=state.derived_address)
    for _ in range(2):
        daemon.rpc.call("sendtoaddress", answerer, 3.0)
        _settle(*node, blocks=2)
    assert funded_address(daemon.rpc, prefer=state.derived_address) == answerer, \
        "the address that answers has to stay the one that can pay"

    # A shopkeeper's first pass on a chain only parks its cursor: what was in the
    # inbox before there was a shopkeeper was not an order to it. Without this,
    # the first shop buy in the file is answered by the second one.
    assert Shopkeeper(state).tick() == 0
    return shop, piece, keeper


def _opened(*node):
    """Mine, and let the node open the machine messages that were for it.

    The one place in this file that hands a Scanner an identity. Everything else
    here goes between two accounts, and the node carries those sealed and cannot
    read them, which is the arrangement D-155 says must stay true. An order at a
    shop is the exception: it is addressed to a program, and the program has to
    open it to act on it.
    """
    daemon, state = node[0], node[1]
    _settle(*node)
    with state.messaging.rpc() as rpc, state.store() as store:
        scanner = Scanner(rpc, state.messaging.params, store,
                          identity=state.ensure_identity())
        scanner.scan()
        # scan() opens what it fetched and nothing else: the opening hangs off
        # its tail, and the tail is never reached once the cursor is already at
        # the tip. `_settle` gets there first, scanning as it mines with no
        # identity to open with, so the candidate is filed and left shut. The
        # watcher does not have this problem -- its mempool pass opens each
        # message the block before it confirms -- so ask here, by name.
        scanner.open_pending()


def _answered(*node) -> int:
    """The shop's turn: read what arrived, answer it, land the answers."""
    _opened(*node)
    answered = Shopkeeper(node[1]).tick()
    _opened(*node)                       # so the buyer can read the reply back
    return answered


def _answers(person, after: int = 0) -> list[dict]:
    """What the shop said, opened by the only key that can.

    Read off `/account/messages` rather than out of a node-side inbox because
    that is the whole arrangement: the answer comes back sealed to the account's
    own messaging key, which the node that relayed it cannot open.

    What will not open is passed over, exactly as `_letters` does it. This
    account's OWN orders are on the same list, sealed to a shop rather than to
    whoever wrote them, so a reader that could open every row here would be a
    reader that opens strangers' trades. Which is why counting what comes back
    says something: one row is the order going out and one is the answer
    coming in, and only one of the two opens.
    """
    said = person.client.get(f"/account/messages?after={after}")
    assert said.status_code == 200, said.text
    out = []
    for row in said.json()["candidates"]:
        if int(row["type"]) != envelope.TYPE_API:
            continue
        try:
            sender, plain, _ = envelope.open_message(
                person.identity, bytes.fromhex(row["payload"]))
        except envelope.EnvelopeError:
            continue
        version, fingerprint, body = apilib.read_stamp(plain)
        assert version == apilib.PROTOCOL
        assert fingerprint == apilib.api_fingerprint()
        out.append(json.loads(body.decode()))
    return out


def _seal_for_program(person, key: bytes, body: dict) -> str:
    """The ciphertext half of an API message, which is all the node is given.

    `apilib.seal` is not what goes here: it returns a finished payload, header
    included, and the route writes the header itself -- the same split `_write`
    above uses for a private message, and for the same reason. A second header
    in front of the node's is the difference between an order and a transaction
    the node scans past without a word, which is the bug this helper exists to
    keep out of the file. The stamp travels inside the encryption, so the page
    that checks it is the one that has to put it there.
    """
    return envelope.seal_ciphertext(
        person.identity, key, envelope.Header(type=envelope.TYPE_API),
        apilib.stamp() + json.dumps(body).encode()).hex()


def test_an_account_buys_from_a_shop_its_own_node_runs(node, crowd):
    """A shop buy with the account's key at one end and a wallet at the other.

    The whole conversation, four times over what the operator's wallet spends:
    the order, the offer it gets back, the signed half, and the message carrying
    it. Two of those are transactions this node built and broadcast on maple's
    behalf without ever holding the key that paid for them, and the trade itself
    is broadcast by nobody until the shop signs it -- which is the arrangement
    `/swap/{txid}` has always had, now reachable from an account.

    What makes this worth a test rather than a route is that the shop is not
    another node. It is this one. The order leaves from an account's key, goes
    out through the machine that will answer it, and comes back sealed to a key
    that machine cannot open; if any part of that were quietly done by the node
    on the account's behalf, the round trip would still appear to work and only
    the key would be missing from the design.
    """
    daemon, state = node[0], node[1]
    maple = crowd[0]
    # A shop buy pays for three transactions on top of its price -- the order
    # going out, the trade the shop broadcasts, and the message that carries this
    # account's signature onto a trade it cannot broadcast itself -- and all three
    # come out of the buyer's coins. The middle one spends coins that stay held
    # as committed until the shop lands them, so the last message has to be paid
    # for out of a coin the trade did not touch. Hence two piles: one big enough
    # to buy with, and one it has no reason to reach for.
    _funded(*node, who=maple, amount=5.0)
    _funded(*node, who=maple, amount=0.5)
    shop, piece, keeper = _a_shop_on_the_node(*node, name="Node shop", price="2")
    key = state.ensure_identity().public_bytes
    index = state.token_index(state.messaging)
    seen = maple.client.get("/account/messages").json()["newest"]

    # 1. The shop, read off the chain rather than from anything the page said.
    front = maple.client.post("/account/shop", json={"shop": shop, "op": "shop"})
    assert front.status_code == 200, front.text
    said = front.json()
    assert said["seller"] == keeper and said["buyer"] == maple.address
    assert said["node"] == key.hex(), "the shop answers to this node's own key"
    assert said["mine"] is False and said["open"] is True
    assert said["can_buy"] is True, "maple has the two outputs a buy costs (D-051)"
    assert said["ready"] is True and said["from"] == 0
    assert said["listings"][0]["give"]["txid"] == piece
    assert int(said["listings"][0]["take"]["sats"]) == 2 * COIN
    assert len(said["stamp"]) == 16, "the browser checks this before it seals"

    # 2. The order, written by this node so both doors write the same one.
    asked = maple.client.post("/account/shop",
                              json={"shop": shop, "op": "offer", "listing": 0})
    assert asked.status_code == 200, asked.text
    order = asked.json()
    assert order["order"] == {"swap": "offer", "swapv": swaplib.PROTOCOL,
                              "shop": shop, "listing": 0,
                              "buyer": maple.address}, order
    assert order["seal_to"] == key.hex() and order["to"] == keeper
    assert "2 coins" in order["buying"], order["buying"]

    # 3. Sealed here, carried by a transaction maple signs and this node sends.
    sent = _do(maple, "/account/shop",
               {"shop": shop, "op": "send",
                "sealed": _seal_for_program(maple, key, order["order"])})
    assert sent.status_code == 200, sent.text
    assert daemon.rpc.call("getrawmempool"), "the order went out"

    assert _answered(*node) == 1, "one order in, one answer out"
    heard = _answers(maple, seen)
    assert len(heard) == 1, heard
    back = heard[0]
    assert back["swap"] == "offer" and back["ok"] is True, back
    offer = back["offer"]
    assert offer["shop"] == shop and offer["listing"] == 0
    assert offer["seller"] == keeper and offer["buyer"] == maple.address
    assert offer["give"]["txid"] == piece
    assert int(offer["take"]["sats"]) == 2 * COIN
    assert "cut" not in offer, "this node announced no cut, and asks none (§1d)"

    # 4. The buyer's half, with the shop's coin sitting in front of it unsigned.
    had = _balance(state, maple.address)
    shown = maple.client.post("/account/shop",
                              json={"shop": shop, "op": "accept", "offer": offer})
    assert shown.status_code == 200, shown.text
    half = shown.json()
    assert half["signed_from"] == 1, \
        "the offered output leads the inputs, and this account signs none before it"
    assert [half["inputs"][0]["txid"], half["inputs"][0]["vout"]] == \
        [offer["outpoint"]["txid"], offer["outpoint"]["vout"]], half["inputs"]
    assert len(half["sighashes"]) == len(half["inputs"]) - 1, \
        "not one digest is offered for a coin that is not maple's"
    assert half["what"].startswith("buy "), half["what"]
    assert half["cut"] == {}

    done = maple.client.post("/account/shop/sign", json={
        "raw": half["raw"], "shop": shop, "offer": offer,
        "pubkey": maple.pubkey.hex(),
        "signatures": [_sign(maple.secret, bytes.fromhex(d)).hex()
                       for d in half["sighashes"]]})
    assert done.status_code == 200, done.text
    signed = done.json()
    assert signed["order"] == {"swap": "sign", "swapv": swaplib.PROTOCOL,
                               "offer": offer["id"], "hex": signed["hex"]}, signed
    assert signed["seal_to"] == key.hex() and signed["to"] == keeper
    assert daemon.rpc.call("getrawmempool") == [], \
        "a shop buy is finished by the shop: signing it broadcast nothing"

    # 5. The half goes to the shop the same way the order did, and the shop
    #    signs exactly what it offered and broadcasts the trade itself.
    sent = _do(maple, "/account/shop",
               {"shop": shop, "op": "send",
                "sealed": _seal_for_program(maple, key, signed["order"])})
    assert sent.status_code == 200, sent.text
    assert _answered(*node) == 1
    answer = _answers(maple, seen)[-1]
    assert answer["swap"] == "sign" and answer["ok"] is True, answer

    assert index.inscription(piece)["owner"] == maple.address, "the piece moved"
    assert piece in [p["txid"] for p in _pieces(maple)], _pieces(maple)

    # The seller is made whole to the satoshi -- its own output back, plus the
    # price -- which is the arithmetic `countersign` refuses to sign without.
    decoded = daemon.rpc.call("decoderawtransaction",
                              daemon.rpc.call("getrawtransaction", answer["txid"]))
    pays = [out for out in decoded["vout"]
            if int(round(float(out["value"]) * COIN))
            == int(offer["outpoint"]["value"]) + 2 * COIN]
    assert len(pays) == 1, decoded["vout"]
    where = pays[0]["scriptPubKey"]
    assert (where.get("address") or where["addresses"][0]) == keeper

    # And maple paid for the whole conversation, not just the trade: the price,
    # the dust each of its two messages leaves behind (0.01 to the shop, plus
    # what carries the bytes), and the fee on three transactions -- including
    # the trade's own, which the shop broadcasts but maple's coins pay for
    # (§1d's rule, seen from the side that pays it). The shop's coin stays out
    # of maple's reaching the whole time, which is what `note_committed` is for:
    # without it the message that carries the swap would pay for itself by
    # spending the swap.
    paid = had - _balance(state, maple.address)
    assert 2 * COIN < paid < 2 * COIN + (2 * COIN) // 5, \
        f"maple paid {paid} for a piece priced at 2"
    assert daemon.rpc.call("getrawmempool") == []


def test_a_shop_an_account_owns_sells_nothing_to_it(node, crowd):
    """The self-sale refusal, on the shop side, where nothing else would catch it.

    [2026-09-22: "An account should not be able to buy from its own
    shop."] `test_an_account_cannot_buy_from_its_own_shop` covers the listing
    half, where `paste_leg` would have caught it anyway. Here nothing else
    would: `check_offer` and `swap.countersign` both ask whether the seller sits
    in the buyer's wallet, and an account's address is in no wallet at all --
    which is the whole design. So the check is in the route, and this is the test
    that says it is not decoration: a shop is handed to an account, and the
    account is then refused its own shelf, having spent nothing.
    """
    daemon, state = node[0], node[1]
    maple = crowd[0]
    shop, piece, keeper = _a_shop_on_the_node(*node, name="Maple's shop", price="3")
    moved = protocol.AnyData(
        data=pieces.Transfer(txid=bytes.fromhex(shop)).encode()).encode()
    sender = TokenSender(daemon.rpc, state.messaging.params)
    sender.broadcast(sender.prepare(keeper, moved, maple.address))
    _settle(*node)
    index = state.token_index(state.messaging)
    assert index.inscription(shop)["owner"] == maple.address

    # Reading it stays allowed. It is public, and `mine` is how a page knows to
    # hide the buy button rather than the shop.
    front = maple.client.post("/account/shop", json={"shop": shop, "op": "shop"})
    assert front.status_code == 200, front.text
    assert front.json()["mine"] is True
    assert front.json()["seller"] == maple.address

    for body, what in (({"op": "offer", "listing": 0}, "an order"),
                       ({"op": "send", "sealed": "00" * 40}, "a message")):
        refused = maple.client.post("/account/shop", json={"shop": shop, **body})
        assert refused.status_code == 400, f"{what}: {refused.text}"
        assert refused.json()["detail"] == "this is your own shop", what

    # The trade half is refused one check earlier, and which one is worth
    # writing down: an offer that names this shop's owner as its seller is
    # refused by `check_offer` for naming a seller this account holds the key to,
    # and one that names anybody else never gets to be called the shop's. So the
    # refusal in `_not_your_own_shop` is for the case where the shop's own terms
    # are the honest ones -- and there is no road from an account to its own
    # shelf either way, which is the thing that had to be true.
    forged = {"id": "0" * 16, "network": "regtest", "shop": shop, "listing": 0,
              "seller": keeper, "buyer": maple.address,
              "give": {"kind": "inscription", "txid": piece},
              "take": {"kind": "coins", "amount": "3", "sats": 3 * COIN},
              "outpoint": {"txid": "1" * 64, "vout": 0, "value": COIN},
              "created": time.time(), "expires": time.time() + 60}
    refused = maple.client.post("/account/shop",
                                json={"shop": shop, "op": "accept", "offer": forged})
    assert refused.status_code == 400, refused.text
    assert refused.json()["detail"] == ("the offer is not from the wallet that "
                                        "holds this shop")

    assert daemon.rpc.call("getrawmempool") == [], "a refusal spends nothing"
    assert index.inscription(piece)["owner"] == keeper, "and sells nothing"


# --- what they say about themselves -------------------------------------------

def _published(person, **fields):
    """Say something about yourself, sign it, and let the node put it on chain.

    The name is not one of the fields, on purpose: the node reads it off the
    chain, so publishing a new face cannot publish a name the account does not
    hold. What the request leaves out is filled from what is already published,
    which the second test below is about.
    """
    return _do(person, "/account/profile", {"key": person.key.hex(), **fields})


def _said(state, person):
    """What the chain says this account published about itself.

    Read out of the announcement rows rather than from anything the node kept
    beside them: a profile is published or it is nothing, and the point of
    putting it on the chain is that a node which has never spoken to this
    account can draw it.
    """
    with state.store() as store:
        row = store.key_for(person.address)
    assert row is not None, "an account that announced has a row"
    return {"bio": row["bio"] or "", "url": row["url"] or "",
            "pfp": row["pfp"] or "", "tag": row["tag"] or ""}


def test_an_account_publishes_its_own_profile(node, crowd):
    """A face, a line and a link, published by the person who owns them.

    The operator's `/profile/picture` and `/profile/about` write into this
    installation's settings and announce from the node's own key. An account
    has no settings on this machine and no key in this node, so its three
    fields travel in the request and go into the same announcement the
    operator's wallet makes -- signed in the browser, broadcast here, and kept
    afterwards as what THEY said rather than as a fact about anybody.

    One assertion below is not about the announcement at all, and matters more
    than it looks: the link is on the page as words, not as an anchor. A
    stranger's clickable link inside a wallet is the one-tap delivery path for
    "claim your airdrop" onto a lookalike arcade, which is the attack
    docs/multi-user.md is written against. Copying it costs a paste; clicking
    it would not.
    """
    daemon, state = node[0], node[1]
    maple = crowd[0]
    _funded(*node, who=maple)
    piece = _inscribed(*node, owner=maple.address, name="Maple face",
                       content=b"arcade" * 40)

    said = _published(maple, pfp=piece, bio="makes things out of chain",
                      url="https://maple-of-yours.example")
    assert said.status_code == 200, said.text
    assert said.json()["what"] == "publish your profile", said.json()
    _settle(*node)

    assert _said(state, maple) == {
        "bio": "makes things out of chain",
        "url": "https://maple-of-yours.example",
        "pfp": piece, "tag": "maple"}

    page = " ".join(maple.client.get("/u/maple").text.split())
    assert "makes things out of chain" in page, "the line they wrote, on the page"
    assert "https://maple-of-yours.example" in page, "shown in full, to be read"
    assert 'href="https://maple-of-yours.example' not in page, \
        "as words. Not as a link somebody taps"
    assert f"/content/{piece}" in page, "and their face"

    # A face is not only on the profile page: it is what every post of theirs
    # draws beside the name, which is the reason it is published at all.
    _post(maple, "posted after the face went up")
    _settle(*node)
    feed = maple.client.get("/feed").text
    assert f'src="/content/{piece}"' in feed, \
        "somebody else's feed draws their posts without their face"


def test_changing_a_face_publishes_the_same_name_again(node, crowd):
    """Changing a face is not changing a name, and it is not erasing a bio.

    Two things the operator's page gets for free and this one has to be
    reminded of. The name travels inside the announcement, so it comes from the
    chain and not from the request -- an announcement naming a name its signer
    does not hold is a lie signed by the wrong person. And one announcement
    replaces a whole profile instead of adding to the last one, so a field the
    request never mentioned is filled from what stands published; otherwise a
    new picture would quietly take a bio down with it, for a fee.
    """
    daemon, state = node[0], node[1]
    maple = crowd[0]
    face = _inscribed(*node, owner=maple.address, name="First face",
                      content=b"arcade" * 40)
    print(f"MAPLE-INDEX {_balance(state, maple.address) / COIN}")
    first = _published(maple, pfp=face, bio="makes things out of chain",
                       url="https://maple-of-yours.example")
    assert first.status_code == 200, first.text
    _settle(*node)
    before = _said(state, maple)
    assert before["pfp"] and before["bio"], \
        "this test is about what a second announcement leaves alone"

    # The same three boxes the page shows, filled the same way it fills them:
    # from `GET /account`, which carries what is already published.
    shown = maple.client.get("/account").json()["profile"]
    assert shown == {"pfp": before["pfp"], "bio": before["bio"],
                    "url": before["url"]}, \
        f"the page would start from: {shown}, not from: {before}"

    other = _inscribed(*node, owner=maple.address, name="Second face",
                       content=b"arcade" * 36)
    again = _published(maple, pfp=other)
    assert again.status_code == 200, again.text
    _settle(*node)

    now = _said(state, maple)
    assert now["pfp"] == other, "the new picture is the one published"
    assert now["tag"] == before["tag"] == "maple", "the name did not move"
    assert now["bio"] == before["bio"] and now["url"] == before["url"], \
        f"a field nobody sent took itself down: {now}"


def test_a_face_has_to_be_a_piece_you_hold(node, crowd):
    """Pointing your name at somebody else's property is refused, not ignored.

    The draw already refuses it -- `_face_for` shows a picture only while the
    chain says the address still holds the piece -- which is exactly why the
    build refuses too. A silent drop here would mean an account paid a fee for
    an announcement that shows nothing anywhere, forever.
    """
    daemon, state = node[0], node[1]
    maple, ferns = crowd[0], crowd[1]
    piece = _inscribed(*node, owner=ferns.address, name="Ferns own piece",
                       content=b"arcade" * 24)
    before = _said(state, maple)

    tried = maple.client.post("/account/profile",
                              json={"key": maple.key.hex(), "pfp": piece})
    assert tried.status_code == 400, tried.text
    assert "not yours to wear" in tried.json()["detail"]

    nonsense = maple.client.post("/account/profile", json={
        "key": maple.key.hex(), "pfp": "a nice photo of myself"})
    assert nonsense.status_code == 400, nonsense.text
    assert "inscription" in nonsense.json()["detail"], nonsense.json()

    assert daemon.rpc.call("getrawmempool") == [], "a refusal builds nothing"
    assert _said(state, maple) == before


def test_what_a_bio_and_a_link_may_say_is_refused_before_anything_is_built(node,
                                                                          crowd):
    """Refused rather than trimmed, in the node's own words, before a fee.

    The same ceilings the operator's form has -- 160 characters of bio, a link
    that starts with a scheme -- because they are the ceilings of the format,
    not of one page. An account reaches them through a JSON body instead of a
    form, which is the one place a browser could otherwise quietly send a
    length the announcement refuses to carry.
    """
    daemon, state = node[0], node[1]
    maple = crowd[0]
    before = _said(state, maple)

    longwinded = maple.client.post("/account/profile",
                                   json={"key": maple.key.hex(),
                                         "bio": "x" * 200})
    assert longwinded.status_code == 400, longwinded.text
    assert "a bio is at most 160 characters" in longwinded.json()["detail"]

    clickable = maple.client.post("/account/profile",
                                  json={"key": maple.key.hex(),
                                        "url": "javascript:alert(1)"})
    assert clickable.status_code == 400, clickable.text
    assert clickable.json()["detail"].startswith("a link starts with https://"), \
        clickable.json()

    assert daemon.rpc.call("getrawmempool") == []
    assert _said(state, maple) == before, "and the refusal cost nothing"


# --- what each of them makes --------------------------------------------------

#: Pictures, from `tests/media`: a hundred unique pieces from a real generator
#: run and the four faces the arcade's own tokens wear. See the README beside
#: them for why they are files rather than something this file draws.
MEDIA = pathlib.Path(__file__).resolve().parent / "media"
GOOFBALL = MEDIA / "goofball"
FACES = ("arcadecoin", "cabinet", "joypad", "shibacoin")


def _build(person):
    """Hand the whole collection to the node, as a chosen folder is uploaded.

    The names arrive flattened, because that is what a browser sends: `1.png`
    and `1.json` in one heap with `_metadata.json` on top of it, which
    `collections.find_build` is written to make sense of. Nothing is inscribed
    by this; it is a write-down, and the first transaction comes later, one
    piece at a time.
    """
    files = [("files", (one.name, one.read_bytes()))
             for one in sorted(GOOFBALL.rglob("*")) if one.is_file()]
    return person.client.post("/account/run/start", files=files,
                              data={"run_chain": "regtest"})


def _land(*node, person, asked):
    """Sign the piece that was just offered, and mine the block that lands it.

    The block is not a courtesy to the assertion that follows it. The piece
    after the first spends the previous one's change, so a run goes out at the
    pace of blocks whether anybody is watching or not, and this is that pace.
    What gets signed is the bytes that were just looked at, rather than a
    second build of the same piece -- the arrangement is the one the whole file
    signs by.
    """
    done = _complete(person, asked)
    assert done.status_code == 200, done.text
    _settle(*node)
    return done.json()["txid"]


def test_a_hundred_piece_collection_is_a_hundred_pieces_for_each_of_them(node,
                                                                        crowd):
    """Four runs of one generated collection, and nobody owing another's pieces.

    The artwork is a real HashLips Art Engine run -- a hundred unique files with
    their dna and their traits -- because the sentence worth proving here is that
    an account ran a COLLECTION, and the build the other collection tests use is
    one placeholder with a different byte in it a hundred times, which proves
    the plumbing and nothing about the pieces. Each of the four uploads the same
    folder and gets their own run out of it.

    Two pieces each, out of a hundred. That is the honest size: four accounts
    running a hundred pieces is four hundred transactions and four hundred
    blocks, which is not a test but a suite that never finishes. So what is
    asserted about the other ninety-eight is what the node actually knows about
    them -- that they belong to this account's run, in this account's book, and
    that the run is still waiting for them.
    """
    daemon, state = node[0], node[1]
    index = state.token_index(state.messaging)
    runs, landed = {}, {}

    for person in crowd:
        _funded(*node, who=person, amount=8.0)
        started = _build(person)
        assert started.status_code == 200, started.text
        book = started.json()
        assert book["items"] == 100 and book["name"] == "Goofball", book
        assert book["next"] == "Goofball #1", book
        runs[person.tag] = book["run"]
        assert daemon.rpc.call("getrawmempool") == [], \
            "writing a collection down costs nothing"

    for person in crowd:
        landed[person.tag] = []
        for edition in (1, 2):
            asked = _offer(person, "/account/run/piece",
                           {"run": runs[person.tag]})
            assert asked.json()["what"] == f"inscribe Goofball #{edition}", \
                asked.json()
            landed[person.tag].append(_land(*node, person=person, asked=asked))

    for person in crowd:
        for txid, edition in zip(landed[person.tag], (1, 2)):
            row = index.inscription(txid)
            assert row is not None, txid
            assert row["collection"] == "Goofball" and row["edition"] == edition
            assert row["creator"] == person.address == row["owner"], row
            on_chain = index.inscription_content(txid)[1]
            picture = (GOOFBALL / "images" / f"{edition}.png").read_bytes()
            assert on_chain == picture, \
                "what went up is the file, byte for byte"

    # Two pieces of one account's run are two DIFFERENT pictures. This is the
    # whole reason the artwork is here: an edition number in a name is not a
    # collection, and a build that repeats one square a hundred times would
    # pass every assertion above except this one.
    first, second = (index.inscription(txid)["sha256"]
                     for txid in landed[crowd[0].tag])
    assert first != second, "a hundred pieces, one picture"

    for person in crowd:
        book = person.client.post("/account/run").json()["runs"]
        assert len(book) == 1, book
        mine = book[0]
        assert mine["items"] == 100 and mine["sent"] == 2, mine
        assert mine["next"] == "Goofball #3" and mine["status"] != "done", mine
        # A run is a thing one account is doing. Nobody else's book has it, and
        # nobody else holds its pieces.
        held = {p["txid"] for p in _pieces(person)}
        assert set(landed[person.tag]) <= held, held
        for other in crowd:
            if other is not person:
                assert not set(landed[other.tag]) & held, other.tag

    twice = _build(crowd[0])
    assert twice.status_code == 400, twice.text
    assert "still going" in twice.json()["detail"], twice.json()


def test_each_of_them_issues_a_token_wearing_a_picture_of_its_own(node, crowd):
    """Four tokens, four faces, every one of the eight transactions paid by the
    account that owns the result.

    The icon half matters more than it looks. An issuance has five strings and
    no sixth, so a token's face is sixty-four characters of inscription id
    riding inside the description -- which is too long for a Class C OP_RETURN,
    and the transaction the browser is about to be shown is Class B instead,
    with sweepable dust outputs in it that are not a fee. A token wearing a
    number that names nothing is the other way this quietly fails: the chain
    accepts it, credits it, and the token is faceless forever. So each account
    inscribes its own icon first and spends that id, and the assertion is the
    round trip -- bytes out, id back, same picture on both ends.
    """
    daemon, state = node[0], node[1]
    index = state.token_index(state.messaging)

    for person, face in zip(crowd, FACES):
        _funded(*node, who=person, amount=6.0)
        picture = (MEDIA / "icons" / f"{face}.png").read_bytes()

        worn = _do(person, "/account/inscribe", {
            "content": base64.b64encode(picture).decode(),
            "content_type": "image/png", "name": f"{face} icon",
            "json": json.dumps({"name": f"{face} icon"})})
        assert worn.status_code == 200, worn.text
        _settle(*node)
        icon = worn.json()["txid"]
        assert icon in {p["txid"] for p in _pieces(person)}, _pieces(person)
        assert index.inscription_content(icon)[1] == picture

        asked = _offer(person, "/account/token/create",
                       {"name": f"{person.tag.title()} Coin",
                        "supply": "1000", "icon": icon})
        assert asked.status_code == 200, asked.text
        assert asked.json()["class"] == "B", \
            "an inscription id does not fit one OP_RETURN"
        made = _complete(person, asked)
        assert made.status_code == 200, made.text
        _settle(*node)

        created = [row for row in index.properties()
                   if str(row["creation_txid"]) == made.json()["txid"]]
        assert created, f"nothing was created by {made.json()['txid']}"
        token = created[0]
        assert token["issuer"] == person.address, \
            "the chain credited the account, not the node"
        assert tokenlib.details(token)["icon"] == icon, token
        assert _tokens(person)[str(token["property_id"])]["balance"] \
            == 1000 * COIN, _tokens(person)


# --- the trading half: offers, both ways (plan item 3) -------------------------

def test_offers_between_accounts_are_made_answered_refused_and_finished(node, crowd):
    """The half of "trading between them, both ways" that waited for routes.

    Ferns offers for a piece of Maple's and Maple offers for a piece of Ferns's.
    Maple answers yes -- a leg at the price Ferns asked, two signatures, sent as a
    message -- and Ferns finishes it from the leg alone, which is the path a
    buyer's tab takes (/account/fill, then /account/fill/sign). Ferns answers
    Maple's no. What changes hands is read off the index afterwards, not off
    any route's own report of what it did.
    """
    state = node[1]
    maple, ferns = crowd[0], crowd[1]
    ours = _inscribed(*node, owner=maple.address, name="Traded piece",
                      content=b"offers" * 60)
    theirs = _inscribed(*node, owner=ferns.address, name="Kept piece",
                        content=b"refuse" * 60)
    index = state.token_index(state.messaging)

    # Both offers, one each way.
    made = _do(ferns, "/account/offer", {"piece": ours, "amount": "3"})
    assert made.status_code == 200, made.text
    wanted = _do(maple, "/account/offer", {"piece": theirs, "amount": "2"})
    assert wanted.status_code == 200, wanted.text
    _settle(*node)
    offer_in, offer_back = made.json()["txid"], wanted.json()["txid"]

    # An answer is a leg on two of the seller's coins, and Maple's own offer
    # just spent hers down to one. The refusal says to split it by sending a
    # coin to yourself -- which /account/send used to refuse, a dead end -- so
    # that is what Maple does.
    # (Only when she really is down to one: tests earlier in this module may
    # have left her spare coins, and then there is nothing to split.)
    one = maple.client.post("/account/accept", json={"piece": ours, "offer": offer_in})
    if one.status_code != 200:
        assert one.status_code == 400 and "Split it first" in one.json()["detail"], one.text
        change = _do(maple, "/account/send", {"to": maple.address, "amount": "1"})
        assert change.status_code == 200, change.text
        _settle(*node)

    # Maple says yes to Ferns: the leg at Ferns's price, signed twice.
    leg = maple.client.post("/account/accept", json={"piece": ours, "offer": offer_in})
    assert leg.status_code == 200, leg.text
    leg = leg.json()
    answered = maple.client.post("/account/accept/sign", json={
        "piece": ours, "offer": offer_in, "raw": leg["raw"],
        "pubkey": maple.pubkey.hex(),
        "signatures": [_sign(maple.secret, bytes.fromhex(d),
                             funding.SINGLE_ANYONECANPAY).hex()
                       for d in leg["sighashes"]]})
    assert answered.status_code == 200, answered.text
    answer = answered.json()["answer"]
    assert answer["ok"] is True and answer["id"] == offer_in

    # Ferns says no to Maple, and nothing is signed for it.
    no = ferns.client.post("/account/accept", json={
        "piece": theirs, "offer": offer_back, "decision": "refuse"})
    assert no.status_code == 200, no.text
    assert no.json()["refused"] is True and no.json()["answer"]["ok"] is False

    # Ferns finishes Maple's yes from the leg it was sent.
    shown = ferns.client.post("/account/fill", json={"leg": answer["leg"]})
    assert shown.status_code == 200, shown.text
    shown = shown.json()
    assert shown["seller"] == maple.address
    done = ferns.client.post("/account/fill/sign", json={
        "leg": answer["leg"], "raw": shown["raw"], "pubkey": ferns.pubkey.hex(),
        "signatures": [_sign(ferns.secret, bytes.fromhex(d)).hex()
                       for d in shown["sighashes"]]})
    assert done.status_code == 200, done.text
    _settle(*node)

    assert index.inscription(ours)["owner"] == ferns.address, "yes moved the piece"
    assert index.inscription(theirs)["owner"] == ferns.address, "no kept it"

    # After the sale (filming, 2026-09-26): the new owner is not shown its own
    # filled offer as one to answer, and both sides are told in Notifications.
    was, state.public = state.public, True
    try:
        page = ferns.client.get(f"/inscriptions/{ours}/view").text
        assert f'data-offer="{offer_in}"' not in page, "a filled offer is not answerable"
        told = maple.client.get("/me/notifications").text
        assert "You sold" in told, "the seller is told about a swap sale"
        bought = ferns.client.get("/me/notifications").text
        assert "You bought" in bought
    finally:
        state.public = was


# --- the big trading test: many accounts at once (roadmap, 2026-09-26) ---------
#
# Everything above is one thing at a time. These are the same routes hit by
# several accounts at the same moment, in threads, and then checked against what
# must always be true, whatever the timing: a piece has exactly one owner, a race
# for one listing has one winner and a clean refusal, and every transaction the
# routes broadcast would be relayed by a real peer. That last one is the check
# regtest cannot make by itself -- our own node takes a transaction that skips
# Pepecoin's soft-dust surcharge (the swap of 2026-09-26 sat unmined for days),
# so it is made here, output by output.

import threading                                             # noqa: E402

from arcade import fees                                      # noqa: E402


def _relayable(rpc, txid: str) -> str:
    """"" if peers would relay it, else why not: every spendable output under
    DUST_LIMIT must have paid DUST_LIMIT again in fee (fees.soft_dust_fee)."""
    tx = rpc.call("getrawtransaction", txid, True)
    outs = []
    for out in tx["vout"]:
        sats = round(float(out["value"]) * COIN)
        script = bytes.fromhex(out["scriptPubKey"]["hex"])
        outs.append((sats, script))
    paid_in = 0
    for vin in tx["vin"]:
        prev = rpc.call("getrawtransaction", vin["txid"], True)
        paid_in += round(float(prev["vout"][vin["vout"]]["value"]) * COIN)
    fee = paid_in - sum(v for v, _ in outs)
    owed = fees.soft_dust_fee(outs)
    if owed and fee < owed:
        return f"{txid}: fee {fee} under the soft-dust surcharge {owed}"
    return ""


def _all_at_once(*calls):
    """Run each call in its own thread, started together; results in order."""
    results = [None] * len(calls)
    gate = threading.Barrier(len(calls))

    def run(i, call):
        gate.wait()
        try:
            results[i] = call()
        except Exception as exc:                     # a crash is a result too
            results[i] = exc

    threads = [threading.Thread(target=run, args=(i, c)) for i, c in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=300)
    return results


def test_two_buyers_race_for_one_listing_and_exactly_one_wins(node, crowd):
    daemon, state = node[0], node[1]
    maple, osier, quill = crowd[0], crowd[2], crowd[3]
    for who in (maple, osier, osier, quill, quill):
        _funded(*node, who=who, amount=5.0)
    piece = _inscribed(*node, owner=maple.address, name="Raced piece",
                       content=b"race" * 70)
    listed = _listed(maple, piece, "2")
    assert listed.status_code == 200, listed.text
    row = listed.json()["listed"]

    first, second = _all_at_once(lambda: _bought(osier, row), lambda: _bought(quill, row))
    codes = sorted(r.status_code if hasattr(r, "status_code") else 999
                   for r in (first, second))
    assert codes[0] == 200, (first, second)
    assert codes[1] in (400, 409), \
        f"the loser is refused cleanly, not crashed: {getattr(second, 'text', second)}"
    won = next(r for r in (first, second) if getattr(r, "status_code", 0) == 200).json()
    assert not _relayable(daemon.rpc, won["txid"]), _relayable(daemon.rpc, won["txid"])

    _settle(*node)
    index = state.token_index(state.messaging)
    owner = index.inscription(piece)["owner"]
    assert owner in (osier.address, quill.address), "the piece went to one of them"
    assert daemon.rpc.call("getrawmempool") == [], "nothing left waiting"


def test_four_accounts_trade_in_a_ring_at_the_same_moment(node, crowd):
    """Each lists a piece and each buys the next one's, all four buys at once."""
    daemon, state = node[0], node[1]
    for who in crowd:
        _funded(*node, who=who, amount=5.0)
        _funded(*node, who=who, amount=5.0)
    pieces, rows = [], []
    for i, who in enumerate(crowd):
        piece = _inscribed(*node, owner=who.address, name=f"Ring piece {i}",
                           content=bytes([65 + i]) * 300)
        listed = _listed(who, piece, "1")
        assert listed.status_code == 200, listed.text
        pieces.append(piece)
        rows.append(listed.json()["listed"])

    # Person i buys person (i+1)'s piece; all four at once.
    n = len(crowd)
    results = _all_at_once(*[
        (lambda i=i: _bought(crowd[i], rows[(i + 1) % n])) for i in range(n)])
    for i, r in enumerate(results):
        assert getattr(r, "status_code", 0) == 200, (i, getattr(r, "text", r))
        assert not _relayable(daemon.rpc, r.json()["txid"])

    _settle(*node)
    index = state.token_index(state.messaging)
    for i in range(n):
        assert index.inscription(pieces[(i + 1) % n])["owner"] == crowd[i].address, i
    assert daemon.rpc.call("getrawmempool") == []


def test_every_account_offers_on_every_other_piece_and_the_best_offer_wins(node, crowd):
    """Twelve offers at once, one answer per holder, every buyer finishes."""
    daemon, state = node[0], node[1]
    for who in crowd:
        _funded(*node, who=who, amount=5.0)
        _funded(*node, who=who, amount=5.0)
    pieces = [_inscribed(*node, owner=who.address, name=f"Offered piece {i}",
                         content=bytes([97 + i]) * 280)
              for i, who in enumerate(crowd)]

    bids = []
    for h, holder in enumerate(crowd):
        for b, bidder in enumerate(crowd):
            if b != h:
                bids.append((h, b, str(1 + b)))        # the richest bid is the last person's
    # One account's own sends are refused while another of its sends is going
    # (its lane), which is the design; so each bidder makes its offers one after
    # another, and the four bidders run at the same moment.
    def bidder_run(b):
        out = {}
        for h, bb, amt in bids:
            if bb == b:
                out[(h, b)] = _do(crowd[b], "/account/offer",
                                  {"piece": pieces[h], "amount": amt})
        return out
    runs = _all_at_once(*[(lambda b=b: bidder_run(b)) for b in range(len(crowd))])
    by_pair = {}
    for r in runs:
        assert isinstance(r, dict), r
        by_pair.update(r)
    made = [by_pair[(h, b)] for h, b, _amt in bids]
    for (h, b, _amt), r in zip(bids, made):
        assert getattr(r, "status_code", 0) == 200, (h, b, getattr(r, "text", r))
    _settle(*node)
    # Answering takes two of the holder's coins; its own offers just spent some.
    for who in crowd:
        _funded(*node, who=who, amount=2.0)
        _funded(*node, who=who, amount=2.0)

    # Each holder answers its best offer; the answers go out at once.
    best = {}
    for (h, b, amt), r in zip(bids, made):
        if h not in best or int(amt) > int(best[h][1]):
            best[h] = (b, amt, r.json()["txid"])

    def answer(h):
        holder, (b, amt, offer) = crowd[h], best[h]
        leg = holder.client.post("/account/accept", json={"piece": pieces[h], "offer": offer})
        if leg.status_code != 200:
            return leg
        leg = leg.json()
        return holder.client.post("/account/accept/sign", json={
            "piece": pieces[h], "offer": offer, "raw": leg["raw"],
            "pubkey": holder.pubkey.hex(),
            "signatures": [_sign(holder.secret, bytes.fromhex(d),
                                 funding.SINGLE_ANYONECANPAY).hex()
                           for d in leg["sighashes"]]})

    answers = _all_at_once(*[(lambda h=h: answer(h)) for h in range(len(crowd))])
    for h, a in enumerate(answers):
        assert getattr(a, "status_code", 0) == 200, (h, getattr(a, "text", a))

    # Each winning buyer finishes its own purchase; all at once.
    def finish(h):
        b = best[h][0]
        buyer, leg = crowd[b], answers[h].json()["answer"]["leg"]
        shown = buyer.client.post("/account/fill", json={"leg": leg})
        if shown.status_code != 200:
            return shown
        shown = shown.json()
        return buyer.client.post("/account/fill/sign", json={
            "leg": leg, "raw": shown["raw"], "pubkey": buyer.pubkey.hex(),
            "signatures": [_sign(buyer.secret, bytes.fromhex(d)).hex()
                           for d in shown["sighashes"]]})

    # A buyer who won several finishes them one after another (its lane refuses
    # two at once, by design); different buyers run at the same moment.
    by_buyer = {}
    for h in range(len(crowd)):
        by_buyer.setdefault(best[h][0], []).append(h)
    def buyer_run(hs):
        return {h: finish(h) for h in hs}
    runs = _all_at_once(*[(lambda hs=hs: buyer_run(hs)) for hs in by_buyer.values()])
    finished = {}
    for r in runs:
        assert isinstance(r, dict), r
        finished.update(r)
    done = [finished[h] for h in range(len(crowd))]
    for h, d in enumerate(done):
        assert getattr(d, "status_code", 0) == 200, (h, getattr(d, "text", d))
        assert not _relayable(daemon.rpc, d.json()["txid"]), _relayable(daemon.rpc, d.json()["txid"])

    _settle(*node)
    index = state.token_index(state.messaging)
    for h in range(len(crowd)):
        assert index.inscription(pieces[h])["owner"] == crowd[best[h][0]].address, h
    assert daemon.rpc.call("getrawmempool") == []


def test_a_piece_listed_and_answered_cannot_be_sold_twice(node, crowd):
    """The double sale. Maple lists a piece AND answers Ferns's offer on it; the
    listing and the answer stand on different coins of Maple's, so nothing at the
    coin level stops both. Osier buys the listing and Ferns finishes the answer
    at the same moment. Whatever the timing, exactly one of them may pay: the
    other must be refused before its coins move, because a coin payment settles
    even when the piece has already gone (D-082)."""
    daemon, state = node[0], node[1]
    maple, ferns, osier = crowd[0], crowd[1], crowd[2]
    for who in (maple, maple, maple, maple, ferns, ferns, osier, osier):
        _funded(*node, who=who, amount=3.0)
    piece = _inscribed(*node, owner=maple.address, name="Twice sold?",
                       content=b"twice" * 60)
    listed = _listed(maple, piece, "2")
    assert listed.status_code == 200, listed.text
    row = listed.json()["listed"]
    offer = _do(ferns, "/account/offer", {"piece": piece, "amount": "3"})
    assert offer.status_code == 200, offer.text
    _settle(*node)
    offer = offer.json()["txid"]
    leg = maple.client.post("/account/accept", json={"piece": piece, "offer": offer})
    assert leg.status_code == 200, leg.text
    leg = leg.json()
    answered = maple.client.post("/account/accept/sign", json={
        "piece": piece, "offer": offer, "raw": leg["raw"], "pubkey": maple.pubkey.hex(),
        "signatures": [_sign(maple.secret, bytes.fromhex(d),
                             funding.SINGLE_ANYONECANPAY).hex() for d in leg["sighashes"]]})
    assert answered.status_code == 200, answered.text
    answer_leg = answered.json()["answer"]["leg"]

    def fill():
        shown = ferns.client.post("/account/fill", json={"leg": answer_leg})
        if shown.status_code != 200:
            return shown
        shown = shown.json()
        return ferns.client.post("/account/fill/sign", json={
            "leg": answer_leg, "raw": shown["raw"], "pubkey": ferns.pubkey.hex(),
            "signatures": [_sign(ferns.secret, bytes.fromhex(d)).hex()
                           for d in shown["sighashes"]]})

    by_listing, by_answer = _all_at_once(lambda: _bought(osier, row), fill)
    paid = [r for r in (by_listing, by_answer) if getattr(r, "status_code", 0) == 200]
    assert len(paid) == 1, ("exactly one sale may be broadcast",
                            getattr(by_listing, "text", by_listing),
                            getattr(by_answer, "text", by_answer))
    _settle(*node)
    index = state.token_index(state.messaging)
    assert index.inscription(piece)["owner"] in (osier.address, ferns.address)

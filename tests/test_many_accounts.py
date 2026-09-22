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

What is here is what an account can already do, and that list has holes in it
on purpose. `door.py` refuses every POST that would spend the NODE's wallet,
and three things are still that shape: inscribing a collection, creating a
token, and buying (a swap needs both halves signed and only one half is in a
browser). So there is no hundred-piece collection in this file and no purchase
in it. Where one of those three becomes an account action, its half of the test
belongs in this file rather than in a new one.

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

import contextlib
import dataclasses
import pathlib
import sys
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
from arcade import inscribe, seed, utxos                    # noqa: E402
from arcade import inscriptions as pieces                   # noqa: E402
from arcade import payload as protocol                      # noqa: E402
from arcade.messaging import envelope                       # noqa: E402
from arcade.messaging import feed as feedlib                # noqa: E402
from arcade.messaging.keys import Identity                  # noqa: E402
from arcade.messaging.scanner import Scanner                # noqa: E402
from arcade.script import b58check_encode, hash160          # noqa: E402
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


def _tokens(person):
    said = person.client.get("/account/tokens").json()
    return {str(token["property_id"]): token
            for chain in said["chains"] for token in chain["tokens"]}


def _pieces(person):
    said = person.client.get("/account/nfts").json()
    return [piece for chain in said["chains"] for piece in chain["pieces"]]


def _inscribed(*node, owner, name, content):
    """One real inscription on the chain, ending up owned by `owner`.

    Two steps, and both are the honest shape of the thing. Inscribing is the
    node's action and not an account's -- see the note at the top -- so the node
    makes the piece with its own wallet and then hands it over with
    `TokenSender`, which is the transfer the interface itself makes. A test that
    inscribed straight into an account's address would be asserting that an
    account can inscribe, which is the sentence this file refuses to write.
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
    """The node makes the token, because making one is still its own action.

    That is the honest shape of a first token today: whoever can pay for a
    creation makes one, and from then on it moves between accounts with nobody's
    help. `door.py` says why the creation is not an account's -- it would spend
    the node's wallet -- and when it stops being one, the creation belongs in
    this test rather than in a new file.
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

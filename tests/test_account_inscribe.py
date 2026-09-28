"""An account inscribing a piece, with a key the node has never seen.

The bytes are the same bytes the node's own wallet would put up, and so is the
transaction shape. The only difference is who is able to sign for it -- which
is the difference between a node that holds everybody's money and a node that
carries other people's coins.

Against a real regtest node, for the reason every account test before this one
gives: the parts that can be wrong here are the ones that look right on paper,
and an inscription the index cannot see is a file somebody paid for and never
got. This one goes further than the message tests do for the same reason --
`inscribe.py`'s own docstring records an inscription that was paid for,
accepted by the chain, filed as a valid type-200 transaction, and simply never
appeared, because it was wrapped twice.
"""

import base64
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_claim import _catch_up, _sign_in                # noqa: E402
from test_funding import _pubkey, _sign                           # noqa: E402
from test_web import app_state, client                            # noqa: E402,F401

from arcade.script import b58check_encode, hash160                 # noqa: E402

COIN = 100_000_000
SECRET = 0x5e5e5e5e0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c


@pytest.fixture
def arcade(tmp_path, regtest):
    """The application pointed at a regtest node, with nothing in it."""
    from fastapi.testclient import TestClient

    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    class Pointed(ChainContext):
        def credentials(self):
            return regtest.rpc._creds

        @property
        def params(self):
            return regtest.params

    chain = Pointed(network="regtest", role="messaging", label="Testnet",
                    datadir=regtest.datadir)
    state = AppState(home=tmp_path, messaging=chain,
                     ledger=ChainContext(network="main", role="ledger",
                                         label="Mainnet",
                                         datadir=pathlib.Path("/nonexistent")))
    (tmp_path / "tokens-chain").write_text("regtest\n")
    return TestClient(create_app(state)), state, regtest.rpc


@pytest.fixture
def seated(arcade):
    """A seat, an address this node cannot sign for, and four coins on it."""
    app, state, rpc = arcade
    _sign_in(app)
    pubkey = _pubkey(SECRET)
    mine = b58check_encode(state.messaging.params.pubkeyhash_version,
                           hash160(pubkey))
    assert not rpc.call("validateaddress", mine).get("ismine"), \
        "the node has no key for this address, which is the whole point"
    rpc.call("generate", 101)
    _catch_up(state, rpc)
    # The coin key travels with the address because a Class B payload puts
    # the sender's key into every data output, and the node cannot derive it:
    # the one thing it is not allowed to have is the key itself.
    assert app.post("/account/address", json={
        "address": mine, "coin_pubkey": pubkey.hex()}).status_code == 200
    rpc.call("sendtoaddress", mine, 4.0)
    rpc.call("generate", 1)
    _catch_up(state, rpc)
    return app, state, rpc, pubkey, mine


def _ask(app, content: bytes, kind: str = "text/plain; charset=utf-8", **extra):
    return app.post("/account/inscribe", json={
        "content": base64.b64encode(content).decode(),
        "content_type": kind, **extra})


def _sign_and_send(app, pubkey, offer):
    signatures = [_sign(SECRET, bytes.fromhex(digest)).hex()
                  for digest in offer["sighashes"]]
    return app.post("/account/sign", json={
        "offer": offer["offer"], "signatures": signatures,
        "pubkey": pubkey.hex()})


def test_an_account_inscribes_one_piece_with_a_key_the_node_never_saw(seated):
    app, state, rpc, pubkey, mine = seated
    text = "the first thing this account put on the chain by itself"

    # Every account test signs for the same key on the session's chain, so
    # this address already holds other tests' pieces by the time this one
    # runs -- and a run of them inscribes a dozen. What this test asks is
    # whether the page gained the piece this test paid for, not how many
    # pieces that key has ever held, so it asks it as a difference.
    held = lambda: {piece["txid"]
                    for piece in app.get("/account/nfts").json()["chains"][0]["pieces"]}
    before = held()

    offered = _ask(app, text.encode(), name="a note")
    assert offered.status_code == 200, offered.text
    offer = offered.json()
    assert offer["what"] == "inscribe a note"
    assert offer["sighashes"] and len(offer["sighashes"]) == len(offer["inputs"])
    assert offer["bytes"] == len(text.encode())
    assert rpc.call("getrawmempool") == [], "nothing has gone out"

    done = _sign_and_send(app, pubkey, offer)
    assert done.status_code == 200, done.text
    txid = done.json()["txid"]
    assert txid in rpc.call("getrawmempool"), "the node took it"

    rpc.call("generate", 1)
    index = _catch_up(state, rpc)
    row = index.inscription(txid)
    assert row is not None, "the engine filed it as an inscription"
    assert row["creator"] == mine and row["owner"] == mine
    assert row["content_len"] == len(text.encode())
    assert index.inscription_content(txid) == (
        "text/plain; charset=utf-8", text.encode()), "the file is the file"

    # And the account's own page can see it, which is how they find out it
    # landed -- without this, a piece that only the index could name.
    assert held() - before == {txid}


def _parts(state):
    """The same book the routes write in, opened the long way round.

    What the page is shown and what this reads have to be one file, so this
    reads `state.home` rather than a path of its own.
    """
    from arcade import accountparts

    return accountparts.Parts(state.home / "accountparts.sqlite")


def _piece_of(data: bytes, n: int, chunk_len: int, manifest_len: int) -> bytes:
    """The bytes of piece n, cut the way the browser cuts them.

    The stream is the manifest and then the file, so piece n holds stream
    bytes `n * chunk_len` to `(n + 1) * chunk_len` -- and the first piece's
    slice of the FILE starts before zero, which is the manifest, so it clamps.
    If this and `pieceOf` in the template disagree, the test passes and the
    page writes a file nobody chose.
    """
    return data[max(0, n * chunk_len - manifest_len):
                min(len(data), (n + 1) * chunk_len - manifest_len)]


def _rich(state, rpc, address: str, coins: float = 14.0):
    """Give the address enough to split.

    `seated` puts four coins on it, which is a piece of change for a one
    transaction inscription and not enough for a split: three outputs of about
    two coins each plus the fee is seven, and a split that cannot be funded
    fails here rather than in front of somebody's photograph.
    """
    rpc.call("sendtoaddress", address, coins)
    rpc.call("generate", 1)
    _catch_up(state, rpc)


def _piece(app, job: dict, n: int, blob: bytes, **extra):
    return app.post("/account/inscribe", json={
        "part": job["part"], "chunk": n,
        "content": base64.b64encode(_piece_of(
            blob, n, int(job["chunk_len"]),
            int(job["manifest_len"]))).decode(), **extra})


def _pool(rpc):
    """What the node is holding, in the units its own limits are counted in.

    A refused broadcast says only `too-long-mempool-chain`, which names four
    different numbers. The mempool's verbose listing names all of them.
    """
    return {"info": rpc.call("getmempoolinfo"),
            "txs": {t: {k: v for k, v in row.items()
                        if k in ("bytes", "ancestorcount", "ancestorbytes",
                                "descendantcount", "descendantsize",
                                "depends", "spentby")}
                    for t, row in rpc.call("getrawmempool", True).items()}}


# A 20 KB file: three transactions at about 6.7 KB each, which is what a
# phone photograph arrives as once the page has resized it.
BIG = bytes(range(256)) * 78 + b"!" * 40


def test_a_big_file_goes_out_as_a_split_and_then_one_transaction_per_piece(seated):
    """The stall this removes: a piece used to wait for the one before it.

    What has to be proved is not that the file assembles -- the index says
    that -- but how many blocks it costs. One, the split's, and then every
    piece is in the mempool at the same moment. That is what the node's own
    wallet has always arranged for itself, in `sender.ensure_outputs`, and
    what an account could not ask for until there was a book to keep the
    score. The old test here refused the whole idea, on the belief that a
    piece spends the piece before it; `inscriptions.py` says the opposite and
    the carriage is built the other way. What it does wait for is the split,
    and that is the second half of this test.
    """
    app, state, rpc, pubkey, mine = seated
    _rich(state, rpc, mine)
    held = lambda: {piece["txid"]
                    for piece in app.get("/account/nfts").json()["chains"][0]["pieces"]}
    before = held()

    started = _ask(app, BIG, kind="image/png", name="a photograph")
    assert started.status_code == 200, started.text
    go = started.json()
    pieces = int(go["chunks"])
    assert pieces > 1, "20 KB is several transactions, not one"
    assert go["split"] == "offered"
    assert "split into" in go["what"], go["what"]
    assert rpc.call("getrawmempool") == [], "nothing has gone out yet"

    out = _sign_and_send(app, pubkey, go)
    assert out.status_code == 200, out.text
    split_txid = out.json()["txid"]
    book = _parts(state)
    assert book.get(go["part"])["split_txid"] == split_txid, \
        "the split is written down the moment it is out"

    # That split is the ONLY block any of this waits for -- and it does wait,
    # which is the half of the story the old refusal in this file got wrong in
    # the other direction. A piece spends an output of the split, so while the
    # split is only in the mempool every piece is its grandchild, and the
    # chain counts the package under an unconfirmed parent at about a hundred
    # kilobytes: two of these transactions, and the third came back
    # `too-long-mempool-chain`, measured. So the answer here is where things
    # stand, not a transaction the network would throw away.
    waiting = _piece(app, go, 0, BIG)
    assert waiting.status_code == 200, waiting.text
    assert waiting.json()["waiting"], waiting.json()
    assert "offer" not in waiting.json()
    assert set(rpc.call("getrawmempool")) == {split_txid}, \
        "waiting puts nothing out and costs nothing"

    rpc.call("generate", 1)
    _catch_up(state, rpc)

    txids = []
    for n in range(pieces):
        asked = _piece(app, go, n, BIG)
        assert asked.status_code == 200, asked.text
        assert "waiting" not in asked.json(), \
            "the split is in a block; there is nothing left to wait for"
        assert asked.json()["chunk"] == n
        sent = _sign_and_send(app, pubkey, asked.json())
        assert sent.status_code == 200, \
            f"piece {n + 1} of {pieces}: {sent.text} {_pool(rpc)}"
        txids.append(sent.json()["txid"])

    pool = set(rpc.call("getrawmempool"))
    assert set(txids) <= pool, \
        "all of them unconfirmed together: one block for the file, not one " \
        "block per piece"
    for n, txid in enumerate(txids):
        raw = rpc.call("decoderawtransaction",
                       rpc.call("getrawtransaction", txid))
        assert [(v["txid"], v["vout"]) for v in raw["vin"]] == [(split_txid, n)], \
            "piece %d spends the output the split made for it, and nothing else" % n

    rpc.call("generate", 1)
    index = _catch_up(state, rpc)
    row = index.inscription(txids[0])
    assert row is not None, "the manifest piece is where it is filed"
    assert row["content_len"] == len(BIG)
    assert int(row["chunks"]) == pieces
    assert row["creator"] == mine and row["owner"] == mine
    assert index.inscription_content(txids[0]) == ("image/png", BIG), \
        "the file is the file, piece by piece and back again"
    assert held() - before == {txids[0]}, \
        "the account gained one inscription, not three"

    done = book.get(go["part"])
    assert done["status"] == "done" and done["sent"] == pieces
    assert done["next"] is None
    assert all(c["status"] == "sent" for c in book.chunks(go["part"]))


def test_a_piece_that_is_not_the_file_that_started_it_is_refused(seated):
    """A piece is permanent, so the bytes have to be the promised ones.

    The tab that resumes is not the tab that started, and the file it read off
    the disk may be a different one -- same name, resized differently, or a
    second photograph that happens to be the same length. Pieces of two files
    assemble into a third file that nobody chose and everybody paid for, and
    there is no taking it back.
    """
    app, state, rpc, pubkey, mine = seated
    _rich(state, rpc, mine)
    started = _ask(app, BIG, kind="image/png", name="a photograph")
    go = started.json()
    out = _sign_and_send(app, pubkey, go)
    assert out.status_code == 200, out.text

    answer = _piece(app, go, 1, bytes(len(BIG)))
    assert answer.status_code == 400
    said = answer.json()["detail"]
    assert "not the bytes" in said, said
    assert "Nothing has been paid for" in said, said
    book = _parts(state)
    assert book.chunks(go["part"])[1]["status"] == "pending", \
        "the piece is still owed, unchanged"
    assert set(rpc.call("getrawmempool")) == {out.json()["txid"]}


def test_the_same_file_asked_again_continues_it_rather_than_starting_again(seated):
    """A tab that closed in the middle comes back to the same inscription.

    Asked again with the whole file and no id at all -- which is all a browser
    that restarted has -- the node recognises the file by its hash, says where
    it stopped, and offers nothing, because the split is already on its way. A
    second split would be the same inscription paid for twice.
    """
    app, state, rpc, pubkey, mine = seated
    _rich(state, rpc, mine)
    go = _ask(app, BIG, kind="image/png", name="a photograph").json()
    assert _sign_and_send(app, pubkey, go).status_code == 200
    rpc.call("generate", 1)
    _catch_up(state, rpc)
    first = _piece(app, go, 0, BIG)
    assert _sign_and_send(app, pubkey, first.json()).status_code == 200

    back = _ask(app, BIG, kind="image/png", name="a photograph")
    assert back.status_code == 200, back.text
    again = back.json()
    assert again["part"] == go["part"], "the same job, not a second one"
    assert again["resumed"] is True
    assert "offer" not in again, "the split is out; there is nothing to offer"
    assert again["sent"] == 1 and again["next"] == 1
    assert again["chunks"] == go["chunks"]
    assert again["chunk_len"] == go["chunk_len"]
    assert len(_parts(state).list()) == 1, "no second row for the same file"

    for n in range(again["next"], int(again["chunks"])):
        asked = _piece(app, again, n, BIG)
        assert asked.status_code == 200, asked.text
        assert "offer" in asked.json(), asked.json()
        assert _sign_and_send(app, pubkey, asked.json()).status_code == 200
    assert _parts(state).get(go["part"])["status"] == "done"


def test_a_hundred_signatures_are_one_inscription_not_a_hundred(seated):
    """The allowance is a count of gestures, and this was one gesture.

    `PER_HOUR["inscribe"]` is ten because an inscription is a thing that stays.
    Reading it as a count of SIGNATURES would turn a photograph into a job that
    runs four hours and gets refused halfway, which is the stall this whole
    step exists to remove -- so the split is charged and the pieces are not.
    What still bounds them is the day's bytes, and the pile of offers.
    """
    app, state, rpc, pubkey, mine = seated
    _rich(state, rpc, mine)
    state.set_setting("quota:inscribe", 1)

    go = _ask(app, BIG, kind="image/png", name="a photograph").json()
    assert _sign_and_send(app, pubkey, go).status_code == 200
    rpc.call("generate", 1)
    _catch_up(state, rpc)
    for n in range(int(go["chunks"])):
        asked = _piece(app, go, n, BIG)
        assert asked.status_code == 200, \
            f"piece {n + 1} refused: {asked.json()}"
        assert "offer" in asked.json(), asked.json()
        assert _sign_and_send(app, pubkey, asked.json()).status_code == 200

    answer = _ask(app, b"a different file entirely", name="a note")
    assert answer.status_code == 400, \
        "the allowance was spent once, and it is spent"
    assert "in an hour" in answer.json()["detail"]


def _spent(app, kind: str = "inscribe") -> int:
    """What the account's own page says it has spent.

    Read through the page rather than off the register, because the number the
    person is shown and the number a refusal is counting have to be one
    number -- which is what §6 is about.
    """
    return next(one for one in app.get("/account").json()["quota"]["actions"]
                if one["kind"] == kind)["used"]


def test_a_confirmation_that_was_never_signed_costs_the_hour_once(seated):
    """Asking again about the same file is one gesture said twice.

    The node built the offer, the page showed the fee, and the person said no
    -- or the phone locked, or the tab was closed. Nothing went out and nobody
    paid for anything, and the walk-through found the hour had been charged
    anyway: ten changes of mind and an account is out of ten inscriptions with
    an empty chain to show for it. A split already had the answer in its job
    book -- `_inscribe_again`, "nothing is charged and nothing is inscribed
    twice" -- and a one-piece file had nothing that remembered it had been
    asked for, so the offer pile remembers it instead.

    One gesture, one charge, so the echo is not gated on the allowance either:
    the middle of this test has the hour full and a repeat still hands over an
    offer, because refusing there would strand an inscription whose allowance
    was already spent. That is not free attempts for sale -- an offer is only
    in the pile at all because an earlier ask paid for it, and the last line is
    still a refusal.
    """
    app, state, rpc, pubkey, mine = seated
    state.set_setting("quota:inscribe", 2)
    wavering = b"the photograph I am not sure about"

    first = _ask(app, wavering, name="a")
    assert first.status_code == 200, first.text
    assert _spent(app) == 1

    again = _ask(app, wavering, name="a")
    assert again.status_code == 200, again.text
    assert "offer" in again.json(), \
        "the second ask still has to hand something over to sign"
    assert _spent(app) == 1, "the same file asked twice is one gesture"
    # Not "is the mempool empty" -- it is a shared regtest node and another
    # test's funding transaction can be in it, mined nothing in between. What
    # has to be true is that none of THIS offer's coins has gone out.
    named = {(one["txid"], one["vout"]) for one in again.json()["inputs"]}
    gone = [txid for txid in rpc.call("getrawmempool")
            if any((vin["txid"], vin["vout"]) in named
                   for vin in rpc.call("getrawtransaction", txid, True)["vin"])]
    assert gone == [], "and nothing went out while they were deciding"

    other = _ask(app, b"a different file, a different gesture", name="b")
    assert other.status_code == 200, other.text
    assert _spent(app) == 2, "a different file is a different gesture"

    # The hour is full, and the echo of a file already in the pile still comes
    # back with an offer.
    echo = _ask(app, b"a different file, a different gesture", name="b")
    assert echo.status_code == 200, echo.text
    assert "offer" in echo.json(), echo.json()
    assert _spent(app) == 2, "an echo counts nothing, cap or no cap"

    # Empty the pile the honest way rather than waiting five minutes for the
    # offers to go stale: sign one, which is the gesture those asks were about.
    assert _sign_and_send(app, pubkey, again.json()).status_code == 200
    assert _spent(app) == 2, "signing is not a second charge for one gesture"

    refused = _ask(app, b"the next file, three minutes from now", name="c")
    assert refused.status_code == 400, \
        "the hour is spent, and a new file cannot ride in on an old offer"
    assert "in an hour" in refused.json()["detail"], refused.json()
    assert _spent(app) == 2, "a refusal counts nothing"


def test_an_operator_who_closed_inscriptions_says_which_it_is(seated):
    """The dial on the Overview is the one an operator most wants: an
    inscription is the one thing here that cannot be taken back."""
    app, state, rpc, pubkey, mine = seated
    state.set_setting("quota:inscribe", 0)
    answer = _ask(app, b"closed for business")
    assert answer.status_code == 400
    assert "not taking inscriptions" in answer.json()["detail"]
    assert rpc.call("getrawmempool") == []


def test_the_bytes_of_an_inscription_count_against_the_day(seated):
    """Whatever carries them, the ceiling is on what the chain has to keep."""
    app, state, rpc, pubkey, mine = seated
    state.set_setting("quota:bytes", 30)
    answer = _ask(app, b"y" * 200)
    assert answer.status_code == 400
    assert "bytes a day" in answer.json()["detail"]
    assert rpc.call("getrawmempool") == []


def test_every_account_route_is_one_a_public_node_will_reach(client):
    """The door lists its POST paths one at a time, so a route left off the
    list works on every test machine and 403s on the instances where the
    people actually are."""
    from arcade.web import door

    app, _ = client
    asked = {route.path for route in app.app.routes
             if (getattr(route, "methods", None) or set()) & {"POST"}
             and route.path.startswith("/account/")}
    listed = {path.rstrip("/") or "/" for path in door.PUBLIC_POST}
    assert asked - listed == set(), \
        f"{sorted(asked - listed)} needs adding to door.PUBLIC_POST"

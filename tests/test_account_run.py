"""An account running a collection, with a key this node has never held.

The one-piece route says what an account cannot do: a file that needs two
transactions is refused, because the second spends the first's output and
cannot even be built until that one is in a block. That is a job, and a job is
what these tests are about -- a run that outlives the request that started it,
the tab that was open when it started, and in one case the offer that expired
while somebody was reading the page.

Against a real regtest node, as every account test is: the parts of an
inscription that can be wrong are the parts that look right on paper, and a
collection that half-inscribes is the specific failure this step is shaped
around.

The fixtures are the one-piece test's, imported rather than repeated -- same
seat, same address the node cannot sign for, same four coins.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_claim import _catch_up, _sign_in                   # noqa: E402
from test_account_inscribe import arcade, seated                    # noqa: E402,F401
from test_account_inscribe import _sign_and_send                    # noqa: E402
from test_collections import hashlips                               # noqa: E402
from test_web import app_state, client                              # noqa: E402,F401

from arcade import collections as collectionlib                      # noqa: E402
from arcade import accountruns                                       # noqa: E402

COIN = 100_000_000


def _start(app, build, **fields):
    """Upload a build folder the way a browser sends a chosen folder."""
    files = [("files", (path.name, path.read_bytes()))
             for path in sorted(build.rglob("*")) if path.is_file()]
    return app.post("/account/run/start", files=files,
                    data={"run_chain": "regtest", **fields})


def _book(state):
    """The run book, read directly, the way this suite reads the operator's."""
    return accountruns.Runs(state.home / "accountruns.sqlite")


def _flat(page: str) -> str:
    """A page's words, with the markup's line breaks folded away.

    A sentence here is split over three lines of template because the lines
    have to stay short, so asserting a phrase means asserting it on the words
    rather than on the bytes between them.
    """
    return " ".join(page.split())


def _next(app, pubkey, run_id):
    """Ask for the next piece, sign it, let the node take it."""
    asked = app.post("/account/run/piece", json={"run": run_id})
    assert asked.status_code == 200, asked.text
    offer = asked.json()
    done = _sign_and_send(app, pubkey, offer)
    assert done.status_code == 200, done.text
    return offer, done.json()["txid"]


@pytest.fixture(autouse=True)
def _faucet_off(seated):
    """The daily top-off is switched off for this file, as it is for the one
    test in `test_account_order` that noticed it first (app.py:8074, wired in
    `884db08`): any page that reads a balance pays the shortfall to that account
    in a background thread, so the `getrawmempool == []` and `balance == held`
    lines below stopped being a fact about the refusal and became a race with a
    gift. Reading `/account` to prove an account holds nothing is the very act
    that hands it a coin. The feature is what Robin asked for; what had to go
    is the assumption that an account stays as it was left. `faucet: 0` is the
    operator's own switch, so no rule is invented here."""
    app, state, rpc, pubkey, mine = seated
    state.set_setting("faucet", 0)


def test_an_account_runs_a_collection_one_piece_at_a_time(seated, tmp_path):
    """Three pieces, three signatures, one collection, no key the node holds.

    The run's status comes from its pieces rather than from anybody remembering
    to set it, so the last assertion is the one that matters: a run that reads
    `done` while owing a piece is the bug this whole shape exists to avoid.
    """
    app, state, rpc, pubkey, mine = seated
    started = _start(app, hashlips(tmp_path, count=3))
    assert started.status_code == 200, started.text
    run = started.json()
    assert run["items"] == 3 and run["name"] == "Doge Punks"
    assert run["next"] == "Doge Punks #1"
    assert rpc.call("getrawmempool") == [], "starting a run costs nothing"

    landed = []
    for edition in (1, 2, 3):
        offer, txid = _next(app, pubkey, run["run"])
        assert offer["piece"] == edition and offer["items"] == 3
        assert offer["what"] == f"inscribe Doge Punks #{edition}"
        assert txid in rpc.call("getrawmempool")
        # A block between pieces, which is the pace a run actually goes out
        # at once the previous piece's change is what the next one spends.
        rpc.call("generate", 1)
        _catch_up(state, rpc)
        landed.append(txid)

    index = state.token_index(state.chain_named("regtest"))
    for txid in landed:
        row = index.inscription(txid)
        assert row is not None, "the engine filed it as an inscription"
        assert row["creator"] == mine and row["owner"] == mine
        assert row["collection"] == "Doge Punks"

    # Nothing left to offer, which is how a run finishes: not a status somebody
    # set, but a book with no pending row in it.
    over = app.post("/account/run/piece", json={"run": run["run"]})
    assert over.status_code == 200
    assert over.json()["finished"] and over.json()["sent"] == 3
    assert rpc.call("getrawmempool") == []


def test_an_expired_offer_builds_the_same_piece_not_a_new_one(seated, tmp_path):
    """A tab left open past five minutes is not a lost piece.

    Rebuilding has to mean the same piece -- same edition, same inscription id
    -- because a new id would put the same picture on the chain twice under two
    names, with the money for the first one gone either way.

    Each test names its own set for a reason that started with the refusal
    added to `/account/run/start`: an upload of a collection this address
    already put on the chain is refused now, and it should be. Every account
    test signs for the same key on the session's chain, so a test reusing a
    finished set's name would spend its assertions on that refusal instead of
    on its own question.
    """
    app, state, rpc, pubkey, mine = seated
    run_id = _start(app, hashlips(tmp_path, count=1,
                                  prefix="Doge Punks Twice")).json()["run"]

    # The chain is the session's and every account test signs for the same
    # `SECRET`, so this address already holds other tests' inscriptions. What a
    # second offer must not do is ADD one, which is a different question from
    # how many the account has ever held.
    held = lambda: {p["txid"] for p in
                    app.get("/account/nfts").json()["chains"][0]["pieces"]}
    before = held()

    first = app.post("/account/run/piece", json={"run": run_id}).json()
    # And again without signing the first: a reload, a second tab, an offer that
    # went stale. It names the same piece, and signing either one puts one
    # inscription on the chain rather than two.
    again = app.post("/account/run/piece", json={"run": run_id}).json()
    assert again["piece"] == first["piece"] == 1
    assert again["offer"] != first["offer"]

    done = _sign_and_send(app, pubkey, again)
    assert done.status_code == 200, done.text
    rpc.call("generate", 1)
    _catch_up(state, rpc)

    assert held() - before == {done.json()["txid"]}, \
        "two offers for one piece put two inscriptions on the chain"
    assert app.post("/account/run/piece",
                    json={"run": run_id}).json()["finished"]


def test_an_item_of_more_than_one_transaction_goes_up_as_it_is(seated, tmp_path):
    """Never shrunk (2026-10-08: "when inscribing a collection the files should
    never be compressed"). An item bigger than one transaction is written down, not
    refused; the run hands it to the page as `big`, with its bytes exactly as uploaded,
    and the page sends it the one-big-file way and reports its first transaction."""
    app, state, rpc, pubkey, mine = seated
    build = hashlips(tmp_path, count=2, prefix="Doge Punks Vast", sizes={2: 30_000})
    answer = _start(app, build)
    assert answer.status_code == 200, answer.text
    run_id = answer.json()["run"]
    assert answer.json()["items"] == 2
    _next(app, pubkey, run_id)                       # #1 is one transaction, as before
    rpc.call("generate", 1)
    _catch_up(state, rpc)

    big = app.post("/account/run/piece", json={"run": run_id})
    assert big.status_code == 200, big.text
    big = big.json()
    assert big["big"] and big["piece"] == 2 and big["chunks"] > 1
    assert "offer" not in big, "nothing is offered for a big item: the page sends it"
    got = app.get(f"/account/run/item?run={run_id}&edition=2")
    assert got.status_code == 200
    assert got.content == (build / "images" / "2.png").read_bytes(), "the bytes as uploaded"
    # asked again before it is done: the same item, still owed
    assert app.post("/account/run/piece", json={"run": run_id}).json()["piece"] == 2

    assert app.post("/account/run/done", json={"run": run_id, "piece": 1,
                                               "txid": "ab" * 32}).status_code == 400
    assert app.post("/account/run/done", json={"run": run_id, "piece": 2,
                                               "txid": "not a txid"}).status_code == 400
    done = app.post("/account/run/done", json={"run": run_id, "piece": 2, "txid": "ab" * 32})
    assert done.status_code == 200, done.text
    over = app.post("/account/run/piece", json={"run": run_id}).json()
    assert over["finished"] and over["sent"] == 2


def test_a_folder_too_big_for_one_request_goes_up_in_batches(seated, tmp_path):
    """Cloudflare refuses a body over 100 MB, and a 6,480-picture folder is 112 MB
    (2026-10-08): the page sends batches into one upload and starts from it."""
    app, state, rpc, pubkey, mine = seated
    build = hashlips(tmp_path, count=3, prefix="Doge Punks Batched")
    paths = [p for p in sorted(build.rglob("*")) if p.is_file()]
    first = app.post("/account/run/upload", files=[("files", (p.name, p.read_bytes())) for p in paths[:3]],
                     data={"upload": ""})
    assert first.status_code == 200, first.text
    upload = first.json()["upload"]
    rest = app.post("/account/run/upload", files=[("files", (p.name, p.read_bytes())) for p in paths[3:]],
                    data={"upload": upload})
    assert rest.status_code == 200 and rest.json()["upload"] == upload
    started = app.post("/account/run/start", data={"run_chain": "regtest", "upload": upload})
    assert started.status_code == 200, started.text
    assert started.json()["items"] == 3 and started.json()["name"] == "Doge Punks Batched"
    # an upload id that is not this account's is not a way into somebody's folder
    assert app.post("/account/run/start", data={"run_chain": "regtest",
                                                "upload": "0" * 8}).status_code == 400
    assert app.post("/account/run/upload", files=[("files", (paths[0].name, b"x"))],
                    data={"upload": "../../etc"}).status_code == 400
    app.post("/account/run/stop", json={"run": started.json()["run"]})


def test_a_run_an_operator_closed_costs_nothing(seated, tmp_path):
    """The dial that paces a run is the one-piece dial, not a waiver for it.

    A hundred pieces at ten an hour takes ten hours, and that is the operator's
    number doing its job on the one action this node carries forever. Closing
    the dial closes runs with it, which is the honest reading of the sentence
    on the Overview page.
    """
    app, state, rpc, pubkey, mine = seated
    run_id = _start(app, hashlips(tmp_path, count=2,
                                  prefix="Doge Punks Metered")).json()["run"]
    state.set_setting("quota:inscribe", 0)
    answer = app.post("/account/run/piece", json={"run": run_id})
    assert answer.status_code == 400
    assert "not taking inscriptions" in answer.json()["detail"]
    assert rpc.call("getrawmempool") == []


def test_stopping_a_run_files_it_without_cancelling_anything(seated, tmp_path):
    """There is no thread to stop, so "stop" has to mean something else.

    What it changes is which run this account is working on. That is not a
    nicety: an account is allowed one run going at a time, so a run its owner
    walked away from rather than finished would hold that place for as long as
    this node exists.
    """
    app, state, rpc, pubkey, mine = seated
    run_id = _start(app, hashlips(tmp_path, count=2,
                                  prefix="Doge Punks Stopped")).json()["run"]
    _next(app, pubkey, run_id)
    rpc.call("generate", 1)
    _catch_up(state, rpc)

    stopped = app.post("/account/run/stop", json={"run": run_id})
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["status"] == "stopped"
    assert stopped.json()["sent"] == 1, "what landed is still counted"
    assert rpc.call("getrawmempool") == [], "filing a run costs nothing"
    listed = app.post("/account/run").json()["runs"]
    assert [run["status"] for run in listed] == ["stopped"]

    # And the account can change its mind with the action that would have gone
    # on anyway, rather than needing a second button to undo the first.
    asked = app.post("/account/run/piece", json={"run": run_id})
    assert asked.status_code == 200, asked.text
    assert asked.json()["piece"] == 2
    assert app.post("/account/run").json()["runs"][0]["status"] == "running"


def test_an_account_has_one_run_going_not_two(seated, tmp_path):
    """The rule the stop button exists for, enforced where a run is written.

    Two runs from one address would go out in whichever order the account
    clicked, and a signature over piece nine would not say which collection it
    paid for. `due` is the book's question for exactly this, so it is asked
    before the upload is even read off disk.
    """
    app, state, rpc, pubkey, mine = seated
    build = hashlips(tmp_path, count=2, prefix="Doge Punks Second")
    started = _start(app, build)
    assert started.status_code == 200, started.text

    second = _start(app, build)
    assert second.status_code == 400, second.text
    assert "still going" in second.json()["detail"]
    assert rpc.call("getrawmempool") == [], "a refusal costs nothing"

    # Stopping is what gives the place back, which is the whole reason there is
    # a button for it rather than a shrug.
    stopped = app.post("/account/run/stop", json={"run": started.json()["run"]})
    assert stopped.status_code == 200, stopped.text
    again = _start(app, build)
    assert again.status_code == 200, again.text


def test_a_run_id_is_not_a_key_to_somebody_elses_collection(seated):
    """The name and the pictures in a build are one thing; the next piece of
    it is another.

    A piece is a transaction built out of coins the asker cannot sign for and
    offered to them to sign, so the answer to a run that is not theirs has to
    be no -- and a flat no, since confirming which of the two it is tells a
    stranger how many runs this node is holding.
    """
    app, state, rpc, pubkey, mine = seated
    assert app.post("/account/run/piece", json={"run": "abcdef"}).status_code == 404


def test_an_account_run_is_nowhere_the_operators_runner_looks(seated, tmp_path):
    """The reason the book is a separate file, said as an assertion.

    `Runner.resume_interrupted` walks every row in `collections.Jobs` at
    startup and builds each one's signer out of the node's own wallet, so a run
    written into that book would be inscribed with the node's coins and the
    node's key by the next restart. That is the hazard the unifying refactor
    would reintroduce, which is why it is tested rather than commented.
    """
    app, state, rpc, pubkey, mine = seated
    run_id = _start(app, hashlips(tmp_path, count=2,
                                  prefix="Doge Punks Private")).json()["run"]
    assert run_id
    assert collectionlib.Jobs(state.home / "collections.sqlite").list() == [], \
        "an account's run appeared in the book the node's Runner resumes"


def test_a_set_already_on_the_chain_is_not_offered_again(seated, tmp_path):
    """The expensive mistake, refused at the upload instead of at the fee.

    A finished run leaves no mark on the upload form, so an account that could
    not tell whether it had done the thing uploaded the folder again and was
    handed a second run -- a second payment for inscriptions that join nothing,
    because a collection admits one piece per edition (D-120). The operator's
    wizard asks this question before it prices anything; this is the same
    question, asked of the chain for one address rather than of a wallet this
    node holds.
    """
    app, state, rpc, pubkey, mine = seated
    build = hashlips(tmp_path, count=1, prefix="Doge Punks Once")
    run_id = _start(app, build).json()["run"]
    _next(app, pubkey, run_id)
    rpc.call("generate", 1)
    _catch_up(state, rpc)

    held = app.get("/account").json()["balance"]
    again = _start(app, build)
    assert again.status_code == 400, again.text
    said = again.json()["detail"]
    assert "Doge Punks Once" in said and "already on this chain" in said, said
    assert rpc.call("getrawmempool") == [] and \
        app.get("/account").json()["balance"] == held, "a refusal costs nothing"
    runs = app.post("/account/run").json()["runs"]
    assert len(runs) == 1 and runs[0]["status"] == "done", \
        "the refusal must not write a second run either"


def test_a_set_that_is_out_but_not_in_a_block_yet_is_not_offered_again(seated,
                                                                      tmp_path):
    """Empty index, paid-for pieces: the minutes that read as nothing happened.

    Between the last piece being broadcast and its block, the chain genuinely
    does not have the set yet, so the question above gets an honest empty
    answer and would let the account pay for the whole thing twice. The run
    book is what speaks here -- a run of this name that finished at this
    floor's height has sent everything and is waiting on a block, which is a
    reason to wait, not a reason to re-inscribe.
    """
    app, state, rpc, pubkey, mine = seated
    build = hashlips(tmp_path, count=1, prefix="Doge Punks Out")
    run_id = _start(app, build).json()["run"]
    _next(app, pubkey, run_id)                    # broadcast, not yet mined
    assert app.post("/account/run").json()["runs"][0]["status"] == "done", \
        "the last piece went out, which is all the book can know"
    index = state.token_index(state.chain_named("regtest"))
    assert index.collection_editions(mine, "Doge Punks Out") == set(), \
        "and the index has not seen it, honestly"

    out = _start(app, build)
    assert out.status_code == 400, out.text
    assert "indexed" in out.json()["detail"], out.json()
    assert rpc.call("getrawmempool") != [], "the piece is still out there"

    rpc.call("generate", 1)
    _catch_up(state, rpc)
    landed = _start(app, build)
    assert landed.status_code == 400, landed.text
    assert "already on this chain" in landed.json()["detail"], landed.json()


def test_half_a_set_that_is_up_is_written_down_as_the_other_half(seated,
                                                                tmp_path):
    """Not a refusal: a run of two pieces, priced as two, starting at #2.

    The alternative to refusing a repeat is refusing a resume, and an account
    that stopped halfway for a reason this node cannot fix -- a closed tab, an
    hour's quota -- needs the second half, not a lecture. So the pieces the
    chain already has are dropped from the build before the run is written,
    which is what makes the fee on the page the fee the run charges (D-120)
    and keeps the pieces that are up out of the table to be re-offered.
    """
    app, state, rpc, pubkey, mine = seated
    build = hashlips(tmp_path, count=3, prefix="Doge Punks Half")
    first = _start(app, build).json()
    _next(app, pubkey, first["run"])
    rpc.call("generate", 1)
    _catch_up(state, rpc)
    assert app.post("/account/run/stop",
                    json={"run": first["run"]}).json()["status"] == "stopped"

    again = _start(app, build)
    assert again.status_code == 200, again.text
    book = again.json()
    assert book["items"] == 2, "the piece that is up is not in this run"
    assert book["next"] == "Doge Punks Half #2"
    assert book["fee"] < first["fee"], "priced as the two, not the three"
    assert "already on this chain" in book["note"], book["note"]

    # The sentence goes on the row and not only in the answer, because the
    # page that follows the run has to still be able to say next week why it
    # is a piece short of the folder.
    listed = app.post("/account/run").json()["runs"][0]
    assert listed["items"] == 2 and "already on this chain" in listed["note"]

    asked = app.post("/account/run/piece", json={"run": book["run"]})
    assert asked.status_code == 200, asked.text
    assert asked.json()["piece"] == 2 and asked.json()["items"] == 2
    assert asked.json()["name"] == "Doge Punks Half #2", "not #1 again"


def test_a_piece_this_node_cannot_build_is_named_and_the_rest_still_goes(seated,
                                                                        tmp_path):
    """A hole in a set is said, in the middle of a run, and does not stop it.

    The picture is missing from the folder this node kept -- an upload that
    lost a file, a disk that lost one later. Every other piece of the build is
    fine, so the set still goes out; what must not happen is the run reading
    `done` around the hole, which is what a status derived only from "nothing
    pending" would say, and it must not hold the account's place as if it were
    still working.
    """
    app, state, rpc, pubkey, mine = seated
    run_id = _start(app, hashlips(tmp_path, count=3,
                                  prefix="Doge Punks Hole")).json()["run"]
    folder = pathlib.Path(_book(state).get(run_id)["folder"])
    (folder / "images" / "2.png").unlink()

    _next(app, pubkey, run_id)
    rpc.call("generate", 1)
    _catch_up(state, rpc)
    held = app.get("/account").json()["balance"]

    stuck = app.post("/account/run/piece", json={"run": run_id})
    assert stuck.status_code == 400, stuck.text
    assert "Doge Punks Hole #2" in stuck.json()["detail"], stuck.json()
    assert rpc.call("getrawmempool") == [] and \
        app.get("/account").json()["balance"] == held, "a refusal costs nothing"

    # The hole is one file, not the whole folder: #3 is a different picture.
    offer, txid = _next(app, pubkey, run_id)
    assert offer["piece"] == 3, "the pieces after it are still here to ask for"
    rpc.call("generate", 1)
    _catch_up(state, rpc)

    row = app.post("/account/run").json()["runs"][0]
    assert row["sent"] == 2 and row["items"] == 3 and row["refused"] == 1, row
    assert row["status"] == "failed", "a run with a hole in it is not finished"

    over = app.post("/account/run/piece", json={"run": run_id})
    assert over.status_code == 400, over.text
    assert "refused" in over.json()["detail"] and not over.json().get("finished"), \
        "asking for the next piece must not say the run is complete"

    # And it is still the run this account is on, which is what `failed` being
    # an unfinished status buys: a second collection beside a broken one would
    # come out of one address in whichever order the account clicked.
    another = _start(app, hashlips(tmp_path / "later", prefix="Doge Punks After"))
    assert another.status_code == 400
    assert "still going" in another.json()["detail"], another.json()


def test_retry_puts_a_refused_piece_back_and_the_same_piece_lands(seated,
                                                               tmp_path):
    """The button next to a refusal, and the one thing it cannot do.

    The piece keeps the inscription id it was written down with, so what comes
    back is the same piece rather than a second copy beside it. What the button
    cannot do is fix a reason that lives in the build, which is why the reason
    comes back with the count instead of a bare "1 retried" -- and why the
    second half of this test puts the file back before pressing it again.
    """
    app, state, rpc, pubkey, mine = seated
    build = hashlips(tmp_path, count=2, prefix="Doge Punks Mended")
    run_id = _start(app, build).json()["run"]
    missing = pathlib.Path(_book(state).get(run_id)["folder"]) / "images" / "2.png"
    bytes_ = missing.read_bytes()
    missing.unlink()

    _next(app, pubkey, run_id)
    rpc.call("generate", 1)
    _catch_up(state, rpc)
    assert app.post("/account/run/piece", json={"run": run_id}).status_code == 400

    retry = app.post("/account/run/retry", json={"run": run_id})
    assert retry.status_code == 200, retry.text
    assert retry.json()["retried"] == 1 and retry.json()["status"] == "running"
    assert retry.json()["refused"][0].startswith("Doge Punks Mended #2:"), \
        retry.json()

    assert app.post("/account/run/piece", json={"run": run_id}).status_code == 400, \
        "a missing file is not fixed by pressing the button again"

    missing.write_bytes(bytes_)
    assert app.post("/account/run/retry",
                    json={"run": run_id}).json()["retried"] == 1
    offer, txid = _next(app, pubkey, run_id)
    assert offer["piece"] == 2
    rpc.call("generate", 1)
    _catch_up(state, rpc)

    over = app.post("/account/run/piece", json={"run": run_id})
    assert over.status_code == 200 and over.json()["finished"], over.text
    assert over.json()["sent"] == 2
    assert app.post("/account/run").json()["runs"][0]["status"] == "done"
    index = state.token_index(state.chain_named("regtest"))
    assert index.collection_editions(mine, "Doge Punks Mended") == {1, 2}, \
        "two pieces, one each: the retry did not put #2 up twice"


def test_removing_a_run_takes_its_pictures_off_this_node_with_it(seated,
                                                                tmp_path):
    """What an account's departure deletes, and what it is not allowed to.

    Nothing on the chain is in reach of this -- there is no un-inscribing -- so
    what is deleted is the node's memory of the set plus the folder of pictures
    it kept to build the rest from. That folder is the part worth being careful
    about from both sides: it is the account's work sitting on a stranger's
    disk, and it is a path on a stranger's disk. So it goes when and only when
    the account's own run goes, and only inside this node's upload directory.

    A piece that is out for a signature holds the deletion up rather than the
    other way round, because the broadcast it finishes would come looking for
    rows that are gone. `Stop` is what says that signature is not coming, which
    is why stopping unlocks the removal.
    """
    app, state, rpc, pubkey, mine = seated
    run_id = _start(app, hashlips(tmp_path, count=2,
                                  prefix="Doge Punks Gone")).json()["run"]
    folder = pathlib.Path(_book(state).get(run_id)["folder"])
    assert folder.exists(), "the node kept the pictures it was given"
    _, landed = _next(app, pubkey, run_id)
    rpc.call("generate", 1)
    _catch_up(state, rpc)

    # Piece two is offered and never signed: the ordinary end of a run, a tab
    # that closed while the offer on it was still warm.
    assert app.post("/account/run/piece", json={"run": run_id}).status_code == 200
    early = app.post("/account/run/delete", json={"run": run_id})
    assert early.status_code == 400, early.text
    assert "signature" in early.json()["detail"], early.json()
    assert folder.exists() and app.post("/account/run").json()["runs"], \
        "a refusal deletes nothing"

    assert app.post("/account/run/stop",
                    json={"run": run_id}).json()["status"] == "stopped"
    gone = app.post("/account/run/delete", json={"run": run_id})
    assert gone.status_code == 200, gone.text
    assert gone.json()["forgotten"]
    assert app.post("/account/run").json()["runs"] == []
    assert not folder.exists(), "the pictures do not outlive the run"
    assert app.post("/account/run/delete",
                    json={"run": run_id}).status_code == 404

    index = state.token_index(state.chain_named("regtest"))
    assert index.inscription(landed)["collection"] == "Doge Punks Gone", \
        "what is on the chain is not this route's to delete"


def test_a_run_remembers_which_chain_it_belongs_to(seated, tmp_path):
    """The floor, on the row -- §1c's "a run belongs to a chain era".

    When the floor moves, everything below it is unread, and a run written
    before the move is a promise this node can no longer keep. That is only
    knowable afterwards if the height it was written at went in at the time.
    """
    app, state, rpc, pubkey, mine = seated
    run_id = _start(app, hashlips(tmp_path, count=2,
                                  prefix="Doge Punks Floored")).json()["run"]
    assert _book(state).get(run_id)["floor"] == \
        state.chain_named("regtest").params.activation_height


# --- the pages ----------------------------------------------------------------
#
# Everything above asks the routes; from here the questions are asked of a
# page. `collection_job.html` has had a page for the operator's runs since the
# beginning, and the account's side of the same book had a paragraph in the
# wallet page and six POST routes with one caller. These are the assertions
# that the paragraph is not all there is: which pieces are up, which one
# stopped it and why, and what a stranger is not told.


def test_the_run_page_is_the_account_s_own_run_page(seated, tmp_path):
    """The account's `/inscriptions/collection/{id}`, drawn from its own book.

    Mostly an assertion about what is on the page -- the run's own name, its
    own address, one row per piece -- and about the one thing that must not
    be: the folder on this machine where the pictures sit. The operator's page
    prints that path because the operator owns the machine. This one is on a
    public instance, where it is a directory on a stranger's disk, and a
    stranger's page has no use for it.
    """
    app, state, rpc, pubkey, mine = seated
    run_id = _start(app, hashlips(tmp_path, count=3,
                                  prefix="Doge Punks Shown")).json()["run"]

    page = app.get(f"/me/run/{run_id}")
    assert page.status_code == 200, page.text
    words = _flat(page.text)
    assert "Doge Punks Shown" in words
    assert "0 of 3 pieces on the chain" in words
    assert mine in page.text, "it says where the pieces come from"
    for edition in (1, 2, 3):
        assert f"Doge Punks Shown #{edition}" in page.text
    assert 'id="run-piece"' in page.text and "wallet.opened" in page.text, \
        "it asks the tab for a signature; it has no key of its own"
    assert "next is Doge Punks Shown #1" in words
    assert str(state.home) not in page.text and "images/1.png" not in page.text, \
        "the folder is not printed, only what it is for"


def test_the_run_page_links_the_pieces_the_chain_has(seated, tmp_path):
    """A landed piece is a number, not a hex prefix.

    The wallet page's panel says `Inscribed: ab12…`; what the account wants
    next is the piece itself, and it is only a link once this node has read
    the block that carries it. So half of this is the wait the page has no
    part in -- the other half is that the page cannot make the link early.
    """
    app, state, rpc, pubkey, mine = seated
    run_id = _start(app, hashlips(tmp_path, count=2,
                                  prefix="Doge Punks Landed")).json()["run"]
    _, landed = _next(app, pubkey, run_id)
    # The row's two states, word for word. Not the word "unconfirmed" on its
    # own: base.html's stylesheet has a rule by that name, so it is on every
    # page drawn or not.
    waiting = f"{landed[:16]}&hellip; unconfirmed"
    assert waiting in app.get(f"/me/run/{run_id}").text, \
        "a broadcast is not yet a page"
    assert f"/inscriptions/{landed}/view" not in app.get(
        f"/me/run/{run_id}").text, "and no link to a page that 404s"

    rpc.call("generate", 1)
    _catch_up(state, rpc)
    page = app.get(f"/me/run/{run_id}")
    assert f"/inscriptions/{landed}/view" in page.text
    assert "1 of 2 pieces on the chain" in _flat(page.text)
    assert "next is Doge Punks Landed #2" in _flat(page.text)
    assert waiting not in page.text


def test_a_refused_piece_is_on_the_page_with_the_reason_for_it(seated,
                                                               tmp_path):
    """The reason this page exists rather than another paragraph.

    A hole in a set is said at the moment it happens, in a tab that closes.
    A run that stopped for that reason keeps its place in the account's list
    until it is finished or thrown away, so the reason has to live on the
    run, where it is still there when looked at again next week.
    """
    app, state, rpc, pubkey, mine = seated
    run_id = _start(app, hashlips(tmp_path, count=2,
                                  prefix="Doge Punks Holed")).json()["run"]
    book = _book(state)
    for edition in (1, 2):
        book.piece_failed(run_id, edition, "that picture is not in the folder")

    page = app.get(f"/me/run/{run_id}")
    assert page.status_code == 200, page.text
    words = _flat(page.text)
    assert "2 refused" in words
    assert "that picture is not in the folder" in page.text
    assert "failed" in words, "a run with a hole in it does not read as finished"
    assert 'id="run-retry"' in page.text, "and the way back is on the page"
    assert 'id="run-piece"' not in page.text, "there is no next piece to offer"


def test_a_run_that_is_not_the_askers_says_nothing_about_it(seated, tmp_path):
    """The one place a run id is asked for by a stranger, so: one flat answer.

    The POST routes tell these two apart and have to -- refusing to touch a
    run and saying there is no such run are different sentences, and the
    account is paying for one of them. This is a page on a public instance,
    where the difference is an answer about somebody else's work.
    """
    app, state, rpc, pubkey, mine = seated
    run_id = _start(app, hashlips(tmp_path, count=1,
                                  prefix="Doge Punks Sealed")).json()["run"]
    _sign_in(app)                          # a different key, the same tab

    for path in (f"/me/run/{run_id}", "/me/run/0123456789ab"):
        answer = app.get(path, follow_redirects=False)
        assert answer.status_code == 303, path
        assert answer.headers["location"] == "/me/runs"
        landed = app.get("/me/runs")
        assert "there is no run of that id" in _flat(landed.text), path
        assert "Doge Punks Sealed" not in landed.text, \
            "not even the name of somebody else's collection"


def test_the_runs_page_is_the_list_of_what_was_written_down(seated, tmp_path):
    """Everything this account started, including what it walked away from.

    The wallet page's panel shows the one run it is currently on. The question
    this page answers is the other one -- what have I got half-done on this
    node -- which includes the stopped ones, because a stopped run still has
    its pictures here and still holds the coins its unsent pieces were priced
    at.
    """
    app, state, rpc, pubkey, mine = seated
    empty = app.get("/me/runs")
    assert empty.status_code == 200, empty.text
    assert "Nothing written down yet" in _flat(empty.text)

    left = _start(app, hashlips(tmp_path, count=2,
                                prefix="Doge Punks Left")).json()["run"]
    assert app.post("/account/run/stop",
                    json={"run": left}).json()["status"] == "stopped"
    going = _start(app, hashlips(tmp_path / "again", count=1,
                                 prefix="Doge Punks Going"))
    assert going.status_code == 200, going.text

    page = app.get("/me/runs")
    words = _flat(page.text)
    assert "Doge Punks Left" in words and "Doge Punks Going" in words
    assert f'href="/me/run/{left}"' in page.text
    assert f'href="/me/run/{going.json()["run"]}"' in page.text
    assert "stopped" in words, "and it says which one was abandoned"
    # The book keeps the network because that is what finds the chain again;
    # the column is for reading, and "regtest" is not what anyone calls it.
    assert state.messaging.label in words
    assert ">regtest<" not in page.text, "the chain column prints the id, not the name"
    assert "Nothing written down yet" not in page.text


def test_the_run_pages_are_ones_a_public_node_will_reach(client):
    """A page the menu links and the door refuses is a page that lies.

    `/me/run/` had to be a shape and not a tree, and that is only safe because
    of what stands behind the shape: the route answers a run that is not the
    asker's with the same words it answers a run that is not there, and there
    is nothing under that prefix that spends, names a folder, or reads
    anybody but the asker's own rows. The last two assertions are the tree
    that was NOT taken -- an added path under `/me/run/` arrives private.
    """
    from arcade.web import door

    app, _ = client
    assert door.public_path("/me/runs")
    assert door.public_path("/me/run/0123456789ab")
    assert not door.public_path("/me/run/0123456789ab/export")
    assert not door.public_path("/me/run/0123456789abcdef0123456789abcdef0123")


def test_a_run_waits_for_a_block_before_the_chain_is_too_long(seated, tmp_path,
                                                              monkeypatch):
    """2026-09-30: a 462-piece run stalled at 26 pieces with
    "too-long-mempool-chain": each piece spends the change of the one before,
    and the network holds 25 unconfirmed in a row. The next piece is now held
    back with the wording the run page already waits on, and nothing charged."""
    from arcade import rpc as rpclib

    app, state, rpc, pubkey, mine = seated
    started = _start(app, hashlips(tmp_path, count=3, prefix="Chain Room Punks"))
    assert started.status_code == 200, started.text
    run = started.json()
    _next(app, pubkey, run["run"])                    # in the mempool, no block

    real = rpclib.RpcClient.call

    def deep(self, method, *args):
        if method == "getmempoolentry":
            return {"ancestorcount": 24, "descendantcount": 1}
        return real(self, method, *args)

    monkeypatch.setattr(rpclib.RpcClient, "call", deep)
    held = app.post("/account/run/piece", json={"run": run["run"]})
    assert held.status_code == 400
    assert "again in 1 minute" in held.json()["detail"], held.text
    monkeypatch.setattr(rpclib.RpcClient, "call", real)
    rpc.call("generate", 1)
    _catch_up(state, rpc)
    offer, _ = _next(app, pubkey, run["run"])
    assert offer["piece"] == 2, "the same piece, after the block"


def test_asking_again_for_the_same_piece_is_not_charged_again(seated, tmp_path):
    """2026-09-30: a stalled run re-asked for the same piece hundreds of times
    and was charged for every ask, until the hour's allowance was gone with
    about 60 pieces on the chain. A piece is charged when it is first offered."""
    app, state, rpc, pubkey, mine = seated
    started = _start(app, hashlips(tmp_path, count=2, prefix="Charged Once Punks"))
    assert started.status_code == 200, started.text
    run = started.json()["run"]

    def charged():
        return state.accounts().conn.execute(
            "SELECT COUNT(*) FROM deed WHERE kind = 'inscribe'").fetchone()[0]

    before = charged()
    for _ in range(3):
        asked = app.post("/account/run/piece", json={"run": run})
        assert asked.status_code == 200, asked.text
        assert asked.json()["piece"] == 1, "the same piece each time"
    assert charged() == before + 1, "one piece, one charge"

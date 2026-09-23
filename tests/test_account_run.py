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

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_claim import _catch_up                            # noqa: E402
from test_account_inscribe import arcade, seated                    # noqa: E402,F401
from test_account_inscribe import _sign_and_send                    # noqa: E402
from test_collections import hashlips                               # noqa: E402
from test_web import app_state, client                              # noqa: E402,F401

from arcade import collections as collectionlib                      # noqa: E402

COIN = 100_000_000


def _start(app, build, **fields):
    """Upload a build folder the way a browser sends a chosen folder."""
    files = [("files", (path.name, path.read_bytes()))
             for path in sorted(build.rglob("*")) if path.is_file()]
    return app.post("/account/run/start", files=files,
                    data={"run_chain": "regtest", **fields})


def _next(app, pubkey, run_id):
    """Ask for the next piece, sign it, let the node take it."""
    asked = app.post("/account/run/piece", json={"run": run_id})
    assert asked.status_code == 200, asked.text
    offer = asked.json()
    done = _sign_and_send(app, pubkey, offer)
    assert done.status_code == 200, done.text
    return offer, done.json()["txid"]


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
    """
    app, state, rpc, pubkey, mine = seated
    run_id = _start(app, hashlips(tmp_path, count=1)).json()["run"]

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


def test_a_build_needing_two_transactions_for_one_item_is_refused(seated, tmp_path):
    """Refused at the upload, naming the item, with nothing paid for.

    The alternative is a run that inscribes forty pieces and stops on the
    forty-first because it cannot be built -- half a collection, the money
    spent, and no way to finish it.
    """
    app, state, rpc, pubkey, mine = seated
    answer = _start(app, hashlips(tmp_path, count=2, sizes={2: 30_000}))
    assert answer.status_code == 400, answer.text
    assert "Doge Punks #2" in answer.json()["detail"]
    assert rpc.call("getrawmempool") == []
    assert app.get("/account").json()["balance"] == int(4.0 * COIN), \
        "a refusal costs nothing"


def test_a_run_an_operator_closed_costs_nothing(seated, tmp_path):
    """The dial that paces a run is the one-piece dial, not a waiver for it.

    A hundred pieces at ten an hour takes ten hours, and that is the operator's
    number doing its job on the one action this node carries forever. Closing
    the dial closes runs with it, which is the honest reading of the sentence
    on the Overview page.
    """
    app, state, rpc, pubkey, mine = seated
    run_id = _start(app, hashlips(tmp_path, count=2)).json()["run"]
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
    run_id = _start(app, hashlips(tmp_path, count=2)).json()["run"]
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
    build = hashlips(tmp_path, count=2)
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
    run_id = _start(app, hashlips(tmp_path, count=2)).json()["run"]
    assert run_id
    assert collectionlib.Jobs(state.home / "collections.sqlite").list() == [], \
        "an account's run appeared in the book the node's Runner resumes"

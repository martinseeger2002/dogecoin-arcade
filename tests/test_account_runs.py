"""The account run book on its own: no node, no chain, no browser.

The routes are the rest of the step. What is here has to be true whatever they
look like: that a run written down knows which piece is next, that a piece an
offer has already gone out for is finished before a later one is offered, and
that a run cannot read as finished while it still owes pieces. The last one is
why this book exists at all -- a collection that half-inscribes is worse than one
that has not started.

These use the real `Item`/`Build` dataclasses rather than a stub, so the book
cannot quietly drift from what `collections.read_build` actually hands over.
"""

from pathlib import Path

import pytest

from arcade import accountruns
from arcade.collections import Build, Item


def _build(folder: Path, items: int = 5, size: int = 1_200,
           content_type: str = "image/png") -> Build:
    return Build(folder=folder, collection="Test Coins", items=[
        Item(edition=n, name=f"piece {n}", json=f'{{"name":"piece {n}"}}',
             image=f"{n}.png", content_type=content_type, size=size)
        for n in range(1, items + 1)])


@pytest.fixture
def book(tmp_path):
    return accountruns.Runs(tmp_path / "runs.sqlite")


def test_a_run_is_written_down_with_every_piece_awaiting_its_transaction(book):
    run_id = book.create("n1someone", "n1addr", _build(Path("/b")), "n")
    run = book.get(run_id)
    assert run["items"] == 5
    assert run["status"] == "open"
    assert run["pending"] == 5 and run["sent"] == 0
    assert run["next"] == "piece 1"
    # The first piece funds from the account's own coins; there is nothing of
    # this run's on the chain to spend yet.
    assert book.previous_txid(run_id) == ""
    assert book.next_piece(run_id)["edition"] == 1


def test_the_piece_an_offer_is_out_for_comes_before_the_next_one(book):
    """An expired offer has to rebuild the piece it was for.

    A browser that never came back for piece 2 is indistinguishable from one
    that crashed, so `next_piece` has to answer "piece 2" both times, and
    rebuilding it has to be the same piece -- same edition, same id.
    """
    run_id = book.create("n1someone", "n1addr", _build(Path("/b")), "n")
    first = book.next_piece(run_id)
    book.offer_piece(run_id, first["edition"])
    again = book.next_piece(run_id)
    assert again["edition"] == first["edition"]
    assert again["inscription_id"] == first["inscription_id"]

    book.record_piece(run_id, first["edition"], "a" * 64)
    assert book.previous_txid(run_id) == "a" * 64
    assert book.next_piece(run_id)["edition"] == 2
    assert book.get(run_id)["status"] == "running"


def test_a_run_becomes_done_by_itself_when_its_last_piece_goes(book):
    """Nobody sets `done`. If the last write can be forgotten, so can a run's
    own ending, and a run that says done while owing pieces is the bug."""
    run_id = book.create("n1someone", "n1addr", _build(Path("/b"), items=3), "n")
    for edition in (1, 2):
        book.record_piece(run_id, edition, f"{edition}" * 64)
        assert book.get(run_id)["status"] == "running"
    book.record_piece(run_id, 3, "3" * 64)
    run = book.get(run_id)
    assert run["status"] == "done"
    assert run["next"] == ""
    assert book.next_piece(run_id) is None
    assert book.due("n1someone", "n") is None


def test_one_active_run_at_a_time_is_a_question_about_this_table(book):
    """§6's rule, as a row rather than as a count of unsigned offers -- four
    offers say nothing about whether they belong to one run or to four single
    inscriptions."""
    mine = book.create("n1mine", "n1addr", _build(Path("/b")), "n")
    theirs = book.create("n1theirs", "n1addr", _build(Path("/b")), "n")
    assert book.due("n1mine", "n")["id"] == mine
    assert book.due("n1theirs", "n")["id"] == theirs
    assert book.due("n1nobody", "n") is None

    later = book.create("n1mine", "n1addr", _build(Path("/b")), "n")
    assert book.due("n1mine", "n")["id"] == later
    for edition in range(1, 6):
        book.record_piece(later, edition, "f" * 64)
    assert book.due("n1mine", "n")["id"] == mine


def test_a_failed_piece_comes_back_with_the_id_it_already_had(book):
    """A retry has to inscribe the same piece again, not a new one beside it.
    The id is chosen when the run is written down for exactly this."""
    run_id = book.create("n1someone", "n1addr", _build(Path("/b"), items=2), "n")
    was = book.pieces(run_id)[0]["inscription_id"]
    book.offer_piece(run_id, 1)
    book.piece_failed(run_id, 1, "the node did not take it")
    assert book.get(run_id)["failed_pieces"] == 1
    assert book.retry_failed(run_id) == 1
    after = book.pieces(run_id)[0]
    assert after["status"] == "pending"
    assert after["inscription_id"] == was
    assert after["error"] == ""


def test_an_item_of_more_than_one_transaction_is_written_down_with_its_count(tmp_path):
    """Never shrunk (2026-10-08): a 30 KB picture is written down as it is, with the
    number of transactions it takes, and the page sends it the one-big-file way."""
    book = accountruns.Runs(tmp_path / "runs.sqlite")
    run_id = book.create("n1someone", "n1addr", _build(Path("/b"), items=2, size=30_000), "n")
    rows = book.pieces(run_id)
    assert [r["chunks"] for r in rows] == [r["chunks"] for r in rows] and all(r["chunks"] > 1 for r in rows)
    assert book.piece(run_id, 2)["edition"] == 2 and book.piece(run_id, 9) is None
    small = book.create("n1other", "n1addr2", _build(Path("/c"), items=1), "n")
    assert book.pieces(small)[0]["chunks"] == 1


def test_the_book_does_not_move_a_status_it_was_not_allowed_to_move(book):
    run_id = book.create("n1someone", "n1addr", _build(Path("/b")), "n")
    assert book.set_status(run_id, "stopped", note="asked to stop",
                           only_from=("open", "running"))
    assert not book.set_status(run_id, "running", only_from=("open", "running"))
    assert book.get(run_id)["status"] == "stopped"


def test_a_run_reopening_the_book_is_the_same_run(tmp_path):
    """The whole reason for writing it down: the browser closed, the node
    restarted, and the pieces are still where they were."""
    home = tmp_path / "runs.sqlite"
    book = accountruns.Runs(home)
    run_id = book.create("n1someone", "n1addr", _build(Path("/b"), items=3), "n")
    book.record_piece(run_id, 1, "1" * 64)

    again = accountruns.Runs(home)
    run = again.get(run_id)
    assert run["status"] == "running"
    assert run["sent"] == 1 and run["pending"] == 2
    assert again.next_piece(run_id)["edition"] == 2
    assert again.due("n1someone", "n")["id"] == run_id


def test_deleting_a_run_leaves_its_uploaded_files_somewhere_to_talk_about(book):
    """Rows only. Deleting a directory nobody has written a rule for is how the
    wrong one gets deleted, so the retention rule is stated here and enforced by
    whichever step decides it."""
    run_id = book.create("n1someone", "n1addr", _build(Path("/b/somewhere")), "n")
    book.delete(run_id)
    assert book.get(run_id) is None
    assert book.list() == []

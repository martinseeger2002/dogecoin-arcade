"""A HashLips build, read, priced, written down and sent -- with a pause, a
resume and a crash in the middle -- against a sender that only pretends."""

import dataclasses
import json
import threading
import time

import pytest

from arcade import collections as C
from arcade import inscribe
from arcade import inscriptions as I
from arcade.web.state import SendQueue


def hashlips(tmp_path, count=3, prefix="Doge Punks", sizes=None):
    """What the Art Engine leaves in `build/`."""
    build = tmp_path / "build"
    (build / "json").mkdir(parents=True)
    (build / "images").mkdir()
    metadata = []
    for edition in range(1, count + 1):
        item = {
            "name": f"{prefix} #{edition}", "description": "test set",
            "image": f"ipfs://NewUriToReplace/{edition}.png",
            "dna": f"dna{edition}", "edition": edition, "date": 1690000000000 + edition,
            "attributes": [{"trait_type": "Background", "value": "Blue" if edition % 2 else "Red"},
                           {"trait_type": "Hat", "value": f"hat{edition}"}],
            "compiler": "HashLips Art Engine",
        }
        metadata.append(item)
        (build / "json" / f"{edition}.json").write_text(json.dumps(item, indent=2))
        size = (sizes or {}).get(edition, 3000 + edition)
        (build / "images" / f"{edition}.png").write_bytes(
            b"\x89PNG" + bytes([edition]) * (size - 4))
    (build / "json" / "_metadata.json").write_text(json.dumps(metadata, indent=2))
    return build


# --- reading ------------------------------------------------------------------

def test_a_build_folder_is_read_item_by_item(tmp_path):
    build = C.read_build(hashlips(tmp_path))
    assert [i.edition for i in build.items] == [1, 2, 3]
    assert build.collection == "Doge Punks"
    assert build.items[0].name == "Doge Punks #1"
    assert build.items[0].image == "images/1.png"
    assert build.items[0].content_type == "image/png"
    assert build.items[0].size == 3001
    assert build.problems == []


def test_the_item_json_goes_up_as_it_is_compacted(tmp_path):
    build = C.read_build(hashlips(tmp_path))
    original = json.loads((build.folder / "json" / "2.json").read_text())
    # Same data but for the IPFS pointer, which names a place the picture is
    # not: here the picture IS the inscription (D-100).
    wanted = {k: v for k, v in original.items() if k != "image"}
    assert json.loads(build.items[1].json) == wanted, "same data, minus the pointer"
    assert build.items[1].json == json.dumps(wanted, separators=(",", ":")), \
        "no whitespace paid for"
    assert I.collection_of(build.items[1].json) == ("Doge Punks", 2, "Doge Punks #2")


def test_the_folder_can_be_pointed_at_loosely(tmp_path):
    build = hashlips(tmp_path)
    for where in (build, build / "json", build / "json" / "_metadata.json", tmp_path):
        assert C.find_build(where) == build, where


def test_without_the_combined_file_the_per_item_files_serve(tmp_path):
    build = hashlips(tmp_path)
    (build / "json" / "_metadata.json").unlink()
    read = C.read_build(build)
    assert [i.edition for i in read.items] == [1, 2, 3]


def test_a_missing_image_is_a_problem_shown_not_a_gap_inscribed(tmp_path):
    build = hashlips(tmp_path)
    (build / "images" / "2.png").unlink()
    read = C.read_build(build)
    assert [i.edition for i in read.items] == [1, 3]
    assert any("edition 2" in p for p in read.problems)


def test_not_a_build(tmp_path):
    with pytest.raises(C.CollectionError):
        C.read_build(tmp_path)


def test_the_estimate_is_the_sum_of_the_items(tmp_path):
    build = C.read_build(hashlips(tmp_path))
    cost = C.estimate_build(build)
    parts = [inscribe.estimate(i.size, i.content_type, i.json) for i in build.items]
    assert cost["chunks"] == sum(p.chunks for p in parts) == 3
    assert cost["total"] == pytest.approx(sum(p.total for p in parts))


# --- the store ----------------------------------------------------------------

def test_a_job_is_written_down_with_an_id_per_item(tmp_path):
    build = C.read_build(hashlips(tmp_path))
    jobs = C.Jobs(tmp_path / "collections.sqlite")
    job_id = jobs.create("regtest", "nSender", build)
    job = jobs.get(job_id)
    assert job["status"] == "paused" and job["items"] == 3 and job["chunks"] == 3
    assert job["name"] == "Doge Punks"
    items = jobs.items(job_id)
    assert len({i["inscription_id"] for i in items}) == 3
    assert all(len(i["inscription_id"]) == 16 for i in items)
    assert jobs.next_item(job_id)["edition"] == 1


def test_a_half_sent_item_comes_before_an_unstarted_one(tmp_path):
    build = C.read_build(hashlips(tmp_path))
    jobs = C.Jobs(tmp_path / "collections.sqlite")
    job_id = jobs.create("regtest", "nSender", build)
    jobs.item_status(job_id, 1, "sent")
    jobs.record_piece(job_id, 3, 0, "tx3", manifest=True)
    assert jobs.next_item(job_id)["edition"] == 3
    assert jobs.get(job_id)["sent_chunks"] == 1
    assert jobs.items(job_id)[2]["txid"] == "tx3"


# --- the runner ---------------------------------------------------------------

class FakeSender:
    """Accepts every piece, remembers what it was handed, never waits."""

    def __init__(self):
        self.sent: list[bytes] = []
        self.splits = 0
        self.outputs = 1000
        self.lock = threading.Lock()
        self.rpc = self                # get_block_count, like the real one's node

    height = 100

    def get_block_count(self):
        return self.height

    def spendable_outputs(self, address, at_least=0, minconf=0):
        return self.outputs

    def ensure_outputs(self, address, wanted, each_sats=0, on_progress=None):
        self.splits += 1
        self.outputs = wanted
        return True

    def send_all(self, address, payloads, approve=None, on_broadcast=None,
                 on_progress=None):
        from arcade.messaging.sender import PartialSend
        txids = []
        for n, payload in enumerate(payloads, 1):
            if approve is not None and not approve(n, len(payloads), None):
                if txids:
                    raise PartialSend("stopped", txids, len(payloads))
                return []
            txid = f"tx{len(self.sent):04d}"
            with self.lock:
                self.sent.append(payload)
            txids.append(txid)
            if on_broadcast:
                on_broadcast(n, len(payloads), txid)
        return txids


class FakeChain:
    params = None

    class _Rpc:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def rpc(self): return self._Rpc()


class FakeIndex:
    def __init__(self): self.seen = {}; self.complete = set()
    def chunks_seen(self, sender, inscription_id):
        return self.seen.get(inscription_id, [])
    def inscription(self, txid):
        return {"txid": txid} if txid in self.complete else None


def runner_for(tmp_path, sender, index=None):
    jobs = C.Jobs(tmp_path / "collections.sqlite")
    runner = C.Runner(jobs, chain_for=lambda n: FakeChain(),
                      index_for=lambda n: index or FakeIndex(),
                      make_sender=lambda rpc, params: sender)
    return jobs, runner


def wait(runner, job_id, timeout=20):
    thread = runner._threads.get(job_id)
    if thread:
        thread.join(timeout)
    assert not runner.running(job_id), "the job did not finish"


def expected_payloads(build, jobs, job_id):
    """Every piece the collection should put on the chain, by item."""
    out = {}
    for item in jobs.items(job_id):
        content = (build.folder / item["image"]).read_bytes()
        plan = inscribe.plan(content, item["content_type"], item["json"],
                             inscription_id=bytes.fromhex(item["inscription_id"]))
        out[item["edition"]] = plan.payloads
    return out


def test_a_job_runs_to_done_and_every_piece_is_written_down(tmp_path):
    # Sizes that make items 2 and 3 multi-piece, so countdowns matter.
    build = C.read_build(hashlips(tmp_path, sizes={2: 20_000, 3: 9_000}))
    sender = FakeSender()
    jobs, runner = runner_for(tmp_path, sender)
    job_id = jobs.create("regtest", "nSender", build)

    assert runner.start(job_id)
    assert not runner.start(job_id), "already running"
    wait(runner, job_id)

    job = jobs.get(job_id)
    assert job["status"] == "done", job
    assert job["sent"] == 3 and job["pending"] == 0
    wanted = expected_payloads(build, jobs, job_id)
    assert sorted(sender.sent) == sorted(p for ps in wanted.values() for p in ps)
    assert job["sent_chunks"] == len(sender.sent) == 3 + 2 + 1 or job["sent_chunks"] == len(sender.sent)
    for item in jobs.items(job_id):
        pieces = json.loads(item["txids"])
        assert len(pieces) == item["chunks"]
        assert item["txid"] == pieces[str(item["chunks"] - 1)], "named by the manifest piece"
        assert item["status"] == "sent"


def test_pause_stops_between_pieces_and_resume_sends_only_the_rest(tmp_path):
    build = C.read_build(hashlips(tmp_path, count=4, sizes={2: 20_000}))
    sender = FakeSender()
    jobs, runner = runner_for(tmp_path, sender)
    job_id = jobs.create("regtest", "nSender", build)

    # Pause from inside the second broadcast, the way a person would mid-run.
    real = sender.send_all
    def pausing(address, payloads, approve=None, on_broadcast=None, on_progress=None):
        def hook(n, total, txid):
            on_broadcast(n, total, txid)
            if len(sender.sent) == 2:
                runner.pause(job_id)
        return real(address, payloads, approve=approve, on_broadcast=hook)
    sender.send_all = pausing

    runner.start(job_id)
    wait(runner, job_id)
    job = jobs.get(job_id)
    assert job["status"] == "paused", job
    assert len(sender.sent) == 2
    assert job["sent_chunks"] == 2
    assert jobs.next_item(job_id)["edition"] == 2, "half sent, so it goes next"

    sender.send_all = real
    runner.start(job_id)
    wait(runner, job_id)
    job = jobs.get(job_id)
    assert job["status"] == "done"
    wanted = expected_payloads(build, jobs, job_id)
    assert sorted(sender.sent) == sorted(p for ps in wanted.values() for p in ps), \
        "nothing sent twice, nothing missed"


def test_a_pause_while_waiting_for_the_lane_gives_the_lane_back(tmp_path, monkeypatch):
    """Pausing in that gap used to lock a wallet out of its own sends.

    `_hold_send_lock` waits by asking for the lane over and over, so a run told
    to pause while it waits still gets the lane the instant somebody else's send
    finishes -- and it used to return right there, holding it, because the stop
    check sat between taking the lane and the `try` that hands it back. After
    that nothing else this wallet sends could go, and resuming the job waited
    for a lane the job itself had taken. Pressing Pause on a run while a message
    is going out is exactly when this lands, and waking the sleep out of the
    wait made it likelier, not less.

    The lane is the real `SendQueue` and not a pair of counters, so what gets
    asserted is `held()` -- the same call a busy page makes, through the same
    begin and end the app wires into a run.
    """
    build = C.read_build(hashlips(tmp_path, count=3))
    sender = FakeSender()
    sends = SendQueue()
    assert sends.begin() is True, "another send is in flight"
    asks: list[int] = []

    def begin():
        asks.append(1)
        got = sends.begin()
        if got and len(asks) > 1:
            runner.pause(job_id)        # the pause lands as the lane is taken
        return got

    jobs = C.Jobs(tmp_path / "collections.sqlite")
    runner = C.Runner(jobs, chain_for=lambda n: FakeChain(),
                      index_for=lambda n: FakeIndex(),
                      send_lock=(begin, sends.end),
                      make_sender=lambda rpc, params: sender)
    job_id = jobs.create("regtest", "nSender", build)
    monkeypatch.setattr(C, "POLL", 0.02)
    runner.start(job_id)
    deadline = time.time() + 5
    while "waiting for another send" not in jobs.get(job_id)["note"] and time.time() < deadline:
        time.sleep(0.01)
    assert "waiting for another send" in jobs.get(job_id)["note"]
    assert sends.held() and sender.sent == [], "it is waiting, not sending"

    sends.end()                          # the other send finishes
    wait(runner, job_id)
    assert jobs.get(job_id)["status"] == "paused"
    assert sends.held() == [], "the run gave the lane back on the way out"
    assert sends.begin() is True, "and nobody holds it"
    sends.end()


def test_a_crash_is_resumed_on_the_next_start_without_resending(tmp_path):
    build = C.read_build(hashlips(tmp_path, count=3, sizes={1: 20_000}))
    sender = FakeSender()
    index = FakeIndex()
    jobs, runner = runner_for(tmp_path, sender, index)
    job_id = jobs.create("regtest", "nSender", build)
    wanted = expected_payloads(build, jobs, job_id)

    # What the store looks like after a crash: the job says running, item 1
    # has its first two pieces written down, and a THIRD went out in the
    # seconds before the lights went off and was never recorded -- but the
    # index saw it.
    item = jobs.items(job_id)[0]
    last = item["chunks"] - 1
    assert last >= 2
    jobs.set_status(job_id, "running")
    jobs.record_piece(job_id, 1, last, "tx-a", manifest=True)
    jobs.record_piece(job_id, 1, last - 1, "tx-b", manifest=False)
    index.seen[item["inscription_id"]] = [{"countdown": last - 2, "txid": "tx-c",
                                          "block_height": 5}]

    assert runner.resume_interrupted() == [job_id]
    wait(runner, job_id)
    assert jobs.get(job_id)["status"] == "done"
    already = set(wanted[1][:3])          # countdowns last, last-1, last-2
    assert not (already & set(sender.sent)), "pieces already out were not sent again"
    assert set(sender.sent) == (set(p for ps in wanted.values() for p in ps) - already)


def test_a_finished_item_the_index_knows_is_not_sent_at_all(tmp_path):
    build = C.read_build(hashlips(tmp_path, count=2))
    sender = FakeSender()
    index = FakeIndex()
    jobs, runner = runner_for(tmp_path, sender, index)
    job_id = jobs.create("regtest", "nSender", build)
    jobs.record_piece(job_id, 1, 0, "tx-done", manifest=True)
    index.complete.add("tx-done")

    runner.start(job_id)
    wait(runner, job_id)
    assert jobs.get(job_id)["status"] == "done"
    assert len(sender.sent) == 1, "only item 2"


def test_a_failing_item_does_not_stop_the_others(tmp_path):
    build = C.read_build(hashlips(tmp_path, count=3))
    sender = FakeSender()
    jobs, runner = runner_for(tmp_path, sender)
    job_id = jobs.create("regtest", "nSender", build)
    (build.folder / "images" / "2.png").unlink()      # gone between pricing and sending

    runner.start(job_id)
    wait(runner, job_id)
    job = jobs.get(job_id)
    assert job["status"] == "failed" and job["failed_items"] == 1 and job["sent"] == 2
    assert jobs.items(job_id)[1]["error"]


def test_the_wallet_is_split_ahead_when_it_runs_short(tmp_path):
    build = C.read_build(hashlips(tmp_path, count=5))
    sender = FakeSender()
    sender.outputs = 0
    jobs, runner = runner_for(tmp_path, sender)
    job_id = jobs.create("regtest", "nSender", build)
    runner.start(job_id)
    wait(runner, job_id)
    assert jobs.get(job_id)["status"] == "done"
    assert sender.splits == 1, "one split for the batch, not one per item"


def test_the_send_lock_is_taken_per_item(tmp_path):
    build = C.read_build(hashlips(tmp_path, count=3))
    sender = FakeSender()
    jobs = C.Jobs(tmp_path / "collections.sqlite")
    held = []
    runner = C.Runner(jobs, chain_for=lambda n: FakeChain(), index_for=lambda n: FakeIndex(),
                      send_lock=(lambda: held.append(1) or True, lambda: held.pop()),
                      make_sender=lambda rpc, params: sender)
    job_id = jobs.create("regtest", "nSender", build)
    runner.start(job_id)
    wait(runner, job_id)
    assert held == [], "released after every item"


def test_a_refusal_by_the_node_pauses_the_job_and_resume_goes_on(tmp_path, monkeypatch):
    """One afternoon on testnet: pieces confirmed one a block, the wallet's
    change stayed unconfirmed, the third piece to spend it was refused
    (-26 too-long-mempool-chain) -- and so was every item after it, 39 in a
    row, each marked failed for good. The node refusing a piece is the
    job's problem, not the item's: pause on it, and send the rest on Resume."""
    build = C.read_build(hashlips(tmp_path, count=4))
    sender = FakeSender()
    refusing = {"on": True}
    real_send_all = sender.send_all

    def send_all(address, payloads, approve=None, on_broadcast=None, on_progress=None):
        if refusing["on"] and len(sender.sent) >= 1:
            raise RuntimeError("sendrawtransaction: [-26] 64: too-long-mempool-chain")
        return real_send_all(address, payloads, approve=approve, on_broadcast=on_broadcast)

    sender.send_all = send_all
    monkeypatch.setattr(C, "FUND_WAIT", 0)     # no block is coming in this test
    monkeypatch.setattr(C, "POLL", 0.01)
    jobs, runner = runner_for(tmp_path, sender)
    job_id = jobs.create("regtest", "nSender", build)
    runner.start(job_id)
    wait(runner, job_id)
    job = jobs.get(job_id)
    assert job["status"] == "paused" and "too-long-mempool-chain" in job["error"]
    assert job["failed_items"] == 0 and job["sent"] == 1
    second = jobs.items(job_id)[1]
    assert second["status"] == "pending" and "too-long-mempool-chain" in second["error"]

    refusing["on"] = False
    runner.start(job_id)
    wait(runner, job_id)
    assert jobs.get(job_id)["status"] == "done" and jobs.get(job_id)["sent"] == 4
    assert len(sender.sent) == 4, "the first item was not sent twice"

    # A job from before this, with items marked failed: Resume tries them again.
    jobs.item_status(job_id, 3, "failed", "sendrawtransaction: [-26] 64: too-long-mempool-chain")
    jobs.item_status(job_id, 4, "failed", "sendrawtransaction: [-26] 64: too-long-mempool-chain")
    jobs.set_status(job_id, "failed", note="2 item(s) could not be sent")
    runner.start(job_id)
    wait(runner, job_id)
    assert jobs.get(job_id)["status"] == "done"
    assert len(sender.sent) == 4, "their recorded pieces are reused, not paid for again"


def test_a_chain_that_never_produces_a_block_pauses_rather_than_chains(
        tmp_path, monkeypatch):
    """One of the two waits, and it says which one it was: no block at all."""
    build = C.read_build(hashlips(tmp_path, count=2))
    sender = FakeSender()
    sender.outputs = 0
    sender.spendable_outputs = lambda address, at_least=0, minconf=0: 5 if minconf == 0 else 0
    jobs, runner = runner_for(tmp_path, sender)
    monkeypatch.setattr(C, "FUND_WAIT", 0)
    monkeypatch.setattr(C, "POLL", 0.01)
    job_id = jobs.create("regtest", "nSender", build)
    runner.start(job_id)
    wait(runner, job_id)
    job = jobs.get(job_id)
    assert job["status"] == "paused" and "no block at all" in job["error"]
    assert sender.sent == [] and sender.splits == 0, "nothing sent on top of the unconfirmed"


def test_blocks_passing_without_our_pieces_is_a_different_fault(tmp_path, monkeypatch):
    """The other wait, and the reason it is counted in blocks.

    A wall clock cannot tell "the chain is slow" from "the chain is moving
    and our pieces are not in it". The first is waiting; the second is a fee
    too low for the mempool as it stands, and saying "resume when the chain
    has moved" to somebody whose chain has moved ten blocks helps nobody
    (D-127).
    """
    build = C.read_build(hashlips(tmp_path, count=2))
    sender = FakeSender()
    sender.outputs = 0
    sender.spendable_outputs = lambda address, at_least=0, minconf=0: 5 if minconf == 0 else 0

    # The chain is alive and busy: a block every time anybody looks.
    def rising():
        sender.height += 1
        return sender.height

    sender.rpc.get_block_count = rising
    jobs, runner = runner_for(tmp_path, sender)
    monkeypatch.setattr(C, "POLL", 0.01)
    monkeypatch.setattr(C, "POLL_MAX", 0.01)
    job_id = jobs.create("regtest", "nSender", build)
    runner.start(job_id)
    wait(runner, job_id)
    job = jobs.get(job_id)
    assert job["status"] == "paused"
    assert "blocks have come" in job["error"] and "fee is too low" in job["error"]
    assert "no block at all" not in job["error"], "the other fault, and not this one"
    assert sender.sent == [] and sender.splits == 0


def test_pausing_a_run_that_is_waiting_for_a_block_does_not_wait_with_it(tmp_path,
                                                                         monkeypatch):
    """A minute asleep is not a minute paused.

    Between two pieces a run is asleep, and the gaps double to a minute. A
    pause that only took effect when the sleep happened to end was a dead
    button for a minute -- and a minute is more than either shutdown or a test
    teardown is willing to give, so a run that ignored its own stop flag was a
    thread that outlived the process that started it (2026-09-22).
    """
    build = C.read_build(hashlips(tmp_path, count=3))
    sender = FakeSender()
    real_send_all = sender.send_all
    refusals = []

    def send_all(address, payloads, approve=None, on_broadcast=None, on_progress=None):
        if len(sender.sent) >= 1 and sender.height == 100:
            refusals.append(len(sender.sent))
            raise RuntimeError("sendrawtransaction: [-26] 64: too-long-mempool-chain")
        return real_send_all(address, payloads, approve=approve,
                             on_broadcast=on_broadcast)

    sender.send_all = send_all
    monkeypatch.setattr(C, "POLL", 30.0)          # nobody sleeps a minute in a test
    monkeypatch.setattr(C, "POLL_MAX", 30.0)
    jobs, runner = runner_for(tmp_path, sender)
    job_id = jobs.create("regtest", "nSender", build)
    runner.start(job_id)
    thread = runner._threads[job_id]
    deadline = time.time() + 5
    while ("waiting for a block" not in jobs.get(job_id)["note"]
           and time.time() < deadline):
        time.sleep(0.01)
    assert "waiting for a block" in jobs.get(job_id)["note"], jobs.get(job_id)["note"]
    time.sleep(0.3)
    assert thread.is_alive(), "with a 30s gap to the next ask it is asleep, not working"

    runner.pause(job_id)
    thread.join(5)
    assert not thread.is_alive(), "it slept through the pause"
    assert jobs.get(job_id)["status"] == "paused"


def test_a_pause_that_arrives_as_the_run_settles_leaves_it_paused(tmp_path, monkeypatch):
    """The button and the run's last word are the same instant, sometimes.

    `pause` writes "pausing" so the page can say *finishing the piece in
    flight*. A run that had already written down that it stopped was
    overwritten by that write -- there is a moment when the thread is still
    alive and the job is already stopped -- and the job then claimed a piece
    was in flight forever. The wizard reads that as "already being inscribed",
    and `resume_interrupted` reads it as "start it again" (2026-09-22: a test
    of the wake only passed when the machine was slow enough to lose the race
    the other way).
    """
    build = C.read_build(hashlips(tmp_path, count=1))
    sender = FakeSender()

    def send_all(address, payloads, approve=None, on_broadcast=None, on_progress=None):
        raise RuntimeError("sendrawtransaction: [-26] 56: bad-txns-inputs-missingorspent")

    sender.send_all = send_all
    jobs, runner = runner_for(tmp_path, sender)
    job_id = jobs.create("regtest", "nSender", build)

    settled, released = threading.Event(), threading.Event()
    real_set_status = C.Jobs.set_status
    main_thread = threading.main_thread()

    def set_status(self, id, status, note=None, error=None, only_from=()):
        wrote = real_set_status(self, id, status, note, error, only_from)
        # Hold the run's thread on its way out, so that the pause below lands
        # in the gap it is meant to be tested against rather than before it.
        if (id == job_id and status == note == "paused"
                and threading.current_thread() is not main_thread):
            settled.set()
            released.wait(5)
        return wrote

    monkeypatch.setattr(C.Jobs, "set_status", set_status)
    runner.start(job_id)
    assert settled.wait(5), "the run did not pause itself"
    runner.pause(job_id)              # pressed in that gap
    released.set()
    wait(runner, job_id)
    assert jobs.get(job_id)["status"] == "paused", "the pause overwrote the run's own word"


def test_the_wait_backs_off_rather_than_asking_a_thousand_times():
    """Blocks are a minute apart and longer when busy, so the gap doubles to
    a minute rather than polling every three seconds throughout (the operator)."""
    gaps, seconds = [], C.POLL
    for _ in range(8):
        gaps.append(seconds)
        seconds = C.backoff(seconds)
    assert gaps[:4] == [3.0, 6.0, 12.0, 24.0]
    assert gaps[-1] == C.POLL_MAX == 60.0
    assert C.backoff(0) == C.POLL, "never faster than the first gap"


def test_a_failed_job_says_why(tmp_path):
    build = C.read_build(hashlips(tmp_path, count=3))
    sender = FakeSender()
    jobs, runner = runner_for(tmp_path, sender)
    job_id = jobs.create("regtest", "nSender", build)
    (build.folder / "images" / "2.png").unlink()
    runner.start(job_id)
    wait(runner, job_id)
    job = jobs.get(job_id)
    assert job["status"] == "failed" and "2.png" in job["error"], job["error"]


def test_a_chain_limit_is_waited_out_rather_than_paused_on(tmp_path, monkeypatch):
    """The node's -26 says 'not until a block': the run waits for one and
    tries the same item again, and nobody has to press Resume."""
    build = C.read_build(hashlips(tmp_path, count=3))
    sender = FakeSender()
    real_send_all = sender.send_all
    refusals = []

    def send_all(address, payloads, approve=None, on_broadcast=None, on_progress=None):
        if len(sender.sent) >= 1 and sender.height == 100:
            refusals.append(len(sender.sent))
            raise RuntimeError("sendrawtransaction: [-26] 64: too-long-mempool-chain")
        return real_send_all(address, payloads, approve=approve, on_broadcast=on_broadcast)

    sender.send_all = send_all
    monkeypatch.setattr(C, "POLL", 0.01)
    jobs, runner = runner_for(tmp_path, sender)
    job_id = jobs.create("regtest", "nSender", build)
    runner.start(job_id)
    deadline = time.time() + 5
    while not refusals and time.time() < deadline:
        time.sleep(0.01)
    assert refusals == [1]
    while "waiting for a block" not in jobs.get(job_id)["note"] and time.time() < deadline:
        time.sleep(0.01)
    job = jobs.get(job_id)
    assert job["status"] == "running" and "waiting for a block" in job["note"], job["note"]
    sender.height = 101                       # the block comes
    wait(runner, job_id)
    job = jobs.get(job_id)
    assert job["status"] == "done" and job["sent"] == 3 and job["error"] == ""
    assert refusals == [1], "asked once more only after the block"


def test_a_run_inscribes_the_collection_s_mintpad_last(tmp_path):
    """The pad goes up when the last item is on its way, not before.

    A pad that offers a random item of a collection half of which was never
    inscribed would be selling things that do not exist (D-036).
    """
    from arcade import mintpad as M

    build = C.read_build(hashlips(tmp_path))
    sender = FakeSender()
    jobs, runner = runner_for(tmp_path, sender)
    pad_json = M.shop_json("arcade:test:abc:1234", "Goofball",
                           M.take_of("token", "10", 3))
    job_id = jobs.create("regtest", "nSender", build, pad_json=pad_json)

    items_only = len(expected_payloads(build, jobs, job_id))
    assert runner.start(job_id)
    wait(runner, job_id)

    job = jobs.get(job_id)
    assert job["status"] == "done"
    assert job["pad_txid"] and not job["pad_error"]
    assert job["note"].endswith("and the mintpad with them")

    # The pad's own pieces went last, and carry the page, not an item.
    page = M.page("nSender", "Goofball")
    pad_pieces = [p for p in sender.sent if b"MINTPAD" in p or b"mintpad" in p]
    assert pad_pieces, "the mintpad is on the chain"
    assert len(sender.sent) > items_only
    assert b"%%" not in page and b"Goofball" in page

    # Running it again does not inscribe a second pad.
    before = len(sender.sent)
    runner._inscribe_pad(job_id, sender)
    assert len(sender.sent) == before, "a pad already sent is not sent twice"


def test_a_run_without_a_mintpad_inscribes_nothing_extra(tmp_path):
    build = C.read_build(hashlips(tmp_path))
    sender = FakeSender()
    jobs, runner = runner_for(tmp_path, sender)
    job_id = jobs.create("regtest", "nSender", build)
    assert runner.start(job_id)
    wait(runner, job_id)
    job = jobs.get(job_id)
    assert job["status"] == "done" and not job["pad_txid"]
    assert job["note"] == "every item is on its way"
    wanted = expected_payloads(build, jobs, job_id)
    assert sorted(sender.sent) == sorted(p for ps in wanted.values() for p in ps)


def test_the_ipfs_image_is_not_paid_for(tmp_path):
    """A HashLips build writes `"image": "ipfs://NewUriToReplace/1.png"` into
    every item. The picture here IS the inscription, so that field names a
    place the art is not, at about fifty bytes an item somebody pays for and
    nobody can follow."""
    from arcade.collections import strip_offchain

    item = {"name": "Doge Punks #1", "description": "a punk", "edition": 1,
            "image": "ipfs://NewUriToReplace/1.png", "dna": "abc",
            "attributes": [{"trait_type": "Background", "value": "Blue"}]}
    kept = strip_offchain(item)
    assert "image" not in kept
    assert kept["edition"] == 1 and kept["attributes"] == item["attributes"], \
        "membership, numbering and rarity are read from the rest of it"

    for gone in ("ipfs://QmX/1.png", "ipns://x/1.png", "ar://x",
                 "https://gateway.example/ipfs/QmX/1.png", "1.png", ""):
        assert "image" not in strip_offchain({**item, "image": gone}), gone

    # Somebody's own site resolves, and is their decision.
    stays = strip_offchain({**item, "image": "https://punks.example/1.png"})
    assert stays["image"] == "https://punks.example/1.png"
    # An `image` that is not text at all is left exactly as it was.
    assert strip_offchain({"image": 7}) == {"image": 7}


def test_a_build_inscribes_without_the_ipfs_pointer(tmp_path):
    from arcade import collections as C

    build = C.read_build(hashlips(tmp_path, count=2))
    for item in build.items:
        assert "ipfs" not in item.json, item.json
        assert '"edition"' in item.json


def test_a_set_can_be_given_its_own_face_and_words(tmp_path):
    """A collection is not an object on the chain, so what it says about
    itself goes on the piece it is known by -- its #1 (D-097, D-103)."""
    import json as jsonlib

    from arcade import collections as C
    from arcade import inscriptions as I

    build = C.read_build(hashlips(tmp_path, count=3))
    piece = "ab" * 32
    said = C.with_details(build, {"icon": piece, "description": "five punks",
                                  "url": "https://punks.example", "twitter": ""})
    first = min(said.items, key=lambda i: i.edition)
    data = jsonlib.loads(first.json)
    assert data["collection"] == {"name": "Doge Punks", "icon": piece,
                                  "description": "five punks",
                                  "url": "https://punks.example", "supply": 3}, \
        "empty fields are not written, and cost nothing"
    assert data["edition"] == 1 and data["attributes"], "the item is otherwise itself"
    assert I.collection_of(first.json) == ("Doge Punks", 1, "Doge Punks #1"), \
        "membership is decided by the name, not by the object"
    assert I.collection_details(first.json)["icon"] == piece
    # Nothing said, and the size is still written: it is what seals the set,
    # so it is not the creator's to leave out (D-120).
    plain = jsonlib.loads(C.with_details(build, {}).items[0].json)
    assert plain["collection"]["supply"] == 3
    assert plain["collection"]["description"] == "test set", \
        "and what the build already said about itself is still carried (D-114)"
    others = [i.json for i in said.items if i.edition != 1]
    assert others == [i.json for i in build.items if i.edition != 1], \
        "and it is written once, not on all five hundred"


def test_a_set_is_costed_in_whole_chunks_and_has_no_size_limit(tmp_path):
    """No cap belongs on a piece: the pictures come from a folder and are
    planned as a job, and a piece takes as many chunks as it takes. What a
    form caps is a single image chosen in that form, and that cap must never
    reach this path (a test machine)."""
    from arcade import collections as C

    build = C.read_build(hashlips(tmp_path, count=3))
    big = dataclasses.replace(build.items[0], size=40_000)
    build = dataclasses.replace(build, items=[big] + build.items[1:])

    cost = C.estimate_build(build)
    assert isinstance(cost["chunks"], int)
    assert cost["chunks"] >= 7, "a 40 KB piece is several chunks, and is allowed"
    assert cost["items"] == 3, "and nothing refused it"


def test_an_edited_mintpad_page_is_the_one_inscribed(tmp_path):
    """The page is the creator's shop front and goes on the chain under
    their name, so what they looked at in the editor is what goes up --
    exactly as written, not merged with the template of the day."""
    from arcade import mintpad as M

    build = C.read_build(hashlips(tmp_path))
    sender = FakeSender()
    jobs, runner = runner_for(tmp_path, sender)
    pad_json = M.shop_json("arcade:test:abc:1234", "Goofball",
                           M.take_of("coins", "10"))
    mine = ("<!doctype html><title>GOOFBALL MINTPAD</title>"
            "<script>const CREATOR='nSender';const COLLECTION='Goofball';</script>"
            "<h1>my own shop front</h1>")
    job_id = jobs.create("regtest", "nSender", build, pad_json=pad_json,
                         pad_html=mine)
    assert runner.start(job_id)
    wait(runner, job_id)

    job = jobs.get(job_id)
    assert job["status"] == "done" and job["pad_txid"]
    sent = b"".join(sender.sent)
    assert b"my own shop front" in sent
    assert b"MINTPAD</title>" in sent
    # And NOT the standard page: its buy button is nowhere in what went out.
    assert b'<button id="buy"' not in sent


def test_a_page_left_alone_is_not_frozen_onto_the_job(tmp_path):
    """`pad_html` empty means "the standard page", generated when it is
    inscribed -- so a mintpad improved by a later release reaches a run
    written down before it."""
    from arcade import mintpad as M

    build = C.read_build(hashlips(tmp_path))
    sender = FakeSender()
    jobs, runner = runner_for(tmp_path, sender)
    job_id = jobs.create(
        "regtest", "nSender", build,
        pad_json=M.shop_json("arcade:test:abc:1234", "Goofball",
                             M.take_of("coins", "10")))
    assert jobs.get(job_id)["pad_html"] == ""
    assert runner.start(job_id)
    wait(runner, job_id)
    # The pieces are chunks of it, so the page is looked for in the whole
    # of what went out rather than in any one payload.
    sent = b"".join(sender.sent)
    assert b'<button id="buy"' in sent, "the standard page, built at sending"
    assert b"GOOFBALL MINTPAD" in sent

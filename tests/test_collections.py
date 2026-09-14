"""A HashLips build, read, priced, written down and sent -- with a pause, a
resume and a crash in the middle -- against a sender that only pretends."""

import json
import threading
import time

import pytest

from arcade import collections as C
from arcade import inscribe
from arcade import inscriptions as I


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
    assert json.loads(build.items[1].json) == original, "same data"
    assert build.items[1].json == json.dumps(original, separators=(",", ":")), \
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


def test_funding_that_never_confirms_pauses_rather_than_chains(tmp_path, monkeypatch):
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
    assert job["status"] == "paused" and "waited 0 minutes for a block" in job["error"]
    assert sender.sent == [] and sender.splits == 0, "nothing sent on top of the unconfirmed"


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

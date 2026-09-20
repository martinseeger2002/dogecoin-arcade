"""The feed as it is this second.

A block is a minute or ten, and a like that takes ten minutes to appear
reads as a like that did not work. Read fresh, never written down: the store
is built from blocks and stays that way (D-117, D-141).
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

from arcade import payload as P                                  # noqa: E402
from arcade.messaging import feed, group, mempool                # noqa: E402

THEM = "nThemAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


class Node:
    """A node whose mempool holds whatever it was given."""

    def __init__(self, payloads):
        self.payloads = payloads

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def call(self, method, *args):
        if method == "getrawmempool":
            return [f"{n:064x}" for n in range(len(self.payloads))]
        if method == "getrawtransaction":
            return {"txid": args[0], "vin": [], "vout": []}
        raise AssertionError(method)


def pending(monkeypatch, params, payloads, senders=None):
    """Read the mempool with the extractor standing in for a real chain."""
    import arcade.messaging.mempool as under_test

    senders = senders or [THEM] * len(payloads)
    seen = iter(list(zip(payloads, senders)))

    class Fake:
        def __init__(self, txid):
            body, sender = next(seen)
            self.txid = txid
            self.sender = sender
            self.payload = P.AnyData(data=body).encode()

    monkeypatch.setattr(under_test, "extract",
                        lambda raw, *a, **k: Fake(raw["txid"]))
    return under_test.read(Node(payloads), params, "test")


def test_a_like_in_the_mempool_is_counted(client, monkeypatch):
    app, state = client
    post = "a" * 64
    with state.store() as store:
        store.add_group_post("test", "", post, 100, 1000, THEM, "", "the post")

    found = pending(monkeypatch, state.messaging.params,
                    [feed.build(feed.LIKE, post)])
    assert len(found.acts) == 1
    assert found.acts[0]["kind"] == feed.LIKE and found.acts[0]["height"] == 0
    assert found.acts[0]["pending"] == 1


def test_a_post_in_the_mempool_is_read(client, monkeypatch):
    app, state = client
    payload = group.build(group.GroupPost(text="hot off the press",
                                          channel="", nickname=""))
    found = pending(monkeypatch, state.messaging.params, [payload])
    assert len(found.posts) == 1
    assert found.posts[0]["text"] == "hot off the press"
    assert found.posts[0]["height"] == 0


def test_anything_that_is_not_ours_is_left_alone(client, monkeypatch):
    """A mempool is full of other people's transactions."""
    app, state = client
    found = pending(monkeypatch, state.messaging.params,
                    [b"not an arcade payload at all", b""])
    assert not found and found.posts == [] and found.acts == []


def test_a_kind_this_version_does_not_know_is_skipped(client, monkeypatch):
    app, state = client
    payload = bytearray(feed.build(feed.LIKE, "a" * 64))
    payload[6] = 99
    found = pending(monkeypatch, state.messaging.params, [bytes(payload)])
    assert found.acts == [], "not guessed at, and not fatal either"


def test_nothing_read_from_the_mempool_is_written_down(client, monkeypatch):
    """The ledger is built from blocks and stays that way: a like that never
    confirms must leave nothing behind."""
    app, state = client
    post = "a" * 64
    with state.store() as store:
        store.add_group_post("test", "", post, 100, 1000, THEM, "", "the post")

    found = pending(monkeypatch, state.messaging.params,
                    [feed.build(feed.LIKE, post)])
    assert found.acts

    with state.store() as store:
        assert store.feed_acts_on("test", [post]) == [], "still only blocks"


def test_a_pending_row_answers_the_same_questions_a_stored_one_does():
    """The page must not need to know which kind of row it is holding."""
    row = mempool.Row({"txid": "a" * 64, "kind": 1, "height": 0})
    assert "height" in row.keys() and row["height"] == 0


def test_a_reaction_in_the_pool_is_never_written_down(client):
    """Two mechanisms were doing this job and one of them was wrong: the
    scanner writes pool rows at height 0 and a block promotes them in place,
    which is right for a message and wrong for a COUNT -- a like whose
    transaction is dropped would be counted for ever (D-144)."""
    import time as clock

    from arcade.messaging.scanner import Scanner

    app, state = client
    post = "a" * 64
    like = feed.build(feed.LIKE, post)

    class PoolNode:
        def call(self, method, *args):
            if method == "getrawmempool":
                return ["b" * 64]
            if method == "getrawtransaction":
                return {"txid": "b" * 64, "vin": [], "vout": [],
                        "time": int(clock.time())}
            raise AssertionError(method)

        def get_block_count(self):
            return 100

    with state.store() as store:
        store.add_group_post("test", "", post, 100, 1000, THEM, "", "the post")
        scanner = Scanner(PoolNode(), state.messaging.params, store)
        import arcade.messaging.scanner as under_test

        class Fake:
            txid = "b" * 64
            sender = THEM
            payload = P.AnyData(data=like).encode()

        original = under_test.extract
        under_test.extract = lambda tx, *a, **k: Fake()
        try:
            scanner.scan_mempool()
        finally:
            under_test.extract = original
        assert store.feed_acts_on("test", [post]) == [], \
            "counted from the pool, never stored from it"

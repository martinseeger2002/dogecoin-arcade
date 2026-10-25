"""The files read back off the chain are kept in a small cache per index, and
request threads read and fill it at once: a game page asks for its few hundred
modules in parallel. Before 2026-10-25 nothing guarded it, and node 3 logged
"dictionary keys changed during iteration" from `_remember` while a player's
browser loaded a game. These hold the cache to its rules under that load."""

import threading

import pytest

from arcade.config import NETWORKS
from arcade.ledger import LedgerIndex


@pytest.fixture
def index(tmp_path):
    idx = LedgerIndex(tmp_path / "ledger.sqlite", NETWORKS["regtest"], rpc_factory=lambda: None)
    idx.CACHE_BYTES = 64 * 1024                  # small, so trimming happens all the time
    return idx


def test_many_threads_reading_and_filling_at_once_raise_nothing(index):
    errors, start = [], threading.Barrier(8)

    def work(t):
        try:
            start.wait()
            for i in range(3000):
                key = f"tx{(i * 7 + t) % 160}"
                if index._cached(key) is None:
                    index._remember(key, bytes(1024 + (i % 5) * 512))
        except Exception as exc:                 # noqa: BLE001 -- any of them is the failure
            errors.append(repr(exc))

    threads = [threading.Thread(target=work, args=(t,)) for t in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert errors == []
    held = sum(len(v) for v in index._content_cache.values())
    assert held == index._content_held          # the running total matches what is there
    assert held <= index.CACHE_BYTES


def test_it_keeps_the_most_recently_wanted_and_drops_the_oldest(index):
    index.CACHE_BYTES = 3000
    for k in ("a", "b", "c"):
        index._remember(k, bytes(1000))
    assert index._cached("a") is not None        # "a" is now the newest
    index._remember("d", bytes(1000))            # over the limit: the oldest, "b", goes
    assert list(index._content_cache) == ["c", "a", "d"]
    assert index._content_held == 3000


def test_filling_the_same_file_again_does_not_count_it_twice(index):
    index._remember("a", bytes(1000))
    index._remember("a", bytes(1500))
    assert index._content_held == 1500
    assert list(index._content_cache) == ["a"]

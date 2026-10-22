"""After a block, the trades are read once, not by every page at once (2026-10-21: app.dogecoinarcade.com's /feed
stalled 57 s with every request thread reading the whole transaction table for its notification count)."""
import threading
import time

from arcade import ledger


def test_one_reader_and_the_rest_get_the_last_answer(monkeypatch):
    index = ledger.LedgerIndex.__new__(ledger.LedgerIndex)
    tips = {"tip": 10}

    class _Db:
        def __init__(self):
            self.conn = self

        def execute(self, sql, *a):
            return self

        def fetchone(self):
            return (tips["tip"],)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False
    monkeypatch.setattr(ledger.LedgerIndex, "open", lambda self: _Db())
    reads, gate = [], threading.Event()

    def uncached(self, limit, since):
        reads.append(tips["tip"])
        if len(reads) > 1:
            gate.wait(5)
        return [{"txid": f"t{tips['tip']}"}]
    monkeypatch.setattr(ledger.LedgerIndex, "_trades_uncached", uncached)
    assert index.trades()[0]["txid"] == "t10"
    tips["tip"] = 11                                   # a new block
    got, took = [], []

    def page():
        t = time.monotonic()
        got.append(index.trades()[0]["txid"])
        took.append(time.monotonic() - t)
    first = threading.Thread(target=page)
    first.start()
    time.sleep(0.2)                                    # the first page is reading
    others = [threading.Thread(target=page) for _ in range(6)]
    for o in others:
        o.start()
    for o in others:
        o.join(2)
    assert len(reads) == 2, "one read for the new block"
    assert got.count("t10") == 6 and max(took) < 1.0, "the other pages answered at once with the last answer"
    gate.set()
    first.join(5)
    assert got[-1] == "t11" and index.trades()[0]["txid"] == "t11"

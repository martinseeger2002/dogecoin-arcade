"""link_up's row dump is built once for many links, and few links get it at once (2026-11-16, node 3 held at one core)."""
import asyncio
import types

from arcade.mesh import doginalshare as ds


class FakeIndex:
    def __init__(self, net, rows):
        self.net, self.rows, self.calls = net, rows, 0
        self.chunks = types.SimpleNamespace(lanes=[], gone=lambda peer: None)
        self.share = self.me = None

    def mode(self):
        return "share"

    def hello(self):
        return {"op": "hi", "net": self.net}

    def all_mine(self):
        self.calls += 1
        return list(self.rows)


class FakeLink:
    def __init__(self, gate_log=None):
        self.sent, self.gate_log = [], gate_log

    async def send(self, frame):
        self.sent.append(frame)
        await asyncio.sleep(0)


def run(coro):
    return asyncio.run(coro)


def test_one_build_for_many_links():
    idx = FakeIndex("dgb", [[c, "h" * 64, 1] for c in range(2000)])
    share = ds.DxShare({"dgb": idx})
    links = [FakeLink() for _ in range(6)]

    async def go():
        await asyncio.gather(*(share.link_up(link) for link in links))
    run(go())
    assert idx.calls == 1
    want = [f for f in links[0].sent if f.get("op") == "dn"]
    assert sum(len(f["rows"]) for f in want) == 2000
    for link in links[1:]:
        assert [f for f in link.sent if f.get("op") == "dn"] == want
        assert link.sent[0]["op"] == "hi"


def test_rebuilt_after_dump_seconds(monkeypatch):
    idx = FakeIndex("doge", [[1, "a" * 64, 2]])
    share = ds.DxShare({"doge": idx})
    run(share.link_up(FakeLink()))
    idx.rows.append([2, "b" * 64, 3])
    run(share.link_up(FakeLink()))
    assert idx.calls == 1                        # within DUMP_SECONDS: the same frames
    monkeypatch.setattr(ds, "DUMP_SECONDS", 0.0)
    link = FakeLink()
    run(share.link_up(link))
    assert idx.calls == 2
    assert sum(len(f["rows"]) for f in link.sent if f.get("op") == "dn") == 2


def test_few_dumps_at_once(monkeypatch):
    monkeypatch.setattr(ds, "DUMPS_AT_ONCE", 2)
    idx = FakeIndex("main", [[c, "c" * 64, 1] for c in range(1500)])
    share = ds.DxShare({"main": idx})
    busy = {"now": 0, "most": 0}

    class SlowLink(FakeLink):
        async def send(self, frame):
            if frame.get("op") == "dn":
                busy["now"] += 1
                busy["most"] = max(busy["most"], busy["now"])
                await asyncio.sleep(0.001)
                busy["now"] -= 1
            self.sent.append(frame)

    async def go():
        await asyncio.gather(*(share.link_up(SlowLink()) for _ in range(5)))
    run(go())
    assert busy["most"] <= 2

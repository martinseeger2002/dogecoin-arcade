"""The chain's own reading of inscriptions (2026-10-15: the outside Doginals API removed; "do the same for small .01 pep txs.
Not made by the Arcade"): a coin's koinu are followed back to the envelope that inscribed them, and an inscription's
content is read out of its envelope, across as many transactions as it runs on. A fake node here; the real one is
checked against live Doginals (Anti Doginal Dog #1089 at 81690a86…:0, and eight indexed ones) by hand."""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                                # noqa: F401,E402

from arcade import doginals as D, funding                            # noqa: E402
from arcade.txbuild import push                                       # noqa: E402


def _sig(script: bytes) -> dict:
    return {"hex": script.hex()}


class Node:
    """getrawtransaction / getblockhash / getblock / getblockheader over a dict of transactions."""

    def __init__(self, txs, blocks=None):
        self.txs, self.blocks, self.calls = txs, blocks or {}, 0

    def call(self, method, *a):
        self.calls += 1
        if method == "getrawtransaction":
            return self.txs[a[0]]
        if method == "getblockhash":
            return f"h{a[0]}"
        if method == "getblock":
            ids = self.blocks.get(int(a[0][1:]), [])
            if len(a) > 1 and a[1] == 0:
                # The raw block, as far as the reader looks at it: every input's outpoint, serialised.
                return "".join(bytes.fromhex(v["txid"])[::-1].hex() + int(v.get("vout", 0)).to_bytes(4, "little").hex()
                               for t in ids for v in self.txs[t].get("vin") or [] if "txid" in v)
            return {"tx": ids}
        if method == "getblockheader":
            return {"height": int(a[0][1:])}
        raise AssertionError(method)


def _tx(txid, vin, vout, block=None):
    return {"txid": txid, "vin": vin, "vout": [{"n": n, "value": v} for n, v in enumerate(vout)],
            **({"blockhash": f"h{block}"} if block is not None else {})}


ENVELOPE = push(b"ord") + bytes([0x51]) + push(b"text/plain") + bytes([0x00]) + push(b"hi") + push(b"\x30" * 71) + push(b"\x51")
G, X, F, M = "aa" * 32, "bb" * 32, "cc" * 32, "dd" * 32


def _chain():
    return {
        F: _tx(F, [{"coinbase": "00"}], [100.0]),
        G: _tx(G, [{"txid": F, "vout": 0, "scriptSig": _sig(ENVELOPE)}], [0.001, 9.0]),       # the inscription is born
        M: _tx(M, [{"coinbase": "00"}], [0.5]),
        # A send: the inscribed 0.001 first, a 0.5 coin after -> output 0 (0.01) carries it at koinu 0.
        X: _tx(X, [{"txid": G, "vout": 0, "scriptSig": _sig(b"\x01")}, {"txid": M, "vout": 0, "scriptSig": _sig(b"\x01")}],
               [0.01, 0.4899]),
    }


def test_a_sent_doginal_is_found_on_the_coin_it_went_into():
    node = Node(_chain())
    assert D.trace(node, X, 0) == [{"id": f"{G}i0", "offset": 0}]
    assert D.trace(node, G, 0) == [{"id": f"{G}i0", "offset": 0}], "where it was born"
    assert D.trace(node, M, 0) == [], "a coinbase coin carries nothing"


def test_a_coin_whose_koinu_come_after_the_inscription_is_clean():
    txs = _chain()
    # The inscribed coin second: output 0 (0.01) is made of the 0.5 coin's first koinu, so it carries nothing.
    txs[X] = _tx(X, [{"txid": M, "vout": 0, "scriptSig": _sig(b"\x01")}, {"txid": G, "vout": 0, "scriptSig": _sig(b"\x01")}],
                 [0.01, 0.49099])
    assert D.trace(Node(txs), X, 0) == []
    assert D.trace(Node(txs), X, 1) == [{"id": f"{G}i0", "offset": 49000000}], "the change took it, 0.49 in"


def test_a_walk_that_does_not_end_is_never_called_clean():
    """A walk longer than one ask's steps is "cannot tell yet", never clean; it keeps where it got to and the next ask
    goes on from there (2026-10-20, 52b0a65: no depth limit -- a Doginal Frog ~60 deep was filed unknown for good)."""
    txs, prev = {}, None
    for n in range(60):
        t = f"{n:064x}"
        vin = [{"txid": prev, "vout": 0, "scriptSig": _sig(b"\x01")}] if prev else [{"coinbase": "00"}]
        txs[t] = _tx(t, vin, [0.01])
        prev = t
    with pytest.raises(D.TraceIncomplete) as stopped:
        D.trace(Node(txs), prev, 0, limit=40)
    assert stopped.value.state["queue"], "where it got to"
    assert D.trace(Node(txs), prev, 0, limit=40, resume=stopped.value.state) == [], "and the next ask finishes it"


def test_content_is_read_across_the_transactions_it_runs_on():
    genesis = push(b"ord") + bytes([0x52]) + push(b"text/html") + bytes([0x51]) + push(b"<p>hel") + push(b"\x30" * 71) + push(b"\x51")
    rest = bytes([0x00]) + push(b"lo</p>") + push(b"\x30" * 71) + push(b"\x51")
    g, nxt = "ee" * 32, "ff" * 32
    txs = {g: _tx(g, [{"txid": F, "vout": 0, "scriptSig": _sig(genesis)}], [0.001], block=10),
           nxt: _tx(nxt, [{"txid": g, "vout": 0, "scriptSig": _sig(rest)}], [0.001], block=11)}
    assert D.content(Node(txs, blocks={10: [g], 11: [nxt]}), f"{g}i0") == ("text/html", b"<p>hello</p>")
    with pytest.raises(ValueError):
        D.content(Node(txs, blocks={10: [g]}, ), f"{g}i0", look=1)


def test_a_guard_on_the_chain_marks_what_it_cannot_finish_and_keeps_it_out(tmp_path, monkeypatch):
    txs, prev = dict(_chain()), None
    for n in range(D.TRACE_TXS + 50):                      # longer than one ask goes
        t = f"{n:064x}"
        vin = [{"txid": prev, "vout": 0, "scriptSig": _sig(b"\x01")}] if prev else [{"coinbase": "00"}]
        txs[t] = _tx(t, vin, [0.01])
        prev = t

    class Rpc:
        def __enter__(self):
            return Node(txs)

        def __exit__(self, *a):
            return False
    guard = D.Doginals(tmp_path / "g.sqlite", rpc_factory=Rpc)
    assert guard.held(X)[0] == [{"id": f"{G}i0", "offset": 0}]
    assert guard.held(prev)[0][0]["unknown"] is True
    assert guard.spendable([{"txid": X, "vout": 0, "value": 1_000_000}, {"txid": prev, "vout": 0, "value": 1_000_000},
                            {"txid": X, "vout": 1, "value": 48_990_000}]) == [{"txid": X, "vout": 1, "value": 48_990_000}]
    # Asked again once the retry time is up: the walk goes on from where it stopped, reaches the coinbase, and the coin
    # is known clean at last.
    monkeypatch.setattr(D, "UNKNOWN_RETRY", 0)
    assert guard.held(prev) == {}


def test_pep_mainnet_coins_get_the_same_guard(client):
    app, state = client
    from arcade.config import NETWORKS
    from arcade.script import b58check_encode
    pep = b58check_encode(NETWORKS["main"].pubkeyhash_version, b"\x11" * 20)
    test = b58check_encode(state.messaging.params.pubkeyhash_version, b"\x11" * 20)
    assert funding.guard_for(pep) is not None, "a Pepecoin mainnet address is watched"
    assert funding.guard_for(test) is None, "the test chain is not"


def test_content_this_arcade_does_not_index_is_read_from_the_chain(client, monkeypatch):
    app, state = client
    iid = "ab" * 32 + "i0"
    guard = funding.GUARDS[0][1]
    monkeypatch.setattr(guard, "content", lambda i: ("image/png", b"\x89PNG-from-chain") if i == iid else (_ for _ in ()).throw(ValueError()))
    got = app.get(f"/content/{iid}")
    assert got.status_code == 200 and got.content == b"\x89PNG-from-chain" and got.headers["content-type"] == "image/png"
    assert app.get(f"/content/{'cd' * 32}i0").status_code == 404


def _delegating(delegate_txid: str) -> bytes:
    """A Doginal with no body of its own and a delegate tag (11), as the Dogecoin "bone" pieces are inscribed."""
    return (push(b"ord") + bytes([0x51]) + bytes([0x00]) + bytes([0x00]) + bytes([0x00])
            + push(b"\x0b") + push(bytes.fromhex(delegate_txid)[::-1]) + push(b"\x30" * 71) + push(b"\x51"))


def test_a_piece_that_names_a_delegate_shows_the_delegates_content(tmp_path):
    """2026-10-28: Explore's Dogecoin "bone" pieces came back as an empty download -- each is a delegate of one video,
    and the chain reader (used while the ord index has not walked that far) read only the piece's own empty body."""
    d = "ab" * 32
    txs = {**_chain(), d: _tx(d, [{"txid": F, "vout": 0, "scriptSig": _sig(_delegating(G))}], [0.001])}
    assert D.read(Node(txs), f"{d}i0") == ("", b"", f"{G}i0")
    assert D.content(Node(txs), f"{d}i0") == ("", b""), "the envelope alone, as before"

    class Rpc:
        def __enter__(self):
            return Node(txs)

        def __exit__(self, *a):
            return False
    guard = D.Doginals(tmp_path / "g.sqlite", rpc_factory=Rpc)
    assert guard.content(f"{d}i0") == ("text/plain", b"hi"), "the delegate's content and type"
    assert guard.content(f"{d}i0", follow=False) == ("text/plain", b"hi"), "kept, so not read again"
    # One read before delegates were followed kept no type and no bytes: read again, not served empty for good.
    (tmp_path / "inscription-content" / f"{d}i0").write_bytes(b"")
    (tmp_path / "inscription-content" / f"{d}i0.type").write_text("")
    assert guard.content(f"{d}i0") == ("text/plain", b"hi")


def test_a_piece_on_another_chain_is_said_at_once_without_waiting_for_a_turn(tmp_path):
    """2026-10-28: /content asks every chain's guard in turn, and each waited for one of two reading turns first: a
    Bells piece on node 2 took 225 s while a collection page of 48 waited. Not on this chain is said before the wait,
    and each chain has its own two turns."""
    class Rpc:
        def __enter__(self):
            return Node(_chain())

        def __exit__(self, *a):
            return False
    guard = D.Doginals(tmp_path / "g.sqlite", rpc_factory=Rpc)
    other = D.Doginals(tmp_path / "o.sqlite", rpc_factory=Rpc)
    assert guard._reading.acquire(timeout=1) and guard._reading.acquire(timeout=1), "this chain's turns, both taken"
    import time
    started = time.time()
    with pytest.raises(D.Unknown, match="not on this chain"):
        guard.content(f"{'ef' * 32}i0")
    assert time.time() - started < 2, "said at once, not after waiting for a turn"
    assert other.content(f"{G}i0") == ("text/plain", b"hi"), "another chain reads with its own turns"

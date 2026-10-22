"""A Core rescanning for wallet imports borrows the chain's reads, across a restart (2026-10-21).

A 1.14 Core holds its lock for the whole rescan and answers nothing; the import says so (core_catchup.hold), the
hold is kept in the arcade's settings, and it is let go of only once no import runs here and the Core answers again.
"""
from types import SimpleNamespace

import pathlib

import pytest

from arcade import core_catchup as cc
from arcade.rpc import RpcTransportError


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    cc.forget()
    cc.HOLDS.clear()
    cc._running.clear()
    kept = {}
    monkeypatch.setattr(cc, "store", (lambda: dict(kept), lambda d: (kept.clear(), kept.update(d))))
    monkeypatch.setattr(cc, "_loaded", False)
    monkeypatch.setattr(cc, "_sharers", lambda share, net: (["peerpeerpeer01"], 1000))
    yield kept
    cc.HOLDS.clear()
    cc._running.clear()
    cc.forget()


CHAIN = SimpleNamespace(network="doge", borrowed=False, params=SimpleNamespace(label="DOGE"))


class _Core:
    def __init__(self, answers):
        self.answers = answers

    def call(self, method, *args):
        if not self.answers:
            raise RpcTransportError("timed out")
        if method == "getblockchaininfo":
            return {"blocks": 1000, "headers": 1000, "verificationprogress": 1.0, "initialblockdownload": False}
        return {}

    def close(self):
        pass


def test_a_hold_borrows_at_once_and_is_kept(fresh):
    assert cc.hold(CHAIN, "rescanning for wallet imports")
    assert cc._state("doge").borrowing and fresh == {"doge": "rescanning for wallet imports"}
    st = cc.check(CHAIN, share=object(), client=lambda chain: _Core(answers=True))
    assert st.borrowing and st.reading.behind, "while the import runs, the Core is not even asked"


def test_after_a_restart_a_busy_core_still_borrows_and_one_that_answers_lets_go(fresh, monkeypatch):
    cc.hold(CHAIN, "rescanning for wallet imports")
    # a restart: memory gone, the settings kept
    cc.HOLDS.clear()
    cc._running.clear()
    cc.forget()
    monkeypatch.setattr(cc, "_loaded", False)
    st = cc.check(CHAIN, share=object(), client=lambda chain: _Core(answers=False))
    assert cc.held("doge") and st.reading.behind and st.borrowing, "the Core does not answer: still rescanning"
    st = cc.check(CHAIN, share=object(), client=lambda chain: _Core(answers=True))
    assert cc.held("doge") is None and fresh == {}, "it answers again: the rescan is over"
    assert st.reading.behind is False


def test_finished_lets_the_next_check_ask_the_core(fresh):
    cc.hold(CHAIN, "rescanning for wallet imports")
    cc.finished("doge")
    cc.check(CHAIN, share=object(), client=lambda chain: _Core(answers=True))
    assert cc.held("doge") is None


def test_a_chain_borrowing_while_its_core_rescans_is_never_asked_on_a_page(tmp_path, monkeypatch):
    """2026-10-21, app.dogecoinarcade.com down 16:13: a Dogecoin run here but borrowing while its Core rescanned was
    asked on every request thread at once, each waiting minutes on the mesh. Its status is kept and refreshed on one
    thread of its own; a page gets what is known at once."""
    import threading
    import time as _time
    from arcade.web.state import AppState, ChainContext
    state = AppState(home=tmp_path,
                     messaging=ChainContext(network="regtest", role="messaging", label="Testnet",
                                            datadir=pathlib.Path("/nonexistent")),
                     ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                                         datadir=pathlib.Path("/nonexistent")),
                     doge=ChainContext(network="doge", role="ledger", label="Dogecoin",
                                       datadir=pathlib.Path("/nonexistent")))
    gate, asked = threading.Event(), []

    def slow(chain):
        asked.append(threading.current_thread().name)
        gate.wait(10)
        return {"ready": True}
    monkeypatch.setattr(AppState, "_sync_now", lambda self, chain: slow(chain))
    state._sync["doge"] = (_time.time() - 1, {"ready": True, "online": True})      # known, gone stale
    took = []
    def page():
        t = _time.monotonic(); state.chain_sync(state.doge); took.append(_time.monotonic() - t)
    pages = [threading.Thread(target=page) for _ in range(8)]
    for p in pages:
        p.start()
    for p in pages:
        p.join(5)
    gate.set()
    assert max(took) < 1.0, "every page answered at once from what is known"
    assert len(asked) == 1 and asked[0].startswith("arcade-sync-"), "asked once, on a thread of its own"


def test_one_check_of_the_sharers_at_a_time_and_a_failure_remembered(monkeypatch):
    from arcade.web import state as statemod
    from arcade.rpc import RpcTransportError
    chain = statemod.ChainContext(network="doge", role="ledger", label="Dogecoin", datadir=pathlib.Path("/nonexistent"))
    calls = []

    def verify(client, params):
        calls.append(1)
        raise RpcTransportError("no arcade sharing doge answered")
    monkeypatch.setattr(statemod, "verify_connected_chain", verify)
    monkeypatch.setitem(statemod._BORROWED_SEEN, "doge", 0)
    statemod._BORROWED_FAILED.pop("doge", None)
    with pytest.raises(RpcTransportError):
        chain._borrowed_rpc()
    with pytest.raises(RpcTransportError, match="did not answer just now"):
        chain._borrowed_rpc()
    assert len(calls) == 1, "a failed check is not asked again for half a minute"
    statemod._BORROWED_FAILED.pop("doge", None)
    lock = statemod._BORROWED_LOCKS.setdefault("doge", __import__("threading").Lock())
    lock.acquire()
    try:
        with pytest.raises(RpcTransportError, match="being checked"):
            chain._borrowed_rpc()
    finally:
        lock.release()
    assert len(calls) == 1, "a thread arriving while one checks is told at once"

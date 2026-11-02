"""The first look at an add-on chain, asked twice at once (2026-10-31, Darrell, issue #51): "RuntimeError: cannot join
thread before it is started", from watcher._sync_ledgers on a fresh start."""
import threading
import time

import pytest

from arcade.web import state as statelib

State = next(v for v in vars(statelib).values() if isinstance(v, type) and "chain_sync" in vars(v))


class _Chain:
    is_addon, borrowed, network = True, False, "dogecoin"


def _state():
    s = State.__new__(State)
    s._sync = {}
    s._sync_now = lambda c: s._sync.__setitem__(c.network, (time.time() + 60, {"ready": True}))
    return s


def test_a_second_caller_waits_for_the_thread_to_start(monkeypatch):
    s, real = _state(), threading.Thread.start
    entered, gate = threading.Event(), threading.Event()

    def slow(self):
        if self.name.startswith("arcade-sync"):
            entered.set()
            gate.wait(5)
        real(self)
    monkeypatch.setattr(threading.Thread, "start", slow)
    said, errors = [], []

    def ask():
        try:
            said.append(s.chain_sync(_Chain))
        except Exception as exc:                         # noqa: BLE001 -- what the test is for
            errors.append(exc)
    first = threading.Thread(target=ask)
    first.start()
    assert entered.wait(5)
    second = threading.Thread(target=ask)
    second.start()
    time.sleep(0.2)                                      # the second is now where it used to take the unstarted thread
    gate.set()
    first.join(5)
    second.join(5)
    assert not errors, errors
    assert said == [{"ready": True}, {"ready": True}]


def test_many_at_once_start_one_thread():
    s, asked = _state(), []
    answer = s._sync_now

    def counted(chain):
        asked.append(chain)
        time.sleep(0.05)
        answer(chain)
    s._sync_now = counted
    errors, go = [], threading.Barrier(16)

    def ask():
        go.wait()
        try:
            s.chain_sync(_Chain)
        except Exception as exc:                         # noqa: BLE001
            errors.append(exc)
    many = [threading.Thread(target=ask) for _ in range(16)]
    for t in many:
        t.start()
    for t in many:
        t.join(5)
    assert not errors and len(asked) == 1, (errors, len(asked))


def test_a_thread_that_will_not_start_does_not_shut_the_chain_out(monkeypatch):
    s, real = _state(), threading.Thread.start

    def refused(self):
        raise RuntimeError("can't start new thread")
    monkeypatch.setattr(threading.Thread, "start", refused)
    with pytest.raises(RuntimeError):
        s.chain_sync(_Chain)
    monkeypatch.setattr(threading.Thread, "start", real)
    assert s.chain_sync(_Chain) == {"ready": True}

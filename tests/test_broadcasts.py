"""Public machine talk (messaging/broadcast.py) and its first user, content flags (arcade/flags.py).

2026-10-17: "a machine talk public message layer" -- one node telling every node
something under a topic -- and sensitive-material flags on it: an arcade broadcasts what
its model flagged, every arcade lists who broadcasts, and each operator picks whom to use."""
import json
import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from test_web import app_state, client                       # noqa: F401,E402
from test_admin import LOCAL, WRITE, public                  # noqa: F401,E402

from arcade import flags as flagslib                         # noqa: E402
from arcade import moderation as mod                         # noqa: E402
from arcade.messaging import broadcast as bc                 # noqa: E402
from arcade.messaging.envelope import Header, TYPE_BROADCAST  # noqa: E402
from arcade.messaging.store import MessageStore              # noqa: E402

A, B, C = "n" + "A" * 33, "n" + "B" * 33, "n" + "C" * 33
D1, D2, D3 = "1" * 64, "2" * 64, "3" * 64


# --- the message ---------------------------------------------------------------------

def test_a_broadcast_round_trips_and_names_its_type():
    payload = bc.build("my-game/scores/v1", {"week": 41, "top": ["@pip"]})
    assert payload[5] == TYPE_BROADCAST
    assert Header.decode(payload).type == TYPE_BROADCAST
    assert bc.parse(payload) == {"topic": "my-game/scores/v1",
                                 "body": {"week": 41, "top": ["@pip"]}}
    # Class B pads with NULs; they are not part of the body.
    assert bc.parse(payload + b"\x00" * 9)["body"]["week"] == 41


@pytest.mark.parametrize("topic", ["", "Upper", "has space", "x" * 65, "/lead"])
def test_a_topic_is_a_short_lower_case_name(topic):
    with pytest.raises(bc.BroadcastError):
        bc.build(topic, {})


def test_one_broadcast_is_one_transaction():
    with pytest.raises(bc.BroadcastError, match="split it"):
        bc.build("big/v1", "x" * bc.MAX_BYTES)


def test_anything_else_is_not_read_as_a_broadcast():
    from arcade.gamestate import build as gamestate
    assert bc.parse(gamestate("g", [["a" * 64, 1, {"hp": 1}]])) is None
    assert bc.parse(b"not ours at all") is None


# --- the index -----------------------------------------------------------------------

def test_the_index_reads_by_topic_sender_and_cursor_and_rewinds(tmp_path):
    with MessageStore(tmp_path / "s.sqlite") as store:
        store.add_node_broadcast("test", "t1", A, "flags/v1", {"f": []}, 10, 100)
        store.add_node_broadcast("test", "t2", B, "game/v1", {"x": 1}, 11, 110)
        store.add_node_broadcast("test", "t3", A, "flags/v1", {"f": []}, 12, 120)
        store.add_node_broadcast("main", "t4", A, "flags/v1", {"f": []}, 12, 120)
        assert [r["txid"] for r in store.node_broadcasts("test")] == ["t1", "t2", "t3"]
        assert [r["txid"] for r in store.node_broadcasts("test", "flags/v1")] == ["t1", "t3"]
        first = store.node_broadcasts("test", limit=1)
        assert [r["txid"] for r in store.node_broadcasts("test", after=first[0]["id"])] == ["t2", "t3"]
        assert [r["txid"] for r in store.node_broadcasts("test", senders=[B])] == ["t2"]
        assert store.node_broadcasts("test", senders=[]) == []
        # published here at height 0, then confirmed in place by the scan
        store.add_node_broadcast("test", "t5", C, "flags/v1", {"f": []}, 0, 0)
        store.add_node_broadcast("test", "t5", C, "flags/v1", {"f": []}, 13, 130)
        assert store.node_broadcasts("test", senders=[C])[0]["height"] == 13
        store.rewind("test", 12)
        assert [r["txid"] for r in store.node_broadcasts("test")] == ["t1", "t2"]
        assert len(store.node_broadcasts("main")) == 1, "another chain's rows are left alone"


# --- flags ---------------------------------------------------------------------------

def _flag(store, txid, sender, entries, height=10, network="test"):
    store.add_node_broadcast(network, txid, sender, flagslib.TOPIC, flagslib.body(entries),
                             height, height * 10)


def test_followed_flags_take_the_latest_word_per_sender_and_the_strictest_across(tmp_path):
    with MessageStore(tmp_path / "s.sqlite") as store:
        _flag(store, "a1", A, [(D1, "sensitive"), (D2, "sensitive")], 10)
        _flag(store, "a2", A, [(D2, "ok")], 11)                       # A takes D2 back
        _flag(store, "b1", B, [(D1, "illegal"), (D3, "sensitive")], 12)
        _flag(store, "c1", C, [(D3, "illegal")], 13)                  # not followed
        store.add_node_broadcast("test", "junk", A, flagslib.TOPIC,
                                 {"f": [["nothex", "sensitive"], [D3, "bogus"], "x"]}, 14, 140)
        assert flagslib.followed_verdicts(store, "test", [A]) == {D1: "sensitive"}
        assert flagslib.followed_verdicts(store, "test", [A, B]) == {D1: "illegal",
                                                                     D3: "sensitive"}
        assert flagslib.followed_verdicts(store, "test", []) == {}
        listed = {b["address"]: b for b in flagslib.broadcasters(store, "test")}
        assert listed[A]["sensitive"] == 2 and listed[A]["retracted"] == 1
        assert listed[B]["illegal"] == 1 and listed[C]["illegal"] == 1


def test_the_broadcaster_batches_never_repeats_and_retracts_only_what_it_said():
    sent, said = [], {}

    def publish(entries):
        sent.append(list(entries))
        said.update(dict(entries))
        return "tx%d" % len(sent)

    out = flagslib.Broadcaster(publish, lambda: dict(said), batch=0.05, retry=0.05)
    assert out.offer(D1, "sensitive") and out.offer(D2, "illegal")
    assert not out.offer(D3, "ok"), "nothing to take back"
    assert not out.offer("short", "sensitive") and not out.offer(D1, "maybe")
    _until(lambda: sent)
    assert sorted(sent[0]) == [(D1, "sensitive"), (D2, "illegal")], "one transaction for both"
    assert not out.offer(D1, "sensitive"), "already said"
    assert out.offer(D1, "ok")
    _until(lambda: len(sent) == 2)
    assert sent[1] == [(D1, "ok")]
    assert out.status()["flags_sent"] == 3 and out.status()["queued"] == 0


def test_a_failed_send_is_kept_and_tried_again():
    tries = []

    def publish(entries):
        tries.append(entries)
        if len(tries) == 1:
            raise ValueError("the fee address has no coins")
        return "tx"

    out = flagslib.Broadcaster(publish, lambda: {}, batch=0.01, retry=0.05)
    out.offer(D1, "sensitive")
    _until(lambda: len(tries) >= 2)
    _until(lambda: out.status()["queued"] == 0)
    assert out.status()["last_error"] == ""


def _until(check, most=5.0):
    end = time.time() + most
    while time.time() < end:
        if check():
            return
        time.sleep(0.02)
    raise AssertionError("never happened")


# --- screening uses them -------------------------------------------------------------

def test_a_followed_flag_covers_content_even_with_screening_off(tmp_path):
    screen = mod.Screen(tmp_path)                         # no model at all
    words = "something a followed arcade flagged"
    digest = mod.digest_of(words)
    assert screen.check_text(words) is None
    screen.flags_source = lambda: {digest: "sensitive"}
    screen._flags = (0.0, {})
    assert screen.check_text(words) == "sensitive"


def test_a_flag_only_makes_things_stricter_and_the_operator_has_the_last_word(tmp_path):
    screen = mod.Screen(tmp_path)
    heard = []
    screen.on_verdict = lambda d, v: heard.append((d, v))
    words = "fine by our model"
    digest = mod.digest_of(words)
    screen._keep(digest, "text", "ok", "")
    assert heard == [], "an ok from the model is not a flag"
    screen.flags_source = lambda: {digest: "illegal"}
    assert screen.known(digest) == "illegal"
    screen.flags_source = lambda: {digest: "ok"}
    screen._flags = (0.0, {})
    assert screen.known(digest) == "ok", "a followed 'ok' clears nothing and adds nothing"
    other = mod.digest_of("ours says sensitive")
    screen._keep(other, "text", "sensitive", "crude")
    assert heard == [(other, "sensitive")], "the model's own flag is offered for broadcast"
    screen.flags_source = lambda: {digest: "sensitive"}
    screen._flags = (0.0, {})
    screen.overrule(words)
    assert screen.known(digest) == "ok", "cleared by this arcade's operator"
    assert heard[-1] == (digest, "ok"), "and a flag this arcade gave is taken back"


def test_a_verdict_taken_from_a_flag_is_never_broadcast_again(tmp_path):
    screen = mod.Screen(tmp_path, {"url": "http://nowhere.invalid/v1", "model": "q"})
    heard = []
    screen.on_verdict = lambda d, v: heard.append((d, v))
    words = "flagged elsewhere"
    screen.flags_source = lambda: {mod.digest_of(words): "sensitive"}
    assert screen.check_text(words) == "sensitive"        # no model asked: the flag answered
    assert heard == []


# --- the admin panel -----------------------------------------------------------------

def test_the_panel_lists_broadcasters_and_saves_both_switches(public):
    app, state = public
    net = state.messaging.network
    with state.store() as store:
        _flag(store, "a1", A, [(D1, "sensitive")], 10, net)
        _flag(store, "b1", B, [(D2, "illegal")], 11, net)
    assert app.post("/admin/api/settings", headers={**LOCAL, **WRITE},
                    json={"flags_broadcast": True, "flags_follow": [B]}).status_code == 200
    assert state.setting("flags:broadcast") is True and state.setting("flags:follow") == [B]
    flags = app.get("/admin/api/state", headers=LOCAL).json()["flags"]
    rows = {b["address"]: b for b in flags["broadcasters"]}
    assert set(rows) == {A, B} and rows[B]["followed"] and not rows[A]["followed"]
    assert flags["broadcast"] is True
    assert state.followed_flags() == {D2: "illegal"}
    assert app.post("/admin/api/settings", headers={**LOCAL, **WRITE},
                    json={"flags_follow": ["not an address"]}).status_code == 400
    page = app.get("/admin", headers=LOCAL).text
    assert 'id="flags_broadcast"' in page and 'id="flaglist"' in page


def test_the_bot_rpc_reads_confirmed_broadcasts(client):
    from arcade.web.rpc import OmniRpc
    app, state = client
    with state.store() as store:
        store.add_node_broadcast(state.messaging.network, "t1", A, "game/v1", {"x": 1}, 10, 100)
        store.add_node_broadcast(state.messaging.network, "t2", A, "game/v1", {"x": 2}, 0, 0)
    assert "da_publish" in dir(OmniRpc) and "da_broadcasts" in dir(OmniRpc)
    rows = OmniRpc.da_broadcasts.__get__(_Rpc(state))("game/v1")
    assert [r["body"] for r in rows] == [{"x": 1}], "a broadcast still in the pool is not read"


class _Rpc:
    """The two attributes da_broadcasts reads."""
    def __init__(self, state):
        self.state = state


# --- on a real chain -----------------------------------------------------------------

from test_account_offer import _settled, node                  # noqa: E402,F401


def test_a_node_publishes_and_every_scan_reads_it(node):
    """The node's fee address publishes; the scan files it under its topic, and a
    followed arcade's flags cover content here."""
    app, state, rpc = node
    if float(rpc.call("getbalance")) < 20:
        rpc.call("generate", 101)
    fee = rpc.call("getnewaddress")
    rpc.call("sendtoaddress", fee, 5)
    rpc.call("generate", 1)
    with state.store() as store:
        store.set_meta(f"identity_address:{state.messaging.network}", fee)

    txid = state.send_broadcast("game/v1", {"week": 41})
    flags_txid = state.send_broadcast(flagslib.TOPIC, flagslib.body([(D1, "sensitive")]))
    _settled(state, rpc)
    with state.store() as store:
        rows = {r["txid"]: r for r in store.node_broadcasts(state.messaging.network)}
    assert rows[txid]["height"] > 0 and rows[txid]["sender"] == fee
    assert json.loads(rows[txid]["body"]) == {"week": 41}
    assert state.own_flags() == {D1: "sensitive"}

    state.set_setting("flags:follow", [fee])
    assert state.followed_flags() == {D1: "sensitive"}
    assert rows[flags_txid]["topic"] == flagslib.TOPIC

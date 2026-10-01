"""Game state on pieces: written only by a game's publisher, ordered, read by anyone.

The record format without a chain, then the whole of it on regtest: a game
declares its publisher and judge, a page asks, the judge decides, the
publisher's announcement lands in a block, every reader sees the newest
sequence number -- and a record from anybody else, an older one replayed, or a
second creator claiming the same family name changes nothing.
"""

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_offer import _settled, node                          # noqa: E402,F401
from test_web import app_state, client                               # noqa: F401,E402

from arcade import gamestate as G                                    # noqa: E402

PIECE = "e4" * 32
JUDGE = (b"function judge(seed, inputs, params) {"
         b" return inputs && inputs.ok === true ? {update: true}"
         b" : {update: false, why: 'that run never happened'}; }")


def test_a_record_is_checked_before_it_is_read():
    payload = G.build("ashvale", [[PIECE, 3, {"c": 62}]])
    assert G.parse(payload) == {"family": "ashvale", "updates": [(PIECE, 3, {"c": 62})]}
    for family, updates in (("has space", [[PIECE, 1, {}]]), ("f", [[PIECE, 0, {}]]),
                            ("f", [["xx", 1, {}]]), ("f", [[PIECE, 1, {"c": "x" * 200}]]),
                            ("f", [[PIECE, 1, [1]]]), ("f", [])):
        with pytest.raises(G.StateError):
            G.build(family, updates)


def test_only_the_publishers_highest_sequence_counts():
    rows = [{"sender": "pub", "seq": 2, "state": '{"c":70}', "height": 10, "position": 1, "txid": "b"},
            {"sender": "pub", "seq": 1, "state": '{"c":90}', "height": 12, "position": 0, "txid": "c"},
            {"sender": "mallory", "seq": 99, "state": '{"c":100}', "height": 13, "position": 0, "txid": "d"}]
    now = G.current(rows, "pub")
    assert now["state"] == {"c": 70} and now["seq"] == 2
    assert G.current(rows, "nobody") is None


def _row(state, txid, number, owner, kind, content, said=None):
    index = state.token_index(state.messaging)
    with index.open() as db:
        db.conn.execute(
            "INSERT OR REPLACE INTO inscription(txid,number,creator,owner,block_height,position,"
            "content_type,content_len,sha256,json,chunks,content) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (txid, number, owner, owner, 1, number, kind, len(content), "ab" * 32,
             json.dumps(said or {}), 1, content))
        db.conn.commit()


def _funded(rpc):
    if float(rpc.call("getbalance")) < 20:
        rpc.call("generate", 101)
    address = rpc.call("getnewaddress")
    rpc.call("sendtoaddress", address, 5)
    return address


def _publish_as(state, rpc, sender, family, updates):
    """Somebody else putting a record on the chain: any address can pay for one."""
    from arcade.messaging.sender import MessageSender
    out = MessageSender(rpc, state.messaging.params, public_only=True)
    return out.broadcast(out.prepare(sender, G.build(family, updates), change_address=sender))


def test_a_game_writes_state_and_nobody_else_can(node):
    app, state, rpc = node
    publisher = _funded(rpc)
    with state.store() as store:
        store.set_meta(f"identity_address:{state.messaging.network}", publisher)
    creator, stranger = "n" + "C" * 33, "n" + "D" * 33
    judge, game, squatter = "e1" * 32, "e2" * 32, "e3" * 32
    _row(state, judge, 8001, creator, "application/javascript", JUDGE)
    _row(state, game, 8002, creator, "text/html", b"<p>game",
         {"game": {"name": "Wear Test", "family": "weartest"},
          "state": {"publisher": publisher, "judge": judge, "show": {"c": "Condition"}}})
    _row(state, PIECE, 8003, creator, "text/plain", b"a sword")
    _settled(state, rpc)

    ask = {"game": game, "items": [{"piece": PIECE, "state": {"c": 90}}]}
    no = app.post("/r/state/update", json={**ask, "replay": {"ok": False}})
    assert no.status_code == 400 and "never happened" in no.text
    yes = app.post("/r/state/update", json={**ask, "replay": {"ok": True}})
    assert yes.status_code == 200, yes.text
    assert yes.json()["updates"] == [[PIECE, 1, {"c": 90}]]
    _settled(state, rpc)
    seen = app.get(f"/r/state/{PIECE}").json()["games"]
    assert len(seen) == 1 and seen[0]["state"] == {"c": 90} and seen[0]["seq"] == 1
    assert seen[0]["show"] == {"c": "Condition"} and seen[0]["name"] == "Wear Test"

    worn = app.post("/r/state/update", json={"game": game, "replay": {"ok": True},
                                             "items": [{"piece": PIECE, "state": {"c": 70}}]})
    assert worn.json()["updates"] == [[PIECE, 2, {"c": 70}]]
    _settled(state, rpc)

    # Somebody else's record, at a far higher number: not the game's, so nothing.
    _publish_as(state, rpc, _funded(rpc), "weartest", [[PIECE, 99, {"c": 100}]])
    # The publisher's own older record, sent again: older, so nothing.
    _publish_as(state, rpc, publisher, "weartest", [[PIECE, 1, {"c": 90}]])
    # A second creator claiming the family name, with a publisher of its own.
    rogue = _funded(rpc)
    _row(state, squatter, 8004, stranger, "text/html", b"<p>mine now",
         {"game": {"name": "Wear Test 2", "family": "weartest"},
          "state": {"publisher": rogue, "judge": judge}})
    _publish_as(state, rpc, rogue, "weartest", [[PIECE, 50, {"c": 100}]])
    _settled(state, rpc)
    one = app.get(f"/r/state/weartest/{PIECE}").json()
    assert one["state"] == {"c": 70} and one["seq"] == 2, one
    assert app.get(f"/r/state/otherfam/{PIECE}").status_code == 404
    page = app.get(f"/inscriptions/{PIECE}/view").text
    assert 'id="game-state"' in page and f"/r/state/{PIECE}" in page, \
        "the piece page draws every game's state for it"

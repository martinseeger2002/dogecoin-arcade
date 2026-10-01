"""The escrow through the arcade: a game declares it, an account opens one, deposits
from its own key, the game's judge releases, and the owner takes the rest back.

On regtest, with an account whose key this node never holds -- the same
seated account the offer and trade files use -- so every transaction is one a
real wallet signed and a real node accepted.
"""

import base64
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_offer import _inscribed, _seated, _settled, _signed, node   # noqa: E402,F401
from test_account_tokens import _balance, _token                               # noqa: E402
from test_funding import _sign                                                 # noqa: E402
from test_web import app_state, client                                       # noqa: F401,E402

from arcade import escrow as E                                               # noqa: E402

COIN = 100_000_000
JUDGE = (b"function judge(seed, inputs, params) {"
         b" return inputs && inputs.ok === true"
         b" ? {release: true} : {release: false, why: 'not earned in the game'}; }")


def _row(state, txid, number, owner, kind, content, said=None):
    index = state.token_index(state.messaging)
    with index.open() as db:
        db.conn.execute(
            "INSERT OR REPLACE INTO inscription(txid,number,creator,owner,block_height,position,"
            "content_type,content_len,sha256,json,chunks,content) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (txid, number, owner, owner, 1, 0, kind, len(content), "ab" * 32,
             json.dumps(said or {}), 1, content))
        db.conn.commit()


def _game(app, state, owner):
    """A game whose JSON names this node as its escrow's referee, and a judge."""
    referee = app.get("/r/referee").json()["pubkey"]
    judge, game = "c1" * 32, "c2" * 32
    _row(state, judge, 9100, owner, "application/javascript", JUDGE)
    _row(state, game, 9101, owner, "text/html", b"<p>a game",
         {"game": {"name": "Escrow Test"},
          "escrow": {"referee": {"pubkey": referee}, "judge": judge, "unlock_hours": 48}})
    return game


def _deposit(who, secret, pubkey, state, rpc, escrow, item):
    offered = who.post("/account/escrow/deposit", json={"escrow": escrow, "item": item})
    assert offered.status_code == 200, offered.text
    done = _signed(who, secret, pubkey, offered.json())
    assert done.status_code == 200, done.text
    return done.json()["txid"]


def test_open_deposit_judge_release_and_no_early_reclaim(node):
    app, state, rpc = node
    who, secret, pubkey, owner = _seated(app, state, rpc, 91, coins=(4.0, 1.0, 1.0))
    sword = _inscribed(who, state, rpc, secret, pubkey, "a sword")
    pid = _token(state, property_id=160, name="Gold")
    _balance(state, owner, pid, 100 * COIN)
    game = _game(app, state, owner)

    opened = who.post("/account/escrow/open", json={"game": game, "hours": 6})
    assert opened.status_code == 200, opened.text
    escrow, unlock = opened.json()["escrow"], opened.json()["unlock"]
    assert 5 * 3600 < unlock - time.time() <= 7 * 3600, "about the hours asked, on the hour"
    _deposit(who, secret, pubkey, state, rpc, escrow, {"inscription": sword})
    _deposit(who, secret, pubkey, state, rpc, escrow, {"token": pid, "amount": "30"})
    _settled(state, rpc)
    held = app.get(f"/r/escrow/{escrow}").json()
    assert held["inscriptions"] == [sword] and held["tokens"] == {str(pid): 30 * COIN}
    assert held["owner"] == owner and held["open"] is False

    other = "n" + "1" * 33 if False else _seated(app, state, rpc, 92)[3]
    ask = {"game": game, "escrow": escrow, "owner": owner, "owner_pubkey": pubkey.hex(),
           "unlock": unlock, "to": other, "items": [{"inscription": sword}]}
    no = app.post("/r/escrow/release", json={**ask, "replay": {"ok": False}})
    assert no.status_code == 400 and "not earned" in no.text
    yes = app.post("/r/escrow/release", json={**ask, "replay": {"ok": True}})
    assert yes.status_code == 200, yes.text
    _settled(state, rpc)
    index = state.token_index(state.messaging)
    assert index.inscription(sword)["owner"] == other, "the judge said so, so it went"

    early = who.post("/account/escrow/reclaim", json={"escrow": escrow})
    assert early.status_code == 400 and "still the game" in early.text
    mine = who.get("/account/escrows").json()["escrows"]
    assert [e["escrow"] for e in mine] == [escrow] and mine[0]["tokens"] == {str(pid): 30 * COIN}


def test_after_unlock_the_owner_takes_everything_back(node, monkeypatch):
    app, state, rpc = node
    who, secret, pubkey, owner = _seated(app, state, rpc, 93, coins=(4.0, 1.0, 1.0))
    shield = _inscribed(who, state, rpc, secret, pubkey, "a shield")
    pid = _token(state, property_id=161, name="Gems")
    _balance(state, owner, pid, 10 * COIN)
    game = _game(app, state, owner)
    # Opened and filled three hours ago, so its hour is already past.
    from arcade.web import app as appmod
    real = time.time
    monkeypatch.setattr(appmod.time, "time", lambda: real() - 3 * 3600)
    escrow = who.post("/account/escrow/open", json={"game": game, "hours": 1}).json()["escrow"]
    _deposit(who, secret, pubkey, state, rpc, escrow, {"inscription": shield})
    _deposit(who, secret, pubkey, state, rpc, escrow, {"token": pid, "amount": "4"})
    monkeypatch.setattr(appmod.time, "time", real)
    _settled(state, rpc)

    asked = who.post("/account/escrow/reclaim", json={"escrow": escrow})
    assert asked.status_code == 200, asked.text
    said = asked.json()
    assert len(said["txs"]) == 2
    signed = [{"raw": t["raw"], "signature": _sign(secret, bytes.fromhex(t["sighash"])).hex()}
              for t in said["txs"]]
    done = who.post("/account/escrow/reclaim/sign", json={"escrow": escrow, "txs": signed})
    assert done.status_code == 200, done.text
    _settled(state, rpc)
    index = state.token_index(state.messaging)
    assert index.inscription(shield)["owner"] == owner
    assert int(index.balance(owner, pid)) == 10 * COIN, "and the gems, every unit"
    assert who.get("/account/escrows").json()["escrows"] == [], "an empty escrow is not listed"

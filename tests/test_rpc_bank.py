"""A game's bank on your own node (examples/rpc_bank): the room by certificate, and the bank's rules.

The certificate is checked by a real node (`verifymessage` on regtest), not a stand-in: a signature this side makes and
Core refuses would join nobody. The bank's own rules -- answer once, cap, coalesce, never pay a job twice, sweep back
only what is certain -- run on a temporary database with a stand-in publisher; the payouts themselves are
tests/test_publish_transfers.py.
"""

import importlib.util
import json
import secrets
import sqlite3
import time
from pathlib import Path

import pytest

from arcade import publish
from arcade.service import CertRoom, ServiceError, sign_message
from test_realtime_web import two_nodes                 # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]


def test_a_signed_message_is_what_core_verifies(regtest):
    for _ in range(4):
        secret = secrets.randbelow(publish.N - 1) + 1
        address = publish.Key(secret).address(regtest.params)
        words = f"arcade-realtime\narcade-test\nnode\n{address}\n{int(time.time()) + 86400}"
        assert regtest.rpc.call("verifymessage", address, sign_message(secret, words), words) is True
    assert regtest.rpc.call("verifymessage", address, sign_message(secret, words), words + "!") is False


def test_a_cert_room_joins_through_the_door_as_its_own_address(two_nodes, regtest):      # noqa: F811
    (ca, sa), _ = two_nodes
    sa.mesh.verify = lambda address, sig, words: bool(regtest.rpc.call("verifymessage", address, sig, words))
    secret = secrets.randbelow(publish.N - 1) + 1
    address = publish.Key(secret).address(regtest.params)

    room = CertRoom(secret, address, "mygame", "bank", node=str(ca.base_url))
    room.http.headers["CF-Connecting-IP"] = "203.0.113.9"   # as a request through the door arrives
    out = room.join()
    assert out["me"]["address"] == address and not out["me"]["guest"], out["me"]
    room.send({"t": "hello"})
    room.leave()

    wrong = CertRoom(secrets.randbelow(publish.N - 1) + 1, address, "mygame", "bank", node=str(ca.base_url))
    wrong.http.headers["CF-Connecting-IP"] = "203.0.113.9"
    with pytest.raises(ServiceError, match="does not prove"):
        wrong.join()


# ------------------------------------------------------------------ the bank's rules, offline

def _bank_module():
    spec = importlib.util.spec_from_file_location("rpc_bank", ROOT / "examples" / "rpc_bank" / "bank.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeJournal:
    def __init__(self):
        self.by_key = {}


class FakePub:
    def __init__(self):
        self.transfers, self.sent, self.known = FakeJournal(), [], set()

    def transfer_token(self, pid, units, to, intent, dry=False):
        if intent in self.transfers.by_key:                       # as the real journal: an intent is paid once
            return {"txid": self.transfers.by_key[intent]["txid"]}
        txid = f"tx{len(self.sent) + 1}"
        self.sent.append((pid, units, to, intent))
        self.transfers.by_key[intent] = {"intent": intent, "txid": txid}
        self.known.add(txid)
        return {"txid": txid}

    def _confirmations(self, txid):
        return 1 if txid in self.known else None


@pytest.fixture
def bank(tmp_path):
    mod = _bank_module()
    b = mod.Bank.__new__(mod.Bank)
    b.cfg = {**mod.DEFAULTS, "tokens": {"gold": 7}, "per_day": 100, "hold_s": 0}
    b.home, b.dry, b.tokens, b.outbox = tmp_path, False, {"gold": 7}, []
    b.state_path, b.state = tmp_path / "reclaim.json", {"dead": {}, "redo": {}}
    b.pub, b.address = FakePub(), "bank-address"
    b.ledger = str(tmp_path / "ledger.sqlite")
    with sqlite3.connect(b.ledger) as db:
        db.execute("create table arcade_tx(txid text primary key, valid integer, invalid_reason text)")
    b.custody = lambda pid: (1_000, {"divisible": False, "managed": False, "issuer": "bank-address"})
    return b


def test_a_deposit_is_answered_once_capped_and_queued(bank):
    c = bank.db()
    first = bank.handle(c, "player", {"t": "dep", "id": "r1", "items": {"gold": 60, "magic": 1}})
    assert first["ok"] and first["paid"] == {"gold": 60} and first["refused"] == ["magic"]
    assert bank.handle(c, "player", {"t": "dep", "id": "r1", "items": {"gold": 60}})["note"] == "Already on its way."
    second = bank.handle(c, "player", {"t": "dep", "id": "r2", "items": {"gold": 60}})
    assert second["paid"] == {"gold": 40}, "the day's cap is 100"
    assert c.execute("select sum(units) from jobs").fetchone()[0] == 100


def test_jobs_of_one_player_and_item_go_as_one_payout_and_never_twice(bank):
    c = bank.db()
    for i in range(3):
        bank.handle(c, "player", {"t": "dep", "id": f"r{i}", "items": {"gold": 5}})
    job, ids = bank.take_batch(c)
    assert job["units"] == 15 and len(ids) == 3
    txid = bank.pay(job, ids)
    assert bank.pub.sent[0][:3] == (7, 15, "player") and txid == "tx1"
    assert bank.pay(job, ids) == "tx1" and len(bank.pub.sent) == 1, "the same jobs again: the same payout"
    with pytest.raises(RuntimeError, match="HOLD"):
        bank.pay({**job, "units": 5}, ids[:1])                    # a job inside a payout already sent, under another intent
    assert len(bank.pub.sent) == 1


def _done(bank, c, txid, n=1):
    for _ in range(n):
        c.execute("insert into jobs(addr,item,units,status,txid,at,done_at) values('p','gold',5,'done',?,?,?)",
                  (txid, time.time() - 3600, time.time() - 3600))
    c.commit()


def test_sweep_back_pays_again_only_what_is_certain(bank):
    c = bank.db()
    _done(bank, c, "good"); _done(bank, c, "refused", 2); _done(bank, c, "mine"); _done(bank, c, "stranger")
    with sqlite3.connect(bank.ledger) as db:
        db.execute("insert into arcade_tx values('good', 1, null)")
        db.execute("insert into arcade_tx values('refused', 0, 'not enough tokens')")
    bank.pub.transfers.by_key["x"] = {"intent": "bank send jobs=9 7 5 p", "txid": "mine"}
    bank.reclaim(c)
    status = lambda t: sorted({r[0] for r in c.execute("select status from jobs where txid=? or err like ?", (t, f"%{t}%"))})
    assert status("good") == ["done"]
    assert status("refused") == ["queued"] and bank.redo_of([2, 3]) == 1, "refused by the chain: once more"
    assert status("mine") == ["queued"], "lost by the node and signed here: the same bytes again"
    assert status("stranger") == ["held"], "lost and not signed here: a person looks"
    assert json.loads(bank.state_path.read_text())["dead"] == {"refused": "invalid: not enough tokens"}

    c.execute("update jobs set status='done', txid='refused2', done_at=? where txid is null", (time.time() - 3600,))
    c.commit()
    with sqlite3.connect(bank.ledger) as db:
        db.execute("insert into arcade_tx values('refused2', 0, 'not enough tokens')")
    bank.reclaim(c)
    assert status("refused2") == ["held"], "a second refusal is never paid a third time by itself"

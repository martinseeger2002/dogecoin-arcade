"""An account's coin history: what each transaction did to its addresses.

2026-10-08, of the Wallet tab: "It needs to give feedback when
transactions are sent it should have a history." The coin index keeps only
what can still be spent (arcade/utxos.py deletes a spent row), so a history
needs rows of its own -- one per (transaction, address), written where coins
are added and spent, journalled so a reorg takes them back with the coins.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_utxos import MINE, THEIRS, an_address, connect, db, paying, rollback  # noqa: F401,E402
from test_account_offer import node, _seated, _settled, _signed                # noqa: F401,E402

from arcade import utxos  # noqa: E402

ELSEWHERE = an_address(0x33)


def test_a_payment_in_is_a_row_with_what_arrived(db):
    utxos.watch(db, MINE, 100)
    connect(db, 101, paying("aa" * 32, (MINE, 500), (THEIRS, 700)))
    [row] = utxos.activity(db, [MINE])
    assert row == {"txid": "aa" * 32, "height": 101, "time": 101, "received": 500,
                   "spent": 0, "net": 500, "fee": 0, "other": ""}


def test_a_send_says_who_it_paid_and_what_it_cost(db):
    """Change comes back to the same address, so the net is what left; the
    fee is what nobody was paid."""
    utxos.watch(db, MINE, 100)
    connect(db, 101, paying("aa" * 32, (MINE, 1000)))
    connect(db, 102, paying("bb" * 32, (THEIRS, 300), (MINE, 690),
                            spending=[("aa" * 32, 0)]))
    sent, got = utxos.activity(db, [MINE])
    assert sent["txid"] == "bb" * 32 and got["txid"] == "aa" * 32, "newest first"
    assert sent["net"] == -310 and sent["fee"] == 10
    assert sent["other"] == THEIRS


def test_a_disconnected_block_takes_its_history_back(db):
    utxos.watch(db, MINE, 100)
    connect(db, 101, paying("aa" * 32, (MINE, 500)))
    rollback(db, 101)
    assert utxos.activity(db, [MINE]) == []


def test_nothing_is_written_for_addresses_nobody_watches(db):
    connect(db, 101, paying("aa" * 32, (MINE, 500)))
    assert utxos.activity(db, [MINE]) == []


def test_history_pages_by_height(db):
    utxos.watch(db, MINE, 100)
    for n in range(5):
        connect(db, 101 + n, paying(f"{n:02x}" * 32, (MINE, 100 + n)))
    first = utxos.activity(db, [MINE], limit=2)
    assert [r["height"] for r in first] == [105, 104]
    rest = utxos.activity(db, [MINE], before=first[-1]["height"], limit=10)
    assert [r["height"] for r in rest] == [103, 102, 101]


def test_an_account_reads_its_own_history_and_who_it_paid(node):
    """End to end: coins arrive, the account sends some on, and the Wallet
    tab's endpoint lists both with confirmations, newest first."""
    app, state, rpc = node
    who, secret, pubkey, address = _seated(app, state, rpc, 71, key=False)
    them = rpc.call("getnewaddress")
    offer = who.post("/account/send", json={"to": them, "amount": "1.5"})
    assert offer.status_code == 200, offer.text
    done = _signed(who, secret, pubkey, offer.json())
    assert done.status_code == 200, done.text
    _settled(state, rpc)

    said = who.get("/account/activity").json()
    assert said["address"] == address and said["since"] is not None
    rows = said["rows"]
    assert rows[0]["txid"] == done.json()["txid"]
    assert rows[0]["net"] < -150_000_000 and rows[0]["other"] == them
    assert rows[0]["confirmations"] >= 1
    assert sum(1 for r in rows if r["net"] > 0) == 2, "the two coins it was given"


def test_history_is_only_ever_your_own(node):
    app, state, rpc = node
    from fastapi.testclient import TestClient

    stranger = TestClient(app.app)
    assert stranger.get("/account/activity").status_code in (401, 403)
    who, *_ = _seated(app, state, rpc, 72, coins=(1.0,), key=False)
    assert who.get("/account/activity?chain=nowhere").status_code == 404

"""Token metadata for pages: /r/token/<id>, /r/tokens?ids=..., and the issuer in
/r/balances -- so a game can classify a wallet by its own fields (2026-10-01)."""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_account_tokens import _balance                          # noqa: E402

ISSUER = "ndTq6goKXGb6JLoRRQPwks2kn6bWeGKX1G"


def _issue(state, pid, name, category, subcategory, data, divisible=True):
    index = state.token_index(state.messaging)
    with index.open() as db:
        db.conn.execute(
            "INSERT OR REPLACE INTO property(property_id, ecosystem, property_type, issuer,"
            " category, subcategory, name, url, data, managed, total_tokens, creation_txid,"
            " creation_block) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, 1, 2 if divisible else 1, ISSUER, category, subcategory, name,
             "https://example.com", data, 0, 1000, "aa" * 32, 1))
        db.conn.commit()


def test_a_page_reads_a_token_with_its_own_schema_intact(client):
    app, state = client
    data = json.dumps({"about": "a potion", "icon": "", "mygame": {"tier": 3, "heals": 40}})
    _issue(state, 220, "Potion", "Consumable", "Healing", data)
    _issue(state, 221, "Plain", "Misc", "", "just words")
    got = app.get("/r/token/220").json()
    assert got["category"] == "Consumable" and got["subcategory"] == "Healing"
    assert got["details"]["mygame"] == {"tier": 3, "heals": 40}, "the game's keys, untouched"
    assert got["described"]["about"] == "a potion" and got["issuer"] == ISSUER
    assert got["divisible"] is True and got["supply"] == 1000 and got["data"] == data
    plain = app.get("/r/token/221").json()
    assert plain["details"] is None and plain["described"]["about"] == "just words"
    assert app.get("/r/token/999").status_code == 404
    batch = app.get("/r/tokens?ids=220,999,221,x").json()
    assert [t["propertyid"] for t in batch] == [220, 221], "unknown ids are left out"


def test_balances_name_who_issued_each_token(client):
    app, state = client
    _issue(state, 222, "Gold", "Currency", "", "{}")
    _balance(state, "nHolderAAAAAAAAAAAAAAAAAAAAAAAAAAA", 222, 5)
    held = app.get("/r/balances/nHolderAAAAAAAAAAAAAAAAAAAAAAAAAAA").json()
    assert held[0]["propertyid"] == 222 and held[0]["issuer"] == ISSUER

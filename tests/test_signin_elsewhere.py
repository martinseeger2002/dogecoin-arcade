"""Signing in with an account made on another arcade, without naming it (2026-10-19: "I'm still not able to
login with my account created on dogecoinarcade.com through pepecoinarcade.com ... I shouldn't have to enter a URL for
the other Arcade. It should just know automatically")."""
import json

from fastapi.testclient import TestClient

from arcade.web import app as appmod
from arcade.web.app import create_app
from arcade.web.state import AppState, ChainContext

BLOB = {"sealed": "c2VhbGVk", "salt": "c2FsdA==", "v": 1}


def _arcade(tmp_path, name):
    from pathlib import Path
    (tmp_path / name).mkdir()
    st = AppState(home=tmp_path / name,
                  messaging=ChainContext(network="regtest", role="messaging", label="Testnet",
                                         datadir=Path("/nonexistent")),
                  ledger=ChainContext(network="main", role="ledger", label="Mainnet", datadir=Path("/nonexistent")))
    return st, TestClient(create_app(st))


def test_a_name_made_on_another_arcade_is_found_there_by_itself(tmp_path, no_nodes, monkeypatch):
    home, home_client = _arcade(tmp_path, "home")
    here, here_client = _arcade(tmp_path, "here")
    home.vault().put("wanderer", "ab" * 32, "nWandererAddressXXXXXXXXXXXXXXXXXXX", json.dumps(BLOB))
    with here.store() as store:                       # the home arcade's mesh, as its announcement says it
        store.add_mesh_announcement("t1", here.messaging.network, "nHome", "wss://home.example.com/mesh", 443,
                                    "cd" * 32, 0, 0)

    class Answer:
        def __init__(self, r):
            self.status_code, self._r = r.status_code, r

        def json(self):
            return self._r.json()

    asked = []

    def get(url, timeout=0, headers=None):
        asked.append(url)
        assert url.startswith("https://home.example.com/signin/wanderer") and "here=1" in url
        return Answer(home_client.get(url.replace("https://home.example.com", "")))
    monkeypatch.setattr(appmod.requests, "get", get)

    said = here_client.get("/signin/wanderer").json()
    assert said["elsewhere"] is True and said["from"] == "https://home.example.com" and said["blob"] == BLOB
    assert said["address"] == "nWandererAddressXXXXXXXXXXXXXXXXXXX"
    assert home_client.get("/signin/nobody?here=1").status_code == 404, "an arcade asked never asks on"
    here_client.get("/signin/wanderer")
    assert len(asked) == 1, "kept a while, not asked again"


def test_an_accounts_coins_come_from_its_arcade_and_are_checked_here(tmp_path, no_nodes, monkeypatch):
    """2026-10-19: "Why does it have to read a bunch of blocks when I login can't it get the information it
    needs from another arcade?" The arcade it came from lists the coins; each is checked against this chain."""
    import contextlib
    from arcade import utxos
    home, home_client = _arcade(tmp_path, "home")
    here, _ = _arcade(tmp_path, "here")
    addr = "nWandererAddressXXXXXXXXXXXXXXXXXXX"
    home.vault().put("wanderer", "ab" * 32, addr, json.dumps(BLOB))
    home.set_setting(f"address:{'ab' * 32}", addr)
    with contextlib.closing(home.token_index(home.messaging).open()) as db:
        utxos.watch(db, addr, 10, why="test")
        for txid, value in (("aa" * 32, 5), ("bb" * 32, 7), ("cc" * 32, 9)):
            db.conn.execute("INSERT INTO utxo (txid, vout, address, value, height) VALUES (?,?,?,?,?)",
                            (txid, 0, addr, value * 100_000_000, 20))
        db.conn.commit()
    listed = home_client.get("/signin/wanderer/coins").json()["chains"][home.messaging.network]
    assert listed["address"] == addr and len(listed["coins"]) == 3

    class Answer:
        status_code = 200

        def json(self):
            return home_client.get("/signin/wanderer/coins").json()
    monkeypatch.setattr(appmod.requests, "get", lambda url, timeout=0, headers=None: Answer())

    class Rpc:                                  # this arcade's own chain: aa is real, bb is spent, cc is not theirs
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def call(self, method, txid, vout, mempool):
            return {"aa" * 32: {"confirmations": 3, "value": 5, "scriptPubKey": {"addresses": [addr]}},
                    "cc" * 32: {"confirmations": 3, "value": 9, "scriptPubKey": {"addresses": ["nSomebodyElse"]}}
                    }.get(txid)

        def get_block_count(self):
            return 100
    monkeypatch.setattr(type(here.messaging), "rpc", lambda self: Rpc())
    filed = appmod._coins_from(here, "https://home.example.com", "wanderer", here.messaging, addr)
    assert filed == 1, "only the coin this chain says is unspent, and theirs"
    with contextlib.closing(here.token_index(here.messaging).open()) as db:
        assert [(c["txid"], c["value"], c["height"]) for c in utxos.unspent(db, addr)] == [("aa" * 32, 5 * 100_000_000, 98)]


def test_an_arcade_tells_another_an_accounts_history_and_what_it_holds(tmp_path, no_nodes):
    """2026-10-19: history and balances differed between two arcades. Each serves an account's history and the coins it
    holds for its offers, for the other to show and to leave alone."""
    import contextlib
    from arcade import utxos
    home, client = _arcade(tmp_path, "home")
    addr = "nWandererAddressXXXXXXXXXXXXXXXXXXX"
    home.vault().put("wanderer", "ab" * 32, addr, json.dumps(BLOB))
    home.set_setting(f"address:{'ab' * 32}", addr)
    with contextlib.closing(home.token_index(home.messaging).open()) as db:
        utxos.watch(db, addr, 10, why="test")
        db.conn.execute("INSERT INTO utxo (txid, vout, address, value, height) VALUES (?,?,?,?,?)",
                        ("aa" * 32, 0, addr, 5 * 100_000_000, 20))
        db.conn.execute("INSERT INTO activity (txid, address, height, received, spent, paid_out, other) "
                        "VALUES (?,?,?,?,?,?,?)", ("aa" * 32, addr, 20, 5 * 100_000_000, 0, 0, ""))
        db.conn.commit()
    said = client.get("/signin/wanderer/activity", params={"chain": home.messaging.network}).json()
    assert said["address"] == addr and [r["txid"] for r in said["rows"]] == ["aa" * 32]
    coins = client.get("/signin/wanderer/coins").json()["chains"][home.messaging.network]
    assert coins["held"] == [], "nothing set aside for an offer yet"
    assert client.get("/signin/nobody/activity", params={"chain": home.messaging.network}).status_code == 404

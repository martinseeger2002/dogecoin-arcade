"""Coin lists between arcades (2026-10-25): every holder's list merged, each coin checked here, coverage
carried. The operator: "any of the current arcade accounts that are held by more than one arcade. Like @apple the UTXO's
and balances cross arcade need to be reconciled." """
import contextlib
import json

from fastapi.testclient import TestClient

from arcade import coinlist, utxos
from arcade.web import app as appmod
from arcade.web.app import create_app
from arcade.web.state import AppState, ChainContext

BLOB = {"sealed": "c2VhbGVk", "salt": "c2FsdA==", "v": 1}
ADDR = "nWandererAddressXXXXXXXXXXXXXXXXXXX"
PUB = "ab" * 32


def _arcade(tmp_path, name):
    from pathlib import Path
    (tmp_path / name).mkdir()
    st = AppState(home=tmp_path / name,
                  messaging=ChainContext(network="regtest", role="messaging", label="Testnet",
                                         datadir=Path("/nonexistent")),
                  ledger=ChainContext(network="main", role="ledger", label="Mainnet", datadir=Path("/nonexistent")))
    return st, TestClient(create_app(st))


def _holds(st, since):
    st.vault().put("wanderer", PUB, ADDR, json.dumps(BLOB))
    st.set_setting(f"address:{PUB}", ADDR)
    with contextlib.closing(st.token_index(st.messaging).open()) as db:
        utxos.watch(db, ADDR, since, why="test")
        db.conn.commit()


class _Rpc:            # this arcade's own chain: aa is real, bb is spent, cc pays somebody else
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def call(self, method, txid, vout, mempool):
        return {"aa" * 32: {"confirmations": 3, "value": 5, "scriptPubKey": {"addresses": [ADDR]}},
                "cc" * 32: {"confirmations": 3, "value": 9, "scriptPubKey": {"addresses": ["nSomebodyElse"]}}
                }.get(txid)

    def batch(self, calls):
        return [self.call(m, *p) for m, p in calls]

    def get_block_count(self):
        return 100


def test_the_public_list_has_every_address_coverage_and_coins(tmp_path, no_nodes):
    home, client = _arcade(tmp_path, "home")
    _holds(home, 10)
    with contextlib.closing(home.token_index(home.messaging).open()) as db:
        db.conn.execute("INSERT INTO utxo VALUES (?,?,?,?,?)", ("aa" * 32, 0, ADDR, 5 * 10**8, 20))
        coinlist.mark_covered(db, ADDR, [(0, 9)], "test")
        db.conn.commit()
    said = client.get("/signin/wanderer/coins").json()
    assert said["v"] == coinlist.VERSION
    mine = said["lists"][home.messaging.network][ADDR]
    assert mine["coins"] == [["aa" * 32, 0, 5 * 10**8, 20, ""]]
    assert mine["covered"][0][0] == 0, "watching from 10 plus the range read before it"
    assert said["chains"][home.messaging.network]["address"] == ADDR, "the old shape stays for older arcades"


def test_two_holders_reconcile_coins_checked_and_coverage_merged(tmp_path, no_nodes, monkeypatch):
    home, home_client = _arcade(tmp_path, "home")
    here, _ = _arcade(tmp_path, "here")
    _holds(home, 10)
    _holds(here, 50)                                    # here started watching later, and missed what came before
    with contextlib.closing(home.token_index(home.messaging).open()) as db:
        for txid in ("aa" * 32, "bb" * 32, "cc" * 32):
            db.conn.execute("INSERT INTO utxo VALUES (?,?,?,?,?)", (txid, 0, ADDR, 5 * 10**8, 20))
        coinlist.mark_covered(db, ADDR, [(0, 100)], "test")     # (a test index reads no blocks: its tip is 0)
        db.conn.commit()

    class Answer:
        status_code = 200

        def json(self):
            return home_client.get("/signin/wanderer/coins").json()
    monkeypatch.setattr(appmod.requests, "get", lambda url, timeout=0, headers=None, **kw: Answer())
    monkeypatch.setattr(appmod, "_host_is_public", lambda host: True)
    monkeypatch.setattr(type(here.messaging), "rpc", lambda self: _Rpc())

    got = here.reconcile_coins("wanderer", ["home.example.com"])
    assert got["asked"] == ["home.example.com"] and got["filed"] == 1, "only the coin this chain says is real and theirs"
    with contextlib.closing(here.token_index(here.messaging).open()) as db:
        assert [c["txid"] for c in utxos.unspent(db, ADDR)] == ["aa" * 32]
        tip = 100
        assert coinlist.covers(coinlist.coverage(db, ADDR, tip), 0, tip), "home vouched for 0..; here watched from 50"


def test_a_holder_on_older_code_still_gives_its_coins(tmp_path, no_nodes, monkeypatch):
    here, _ = _arcade(tmp_path, "here")
    _holds(here, 50)

    class Old:
        status_code = 200

        def json(self):
            return {"tag": "wanderer", "chains": {here.messaging.network: {
                "address": ADDR, "since": 10, "coins": [{"txid": "aa" * 32, "vout": 0, "value": 5, "height": 20}],
                "held": []}}}
    monkeypatch.setattr(appmod.requests, "get", lambda url, timeout=0, headers=None, **kw: Old())
    monkeypatch.setattr(appmod, "_host_is_public", lambda host: True)
    monkeypatch.setattr(type(here.messaging), "rpc", lambda self: _Rpc())
    assert here.reconcile_coins("wanderer", ["old.example.com"])["filed"] == 1
    with contextlib.closing(here.token_index(here.messaging).open()) as db:
        assert coinlist.coverage(db, ADDR, 100) == [(50, 100)], "an older arcade vouches for nothing"


def _sign_in(client):
    from nacl.signing import SigningKey

    from arcade import accounts
    key = SigningKey.generate()
    ch = client.get("/auth/challenge").json()
    sig = key.sign(accounts.login_message(ch["origin"], ch["nonce"])).signature
    r = client.post("/auth/login", json={"pubkey": key.verify_key.encode().hex(), "nonce": ch["nonce"],
                                         "signature": sig.hex(), "join": True})
    assert r.status_code == 200, r.text
    return key.verify_key.encode().hex()


def test_the_browsers_copy_is_listed_and_merged_checked(tmp_path, no_nodes, monkeypatch):
    """the operator: "the users valid UTXO list should also be stored in their arcade, browser local storage". The browser
    keeps the node's list; at sign-in it hands its copy back, and only coins this chain says are real and the account's
    own are filed."""
    here, client = _arcade(tmp_path, "here")
    pub = _sign_in(client)
    here.set_setting(f"address:{pub}", ADDR)
    with contextlib.closing(here.token_index(here.messaging).open()) as db:
        utxos.watch(db, ADDR, 50, why="test")
        db.conn.commit()
    assert client.get("/account/coins/list").json()["lists"][here.messaging.network][ADDR]["coins"] == []
    monkeypatch.setattr(type(here.messaging), "rpc", lambda self: _Rpc())
    copy = {here.messaging.network: {
        ADDR: {"coins": [["aa" * 32, 0, 5, 20, ""], ["bb" * 32, 0, 5, 20, ""], ["cc" * 32, 0, 9, 20, ""]],
               "covered": [[0, 49]]},
        "nSomebodyElsesAddressXXXXXXXXXXXXXX": {"coins": [["dd" * 32, 0, 5, 20, ""]], "covered": [[0, 100]]}}}
    said = client.post("/account/coins/merge", json={"lists": copy}).json()
    assert said == {"filed": 1, "covered": 1}
    listed = client.get("/account/coins/list").json()["lists"][here.messaging.network]
    assert [c[0] for c in listed[ADDR]["coins"]] == ["aa" * 32] and "nSomebodyElsesAddressXXXXXXXXXXXXXX" not in listed
    assert client.post("/account/coins/merge", json={"lists": copy}).json().get("skipped"), "once a minute"


def test_admin_wallet_imports_tab(tmp_path, no_nodes, monkeypatch):
    """2026-10-25: "add another admin panel specifically just for Wallet imports"."""
    here, client = _arcade(tmp_path, "here")
    LOCAL, WRITE = {"host": "127.0.0.1:8420"}, {"x-arcade-admin": "1"}
    net = here.messaging.network
    here.set_setting(f"extra_address:{net}:{PUB}", [{"a": ADDR, "k": "p2pkh", "p": "", "t": 1}])
    here.vault().put("wanderer", PUB, "nMainAddressXXXXXXXXXXXXXXXXXXXXXXX", json.dumps(BLOB))
    with contextlib.closing(here.token_index(here.messaging).open()) as db:
        utxos.watch(db, ADDR, 0, why="test")
        db.conn.execute("INSERT INTO utxo VALUES (?,?,?,?,?)", ("aa" * 32, 0, ADDR, 5 * 10**8, 20))
        db.conn.commit()
    said = client.get(f"/admin/api/imports?chain={net}", headers=LOCAL).json()
    assert said["chain"] == net and said["stats"]["wallets"] == 1 and said["stats"]["accounts"] == 1
    assert said["wallets"][0]["tag"] == "wanderer" and said["walk"] is None and "user" in said["files"]
    look = client.get(f"/admin/api/imports/lookup?chain={net}&q={ADDR}", headers=LOCAL).json()
    assert look["owner"]["tag"] == "wanderer" and look["coins"] == 1 and look["sats"] == 5 * 10**8
    assert client.post("/admin/api/imports", headers={**LOCAL, **WRITE},
                       json={"do": "set", "lend": False, "readers_max": 3}).json() == {"saved": True}
    st = client.get(f"/admin/api/imports?chain={net}", headers=LOCAL).json()["settings"]
    assert st == {"lend": False, "readers_max": 3}, "no switch back to a Core rescan (2026-11-03)"
    assert client.get(f"/coins/walk/can?chain={net}").json()["files"] is False, "not lent when lending is off"
    assert client.get("/admin/api/imports", headers={"host": "node.example.com", "cf-ray": "x"}).status_code in (401, 403, 404)

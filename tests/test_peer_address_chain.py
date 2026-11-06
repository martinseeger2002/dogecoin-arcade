"""The bare-address door reads the chain the ADDRESS is on, not the messaging chain (2026-11-06).

Node 1 runs messaging on Pepecoin testnet; a Dogecoin "D" address asked there came back as height 1,560,011 with
0 coins, although its Dogecoin index held 7,139 coins for that address."""
import contextlib
import time
from pathlib import Path

import nacl.signing
import pytest
from fastapi.testclient import TestClient

from arcade import peer_address as pa
from arcade.web.app import create_app
from arcade.web.state import AppState, ChainContext

ADDR = "DCHxodkzaKCLjmnG4LP8uH6NKynmntmCNz"
TX = "ab" * 32


@pytest.fixture(autouse=True)
def _fresh():
    pa._served.clear(); pa._asked.clear()
    yield
    pa._served.clear(); pa._asked.clear()


def _ctx(network):
    return ChainContext(network=network, role="ledger", label=network, datadir=Path("/nonexistent"))


def test_chain_for_picks_by_version_byte():
    test, doge, main = _ctx("test"), _ctx("doge"), _ctx("main")
    assert pa.address_version(ADDR) == 30
    assert pa.chain_for(ADDR, [test, main, doge], fallback=test) is doge
    assert pa.chain_for(ADDR, [test, main, doge], fallback=test, wanted="doge") is doge
    assert pa.chain_for(ADDR, [test, doge], fallback=test, wanted="test") is None      # never the wrong chain
    assert pa.chain_for("not-an-address", [test, doge], fallback=test) is test        # undecodable: messaging
    assert pa.chain_for(ADDR, [test], fallback=test) is test                          # no Dogecoin here at all


class _Rpc:
    def __init__(self, info): self.info = info
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def get_blockchain_info(self): return self.info


def _node(tmp_path, monkeypatch, doge_info):
    st = AppState(home=tmp_path, messaging=_ctx("test"), ledger=_ctx("test"), doge=_ctx("doge"))
    st.messaging = ChainContext(network="test", role="messaging", label="Pepecoin testnet",
                                datadir=Path("/nonexistent"))
    monkeypatch.setattr(ChainContext, "rpc", lambda self: _Rpc(
        doge_info if self.network == "doge" else {"blocks": 1560011, "headers": 1560011}))
    client = TestClient(create_app(st))
    key = nacl.signing.SigningKey.generate()
    with st.store() as store:
        store.add_mesh_announcement("t1", "test", "nAsker", "asker.example", 443,
                                    key.verify_key.encode().hex(), 0, int(time.time()))
    return st, client, key


def test_d_address_reads_dogecoin_on_a_pepecoin_messaging_node(tmp_path, no_nodes, monkeypatch):
    from arcade import utxos
    st, client, key = _node(tmp_path, monkeypatch, {"blocks": 5_100_000, "headers": 5_100_000})
    with contextlib.closing(st.token_index(st.doge).open()) as db:
        utxos.watch(db, ADDR, 10, why="test")
        db.conn.execute("INSERT INTO utxo (txid, vout, address, value, height) VALUES (?,?,?,?,?)",
                        (TX, 0, ADDR, 100_000, 20))
    r = client.get(f"/peer/address/{ADDR}/coins", headers=pa.sign_ask(key, ADDR))
    assert r.status_code == 200, r.text
    said = r.json()
    assert said["chain"] == "doge" and said["height"] == 5_100_000
    assert said["balance_koinu"] == 100_000 and len(said["coins"]) == 1


def test_unsynced_address_chain_is_refused(tmp_path, no_nodes, monkeypatch):
    _st, client, key = _node(tmp_path, monkeypatch, {"blocks": 5_000_000, "headers": 6_000_000})
    r = client.get(f"/peer/address/{ADDR}/coins", headers=pa.sign_ask(key, ADDR))
    assert r.status_code == 503 and r.json()["synced"] is False


def test_unsigned_ask_still_refused(tmp_path, no_nodes, monkeypatch):
    _st, client, _key = _node(tmp_path, monkeypatch, {"blocks": 5_100_000, "headers": 5_100_000})
    assert client.get(f"/peer/address/{ADDR}/coins").status_code == 403
    stranger = nacl.signing.SigningKey.generate()
    assert client.get(f"/peer/address/{ADDR}/coins", headers=pa.sign_ask(stranger, ADDR)).status_code == 403


def test_asker_names_the_chain():
    k = nacl.signing.SigningKey.generate()
    urls = []

    class R:
        status_code = 200
        def json(self): return {"address": ADDR, "synced": True, "coins": []}
    pa.ask_peers(ADDR, ["n1.example"], lambda url, **kw: urls.append(url) or R(), k, chain="doge")
    assert urls == [f"https://n1.example/peer/address/{ADDR}/coins?chain=doge"]


def test_wallet_assets_finds_doge_on_the_running_state_even_unsynced(tmp_path, monkeypatch):
    """`arcade.web.state` is the module; the AppState app.py hands over is where the Dogecoin chain is."""
    from arcade.web.dogefeedx import wallet_assets as wa
    st = AppState(home=tmp_path, messaging=_ctx("test"), ledger=_ctx("test"), doge=_ctx("doge"))
    monkeypatch.setattr(wa, "STATE", st)
    monkeypatch.setattr(st, "chain_ready", lambda chain: False, raising=False)
    assert wa._doge_chain() is st.doge

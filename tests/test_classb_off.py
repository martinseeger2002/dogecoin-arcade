"""No arcade transactions on Litecoin, Litecoin testnet and Kotia (2026-11-05, tbuuol: "Die 3 Chains LTC tLTC und KOT
sollten keinerlei Token Protokolle benutzen" -- "alles abschalten"): Params.class_b.

Their index follows coins only and the token pages do not switch to them. Nothing is checked on a send there.
"""

from __future__ import annotations

import contextlib
import types

import pytest

from arcade import indexer
from arcade.config import NETWORKS
from arcade.encoding import encode_class_c
from arcade.ledger import LedgerIndex
from arcade.rpc import RpcClient
from arcade.txbuild import build_raw_tx, multisig_script, op_return_script, p2pkh_script

OFF = ("ltc", "ltctest", "kot")
KEY = bytes.fromhex("02" + "11" * 32)


def _raw(net, outputs):
    params = NETWORKS[net]
    return build_raw_tx([("ab" * 32, 0)], outputs, ntime=1_800_000_000 if params.tx_time else None)


def _pay(net):
    return (50_000, p2pkh_script(NETWORKS[net].marker))


def test_only_litecoin_litecoin_testnet_and_kotia_carry_no_arcade_transactions():
    assert {n for n, p in NETWORKS.items() if not p.class_b} == set(OFF)
    for net in ("main", "test", "doge", "bel", "lky", "wjk", "dgb", "flop"):
        assert NETWORKS[net].class_b, f"{net} keeps the arcade's tokens (Wojakcoin and DigiByte copy Litecoin's Params)"


@pytest.mark.parametrize("net", OFF)
def test_a_send_there_is_never_checked(net):
    """tbuuol: "null Prüfung auf irgendwelche Daten": whatever a transaction carries, it goes to the node."""
    asked = []
    client = RpcClient.__new__(RpcClient)
    client._ids = iter(range(1, 99))
    client._post = lambda payload: asked.append(payload) or {"result": "cd" * 32}
    client.fan_net = net
    for outputs in ([(0, op_return_script(encode_class_c(b"\x00" * 8)))], [_pay(net), (1_000, multisig_script([KEY], 1))],
                    [(100_000, p2pkh_script(NETWORKS[net].marker))]):
        assert client.call("sendrawtransaction", _raw(net, outputs))
    assert len(asked) == 3


def test_the_index_follows_their_coins_and_reads_no_arcade_transaction(tmp_path, monkeypatch):
    for n, (net, wanted) in enumerate((("ltctest", 0), ("kot", 0), ("test", 1))):
        params = NETWORKS[net]
        read = []
        monkeypatch.setattr(indexer, "extract", lambda *a, **k: read.append(1))
        monkeypatch.setattr(indexer.utxos, "watching", lambda db: {"someone"})
        monkeypatch.setattr(indexer.utxos, "on_block", lambda *a, **k: {"added": 1, "spent": 0})
        index = LedgerIndex(tmp_path / f"{n}-ledger.sqlite", params, rpc_factory=lambda: None)
        handler = indexer.ArcadeHandler(None, params)
        with contextlib.closing(index.open()) as db:
            handler.on_connect(db, params.activation_height, {"tx": [{"txid": "ef" * 32}]})
        assert len(read) == wanted, net
        assert handler.stats["coins_in"] == 1, f"{net}: its coins are followed"


def test_the_token_pages_never_switch_to_them():
    from arcade.web.state import AppState
    chains = {n: types.SimpleNamespace(network=n, params=NETWORKS[n]) for n in ("test", "ltctest", "kot", "doge")}
    me = types.SimpleNamespace(token_chains=list(chains.values()), ledger=chains["test"], _token_chain="ltctest",
                               token_chain_path=None)
    assert [c.network for c in AppState.arcade_chains.fget(me)] == ["test", "doge"]
    me.arcade_chains = AppState.arcade_chains.fget(me)
    assert AppState.token_chain.fget(me).network == "test", "a stored L test choice falls back"

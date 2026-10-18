"""The node's coins on a chain whose Core it borrows, spent with no extra step (2026-10-18): the identity
key, read from the wallet that holds it, signs a send of the coins at its address on that chain -- here a real
regtest litecoind takes the transaction."""
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from arcade import keywallet as K  # noqa: E402
from arcade.script import b58check_decode, b58check_encode, hash160  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("litecoind") is None, reason="litecoind is not installed here")


def test_the_identity_key_spends_its_coins_on_a_borrowed_chain(monkeypatch):
    from arcade.node_trader import HeldKey
    from arcade.regtest import LitecoinRegtestNode
    with LitecoinRegtestNode() as node:
        rpc = node.rpc
        node.generate(110)
        held = rpc.call("getnewaddress")                     # a legacy address whose key the wallet keeps
        key = HeldKey(f"{K._wif_secret(rpc.call('dumpprivkey', held)):064x}")
        assert hash160(key.pubkey) == b58check_decode(held)[1], "dumpprivkey's WIF read back to the same key"
        mine = b58check_encode(node.params.pubkeyhash_version, hash160(key.pubkey))
        paid = [rpc.call("sendtoaddress", mine, 2), rpc.call("sendtoaddress", mine, 3)]
        node.generate(1)
        found = []
        for txid in paid:
            tx = rpc.call("getrawtransaction", txid, 1)
            found += [{"txid": txid, "vout": o["n"], "value": round(o["value"] * 1e8)} for o in tx["vout"]
                      if mine in o["scriptPubKey"].get("addresses", [])]
        chain = SimpleNamespace(params=node.params, rpc=lambda: rpc, network="regtest")
        state = SimpleNamespace(key_address=lambda c: mine)
        monkeypatch.setattr(K, "coins", lambda st, c: found)  # the index's reading, for this test
        assert K.balance(state, chain)["spendable"] == "5.00000000"
        to = rpc.call("getnewaddress")
        prepared = K.prepare(state, chain, to, 4 * 10 ** 8, key=key)
        assert rpc.call("sendrawtransaction", prepared.raw) == prepared.txid, "the node takes it"
        node.generate(1)
        out = rpc.call("getrawtransaction", prepared.txid, 1)
        assert out["confirmations"] == 1
        assert {round(o["value"] * 1e8) for o in out["vout"]} == {4 * 10 ** 8, 10 ** 8 - prepared.fee_sats}
        with pytest.raises(K.KeyWalletError, match="needs"):
            K.prepare(state, chain, to, 6 * 10 ** 8, key=key)

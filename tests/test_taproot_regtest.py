"""A Taproot address's coins, received and spent on a real litecoind (2026-11-07, SadFrog: Taproot on Litecoin and
DigiByte). The arcade builds the spend from its own index (funding.build: BIP-341's digest, change back to the same
address, priced at its virtual size), it is signed here as the browser signs it (BIP-340 by the tweaked key, BIP-86's
key path), assembled by funding.assemble, and Litecoin Core 0.21 takes it into a block. Skipped without litecoind."""
import pathlib
import shutil
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_extension import _pubkey                               # noqa: E402
from test_taproot import schnorr, tweaked_secret                 # noqa: E402

from arcade import fees, funding, taproot                        # noqa: E402
from arcade.db import Database                                   # noqa: E402
from arcade.script import b58check_decode, segwit_encode         # noqa: E402
from arcade.state import install_schema                          # noqa: E402
from arcade.txbuild import p2pkh_script                          # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("litecoind") is None, reason="litecoind is not installed here")
COIN = 100_000_000
RATE = fees.MIN_FEE_PER_KB           # the chain's own minimum, as every coin send asks (fees.rate_for)


def _coin(node, txid: str, script: bytes) -> dict:
    tx = node.rpc.call("getrawtransaction", txid, 1)
    out = next(o for o in tx["vout"] if o["scriptPubKey"]["hex"] == script.hex())
    return {"txid": txid, "vout": out["n"], "value": round(out["value"] * COIN)}


def test_a_taproot_address_receives_and_spends_on_a_real_litecoind(tmp_path):
    from arcade.regtest import LitecoinRegtestNode
    with LitecoinRegtestNode() as node:
        node.generate(200)
        params = node.params
        secret = 0x7A9 << 200
        pub = _pubkey(secret.to_bytes(32, "big"))
        spk = taproot.key_path_script(pub)
        address = segwit_encode(params.bech32_hrp, 1, spk[2:])
        said = node.rpc.call("validateaddress", address)
        assert said["isvalid"] and said["iswitness"] and said["witness_version"] == 1, said
        assert said["scriptPubKey"] == spk.hex()

        # Received: two coins from the node's own wallet, read into an index as the arcade's indexer files them.
        coins = [_coin(node, node.rpc.call("sendtoaddress", address, amount), spk) for amount in (2, 1.5)]
        node.generate(1)
        db = Database(tmp_path / "index.sqlite")
        install_schema(db)
        for c in coins:
            db.conn.execute("INSERT INTO utxo (txid, vout, address, value, height) VALUES (?,?,?,?,?)",
                            (c["txid"], c["vout"], address, c["value"], 201))
        payee = node.rpc.call("getnewaddress")
        assert b58check_decode(payee)[0] == params.pubkeyhash_version
        unsigned = funding.build(db, params, address, [(3 * COIN, p2pkh_script(payee))], rate=RATE)
        assert len(unsigned.inputs) == 2 and all(c["taproot"] == {"key": True} for c in unsigned.inputs)

        # A signature by the untweaked key is not the key path's: refused by the node.
        wrong = funding.assemble(unsigned, [schnorr(secret, bytes.fromhex(h)).hex() for h in unsigned.sighashes], pub)
        refused = node.rpc.call("testmempoolaccept", [wrong])[0]
        assert not refused["allowed"] and "schnorr" in refused["reject-reason"].lower(), refused

        signed = funding.assemble(unsigned, [schnorr(tweaked_secret(secret), bytes.fromhex(h)).hex()
                                             for h in unsigned.sighashes], pub)
        took = node.rpc.call("testmempoolaccept", [signed])[0]
        assert took["allowed"], took
        decoded = node.rpc.call("decoderawtransaction", signed)
        rate = fees.rate_for(params, RATE)
        assert unsigned.fee == fees.fee_for(decoded["vsize"], 0, rate), "priced at the size the node counts"
        txid = node.rpc.call("sendrawtransaction", signed)
        node.generate(1)
        mined = node.rpc.call("getrawtransaction", txid, 1)
        assert mined["confirmations"] == 1
        outs = {o["scriptPubKey"]["hex"]: round(o["value"] * COIN) for o in mined["vout"]}
        assert outs[spk.hex()] == unsigned.change > 0, "the change came back to the same Taproot address"
        assert outs[p2pkh_script(payee).hex()] == 3 * COIN

        # Its change spends again, this time paying another Taproot address (OP_1 <key>).
        change = _coin(node, txid, spk)
        db.conn.execute("DELETE FROM utxo")
        db.conn.execute("INSERT INTO utxo (txid, vout, address, value, height) VALUES (?,?,?,?,?)",
                        (change["txid"], change["vout"], address, change["value"], 202))
        other = taproot.key_path_script(_pubkey((0xB0B).to_bytes(32, "big")))
        again = funding.build(db, params, address, [(change["value"] // 2, other)], rate=RATE)
        raw = funding.assemble(again, [schnorr(tweaked_secret(secret), bytes.fromhex(h)).hex()
                                       for h in again.sighashes], pub)
        second = node.rpc.call("sendrawtransaction", raw)
        node.generate(1)
        assert node.rpc.call("getrawtransaction", second, 1)["confirmations"] == 1
        db.close()

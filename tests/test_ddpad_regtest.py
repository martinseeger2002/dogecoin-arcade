"""The DigiDollar-test launchpad's own transactions against a real DigiByte Core 9.26.7 on regtest (arcade/ddpad.py,
web/ddpadweb.py ArcadeEnv): the pad inscribes a piece to its own address, signing with its own key, and the Core takes
the commit and the reveal; then it sends the piece on, the inscribed coin whole as output 0, and the Core takes that.
The index is a small one filled from the Core's own coin set, and the ord index a guard that knows the pad's piece.
Skipped where `digibyted` is not installed. The payment's side (a DigiDollar send read back as dollars to the pad) is
plain arithmetic, tested without a Core in test_ddpad.py."""
import contextlib
import dataclasses
import shutil
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from arcade import ddpad, funding, ord_inscribe, ord_rules, utxos  # noqa: E402
from arcade.config import NETWORKS                                 # noqa: E402
from arcade.script import b58check_encode, hash160                 # noqa: E402
from arcade.web.ddpadweb import ArcadeEnv                          # noqa: E402

DGB_REGTEST = dataclasses.replace(NETWORKS["dgb"], pubkeyhash_version=126, bech32_hrp="dgbrt", real_coins=False)
#: A Byteling-sized picture: 32 by 32, a few hundred bytes (what examples/bytelings/generate.py writes).
PICTURE = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000020000000200203000000") + b"\x00" * 360


class CoinSet:
    """An index of one address, read from the Core's coin set whenever it is opened: what utxos.unspent answers."""

    def __init__(self, rpc, address: str, maturity: int = 100):
        self.rpc, self.address, self.maturity = rpc, address, maturity

    def indexed_height(self):
        return self.rpc.call("getblockcount")

    def open(self):
        db = SimpleNamespace(conn=sqlite3.connect(":memory:", isolation_level=None))
        db.conn.row_factory = sqlite3.Row
        utxos.install(db)
        tip = self.rpc.call("getblockcount")
        for u in self.rpc.call("scantxoutset", "start", [f"addr({self.address})"])["unspents"]:
            if u.get("coinbase") and tip - int(u["height"]) < self.maturity:
                continue
            db.conn.execute("INSERT INTO utxo (txid, vout, address, value, height) VALUES (?,?,?,?,?)",
                            (u["txid"], u["vout"], self.address, round(u["amount"] * 1e8), u["height"]))
        db.close = db.conn.close
        return db


class Ords:
    """The ord index's answer for the pad's pieces: which coin carries which inscription."""

    def __init__(self):
        self.on: dict[str, dict[int, list]] = {}

    def held(self, txid):
        return self.on.get(txid, {})

    def spendable(self, coins):
        return [c for c in coins if not self.on.get(c["txid"], {}).get(int(c["vout"]))]


pytestmark = pytest.mark.skipif(shutil.which("digibyted") is None, reason="digibyted is not installed here")


@pytest.fixture(scope="module")
def dgb():
    from arcade.regtest import RegtestNode
    with RegtestNode(binary="digibyted", cli="digibyte-cli", extra_args=("-digidollar=1",)) as node:
        yield node


def test_the_pad_inscribes_with_its_own_key_and_sends_the_piece_on(dgb, tmp_path):
    rpc = dgb.rpc
    key = ddpad.PadKey(tmp_path)
    pad_address = b58check_encode(DGB_REGTEST.pubkeyhash_version, hash160(key.pubkey))
    rpc.call("generatetoaddress", 110, pad_address)
    chain = SimpleNamespace(network="dgb", params=DGB_REGTEST, rpc=lambda: rpc)
    pay = SimpleNamespace(network="dgbtest", rpc=lambda: rpc)
    index = CoinSet(rpc, pad_address)
    state = SimpleNamespace(token_index=lambda ch: index)
    pad = ddpad.Pad(tmp_path)
    env = ArcadeEnv(state, pad, key, chain, pay)
    assert env.address == pad_address and env.pay_address.startswith("TD")

    got = env.inscribe("image/png", PICTURE)
    reveal_txid = got["iid"][:-2]
    assert set(rpc.call("getrawmempool")) == {got["commit"], reveal_txid}, "the Core took the commit and the reveal"
    assert not env.ready(), "the next broadcast waits for the index"
    rpc.call("generatetoaddress", 1, b58check_encode(126, hash160(b"\x02" + b"\x01" * 32)))
    assert env.ready()

    reveal = rpc.call("getrawtransaction", reveal_txid, True)
    assert reveal["confirmations"] == 1
    script = bytes.fromhex(reveal["vin"][0]["scriptSig"]["hex"])
    found, ctype, body, _ = ord_rules.read([script], ord_inscribe.rules_of("dgb").dialect)
    assert (found, ctype, body) == ("complete", b"image/png", PICTURE), "the picture reads back from the chain"
    assert reveal["vout"][0]["scriptPubKey"]["hex"] == funding.p2pkh_script(pad_address).hex()
    assert round(reveal["vout"][0]["value"] * 1e8) == ord_inscribe.POSTAGE
    assert got["outpoint"] == f"{reveal_txid}:0"

    # the piece goes to its buyer: the ord index says what the coin carries
    ords = Ords()
    ords.on[reveal_txid] = {0: [{"id": got["iid"], "offset": 0}]}
    guard = (lambda a: a == pad_address, ords)
    funding.GUARDS.append(guard)
    try:
        with contextlib.closing(index.open()) as db:
            assert (reveal_txid, 0) in {(u["txid"], u["vout"]) for u in utxos.unspent(db, pad_address)}
        buyer = b58check_encode(126, hash160(b"\x03" + b"\x07" * 32))
        sent = env.deliver(got["iid"], got["outpoint"], got["value"], buyer)
    finally:
        funding.GUARDS.remove(guard)
    tx = rpc.call("getrawtransaction", sent, True)
    assert (tx["vin"][0]["txid"], tx["vin"][0]["vout"]) == (reveal_txid, 0), "the inscribed coin is input 0"
    assert tx["vout"][0]["scriptPubKey"]["hex"] == funding.p2pkh_script(buyer).hex()
    assert round(tx["vout"][0]["value"] * 1e8) == ord_inscribe.POSTAGE, "whole, to the buyer, as output 0"
    rpc.call("generatetoaddress", 1, buyer)
    assert env.settled(sent)


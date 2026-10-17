"""Litecoin on a real chain (SadFrog's side, 2026-10-17; LITECOIN-PLAN phase 2).

Every test here runs against a throwaway regtest litecoind 0.21 started with -acceptnonstdtxn=0,
so what passes here also relays on Litecoin mainnet:

* the arcade's own (Omni-style, Class B) inscription: built, broadcast, mined, indexed, read back;
* a Doginals-format inscription on Litecoin: written (arcade/ltc_ord.py), read back and traced
  with the unchanged arcade/doginals.py, and moved to a new owner;
* exchanges between two wallets: Pepecoin for Litecoin, both ways and the refund path, and a
  Litecoin inscription for Pepecoin -- the inscription arrives at the buyer's coin, first litoshi.
"""
import dataclasses
import shutil
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_funding import N, _pubkey, _sign                         # noqa: E402
from test_htlc import Payer, _lock                                 # noqa: E402
from test_inscription_e2e import _put, _sync                       # noqa: E402

from arcade import doginals, escrow as E, htlc as H, inscribe, ltc_ord  # noqa: E402
from arcade.chain import ChainFollower                             # noqa: E402
from arcade.db import Database                                     # noqa: E402
from arcade.indexer import ArcadeHandler                           # noqa: E402
from arcade.ledger import LedgerIndex                              # noqa: E402
from arcade.script import b58check_encode, hash160                 # noqa: E402
from arcade.state import install_schema                            # noqa: E402
from arcade.txbuild import p2pkh_script, push                      # noqa: E402

COIN = 100_000_000
LTC_P2SH_REGTEST = 58

pytestmark = pytest.mark.skipif(shutil.which("litecoind") is None,
                                reason="litecoind is not installed here")


class Key:
    """A key nobody's node holds, signing as a browser would (legacy P2PKH, version 111)."""

    def __init__(self, version: int = 111):
        import secrets
        self.secret = secrets.randbelow(N - 1) + 1
        self.pub = self.pubkey = _pubkey(self.secret)
        self.address = b58check_encode(version, hash160(self.pub))

    def sign(self, digest: bytes) -> bytes:
        return _sign(self.secret, digest)


@pytest.fixture(scope="module")
def ltc():
    from arcade.regtest import LitecoinRegtestNode
    with LitecoinRegtestNode() as node:          # -acceptnonstdtxn=0: mainnet's relay rules
        node.generate(200)
        alice = node.rpc.call("getnewaddress")
        node.rpc.call("sendtoaddress", alice, 1000)
        node.generate(1)
        yield node, alice


def _vout_to(rpc, txid: str, address: str) -> int:
    tx = rpc.call("getrawtransaction", txid, 1)
    return next(o["n"] for o in tx["vout"] if address in o["scriptPubKey"].get("addresses", []))


# --- the arcade's own inscription (Class B) ---------------------------------------------------

def test_an_arcade_inscription_goes_on_litecoin_and_comes_back(ltc, tmp_path):
    node, alice = ltc
    assert node.params.scripthash_version == LTC_P2SH_REGTEST
    params = dataclasses.replace(node.params, activation_height=node.rpc.get_block_count() + 1,
                                 marker_address=node.rpc.call("getnewaddress"))
    db = Database(tmp_path / "ltc-inscriptions.sqlite")
    install_schema(db)
    follower = ChainFollower(node.rpc, db, params, handler=ArcadeHandler(node.rpc, params))
    try:
        content = bytes(range(256)) * 40                     # 10,240 bytes: two Class B pieces
        plan = inscribe.plan(content, "image/png", '{"name": "Silver Lantern"}')
        txids = _put(node, params, alice, plan.payloads)
        assert len(txids) == plan.chunks >= 2
        _sync(node, follower)
        index = LedgerIndex(tmp_path / "ltc-inscriptions.sqlite", params,
                            rpc_factory=lambda: node.rpc)
        rows = index.inscriptions()
        assert len(rows) == 1 and rows[0]["owner"] == rows[0]["creator"] == alice
        assert index.inscription_content(rows[0]["txid"]) == ("image/png", content), \
            "byte for byte, off the Litecoin chain"
    finally:
        db.close()


# --- a Doginals-format inscription on Litecoin --------------------------------------------------

def _inscribe(node, owner_script: bytes, content_type: str, content: bytes) -> str:
    """Commit to the envelope's P2SH, reveal it onto `owner_script`; returns the reveal txid."""
    key = Key()
    pushes, redeem = ltc_ord.plan(content_type, content, key.pub)
    commit_to = ltc_ord.commit_address(redeem, LTC_P2SH_REGTEST)
    assert commit_to.startswith("Q"), "a Litecoin regtest P2SH address"
    txid = node.rpc.call("sendtoaddress", commit_to, 0.1)
    commit = {"txid": txid, "vout": _vout_to(node.rpc, txid, commit_to), "value": 10_000_000}
    node.generate(1)
    raw = ltc_ord.reveal(commit, pushes, redeem, owner_script, postage=9_900_000, sign=key.sign)
    reveal = node.rpc.call("sendrawtransaction", raw)
    node.generate(1)
    _keep(node, reveal)
    return reveal


def _keep(node, txid: str, vout: int = 0) -> None:
    """Never let the node's own ordinary sends spend an inscription's coin. Without this the wallet
    funded a later commit with an earlier test's inscription and carried it along (2026-10-17) --
    exactly what the arcade's inscription guard prevents on Pepecoin and Dogecoin, and what a
    Litecoin guard must do before Litecoin is switched on."""
    try:
        node.rpc.call("lockunspent", False, [{"txid": txid, "vout": vout}])
    except Exception:                                       # noqa: BLE001 -- not the wallet's coin
        pass


def test_a_doginal_format_inscription_on_litecoin_is_read_by_the_arcade(ltc):
    node, _ = ltc
    bob = node.rpc.call("getnewaddress")
    for content in (b"gm from LitecoinArcade", bytes(range(256)) * 4):   # one part, then two
        reveal = _inscribe(node, p2pkh_script(bob), "text/plain", content)
        ins = f"{reveal}i0"
        assert doginals.content(node.rpc, ins) == ("text/plain", content), "the unchanged reader"
        assert doginals.trace(node.rpc, reveal, 0) == [{"id": ins, "offset": 0}], \
            "born on the reveal's first output, first litoshi"


def test_a_litecoin_inscription_moves_to_a_new_owner(ltc):
    node, _ = ltc
    bob, carol = node.rpc.call("getnewaddress"), node.rpc.call("getnewaddress")
    reveal = _inscribe(node, p2pkh_script(bob), "text/plain", b"pass it on")
    node.rpc.call("lockunspent", True, [{"txid": reveal, "vout": 0}])
    raw = node.rpc.call("createrawtransaction", [{"txid": reveal, "vout": 0}], [{carol: 0.098}])
    signed = node.rpc.call("signrawtransactionwithwallet", raw)["hex"]
    moved = node.rpc.call("sendrawtransaction", signed)
    node.generate(1)
    _keep(node, moved)
    assert doginals.trace(node.rpc, moved, 0) == [{"id": f"{reveal}i0", "offset": 0}]


def test_too_much_content_for_one_transaction_is_refused():
    with pytest.raises(ltc_ord.InscribeError):
        ltc_ord.plan("text/plain", b"x" * 2000, Key().pub)
    pushes = ltc_ord.envelope_pushes("text/plain", b"\x07")
    assert pushes[-1] == b"\x57", "a one-byte part 1..16 is its OP_n (minimal, standard)"


# --- exchanges between two wallets -------------------------------------------------------------

def _swap_setup(pep_rpc, ltc_node, alice_lock_s: int, bob_lock_s: int):
    """test_htlc's setup, with Litecoin mined by generatetoaddress (`generate` is gone there).
    Alice has PEP and wants LTC; Bob has LTC and wants PEP. Alice makes the secret."""
    ltc_rpc = ltc_node.rpc
    alice_pep, alice_ltc = Payer(pep_rpc), Key()
    bob_ltc, bob_pep = Payer(ltc_rpc), Key()
    secret, h = H.make_secret()
    now = int(time.time())
    a_redeem = H.redeem_script(h, bob_pep.pub, alice_pep.pubkey, now + alice_lock_s)
    a_coin = _lock(pep_rpc, alice_pep, a_redeem, 3 * COIN)
    pep_rpc.call("generate", 1)
    b_redeem = H.redeem_script(h, alice_ltc.pub, bob_ltc.pubkey, now + bob_lock_s)
    b_coin = _lock(ltc_rpc, bob_ltc, b_redeem, 2 * COIN)
    ltc_node.generate(1)
    return dict(alice_pep=alice_pep, alice_doge=alice_ltc, bob_doge=bob_ltc, bob_pep=bob_pep,
                secret=secret, a_redeem=a_redeem, a_coin=a_coin, b_redeem=b_redeem, b_coin=b_coin)


def test_a_pep_ltc_swap_completes_for_both_sides(regtest, ltc):
    """Alice pays PEP and wants LTC; Bob pays LTC and wants PEP. Lock scripts on both chains."""
    pep_rpc, ltc_rpc = regtest.rpc, ltc[0].rpc
    if float(pep_rpc.call("getbalance")) < 10:
        pep_rpc.call("generate", 101)
    s = _swap_setup(pep_rpc, ltc[0], 7200, 3600)
    inputs, outputs = H.pay_out(s["b_coin"], s["alice_doge"].address)        # Alice takes the LTC
    claim = ltc_rpc.call("sendrawtransaction", H.claimed(inputs, outputs, s["b_redeem"],
                                                         s["secret"], s["alice_doge"].sign))
    ltc[0].generate(1)
    seen = ltc_rpc.call("getrawtransaction", claim, 1)                     # Bob reads the secret
    learned = H.secret_from_script_sig(bytes.fromhex(seen["vin"][0]["scriptSig"]["hex"]),
                                       s["b_redeem"])
    assert learned == s["secret"], "the secret is read off the Litecoin chain"
    inputs, outputs = H.pay_out(s["a_coin"], s["bob_pep"].address)
    pep_rpc.call("sendrawtransaction",
                 H.claimed(inputs, outputs, s["a_redeem"], learned, s["bob_pep"].sign))
    pep_rpc.call("generate", 1)
    assert pep_rpc.call("gettxout", s["a_coin"]["txid"], 0) is None
    assert ltc_rpc.call("gettxout", s["b_coin"]["txid"], 0) is None


def test_a_pep_ltc_swap_nobody_finishes_refunds_both_sides(regtest, ltc):
    pep_rpc, ltc_rpc = regtest.rpc, ltc[0].rpc
    if float(pep_rpc.call("getbalance")) < 10:
        pep_rpc.call("generate", 101)
    s = _swap_setup(pep_rpc, ltc[0], -3600, -7200)
    inputs, outputs = H.pay_out(s["a_coin"], s["alice_pep"].address)
    pep_rpc.call("sendrawtransaction", H.refunded(inputs, outputs, s["a_redeem"],
                                                  lambda d: _sign(s["alice_pep"].secret, d)))
    inputs, outputs = H.pay_out(s["b_coin"], s["bob_doge"].address)
    ltc_rpc.call("sendrawtransaction", H.refunded(inputs, outputs, s["b_redeem"],
                                                  lambda d: _sign(s["bob_doge"].secret, d)))
    pep_rpc.call("generate", 1)
    ltc[0].generate(1)
    assert pep_rpc.call("gettxout", s["a_coin"]["txid"], 0) is None
    assert ltc_rpc.call("gettxout", s["b_coin"]["txid"], 0) is None


def test_a_litecoin_inscription_is_traded_for_pep(regtest, ltc):
    """Alice sells a Litecoin inscription for PEP: she locks the inscription's coin (first input,
    so the inscription sits on the lock's first litoshi), Bob locks PEP, Alice claims the PEP and
    reveals the secret, Bob claims the coin -- and the inscription arrives on his coin, offset 0."""
    node, _ = ltc
    pep_rpc, ltc_rpc = regtest.rpc, node.rpc
    if float(pep_rpc.call("getbalance")) < 10:
        pep_rpc.call("generate", 101)
    alice_ltc, bob_ltc, bob_pep, alice_pep = Key(), Key(), Payer(pep_rpc), Key()
    reveal = _inscribe(node, p2pkh_script(alice_ltc.address), "text/plain", b"for sale")
    ins = f"{reveal}i0"
    fee_coin = Payer(ltc_rpc, coins=1.0)                     # Alice's coins for the lock's fee
    node.generate(1)
    secret, h = H.make_secret()
    now = int(time.time())
    a_redeem = H.redeem_script(h, bob_ltc.pub, alice_ltc.pub, now + 7200)   # the inscription lock
    inputs = [{"txid": reveal, "vout": 0}, {"txid": fee_coin.coin["txid"], "vout": fee_coin.coin["vout"]}]
    outputs = [(9_900_000, H.p2sh_script(a_redeem)),
               (fee_coin.coin["value"] - 1_000_000, p2pkh_script(fee_coin.address))]
    sigs = [(alice_ltc.sign(E.sighash(inputs, outputs, 0, p2pkh_script(alice_ltc.address))), alice_ltc.pub),
            (fee_coin.key.sign(E.sighash(inputs, outputs, 1, p2pkh_script(fee_coin.address))), fee_coin.pubkey)]
    raw = E.serialize([{**c, "script_sig": push(sig) + push(pub)} for c, (sig, pub) in zip(inputs, sigs)],
                      outputs)
    locked = ltc_rpc.call("sendrawtransaction", raw.hex())
    node.generate(1)
    assert doginals.trace(ltc_rpc, locked, 0) == [{"id": ins, "offset": 0}], "the lock holds it"
    b_redeem = H.redeem_script(h, alice_pep.pub, bob_pep.pubkey, now + 3600)  # Bob's PEP
    b_coin = _lock(pep_rpc, bob_pep, b_redeem, 3 * COIN)
    pep_rpc.call("generate", 1)
    ins_, outs_ = H.pay_out(b_coin, alice_pep.address)                         # Alice takes the PEP
    claim = pep_rpc.call("sendrawtransaction", H.claimed(ins_, outs_, b_redeem, secret, alice_pep.sign))
    pep_rpc.call("generate", 1)
    learned = H.secret_from_script_sig(
        bytes.fromhex(pep_rpc.call("getrawtransaction", claim, 1)["vin"][0]["scriptSig"]["hex"]), b_redeem)
    ins_, outs_ = H.pay_out({"txid": locked, "vout": 0, "value": 9_900_000}, bob_ltc.address)
    got = ltc_rpc.call("sendrawtransaction", H.claimed(ins_, outs_, a_redeem, learned, bob_ltc.sign))
    node.generate(1)
    assert doginals.trace(ltc_rpc, got, 0) == [{"id": ins, "offset": 0}], \
        "the inscription is Bob's now, on his coin's first litoshi"

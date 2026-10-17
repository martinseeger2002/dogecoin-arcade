"""The lock script every trustless trade settles through (arcade/htlc.py), on real chains.

2026-10-13: trades "need to be 100% trustless with lock scripts". Every claim
this file makes is one only a network can settle: that the claimer with the secret can
spend the lock and nobody else can, that the refunder can only after the lock time,
that a claim puts the secret on the chain for the other side to read, that a token or
an NFT moves out of a lock with the lock as its sender -- and, on two chains at once
(Pepecoin regtest and Dogecoin regtest), that a swap completes for both sides or
refunds for both, never one without the other.
"""

import pathlib
import secrets
import shutil
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_offer import _settled, node                          # noqa: E402,F401
from test_escrow import Owner, _piece                                  # noqa: E402
from test_funding import N, _pubkey, _sign                             # noqa: E402
from test_web import app_state, client                               # noqa: F401,E402

from arcade import escrow as E, htlc as H                            # noqa: E402
from arcade.rpc import RpcError                                       # noqa: E402
from arcade.script import b58check_encode, hash160                    # noqa: E402
from arcade.txbuild import p2pkh_script, push                         # noqa: E402

COIN = 100_000_000


class Key:
    """A key nobody's node holds, signing as a browser would."""

    def __init__(self, version: int = 111):
        self.secret = secrets.randbelow(N - 1) + 1
        self.pub = _pubkey(self.secret)
        self.address = b58check_encode(version, hash160(self.pub))

    def sign(self, digest: bytes) -> bytes:
        return _sign(self.secret, digest)


# --- without a chain ----------------------------------------------------------------------

def test_the_lock_says_what_it_does_and_reads_back():
    secret, h = H.make_secret()
    a, b = Key(), Key()
    for locktime in (500_000_000, 1_800_000_000, 2 ** 31 - 1, 2 ** 31, 2 ** 32 - 1):
        redeem = H.redeem_script(h, a.pub, b.pub, locktime)
        assert H.parse(redeem) == {"secret_hash": h, "claimer": a.pub, "locktime": locktime,
                                   "refunder": b.pub}
    assert H.parse(redeem[:-1]) is None and H.parse(b"\x00" + redeem) is None
    assert H.parse(E.redeem_script(a.pub, b.pub, 1_800_000_000, b"\x00" * 16)) is None, \
        "an escrow is not a lock"
    with pytest.raises(H.HtlcError):
        H.redeem_script(h, a.pub, b.pub, 1_000_000)            # a height, not a time
    with pytest.raises(H.HtlcError):
        H.redeem_script(h, a.pub, a.pub, 1_800_000_000)        # one person on both sides
    with pytest.raises(H.HtlcError):
        H.redeem_script(h[:31], a.pub, b.pub, 1_800_000_000)


def test_a_claim_reveals_the_secret_and_only_the_right_one_reads_back():
    secret, h = H.make_secret()
    a, b = Key(), Key()
    redeem = H.redeem_script(h, a.pub, b.pub, 1_800_000_000)
    sig = a.sign(b"\x11" * 32)
    assert H.secret_from_script_sig(H.claim_script_sig(sig, secret, redeem), redeem) == secret
    assert H.secret_from_script_sig(H.claim_script_sig(sig, b"\x00" * 32, redeem), redeem) is None
    assert H.secret_from_script_sig(H.refund_script_sig(sig, redeem), redeem) is None
    with pytest.raises(H.HtlcError):
        H.claimed([{"txid": "aa" * 32, "vout": 0}], [(1, b"\x51")], redeem, b"\x00" * 32, a.sign)


# --- on regtest ---------------------------------------------------------------------------

def _lock(rpc, payer: Owner, redeem: bytes, amount: int) -> dict:
    """`payer` pays `amount` into the lock; returns the lock's coin."""
    script = H.p2sh_script(redeem)
    inputs = [{"txid": payer.coin["txid"], "vout": payer.coin["vout"]}]
    change = payer.coin["value"] - amount - 1_000_000
    mine = p2pkh_script(payer.address)
    outputs = [(amount, script), (change, mine)]
    sig = _sign(payer.secret, E.sighash(inputs, outputs, 0, mine))
    raw = E.serialize([{**inputs[0], "script_sig": push(sig) + push(payer.pubkey)}], outputs)
    txid = rpc.call("sendrawtransaction", raw.hex())
    payer.coin = {"txid": txid, "vout": 1, "value": change}
    return {"txid": txid, "vout": 0, "value": amount}


def test_the_claimer_takes_it_with_the_secret_and_nobody_else_can(node):
    app, state, rpc = node
    payer, claimer, stranger = Owner(state, rpc), Key(), Key()
    secret, h = H.make_secret()
    redeem = H.redeem_script(h, claimer.pub, payer.pubkey, int(time.time()) + 7200)
    coin = _lock(rpc, payer, redeem, 2 * COIN)
    rpc.call("generate", 1)

    inputs, outputs = H.pay_out(coin, claimer.address)
    with pytest.raises(RpcError):                                        # the wrong key
        rpc.call("sendrawtransaction", H.claimed(inputs, outputs, redeem, secret, stranger.sign))
    early = H.refunded(inputs, H.pay_out(coin, payer.address)[1], redeem,
                       lambda d: _sign(payer.secret, d))
    with pytest.raises(RpcError):                                        # refund before its time
        rpc.call("sendrawtransaction", early)
    txid = rpc.call("sendrawtransaction", H.claimed(inputs, outputs, redeem, secret, claimer.sign))
    rpc.call("generate", 1)
    spent = rpc.call("getrawtransaction", txid, 1)
    paid = [o for o in spent["vout"] if claimer.address in o["scriptPubKey"].get("addresses", [])]
    assert paid and round(paid[0]["value"] * COIN) == 2 * COIN - H.SPEND_FEE
    revealed = H.secret_from_script_sig(bytes.fromhex(spent["vin"][0]["scriptSig"]["hex"]), redeem)
    assert revealed == secret, "the claim put the secret on the chain for the other side"


def test_after_the_lock_time_the_refunder_alone_takes_it_back(node):
    app, state, rpc = node
    payer, claimer = Owner(state, rpc), Key()
    secret, h = H.make_secret()
    redeem = H.redeem_script(h, claimer.pub, payer.pubkey, int(time.time()) - 7200)
    coin = _lock(rpc, payer, redeem, COIN)
    rpc.call("generate", 1)
    inputs, outputs = H.pay_out(coin, payer.address)
    rpc.call("sendrawtransaction", H.refunded(inputs, outputs, redeem,
                                              lambda d: _sign(payer.secret, d)))
    rpc.call("generate", 1)
    assert rpc.call("gettxout", coin["txid"], coin["vout"]) is None, "spent: home again"


def test_an_nft_moves_out_of_a_lock_with_the_lock_as_its_sender(node):
    """An asset in a lock is an asset at the lock's address: the claim is the ledger's
    ordinary transfer, the lock's coin first (the sender), the claimer last (the
    receiver)."""
    app, state, rpc = node
    seller, buyer = Owner(state, rpc), Key()
    _settled(state, rpc)
    piece = "c4" * 32
    _piece(state, piece, seller.address)
    secret, h = H.make_secret()
    redeem = H.redeem_script(h, buyer.pub, seller.pubkey, int(time.time()) + 7200)
    lock = H.address_of(redeem, state.messaging.params)
    coin = seller.send(rpc, E.nft_payload(piece), H.p2sh_script(redeem), 5_000_000)
    _settled(state, rpc)
    index = state.token_index(state.messaging)
    assert index.inscription(piece)["owner"] == lock, "in the lock"
    inputs, outputs = H.move_out(coin, lock, E.nft_payload(piece), buyer.address)
    rpc.call("sendrawtransaction", H.claimed(inputs, outputs, redeem, secret, buyer.sign))
    _settled(state, rpc)
    assert index.inscription(piece)["owner"] == buyer.address, "claimed with the secret"


# --- a swap across two chains -------------------------------------------------------------

@pytest.fixture(scope="module")
def doge():
    """A Dogecoin regtest node beside the session's Pepecoin one."""
    if shutil.which("dogecoind") is None:
        pytest.skip("dogecoind is not installed here")
    from arcade.regtest import RegtestNode
    with RegtestNode(binary="dogecoind", cli="dogecoin-cli") as chain:
        chain.rpc.call("generate", 101)
        yield chain


class Payer:
    """Coins of its own on a chain, from that chain's wallet; signs as a browser would."""

    def __init__(self, rpc, coins: float = 5.0):
        self.key = Key()
        self.secret, self.pubkey, self.address = self.key.secret, self.key.pub, self.key.address
        txid = rpc.call("sendtoaddress", self.address, coins)
        tx = rpc.call("getrawtransaction", txid, 1)
        vout = next(o["n"] for o in tx["vout"] if self.address in o["scriptPubKey"].get("addresses", []))
        self.coin = {"txid": txid, "vout": vout, "value": int(round(coins * COIN))}


def _swap_setup(pep_rpc, doge_rpc, alice_lock_s: int, bob_lock_s: int):
    """Alice has PEP and wants DOGE; Bob has DOGE and wants PEP. Alice makes the secret."""
    alice_pep, alice_doge = Payer(pep_rpc), Key()
    bob_doge, bob_pep = Payer(doge_rpc), Key()
    secret, h = H.make_secret()
    now = int(time.time())
    # Alice locks first, for longer; Bob locks second, for less (he must have time to
    # claim after Alice reveals the secret, before Alice could refund).
    a_redeem = H.redeem_script(h, bob_pep.pub, alice_pep.pubkey, now + alice_lock_s)
    a_coin = _lock(pep_rpc, alice_pep, a_redeem, 3 * COIN)
    pep_rpc.call("generate", 1)
    b_redeem = H.redeem_script(h, alice_doge.pub, bob_doge.pubkey, now + bob_lock_s)
    b_coin = _lock(doge_rpc, bob_doge, b_redeem, 2 * COIN)
    doge_rpc.call("generate", 1)
    return dict(alice_pep=alice_pep, alice_doge=alice_doge, bob_doge=bob_doge, bob_pep=bob_pep,
                secret=secret, a_redeem=a_redeem, a_coin=a_coin, b_redeem=b_redeem, b_coin=b_coin)


def test_a_pep_doge_swap_completes_for_both_sides(regtest, doge):
    pep_rpc, doge_rpc = regtest.rpc, doge.rpc
    if float(pep_rpc.call("getbalance")) < 10:
        pep_rpc.call("generate", 101)
    s = _swap_setup(pep_rpc, doge_rpc, 7200, 3600)
    # Alice claims Bob's DOGE, revealing the secret on the Dogecoin chain...
    inputs, outputs = H.pay_out(s["b_coin"], s["alice_doge"].address)
    claim = doge_rpc.call("sendrawtransaction",
                          H.claimed(inputs, outputs, s["b_redeem"], s["secret"], s["alice_doge"].sign))
    doge_rpc.call("generate", 1)
    # ...Bob reads it there, and claims Alice's PEP with it.
    seen = doge_rpc.call("getrawtransaction", claim, 1)
    learned = H.secret_from_script_sig(bytes.fromhex(seen["vin"][0]["scriptSig"]["hex"]), s["b_redeem"])
    assert learned == s["secret"]
    inputs, outputs = H.pay_out(s["a_coin"], s["bob_pep"].address)
    pep_rpc.call("sendrawtransaction",
                 H.claimed(inputs, outputs, s["a_redeem"], learned, s["bob_pep"].sign))
    pep_rpc.call("generate", 1)
    assert pep_rpc.call("gettxout", s["a_coin"]["txid"], 0) is None
    assert doge_rpc.call("gettxout", s["b_coin"]["txid"], 0) is None


def test_a_swap_nobody_finishes_refunds_both_sides(regtest, doge):
    """Both locks already past their time (as if Alice never claimed): each side takes
    its own coins back, and neither can take the other's."""
    pep_rpc, doge_rpc = regtest.rpc, doge.rpc
    if float(pep_rpc.call("getbalance")) < 10:
        pep_rpc.call("generate", 101)
    s = _swap_setup(pep_rpc, doge_rpc, -3600, -7200)
    inputs, outputs = H.pay_out(s["a_coin"], s["alice_pep"].address)
    pep_rpc.call("sendrawtransaction", H.refunded(inputs, outputs, s["a_redeem"],
                                                  lambda d: _sign(s["alice_pep"].secret, d)))
    inputs, outputs = H.pay_out(s["b_coin"], s["bob_doge"].address)
    doge_rpc.call("sendrawtransaction", H.refunded(inputs, outputs, s["b_redeem"],
                                                   lambda d: _sign(s["bob_doge"].secret, d)))
    pep_rpc.call("generate", 1)
    doge_rpc.call("generate", 1)
    assert pep_rpc.call("gettxout", s["a_coin"]["txid"], 0) is None
    assert doge_rpc.call("gettxout", s["b_coin"]["txid"], 0) is None

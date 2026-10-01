"""A refereed escrow on a real chain: in, out to whoever the referee says, and back
to the owner -- but only once the game's unlock time has passed.

Against regtest, because every claim this file makes is one only the network
can settle: that the script spends at all, that the referee alone can move
things before unlock, that a spend by the owner before unlock is refused as
non-final, and that the ledger treats the escrow address as the sender and the
last output as the recipient.
"""

import contextlib
import pathlib
import secrets
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_offer import _settled, node                          # noqa: E402,F401
from test_account_tokens import _balance, _token                       # noqa: E402
from test_funding import N, _pubkey, _sign                             # noqa: E402
from test_web import app_state, client                               # noqa: F401,E402

from arcade import escrow as E                                       # noqa: E402
from arcade.referee import Referee                                   # noqa: E402
from arcade.rpc import RpcError                                       # noqa: E402
from arcade.script import b58check_encode, hash160                    # noqa: E402
from arcade.txbuild import op_return_script, p2pkh_script, push      # noqa: E402

COIN = 100_000_000
GAME = "a7" * 32


# --- without a chain ------------------------------------------------------------------

def test_the_script_says_what_it_does_and_reads_back():
    ref, own = bytes([2]) + secrets.token_bytes(32), bytes([3]) + secrets.token_bytes(32)
    for unlock in (1_790_900_000, 2 ** 31 - 1, 2 ** 31, 2 ** 32 - 1):
        redeem = E.redeem_script(ref, own, unlock, E.tag_for(GAME, "nOwner", unlock))
        said = E.parse(redeem)
        assert said == {"tag": E.tag_for(GAME, "nOwner", unlock), "referee": ref,
                        "unlock": unlock, "owner": own}
    assert E.parse(redeem[:-1]) is None and E.parse(b"\x00" + redeem) is None
    with pytest.raises(E.EscrowError):
        E.redeem_script(ref, own, 100, E.tag_for(GAME, "x", 100))      # a height, not a time
    assert E.tag_for(GAME, "a", 1) != E.tag_for(GAME, "b", 1) != E.tag_for("other", "a", 1), \
        "each game, owner and unlock time is its own escrow"


# --- on regtest -------------------------------------------------------------------------

class Owner:
    """A key the node never holds, with coins, signing in Python as a browser would."""

    def __init__(self, state, rpc, coins=5.0):
        self.secret = secrets.randbelow(N - 1) + 1
        if float(rpc.call("getbalance")) < coins + 1:
            rpc.call("generate", 101)                    # a wallet with coins to give
        self.pubkey = _pubkey(self.secret)
        self.address = b58check_encode(state.messaging.params.pubkeyhash_version,
                                       hash160(self.pubkey))
        txid = rpc.call("sendtoaddress", self.address, coins)
        tx = rpc.call("getrawtransaction", txid, 1)
        vout = next(o["n"] for o in tx["vout"]
                    if self.address in o["scriptPubKey"].get("addresses", []))
        self.coin = {"txid": txid, "vout": vout, "value": int(round(coins * COIN))}

    def send(self, rpc, data: bytes, to_script: bytes, amount: int, fee: int = 1_000_000) -> dict:
        """Spend this key's coin: the payload, change back here, `amount` to `to` last.
        Returns the coin `to` got."""
        inputs = [{"txid": self.coin["txid"], "vout": self.coin["vout"]}]
        change = self.coin["value"] - amount - fee
        mine = p2pkh_script(self.address)
        outputs = [(0, op_return_script(data)), (change, mine), (amount, to_script)]
        digest = E.sighash(inputs, outputs, 0, mine)
        sig = _sign(self.secret, digest)
        raw = E.serialize([{**inputs[0], "script_sig": push(sig) + push(self.pubkey)}], outputs)
        txid = rpc.call("sendrawtransaction", raw.hex())
        self.coin = {"txid": txid, "vout": 1, "value": change}
        return {"txid": txid, "vout": 2, "value": amount}


def _piece(state, txid, owner):
    index = state.token_index(state.messaging)
    with index.open() as db:
        db.conn.execute(
            "INSERT OR REPLACE INTO inscription(txid,number,creator,owner,block_height,position,"
            "content_type,content_len,sha256,json,chunks,content) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (txid, 7000 + int(txid[:2], 16), owner, owner, 1, 0, "text/plain", 1, "ab" * 32,
             "{}", 1, b"x"))
        db.conn.commit()


def _escrow(state, referee, owner, unlock):
    redeem = E.redeem_script(referee.pubkey, owner.pubkey, unlock,
                             E.tag_for(GAME, owner.address, unlock))
    return redeem, E.address_of(redeem, state.messaging.params)


def test_in_out_by_the_referee_and_back_to_the_owner_only_after_unlock(node, tmp_path):
    app, state, rpc = node
    referee = Referee(tmp_path)
    owner = Owner(state, rpc)
    other = b58check_encode(state.messaging.params.pubkeyhash_version, hash160(b"\x02" * 33))
    _settled(state, rpc)
    sword, shield = "b1" * 32, "b2" * 32
    _piece(state, sword, owner.address)
    pid = _token(state, property_id=150, name="Gold")
    _balance(state, owner.address, pid, 100 * COIN)

    unlock = int(time.time()) + 86400
    redeem, escrow = _escrow(state, referee, owner, unlock)
    into = E.p2sh_script(redeem)
    reserve = 5_000_000                                  # pays for the moves out
    nft_coin = owner.send(rpc, E.nft_payload(sword), into, reserve)
    gold_coin = owner.send(rpc, E.token_payload(pid, 40 * COIN), into, reserve)
    _settled(state, rpc)
    index = state.token_index(state.messaging)
    assert index.inscription(sword)["owner"] == escrow, "the sword is in escrow"
    assert int(index.balance(escrow, pid)) == 40 * COIN and \
        int(index.balance(owner.address, pid)) == 60 * COIN

    # The referee hands the sword to somebody else (whatever the game's judge said).
    inputs, outputs = E.move_out(nft_coin, escrow, E.nft_payload(sword), other)
    rpc.call("sendrawtransaction", E.signed(inputs, outputs, redeem, referee.sign))
    # ...and the gold back to its owner.
    inputs, outputs = E.move_out(gold_coin, escrow, E.token_payload(pid, 40 * COIN), owner.address)
    rpc.call("sendrawtransaction", E.signed(inputs, outputs, redeem, referee.sign))
    _settled(state, rpc)
    assert index.inscription(sword)["owner"] == other, "released to whoever the referee said"
    assert int(index.balance(owner.address, pid)) == 100 * COIN, "the gold came home"

    # The owner, before the unlock time: the network will not have it.
    _piece(state, shield, owner.address)
    shield_coin = owner.send(rpc, E.nft_payload(shield), into, reserve)
    _settled(state, rpc)
    inputs, outputs = E.move_out(shield_coin, escrow, E.nft_payload(shield), owner.address)
    early = E.signed(inputs, outputs, redeem, lambda d: _sign(owner.secret, d),
                     owner=True, unlock=unlock)
    with pytest.raises(RpcError):
        rpc.call("sendrawtransaction", early)
    # Nor can the owner use the referee's branch.
    forged = E.signed(inputs, outputs, redeem, lambda d: _sign(owner.secret, d))
    with pytest.raises(RpcError):
        rpc.call("sendrawtransaction", forged)
    assert index.inscription(shield)["owner"] == escrow


def test_after_unlock_the_owner_alone_takes_it_back(node, tmp_path):
    """No referee at all: it could have vanished. The unlock time is enough."""
    app, state, rpc = node
    referee = Referee(tmp_path)
    owner = Owner(state, rpc)
    _settled(state, rpc)
    helmet = "b3" * 32
    _piece(state, helmet, owner.address)
    unlock = int(time.time()) - 7200                     # the game's time is up
    redeem, escrow = _escrow(state, referee, owner, unlock)
    coin = owner.send(rpc, E.nft_payload(helmet), E.p2sh_script(redeem), 5_000_000)
    _settled(state, rpc)
    index = state.token_index(state.messaging)
    assert index.inscription(helmet)["owner"] == escrow

    inputs, outputs = E.move_out(coin, escrow, E.nft_payload(helmet), owner.address)
    back = E.signed(inputs, outputs, redeem, lambda d: _sign(owner.secret, d),
                    owner=True, unlock=unlock)
    rpc.call("sendrawtransaction", back)
    _settled(state, rpc)
    assert index.inscription(helmet)["owner"] == owner.address, "home, with no referee"

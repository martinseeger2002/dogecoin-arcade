"""Hash-time-locked contracts: the lock script every trustless trade settles through.

2026-10-13: trades "need to be 100% trustless with lock scripts", built and
signed client side; a node that facilitates trades runs "just like a regular lock
script that the client side does". This is that script, for coins, tokens and NFTs,
on any chain this arcade runs (Pepecoin testnet and mainnet, Dogecoin):

    OP_IF
        OP_SHA256 <secret hash> OP_EQUALVERIFY <claimer key> OP_CHECKSIG
    OP_ELSE
        <lock time> OP_CHECKLOCKTIMEVERIFY OP_DROP <refunder key> OP_CHECKSIG
    OP_ENDIF

* **The claimer, with the secret, at any time.** Claiming puts the secret in the
  claiming transaction's input script, on the chain, for anybody -- and above all
  the other side of the trade -- to read (`secret_from_script_sig`). That is the
  whole of a cross-chain swap: whoever made the secret claims on one chain and so
  hands the other side the secret to claim on the other.
* **The refunder alone, after the lock time.** CHECKLOCKTIMEVERIFY with a TIME, not
  a height: the two sides of a swap are on different chains, whose heights mean
  nothing to each other, and relative locks (CSV) never activated here (escrow.py).
  A counterparty that vanishes costs a wait, never the coins.

Nobody else can move what is locked: not this node, not the counterparty without
the secret, not the refunder before the time. The trade's state machine is
`arcade/trade.py`; this file is only the script and the transactions that spend it.

Assets move out of the lock the way they move out of an escrow (escrow.move_out):
the lock's own coin is the first input, which makes the lock address the sender of a
token send or an NFT transfer, and the recipient is the last output, the reference.
"""

from __future__ import annotations

import hashlib
import secrets as _secrets
from typing import Any

from . import escrow as E
from .script import b58check_encode, hash160
from .txbuild import op_return_script, p2pkh_script, push

OP_0, OP_1 = 0x00, 0x51
OP_IF, OP_ELSE, OP_ENDIF = 0x63, 0x67, 0x68
OP_DROP, OP_CHECKSIG = 0x75, 0xAC
OP_EQUALVERIFY = 0x88
OP_SHA256 = 0xA8
OP_CHECKLOCKTIMEVERIFY = 0xB1
SECRET_BYTES = 32
#: A spend after the lock time must not be final, or the lock time is not checked.
NOT_FINAL = E.NOT_FINAL
#: What one claim or refund costs, and the soft-dust floor (escrow.py's numbers).
SPEND_FEE = E.RELEASE_FEE
DUST = E.DUST


class HtlcError(ValueError):
    """Not a lock this file made, or not something a lock can do."""


# --- the secret -------------------------------------------------------------------------

def make_secret() -> tuple[bytes, bytes]:
    """(secret, its SHA-256). The secret stays with whoever made it until they claim."""
    secret = _secrets.token_bytes(SECRET_BYTES)
    return secret, hashlib.sha256(secret).digest()


def hash_of(secret: bytes) -> bytes:
    return hashlib.sha256(secret).digest()


# --- the script -------------------------------------------------------------------------

def redeem_script(secret_hash: bytes, claimer_pub: bytes, refunder_pub: bytes,
                  locktime: int) -> bytes:
    """The lock. `locktime` is a unix time (consensus: >= 500,000,000), never a height."""
    if len(secret_hash) != 32:
        raise HtlcError("the secret's hash is 32 bytes (SHA-256)")
    for key in (claimer_pub, refunder_pub):
        if len(key) != 33 or key[0] not in (2, 3):
            raise HtlcError("a key in a lock is a 33-byte compressed public key")
    if claimer_pub == refunder_pub:
        raise HtlcError("the claimer and the refunder are two different people")
    if not E.LOCKTIME_THRESHOLD <= int(locktime) < 2 ** 32:
        raise HtlcError("a lock time is a time (seconds since 1970), not a block height")
    return (bytes([OP_IF, OP_SHA256]) + push(secret_hash) + bytes([OP_EQUALVERIFY])
            + push(claimer_pub) + bytes([OP_CHECKSIG, OP_ELSE])
            + push(E._scriptnum(int(locktime))) + bytes([OP_CHECKLOCKTIMEVERIFY, OP_DROP])
            + push(refunder_pub) + bytes([OP_CHECKSIG, OP_ENDIF]))


def parse(redeem: bytes) -> dict | None:
    """{secret_hash, claimer, locktime, refunder} for a lock this file wrote, else None.

    Read back exactly, byte by byte: a script that only looks like a lock is not one,
    and the page that shows somebody what they are paying into reads it with this."""
    try:
        at = 0

        def take(n: int) -> bytes:
            nonlocal at
            if at + n > len(redeem):
                raise IndexError
            got = redeem[at:at + n]
            at += n
            return got

        if take(2) != bytes([OP_IF, OP_SHA256]) or take(1)[0] != 32:
            return None
        secret_hash = take(32)
        if take(1)[0] != OP_EQUALVERIFY or take(1)[0] != 33:
            return None
        claimer = take(33)
        if take(2) != bytes([OP_CHECKSIG, OP_ELSE]):
            return None
        size = take(1)[0]
        if not 1 <= size <= 5:
            return None
        locktime = E._readnum(take(size))
        if take(2) != bytes([OP_CHECKLOCKTIMEVERIFY, OP_DROP]) or take(1)[0] != 33:
            return None
        refunder = take(33)
        if take(2) != bytes([OP_CHECKSIG, OP_ENDIF]) or at != len(redeem):
            return None
        if redeem != redeem_script(secret_hash, claimer, refunder, locktime):
            return None
        return {"secret_hash": secret_hash, "claimer": claimer, "locktime": locktime,
                "refunder": refunder}
    except (IndexError, HtlcError):
        return None


def address_of(redeem: bytes, params: Any) -> str:
    """The lock's P2SH address on a chain (its own script-hash version byte)."""
    return b58check_encode(params.scripthash_version, hash160(redeem))


def p2sh_script(redeem: bytes) -> bytes:
    return E.p2sh_script(redeem)


# --- spending a lock --------------------------------------------------------------------

def claim_script_sig(sig: bytes, secret: bytes, redeem: bytes) -> bytes:
    """The IF branch: the claimer's signature, the secret, OP_1, the script."""
    return push(sig) + push(secret) + bytes([OP_1]) + push(redeem)


def refund_script_sig(sig: bytes, redeem: bytes) -> bytes:
    """The ELSE branch: the refunder's signature, OP_0, the script."""
    return push(sig) + bytes([OP_0]) + push(redeem)


def secret_from_script_sig(script_sig: bytes, redeem: bytes) -> bytes | None:
    """The secret a claim revealed, read off its input script -- or None.

    The other side of a swap calls this on the claiming transaction it sees on the
    chain, and checks the result against the hash it locked to before using it."""
    pushes = _pushes(script_sig)
    if len(pushes) != 4 or pushes[2] != bytes([OP_1]) or pushes[3] != redeem:
        return None
    said = parse(redeem)
    secret = pushes[1]
    if said is None or len(secret) != SECRET_BYTES or hash_of(secret) != said["secret_hash"]:
        return None
    return secret


def _pushes(script: bytes) -> list[bytes]:
    """Each push of an input script, with OP_0/OP_1 kept as one-byte items."""
    out, at = [], 0
    while at < len(script):
        op = script[at]
        at += 1
        if op in (OP_0, OP_1):
            out.append(bytes([op]))
            continue
        if 1 <= op <= 75:
            n = op
        elif op == 0x4C:
            n, at = script[at], at + 1
        elif op == 0x4D:
            n, at = int.from_bytes(script[at:at + 2], "little"), at + 2
        else:
            raise HtlcError(f"an input script this file did not write (opcode {op:#x})")
        out.append(script[at:at + n])
        at += n
    return out


def pay_out(coin: dict, to: str, fee: int = SPEND_FEE) -> tuple[list[dict], list[tuple[int, bytes]]]:
    """Coins out of a lock: the whole coin, less the fee, to one address."""
    value = int(coin["value"]) - int(fee)
    if value < DUST:
        raise HtlcError("the lock holds too little to pay for its own spend")
    return ([{"txid": coin["txid"], "vout": int(coin["vout"])}],
            [(value, p2pkh_script(to))])


def move_out(coin: dict, lock_address: str, data: bytes, to: str,
             fee: int = SPEND_FEE) -> tuple[list[dict], list[tuple[int, bytes]]]:
    """A token or an NFT out of a lock (escrow.move_out): the lock's coin first, which
    makes the lock the sender; the payload; `to` last, which makes it the receiver."""
    return E.move_out(coin, lock_address, data, to, fee=fee)


# --- a Doginal in a lock -----------------------------------------------------------------
#
# [Claude 2026-10-15] the operator: "use the atomic swap feature we have to trade doginals." A Doginal moves by koinu order,
# first in, first out (ord-dogecoin), so the lock and both its spends keep the inscribed coin first and whole:
#
#   lock    in:  0 the inscribed coin (V)       1.. clean coins of the giver
#           out: 0 V -> the lock                1 DOGINAL_RESERVE -> the same lock     2 change -> the giver
#   claim   in:  0 lock:0 (V)                   1 lock:1 (the reserve)
#   refund  out: 0 V -> the claimer (or, after the lock time, the refunder); the whole reserve is the fee
#
# The inscription is at offset 0 of output 0 at every step and its coin's value never changes. The fee of a spend comes
# from the reserve, the second input, which the giver locked beside the Doginal -- so neither side needs Dogecoin of its
# own to finish, and no other coin of anybody's (above all no other Doginal) is ever spent with it. Both inputs are
# lock coins, signed by `claimed` / `refunded` as any lock spend.

#: Coin locked beside a Doginal (output 1 of its lock) to pay for the one spend that moves it out: a lock spend's fee,
#: and the soft-dust charge again, since the Doginal's own coin (0.001 DOGE, usually) is under the soft-dust line.
DOGINAL_RESERVE = SPEND_FEE + DUST


def doginal_lock_outputs(value: int, redeem: bytes) -> list[tuple[int, bytes]]:
    """Outputs 0 and 1 of a Doginal's lock: the inscribed coin's whole `value`, then the reserve, both to the lock."""
    script = p2sh_script(redeem)
    return [(int(value), script), (DOGINAL_RESERVE, script)]


def doginal_out(lock_coin: dict, reserve_coin: dict, to: str) -> tuple[list[dict], list[tuple[int, bytes]]]:
    """A Doginal out of a lock, claim or refund: lock:0 first and whole to `to` as output 0, the reserve (lock:1) second
    and spent whole as the fee. Nothing else goes in, so nothing else can land on the Doginal's koinu."""
    if lock_coin["txid"] != reserve_coin["txid"] or int(lock_coin["vout"]) != 0 or int(reserve_coin["vout"]) != 1:
        raise HtlcError("a Doginal's lock is output 0 of its transaction and its reserve output 1")
    if int(reserve_coin["value"]) < SPEND_FEE:
        raise HtlcError("the reserve beside that Doginal is too small to pay for its spend")
    if int(lock_coin["value"]) <= 0:
        raise HtlcError("a Doginal's coin has value")
    return ([{"txid": lock_coin["txid"], "vout": 0}, {"txid": reserve_coin["txid"], "vout": 1}],
            [(int(lock_coin["value"]), p2pkh_script(to))])


def claimed(inputs: list[dict], outputs: list[tuple[int, bytes]], redeem: bytes,
            secret: bytes, sign) -> str:
    """The claim, signed: every input a lock coin, each spent with the secret.
    `sign(digest) -> DER signature + sighash byte`, by the claimer's key."""
    said = parse(redeem)
    if said is None:
        raise HtlcError("that is not a lock this file made")
    if hash_of(secret) != said["secret_hash"]:
        raise HtlcError("that secret does not open this lock")
    done = []
    for n, coin in enumerate(inputs):
        sig = sign(E.sighash(inputs, outputs, n, redeem, 0))
        done.append({**coin, "script_sig": claim_script_sig(sig, secret, redeem)})
    return E.serialize(done, outputs, 0).hex()


def refunded(inputs: list[dict], outputs: list[tuple[int, bytes]], redeem: bytes, sign) -> str:
    """The refund, signed by the refunder, with the lock's own time as nLockTime and
    every input not final -- the only way the network checks the lock time."""
    said = parse(redeem)
    if said is None:
        raise HtlcError("that is not a lock this file made")
    locktime = said["locktime"]
    coins = [{**c, "sequence": NOT_FINAL} for c in inputs]
    done = []
    for n, coin in enumerate(coins):
        sig = sign(E.sighash(coins, outputs, n, redeem, locktime))
        done.append({**coin, "script_sig": refund_script_sig(sig, redeem)})
    return E.serialize(done, outputs, locktime).hex()


__all__ = ["HtlcError", "make_secret", "hash_of", "redeem_script", "parse", "address_of",
           "p2sh_script", "claim_script_sig", "refund_script_sig", "secret_from_script_sig",
           "pay_out", "move_out", "claimed", "refunded", "op_return_script", "DOGINAL_RESERVE",
           "doginal_lock_outputs", "doginal_out"]

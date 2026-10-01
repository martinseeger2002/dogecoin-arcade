"""A refereed escrow: a player's NFTs and tokens held for a game, released by its judge.

Game-agnostic (2026-10-01: "Everything should be game agnostic"). A player puts
any mix of NFTs and tokens into an escrow address for one game; while they are
there, the game's referee -- the node a game names, as a prize pool does --
releases them to whoever the game's judge says: back to the player, or to
somebody else (an item dropped in a game and picked up by another player is
one such rule; the arcade does not know any). After the unlock time the GAME
chose, the player alone can take everything back, whether or not the referee
ever answers again.

    <tag> OP_DROP
    OP_IF   <referee key> OP_CHECKSIG
    OP_ELSE <unlock> OP_CHECKLOCKTIMEVERIFY OP_DROP <owner key> OP_CHECKSIG
    OP_ENDIF

* **The referee alone, before unlock.** Not the referee and the owner: an
  owner who could withdraw at any moment could empty the escrow the instant a
  game's rules went against them, and the escrow would decide nothing. The
  referee is trusted with what is escrowed, as it is with a prize pool's prizes,
  and it signs only what the game's judge says.
* **The owner alone, after unlock.** CHECKLOCKTIMEVERIFY: an absolute time,
  because relative timelocks (CSV) never activated on this chain. A referee
  that vanished, or refuses, costs the owner a wait and nothing else.
* **The tag** makes each escrow its own address: the game, the owner and the
  unlock time hashed together, so the address can be worked out again from
  facts anybody can see and the owner never needs this node to find their way
  back.

Moving things out is the ledger's ordinary payloads sent FROM the escrow
address -- one NFT transfer, or one token send, per transaction -- each with
the escrow's own coin as its first input (that is what makes the escrow the
sender) and the recipient as the last output (that is what makes them the
reference). Fees come out of coins the deposit put there.
"""

from __future__ import annotations

import hashlib
from typing import Any

from . import encoding, inscriptions as I, payload as P
from .script import b58check_encode, hash160
from .txbuild import op_return_script, p2pkh_script, push, varint

OP_0, OP_1 = 0x00, 0x51
OP_IF, OP_ELSE, OP_ENDIF = 0x63, 0x67, 0x68
OP_DROP, OP_CHECKSIG = 0x75, 0xAC
OP_CHECKLOCKTIMEVERIFY = 0xB1
SIGHASH_ALL = 1
#: nLockTime above this is a time, below it a height (consensus).
LOCKTIME_THRESHOLD = 500_000_000
#: A spend that wants its lock time enforced must not be final.
NOT_FINAL = 0xFFFFFFFE
TAG_BYTES = 16
#: What one release costs, and what each output a release makes is worth.
#: The deposit funds the escrow with one of each per thing it holds, plus one.
RELEASE_FEE = 1_000_000          # 0.01 coin: a release is ~300 bytes
DUST = 1_000_000                 # 0.01 coin: the soft-dust floor on this chain
UNLOCK_HOURS_MOST = 24 * 30


class EscrowError(ValueError):
    """Not an escrow, or not something an escrow can do."""


def _scriptnum(n: int) -> bytes:
    """A script number, minimally encoded (what CHECKLOCKTIMEVERIFY reads)."""
    if n == 0:
        return b""
    out = bytearray()
    neg, value = n < 0, abs(n)
    while value:
        out.append(value & 0xFF)
        value >>= 8
    if out[-1] & 0x80:
        out.append(0x80 if neg else 0x00)
    elif neg:
        out[-1] |= 0x80
    return bytes(out)


def _readnum(data: bytes) -> int:
    if not data:
        return 0
    value = int.from_bytes(data, "little")
    if data[-1] & 0x80:
        return -(value & ~(0x80 << (8 * (len(data) - 1))))
    return value


def tag_for(game: str, owner: str, unlock: int) -> bytes:
    """The escrow's own tag: its game, its owner and its unlock time, hashed."""
    seen = f"arcade-escrow\n{game}\n{owner}\n{int(unlock)}".encode()
    return hashlib.sha256(seen).digest()[:TAG_BYTES]


def redeem_script(referee_pub: bytes, owner_pub: bytes, unlock: int, tag: bytes) -> bytes:
    for name, key in (("referee", referee_pub), ("owner", owner_pub)):
        if len(key) != 33 or key[0] not in (2, 3):
            raise EscrowError(f"the {name} key is not a compressed public key")
    if not LOCKTIME_THRESHOLD <= int(unlock) < 2 ** 32:
        raise EscrowError("an escrow unlocks at a time (seconds since 1970)")
    if len(tag) != TAG_BYTES:
        raise EscrowError("an escrow's tag is 16 bytes")
    return (push(tag) + bytes([OP_DROP, OP_IF]) + push(referee_pub) + bytes([OP_CHECKSIG, OP_ELSE])
            + push(_scriptnum(int(unlock))) + bytes([OP_CHECKLOCKTIMEVERIFY, OP_DROP])
            + push(owner_pub) + bytes([OP_CHECKSIG, OP_ENDIF]))


def parse(redeem: bytes) -> dict | None:
    """{tag, referee, unlock, owner} out of a redeem script of exactly this shape."""
    try:
        at = 0

        def take(n):
            nonlocal at
            piece = redeem[at:at + n]
            if len(piece) != n:
                raise IndexError
            at += n
            return piece

        if take(1)[0] != TAG_BYTES:
            return None
        tag = take(TAG_BYTES)
        if take(2) != bytes([OP_DROP, OP_IF]) or take(1)[0] != 33:
            return None
        referee = take(33)
        if take(2) != bytes([OP_CHECKSIG, OP_ELSE]):
            return None
        n = take(1)[0]
        if not 1 <= n <= 5:
            return None
        unlock = _readnum(take(n))
        if take(2) != bytes([OP_CHECKLOCKTIMEVERIFY, OP_DROP]) or take(1)[0] != 33:
            return None
        owner = take(33)
        if take(2) != bytes([OP_CHECKSIG, OP_ENDIF]) or at != len(redeem):
            return None
        return {"tag": tag, "referee": referee, "unlock": unlock, "owner": owner}
    except IndexError:
        return None


def address_of(redeem: bytes, params: Any) -> str:
    return b58check_encode(params.scripthash_version, hash160(redeem))


def p2sh_script(redeem: bytes) -> bytes:
    """OP_HASH160 <hash> OP_EQUAL: what an output paying the escrow carries."""
    return bytes([0xA9]) + push(hash160(redeem)) + bytes([0x87])


# --- what moves -----------------------------------------------------------------------

def nft_payload(txid: str) -> bytes:
    """The OP_RETURN data handing one inscription to the reference output."""
    body = P.AnyData(data=I.Transfer(txid=bytes.fromhex(txid)).encode()).encode()
    return encoding.encode_class_c(body)


def token_payload(property_id: int, units: int) -> bytes:
    """The OP_RETURN data sending `units` of a token to the reference output."""
    if units <= 0:
        raise EscrowError("a token amount must be more than zero")
    return encoding.encode_class_c(P.SimpleSend(property_id=int(property_id),
                                                amount=int(units)).encode())


# --- transactions out of an escrow ------------------------------------------------------

def serialize(inputs: list[dict], outputs: list[tuple[int, bytes]], locktime: int = 0) -> bytes:
    """inputs: [{txid, vout, script_sig (bytes), sequence}], outputs: [(sats, script)]."""
    raw = (1).to_bytes(4, "little") + varint(len(inputs))
    for coin in inputs:
        raw += bytes.fromhex(coin["txid"])[::-1] + int(coin["vout"]).to_bytes(4, "little")
        sig = coin.get("script_sig", b"")
        raw += varint(len(sig)) + sig
        raw += int(coin.get("sequence", 0xFFFFFFFF)).to_bytes(4, "little")
    raw += varint(len(outputs))
    for value, script in outputs:
        raw += int(value).to_bytes(8, "little") + varint(len(script)) + script
    return raw + int(locktime).to_bytes(4, "little")


def sighash(inputs: list[dict], outputs: list[tuple[int, bytes]], index: int,
            redeem: bytes, locktime: int = 0) -> bytes:
    """SIGHASH_ALL for input `index`, spending the escrow (`redeem` as its script)."""
    blank = [{**c, "script_sig": redeem if n == index else b""} for n, c in enumerate(inputs)]
    raw = serialize(blank, outputs, locktime) + SIGHASH_ALL.to_bytes(4, "little")
    return hashlib.sha256(hashlib.sha256(raw).digest()).digest()


def referee_script_sig(sig: bytes, redeem: bytes) -> bytes:
    """The IF branch: the referee's signature, OP_1, the script."""
    return push(sig) + bytes([OP_1]) + push(redeem)


def owner_script_sig(sig: bytes, redeem: bytes) -> bytes:
    """The ELSE branch: the owner's signature, OP_0, the script."""
    return push(sig) + bytes([OP_0]) + push(redeem)


def move_out(coin: dict, escrow_address: str, data: bytes, to: str,
             fee: int = RELEASE_FEE, dust: int = DUST) -> tuple[list[dict], list[tuple[int, bytes]]]:
    """One thing out of the escrow: the escrow's coin in, the payload, the rest of
    the coin back to the escrow, and `dust` to `to` LAST -- the reference.

    `coin` is {txid, vout, value} at the escrow address. With nothing left over
    the change is left out, and the escrow's coin is spent whole."""
    value = int(coin["value"])
    back = value - fee - dust
    if back < 0:
        raise EscrowError("the escrow has too little coin left to pay for moving this; "
                          "a deposit funds one move per thing it holds")
    outputs = [(0, op_return_script(data))]
    if back >= DUST:
        outputs.append((back, p2pkh_script(escrow_address)))   # P2SH-aware
    outputs.append((dust, p2pkh_script(to)))
    return ([{"txid": coin["txid"], "vout": int(coin["vout"])}], outputs)


def signed(inputs: list[dict], outputs: list[tuple[int, bytes]], redeem: bytes,
           sign, *, owner: bool = False, unlock: int = 0) -> str:
    """The finished transaction, every escrow input signed by `sign(digest)`.

    The referee signs with the lock time 0; the owner (`owner=True`) signs
    with the lock time at `unlock` and every input not final, which is what
    makes the network check the escrow's own unlock time against it."""
    locktime = int(unlock) if owner else 0
    sequence = NOT_FINAL if owner else 0xFFFFFFFF
    coins = [{**c, "sequence": sequence} for c in inputs]
    done = []
    for n, coin in enumerate(coins):
        sig = sign(sighash(coins, outputs, n, redeem, locktime))
        script_sig = (owner_script_sig if owner else referee_script_sig)(sig, redeem)
        done.append({**coin, "script_sig": script_sig})
    return serialize(done, outputs, locktime).hex()

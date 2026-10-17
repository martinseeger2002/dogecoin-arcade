"""Inscriptions on Litecoin, the Doginals way (SadFrog's side, 2026-10-17; LITECOIN-PLAN rev. 1).

Litecoin's own ord inscriptions put their envelope in a Taproot witness. This arcade's wallet signs
legacy scripts only, so new Litecoin inscriptions are written the way Doginals are: the envelope rides
in a P2SH input's scriptSig, and the coin it lands on is an ordinary legacy output the wallet can hold,
send and swap. `doginals.py` reads them unchanged (envelope, trace, content).

The envelope, as `doginals.content` reads it:

    "ord"  <part count>  <content type>  <n-1> <part>  ...  <0> <last part>  <signature>  <redeem script>

and the redeem script that lets that scriptSig spend: <pubkey> OP_CHECKSIGVERIFY, one OP_DROP per data
push, OP_TRUE. The inscription is born on the reveal transaction's first output's first litoshi.

Compatibility, said plainly (shown to users too, `TAPROOT_NOTE`): an inscription written here is seen by
this arcade and by Doginals-style indexers pointed at Litecoin; Litecoin's Taproot ord indexers and
marketplaces do not see it. Existing Taproot inscriptions are untouched; reading them is a separate,
Litecoin-only adapter (a later phase), never `doginals.py`.

Litecoin-only by design: nothing here is imported by the Pepecoin or Dogecoin paths.
"""

from __future__ import annotations

from typing import Callable

from . import escrow
from .script import b58check_encode, hash160
from .txbuild import push

#: Standardness on Litecoin 0.21 (the same numbers as Dogecoin): a pushed element is at most 520 bytes,
#: a whole scriptSig at most 1,650 bytes.
MAX_PUSH = 520
MAX_SCRIPT_SIG = 1650
#: Room left in the scriptSig for the signature (73) and the redeem script's push.
SIGNATURE_ROOM = 73 + 3

OP_CHECKSIGVERIFY, OP_DROP, OP_TRUE = 0xAD, 0x75, 0x51

TAPROOT_NOTE = (
    "Litecoin inscriptions made here use the Doginals format (the envelope in a legacy input), so this "
    "arcade can hold, send and trade them with the wallet it already has. Litecoin's Taproot ord "
    "indexers and marketplaces do not show Doginals-format inscriptions, and this arcade cannot yet "
    "move inscriptions that live on Taproot (ltc1p...) addresses -- use a Taproot wallet for those.")


class InscribeError(ValueError):
    pass


def number(n: int) -> bytes:
    """A small number as a script push, the way ord writes part counts: OP_0, OP_1..OP_16, else bytes."""
    if n == 0:
        return b"\x00"
    if 1 <= n <= 16:
        return bytes([0x50 + n])
    return push(n.to_bytes((n.bit_length() + 7) // 8, "little"))


def data(part: bytes) -> bytes:
    """A data push that standard relay accepts: one byte 1..16 must be its OP_n (MINIMALDATA);
    `doginals._pushes` reads that back as the same byte."""
    if len(part) == 1 and 1 <= part[0] <= 16:
        return bytes([0x50 + part[0]])
    return push(part)


def envelope_pushes(content_type: str, content: bytes) -> list[bytes]:
    """The envelope's pushes, each already encoded as script: "ord", count, type, then the parts."""
    if not content_type or len(content_type.encode()) > 80:
        raise InscribeError("a content type is required (at most 80 bytes)")
    parts = [content[i:i + MAX_PUSH] for i in range(0, len(content), MAX_PUSH)] or [b""]
    out = [push(b"ord"), number(len(parts)), push(content_type.encode())]
    for left, part in zip(range(len(parts) - 1, -1, -1), parts):
        out += [number(left), data(part)]
    return out


def redeem_script(pubkey: bytes, drops: int) -> bytes:
    """<pubkey> OP_CHECKSIGVERIFY, then one OP_DROP per envelope push, then OP_TRUE."""
    if len(pubkey) != 33:
        raise InscribeError("a compressed public key is required")
    return push(pubkey) + bytes([OP_CHECKSIGVERIFY]) + bytes([OP_DROP]) * drops + bytes([OP_TRUE])


def plan(content_type: str, content: bytes, pubkey: bytes) -> tuple[list[bytes], bytes]:
    """(envelope pushes, redeem script) for a one-transaction inscription, or InscribeError when the
    content does not fit a standard scriptSig. (Longer content continues in further transactions, as
    `doginals.content` already reads; writing those is a later step.)"""
    pushes = envelope_pushes(content_type, content)
    redeem = redeem_script(pubkey, len(pushes))
    size = sum(len(p) for p in pushes) + SIGNATURE_ROOM + len(push(redeem))
    if size > MAX_SCRIPT_SIG:
        raise InscribeError(f"{len(content)} bytes is more than one Litecoin transaction carries "
                            f"({size} > {MAX_SCRIPT_SIG}); split inscriptions are not written yet")
    return pushes, redeem


def commit_address(redeem: bytes, scripthash_version: int) -> str:
    """Where the commit transaction pays: the P2SH address of the redeem script."""
    return b58check_encode(scripthash_version, hash160(redeem))


def reveal(commit: dict, pushes: list[bytes], redeem: bytes, to_script: bytes, postage: int,
           sign: Callable[[bytes], bytes]) -> str:
    """The reveal transaction, as hex: spends the commit coin ({txid, vout, value}) and puts the
    inscription on output 0 (`postage` litoshi to `to_script`); the rest of the coin is the fee.
    `sign(digest)` returns a DER signature with its SIGHASH_ALL byte."""
    if postage <= 0 or postage >= int(commit["value"]):
        raise InscribeError("postage must be above zero and below the commit coin")
    inputs = [{"txid": commit["txid"], "vout": int(commit["vout"])}]
    outputs = [(postage, to_script)]
    signature = sign(escrow.sighash(inputs, outputs, 0, redeem))
    script_sig = b"".join(pushes) + push(signature) + push(redeem)
    if len(script_sig) > MAX_SCRIPT_SIG:
        raise InscribeError(f"the scriptSig came to {len(script_sig)} bytes, over {MAX_SCRIPT_SIG}")
    return escrow.serialize([{**inputs[0], "script_sig": script_sig}], outputs).hex()

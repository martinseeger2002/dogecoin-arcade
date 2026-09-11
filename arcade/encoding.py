"""Class B and Class C transaction encodings.

Omni can carry a payload in two shapes:

  **Class C** -- a single OP_RETURN output holding `marker || payload`. Simple and
  cheap, but capped by the node's datacarrier limit: 80 usable bytes on Pepecoin
  (MAX_OP_RETURN_RELAY = 83, pepecoin/src/script/standard.h:30), so 76 bytes of
  payload after Arcade's 4-byte marker.

  **Class B** -- the payload is split into 30-byte chunks, each disguised as a
  public key inside a 1-of-3 bare multisig output. Far higher capacity (255
  packets x 30 = 7,650 bytes) at the cost of larger transactions.

Obfuscation
-----------
Class B chunks are XORed with a keystream derived from the sender's address. The
derivation is iterated SHA-256 where **each round hashes the UPPERCASE HEX STRING
of the previous digest**, not the digest bytes (parsing.cpp:108-131). Round 1
hashes the address string itself. Getting this wrong produces plausible-looking
garbage rather than an error, so it is pinned by test vectors.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

from .config import MARKER

PACKET_SIZE = 31          # 1 sequence byte + 30 data bytes (parsing.h:19)
PACKET_DATA = PACKET_SIZE - 1
MAX_PACKETS = 255         # parsing.h:20
MAX_CLASS_B_PAYLOAD = MAX_PACKETS * PACKET_DATA   # 7,650 bytes

# secp256k1 field prime, for checking that a fabricated "public key" is a real
# curve point -- nodes reject multisig outputs containing invalid keys.
_SECP256K1_P = 2**256 - 2**32 - 977


class EncodingError(Exception):
    """The payload cannot be encoded, or a transaction cannot be decoded."""


# --- obfuscation --------------------------------------------------------------


def prepare_obfuscated_hashes(seed: str, count: int) -> list[bytes]:
    """Derive `count` 32-byte obfuscation hashes from `seed` (the sender address).

    Returns a list indexed from 1, matching Omni's 1-based `vstrHashes`, so
    element 0 is a placeholder and packet N uses hashes[N].

    Mirrors parsing.cpp:108-131:
        sha_input = seed
        for j in 1..count:
            digest    = SHA256(sha_input)
            hashes[j] = digest
            sha_input = UPPERCASE_HEX(digest)      <- the string, not the bytes
    """
    if count > MAX_PACKETS:
        count = MAX_PACKETS

    hashes: list[bytes] = [b""]  # index 0 unused
    sha_input = seed.encode("ascii")
    for _ in range(count):
        digest = hashlib.sha256(sha_input).digest()
        hashes.append(digest)
        sha_input = digest.hex().upper().encode("ascii")
    return hashes


def _xor(data: bytes, key: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, key))


# --- fabricated public keys ---------------------------------------------------


def is_valid_compressed_pubkey(key: bytes) -> bool:
    """True if `key` is a valid 33-byte compressed secp256k1 point.

    A compressed key is 0x02/0x03 followed by an x coordinate. It is valid when
    x < p and x^3 + 7 is a quadratic residue mod p -- i.e. a y actually exists.
    Bare multisig outputs containing invalid keys are non-standard, so a
    fabricated key that fails this would make the transaction unrelayable.
    """
    if len(key) != 33 or key[0] not in (0x02, 0x03):
        return False
    x = int.from_bytes(key[1:], "big")
    if x >= _SECP256K1_P or x == 0:
        return False
    y_squared = (pow(x, 3, _SECP256K1_P) + 7) % _SECP256K1_P
    if y_squared == 0:
        return False
    # Euler's criterion: a residue iff a^((p-1)/2) == 1 (mod p).
    return pow(y_squared, (_SECP256K1_P - 1) // 2, _SECP256K1_P) == 1


def _make_fake_pubkey(obfuscated: bytes, rng: bytes | None = None) -> bytes:
    """Turn 31 obfuscated bytes into a valid-looking compressed public key.

    Layout is 0x02 || obfuscated(31) || filler(1). Only the final byte is free,
    so we vary it until the point is on the curve -- the same trick Omni uses
    (encoding.cpp:52-61), which succeeds for roughly half of all candidates.
    """
    if len(obfuscated) != PACKET_SIZE:
        raise EncodingError(f"expected {PACKET_SIZE} obfuscated bytes, got {len(obfuscated)}")

    start = rng[0] if rng else os.urandom(1)[0]
    for offset in range(256):
        candidate = bytes([0x02]) + obfuscated + bytes([(start + offset) % 256])
        if is_valid_compressed_pubkey(candidate):
            return candidate
    raise EncodingError("no valid curve point found in 256 attempts (statistically impossible)")


# --- Class C ------------------------------------------------------------------


def class_c_script_size(data_len: int) -> int:
    """Size of the scriptPubKey holding `data_len` bytes after OP_RETURN.

    The node limits the whole script, not just the data
    (pepecoin/src/policy/policy.cpp:51). A direct push costs one byte up to 75;
    beyond that OP_PUSHDATA1 adds a length byte, so the overhead is 2. Assuming a
    flat overhead lets an over-sized script through, which the node then refuses
    to relay.
    """
    push_overhead = 1 if data_len <= 75 else 2
    return 1 + push_overhead + data_len


def max_class_c_payload(max_datacarrier: int = 83) -> int:
    """Largest payload (after the marker) that still fits the datacarrier limit."""
    for candidate in range(max_datacarrier, -1, -1):
        if class_c_script_size(len(MARKER) + candidate) <= max_datacarrier:
            return candidate
    return 0


def encode_class_c(payload: bytes, max_datacarrier: int = 83) -> bytes:
    """Return the OP_RETURN data (marker || payload).

    The caller wraps this in `OP_RETURN <push>`. `max_datacarrier` is the node's
    -datacarriersize; the default 83 matches Pepecoin's MAX_OP_RETURN_RELAY.
    """
    data = MARKER + payload
    size = class_c_script_size(len(data))
    if size > max_datacarrier:
        raise EncodingError(
            f"Class C payload too large: {len(payload)} bytes of payload plus a "
            f"{len(MARKER)}-byte marker needs a {size}-byte scriptPubKey, over the "
            f"{max_datacarrier}-byte datacarrier limit "
            f"(max payload is {max_class_c_payload(max_datacarrier)})"
        )
    return data


def decode_class_c(op_return_data: bytes) -> bytes | None:
    """Extract a payload from OP_RETURN data, or None if it is not ours.

    Returning None rather than raising is deliberate: most OP_RETURN outputs on
    the chain belong to other protocols, and encountering one is normal, not an
    error.
    """
    if not op_return_data.startswith(MARKER):
        return None
    return op_return_data[len(MARKER) :]


# --- Class B ------------------------------------------------------------------


@dataclass(frozen=True)
class ClassBOutput:
    """One 1-of-3 bare multisig output carrying up to two packets."""

    keys: tuple[bytes, ...]      # [redeeming_pubkey, fake1, (fake2)]
    required: int = 1

    @property
    def data_keys(self) -> tuple[bytes, ...]:
        return self.keys[1:]


def encode_class_b(
    sender: str,
    redeeming_pubkey: bytes,
    payload: bytes,
    rng: bytes | None = None,
) -> list[ClassBOutput]:
    """Split `payload` into obfuscated multisig outputs.

    `redeeming_pubkey` is the sender's own key and is included in every output, so
    the dust remains **spendable by the sender** (encoding.cpp:37). That is what
    keeps Class B from permanently bloating the UTXO set.
    """
    if len(payload) > MAX_CLASS_B_PAYLOAD:
        raise EncodingError(
            f"payload of {len(payload)} bytes exceeds the Class B maximum of "
            f"{MAX_CLASS_B_PAYLOAD} ({MAX_PACKETS} packets x {PACKET_DATA} bytes)"
        )
    if not payload:
        raise EncodingError("refusing to encode an empty payload")
    if not is_valid_compressed_pubkey(redeeming_pubkey):
        raise EncodingError("redeeming_pubkey is not a valid compressed secp256k1 key")

    packet_count = (len(payload) + PACKET_DATA - 1) // PACKET_DATA
    hashes = prepare_obfuscated_hashes(sender, packet_count)

    packets: list[bytes] = []
    for index in range(packet_count):
        chunk = payload[index * PACKET_DATA : (index + 1) * PACKET_DATA]
        sequence = index + 1
        # Sequence byte, data, zero-padded to 31 bytes.
        plain = bytes([sequence]) + chunk
        plain = plain.ljust(PACKET_SIZE, b"\x00")
        packets.append(_make_fake_pubkey(_xor(plain, hashes[sequence][:PACKET_SIZE]), rng))

    outputs: list[ClassBOutput] = []
    for index in range(0, len(packets), 2):
        outputs.append(ClassBOutput(keys=(redeeming_pubkey, *packets[index : index + 2])))
    return outputs


def decode_class_b(sender: str, multisig_keys: list[list[bytes]]) -> bytes:
    """Reassemble a payload from multisig outputs, in transaction output order.

    `multisig_keys` is one list of public keys per multisig output, in the order
    they appear in the transaction. The first key of each output is the sender's
    redeeming key and is skipped.

    **Padding is NOT stripped.** The returned payload is always a multiple of 30
    bytes, because the final packet is zero-filled on the way out. Omni behaves
    identically -- `packet_size = mdata_count * (PACKET_SIZE - 1)`
    (omnicore.cpp:1263) -- so matching it is a consensus requirement, not a
    stylistic choice.

    The consequence matters for type 200 (AnyData), whose body is "the rest of
    the payload": a Class B AnyData will carry up to 29 trailing NUL bytes that
    the sender never wrote. Stripping them here would be worse -- it would
    silently destroy real trailing NULs in a file chunk. The fix belongs one layer
    up: Arcade's v2 inscription header carries an explicit content length, so the
    reader knows exactly where the data ends. Length-delimited message types
    (everything except 200) are unaffected, since they stop reading at their own
    final field.
    """
    data_keys: list[bytes] = []
    for keys in multisig_keys:
        data_keys.extend(keys[1:])

    if not data_keys:
        raise EncodingError("no data-carrying keys found in the supplied multisig outputs")
    if len(data_keys) > MAX_PACKETS:
        raise EncodingError(f"{len(data_keys)} packets exceeds the maximum of {MAX_PACKETS}")

    hashes = prepare_obfuscated_hashes(sender, len(data_keys))

    chunks: list[bytes] = []
    for index, key in enumerate(data_keys):
        sequence = index + 1
        # Skip the 0x02 prefix; the trailing filler byte is not part of the packet
        # (omnicore.cpp:1244 reads exactly PACKET_SIZE bytes from offset 1).
        obfuscated = key[1 : 1 + PACKET_SIZE]
        packet = _xor(obfuscated, hashes[sequence][:PACKET_SIZE])

        if packet[0] != sequence:
            raise EncodingError(
                f"packet {index} has sequence number {packet[0]}, expected {sequence}; "
                "the sender address used for deobfuscation is probably wrong"
            )
        chunks.append(packet[1:])

    return b"".join(chunks)

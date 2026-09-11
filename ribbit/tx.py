"""Extracting a Ribbit transaction from a chain transaction.

Four things must be determined, in order:

  1. **encoding class** -- C (OP_RETURN) or B (marker output + bare multisig)
  2. **sender**        -- differs by class, and Class B needs it to deobfuscate
  3. **reference**     -- the recipient, where the message type has one
  4. **payload**       -- the raw bytes handed to `ribbit.payload.decode`

Rules mirror omnicore/src/omnicore/omnicore.cpp:758-1195. Where Ribbit diverges
it is called out in a comment; the only intended divergence in this module is
that **Class A is not implemented at all** -- it is Omni's 2013 legacy encoding,
never used on Pepecoin because Ribbit starts at its own activation height.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterable

from .config import Params
from .encoding import EncodingError, decode_class_b, decode_class_c
from .script import ALLOWED_INPUT_TYPES, OutputType, ParsedOutput, parse_output


class EncodingClass(Enum):
    NONE = "none"
    B = "B"
    C = "C"


class TxError(Exception):
    """The transaction carries a marker but cannot be interpreted."""


@dataclass(frozen=True)
class PrevOut:
    """A resolved transaction input: what it was, and who owned it."""

    address: str | None
    value: int
    type: OutputType


@dataclass(frozen=True)
class RibbitTransaction:
    """A decoded protocol-carrying transaction, before any state logic runs."""

    txid: str
    block_height: int
    position: int
    encoding_class: EncodingClass
    sender: str
    reference: str | None
    payload: bytes
    fee: int

    def __repr__(self) -> str:
        return (
            f"RibbitTransaction(txid={self.txid[:12]}..., height={self.block_height}, "
            f"class={self.encoding_class.value}, sender={self.sender}, "
            f"reference={self.reference}, payload={len(self.payload)}B)"
        )


PrevOutLookup = Callable[[str, int], PrevOut]


def _parsed_outputs(tx: dict[str, Any], params: Params) -> list[ParsedOutput]:
    outputs: list[ParsedOutput] = []
    for vout in tx.get("vout", []):
        script_hex = vout.get("scriptPubKey", {}).get("hex", "")
        value = int(round(float(vout.get("value", 0)) * 100_000_000))
        outputs.append(parse_output(script_hex, value, params))
    return outputs


def detect_class(outputs: Iterable[ParsedOutput], marker_address: str | None) -> EncodingClass:
    """Determine the encoding class.

    Class C wins over Class B when both shapes are present, matching
    omnicore.cpp:840-846 where `hasOpReturn` is tested first.
    """
    has_marker_output = False
    has_multisig = False

    for output in outputs:
        if output.type is OutputType.NULL_DATA:
            if output.data is not None and decode_class_c(output.data) is not None:
                return EncodingClass.C
        elif output.type is OutputType.MULTISIG:
            has_multisig = True
        elif output.type is OutputType.PUBKEYHASH:
            if marker_address is not None and output.address == marker_address:
                has_marker_output = True

    if has_marker_output and has_multisig:
        return EncodingClass.B
    return EncodingClass.NONE


def determine_sender(
    tx: dict[str, Any],
    encoding_class: EncodingClass,
    lookup: PrevOutLookup,
) -> tuple[str, int]:
    """Return (sender address, total input value).

    Two different rules, exactly as Omni has them (omnicore.cpp:957-1021):

      * **Class C** -- the owner of the FIRST input, full stop.
      * **Class B** -- "largest input by sum": input values are summed per source
        address and the largest total wins.

    The tie-break in the Class B case is subtle and worth spelling out. Omni
    iterates a `std::map<std::string, int64_t>`, which is ordered
    lexicographically by address, and replaces the running maximum only on a
    strictly greater value. So when two addresses contribute equally, the
    **lexicographically smaller address wins**. We reproduce that deliberately.
    """
    vins = tx.get("vin", [])
    if not vins:
        raise TxError("transaction has no inputs")
    if any("coinbase" in vin for vin in vins):
        raise TxError("coinbase transactions cannot carry a payload")

    if encoding_class is EncodingClass.C:
        first = vins[0]
        prev = lookup(first["txid"], int(first["vout"]))
        if prev.type not in ALLOWED_INPUT_TYPES:
            raise TxError(
                f"first input is {prev.type.value}; only pubkeyhash and scripthash "
                "are allowed as inputs (rules.cpp:416-430)"
            )
        if prev.address is None:
            raise TxError("first input has no extractable address")
        total = prev.value
        for vin in vins[1:]:
            total += lookup(vin["txid"], int(vin["vout"])).value
        return prev.address, total

    sums: dict[str, int] = {}
    total = 0
    for vin in vins:
        prev = lookup(vin["txid"], int(vin["vout"]))
        if prev.type not in ALLOWED_INPUT_TYPES:
            raise TxError(
                f"input is {prev.type.value}; only pubkeyhash and scripthash are "
                "allowed as inputs (rules.cpp:416-430)"
            )
        if prev.address is None:
            raise TxError("input has no extractable address")
        sums[prev.address] = sums.get(prev.address, 0) + prev.value
        total += prev.value

    # Lexicographic iteration + strict > reproduces std::map's tie-break.
    best_address = ""
    best_value = 0
    for address in sorted(sums):
        if sums[address] > best_value:
            best_address, best_value = address, sums[address]
    if not best_address:
        raise TxError("could not determine a sender from the inputs")
    return best_address, total


def determine_reference(
    outputs: list[ParsedOutput], sender: str, marker_address: str | None
) -> str | None:
    """Find the reference (recipient) address.

    omnicore.cpp:1154-1190. Candidate outputs are those with an extractable
    destination that are NOT the marker address (omnicore.cpp:1057 excludes the
    marker before this runs).

      * exactly one candidate -> that is the reference
      * more than one        -> skip the FIRST output back to the sender, treating
                                it as change, and take the LAST of the rest
    """
    candidates = [
        output.address
        for output in outputs
        if output.address is not None and output.address != marker_address
    ]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    reference: str | None = None
    change_removed = False
    for address in candidates:
        if address == sender and not change_removed:
            change_removed = True
            continue
        reference = address          # keep overwriting: the last one wins
    return reference


def extract_payload(
    outputs: list[ParsedOutput], encoding_class: EncodingClass, sender: str
) -> bytes:
    """Pull the raw payload out of the transaction's outputs."""
    if encoding_class is EncodingClass.C:
        chunks = [
            decode_class_c(output.data)
            for output in outputs
            if output.type is OutputType.NULL_DATA and output.data is not None
        ]
        present = [chunk for chunk in chunks if chunk is not None]
        if not present:
            raise TxError("Class C transaction has no marked OP_RETURN output")
        # Omni concatenates every marked OP_RETURN in output order.
        return b"".join(present)

    multisig_keys = [
        list(output.pubkeys) for output in outputs if output.type is OutputType.MULTISIG
    ]
    if not multisig_keys:
        raise TxError("Class B transaction has no multisig outputs")
    try:
        return decode_class_b(sender, multisig_keys)
    except EncodingError as exc:
        raise TxError(f"Class B deobfuscation failed: {exc}") from exc


def extract(
    tx: dict[str, Any],
    block_height: int,
    position: int,
    params: Params,
    lookup: PrevOutLookup,
) -> RibbitTransaction | None:
    """Decode one transaction, or return None if it carries no Ribbit payload.

    Returning None for a non-Ribbit transaction is correct and expected -- the
    overwhelming majority of chain transactions are not ours. Raising TxError
    means the transaction *looked* like ours and could not be read, which is a
    different and much more interesting situation.
    """
    outputs = _parsed_outputs(tx, params)
    encoding_class = detect_class(outputs, params.marker_address)
    if encoding_class is EncodingClass.NONE:
        return None

    sender, value_in = determine_sender(tx, encoding_class, lookup)
    reference = determine_reference(outputs, sender, params.marker_address)
    payload = extract_payload(outputs, encoding_class, sender)

    value_out = sum(output.value for output in outputs)
    return RibbitTransaction(
        txid=tx["txid"],
        block_height=block_height,
        position=position,
        encoding_class=encoding_class,
        sender=sender,
        reference=reference,
        payload=payload,
        fee=value_in - value_out,
    )

"""Writing an inscription to the chain, and what it will cost to do it.

The pieces are ordinary Class B transactions carrying type-200 payloads, so
they go out exactly the way a long message does -- through `MessageSender`,
funded independently from a split wallet so they all go at once rather than
one per block. What is here is the arithmetic in front of that: how many
transactions, how much of it is fee, how much is dust, and how much of the
dust comes back.

The estimate is the whole point. An inscription is permanent and paid for in
advance, and a number shown afterwards is a number shown too late.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import inscriptions as I
from .encoding import MAX_CLASS_B_PAYLOAD
from .ledger import COIN

#: What one Class B transaction carries, after the 4-byte AnyData header.
CHUNK_CAPACITY = MAX_CLASS_B_PAYLOAD - 4

#: Every 30-byte packet is half of a multisig output, and each of those costs
#: the dust value. Kept here rather than imported from the messenger so a
#: change to one is not a silent change to the other's prices.
PACKET_DATA = 30
PACKETS_PER_OUTPUT = 2

#: Paid to each data output and to the marker. Spendable by the creator, since
#: the encoder puts their own key in every one of them -- see `sweep`.
DUST = 0.01

#: What a transaction of this shape costs per kilobyte, at the node's ordinary
#: rate. Measured, not assumed: a 14,669-byte chunk paid 0.1467.
FEE_PER_KB = 0.01

#: Bytes on the wire per data output: a 1-of-3 bare multisig with two fake
#: pubkeys, plus value and framing.
BYTES_PER_OUTPUT = 114
BASE_TX_BYTES = 300


@dataclass(frozen=True)
class Estimate:
    """What an inscription will cost before anybody has spent anything."""

    content_bytes: int
    chunks: int
    outputs: int
    chain_bytes: int
    fee: float
    dust: float

    @property
    def total(self) -> float:
        return self.fee + self.dust

    @property
    def recoverable(self) -> float:
        """The dust: spendable again by whoever created the inscription."""
        return self.dust

    @property
    def net(self) -> float:
        """What it costs once the dust has been swept back."""
        return self.fee

    def describe(self) -> str:
        return (f"{self.chunks:,} transaction{'s' if self.chunks != 1 else ''}, "
                f"about {self.total:,.2f} to send and {self.net:,.2f} once the "
                f"dust is swept back")


def estimate(content: bytes | int, content_type: str = "application/octet-stream",
             json_text: str = "") -> Estimate:
    """Price an inscription without building it.

    Accepts a length as well as the bytes, so a page can price a file the
    moment it is chosen without holding a second copy of it.
    """
    size = content if isinstance(content, int) else len(content)
    manifest = I.MANIFEST_FIXED_LEN + len(content_type.encode()) + len(json_text.encode())
    stream = manifest + size
    room = CHUNK_CAPACITY - I.CHUNK_HEADER_LEN
    chunks = max(1, -(-stream // room))
    even = -(-stream // chunks)

    outputs_each = -(-(-(-even // PACKET_DATA) // PACKETS_PER_OUTPUT))
    outputs = outputs_each * chunks
    chain_bytes = chunks * BASE_TX_BYTES + outputs * BYTES_PER_OUTPUT
    fee = round(chain_bytes / 1000 * FEE_PER_KB, 8)
    dust = round((outputs + chunks) * DUST, 8)     # +1 marker output per chunk
    return Estimate(content_bytes=size, chunks=chunks, outputs=outputs,
                    chain_bytes=chain_bytes, fee=fee, dust=dust)


@dataclass
class Plan:
    """An inscription, ready to be paid for."""

    payloads: list[bytes]
    estimate: Estimate
    content_type: str
    json: str
    content_len: int

    @property
    def chunks(self) -> int:
        return len(self.payloads)


def plan(content: bytes, content_type: str, json_text: str = "") -> Plan:
    """Everything that has to go on chain, and what it will cost."""
    from . import payload as P

    bodies = I.plan(content, content_type, json_text, capacity=CHUNK_CAPACITY)
    payloads = [P.AnyData(data=body).encode() for body in bodies]
    return Plan(payloads=payloads,
                estimate=estimate(content, content_type,
                                  I.validate_json(json_text)),
                content_type=content_type or "application/octet-stream",
                json=I.validate_json(json_text), content_len=len(content))


def sats(coins: float) -> int:
    return int(round(coins * COIN))

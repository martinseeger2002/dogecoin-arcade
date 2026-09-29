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
from typing import Any

from . import fees
from . import media
from . import inscriptions as I
from .encoding import MAX_CLASS_B_PAYLOAD
from .ledger import COIN

#: What one Class B transaction carries, after the 4-byte AnyData header the
#: sender adds around whatever it is handed.
CHUNK_CAPACITY = MAX_CLASS_B_PAYLOAD - 4

#: Every 30-byte packet is half of a multisig output, and each of those costs
#: the dust value. Kept here rather than imported from the messenger so a
#: change to one is not a silent change to the other's prices.
PACKET_DATA = 30
PACKETS_PER_OUTPUT = 2

#: Paid to each data output and to the marker. Spendable by the creator, since
#: the encoder puts their own key in every one of them -- see `sweep`.
DUST = 0.01

#: What a transaction costs per kilobyte at the node's ordinary rate -- of
#: its VIRTUAL size, which for a multisig chunk is about 3.5x its bytes
#: (fees.py). A 14,669-byte chunk paid 0.1467 on its bytes and then sat in
#: every mempool, one per block, until it was repriced.
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
    fee = sum(fees.fee_for(BASE_TX_BYTES + outputs_each * BYTES_PER_OUTPUT,
                           outputs_each * fees.SIGOPS_PER_MULTISIG + 2)
              for _ in range(chunks)) / COIN
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


def plan(content: bytes, content_type: str, json_text: str = "",
         inscription_id: bytes | None = None) -> Plan:
    """Everything that has to go on chain, and what it will cost.

    The payloads are the inscription bodies THEMSELVES, not wrapped in AnyData:
    `MessageSender` wraps whatever it is given (sender.py `_class_b_outputs`),
    so wrapping here too put an AnyData inside an AnyData. The chain accepted
    it, the index recorded it as a valid type-200 transaction, and the
    inscription simply never appeared -- because the outer body started with a
    payload header rather than with INSC. Nothing complained anywhere; the file
    was paid for and invisible. Found by putting one through a real node.

    `inscription_id` is normally drawn fresh. A collection run passes the one
    it wrote down, so a piece sent before a crash and a piece sent after it
    belong to the same inscription.
    """
    # One name a type goes by, so a library inscribed from a browser that
    # calls it x-javascript is served as JavaScript (media.standard_type).
    content_type = media.standard_type(content_type)
    bodies = I.plan(content, content_type, json_text, capacity=CHUNK_CAPACITY,
                    inscription_id=inscription_id)
    return Plan(payloads=bodies,
                estimate=estimate(content, content_type,
                                  I.validate_json(json_text)),
                content_type=content_type or "application/octet-stream",
                json=I.validate_json(json_text), content_len=len(content))


def sats(coins: float) -> int:
    return int(round(coins * COIN))


def piece_size(plan: "Plan") -> int:
    """What one output has to be worth to fund one piece of THIS inscription.

    The messenger splits into a fixed 2 coins per piece, which is right for a
    message chunk and marginally too small for an inscription one: a full Class
    B transaction here carries 128 data outputs at 0.01 each plus its fee, and
    a 2.0 piece came up four hundredths short. Sized from the plan instead, with
    a margin, so this cannot drift when either number changes.
    """
    return piece_size_for(plan.estimate)


def piece_size_for(est: Estimate) -> int:
    """`piece_size` from an estimate alone, for a file not yet read."""
    per_chunk = est.total / max(1, est.chunks)
    return sats(per_chunk * 1.25 + 0.5)


def prepare_wallet(sender: Any, address: str, plan: "Plan",
                   on_progress: Any = None) -> bool:
    """Give the address one confirmed output per piece, so they all go at once.

    Without this each piece waits for the one before it to confirm -- a block
    each, which for a megabyte is over two hours. With it the split confirms
    once and then everything goes in one pass.
    """
    return sender.ensure_outputs(address, plan.chunks, each_sats=piece_size(plan),
                                 on_progress=on_progress)

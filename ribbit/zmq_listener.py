"""ZMQ notifications from pepecoind.

Polling `getblockcount` works but wastes a round trip per interval and adds
latency. ZMQ lets the indexer block until something actually happens.

Pepecoin v1.1.0 publishes hashblock, hashtx, rawblock and rawtx
(verified on host: `pepecoind -help`). It does **not** publish `sequence`, which
is a Bitcoin Core 0.19+ feature -- so ZMQ cannot tell us a block was
*disconnected*. Reorg detection stays the ChainFollower's job, by comparing
previousblockhash. ZMQ is a wake-up, never a source of truth.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterator

import zmq

log = logging.getLogger(__name__)

# Topics pepecoind can publish. `sequence` is absent by design; see module docstring.
TOPIC_HASH_BLOCK = b"hashblock"
TOPIC_HASH_TX = b"hashtx"
TOPIC_RAW_BLOCK = b"rawblock"
TOPIC_RAW_TX = b"rawtx"


@dataclass(frozen=True)
class Notification:
    """One ZMQ message.

    `sequence` is pepecoind's per-topic counter. A gap means we missed messages --
    the high-water mark was hit, or we were too slow. That is recoverable (we
    re-sync over RPC) but worth noticing, so it is surfaced rather than hidden.
    """

    topic: bytes
    body: bytes
    sequence: int

    @property
    def hex_body(self) -> str:
        return self.body.hex()


class ZmqListener:
    """Subscribes to pepecoind's ZMQ publishers."""

    def __init__(
        self,
        endpoints: dict[bytes, str],
        rcvhwm: int = 10_000,
        connect_timeout_ms: int = 5_000,
    ):
        """`endpoints` maps topic -> tcp:// address, e.g. {TOPIC_HASH_BLOCK: "tcp://127.0.0.1:28332"}."""
        self.endpoints = endpoints
        self._context = zmq.Context.instance()
        self._socket = self._context.socket(zmq.SUB)
        self._socket.setsockopt(zmq.RCVHWM, rcvhwm)
        self._socket.setsockopt(zmq.RCVTIMEO, connect_timeout_ms)
        self._last_sequence: dict[bytes, int] = {}

        for topic, address in endpoints.items():
            self._socket.setsockopt(zmq.SUBSCRIBE, topic)
            self._socket.connect(address)

    def close(self) -> None:
        self._socket.close(linger=0)

    def __enter__(self) -> "ZmqListener":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def receive(self, timeout_ms: int | None = None) -> Notification | None:
        """Wait for one notification. Returns None on timeout."""
        if timeout_ms is not None:
            self._socket.setsockopt(zmq.RCVTIMEO, timeout_ms)
        try:
            topic, body, seq_bytes = self._socket.recv_multipart()
        except zmq.Again:
            return None

        sequence = int.from_bytes(seq_bytes, "little")
        previous = self._last_sequence.get(topic)
        if previous is not None and sequence != previous + 1:
            log.warning(
                "zmq: missed %d %s message(s) (seq %d -> %d); will re-sync over RPC",
                sequence - previous - 1, topic.decode(), previous, sequence,
            )
        self._last_sequence[topic] = sequence
        return Notification(topic=topic, body=body, sequence=sequence)

    def listen(self, timeout_ms: int = 1_000) -> Iterator[Notification]:
        """Yield notifications forever, skipping timeouts.

        The timeout exists so a caller can still act periodically -- checking for
        shutdown, or re-syncing after a missed message -- rather than blocking
        indefinitely on a quiet chain.
        """
        while True:
            notification = self.receive(timeout_ms)
            if notification is not None:
                yield notification

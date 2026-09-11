"""Finding messages on chain, by trial decryption.

Discovery is by **trial decryption**, not notification outputs
(docs/messaging/02-design.md §5). A dust output to the recipient would make
lookup an index query, but would permanently and publicly link sender to
recipient. Measured cost of the alternative: 227 us per attempt, 4,409/sec --
about 23 seconds for 100,000 messages, and scans are incremental.

Scanning is resumable and reorg-aware. Testnet reorgs are common and can be
deep; a scanner that did not unwind would keep messages from orphaned blocks
forever, which is worse than missing them because they look real.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import payload as P
from ..config import Params, require_messaging_network
from ..indexer import PrevOutCache
from ..rpc import RpcClient
from ..tx import TxError, extract
from .envelope import (
    EnvelopeError,
    Header,
    open_ciphertext,
    TYPE_CHUNK,
    TYPE_KEY_ANNOUNCE,
    TYPE_SINGLE,
    is_message_payload,
    open_message,
    parse_key_announcement,
)
from .keys import Identity, fingerprint_of
from .store import MessageStore

log = logging.getLogger(__name__)


@dataclass
class ScanResult:
    blocks: int = 0
    candidates: int = 0
    announcements: int = 0
    opened: int = 0
    reorg_depth: int = 0
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        parts = [f"{self.blocks} blocks"]
        if self.reorg_depth:
            parts.append(f"rewound {self.reorg_depth}")
        parts += [f"{self.candidates} candidates", f"{self.announcements} keys",
                  f"{self.opened} decrypted"]
        return ", ".join(parts)


class Scanner:
    """Walks the chain, collecting message payloads and key announcements."""

    def __init__(self, rpc: RpcClient, params: Params, store: MessageStore,
                 identity: Identity | None = None):
        require_messaging_network(params)
        self.rpc = rpc
        self.params = params
        self.store = store
        self.identity = identity
        self.prevouts = PrevOutCache(rpc, params)

    # --- reorg safety ---------------------------------------------------------

    def _resolve_fork(self, result: ScanResult) -> int:
        """Return the height to resume from, unwinding if our view is stale."""
        cursor = self.store.scan_cursor(self.params.name)
        if cursor is None:
            return max(self.params.activation_height or 0, 0)

        height, stored_hash = cursor
        try:
            current = self.rpc.get_block_hash(height)
        except Exception:
            current = None

        if current == stored_hash:
            return height + 1

        # Our cursor block is gone or replaced: walk back to the last agreement.
        probe = height
        while probe > (self.params.activation_height or 0):
            probe -= 1
            row = self.store.conn.execute(
                "SELECT height FROM candidate WHERE height<=? ORDER BY height DESC LIMIT 1",
                (probe,),
            ).fetchone()
            try:
                self.rpc.get_block_hash(probe)
            except Exception:
                continue
            break

        result.reorg_depth = height - probe
        self.store.rewind(self.params.name, probe + 1)
        log.warning("reorg: rewound messaging store to height %d", probe)
        return probe + 1

    # --- scanning -------------------------------------------------------------

    def scan(self, max_blocks: int = 2000, progress: Callable[[int, int], None] | None = None
             ) -> ScanResult:
        result = ScanResult()
        tip = self.rpc.get_block_count()
        start = self._resolve_fork(result)
        if start > tip:
            return result

        end = min(tip, start + max_blocks - 1)
        for height in range(start, end + 1):
            block_hash = self.rpc.get_block_hash(height)
            block = self.rpc.get_block(block_hash, 2)
            self._scan_block(height, block, result)
            self.store.set_scan_cursor(self.params.name, height, block_hash)
            result.blocks += 1
            if progress and height % 100 == 0:
                progress(height, end)

        if self.identity is not None:
            result.opened += self.open_pending()
        return result

    def _scan_block(self, height: int, block: dict[str, Any], result: ScanResult) -> None:
        self.prevouts.add_block(block)
        block_time = int(block.get("time", 0))

        for position, tx in enumerate(block.get("tx", [])):
            try:
                atx = extract(tx, height, position, self.params, self.prevouts.lookup)
            except TxError:
                continue                      # marked but unreadable; not ours to fix
            if atx is None:
                continue

            try:
                message = P.decode(atx.payload)
            except P.PayloadError:
                continue
            if not isinstance(message, P.AnyData):
                continue                      # some other Arcade message type

            body = message.data
            if not is_message_payload(body):
                continue

            try:
                header = Header.decode(body)
            except EnvelopeError:
                continue

            if header.type == TYPE_KEY_ANNOUNCE:
                try:
                    pubkey = parse_key_announcement(body)
                except EnvelopeError as exc:
                    result.errors.append(f"{atx.txid}: {exc}")
                    continue
                self.store.add_key_announcement(
                    atx.txid, atx.sender, pubkey, fingerprint_of(pubkey), height, block_time
                )
                result.announcements += 1
                continue

            if header.type in (TYPE_SINGLE, TYPE_CHUNK):
                self.store.add_candidate(
                    atx.txid, height, position, block_time, atx.sender, body,
                    header.type,
                    header.msg_id or None,
                    header.countdown if header.type == TYPE_CHUNK else None,
                )
                result.candidates += 1

    # --- trial decryption -----------------------------------------------------

    def open_pending(self) -> int:
        """Attempt to open every candidate we have not yet tried.

        A failure is the ordinary case -- almost every payload belongs to someone
        else -- so failures are silent. The one exception is a payload that opens
        at the sealed-box layer but fails inner authentication: that means it IS
        addressed to us and someone forged the sender, which is worth recording.
        """
        if self.identity is None:
            return 0

        opened = 0
        me = self.identity.fingerprint

        for row in self.store.unopened_candidates():
            body = bytes(row["payload"])
            try:
                sender_pk, plaintext, header = open_message(self.identity, body)
            except EnvelopeError as exc:
                if "forged sender" in str(exc):
                    log.warning("forged sender identity in %s", row["txid"])
                self.store.mark_opened(row["txid"])
                continue

            if header.type == TYPE_SINGLE:
                self.store.add_message(
                    None, row["txid"], row["txid"], row["height"], row["block_time"],
                    row["sender_addr"], sender_pk, me, plaintext, complete=True,
                )
                opened += 1
            else:
                opened += self._try_assemble(row, sender_pk, me)

            self.store.mark_opened(row["txid"])
        return opened

    def _try_assemble(self, row: Any, sender_pk: bytes, me: str) -> int:
        """Reassemble a chunked message if every link is present.

        Completion is self-describing: the final chunk carries countdown 0, and
        the chunks descend to it. An abandoned chain never reaches 0, so a partial
        message is always distinguishable from a complete one and is simply not
        surfaced.
        """
        msg_id = bytes(row["msg_id"]) if row["msg_id"] else None
        if msg_id is None:
            return 0

        chunks = self.store.chunks_for(msg_id)
        countdowns = [c["countdown"] for c in chunks]
        if 0 not in countdowns:
            return 0                                   # final chunk not seen yet
        expected = max(countdowns) + 1
        if len(set(countdowns)) != expected:
            return 0                                   # gaps remain

        ordered = sorted(chunks, key=lambda c: -c["countdown"])
        header = Header(type=TYPE_CHUNK, msg_id=msg_id)
        # Strip each chunk's own framing and rejoin the ciphertext it carried.
        joined = b"".join(bytes(c["payload"])[header.length :] for c in ordered)

        try:
            sender_pk2, plaintext = open_ciphertext(self.identity, header, joined)
        except EnvelopeError:
            return 0

        self.store.add_message(
            msg_id, ordered[0]["txid"], ordered[-1]["txid"], ordered[-1]["height"],
            ordered[-1]["block_time"], ordered[0]["sender_addr"], sender_pk2, me,
            plaintext, complete=True,
        )
        for chunk in chunks:
            self.store.mark_opened(chunk["txid"])
        return 1

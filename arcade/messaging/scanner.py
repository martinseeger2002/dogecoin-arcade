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
from ..config import MainnetRefused, Params, require_messaging_network
from ..indexer import PrevOutCache
from ..rpc import RpcClient
from ..tx import TxError, extract
from . import content, group
from ..script import b58check_encode
from .envelope import (
    EnvelopeError,
    Header,
    open_ciphertext,
    TYPE_CHUNK,
    TYPE_KEY_ANNOUNCE,
    TYPE_SINGLE,
    is_message_payload,
    open_message,
    parse_announced_identity,
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
    group_posts: int = 0
    reorg_depth: int = 0
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        parts = [f"{self.blocks} blocks"]
        if self.reorg_depth:
            parts.append(f"rewound {self.reorg_depth}")
        parts += [f"{self.candidates} candidates", f"{self.announcements} keys",
                  f"{self.opened} decrypted"]
        if self.group_posts:
            parts.append(f"{self.group_posts} public "
                         f"post{'' if self.group_posts == 1 else 's'}")
        return ", ".join(parts)


class Scanner:
    """Walks the chain, collecting message payloads and key announcements."""

    def __init__(self, rpc: RpcClient, params: Params, store: MessageStore,
                 identity: Identity | None = None, *, public_only: bool = False):
        """`public_only` mirrors the same flag on MessageSender (D-014).

        A scanner that only collects public group posts decrypts nothing and
        needs no identity, so it may run on mainnet. Without this the public
        board could post to mainnet and then never show anything, which is worse
        than not offering it.

        It is enforced, not merely declared: with the flag set, an identity is
        refused outright, so no code path can quietly start trial-decrypting
        mainnet traffic by passing one in.
        """
        if public_only:
            if identity is not None:
                raise MainnetRefused(
                    "a public-only scanner must not be given an identity: "
                    "decryption is exactly what it exists to avoid")
        else:
            require_messaging_network(params)
        self.public_only = public_only
        self.rpc = rpc
        self.params = params
        self.store = store
        self.identity = identity
        self.prevouts = PrevOutCache(rpc, params)

    # --- reorg safety ---------------------------------------------------------

    def start_height(self) -> int:
        """The earliest height worth scanning. A floor, not merely a default.

        Not block 0. A message can only be addressed to a key that existed when
        it was written, so nothing before an identity was created can possibly be
        for it. Starting at the identity's creation height is therefore both
        correct and the difference between scanning a few hundred blocks and 1.5
        million.

        It is applied as a floor on resume as well, which it was not before. A
        store carrying a cursor from the old scan-from-zero behaviour -- or a new
        identity in an old store -- would otherwise crawl up from the genesis
        block 5,000 at a time, finding nothing, for hundreds of scans. That is
        what it did: a cursor at height 24,999 against a tip of 1,482,779, with
        the user pressing "check for new messages" and being told, truthfully and
        uselessly, "5000 blocks, 0 candidates".

        Falls back to the activation height when the creation height is unknown
        -- an imported identity, say, whose messages may genuinely predate this
        installation.
        """
        recorded = self.store.get_meta(f"identity_height:{self.params.name}")
        if recorded is not None:
            return max(int(recorded), self.params.activation_height or 0)
        return max(self.params.activation_height or 0, 0)

    def _resolve_fork(self, result: ScanResult) -> int:
        """Return the height to resume from, unwinding if our view is stale."""
        floor = self.start_height()
        cursor = self.store.scan_cursor(self.params.name)
        if cursor is None:
            return floor

        height, stored_hash = cursor
        if height + 1 < floor:
            # The cursor is below anything that could concern us. Skipping
            # forward is safe precisely because nothing in the skipped range can
            # be addressed to this identity.
            log.info("skipping scan from %d to %d: identity did not exist before then",
                     height + 1, floor)
            return floor
        try:
            current = self.rpc.get_block_hash(height)
        except Exception:
            current = None

        if current == stored_hash:
            return max(height + 1, floor)

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
        return max(probe + 1, floor)

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

            # Public posts are read here and now. There is nothing to decrypt and
            # no identity required, which is the whole difference: a node with no
            # key at all still sees every group post on the chain.
            if group.is_group_chunk_payload(body):
                try:
                    msg_id, countdown, piece = group.parse_chunk(body)
                except EnvelopeError:
                    continue
                self.store.add_group_chunk(
                    self.params.name, msg_id, countdown, atx.txid, height,
                    block_time, atx.sender, piece)
                if self._assemble_group(msg_id, height, block_time):
                    result.group_posts += 1
                continue

            if group.is_group_payload(body):
                try:
                    post = group.parse(body)
                except EnvelopeError:
                    continue
                self.store.add_group_post(
                    self.params.name, post.channel, atx.txid, height, block_time,
                    atx.sender, post.nickname, post.text,
                    mine=(self.identity is not None
                          and atx.sender == self.store.get_meta(
                              f"identity_address:{self.params.name}")),
                    file_name=post.file_name, file_type=post.file_type,
                    file_data=post.file_data or None,
                )
                result.group_posts += 1
                continue

            try:
                header = Header.decode(body)
            except EnvelopeError:
                continue

            if self.public_only:
                continue          # nothing else here is ours to look at

            if header.type == TYPE_KEY_ANNOUNCE:
                try:
                    pubkey = parse_key_announcement(body)
                except EnvelopeError as exc:
                    result.errors.append(f"{atx.txid}: {exc}")
                    continue
                # A newer announcement says which address the key belongs to.
                # Prefer that over the transaction's sender, which is whichever
                # address funded it and therefore changes with coin selection.
                claimed_hash, claimed_name = parse_announced_identity(body)
                address = atx.sender
                if claimed_hash:
                    address = b58check_encode(self.params.pubkeyhash_version,
                                              claimed_hash)
                mine = (self.identity is not None
                        and pubkey == self.identity.public_bytes)
                if claimed_hash and not mine:
                    # The key said where it lives. That beats the address the
                    # address book inferred from an earlier transaction's
                    # inputs, which followed the coins -- so the book was
                    # showing, and handing out, a funding address.
                    self.store.set_contact_address(pubkey, address)
                if claimed_name and not mine:
                    # Unverified, and filling blanks only -- a name the user
                    # typed always wins over one a stranger asserted.
                    #
                    # Never for our own key: scanning your own announcement was
                    # adding you to your own address book, which is not a
                    # contact, and put your published name somewhere that reads
                    # like a stranger's claim about you.
                    self.store.apply_profile(pubkey, claimed_name, address, "")
                self.store.add_key_announcement(
                    atx.txid, address, pubkey, fingerprint_of(pubkey), height,
                    block_time, stated=bool(claimed_hash), name=claimed_name
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

            # Branch on the type BEFORE attempting to open. A single chunk holds
            # only part of a ciphertext, so open_message can never succeed on one
            # -- trying it first made reassembly unreachable.
            if row["msg_type"] == TYPE_CHUNK:
                opened += self._try_assemble(row, me)
                self.store.mark_opened(row["txid"])
                continue

            try:
                sender_pk, plaintext, header = open_message(self.identity, body)
            except EnvelopeError as exc:
                if "forged sender" in str(exc):
                    log.warning("forged sender identity in %s", row["txid"])
                self.store.mark_opened(row["txid"])
                continue

            self._store_message(
                None, row["txid"], row["txid"], row["height"], row["block_time"],
                row["sender_addr"], sender_pk, me, plaintext,
            )
            opened += 1
            self.store.mark_opened(row["txid"])
        return opened

    def _assemble_group(self, msg_id: bytes, height: int, block_time: int) -> bool:
        """Rejoin a chunked public post once every link has been seen.

        No decryption and no identity: a public post is plain, so completeness is
        the only question. The countdown makes that self-describing -- the last
        chunk carries zero, and the number of links is therefore known from it.
        """
        chunks = self.store.group_chunks(self.params.name, msg_id)
        seen = {row["countdown"] for row in chunks}

        # The highest countdown seen says how many links there are -- but only if
        # the FIRST link has arrived. A lone countdown-0 chunk satisfies every
        # naive completeness check ("highest is 0, so there is 1, and we have 1")
        # while being the *last* piece of a message whose start has not landed
        # yet. That is not a hypothetical: chunks arrive in whatever order the
        # scan reaches their blocks.
        #
        # Chunking is only ever used for two or more links, so a single chunk is
        # by definition incomplete, and the set must run contiguously from the
        # highest down to zero.
        if len(chunks) < 2:
            return False
        expected = max(seen) + 1
        if len(chunks) != expected or seen != set(range(expected)):
            return False                      # a gap; wait for the rest

        joined = b"".join(bytes(row["data"]) for row in chunks)
        try:
            post = group.parse(joined)
        except EnvelopeError:
            # Kept, not dropped. These chunks are the only copy held locally, and
            # a parse failure here is more likely to mean something is still
            # missing than that the data is bad. Discarding them was a real bug:
            # it destroyed each chunk as it arrived and the post could never
            # complete.
            return False

        first, last = chunks[0], chunks[-1]
        self.store.add_group_post(
            self.params.name, post.channel, first["txid"], last["height"],
            last["block_time"], first["sender"], post.nickname, post.text,
            mine=(first["sender"] == self.store.get_meta(
                f"identity_address:{self.params.name}")),
            file_name=post.file_name, file_type=post.file_type,
            file_data=post.file_data or None)
        self.store.drop_group_chunks(self.params.name, msg_id)
        return True

    def _store_message(self, msg_id, first_txid, last_txid, height, block_time,
                       sender_addr, sender_pk, me, plaintext) -> int:
        """Store a decrypted message, unpacking whatever the body carries.

        Both decryption paths -- a single transaction and a reassembled chain --
        arrive here, so an attachment or a profile cannot be handled on one and
        forgotten on the other.
        """
        parsed = content.parse(plaintext)

        # The profile goes in FIRST, before `add_message` infers an address from
        # the transaction. Order matters and getting it wrong was a real bug:
        # `add_message` fills a blank address with the *funding* address, which
        # changes with coin selection, and the declared profile then found the
        # field already occupied and lost. a test machine caught it -- the address book
        # showed the address my coins came from rather than the identity address
        # I had explicitly sent.
        #
        # Both are still "fill blanks only", so a name or address the user typed
        # themselves beats either.
        if parsed.profile is not None:
            # What a sender says about themselves. Unverified: anyone can claim
            # any name and any address, and the interface says so where shown.
            self.store.apply_profile(
                sender_pk, parsed.profile.name, parsed.profile.testnet_address,
                parsed.profile.mainnet_address)

        message_id = self.store.add_message(
            msg_id, first_txid, last_txid, height, block_time, sender_addr,
            sender_pk, me, parsed.text.encode() if not parsed.plain else plaintext,
            complete=True,
        )
        if parsed.attachment is not None and message_id:
            self.store.add_attachment(
                message_id, parsed.attachment.name,
                parsed.attachment.content_type, parsed.attachment.data)
        return message_id

    def _try_assemble(self, row: Any, me: str) -> int:
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
        # Strip each chunk's framing and take exactly the ciphertext it declares,
        # discarding any Class B padding beneath it.
        joined = b""
        for chunk in ordered:
            raw = bytes(chunk["payload"])
            try:
                chunk_header = Header.decode(raw)
            except EnvelopeError:
                return 0
            body = raw[chunk_header.length :]
            joined += body[: chunk_header.clen] if chunk_header.clen else body

        try:
            sender_pk, plaintext = open_ciphertext(self.identity, header, joined)
        except EnvelopeError:
            return 0      # not ours, or not yet complete

        self._store_message(
            msg_id, ordered[0]["txid"], ordered[-1]["txid"], ordered[-1]["height"],
            ordered[-1]["block_time"], ordered[0]["sender_addr"], sender_pk, me,
            plaintext,
        )
        for chunk in chunks:
            self.store.mark_opened(chunk["txid"])
        return 1

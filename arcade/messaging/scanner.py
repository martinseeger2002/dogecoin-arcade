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
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import payload as P
from ..config import MainnetRefused, Params, require_messaging_network
from ..indexer import PrevOutCache
from ..rpc import RpcClient
from ..tx import TxError, extract
from .. import release as releaselib
from .. import instance as instancelib
from . import content, feed, group
from ..script import b58check_encode
from .envelope import (
    EnvelopeError,
    Header,
    open_ciphertext,
    TYPE_API,
    TYPE_CHUNK,
    TYPE_KEY_ANNOUNCE,
    TYPE_RELEASE,
    TYPE_INSTANCE,
    TYPE_SINGLE,
    is_message_payload,
    open_message,
    parse_announced_extras,
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
    feed_acts: int = 0
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
        if self.feed_acts:
            parts.append(f"{self.feed_acts} "
                         f"reaction{'' if self.feed_acts == 1 else 's'}")
        return ", ".join(parts)


class Scanner:
    """Walks the chain, collecting message payloads and key announcements."""

    #: Mempool transactions already read. A pool is looked at every few
    #: seconds and mostly does not change; without this the scanner would
    #: fetch every transaction in it, every time.
    _pool_seen: set

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
        self._pool_seen = set()

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
        # The protocol's shared start comes first. Every installation on this
        # version begins here, so two people running the same release see the
        # same history -- previously each began at whatever height its own
        # identity happened to be created, and neither could tell why the other
        # had seen a post they had not.
        floor = max(self.params.messaging_start_height,
                    self.params.activation_height or 0)

        # A local record can only push the start LATER, never earlier: nothing
        # written before a key existed can be addressed to it, so there is no
        # point reading it. It cannot drag the start below the shared floor.
        recorded = self.store.get_meta(f"identity_height:{self.params.name}")
        if recorded is not None:
            return max(int(recorded), floor)
        return floor

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

    def scan_mempool(self, limit: int = 200) -> ScanResult:
        """Read what is in the mempool and not yet in a block.

        A message costs a block to arrive, and a swap costs several: the
        order, the offer, the signed half. Watching the mempool turns that
        into seconds, because a message is carriage -- the thing that
        settles is the swap transaction itself, and nothing here touches a
        balance or the ledger (D-050).

        Rows land with height 0, which means "seen, not yet in a block". The
        block promotes them in place; a transaction that is dropped instead
        leaves a row that says plainly it never confirmed.
        """
        result = ScanResult()
        try:
            pool = self.rpc.call("getrawmempool") or []
        except Exception:
            return result
        fresh = [txid for txid in pool if txid not in self._pool_seen][:limit]
        if not fresh:
            return result
        transactions = []
        for txid in fresh:
            try:
                transactions.append(self.rpc.call("getrawtransaction", txid, 1))
            except Exception:
                continue          # gone from the pool between the two calls
        self._pool_seen.update(fresh)
        if len(self._pool_seen) > 5000:
            self._pool_seen = set(pool)
        block = {"time": int(time.time()), "tx": transactions}
        self._scan_block(0, block, result)
        if self.identity is not None:
            result.opened += self.open_pending()
        return result

    def _scan_block(self, height: int, block: dict[str, Any], result: ScanResult) -> None:
        self.prevouts.add_block(block)
        block_time = int(block.get("time", 0))

        for position, tx in enumerate(block.get("tx", [])):
            try:
                atx = extract(tx, height, position, self.params, self.prevouts.lookup)
            except TxError as why:
                # Marked as ours and unreadable, so there is nothing to store --
                # but it is not silent any more. This is the one case where the
                # chain holds something that looks like a message and this node
                # will never show it to anybody, and the only person who can do
                # anything about it is the operator of the node that sent it.
                # Silence here is how a node that funds itself by mining ended
                # up certain its messages had gone out fine (D-171).
                log.warning("block %s: %s carries an Arcade marker but cannot be "
                            "read: %s", height or "mempool",
                            tx.get("txid", "?"), why)
                continue
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

            # One node telling every other that a version exists. Machine
            # talk: nothing draws it, the watcher acts on it, and only the
            # newest is kept -- a notice is a nudge, not a record (D-147).
            if len(body) >= 6 and body[5] == TYPE_RELEASE:
                said = releaselib.notice_in(body)
                if said:
                    self.store.set_meta("release_notice", json.dumps(
                        {"revision": said, "from": atx.sender,
                         "height": height, "at": int(time.time())}))
                    result.announcements += 1
                continue

            # An arcade saying who runs it: its domain and revision, paid for
            # by its fee address -- which is what `atx.sender` is, and why no
            # other signature is needed (arcade/instance.py). Public; every
            # node indexes every one, so the directory needs no registry.
            if len(body) >= 6 and body[5] == TYPE_INSTANCE:
                said = instancelib.parse(body)
                if said and atx.sender:
                    self.store.add_instance_announcement(
                        atx.txid, self.params.name, atx.sender, said["domain"],
                        said["revision"], height, block_time)
                    result.announcements += 1
                continue

            # What somebody did to a post: public and unsealed, exactly like
            # a post, and read by any node whether or not it has an identity
            # (feed.py, D-138). Read before posts because the check is a
            # handful of bytes and posts are the common case either way.
            if feed.is_feed_act(body):
                if height == 0:
                    # A mempool pass. Reactions are NOT written down from the
                    # pool, unlike messages and posts: a like is a COUNT, and
                    # a row written at height 0 for a transaction that is
                    # then dropped counts for ever. They are read fresh
                    # instead, every draw, by `mempool.read` -- which is also
                    # what makes them disappear the moment the pool does
                    # (D-141, D-144).
                    continue
                try:
                    act = feed.parse(body)
                except EnvelopeError:
                    continue              # a kind this version does not know
                # A tip's payload says WHICH post and not HOW MUCH, and the
                # amount is the whole reason the feed can weigh one tip over
                # another (2026-09-23). The transaction is the only
                # honest source: what it gave to addresses other than the
                # sender's own is what it tipped -- the sender's change is the
                # sender's, and an OP_RETURN pays nobody.
                amount = 0
                if act.kind == feed.TIP:
                    amount = sum(v for where, v in atx.outputs
                                 if where and where != atx.sender)
                self.store.add_feed_act(
                    self.params.name, atx.txid, act.kind, act.target_hex,
                    atx.sender, act.text, height, block_time,
                    mine=(atx.sender == self.store.get_meta(
                        f"identity_address:{self.params.name}")),
                    amount=amount, paid_on=self.params.name)
                result.feed_acts += 1
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
                extras = parse_announced_extras(body)
                claimed_hash, claimed_name = extras["hash160"], extras["name"]
                claimed_tag = extras["tag"]
                other_address = ""
                if extras["other_hash160"]:
                    # The same person on the chain this node does NOT message
                    # on: encoded with that chain's version byte, not this
                    # one's, or the address book would hand out a testnet
                    # address for a mainnet payment (D-032).
                    other = self.params.other_pubkeyhash_version
                    if other is not None:
                        other_address = b58check_encode(other, extras["other_hash160"])
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
                    # From an announcement, so capped at 12 bytes and quite
                    # possibly a truncation of their real name.
                    self.store.apply_profile(pubkey, claimed_name, address, "",
                                             source="announce")
                self.store.add_key_announcement(
                    atx.txid, address, pubkey, fingerprint_of(pubkey), height,
                    block_time, stated=bool(claimed_hash), name=claimed_name,
                    tag=claimed_tag, other_address=other_address,
                    pfp=extras.get("pfp", ""), bio=extras.get("bio", ""),
                    url=extras.get("url", ""),
                )
                result.announcements += 1
                continue

            if header.type in (TYPE_SINGLE, TYPE_CHUNK, TYPE_API):
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

            if header.type == TYPE_API:
                # Addressed to a program, not to a person. It never reaches the
                # conversation: a chat full of machine chatter the reader cannot
                # act on is worse than no chat at all.
                from .api import read_stamp
                protocol, fingerprint, body = read_stamp(plaintext)
                self.store.add_api_message(
                    self.params.name, row["txid"], row["height"],
                    row["block_time"], row["sender_addr"], sender_pk, me,
                    body, protocol=protocol, fingerprint=fingerprint)
            else:
                self._store_message(
                    None, row["txid"], row["txid"], row["height"],
                    row["block_time"], row["sender_addr"], sender_pk, me,
                    plaintext,
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
        all_chunks = self.store.group_chunks(self.params.name, msg_id)
        # Grouped by sender for the same reason sealed chunks are: a message id
        # is in the clear on the chain, so anyone can publish a chunk claiming
        # one, and a single injected chunk would otherwise make the real post
        # look permanently incomplete.
        by_sender: dict[str, list] = {}
        for row in all_chunks:
            by_sender.setdefault(row["sender"], []).append(row)
        for chunks in by_sender.values():
            if self._assemble_group_from(msg_id, chunks):
                return True
        return False

    def _assemble_group_from(self, msg_id: bytes, chunks: list) -> bool:
        """Try one sender's chunks for a public post."""
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
        """Reassemble a chunked message if every link from one sender is present.

        Completion is self-describing: the final chunk carries countdown 0 and
        the chunks descend to it, so an abandoned chain never reaches 0 and a
        partial message is simply not surfaced.

        Chunks are grouped by SENDER before anything is joined. A message id is
        eight bytes in a cleartext header, in plain view on the chain, so anybody
        who sees one can publish a chunk claiming it -- and a single injected
        chunk with an unused countdown makes the real message look permanently
        incomplete. Grouping means such a chunk forms its own group, which simply
        fails to decrypt, instead of poisoning the real one.

        Note what this replaces. The envelope documentation claimed ordering was
        "enforced structurally by the UTXO chain". It was not: this function
        never looked at the chain, and collected by message id alone. Chunked
        sends happened to chain through change outputs, but nothing verified it,
        so the guarantee was asserted rather than enforced. Reassembly does not
        need the chain -- only the countdown and, now, agreement on the sender.
        """
        msg_id = bytes(row["msg_id"]) if row["msg_id"] else None
        if msg_id is None:
            return 0

        by_sender: dict[str, list] = {}
        for chunk in self.store.chunks_for(msg_id):
            by_sender.setdefault(chunk["sender_addr"], []).append(chunk)

        for chunks in by_sender.values():
            if self._assemble_one(msg_id, chunks, me):
                return 1
        return 0

    def _assemble_one(self, msg_id: bytes, chunks: list, me: str) -> bool:
        """Try to open one sender's chunks for a message id."""
        countdowns = [c["countdown"] for c in chunks]
        if 0 not in countdowns:
            return False                               # final chunk not seen yet
        expected = max(countdowns) + 1
        if len(set(countdowns)) != expected:
            return False                               # gaps remain

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
                return False
            body = raw[chunk_header.length :]
            joined += body[: chunk_header.clen] if chunk_header.clen else body

        try:
            sender_pk, plaintext = open_ciphertext(self.identity, header, joined)
        except EnvelopeError:
            return False      # not ours, not complete, or not really one message

        self._store_message(
            msg_id, ordered[0]["txid"], ordered[-1]["txid"], ordered[-1]["height"],
            ordered[-1]["block_time"], ordered[0]["sender_addr"], sender_pk, me,
            plaintext,
        )
        for chunk in chunks:
            self.store.mark_opened(chunk["txid"])
        return True


#: Bumped when a parsing fix means stored rows are worth re-reading. Recorded in
#: the store so the repair runs once rather than on every launch.
REPAIR_GENERATION = "1"


def repair_announcement_names(rpc, params, store) -> int:
    """Re-read stored announcements and correct names an older parser cut.

    Why this is needed at all, rather than a rescan: the fix for truncated names
    reaches only announcements read AFTER it. A reader who scanned earlier holds
    the cut version, their scan cursor is long past that block, and nothing will
    ever take them back to it -- so "Big Chief Energy" was on the chain in full,
    parseable in full, and shown as "Big Chief En" for ever. a test machine measured
    exactly that, including a rewound rescan that re-read the announcement and
    still could not replace the stored row.

    Most people will never rewind a cursor, so the repair has to come to them.
    The txid of every announcement is already stored, and `getrawtransaction`
    serves any transaction with txindex, so each one can be fetched and re-parsed
    directly without touching the cursor.

    Runs once per repair generation and reports how many rows it corrected.
    """
    if store.get_meta("announcement_repair") == REPAIR_GENERATION:
        return 0

    from ..indexer import PrevOutCache
    from ..payload import PayloadError, decode as decode_payload
    from .envelope import parse_announced_identity, parse_key_announcement

    rows = list(store.conn.execute(
        "SELECT txid, name, pubkey, address FROM key_announcement"))
    cache = PrevOutCache(rpc, params)
    repaired = 0
    for row in rows:
        try:
            raw = rpc.call("getrawtransaction", row["txid"], 1)
            atx = extract(raw, 0, 0, params, cache.lookup)
            if atx is None:
                continue
            message = decode_payload(atx.payload)
            parse_key_announcement(message.data)          # confirms the shape
            _, name = parse_announced_identity(message.data)
        except (TxError, EnvelopeError, PayloadError, Exception):
            continue
        if name and len(name) > len(row["name"] or ""):
            store.conn.execute(
                "UPDATE key_announcement SET name=? WHERE txid=?",
                (name, row["txid"]))
            # And carry it into the address book, which is where anybody
            # actually reads a name. Repairing only the announcement row would
            # leave the cut version on screen.
            store.apply_profile(bytes(row["pubkey"]), name, row["address"], "",
                                source="announce")
            repaired += 1
            log.info("repaired announcement name for %s: %r", row["txid"][:12], name)

    store.set_meta("announcement_repair", REPAIR_GENERATION)
    return repaired


#: Separate from the name repair: this one is about a row's height, not about
#: how its payload was parsed, and it must run on a store whose names are
#: already right.
HEIGHT_REPAIR_GENERATION = "1"


def repair_announcement_heights(rpc, params, store) -> int:
    """Move stored mempool announcements into the block that mined them.

    The same failure as the truncated names, one field over. The promotion into
    a block went into the insert statement, so it reaches announcements read
    after that fix and not the ones read before it: a row written from the
    mempool keeps its height 0 for ever, because the scan cursor is long past
    its block and nothing else takes it back. Every page that asks "is this key
    on the chain" then answers no about a key the chain has carried for months
    -- which is what a reader ends up being told about somebody they can in
    fact write to.

    The txid is stored, `getrawtransaction` serves any transaction with txindex,
    and a transaction that is in a block says which one. A row still genuinely
    sitting in the pool has no blockhash and is left at 0, which is the honest
    answer for it.

    Runs once per repair generation and reports how many rows it corrected.
    """
    if store.get_meta("announcement_height_repair") == HEIGHT_REPAIR_GENERATION:
        return 0

    rows = list(store.conn.execute(
        "SELECT txid FROM key_announcement WHERE height = 0"))
    repaired = 0
    for row in rows:
        try:
            raw = rpc.call("getrawtransaction", row["txid"], True)
            if not raw.get("blockhash"):
                continue                    # genuinely still in the pool
            block = rpc.call("getblock", raw["blockhash"])
            height = int(block["height"])
        except Exception:
            continue                        # no txindex, or a dead transaction
        if height <= 0:
            continue
        store.conn.execute(
            "UPDATE key_announcement SET height=?, block_time=? WHERE txid=?",
            (height, int(block.get("time") or 0), row["txid"]))
        repaired += 1
        log.info("found announcement %s in block %d", row["txid"][:12], height)

    store.set_meta("announcement_height_repair", HEIGHT_REPAIR_GENERATION)
    return repaired


def find_own_announcements(rpc, params, pubkey: bytes, limit: int = 400):
    """Look on the CHAIN for an announcement of `pubkey` we already published.

    Returns every match as (txid, address, height, block_time, stated, name),
    oldest first, or an empty list.

    ALL of them, not just one: recovering only the earliest puts back the
    12-byte truncation ("Big Chief En") and leaves the full name that replaced
    it missing, which is the bug the name ranking exists to settle. Handing the
    store every announcement lets that ranking pick, exactly as a scan would.

    This exists because the Keys page decided whether to offer "Publish on
    chain" purely from the local store, and the store can be wrong in the one
    direction that costs money. A reset that dropped the announcement rows left
    the page saying "None seen yet" and inviting a second publication of
    something already permanent -- and because the rows sat below the new
    starting block, no rescan on this version could ever put them back.

    Our own announcement is always in our own wallet's history, whatever the
    scanner's floor says, so `listtransactions` finds it without touching the
    scan cursor and without reading anything that is not ours.
    """
    from ..indexer import PrevOutCache
    from ..tx import extract
    from .envelope import parse_key_announcement, parse_announced_identity

    try:
        entries = rpc.call("listtransactions", "*", limit, 0, True) or []
    except Exception:
        return None

    # The scanner's own resolver, rather than a hand-rolled one: sender
    # determination needs a PrevOut with a parsed output TYPE, not just an
    # address and a value, and reimplementing that got it wrong on the first
    # attempt.
    prevouts = PrevOutCache(rpc, params)
    seen: set[str] = set()
    found: list[tuple] = []
    for entry in reversed(entries):          # newest first
        txid = entry.get("txid")
        if not txid or txid in seen:
            continue
        seen.add(txid)
        try:
            raw = rpc.call("getrawtransaction", txid, True)
            height = 0
            if raw.get("blockhash"):
                height = int(rpc.call("getblock", raw["blockhash"])["height"])
            atx = extract(raw, height, 0, params, prevouts.lookup)
            if atx is None:
                continue
            # The payload is AnyData-wrapped on the wire; decoding the header
            # straight off it fails with "bad magic". Same unwrap the block
            # scanner does, for the same reason.
            message = P.decode(atx.payload)
            if not isinstance(message, P.AnyData):
                continue
            body = message.data
            header = Header.decode(body)
            if header.type != TYPE_KEY_ANNOUNCE:
                continue
            if parse_key_announcement(body) != pubkey:
                continue
        except Exception:
            continue
        claimed_hash, claimed_name = parse_announced_identity(body)
        address = atx.sender
        if claimed_hash:
            address = b58check_encode(params.pubkeyhash_version, claimed_hash)
        found.append((txid, address, height, int(raw.get("blocktime") or 0),
                      bool(claimed_hash), claimed_name))
    found.sort(key=lambda row: (row[2] or 1 << 62))
    return found

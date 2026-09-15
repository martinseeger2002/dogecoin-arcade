"""Encryption, key storage, and chunk integrity.

These are the unit tests the brief asks for in Step 4, written alongside the
implementation rather than after it.
"""

import pytest

from arcade.messaging.envelope import (
    EnvelopeError,
    Header,
    TYPE_CHUNK,
    TYPE_KEY_ANNOUNCE,
    TYPE_SINGLE,
    build_key_announcement,
    is_message_payload,
    open_ciphertext,
    open_message,
    seal_ciphertext,
    parse_key_announcement,
    seal_message,
)
from arcade.messaging.keys import (
    Identity,
    KeyError_,
    decrypt_identity,
    encrypt_identity,
    fingerprint_of,
    load_identity,
    save_identity,
)
import nacl.bindings as sodium

# Cheapest Argon2id settings: these tests exercise correctness, not cost, and
# MODERATE would add ~1.3 s per call.
FAST = dict(
    ops=sodium.crypto_pwhash_argon2id_OPSLIMIT_MIN,
    mem=sodium.crypto_pwhash_argon2id_MEMLIMIT_MIN,
)


@pytest.fixture
def alice():
    return Identity.generate()


@pytest.fixture
def bob():
    return Identity.generate()


# --- round trip ---------------------------------------------------------------


@pytest.mark.parametrize("size", [1, 10, 100, 1000, 7000])
def test_encrypt_decrypt_round_trip(alice, bob, size):
    message = bytes(range(256)) * (size // 256 + 1)
    message = message[:size]
    payload = seal_message(alice, bob.public_bytes, Header(type=TYPE_SINGLE), message)
    sender, got, header = open_message(bob, payload)
    assert got == message
    assert sender == alice.public_bytes
    assert header.type == TYPE_SINGLE


def test_overhead_is_134_bytes(alice, bob):
    """The figure the whole sizing model rests on.

    134 = 48 sealed box + 32 sender key + 40 crypto_box + 6 authenticated header
    copy + 8 cleartext header (which now carries the 2-byte ciphertext length).
    """
    payload = seal_message(alice, bob.public_bytes, Header(type=TYPE_SINGLE), b"x" * 100)
    assert len(payload) - 100 == 134


def test_binary_content_survives(alice, bob):
    blob = bytes(range(256)) + b"\x00" * 40 + b"\xff" * 10
    payload = seal_message(alice, bob.public_bytes, Header(type=TYPE_SINGLE), blob)
    _, got, _ = open_message(bob, payload)
    assert got == blob, "trailing NULs must survive; they are real content"


def test_two_sealings_of_the_same_message_differ(alice, bob):
    """Fresh ephemeral key and nonce per call: no deterministic ciphertext."""
    a = seal_message(alice, bob.public_bytes, Header(type=TYPE_SINGLE), b"same")
    b = seal_message(alice, bob.public_bytes, Header(type=TYPE_SINGLE), b"same")
    assert a != b


# --- failure modes ------------------------------------------------------------


def test_wrong_recipient_cannot_decrypt(alice, bob):
    mallory = Identity.generate()
    payload = seal_message(alice, bob.public_bytes, Header(type=TYPE_SINGLE), b"secret")
    with pytest.raises(EnvelopeError, match="not addressed to us"):
        open_message(mallory, payload)


@pytest.mark.parametrize("offset", [10, 50, 100, -1])
def test_flipped_ciphertext_bit_is_rejected(alice, bob, offset):
    payload = bytearray(seal_message(alice, bob.public_bytes, Header(type=TYPE_SINGLE), b"x" * 80))
    payload[offset] ^= 0x01
    with pytest.raises(EnvelopeError):
        open_message(bob, bytes(payload))


def test_tampered_header_is_rejected(alice, bob):
    """The cleartext header is readable before decryption, so it must be bound."""
    payload = seal_message(alice, bob.public_bytes, Header(type=TYPE_SINGLE), b"hello")
    tampered = Header(type=TYPE_CHUNK, msg_id=b"12345678").encode() + payload[6:]
    with pytest.raises(EnvelopeError):
        open_message(bob, tampered)


def test_forged_sender_identity_is_rejected(alice, bob):
    """Claiming someone else's key must fail, or authentication means nothing."""
    import nacl.public

    mallory = Identity.generate()
    head = Header(type=TYPE_SINGLE).encode()
    inner = nacl.public.Box(mallory.secret, nacl.public.PublicKey(bob.public_bytes)).encrypt(
        head + b"pretending to be alice"
    )
    forged = head + nacl.public.SealedBox(nacl.public.PublicKey(bob.public_bytes)).encrypt(
        alice.public_bytes + bytes(inner)       # claims alice, sealed by mallory
    )
    with pytest.raises(EnvelopeError, match="forged sender"):
        open_message(bob, forged)


def test_truncated_payload_is_rejected(alice, bob):
    payload = seal_message(alice, bob.public_bytes, Header(type=TYPE_SINGLE), b"x" * 50)
    with pytest.raises(EnvelopeError):
        open_message(bob, payload[:-10])


def test_foreign_payload_is_not_mistaken_for_ours():
    assert not is_message_payload(b"ord" + b"\x01\x02")
    assert not is_message_payload(b"")
    assert is_message_payload(b"arcm" + b"\x01\x01\x00\x10")


def test_unknown_version_is_rejected():
    with pytest.raises(EnvelopeError, match="version"):
        Header.decode(b"arcm" + bytes([99, TYPE_SINGLE]))


# --- chunk integrity ----------------------------------------------------------


def make_chunks(alice, bob, message, capacity):
    """Mirror what sender.plan_message does: seal once, then split the ciphertext."""
    msg_id = b"\x01" * 8
    header = Header(type=TYPE_CHUNK, msg_id=msg_id)
    body = seal_ciphertext(alice, bob.public_bytes, header, message)
    pieces = [body[i : i + capacity] for i in range(0, len(body), capacity)]
    total = len(pieces)
    return msg_id, [
        (Header(type=TYPE_CHUNK, msg_id=msg_id, countdown=total - i - 1), piece)
        for i, piece in enumerate(pieces)
    ]


def reassemble(chunks):
    return b"".join(piece for _, piece in chunks)


def open_chunks(bob, msg_id, chunks):
    return open_ciphertext(bob, Header(type=TYPE_CHUNK, msg_id=msg_id), reassemble(chunks))


def test_chunks_reassemble_in_order(alice, bob):
    msg = b"the quick brown fox jumps over the lazy dog " * 20
    msg_id, chunks = make_chunks(alice, bob, msg, 200)
    assert len(chunks) > 1
    _, got = open_chunks(bob, msg_id, chunks)
    assert got == msg


def test_final_chunk_carries_countdown_zero(alice, bob):
    _, chunks = make_chunks(alice, bob, b"y" * 900, 200)
    assert chunks[-1][0].countdown == 0, "completion must be self-describing"
    assert [c[0].countdown for c in chunks] == list(range(len(chunks) - 1, -1, -1))


def test_missing_chunk_is_detected(alice, bob):
    msg_id, chunks = make_chunks(alice, bob, b"z" * 900, 200)
    countdowns = [c[0].countdown for c in chunks]
    del chunks[1]
    remaining = [c[0].countdown for c in chunks]
    assert 0 in remaining, "the final chunk is still present"
    assert len(set(remaining)) != max(remaining) + 1, "the gap must be detectable by countdown"
    with pytest.raises(EnvelopeError):
        open_chunks(bob, msg_id, chunks)


def test_reordered_chunks_are_detected(alice, bob):
    msg_id, chunks = make_chunks(alice, bob, b"w" * 900, 200)
    swapped = [chunks[1], chunks[0]] + chunks[2:]
    with pytest.raises(EnvelopeError):
        open_chunks(bob, msg_id, swapped)


def test_duplicated_chunk_is_detected(alice, bob):
    msg_id, chunks = make_chunks(alice, bob, b"v" * 900, 200)
    duped = [chunks[0], chunks[0]] + chunks[1:]
    with pytest.raises(EnvelopeError):
        open_chunks(bob, msg_id, duped)


# --- key announcements --------------------------------------------------------


def test_key_announcement_round_trip(alice):
    blob = build_key_announcement(alice.public_bytes)
    assert len(blob) == 38, "must fit Class C's 72-byte capacity"
    assert parse_key_announcement(blob) == alice.public_bytes


def test_key_announcement_rejects_a_truncated_payload(alice):
    blob = build_key_announcement(alice.public_bytes)
    with pytest.raises(EnvelopeError):
        parse_key_announcement(blob[:-4])


def test_key_announcement_tolerates_class_b_padding(alice):
    """An announcement carried by Class B arrives NUL-padded to a 30-byte boundary."""
    blob = build_key_announcement(alice.public_bytes)
    assert parse_key_announcement(blob + b"\x00" * 22) == alice.public_bytes


def test_fingerprint_is_stable_and_formatted(alice):
    fp = alice.fingerprint
    assert fp == fingerprint_of(alice.public_bytes)
    assert len(fp.replace(" ", "")) == 16
    assert fp.count(" ") == 3


def test_different_keys_give_different_fingerprints(alice, bob):
    assert alice.fingerprint != bob.fingerprint


# --- key file at rest ---------------------------------------------------------


def test_key_file_round_trip(alice):
    blob = encrypt_identity(alice, "a decent passphrase", **FAST)
    assert len(blob) == 102
    assert decrypt_identity(blob, "a decent passphrase").public_bytes == alice.public_bytes


def test_secret_key_never_appears_in_the_file(alice):
    blob = encrypt_identity(alice, "passphrase", **FAST)
    assert bytes(alice.secret) not in blob, "the secret key must not be stored in the clear"


def test_wrong_passphrase_fails(alice):
    blob = encrypt_identity(alice, "right", **FAST)
    with pytest.raises(KeyError_, match="wrong passphrase"):
        decrypt_identity(blob, "wrong")


def test_corrupt_key_file_is_indistinguishable_from_a_wrong_passphrase(alice):
    """The error text must not work as an oracle."""
    blob = bytearray(encrypt_identity(alice, "right", **FAST))
    blob[60] ^= 0xFF
    with pytest.raises(KeyError_, match="wrong passphrase, or the key file is corrupt"):
        decrypt_identity(bytes(blob), "right")


def test_kdf_parameters_are_stored_not_assumed(alice):
    """Raising the cost later must not orphan existing key files."""
    blob = encrypt_identity(alice, "pw", **FAST)
    assert int.from_bytes(blob[6:10], "big") == FAST["ops"]
    assert int.from_bytes(blob[10:14], "big") == FAST["mem"]
    assert decrypt_identity(blob, "pw").public_bytes == alice.public_bytes


def test_empty_passphrase_is_refused(alice):
    with pytest.raises(KeyError_, match="empty passphrase"):
        encrypt_identity(alice, "", **FAST)


def test_saved_key_file_is_0600(tmp_path, alice):
    path = tmp_path / "id.key"
    save_identity(path, alice, "pw", **FAST)
    assert oct(path.stat().st_mode)[-3:] == "600"
    assert load_identity(path, "pw").public_bytes == alice.public_bytes


def test_save_refuses_to_overwrite(tmp_path, alice):
    path = tmp_path / "id.key"
    save_identity(path, alice, "pw", **FAST)
    with pytest.raises(KeyError_, match="already exists"):
        save_identity(path, Identity.generate(), "pw", **FAST)


def test_identity_repr_hides_the_secret(alice):
    assert "redacted" in repr(alice)
    assert bytes(alice.secret).hex() not in repr(alice)


def test_class_b_padding_is_discarded(alice, bob):
    """Class B pads to a 30-byte boundary; the declared length must survive it.

    This is the bug the integration tests caught: without an explicit ciphertext
    length the sealed box sees trailing NULs it never wrote and rejects the whole
    message. Stripping trailing NULs instead would be worse -- ciphertext ends in
    NUL roughly one time in 256.
    """
    payload = seal_message(alice, bob.public_bytes, Header(type=TYPE_SINGLE), b"padded")
    for pad in (0, 1, 3, 17, 29):
        _, got, _ = open_message(bob, payload + b"\x00" * pad)
        assert got == b"padded", f"failed with {pad} bytes of padding"


def test_lying_about_the_length_is_rejected(alice, bob):
    payload = bytearray(seal_message(alice, bob.public_bytes, Header(type=TYPE_SINGLE), b"x" * 50))
    payload[6:8] = (9999).to_bytes(2, "big")      # claim far more than is present
    with pytest.raises(EnvelopeError, match="claims"):
        open_message(bob, bytes(payload))


# --- the real chunking threshold ----------------------------------------------
# The chunk tests above pass an explicit small capacity, so they exercise the
# machinery but say nothing about where `plan_message` actually divides. a test machine
# nearly ran a cross-machine chunk test against a size that does not chunk at
# all, which would have reported the chunk path as working without ever entering
# it. These pin the boundary itself.


def test_plan_message_does_not_chunk_just_below_the_limit(alice, bob):
    from arcade.encoding import MAX_CLASS_B_PAYLOAD
    from arcade.messaging.sender import plan_message

    ceiling = MAX_CLASS_B_PAYLOAD - 4 - 134        # AnyData header + sealed overhead
    plan = plan_message(alice, bob.public_bytes, b"x" * ceiling)
    assert plan.transactions == 1


def test_plan_message_chunks_just_above_the_limit(alice, bob):
    from arcade.encoding import MAX_CLASS_B_PAYLOAD
    from arcade.messaging.sender import plan_message

    ceiling = MAX_CLASS_B_PAYLOAD - 4 - 134
    plan = plan_message(alice, bob.public_bytes, b"x" * (ceiling + 1))
    assert plan.transactions > 1, (
        "one byte over the ceiling must divide; a test that sends less than this "
        "is not testing chunking"
    )


def test_the_documented_ceiling_is_the_real_one(alice, bob):
    """7,512 is what the compose box and the design document both promise."""
    from arcade.messaging.sender import plan_message

    assert plan_message(alice, bob.public_bytes, b"x" * 7512).transactions == 1
    assert plan_message(alice, bob.public_bytes, b"x" * 7513).transactions > 1


# --- chunked sends survive an interruption ------------------------------------
# a test machine broadcast chunk 1 of 2, then chunk 2 failed to build, and the result was
# 0.148 PEP spent on a transaction that can never be read by anyone: a partial
# message is permanently unreadable, and nothing recorded enough to finish it.
# Re-sending is not a fix -- re-sealing produces a different message id, so a
# fresh attempt strands the first one rather than completing it.


def test_a_chunked_plan_carries_its_message_id(alice, bob):
    """Without it, nothing can identify the chain to resume."""
    from arcade.messaging.sender import plan_message

    plan = plan_message(alice, bob.public_bytes, b"x" * 9000)
    assert plan.chunked and plan.msg_id


def test_a_single_transaction_plan_has_no_message_id(alice, bob):
    from arcade.messaging.sender import plan_message

    assert plan_message(alice, bob.public_bytes, b"short").msg_id is None


def test_chunks_are_split_evenly_rather_than_greedily(alice, bob):
    """Filling each chunk in turn made the first transaction the largest possible.

    a test machine measured 14,783 bytes then 796. The first is the one most likely to meet
    a relay or mempool limit, and dust tracks the total payload rather than the
    transaction count, so evening the split costs nothing.
    """
    from arcade.messaging.sender import plan_message

    sizes = [len(c) for c in plan_message(alice, bob.public_bytes, b"x" * 7751).chunk_payloads]
    assert len(sizes) == 2
    assert max(sizes) - min(sizes) <= 1, sizes


def test_an_interrupted_send_can_be_resumed_with_the_original_chunks(alice, bob):
    """The sealed chunks must come back byte-identical, or the chain is dead."""
    import tempfile
    from pathlib import Path
    from arcade.messaging.sender import plan_message
    from arcade.messaging.store import MessageStore

    plan = plan_message(alice, bob.public_bytes, b"x" * 9000)
    with tempfile.TemporaryDirectory() as directory:
        store = MessageStore(Path(directory) / "m.sqlite")
        store.begin_pending_send(plan.msg_id, bob.public_bytes, "nSender",
                                 b"x" * 9000, plan.chunk_payloads)
        store.record_pending_progress(plan.msg_id, "txid-of-chunk-1")

        (record,) = store.pending_sends()
        assert record["sent_count"] == 1
        assert record["chunks"] == plan.chunk_payloads
        assert record["chunks"][1:] == plan.chunk_payloads[1:]


def test_a_finished_send_leaves_nothing_pending(alice, bob):
    import tempfile
    from pathlib import Path
    from arcade.messaging.sender import plan_message
    from arcade.messaging.store import MessageStore

    plan = plan_message(alice, bob.public_bytes, b"x" * 9000)
    with tempfile.TemporaryDirectory() as directory:
        store = MessageStore(Path(directory) / "m.sqlite")
        store.begin_pending_send(plan.msg_id, bob.public_bytes, "nSender",
                                 b"x" * 9000, plan.chunk_payloads)
        store.finish_pending_send(plan.msg_id)
        assert store.pending_sends() == []


# --- announcing who a key belongs to ------------------------------------------
# A bare announcement is attributed to whichever address funded the transaction,
# and that changes with coin selection -- a test machine measured an identity address, a
# funding address and an announcement address all differing at once. Saying who
# the key belongs to, in the announcement itself, is the stable answer.


def test_a_bare_announcement_is_unchanged():
    """Earlier versions publish exactly this, and must keep working."""
    from arcade.messaging.envelope import build_key_announcement

    assert len(build_key_announcement(b"\x01" * 32)) == 38


def test_an_older_reader_still_gets_the_key_from_a_newer_announcement():
    from arcade.messaging.envelope import build_key_announcement, parse_key_announcement

    key = bytes(range(32))
    full = build_key_announcement(key, b"\x02" * 20, "robin")
    assert parse_key_announcement(full) == key


def test_the_address_and_name_round_trip():
    from arcade.messaging.envelope import build_key_announcement, parse_announced_identity
    from arcade.script import b58check_decode, b58check_encode

    address = "nYW2BPLENpu2nGa7WCExvzxD3hQYueULFa"
    version, hash160 = b58check_decode(address)
    payload = build_key_announcement(bytes(range(32)), hash160, "robin")

    got_hash, got_name = parse_announced_identity(payload)
    assert b58check_encode(version, got_hash) == address
    assert got_name == "robin"


def test_both_addresses_and_the_tag_round_trip():
    """Key, this chain's address, the other chain's, and the @tag (D-032)."""
    from arcade.messaging.envelope import (build_key_announcement,
                                           parse_announced_extras,
                                           parse_announced_identity,
                                           parse_key_announcement)
    from arcade.script import b58check_decode, b58check_encode

    here = "nYW2BPLENpu2nGa7WCExvzxD3hQYueULFa"
    there = "PognhfhGxiSNPrYLQYUaT5bMsVbgumzc6i"
    key = bytes(range(32))
    _, here_hash = b58check_decode(here)
    there_version, there_hash = b58check_decode(there)
    payload = build_key_announcement(key, here_hash, "", there_hash, "robin")

    found = parse_announced_extras(payload)
    assert b58check_encode(113, found["hash160"]) == here
    assert b58check_encode(there_version, found["other_hash160"]) == there
    assert found["tag"] == "robin" and found["name"] == ""

    # Older readers: the key, and the identity tail, are where they were.
    assert parse_key_announcement(payload) == key
    assert parse_announced_identity(payload) == (here_hash, "")

    # Sections are optional and independent.
    assert parse_announced_extras(
        build_key_announcement(key, here_hash, "the operator"))["tag"] == ""
    assert parse_announced_extras(
        build_key_announcement(key, here_hash, "", tag="robin"))["other_hash160"] == b""
    # Class B pads with NULs, which end the walk rather than inventing a section.
    padded = build_key_announcement(key, here_hash, "", there_hash, "robin") + b"\0" * 9
    assert parse_announced_extras(padded)["tag"] == "robin"
    # Damaged extras still yield the key and what came before them.
    assert parse_announced_extras(payload[:-3])["other_hash160"] == there_hash


def test_a_full_announcement_still_fits_one_op_return():
    """If it did not, publishing would cost dust instead of a flat fee."""
    from arcade.encoding import max_class_c_payload
    from arcade.messaging.envelope import MAX_ANNOUNCE_NAME, build_key_announcement
    from arcade.payload import AnyData

    payload = build_key_announcement(bytes(range(32)), b"\x03" * 20,
                                     "x" * MAX_ANNOUNCE_NAME)
    assert len(AnyData(data=payload).encode()) <= max_class_c_payload()


def test_class_b_padding_is_not_mistaken_for_an_identity_tail():
    """The tag is 0x01 precisely so a NUL pad cannot be read as a tail."""
    from arcade.messaging.envelope import build_key_announcement, parse_announced_identity

    padded = build_key_announcement(bytes(range(32))) + b"\x00" * 22
    assert parse_announced_identity(padded) == (b"", "")


def test_a_damaged_tail_does_not_lose_the_key():
    """These bytes came from a stranger; a bad tail must not discard the key."""
    from arcade.messaging.envelope import (
        build_key_announcement, parse_announced_identity, parse_key_announcement)

    key = bytes(range(32))
    truncated = build_key_announcement(key, b"\x04" * 20, "somebody")[:-12]
    assert parse_key_announcement(truncated) == key
    assert parse_announced_identity(truncated) == (b"", "")


def test_a_long_name_is_published_whole_rather_than_cut():
    """It used to be trimmed to 12 bytes in silence.

    "Big Chief Energy" went on the chain twice as "Big Chief En" -- permanently,
    for a fee that cannot be taken back, and cut mid-word. A name that does not
    fit one OP_RETURN now goes as Class B instead: a couple of unspendable
    outputs rather than none, which is a far better trade than half a name.
    """
    from arcade.messaging.envelope import (
        announcement_fits_one_output, build_key_announcement,
        parse_announced_identity)

    payload = build_key_announcement(bytes(range(32)), b"\x05" * 20,
                                     "Big Chief Energy")
    assert parse_announced_identity(payload)[1] == "Big Chief Energy"
    assert announcement_fits_one_output(payload) is False


def test_a_short_name_still_costs_nothing_but_a_fee():
    from arcade.messaging.envelope import (
        announcement_fits_one_output, build_key_announcement)

    payload = build_key_announcement(bytes(range(32)), b"\x05" * 20, "robin")
    assert announcement_fits_one_output(payload) is True


def test_an_absurd_name_is_refused_rather_than_trimmed():
    from arcade.messaging.envelope import EnvelopeError, build_key_announcement

    with pytest.raises(EnvelopeError):
        build_key_announcement(bytes(range(32)), b"\x05" * 20, "x" * 100)


# --- a sender keeps the only copy of what it sent ------------------------------
# A message is sealed to the recipient, so whoever sent it cannot read it back
# off the chain. That is crypto_box_seal working, not a gap -- but it means a
# conversation on any machine is what it received plus what it sent, and a
# machine that does not write down its own half shows half a conversation.
#
# Which is exactly what happened: `add_sent` was called from the web interface
# and not from the CLI, so a machine that sent with `arcade-msg` saw only the
# other person's messages.


def test_a_sender_cannot_read_its_own_message_back(alice, bob):
    """The reason a local copy is the only copy, stated as a test."""
    from arcade.messaging.envelope import EnvelopeError, open_message, seal_message
    from arcade.messaging.envelope import Header, TYPE_SINGLE

    payload = seal_message(alice, bob.public_bytes, Header(type=TYPE_SINGLE), b"hello")

    assert open_message(bob, payload)[1] == b"hello"
    with pytest.raises(EnvelopeError):
        open_message(alice, payload)


def test_recording_a_send_keeps_the_plaintext(tmp_path):
    from arcade.messaging.sender import record_sent
    from arcade.messaging.store import MessageStore

    store = MessageStore(tmp_path / "m.sqlite")
    record_sent(store, "txid1", b"\x40" * 32, "my fingerprint", b"what I said")

    (row,) = store.conn.execute("SELECT * FROM sent").fetchall()
    assert bytes(row["body"]) == b"what I said"
    assert row["sender_fp"] == "my fingerprint"


def test_a_recorded_send_appears_in_the_thread(tmp_path):
    """Both halves, from two different sources, in one conversation."""
    from arcade.messaging.sender import record_sent
    from arcade.messaging.store import MessageStore

    store = MessageStore(tmp_path / "m.sqlite")
    peer = b"\x41" * 32
    store.add_message(None, "tx-in", "tx-in", 1, 100, "nThem", peer, "me", b"their reply")
    record_sent(store, "tx-out", peer, "me", b"my message")

    thread = store.thread("me", peer)
    assert [(item["mine"], bytes(item["body"])) for item in thread] == [
        (False, b"their reply"), (True, b"my message")]


def test_a_storage_failure_does_not_look_like_a_failed_send(tmp_path):
    """The transaction is already on the chain by the time this runs."""
    from arcade.messaging.sender import record_sent

    class Broken:
        def add_sent(self, *args, **kwargs):
            raise RuntimeError("database is locked")

    record_sent(Broken(), "txid", b"\x42" * 32, "fp", b"body")   # must not raise


def test_reassembly_does_not_depend_on_the_utxo_chain():
    """Chunks are joined by message id and countdown, never by their inputs.

    Worth pinning, because the envelope once claimed ordering was "enforced
    structurally by the UTXO chain" -- and nothing enforced it. Reassembly never
    looked at the chain, so the guarantee was asserted rather than checked. Once
    chunks can be funded from separate outputs they do not chain at all, and this
    is what must hold instead: the whole ciphertext is authenticated, so a wrong
    order fails the MAC rather than producing wrong text.
    """
    import inspect
    from arcade.messaging import scanner

    source = inspect.getsource(scanner.Scanner._assemble_one)
    for chain_word in ("vin", "prevout", "spends", "input"):
        assert chain_word not in source, (
            f"reassembly should not consult {chain_word!r}")


def test_chunks_joined_in_the_wrong_order_fail_rather_than_lie(alice, bob):
    """Tampering can stop a message being read. It cannot change what it says."""
    from arcade.messaging.envelope import (
        EnvelopeError, Header, TYPE_CHUNK, open_ciphertext, seal_ciphertext)

    msg_id = b"\x01" * 8
    body = seal_ciphertext(alice, bob.public_bytes,
                           Header(type=TYPE_CHUNK, msg_id=msg_id), b"x" * 400)
    half = len(body) // 2
    swapped = body[half:] + body[:half]

    with pytest.raises(EnvelopeError):
        open_ciphertext(bob, Header(type=TYPE_CHUNK, msg_id=msg_id), swapped)


# --- splitting the wallet so chunks need not chain ----------------------------
# Chunks chain through change outputs only because there is one output to spend.
# Give the address many and each chunk funds itself: measured at 2 seconds for
# six transactions, against about four minutes chained.


def test_splitting_needs_at_least_two_pieces():
    from arcade.messaging.sender import MessageSender, SendError
    from arcade.config import NETWORKS

    sender = MessageSender.__new__(MessageSender)
    sender.params = NETWORKS["regtest"]
    with pytest.raises(SendError):
        sender.split_outputs("nAddr", 1, 1_000_000_000)


def test_pieces_must_be_worth_spending():
    """Below the wallet's discard threshold an output can never be spent again."""
    from arcade.messaging.sender import MessageSender, SendError
    from arcade.config import NETWORKS

    sender = MessageSender.__new__(MessageSender)
    sender.params = NETWORKS["regtest"]
    with pytest.raises(SendError) as caught:
        sender.split_outputs("nAddr", 30, 1000)
    assert "dust" in str(caught.value)


def test_independent_outputs_remove_the_waiting():
    from arcade.messaging.sender import estimate_send_seconds

    chained = estimate_send_seconds(6, 60.0, 180.0, independent_outputs=1)
    split = estimate_send_seconds(6, 60.0, 180.0, independent_outputs=30)
    assert chained[0] == 300
    assert split[0] == 0


def test_more_chunks_than_outputs_still_waits_for_the_remainder():
    """Half a split wallet is half the benefit, not all of it."""
    from arcade.messaging.sender import estimate_send_seconds

    typical, _ = estimate_send_seconds(10, 60.0, 180.0, independent_outputs=4)
    assert typical == 6 * 60          # nine waits, three of them avoided


def test_input_selection_skips_what_another_chunk_claimed():
    """Without this every chunk picks the largest output and double-spends it."""
    from arcade.config import NETWORKS
    from arcade.messaging.sender import MessageSender

    class FakeRpc:
        def call(self, method, *args):
            assert method == "listunspent"
            return [{"txid": "a", "vout": 0, "amount": 10.0},
                    {"txid": "b", "vout": 1, "amount": 10.0}]

    sender = MessageSender.__new__(MessageSender)
    sender.rpc = FakeRpc()
    sender.params = NETWORKS["regtest"]

    first = sender._select_inputs("nAddr", 100_000_000)
    second = sender._select_inputs("nAddr", 100_000_000,
                                   exclude=frozenset(first))
    assert first and second and set(first).isdisjoint(second)


def test_selection_prefers_the_smallest_output_that_covers_it():
    """Otherwise one message breaks a large output and undoes the split."""
    from arcade.config import NETWORKS
    from arcade.messaging.sender import MessageSender

    class FakeRpc:
        def call(self, method, *args):
            return [{"txid": "big", "vout": 0, "amount": 9000.0},
                    {"txid": "small", "vout": 0, "amount": 10.0}]

    sender = MessageSender.__new__(MessageSender)
    sender.rpc = FakeRpc()
    sender.params = NETWORKS["regtest"]

    assert sender._select_inputs("nAddr", 100_000_000) == [("small", 0)]


def test_a_large_message_can_be_planned_at_all(alice, bob):
    """Sealing before deciding was a hard limit at about 64 KB.

    The single-transaction header carries the ciphertext length in a uint16, and
    `plan_message` built that form first to decide whether to chunk -- so any
    message over 64 KB raised "exceeds a uint16" and could not be planned, even
    though chunking handles it. A private attachment may be 5 MB, so an ordinary
    photo hit this.
    """
    from arcade.messaging.sender import plan_message

    plan = plan_message(alice, bob.public_bytes, b"x" * 400_000)
    assert plan.chunked and plan.transactions > 1


def test_the_chunk_boundary_is_unchanged_by_that(alice, bob):
    """Deciding on size must give the same answer sealing did."""
    from arcade.messaging.sender import plan_message

    assert plan_message(alice, bob.public_bytes, b"x" * 7512).transactions == 1
    assert plan_message(alice, bob.public_bytes, b"x" * 7513).transactions > 1


def test_a_single_transaction_message_never_splits_the_wallet():
    """Nothing to gain, and it would spend a fee for nothing."""
    from arcade.config import NETWORKS
    from arcade.messaging.sender import MessageSender

    sender = MessageSender.__new__(MessageSender)
    sender.params = NETWORKS["regtest"]
    assert sender.ensure_outputs("nAddr", wanted=1) is False


def test_the_split_is_sized_by_the_sending_address_not_the_wallet():
    """a test machine, testnet: the wallet held 5,985 coins but the identity address 4.996.

    `ensure_outputs` sized the split from getbalance (the whole wallet) and then
    funded it from the identity address alone, so it planned 8 pieces at 17
    coins and died with "holds 4.99641000, which is short of the 17.00000000"
    before the first transaction of the message. The address that funds the
    split is the one whose balance must size it.
    """
    from arcade.config import NETWORKS
    from arcade.messaging.sender import COIN, MessageSender

    calls = []

    class FakeRpc:
        def call(self, method, *args):
            calls.append(method)
            if method == "getbalance":
                return 5985.16487
            if method == "listunspent":
                assert args[2] == ["nIdentity"], "only the sender's own coins count"
                return [{"txid": "a", "vout": 0, "amount": 1.5},
                        {"txid": "b", "vout": 0, "amount": 1.5},
                        {"txid": "c", "vout": 0, "amount": 1.99641}]
            raise AssertionError(f"unexpected {method}")

    sender = MessageSender.__new__(MessageSender)
    sender.rpc = FakeRpc()
    sender.params = NETWORKS["regtest"]
    sender.split_outputs = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not plan a split the address cannot pay for"))

    assert sender.spendable_value("nIdentity") == 499_641_000
    # 4.996 minus a coin of headroom buys one 2-coin piece: not worth a split,
    # so the send chains one chunk per block instead of failing outright.
    assert sender.ensure_outputs("nIdentity", wanted=4) is False
    assert "getbalance" not in calls

    # With enough on the address itself, it splits into what THAT can afford.
    class RicherRpc(FakeRpc):
        def call(self, method, *args):
            if method == "listunspent":
                return [{"txid": "big", "vout": 0, "amount": 9.5}]
            return super().call(method, *args)

    planned = {}
    sender.rpc = RicherRpc()
    sender.split_outputs = lambda address, pieces, each: planned.update(pieces=pieces) or "raw"
    sender.broadcast = lambda prepared: "txid"
    sender._await_confirmation = lambda txid, timeout: None
    assert sender.ensure_outputs("nIdentity", wanted=6) is True
    assert planned["pieces"] == 4, "(9.5 - 1) // 2, not wanted + 4 = 10"


def test_the_short_of_coins_advice_says_to_fund_the_sending_address():
    """"Use that one" cannot be followed: a message is sent from the messaging
    identity and nothing else. The advice has to be to move coins onto it."""
    import pytest

    from arcade.config import NETWORKS
    from arcade.messaging.sender import MessageSender, SendError

    class FakeRpc:
        def call(self, method, *args):
            if args and args[-1] == ["nIdentity"]:
                return [{"txid": "x", "vout": 0, "amount": 4.99641, "address": "nIdentity"}]
            return [{"txid": "y", "vout": 1, "amount": 4978.18004, "address": "nChange"},
                    {"txid": "x", "vout": 0, "amount": 4.99641, "address": "nIdentity"}]

    sender = MessageSender.__new__(MessageSender)
    sender.rpc = FakeRpc()
    sender.params = NETWORKS["regtest"]
    with pytest.raises(SendError) as excinfo:
        sender._select_inputs("nIdentity", 17 * 100_000_000)
    text = str(excinfo.value)
    assert "nChange holds 4978.18004000" in text
    assert "to nIdentity" in text and "Wallet page" in text
    assert "Use that one" not in text


def test_only_confirmed_outputs_count_towards_not_splitting():
    """a test machine topped the identity up with 3,000 and pressed send twenty seconds
    later. The unconfirmed output made four, so no split happened -- and
    send_all funds chunks two onwards from CONFIRMED outputs only, so they
    chained a block apart anyway. The count has to use the same rule."""
    from arcade.config import NETWORKS
    from arcade.messaging.sender import MessageSender

    class FakeRpc:
        def call(self, method, *args):
            if method == "listunspent":
                minconf = args[0]
                confirmed = [{"txid": "c", "vout": 0, "amount": 1.0217, "confirmations": 24}]
                fresh = [{"txid": "f", "vout": 0, "amount": 3000.0, "confirmations": 0},
                         {"txid": "s1", "vout": 0, "amount": 1.5, "confirmations": 0},
                         {"txid": "s2", "vout": 0, "amount": 1.5, "confirmations": 0}]
                return confirmed if minconf >= 1 else confirmed + fresh
            raise AssertionError(method)

    sender = MessageSender.__new__(MessageSender)
    sender.rpc = FakeRpc()
    sender.params = NETWORKS["regtest"]
    planned = {}
    sender.split_outputs = lambda address, pieces, each: planned.update(pieces=pieces) or "raw"
    sender.broadcast = lambda prepared: "txid"
    sender._await_confirmation = lambda txid, timeout: None

    assert sender.spendable_outputs("nIdentity", at_least=100_000_000, minconf=1) == 1
    assert sender.ensure_outputs("nIdentity", wanted=4) is True, "one confirmed output is not four"
    assert planned["pieces"] == 8, "sized by everything the address holds, unconfirmed included"


def test_enough_outputs_means_no_split():
    from arcade.config import NETWORKS
    from arcade.messaging.sender import MessageSender

    class FakeRpc:
        def call(self, method, *args):
            return [{"txid": f"t{i}", "vout": 0, "amount": 10.0} for i in range(40)]

    sender = MessageSender.__new__(MessageSender)
    sender.rpc = FakeRpc()
    sender.params = NETWORKS["regtest"]
    assert sender.ensure_outputs("nAddr", wanted=6) is False

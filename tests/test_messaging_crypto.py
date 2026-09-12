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


def test_a_long_name_is_truncated_not_refused():
    from arcade.messaging.envelope import (
        MAX_ANNOUNCE_NAME, build_key_announcement, parse_announced_identity)

    payload = build_key_announcement(bytes(range(32)), b"\x05" * 20, "a" * 80)
    _, name = parse_announced_identity(payload)
    assert name == "a" * MAX_ANNOUNCE_NAME


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

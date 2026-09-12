"""Files and profiles carried inside a message.

This layer lives inside the sealed box, so the filename and the sender's claimed
name are encrypted along with the text and invisible on the chain. It needed no
wire format change, which is why chunking, scanning and reassembly are untouched.
"""

import pytest

from arcade.messaging import content as C


def test_plain_text_is_byte_identical_to_what_it_always_was(alice=None):
    """Backward compatibility is not a nice-to-have here: messages are permanent."""
    assert C.build("hello") == b"hello"


def test_a_message_from_before_this_existed_still_reads():
    parsed = C.parse(b"sent last week, before attachments")
    assert parsed.plain
    assert parsed.text == "sent last week, before attachments"


def test_a_file_round_trips():
    blob = bytes(range(256)) * 4
    body = C.build("here it is", C.Attachment("photo.png", "image/png", blob))
    got = C.parse(body)
    assert got.text == "here it is"
    assert got.attachment.data == blob
    assert got.attachment.name == "photo.png"
    assert got.attachment.content_type == "image/png"


def test_a_file_with_no_text_round_trips():
    got = C.parse(C.build("", C.Attachment("a.bin", "application/octet-stream", b"\x00\x01")))
    assert got.text == ""
    assert got.attachment.data == b"\x00\x01"


def test_a_profile_round_trips():
    body = C.build("offer attached", profile=C.Profile("the operator", "nTest", "PMain"))
    got = C.parse(body)
    assert got.profile.name == "the operator"
    assert got.profile.testnet_address == "nTest"
    assert got.profile.mainnet_address == "PMain"


@pytest.mark.parametrize("name,expected", [
    ("../../etc/passwd", "passwd"),
    ("..\\..\\windows\\system32\\cmd.exe", "cmd.exe"),
    ("/absolute/path.txt", "path.txt"),
    ("", "attachment"),
    ("...", "attachment"),
    ('bad"quote.txt', "badquote.txt"),
])
def test_a_sender_cannot_choose_a_dangerous_filename(name, expected):
    """The sender picks this string and the receiver may save it."""
    assert C._safe_name(name) == expected


def test_a_dangerous_filename_is_cleaned_on_the_way_out_too():
    body = C.build("", C.Attachment("../../../evil.sh", "text/plain", b"rm -rf /"))
    assert C.parse(body).attachment.name == "evil.sh"


@pytest.mark.parametrize("body", [
    b"\x01ARCB",
    b"\x01ARCB\x01",
    b"\x01ARCB\x01\xff\xffnot json at all",
    b"\x01ARCB\x09" + b"\x00\x02" + b"{}",       # unknown version
    b"\x01ARCB\x01\x00\x02[]",                    # json, but not an object
])
def test_malformed_bodies_never_raise(body):
    """These bytes came from somebody else. A bad one must not lose the message."""
    parsed = C.parse(body)
    assert isinstance(parsed.text, str)


def test_a_truncated_file_gives_short_bytes_rather_than_an_exception():
    body = C.build("", C.Attachment("x.bin", "application/octet-stream", b"y" * 100))
    parsed = C.parse(body[:-40])
    assert len(parsed.attachment.data) == 60


def test_an_oversized_file_is_refused_with_the_reason():
    with pytest.raises(C.ContentError) as caught:
        C.build("", C.Attachment("huge.bin", "application/octet-stream",
                                 b"x" * (C.MAX_FILE_BYTES + 1)))
    assert "never be spent again" in str(caught.value)


def test_an_empty_file_is_refused():
    with pytest.raises(C.ContentError):
        C.build("", C.Attachment("empty.bin", "application/octet-stream", b""))


def test_overhead_is_small_relative_to_the_file():
    """Raw bytes, not base64: a third more payload would be a third more dust."""
    blob = b"z" * 10_000
    body = C.build("", C.Attachment("big.bin", "application/octet-stream", blob))
    assert len(body) - len(blob) < 120


# --- what a first message tells the recipient ---------------------------------


def _store(tmp_path):
    from arcade.messaging.store import MessageStore
    return MessageStore(tmp_path / "m.sqlite")


def test_receiving_a_message_fills_in_the_address_book(tmp_path):
    """Receiving from somebody is itself the introduction."""
    store = _store(tmp_path)
    key = b"\x0a" * 32
    store.add_message(None, "tx", "tx", 1, 0, "nTheirAddress", key, "me", b"hello")

    row = store.contact_by_key(key)
    assert row["testnet_address"] == "nTheirAddress"


def test_a_name_the_user_typed_is_never_overwritten(tmp_path):
    """The value of a local name is precisely that nobody else chose it."""
    store = _store(tmp_path)
    key = b"\x0a" * 32
    store.apply_profile(key, "Claimed Name", "nClaimed", "PClaimed")
    row = store.contact_by_key(key)
    store.save_contact(contact_id=row["id"], name="What I Call Them",
                       testnet_address="nIChoseThis", mainnet_address="PIChoseThis")

    store.apply_profile(key, "Someone Else Entirely", "nOther", "POther")

    row = store.contact_by_key(key)
    assert row["name"] == "What I Call Them"
    assert row["testnet_address"] == "nIChoseThis"
    assert row["mainnet_address"] == "PIChoseThis"


def test_a_profile_fills_blanks_only(tmp_path):
    store = _store(tmp_path)
    key = b"\x0a" * 32
    store.apply_profile(key, "", "nFromProfile", "")
    store.apply_profile(key, "Later Name", "nDifferent", "PFromProfile")

    row = store.contact_by_key(key)
    assert row["testnet_address"] == "nFromProfile"    # already set, kept
    assert row["name"] == "Later Name"                 # was blank, filled
    assert row["mainnet_address"] == "PFromProfile"    # was blank, filled


def test_a_profile_arriving_by_message_reaches_the_address_book(tmp_path):
    """End to end through the body format, as the scanner does it."""
    store = _store(tmp_path)
    key = b"\x0b" * 32
    body = C.build("Offer on your NFT",
                   profile=C.Profile("the operator", "nTheirTest", "PTheirMain"))
    parsed = C.parse(body)
    store.add_message(None, "tx", "tx", 1, 0, "nTheirTest", key, "me",
                      parsed.text.encode())
    store.apply_profile(key, parsed.profile.name, parsed.profile.testnet_address,
                        parsed.profile.mainnet_address)

    row = store.contact_by_key(key)
    assert row["name"] == "the operator"
    assert row["mainnet_address"] == "PTheirMain"


# --- every byte value survives every container --------------------------------
# a test machine's check, and the right one: the wire is binary-clean, so the risk is not
# the chain but anywhere the bytes pass through a text-only container on the way
# to disk or to a browser. A file is guaranteed to contain bytes that are not
# valid UTF-8, and the resume path is the one nobody exercises twice.

ALL_BYTES = bytes(range(256)) * 8          # every value, and not valid UTF-8


def test_every_byte_value_survives_the_body_format():
    got = C.parse(C.build("binary", C.Attachment("all.bin", "application/octet-stream",
                                                 ALL_BYTES)))
    assert got.attachment.data == ALL_BYTES


def test_every_byte_value_survives_the_attachment_table(tmp_path):
    """A TEXT column would mangle this; the column must be BLOB."""
    store = _store(tmp_path)
    message_id = store.add_message(None, "tx", "tx", 1, 0, "nS", b"\x0c" * 32,
                                   "me", b"see attached")
    store.add_attachment(message_id, "all.bin", "application/octet-stream", ALL_BYTES)

    assert bytes(store.attachment_for(message_id)["data"]) == ALL_BYTES


def test_every_byte_value_survives_a_store_reopen(tmp_path):
    """Written by one connection, read by another -- as a resume really does."""
    from arcade.messaging.store import MessageStore

    store = _store(tmp_path)
    message_id = store.add_message(None, "tx", "tx", 1, 0, "nS", b"\x0c" * 32,
                                   "me", ALL_BYTES)
    store.add_attachment(message_id, "all.bin", "application/octet-stream", ALL_BYTES)
    store.close()

    reopened = MessageStore(tmp_path / "m.sqlite")
    assert bytes(reopened.attachment_for(message_id)["data"]) == ALL_BYTES
    row = reopened.conn.execute("SELECT body FROM message WHERE id=?",
                                (message_id,)).fetchone()
    assert bytes(row["body"]) == ALL_BYTES


def test_every_byte_value_survives_the_resume_path(tmp_path):
    """The path nobody tests twice: sealed chunks out to disk and back.

    If the chunks were serialised through JSON or str() this would corrupt
    silently, and only on resume.
    """
    from arcade.messaging.keys import Identity
    from arcade.messaging.sender import plan_message
    from arcade.messaging.store import MessageStore

    alice, bob = Identity.generate(), Identity.generate()
    body = C.build("big binary", C.Attachment("all.bin", "application/octet-stream",
                                              ALL_BYTES * 6))
    plan = plan_message(alice, bob.public_bytes, body)
    assert plan.chunked, "the fixture must be large enough to chunk"

    store = _store(tmp_path)
    store.begin_pending_send(plan.msg_id, bob.public_bytes, "nSender", body,
                             plan.chunk_payloads)
    store.record_pending_progress(plan.msg_id, "txid-1")
    store.close()

    reopened = MessageStore(tmp_path / "m.sqlite")
    (record,) = reopened.pending_sends()
    assert record["chunks"] == plan.chunk_payloads
    assert record["body"] == body


def test_a_binary_attachment_round_trips_through_encryption_and_chunking():
    """Send, chunk, reassemble, decrypt, unpack -- the whole path, all 256 values."""
    from arcade.messaging.envelope import Header, TYPE_CHUNK, open_ciphertext
    from arcade.messaging.keys import Identity
    from arcade.messaging.sender import plan_message

    alice, bob = Identity.generate(), Identity.generate()
    original = ALL_BYTES * 6
    body = C.build("photo", C.Attachment("all.bin", "image/png", original))
    plan = plan_message(alice, bob.public_bytes, body)
    assert plan.chunked

    # Reassemble the way the scanner does: strip each chunk header, join, open.
    pieces = []
    for payload in plan.chunk_payloads:
        header = Header.decode(payload)
        pieces.append(payload[header.length:][:header.clen])
    _, plaintext = open_ciphertext(
        bob, Header(type=TYPE_CHUNK, msg_id=plan.msg_id), b"".join(pieces))

    got = C.parse(plaintext)
    assert got.attachment.data == original
    assert got.attachment.content_type == "image/png"
    assert got.text == "photo"


def test_a_declared_profile_address_beats_the_funding_address(tmp_path):
    """Found across machines by a test machine: the address book showed the wrong address.

    `add_message` fills a blank address from the transaction, and that address is
    whichever one funded the send -- it changes with coin selection. A profile
    declares the identity address explicitly. If the inferred one is written
    first it occupies the field and the declared one, being "fill blanks only",
    silently loses.
    """
    store = _store(tmp_path)
    key = b"\x0f" * 32
    identity_address = "nYW2BPLENpu2nGa7WCExvzxD3hQYueULFa"
    funding_address = "nou2qUYgAU58cKHWEPCy9QQdg1Psmqig1v"

    # The order the scanner uses: profile first, then the message.
    store.apply_profile(key, "", identity_address, "")
    store.add_message(None, "tx", "tx", 1, 0, funding_address, key, "me", b"hi")

    assert store.contact_by_key(key)["testnet_address"] == identity_address


def test_without_a_profile_the_funding_address_is_still_better_than_nothing(tmp_path):
    """It is not wrong, only less stable -- and it is all there is to go on."""
    store = _store(tmp_path)
    key = b"\x10" * 32
    store.add_message(None, "tx", "tx", 1, 0, "nFundingAddress", key, "me", b"hi")

    assert store.contact_by_key(key)["testnet_address"] == "nFundingAddress"

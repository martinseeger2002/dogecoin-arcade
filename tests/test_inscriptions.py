"""The inscription wire format.

This is the part that cannot change. Once one inscription exists on mainnet the
format is fixed: a reader that disagrees about a single byte reads a different
file, or no file. So the tests here are about the bytes, not about convenience.
"""

import hashlib

import pytest

from arcade import inscriptions as I


def test_a_file_goes_out_and_comes_back_byte_for_byte():
    data = bytes(range(256)) * 120                     # 30,720 bytes
    bodies = I.plan(data, "image/png", '{"name":"Sunrise"}')

    assembly = I.Assembly(inscription_id=b"")
    for body in bodies:
        assembly.add(I.parse(body))
    manifest, content = assembly.join()

    assert content == data
    assert manifest.content_type == "image/png"
    assert manifest.json == '{"name":"Sunrise"}'
    assert manifest.total == len(data)
    assert manifest.sha256 == hashlib.sha256(data).digest()


def test_chunks_may_arrive_in_any_order():
    """They are independent transactions -- a split wallet funds each from its
    own output -- so the miner takes them in whatever order it likes."""
    data = b"\xff\xd8" * 9000
    bodies = I.plan(data, "image/jpeg")
    assembly = I.Assembly(inscription_id=b"")
    for body in reversed(bodies):
        assembly.add(I.parse(body))
    assert assembly.join()[1] == data


def test_a_missing_chunk_is_missing_rather_than_a_broken_file():
    data = b"x" * 40_000
    bodies = I.plan(data, "text/plain")
    assembly = I.Assembly(inscription_id=b"")
    for body in bodies[:-1]:
        assembly.add(I.parse(body))
    assert not assembly.complete()
    with pytest.raises(I.InscriptionError, match="missing"):
        assembly.join()


def test_a_gap_in_the_middle_is_not_complete():
    data = b"y" * 40_000
    bodies = I.plan(data, "text/plain")
    assembly = I.Assembly(inscription_id=b"")
    for body in bodies[:1] + bodies[2:]:
        assembly.add(I.parse(body))
    assert not assembly.complete()


def test_trailing_nul_padding_cannot_corrupt_the_file():
    """Class B pads its last packet to a 30-byte boundary and nothing strips
    it, so a chunk arrives with up to 29 bytes the sender never wrote. The
    length field in the header is what makes that harmless -- and a file whose
    own last byte is NUL is the case that proves the field is load-bearing."""
    data = b"a real file that ends in a zero byte\x00\x00\x00"
    bodies = I.plan(data, "application/octet-stream")
    padded = [b + b"\x00" * 29 for b in bodies]        # what Class B hands back
    assembly = I.Assembly(inscription_id=b"")
    for body in padded:
        assembly.add(I.parse(body))
    assert assembly.join()[1] == data


def test_content_that_does_not_match_its_hash_is_refused():
    """The hash is what makes a reassembled file verifiable rather than
    hopeful."""
    data = b"z" * 20_000
    bodies = I.plan(data, "text/plain")
    assembly = I.Assembly(inscription_id=b"")
    for body in bodies:
        assembly.add(I.parse(body))
    # Corrupt one byte of the middle of the content, keeping every length right.
    worst = max(assembly.pieces)
    assembly.pieces[worst - 1] = b"\x00" + assembly.pieces[worst - 1][1:]
    with pytest.raises(I.InscriptionError, match="does not match its own hash"):
        assembly.join()


def test_the_json_field_has_to_be_json():
    """An inscription cannot be corrected. A typo found at read time is found
    far too late."""
    with pytest.raises(I.InscriptionError, match="not valid JSON"):
        I.plan(b"x", "text/plain", "{not json")
    assert I.validate_json("") == ""
    assert I.validate_json('  {"a": 1}  ') == '{"a": 1}'
    assert I.validate_json("[1, 2, 3]") == "[1, 2, 3]"


def test_a_transfer_fits_in_the_cheapest_carriage():
    """Moving one should never cost what making one costs. 38 bytes fits the
    72 an OP_RETURN carries, so a transfer is one cheap transaction."""
    from arcade.encoding import max_class_c_payload

    payload = I.Transfer(txid=bytes(range(32))).encode()
    assert len(payload) == I.TRANSFER_LEN == 38
    assert len(payload) + 4 <= max_class_c_payload()   # +4 for the type header

    back = I.parse(payload)
    assert isinstance(back, I.Transfer)
    assert back.txid == bytes(range(32))


def test_an_older_reader_sees_something_it_can_skip():
    """Inscriptions ride inside AnyData (type 200), which the engine already
    decodes and ignores. A copy of this software that predates them therefore
    skips one instead of halting on an unknown type -- which is what it does
    with anything it does not recognise, and halting is what it must not do."""
    from arcade.payload import AnyData, decode

    body = I.plan(b"hello", "text/plain")[0]
    wire = AnyData(data=body).encode()
    back = decode(wire)
    assert isinstance(back, AnyData)
    assert I.is_inscription(back.data)


def test_something_that_is_not_an_inscription_is_left_alone():
    """A public post is AnyData too, and must not be read as an inscription."""
    from arcade.messaging import group

    post = group.build(group.GroupPost("main", "Me", "hello"))
    assert not I.is_inscription(post)
    with pytest.raises(I.InscriptionError):
        I.parse(post)


def test_a_version_we_do_not_know_is_refused_rather_than_guessed():
    body = bytearray(I.plan(b"hello", "text/plain")[0])
    body[4] = 9
    with pytest.raises(I.InscriptionError, match="version 9"):
        I.parse(bytes(body))


def test_nothing_is_not_an_inscription():
    with pytest.raises(I.InscriptionError, match="nothing to inscribe"):
        I.plan(b"", "text/plain")


def test_a_file_past_the_ceiling_says_so_in_megabytes():
    with pytest.raises(I.InscriptionError, match="limited to 5 MB"):
        I.plan(b"x" * (I.MAX_CONTENT + 1), "text/plain")


def test_the_split_is_even_so_no_transaction_is_the_largest_possible():
    """A greedy split makes the first transaction as large as it can be, and
    that is the one most likely to meet a relay limit."""
    bodies = I.plan(b"q" * 30_000, "text/plain")
    sizes = [len(b) for b in bodies]
    assert len(set(sizes[:-1])) == 1, "every chunk but the last is the same size"
    assert sizes[-1] <= sizes[0], "and the last one carries the remainder"
    assert all(size <= 7_646 for size in sizes)
    # The greedy split it is not: that would put 7,646 in the first and the
    # leftovers in the last.
    assert sizes[0] < 7_646


def test_the_last_chunk_is_the_one_that_counts_down_to_zero():
    """The Doginals convention, used everywhere else here: an abandoned set is
    always distinguishable from a complete one."""
    bodies = [I.parse(b) for b in I.plan(b"w" * 25_000, "text/plain")]
    assert [c.countdown for c in bodies] == list(range(len(bodies) - 1, -1, -1))
    assert bodies[-1].countdown == 0
    assert len({c.inscription_id for c in bodies}) == 1


def test_one_transaction_is_enough_for_a_small_file():
    bodies = I.plan(b"hello world", "text/plain", '{"n":1}')
    assert len(bodies) == 1
    assembly = I.Assembly(inscription_id=b"")
    assembly.add(I.parse(bodies[0]))
    assert assembly.complete()
    assert assembly.join()[1] == b"hello world"

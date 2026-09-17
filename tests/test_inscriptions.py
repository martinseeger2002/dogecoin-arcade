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


def test_there_is_no_policy_ceiling(monkeypatch):
    """What an inscription costs is the creator's to decide. The only limit is
    arithmetic: the countdown that marks the last chunk is two bytes."""
    # 40 MB, far past anything a policy would have allowed, planned without
    # complaint -- at the real per-transaction capacity, so the count is real.
    bodies = I.plan(b"x" * (40 * 1024 * 1024), "video/mp4")
    assert len(bodies) == 5_499
    assert I.parse(bodies[-1]).countdown == 0

    # The one limit there is, and it is arithmetic: 65,536 chunks.
    with pytest.raises(I.InscriptionError, match="two bytes"):
        I.plan(b"y" * 6_000_000, "video/mp4", capacity=100)


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


# --- what it will cost ---------------------------------------------------------
# An inscription is permanent and paid for in advance. A number shown afterwards
# is a number shown too late.


def test_the_estimate_matches_what_a_real_send_did():
    """135,150 bytes went out as 18 transactions on testnet. The estimate has
    to say 18, or it is not an estimate of anything."""
    from arcade import inscribe

    assert inscribe.estimate(135_150, "image/jpeg").chunks == 18


def test_an_estimate_needs_only_a_length():
    """So a page can price a file the moment it is chosen, without holding a
    second copy of it in memory."""
    from arcade import inscribe

    data = b"x" * 50_000
    assert inscribe.estimate(data, "text/plain") == inscribe.estimate(50_000, "text/plain")


def test_the_dust_is_counted_separately_because_it_comes_back():
    """Every data output carries the creator's own key, so it is spendable
    again. Reporting one number would overstate the cost several times over."""
    from arcade import inscribe

    one_mb = inscribe.estimate(1_048_576, "image/png")
    assert one_mb.dust > one_mb.fee * 2, "dust dominates, which is the point"
    # The fee is on the virtual size (fees.py): 0.01 per 20 sigops per output,
    # so 0.004 per data output and a little for the rest of each chunk.
    assert one_mb.fee == pytest.approx(one_mb.outputs * 0.004, rel=0.02)
    assert one_mb.total == one_mb.fee + one_mb.dust
    assert one_mb.net == one_mb.fee
    assert one_mb.recoverable == one_mb.dust


def test_a_bigger_file_costs_more_in_every_direction():
    from arcade import inscribe

    small = inscribe.estimate(10_000)
    large = inscribe.estimate(100_000)
    assert large.chunks > small.chunks
    assert large.outputs > small.outputs
    assert large.fee > small.fee and large.dust > small.dust


def test_planning_gives_payloads_ready_for_the_chain():
    """The bodies themselves, NOT wrapped in AnyData: the sender wraps whatever
    it is handed, and wrapping here as well produced an AnyData inside an
    AnyData -- accepted by the chain, recorded as valid, and never an
    inscription, because the outer body started with a payload header rather
    than with INSC. Paid for and invisible."""
    from arcade import inscribe
    from arcade.payload import AnyData, decode

    plan = inscribe.plan(b"a picture" * 2000, "image/png", '{"n":1}')
    assert plan.chunks == plan.estimate.chunks
    assert plan.content_len == len(b"a picture" * 2000)
    assert plan.json == '{"n":1}'
    for payload in plan.payloads:
        assert I.is_inscription(payload), "a bare inscription body"
        # What the sender will make of it is what the engine has to read.
        assert I.is_inscription(decode(AnyData(data=payload).encode()).data)
        assert len(payload) + 4 <= 7_650, "one Class B transaction's worth"


def test_the_json_is_checked_before_anything_is_priced():
    from arcade import inscribe

    with pytest.raises(I.InscriptionError, match="not valid JSON"):
        inscribe.plan(b"x", "text/plain", "{oops")


def test_a_set_may_describe_itself_without_changing_what_it_contains():
    """Collection-level details are read from an item's JSON; membership is
    not decided by them. An object where a string was expected must not move
    a piece out of its collection -- two nodes cannot be allowed to disagree
    about that because one of them understood a richer JSON."""
    import json

    rich = json.dumps({
        "name": "Goofball #1", "edition": 1,
        "collection": {"name": "Goofball", "description": "  100   goofballs ",
                       "external_url": "https://goofball.example",
                       "twitter": "@goofballs", "supply": 100,
                       "discord": "javascript:alert(1)", "royalty": "5%"}})
    assert I.collection_of(rich) == ("Goofball", 1, "Goofball #1")
    assert I.collection_details(rich) == {
        "description": "100 goofballs", "url": "https://goofball.example",
        "twitter": "@goofballs", "supply": 100}, \
        "whitelisted, tidied, and a link that is not http(s) is not a link"

    # HashLips writes these at the top level of every item, which is where
    # they are read from when there is no object.
    plain = json.dumps({"name": "Goofball #2", "edition": 2,
                        "description": "one goofball",
                        "external_url": "http://goofball.example"})
    assert I.collection_details(plain) == {"description": "one goofball",
                                           "url": "http://goofball.example"}

    for nothing in ("", "not json", "[1,2]", json.dumps({"name": "x"})):
        assert I.collection_details(nothing) == {}

    # Inscribed text is written by anybody: a description is capped and a
    # supply that is not a whole number is not a supply.
    long = json.dumps({"description": "x" * 900, "supply": True})
    assert I.collection_details(long) == {"description": "x" * 400}

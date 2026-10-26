"""ord's witness envelopes read as ord-litecoin reads them (arcade/ord_envelope.py): the cases of
ynohtna92/ord-litecoin src/inscriptions/envelope.rs's own tests, ported."""
from arcade import ord_envelope as E


def _push(data: bytes) -> bytes:
    if len(data) == 0:
        return b"\x00"
    if len(data) < 76:
        return bytes([len(data)]) + data
    if len(data) < 256:
        return b"\x4c" + bytes([len(data)]) + data
    return b"\x4d" + len(data).to_bytes(2, "little") + data


def _envelope(*pushes: bytes) -> list[bytes]:
    """The tests' `envelope(&[...])`: OP_FALSE OP_IF pushes OP_ENDIF as a tapscript, an empty control block after it."""
    return [b"\x00\x63" + b"".join(_push(p) for p in pushes) + b"\x68", b""]


def _one(witness) -> list[E.Envelope]:
    return E.envelopes([{"witness": witness}])


def test_nothing_in_an_empty_or_key_path_witness():
    assert _one([]) == []
    assert _one([b"\x30" * 64]) == [], "a key path spend"
    assert _one([b"\x30" * 64, b"\x50annex"]) == [], "a key path spend with an annex"


def test_an_envelope_and_its_fields():
    [e] = _one(_envelope(b"ord", b"\x01", b"text/plain;charset=utf-8", b"", b"ord"))
    assert (e.content_type, e.body, e.input, e.offset) == (b"text/plain;charset=utf-8", b"ord", 0, 0)
    assert not (e.pushnum or e.stutter or e.duplicate_field or e.incomplete_field or e.unrecognized_even_field)


def test_duplicate_incomplete_and_unrecognized_fields():
    [d] = _one(_envelope(b"ord", b"\xff", b"\x01", b"\xff", b"\x02"))
    assert d.duplicate_field and not d.unrecognized_even_field, "255 is odd: ignored, but twice is a duplicate"
    [i] = _one(_envelope(b"ord", b"\x01"))
    assert i.incomplete_field and i.body is None
    [u] = _one(_envelope(b"ord", bytes([22]), b"\x00"))
    assert u.unrecognized_even_field
    [ok] = _one(_envelope(b"ord", bytes([255]), b"\x00"))
    assert not ok.unrecognized_even_field, "unknown odd fields are ignored"


def test_bodies():
    assert _one(_envelope(b"ord", b"", b"foo"))[0].body == b"foo", "no content type"
    assert _one(_envelope(b"ord", b"\x01", b"text/plain"))[0].body is None, "no body"
    assert _one(_envelope(b"ord", b"\x01", b"text/plain", b"", b"foo", b"bar"))[0].body == b"foobar"
    assert _one(_envelope(b"ord", b"\x01", b"text/plain", b""))[0].body == b"", "a body in zero pushes"
    assert _one(_envelope(b"ord", b"\x01", b"text/plain", b"", b"", b"", b""))[0].body == b""
    assert _one(_envelope(b"ord", b"", b"\xc3\x28"))[0].body == b"\xc3\x28", "invalid UTF-8 stays an inscription"


def test_preceding_and_trailing_script_and_many_envelopes():
    script = (b"\x51" + b"\x00\x63" + _push(b"ord") + _push(b"") + _push(b"a") + b"\x68" + b"\x52"
              + b"\x00\x63" + _push(b"ord") + _push(b"") + _push(b"b") + b"\x68" + b"\x53")
    got = _one([script, b""])
    assert [(e.body, e.offset) for e in got] == [(b"a", 0), (b"b", 1)]


def test_no_endif_no_op_false_wrong_protocol_or_an_opcode_inside_is_no_envelope():
    assert _one([b"\x00\x63" + _push(b"ord") + _push(b"") + _push(b"x"), b""]) == [], "no OP_ENDIF"
    assert _one([b"\x63" + _push(b"ord") + _push(b"") + _push(b"x") + b"\x68", b""]) == [], "no OP_FALSE"
    assert _one(_envelope()) == [] and _one(_envelope(b"foo")) == []
    assert _one([b"\x00\x63" + _push(b"ord") + b"\xac" + _push(b"") + b"\x68", b""]) == [], "OP_CHECKSIG inside"


def test_an_unparsable_script_drops_every_envelope_in_its_input():
    good = b"\x00\x63" + _push(b"ord") + _push(b"") + _push(b"a") + b"\x68"
    assert len(_one([good, b""])) == 1
    assert _one([good + b"\x4d\xff", b""]) == [], "a push past the end, after a good envelope"


def test_pushnum_and_stutter():
    [p] = _one([b"\x00\x63" + _push(b"ord") + b"\x51" + _push(b"text/plain") + _push(b"") + b"\x4f" + b"\x68", b""])
    assert p.pushnum and p.content_type == b"text/plain" and p.body == b"\x81"
    stutter = b"\x00\x00\x63" + _push(b"ord") + _push(b"") + _push(b"x") + b"\x68"
    [s] = _one([stutter, b""])
    assert s.stutter, "OP_FALSE OP_FALSE OP_IF: the first OP_FALSE opened nothing"
    [n] = _one(_envelope(b"ord", b"", b"x"))
    assert not n.stutter


def test_the_pointer():
    def ptr(value):
        return _one(_envelope(b"ord", b"\x02", value))[0]
    assert ptr(b"\x01").pointer() == 1 and ptr(b"\x00\x01").pointer() == 256
    assert ptr(b"\x01" + b"\x00" * 10).pointer() == 1, "zeros past the eighth byte are fine"
    assert ptr(b"\x01" + b"\x00" * 7 + b"\x01").pointer() is None
    assert ptr(b"\x01").pointer_raw == b"\x01" and not ptr(b"\x01").unrecognized_even_field
    two = _one(_envelope(b"ord", b"\x02", b"\x01", b"\x02", b"\x02"))[0]
    assert two.duplicate_field and two.unrecognized_even_field, "a second pointer is left over: even, unknown"


def test_an_annex_and_the_second_input():
    script = b"\x00\x63" + _push(b"ord") + _push(b"") + _push(b"a") + b"\x68"
    assert len(_one([b"sig", script, b"\xc0" + b"\x11" * 32, b"\x50annex"])) == 1
    got = E.envelopes([{"witness": []}, {"witness": [script, b""]}])
    assert [(e.input, e.offset) for e in got] == [(1, 0)]


def test_parents_and_delegate_and_metadata():
    [e] = _one(_envelope(b"ord", b"\x03", b"p1", b"\x03", b"p2", b"\x0b", b"d", b"\x05", b"m1", b"\x05", b"m2"))
    assert e.parents == [b"p1", b"p2"] and e.delegate == b"d" and e.metadata == b"m1m2"
    assert e.duplicate_field, "two parents is a duplicate field (and so a curse) in 0.20"

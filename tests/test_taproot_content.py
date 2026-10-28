"""The content of ord's Taproot inscriptions (Litecoin ords, Bellinals): the ord index already counted their births from
the reveal's witness (doginal_index.taproot_read), and /content/<id> now shows them too, read from the witness envelope
-- OP_FALSE OP_IF "ord" <fields> OP_0 <body pushes> OP_ENDIF in the tapscript of a script-path spend -- through the
chain's own node (getrawtransaction). A fake node here, and witness stacks built by hand in ord's own format."""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                                # noqa: F401,E402

from arcade import doginal_index as dx, doginals as D, funding        # noqa: E402
from arcade.txbuild import push                                       # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 5                    # 1,288 bytes: more than one 520-byte push
SIG = b"\x30" * 64
CONTROL = b"\xc0" + b"\x11" * 32


def envelope(ctype: bytes | None, body: bytes | None, chunk: int = 520, tags: bytes = b"") -> bytes:
    """One ord envelope: OP_FALSE OP_IF "ord", the content type (tag 1), other tags, OP_0, the body in pushes of at
    most `chunk` bytes (ord's own split), OP_ENDIF. `body` None: no body tag at all."""
    out = b"\x00\x63" + push(b"ord") + (push(b"\x01") + push(ctype) if ctype is not None else b"") + tags
    if body is not None:
        out += b"\x00" + b"".join(push(body[i:i + chunk]) for i in range(0, len(body), chunk))
    return out + b"\x68"


def tapscript(*envelopes: bytes) -> bytes:
    """<x-only key> OP_CHECKSIG, then the envelopes (ord's reveal script)."""
    return push(b"\x02" * 32) + b"\xac" + b"".join(envelopes)


def witness(script: bytes, annex: bool = False) -> list[str]:
    return [w.hex() for w in [SIG, script, CONTROL] + ([b"\x50annex"] if annex else [])]


def _tx(txid, vin):
    return {"txid": txid, "vin": vin, "vout": [{"n": 0, "value": 0.0001}], "blockhash": "h10"}


def _in(prev, wit=None, script_sig=b""):
    return {"txid": prev, "vout": 0, "scriptSig": {"hex": script_sig.hex()}, **({"txinwitness": wit} if wit else {})}


class Node:
    def __init__(self, txs):
        self.txs, self.calls = txs, []

    def call(self, method, *a):
        self.calls.append((method, *a))
        if method == "getrawtransaction":
            if a[0] not in self.txs:
                raise OSError("no such transaction")
            return self.txs[a[0]]
        if method == "getblockheader":
            return {"height": 10}
        raise AssertionError(method)


A, B, C, P = "a1" * 32, "b2" * 32, "c3" * 32, "f0" * 32
POINTER = push(b"\x02") + push(b"\x00\x01")                             # pointer (2)
PARENT = push(b"\x03") + push(bytes.fromhex(P)[::-1])                  # parent (3)
META = push(b"\x05") + push(b"\xa1\x61x\x01")                          # metadata (5)


def _chain():
    two = tapscript(envelope(b"text/plain;charset=utf-8", b"hello, litecoin", tags=POINTER),
                    envelope(b"image/png", PNG, tags=PARENT + META))
    doginal = push(b"ord") + bytes([0x51]) + push(b"text/plain") + bytes([0x00]) + push(b"doginal") \
        + push(b"\x30" * 71) + push(b"\x51")
    return {
        A: _tx(A, [_in(P, witness(two))]),                                                 # A i0, A i1
        B: _tx(B, [_in(P, ["00" * 64]),                                                    # a key-path spend
                   _in(P, witness(tapscript(envelope(b"text/html", b"<p>second input</p>")), annex=True))]),  # B i0
        C: _tx(C, [_in(P, script_sig=doginal),                                             # C i0: the Doginals kind
                   _in(P, witness(tapscript(envelope(b"application/json", b'{"p":"x"}'))))]),  # C i1
    }


def test_witness_envelopes_give_their_bodies():
    script = tapscript(envelope(b"text/plain", b"hi"), envelope(b"image/png", PNG, tags=POINTER + PARENT + META))
    got = dx.taproot_bodies([SIG, script, CONTROL])
    assert [(e["ctype"], e["body"], e["big"]) for e in got] == [(b"text/plain", b"hi", False),
                                                                 (b"image/png", PNG, False)], "pushes joined, tags passed over"
    assert dx.taproot_bodies([SIG, script, CONTROL, b"\x50annex"]) == got, "an annex is passed over"
    assert dx.taproot_read([SIG, script, CONTROL]) == [(b"text/plain".hex(), None), (b"image/png".hex(), None)], \
        "the block scan reads the same envelopes"
    assert dx.taproot_bodies([SIG, tapscript(envelope(b"text/plain", None)), CONTROL])[0]["body"] is None, "no body tag"
    assert dx.taproot_bodies([SIG, tapscript(envelope(b"text/plain", b"")), CONTROL])[0]["body"] == b"", "an empty one"
    assert dx.taproot_bodies([SIG, tapscript(envelope(None, b"x")), CONTROL])[0]["ctype"] == b"", "no content type"


def test_malformed_witnesses_are_passed_over_safely():
    for w in ([], [SIG], [SIG, b"\x02" * 33], [b"\x50annex"],                     # key-path spends, nothing at all
              [SIG, tapscript(b"\x63" + push(b"ord") + b"\x00" + push(b"x") + b"\x68"), CONTROL],  # no OP_FALSE
              [SIG, tapscript(b"\x00\x63" + push(b"orb") + b"\x00" + push(b"x") + b"\x68"), CONTROL],  # not "ord"
              [SIG, b"\x00\x63\x03or", CONTROL],                                  # a push cut short
              [SIG, b"\x00\x63" + push(b"ord") + b"\x01\x01\x4d\xff\xff", CONTROL]):  # a body push past the end
        assert all(isinstance(e, dict) for e in dx.taproot_bodies(w))
        assert len(dx.taproot_bodies(w)) == len(dx.taproot_read(w)), "the reader and the scan agree"
    unclosed = dx.taproot_bodies([SIG, tapscript(b"\x00\x63" + push(b"ord") + push(b"\x01") + push(b"text/plain")
                                                 + b"\x00" + push(b"no endif")), CONTROL])
    assert [e["body"] for e in unclosed] == [b"no endif"], "as the index counts it: to the script's end"


def test_an_oversized_body_is_not_gathered(monkeypatch):
    monkeypatch.setattr(dx, "TAPROOT_BODY_MAX", 1000)
    e = dx.taproot_bodies([SIG, tapscript(envelope(b"image/png", PNG)), CONTROL])[0]
    assert e["big"] is True and e["body"] is None
    with pytest.raises(ValueError, match="over 1000 bytes"):
        D.content(Node(_chain()), f"{A}i1", taproot=True)
    assert D.content(Node(_chain()), f"{A}i0", taproot=True)[1] == b"hello, litecoin", "the small one beside it"


def test_content_is_read_from_the_witness_numbered_as_the_index_numbers_births():
    node = Node(_chain())
    assert D.content(node, f"{A}i0", taproot=True) == ("text/plain;charset=utf-8", b"hello, litecoin")
    assert D.content(node, f"{A}i1", taproot=True) == ("image/png", PNG), "the second envelope in one input"
    assert D.content(node, f"{B}i0", taproot=True) == ("text/html", b"<p>second input</p>"), "a later input, annex"
    assert D.content(node, f"{C}i0", taproot=True) == ("text/plain", b"doginal"), "the Doginals kind first, as before"
    assert D.content(node, f"{C}i1", taproot=True) == ("application/json", b'{"p":"x"}'), "then the witness"
    assert [m for m, *_ in node.calls if m != "getrawtransaction"] == ["getblockheader"], \
        "a witness inscription is one call: its transaction (the Doginals kind still asks its block, as before)"
    for missing in (f"{A}i2", f"{B}i1", f"{C}i2"):
        with pytest.raises(ValueError):
            D.content(node, missing, taproot=True)
    with pytest.raises(OSError):
        D.content(node, f"{'99' * 32}i0", taproot=True)


def test_ids_line_up_with_the_index_births():
    from arcade import rawchain
    for txid, tx in _chain().items():
        t = rawchain.Tx(txid=txid, wtxid="", raw=b"", version=2, locktime=0, stripped_size=0, vout=[], vin=[
            {"txid": i["txid"], "vout": 0, "script": bytes.fromhex(i["scriptSig"]["hex"]), "sequence": 0,
             "witness": [bytes.fromhex(w) for w in i.get("txinwitness") or []]} for i in tx["vin"]])
        for b in dx.births_of(t, taproot=True):
            assert D.content(Node(_chain()), f"{txid}i{b['n']}", taproot=True)[0].encode().hex() == b["ctype"]


def test_a_chain_without_taproot_reads_as_before():
    node = Node(_chain())
    with pytest.raises(ValueError, match="no ord envelope"):
        D.content(node, f"{A}i0")
    assert D.content(node, f"{C}i0") == ("text/plain", b"doginal")


def test_no_body_tag_has_no_content():
    t = "d4" * 32
    node = Node({t: _tx(t, [_in(P, witness(tapscript(envelope(b"text/plain", None))))])})
    with pytest.raises(ValueError, match="no body"):
        D.content(node, f"{t}i0", taproot=True)


def _guard(tmp_path, txs, taproot=True):
    node = Node(txs)

    class Rpc:
        def __enter__(self):
            return node

        def __exit__(self, *a):
            return False
    return D.Doginals(tmp_path / "ltc-inscriptions.sqlite", fetch=lambda t: {"txid": t, "inscriptions": []},
                      rpc_factory=Rpc, taproot=taproot), node


def test_the_guard_reads_taproot_content_and_keeps_it(tmp_path):
    guard, node = _guard(tmp_path, _chain())
    assert guard.content(f"{A}i1") == ("image/png", PNG)
    first = len(node.calls)            # "is it on this chain?" (2026-10-28), then the read
    assert guard.content(f"{A}i1") == ("image/png", PNG)
    assert len(node.calls) == first == 2, "kept on disk after the first read"
    assert guard.ctype_of(f"{A}i1") == "image/png"
    (tmp_path / "doge").mkdir()
    plain, _ = _guard(tmp_path / "doge", _chain(), taproot=False)
    with pytest.raises(ValueError):
        plain.content(f"{B}i0")


def test_content_route_serves_a_taproot_inscription(client, monkeypatch, tmp_path):
    app, _state = client
    guard, _ = _guard(tmp_path, _chain())
    monkeypatch.setattr(funding, "GUARD", None)
    monkeypatch.setattr(funding, "GUARDS", [(lambda address: False, guard)])
    monkeypatch.setattr(funding, "GUARD_BY_NET", {"ltc": guard})
    got = app.get(f"/content/{A}i1")
    assert got.status_code == 200, got.text
    assert got.content == PNG and got.headers["content-type"] == "image/png"
    text = app.get(f"/content/{A}i0")
    assert text.status_code == 200 and text.content == b"hello, litecoin"
    assert text.headers["content-type"].startswith("text/plain")
    assert app.get(f"/content/{A}i5").status_code == 404, "no such envelope"

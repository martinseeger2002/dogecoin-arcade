"""Sending a DRC-20 / PRC-20 token (arcade/token20_send.py, 2026-10-22: "the ability to send a PRC 20 token in
the same way I can send any other token" ... "needs to match that of Pepe block and Nintondo"): the note written byte
for byte as the frgy notes @apple received, and a send built here walked by our own index as the explorers count it."""
import pytest

from arcade import doginal_index as dx
from arcade import funding, ord_rules, rawchain, token20_send as t20
from arcade.script import b58check_encode, hash160
from arcade.txbuild import p2pkh_script

from test_doginal_index import FUND, SIG, Chain, _tx, _tx_to  # noqa: E402
from test_ord_rules import TEXT, _drc, _json, _walk  # noqa: E402

PUBKEY = bytes.fromhex("02" + "11" * 32)


@pytest.fixture(autouse=True)
def small(monkeypatch):
    monkeypatch.setattr(dx, "CHUNK", 4)
    monkeypatch.setattr(dx, "SETTLE", 0)
    monkeypatch.setattr(dx, "WALK_SETTLE", 0)


def test_the_note_is_the_frgy_notes_bytes():
    """ddc29df9...i0, one of the five notes that took 50,000 frgy to @apple (block 778,863, sent at 1,261,581): its
    scriptSig began with exactly these bytes, and pepeblocks counts it."""
    content = t20.note("prc-20", "frgy", "10000")
    assert content == b'{"p":"prc-20","op":"transfer","tick":"frgy","amt":"10000"}'
    pushes, redeem = t20.envelope(content, PUBKEY)
    assert b"".join(pushes) == b"\x03ordQ\x18text/plain;charset=utf-8\x00:" + content
    assert redeem == b"\x21" + PUBKEY + b"\xad" + b"\x75" * len(pushes) + b"\x51"


def test_an_amount_is_read_as_typed_and_refused_past_its_decimals():
    assert t20.amount_text("10000", 18) == ("10000", 10000 * 10 ** 18)
    assert t20.amount_text("0.50", 2) == ("0.5", 50)
    assert t20.amount_text("1e3", 0) == ("1000", 1000)
    for bad in ("", "0", "-1", "abc", "1.234"):
        with pytest.raises(funding.FundingError):
            t20.amount_text(bad, 2)


def test_a_send_built_here_moves_the_tokens_as_the_indexers_count_it(tmp_path):
    """Deploy and mint to a holder, then this module's reveal (its envelope, signature and redeem script, assembled by
    funding.assemble) lands the note on the holder, and a send moves that coin: the receiver has the amount."""
    holder = b58check_encode(30, hash160(PUBKEY))
    chain, txs = Chain(), []
    _drc(txs, chain, {"p": "drc-20", "op": "deploy", "tick": "abcd", "max": "1000", "lim": "1000"}, 0, b"d")
    chain.add(txs)
    mint = _tx_to([(FUND, 1, _envelope_of({"p": "drc-20", "op": "mint", "tick": "abcd", "amt": "700"}))],
                  [(100_000, p2pkh_script(holder))])
    chain.add([mint])
    index = _walk(tmp_path, chain)
    commit = {"txid": FUND, "vout": 2, "value": 5_000_000}
    pushes, redeem = t20.envelope(t20.note("drc-20", "abcd", "250"), PUBKEY)
    params = type("P", (), {"scripthash_version": 22})()
    unsigned = t20.build_reveal(params, commit, pushes, redeem, holder)
    assert unsigned.inputs[0]["envelope"] == b"".join(pushes).hex()
    raw = bytes.fromhex(funding.assemble(unsigned, ["30" * 71], PUBKEY))
    reveal = rawchain.read_tx(raw).txid
    assert rawchain.read_tx(raw).vout[0] == (t20.POSTAGE, p2pkh_script(holder))
    chain.add([(raw, reveal)])
    index.step()
    tok = {b["owner"]: (b["balance"], b["transferable"]) for b in index.token("abcd")["top"]}
    assert tok[holder] == ("700", "250"), "the note set 250 aside on the holder"
    carol = b58check_encode(30, hash160(bytes.fromhex("03" + "22" * 32)))
    send = _tx_to([(reveal, 0, SIG)], [(t20.POSTAGE, p2pkh_script(carol))])
    chain.add([send])
    index.step()
    tok = {b["owner"]: (b["balance"], b["transferable"]) for b in index.token("abcd")["top"]}
    assert tok[holder] == ("450", "0") and tok[carol] == ("250", "0"), tok


def _envelope_of(op):
    from test_doginal_index import _envelope
    return _envelope(TEXT, parts=((0, _json(op)),))


def test_a_note_on_its_way_never_pays_and_other_small_coins_do(tmp_path, monkeypatch):
    """A note's reveal lands its note on the sender's own address, unconfirmed: no send may take it to pay a fee. Only
    that coin: a take's own small coins on their way still pay (2026-10-22)."""
    from arcade.db import Database
    from arcade.state import install_schema
    db = Database(tmp_path / "l.sqlite")
    install_schema(db)
    address = b58check_encode(30, hash160(PUBKEY))
    monkeypatch.setattr(funding, "guard_for", lambda a: type("G", (), {"spendable": lambda self, c: c})())
    note = {"txid": "ab" * 32, "vout": 0, "value": t20.POSTAGE, "address": address}
    big = {"txid": "cd" * 32, "vout": 1, "value": 300_000_000, "address": address}
    small = {"txid": "ef" * 32, "vout": 2, "value": 1_000_000, "address": address}
    monkeypatch.setattr(funding, "NOTE_COINS", {("ab" * 32, 0)})
    assert funding.choose(db, address, 50_000, extra=[note, big]) == [big]
    with pytest.raises(funding.FundingError):
        funding.choose(db, address, 50_000, extra=[note])
    assert funding.choose(db, address, 50_000, extra=[small]) == [small], "any other small coin on its way still pays"

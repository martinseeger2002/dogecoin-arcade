"""MoonBrix on Kotia, read the way tbuuol's own Kurator reads it (arcade/moonbrix.py, 2026-11-10).

The first test replays every MoonBrix command Kotia has carried and must come to exactly the five files MoonBrix
itself wrote from them. The others hold the rules that decide what counts, and the quirks the arcade copies on
purpose (moonbrix.QUIRKS)."""
import json
import pathlib
import sys

import pytest

from arcade import moonbrix as M, rawchain
from arcade.config import NETWORKS
from arcade.script import b58check_encode
from arcade.txbuild import p2pkh_script, varint

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_utxoset import tx, txid                              # noqa: E402

KOT = NETWORKS["kot"]
FIXTURE = json.loads((pathlib.Path(__file__).parent / "fixtures" / "moonbrix_kotia.json").read_text())
A, B, C = (b58check_encode(46, bytes([n]) * 20) for n in (1, 2, 3))
TLD = b"\x00MoonBrix".hex()
SPACE = TLD + "13374284"


def replay(commands):
    doc = M.empty()
    for c in commands:
        outputs = c["outputs"]
        M.Kurator(doc).apply(c["height"], c["txid"], c["chunk"], lambda c=c: c["origin"],
                             lambda i, o=outputs: o[i] if i < len(o) else None, len(outputs))
    return doc


def test_every_moonbrix_command_on_kotia_comes_to_the_files_moonbrix_wrote():
    doc = replay(FIXTURE["commands"])
    for name, want in FIXTURE["moonbrix"].items():
        assert json.dumps(doc[name], sort_keys=True) == json.dumps(want, sort_keys=True), name


def test_kotia_and_only_kotia_reads_moonbrix():
    assert {n for n, p in NETWORKS.items() if p.moonbrix} == {"kot"}
    assert KOT.full_utxo_set, "the unspent set says which blocks carry data"


# --- transactions, as the chain carries them -------------------------------------------------------------------

def op_return(hexs: str) -> bytes:
    data = bytes.fromhex(hexs)
    if len(data) < 0x4C:
        push = bytes([len(data)])
    elif len(data) <= 0xFF:
        push = b"\x4c" + bytes([len(data)])
    else:
        push = b"\x4d" + len(data).to_bytes(2, "little")
    return b"\x6a" + push + data


def head(space: str, action: str, sub: str = "00", data: str = "") -> str:
    return space + action + "00" + sub + data


def command_tx(chunk: str, spends: str, pays=(), first=None):
    """A transaction whose vout[0] carries `chunk` (or `first`, a script of its own), spending `spends`:0."""
    outs = [(0, first if first is not None else op_return(chunk)), (100_000, p2pkh_script(A))]
    outs += [(100_000, p2pkh_script(p)) for p in pays]
    raw = tx([(spends, 0)], outs)
    return rawchain.read_tx(raw, txid(raw), timed=True)


class Coins:
    """spent_output: every coin `<n>`*32 is paid to the n-th address of (A, B, C)."""
    def __call__(self, txid, vout):
        return p2pkh_script((A, B, C)[int(txid[0]) - 1]), 100_000


FROM_A, FROM_B, FROM_C = "1" * 64, "2" * 64, "3" * 64


def store(tmp_path):
    return M.MoonBrix(tmp_path / "kot-moonbrix.sqlite", KOT)


def activate(mb, height=10):
    mb.connect(height, "aa" * 32, [command_tx(M.ACTIVATION + b"hi".hex(), FROM_A)], Coins())


def test_nothing_counts_before_the_activation_header(tmp_path):
    mb = store(tmp_path)
    mb.connect(5, "05" * 32, [command_tx(head(TLD + "00000000", M.CLAIM), FROM_A)], Coins())
    assert mb.document() == M.empty()
    activate(mb)
    assert [e["Event"] for e in mb.document()["lib"]] == ["MoonBrix GENESIS"]
    assert mb.document()["lib"][0]["Message"] == "hi"


def test_a_tld_is_claimed_at_its_first_ip_and_its_owner_then_claims_ips(tmp_path):
    mb = store(tmp_path)
    activate(mb)
    mb.connect(11, "11" * 32, [command_tx(head(TLD + "00000000", M.CLAIM), FROM_A)], Coins())
    mb.connect(12, "12" * 32, [command_tx(head(SPACE, M.CLAIM), FROM_A)], Coins())
    mb.connect(13, "13" * 32, [command_tx(head(SPACE, M.CLAIM), FROM_B)], Coins())     # taken already
    doc = mb.document()
    assert [(t["Ascii"], t["Owner"]) for t in doc["tld"]] == [("MoonBrix", A)]
    assert doc["tld"][0]["IPs"] == [["13374284", A, ""]]
    assert doc["fold"] == [{"Key": SPACE, "Owner": A, "root": {}}]


def test_only_the_first_command_of_a_block_counts(tmp_path):
    mb = store(tmp_path)
    activate(mb)
    first = command_tx(head(TLD + "00000000", M.CLAIM), FROM_A)
    second = command_tx(head(b"\x00Another".hex() + "00000000", M.CLAIM), FROM_B)
    mb.connect(11, "11" * 32, [first, second], Coins())
    assert [t["Ascii"] for t in mb.document()["tld"]] == ["MoonBrix"]


def test_a_data_output_that_is_not_vout_0_or_not_pushes_only_is_no_command(tmp_path):
    mb = store(tmp_path)
    activate(mb)
    claim = head(TLD + "00000000", M.CLAIM)
    raw = tx([(FROM_A, 0)], [(100_000, p2pkh_script(A)), (0, op_return(claim))])
    later = rawchain.read_tx(raw, txid(raw), timed=True)
    not_pushes = command_tx(claim, FROM_A, first=op_return(claim) + b"\x76")      # OP_DUP after the push
    mb.connect(11, "11" * 32, [later], Coins())
    mb.connect(12, "12" * 32, [not_pushes], Coins())
    assert mb.document()["tld"] == []


def test_a_slot_taken_by_something_that_is_not_a_command_shuts_the_block(tmp_path):
    """Two pushes ("OP_RETURN <a> <b>") still take the block's slot (MasterDB keeps it) but are read as nothing."""
    mb = store(tmp_path)
    activate(mb)
    claim = head(TLD + "00000000", M.CLAIM)
    two = command_tx("", FROM_A, first=op_return(claim)[:] + bytes([16]) + b"\x01" * 16)
    mb.connect(11, "11" * 32, [two, command_tx(claim, FROM_B)], Coins())
    assert mb.document()["tld"] == []


def test_the_sender_is_whoever_the_first_input_spends_from(tmp_path):
    mb = store(tmp_path)
    activate(mb)
    mb.connect(11, "11" * 32, [command_tx(head(TLD + "00000000", M.CLAIM), FROM_C)], Coins())
    assert mb.document()["tld"][0]["Owner"] == C


def property_command(define="00", name="MoonBrix", ticker="MBX", supply=1000):
    return head(SPACE, M.PROPERTY, "00", define + name.encode().hex().ljust(42, "0")
                + ticker.encode().hex().ljust(10, "0") + f"{supply:016x}")


def token_command(sub, ticker, amount):
    return head(SPACE, M.TOKEN, sub, ticker.encode().hex().ljust(10, "0") + f"{amount:016x}")


def test_a_token_moves_to_vout_2_and_is_minted_by_its_owner(tmp_path):
    mb = store(tmp_path)
    activate(mb)
    mb.connect(11, "11" * 32, [command_tx(property_command(), FROM_A)], Coins())
    mb.connect(12, "12" * 32, [command_tx(token_command("00", "mbx", 300), FROM_A, pays=[B])], Coins())
    mb.connect(13, "13" * 32, [command_tx(token_command("00", "MBX", 301), FROM_B, pays=[C])], Coins())   # too much
    mb.connect(14, "14" * 32, [command_tx(token_command("01", "MBX", 5), FROM_A)], Coins())
    mb.connect(15, "15" * 32, [command_tx(token_command("01", "MBX", 5), FROM_B)], Coins())               # not owner
    prop = mb.document()["prop"][0]
    assert prop["Holder"] == {A: 705, B: 300}
    assert prop["Supply"] == 1005, "a fixed supply is still minted by its owner (a quirk MoonBrix has)"
    assert M.balances(mb.document(), B) == [{"ticker": "MBX", "name": "MoonBrix", "amount": 300, "key": SPACE,
                                             "divisible": False, "fungible": True}]
    assert [e["Action"] for e in mb.document()["lib"][-2:]] == ["move", "move"], "a mint is logged as a move"


def test_names_keep_letters_only_and_a_ticker_or_name_is_had_once(tmp_path):
    mb = store(tmp_path)
    activate(mb)
    mb.connect(11, "11" * 32, [command_tx(property_command(name="Moon Brix 2", ticker="m1x"), FROM_A)], Coins())
    mb.connect(12, "12" * 32, [command_tx(property_command(name="Other", ticker="MX"), FROM_B)], Coins())
    assert [(p["Name"], p["Ticker"]) for p in mb.document()["prop"]] == [("MoonBrix", "MX")]


def test_a_meta_inscription_counts_with_next_false_and_its_chunks_make_the_file(tmp_path):
    data = b"\xff\xd8 a picture"
    chunk_tx = command_tx(head(SPACE, M.INSCRIBE, "00", data.hex()), FROM_A)
    meta = {"name": "Pic", "type": "image/jpg", "next": False, "chunks": [chunk_tx.txid]}
    meta_tx = command_tx(head(SPACE, M.INSCRIBE, "01", "00" + json.dumps(meta).encode().hex()), FROM_A)
    nulled = command_tx(head(SPACE, M.INSCRIBE, "01", "00" + json.dumps({**meta, "next": None}).encode().hex()),
                        FROM_A)
    mb = store(tmp_path)
    activate(mb)
    mb.connect(11, "11" * 32, [meta_tx], Coins())
    mb.connect(12, "12" * 32, [nulled], Coins())
    assert mb.document()["ins"] == [{"TXID": meta_tx.txid, "Name": "Pic", "Type": "image/jpg"}]
    assert [s["txid"] for s in mb.skipped()] == [nulled.txid], "MoonBrix itself stops at next: null"

    class Rpc:
        raws = {t.txid: t.raw.hex() for t in (chunk_tx, meta_tx)}

        def call(self, method, txid, _verbose):
            return self.raws[txid]

        def batch(self, calls):
            return [self.raws[args[0]] for _m, args in calls]

    assert M.inscription(Rpc(), KOT, meta_tx.txid) == ("image/jpg", data)


def test_folders_and_files_go_where_kurator_puts_them(tmp_path):
    mb = store(tmp_path)
    activate(mb)
    mb.connect(11, "11" * 32, [command_tx(head(TLD + "00000000", M.CLAIM), FROM_A)], Coins())
    mb.connect(12, "12" * 32, [command_tx(head(SPACE, M.CLAIM), FROM_A)], Coins())
    name = lambda word: word.encode().hex().ljust(30, "0")                       # noqa: E731
    mb.connect(13, "13" * 32, [command_tx(head(SPACE, M.FOLDER, "00", name("www")), FROM_A)], Coins())
    mb.connect(14, "14" * 32, [command_tx(head(SPACE, M.FILE, "00", name("Genesis") + name("www") + "ab" * 32),
                                          FROM_A)], Coins())
    mb.connect(15, "15" * 32, [command_tx(head(SPACE, M.FOLDER, "00", name("Pics") + name("www")), FROM_A)], Coins())
    mb.connect(16, "16" * 32, [command_tx(head(SPACE, M.FILE, "00", name("M")), FROM_A)], Coins())
    mb.connect(17, "17" * 32, [command_tx(head(SPACE, M.FILE, "01", name("M")), FROM_A)], Coins())
    fold = mb.document()["fold"][0]
    assert fold["root"] == {"www": {"Genesis": "ab" * 32}}
    assert fold["www"] == {"Pics": {}}, "a folder made outside root goes beside it (Kurator::folder)"


def test_a_reorg_rolls_the_document_back(tmp_path):
    mb = store(tmp_path)
    activate(mb)
    mb.connect(11, "11" * 32, [command_tx(head(TLD + "00000000", M.CLAIM), FROM_A)], Coins())
    mb.roll_back(10, "aa" * 32)
    assert mb.document()["tld"] == [] and mb.height == 10
    mb.connect(11, "b1" * 32, [command_tx(head(TLD + "00000000", M.CLAIM), FROM_B)], Coins())
    assert mb.document()["tld"][0]["Owner"] == B


def test_follow_reads_the_sets_data_blocks_and_rolls_back_a_reorg(tmp_path):
    claim_a = command_tx(head(TLD + "00000000", M.CLAIM), FROM_A)
    claim_b = command_tx(head(TLD + "00000000", M.CLAIM), FROM_B)
    genesis = command_tx(M.ACTIVATION, FROM_A)
    chain = {10: "aa" * 32, 11: "bb" * 32, 12: "cc" * 32}
    raws = {t.txid: t.raw.hex() for t in (claim_a, claim_b, genesis)}

    class Set:
        height, best = 12, chain[12]
        blocks = {10: [genesis.txid], 11: [claim_a.txid]}

        def data_blocks(self, since=0):
            return [(h, t) for h, t in sorted(self.blocks.items()) if h >= since]

    class Rpc:
        def call(self, method, *args):
            if method == "getblockhash":
                return chain[args[0]]
            return p2pkh_like(args[0])

        def batch(self, calls):
            return [raws[args[0]] for _m, args in calls]

    def p2pkh_like(spent):                                  # getrawtransaction <spent> 1, for the sender
        who = (A, B, C)[int(spent[0]) - 1]
        return {"vout": [{"scriptPubKey": {"hex": p2pkh_script(who).hex()}, "value": 0.001}]}

    mb, uset = store(tmp_path), Set()
    assert M.follow(mb, uset, Rpc(), KOT) == 2
    assert mb.height == 12 and mb.document()["tld"][0]["Owner"] == A
    chain[11], chain[12] = "b2" * 32, "c2" * 32               # block 11 left the chain; B's claim is there instead
    uset.blocks, uset.best = {10: [genesis.txid], 11: [claim_b.txid]}, chain[12]
    assert M.follow(mb, uset, Rpc(), KOT) == 1
    assert mb.document()["tld"][0]["Owner"] == B


def test_every_quirk_is_named_for_tbuuol():
    assert len(M.QUIRKS) >= 10 and all(q.endswith(")") or q.endswith(".") for q in M.QUIRKS)


@pytest.mark.parametrize("hexs,want", [("4d6f6f6e", "Moon"), ("4d20 31".replace(" ", ""), "M"), ("", "")])
def test_text_keeps_letters_as_helper_does(hexs, want):
    assert M.text(hexs) == want
    assert M.text("4d2031", space=True) == "M "


# --- the pages ---------------------------------------------------------------------------------------------------

from test_web import app_state, client                          # noqa: F401,E402


def test_the_moonbrix_page_and_its_data_show_what_kurator_read(client, monkeypatch):
    from arcade.web.state import ChainContext
    app, state = client
    kot = ChainContext(network="kot", role="ledger", label="Kotia", conf=pathlib.Path("/nonexistent/kotia.conf"))
    monkeypatch.setattr(state, "more", {"kot": kot}, raising=False)
    mb = state.moonbrix(kot)
    doc = replay(FIXTURE["commands"])
    with mb.conn:
        mb._set(1_539_161, "ab" * 32, doc)
    page = app.get("/moonbrix")
    assert page.status_code == 200
    assert "MBX · MoonBrix" in page.text and ".MoonBrix" in page.text and "TestInscription" in page.text
    data = app.get("/moonbrix.json").json()
    assert data["height"] == 1_539_161 and [p["Ticker"] for p in data["prop"]] == ["MBX", "MONO"]
    assert app.get("/moonbrix/content/" + "zz" * 32).status_code in (400, 404)
    assert app.get("/moonbrix/content/" + "ab" * 32).status_code == 404, "only an inscription MoonBrix has"
    kot.borrowed = True
    assert "borrowed" in app.get("/moonbrix").text
    assert app.get("/moonbrix.json").status_code == 404


def test_moonbrix_is_public_and_claimed_by_the_tokens_module():
    from arcade import modules
    from arcade.web import door
    for path in ("/moonbrix", "/moonbrix.json", "/moonbrix/content/" + "ab" * 32):
        assert door.public_path(path), path
    assert not door.public_path("/moonbrix/content/../admin")
    claimed = {r for m in modules._MODULES if m.id == "tokens" for r in m.routes}
    assert {"/moonbrix", "/moonbrix.json", "/account/moonbrix"} <= claimed

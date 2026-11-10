"""MoonBrix 2 on Kotia (arcade/moonbrix.py, arcade/web/docs/moonbrix.md, tbuuol 2026-11-10).

Each test holds a rule of the protocol page: which transaction is a command, who sends it and to whom, that a command
is applied whole or rejected whole and logged, and what each command may do."""
import hashlib
import pathlib
import sys

import pytest

from arcade import moonbrix as M, rawchain
from arcade.config import NETWORKS
from arcade.script import b58check_encode
from arcade.txbuild import p2pkh_script

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_utxoset import tx, txid                              # noqa: E402

KOT = NETWORKS["kot"]
A, B, C = (b58check_encode(46, bytes([n]) * 20) for n in (1, 2, 3))
FROM_A, FROM_B, FROM_C = "1" * 64, "2" * 64, "3" * 64
H = M.ACTIVATION
E = M.encode
HOME = 0x13374284                                               # 19.55.66.132


class Coins:
    """spent_output: every coin `<n>`*32 is paid to the n-th address of (A, B, C)."""
    def __call__(self, txid, vout):
        return p2pkh_script((A, B, C)[int(txid[0]) - 1]), 100_000


def sender_of(spends):
    return (A, B, C)[int(spends[0]) - 1]


def command_tx(data: bytes, spends: str, pays=(), change_first=False, scripts=None):
    """A transaction carrying `data` in its OP_RETURN outputs, change to the sender (one per data output, as Kotia
    relays it) after them or before them, and then `pays` (an address, or (address, koinu))."""
    datas = [(0, s) for s in (scripts if scripts is not None else M.data_scripts(data))]
    change = [(100_000, p2pkh_script(sender_of(spends)))] * max(1, len(datas))
    paid = [(p[1], p2pkh_script(p[0])) if isinstance(p, tuple) else (100_000, p2pkh_script(p)) for p in pays]
    outs = (change + datas + paid) if change_first else (datas + change + paid)
    raw = tx([(spends, 0)], outs)
    return rawchain.read_tx(raw, txid(raw), timed=True)


class Chain:
    """A MoonBrix store fed one block at a time, with every transaction it was given there to fetch."""

    def __init__(self, tmp_path):
        self.mb = M.MoonBrix(tmp_path / "kot-moonbrix.sqlite", KOT)
        self.height = H - 1
        self.txs = {}

    def block(self, *txs):
        self.height += 1
        for t in txs:
            self.txs[t.txid] = t
        self.mb.connect(self.height, f"{self.height:064x}", txs, Coins(), self.txs.__getitem__)
        return [e for e in self.mb.document()["log"] if e["height"] == self.height]

    def one(self, data, spends=FROM_A, **kw):
        t = command_tx(data, spends, **kw)
        return self.block(t)[-1], t

    def ok(self, data, spends=FROM_A, **kw):
        return self.one(data, spends, **kw)[0]["ok"]

    @property
    def doc(self):
        return self.mb.document()


@pytest.fixture
def chain(tmp_path):
    return Chain(tmp_path)


def space(chain, tld="moon", at=HOME, by=FROM_A):
    assert chain.ok(E("claim tld", tld, M.WHITELIST), by)
    assert chain.ok(E("claim space", tld, at), by)
    return M.space_key(tld, at)


# --- which transaction is a command ------------------------------------------------------------------------------

def test_only_magic_and_version_2_make_a_command_and_reading_starts_at_the_activation(chain):
    chain.mb.connect(H - 1, "00" * 32, [command_tx(E("claim tld", "early", 0), FROM_A)], Coins())
    assert chain.doc["tlds"] == {}
    other = E("claim tld", "later", 0).replace(b"MB\x02", b"MB\x03", 1)
    prototype = bytes.fromhex("004d6f6f6e4272697800000000000000")         # the prototype's TLD claim
    assert chain.block(command_tx(other, FROM_A), command_tx(prototype, FROM_A)) == []
    assert chain.doc["tlds"] == {}


def test_every_command_of_a_block_counts_in_block_order(chain):
    log = chain.block(command_tx(E("claim tld", "moon", 0), FROM_A), command_tx(E("claim tld", "moon", 0), FROM_B),
                      command_tx(E("claim tld", "sun", 0), FROM_B))
    assert [e["ok"] for e in log] == [True, False, True]
    assert log[1]["why"] == "moon is taken"
    assert chain.doc["tlds"]["moon"]["owner"] == A and chain.doc["tlds"]["sun"]["owner"] == B


def test_the_payload_runs_on_over_every_data_output(chain):
    data = bytes(range(256)) * 200                                   # 51,200 bytes: four OP_RETURN outputs
    scripts = M.data_scripts(E("inscribe", "application/octet-stream", "big", data))
    assert len(scripts) == 4 and all(len(s) <= 15_000 for s in scripts)
    entry, t = chain.one(b"", scripts=scripts)
    assert entry["ok"], entry
    assert M.content(chain.doc, chain.txs.__getitem__, t.txid) == ("application/octet-stream", data)


def test_a_data_output_that_is_not_data_pushes_only_rejects_the_command(chain):
    good = M.data_scripts(E("claim tld", "moon", 0))[0]
    entry, _t = chain.one(b"", scripts=[good, b"\x6a\x51"])                  # OP_RETURN OP_1
    assert not entry["ok"] and "data pushes" in entry["why"] and chain.doc["tlds"] == {}


def test_the_target_is_the_first_output_paying_someone_but_the_sender(chain):
    chain.one(E("claim tld", "moon", 0))
    entry, _t = chain.one(E("give tld", "moon"), pays=(B, C), change_first=True)
    assert entry["ok"] and chain.doc["tlds"]["moon"]["owner"] == B
    entry, _t = chain.one(E("give tld", "moon"), FROM_B)
    assert not entry["ok"] and entry["why"] == "no output pays an address other than the sender's"
    assert chain.doc["tlds"]["moon"]["owner"] == B


@pytest.mark.parametrize("data,why", [
    (E("claim tld", "moon", 0) + b"\x00", "bytes after the last field"),
    (E("claim tld", "MoonBrix", 0), "a TLD is"),
    (E("claim tld", "moon", 2), "a list mode is"),
    (b"MB\x02\x01\x84\x00", "shortest form"),
    (b"MB\x02\x01\x09moon", "ends too early"),
    (b"MB\x02\x99", "not MoonBrix 2's"),
])
def test_a_command_that_breaks_a_rule_is_rejected_whole_and_logged(chain, data, why):
    entry, _t = chain.one(data)
    assert not entry["ok"] and why in entry["why"]
    assert chain.doc["tlds"] == {}


# --- TLDs and spaces ---------------------------------------------------------------------------------------------

def test_a_whitelist_lets_its_list_claim_and_a_blacklist_everybody_else(chain):
    chain.one(E("claim tld", "moon", M.WHITELIST))
    assert not chain.ok(E("claim space", "moon", 1), FROM_B)
    chain.one(E("list add", "moon"), pays=(B,))
    assert chain.ok(E("claim space", "moon", 1), FROM_B)
    assert not chain.ok(E("claim space", "moon", 2), FROM_C)
    chain.one(E("set list mode", "moon", M.BLACKLIST))
    assert chain.ok(E("claim space", "moon", 2), FROM_C)
    assert not chain.ok(E("claim space", "moon", 3), FROM_B), "B is on the list: barred now"
    assert chain.ok(E("claim space", "moon", 3)), "the owner always may"
    assert not chain.ok(E("claim space", "moon", 3), FROM_C), "a space is had once"
    assert {k: s["owner"] for k, s in chain.doc["spaces"].items()} == {
        "0.0.0.1.moon": B, "0.0.0.2.moon": C, "0.0.0.3.moon": A}


def test_only_the_tlds_owner_changes_its_list(chain):
    chain.one(E("claim tld", "moon", 0))
    assert not chain.ok(E("list add", "moon"), FROM_B, pays=(C,))
    assert chain.ok(E("list add", "moon"), pays=(C,))
    assert not chain.ok(E("list add", "moon"), pays=(C,)), "on the list already"
    assert chain.ok(E("list remove", "moon"), pays=(C,))
    assert chain.doc["tlds"]["moon"]["list"] == []


# --- properties --------------------------------------------------------------------------------------------------

def test_only_the_spaces_owner_creates_a_property_and_a_fixed_one_is_never_minted(chain):
    key = space(chain)
    assert not chain.ok(E("create property", "moon", HOME, "MBX", "MoonBrix", M.FIXED, 8, 1000), FROM_B)
    assert chain.ok(E("create property", "moon", HOME, "MBX", "MoonBrix", M.FIXED, 8, 1000))
    prop = chain.doc["properties"]["MBX"]
    assert prop["space"] == key and prop["holders"] == {A: 1000} and prop["locked"]
    entry = chain.one(E("mint", "MBX", 5))[0]
    assert not entry["ok"] and "only a managed property is minted" in entry["why"]
    assert not chain.ok(E("create property", "moon", HOME, "MBX", "Again", M.MANAGED, 0, 0)), "a ticker is had once"
    assert not chain.ok(E("create property", "moon", HOME, "ZZZ", "Zero", M.FIXED, 0, 0))
    assert not chain.ok(E("create property", "moon", HOME, "NFTX", "Bad", M.NFT, 2, 0))


def test_a_managed_property_is_minted_by_its_owner_until_locked(chain):
    space(chain)
    chain.one(E("create property", "moon", HOME, "ORE", "Iron ore", M.MANAGED, 0, 0))
    assert chain.ok(E("mint", "ORE", 50), pays=(B,))
    assert chain.ok(E("mint", "ORE", 7)), "no target: to the sender"
    assert not chain.ok(E("mint", "ORE", 1), FROM_B), "only the owner mints"
    assert chain.ok(E("lock property", "ORE"))
    assert not chain.ok(E("mint", "ORE", 1))
    prop = chain.doc["properties"]["ORE"]
    assert prop["supply"] == 57 and prop["holders"] == {B: 50, A: 7}


def test_tokens_are_sent_and_burnt_only_from_what_the_sender_holds(chain):
    space(chain)
    chain.one(E("create property", "moon", HOME, "MBX", "MoonBrix", M.FIXED, 0, 100))
    assert chain.ok(E("send", "MBX", 60), pays=(B,))
    assert not chain.ok(E("send", "MBX", 41), pays=(B,))
    assert not chain.ok(E("send", "MBX", 0), pays=(B,))
    assert chain.ok(E("burn", "MBX", 10), FROM_B)
    prop = chain.doc["properties"]["MBX"]
    assert prop["holders"] == {A: 40, B: 50} and prop["supply"] == 90


def test_an_nft_has_numbered_items_that_move_whole_and_are_never_burnt(chain):
    space(chain)
    picture = chain.one(E("inscribe", "image/png", "sword", b"\x89PNG..."))[1].txid
    chain.one(E("create property", "moon", HOME, "SWORD", "Swords", M.NFT, 0, 0))
    assert chain.ok(E("mint nft", "SWORD", picture), pays=(B,))
    assert chain.ok(E("mint nft", "SWORD", None))
    assert not chain.ok(E("mint nft", "SWORD", "ab" * 32)), "an inscription that exists"
    assert not chain.ok(E("send nft", "SWORD", 1), pays=(C,)), "A does not hold #1"
    assert chain.ok(E("send nft", "SWORD", 1), FROM_B, pays=(C,))
    assert not chain.ok(E("burn", "SWORD", 1), FROM_C)
    items = chain.doc["properties"]["SWORD"]["items"]
    assert items == {"1": {"holder": C, "inscription": picture}, "2": {"holder": A, "inscription": None}}
    assert M.balances(chain.doc, C) == [{"ticker": "SWORD", "name": "Swords", "kind": "nft", "items": [1]}]


# --- inscriptions, folders and files -----------------------------------------------------------------------------

def test_an_assembled_inscription_needs_parts_from_before_of_the_size_and_hash_it_names(chain):
    one, two = b"first half,", b" second half"
    p1 = chain.one(E("part", one))[1].txid
    p2 = chain.one(E("part", two), FROM_B)[1].txid
    whole = one + two + one
    sha = hashlib.sha256(whole).hexdigest()
    assert not chain.ok(E("assemble", "text/plain", "x", len(whole), "00" * 32, [p1, p2, p1]))
    assert not chain.ok(E("assemble", "text/plain", "x", len(whole) + 1, sha, [p1, p2, p1]))
    late = command_tx(E("part", b"late"), FROM_C)
    early = command_tx(E("assemble", "text/plain", "x", 4, hashlib.sha256(b"late").hexdigest(), [late.txid]), FROM_A)
    assert [e["ok"] for e in chain.block(early, late)] == [False, True], "a part comes before its assemble"
    entry, t = chain.one(E("assemble", "text/plain", "story", len(whole), sha, [p1, p2, p1]))
    assert entry["ok"]
    assert M.content(chain.doc, chain.txs.__getitem__, t.txid) == ("text/plain", whole)


def test_folders_and_files_are_never_overwritten_but_by_a_replace(chain):
    key = space(chain)
    a = chain.one(E("inscribe", "text/html", "a", b"<p>a"))[1].txid
    b = chain.one(E("inscribe", "text/html", "b", b"<p>b"))[1].txid
    assert chain.ok(E("make folder", "moon", HOME, "www"))
    assert not chain.ok(E("make folder", "moon", HOME, "www"))
    assert not chain.ok(E("put file", "moon", HOME, "nope/index.html", a)), "a parent folder that exists"
    assert chain.ok(E("put file", "moon", HOME, "www/index.html", a))
    assert not chain.ok(E("put file", "moon", HOME, "www/index.html", b))
    assert chain.ok(E("replace file", "moon", HOME, "www/index.html", b))
    assert not chain.ok(E("remove", "moon", HOME, "www")), "not empty"
    assert not chain.ok(E("move", "moon", HOME, "www", "www/inner"))
    assert chain.ok(E("move", "moon", HOME, "www", "site"))
    assert not chain.ok(E("put file", "moon", HOME, "x", a), FROM_B), "only the space's owner"
    assert not chain.ok(E("make folder", "moon", HOME, "a/../b"))
    assert chain.doc["spaces"][key]["root"] == {"site": {"dir": {"index.html": {"file": b}}}}


# --- the DEX -----------------------------------------------------------------------------------------------------

def test_an_offer_holds_the_tokens_and_a_buy_pays_the_seller_in_the_same_transaction(chain):
    space(chain)
    chain.one(E("create property", "moon", HOME, "MBX", "MoonBrix", M.FIXED, 0, 100))
    offer = chain.one(E("offer", "MBX", 30, 5_000_000))[1].txid
    assert chain.doc["properties"]["MBX"]["holders"] == {A: 70}
    assert not chain.ok(E("buy", offer), FROM_B, pays=((A, 4_999_999),)), "underpaid"
    assert not chain.ok(E("buy", offer), FROM_A), "not one's own"
    first = command_tx(E("buy", offer), FROM_B, pays=((A, 5_000_000),))
    second = command_tx(E("buy", offer), FROM_C, pays=((A, 5_000_000),))
    assert [e["ok"] for e in chain.block(first, second)] == [True, False]
    assert chain.doc["properties"]["MBX"]["holders"] == {A: 70, B: 30} and chain.doc["offers"] == {}
    assert not chain.ok(E("offer", "MBX", 71, 1)), "more than A holds"


def test_an_nft_offer_and_its_cancel(chain):
    space(chain)
    chain.one(E("create property", "moon", HOME, "SWORD", "Swords", M.NFT, 0, 0))
    chain.one(E("mint nft", "SWORD", None))
    offer = chain.one(E("offer", "SWORD", 1, 100))[1].txid
    assert not chain.ok(E("send nft", "SWORD", 1), pays=(B,)), "the offer holds it"
    assert not chain.ok(E("cancel", offer), FROM_B)
    assert chain.ok(E("cancel", offer))
    assert chain.doc["properties"]["SWORD"]["items"]["1"]["holder"] == A


# --- following the chain -----------------------------------------------------------------------------------------

def test_a_reorg_rolls_the_document_back(chain):
    chain.one(E("claim tld", "moon", 0))
    chain.mb.roll_back(H - 1, "00" * 32)
    assert chain.doc["tlds"] == {} and chain.mb.height == H - 1
    chain.height = H - 1
    chain.one(E("claim tld", "moon", 0), FROM_B)
    assert chain.doc["tlds"]["moon"]["owner"] == B


def test_a_prototype_document_is_read_again(tmp_path):
    mb = M.MoonBrix(tmp_path / "kot-moonbrix.sqlite", KOT)
    with mb.conn:
        mb._set(H + 5, "ab" * 32, {"lib": [], "tld": [], "fold": [], "ins": [], "prop": []})
    mb.close()
    again = M.MoonBrix(tmp_path / "kot-moonbrix.sqlite", KOT)
    assert again.height is None and again.document() == M.empty()


def test_a_part_the_node_cannot_give_keeps_nothing_of_the_block(chain):
    part = chain.one(E("part", b"data"))[1]
    assemble = command_tx(E("assemble", "text/plain", "x", 4, hashlib.sha256(b"data").hexdigest(), [part.txid]),
                          FROM_A)
    before = chain.mb.height
    with pytest.raises(M.MoonBrixError):
        chain.mb.connect(before + 1, "ff" * 32, [assemble], Coins())
    assert chain.mb.height == before


def test_follow_reads_the_sets_data_blocks_and_rolls_back_a_reorg(tmp_path):
    claim_a = command_tx(E("claim tld", "moon", 0), FROM_A)
    claim_b = command_tx(E("claim tld", "moon", 0), FROM_B)
    hashes = {H: "aa" * 32, H + 1: "bb" * 32, H + 2: "cc" * 32}
    raws = {t.txid: t.raw.hex() for t in (claim_a, claim_b)}

    class Set:
        height, best = H + 2, hashes[H + 2]
        blocks = {H - 5: [claim_b.txid], H + 1: [claim_a.txid]}

        def data_blocks(self, since=0):
            return [(h, t) for h, t in sorted(self.blocks.items()) if h >= since]

    class Rpc:
        def call(self, method, *args):
            if method == "getblockhash":
                return hashes[args[0]]
            if args[1] == 0:
                return raws[args[0]]
            who = (A, B, C)[int(args[0][0]) - 1]                  # getrawtransaction <spent> 1, for the sender
            return {"vout": [{"scriptPubKey": {"hex": p2pkh_script(who).hex()}, "value": 0.001}]}

        def batch(self, calls):
            return [raws[args[0]] for _m, args in calls]

    mb, uset = M.MoonBrix(tmp_path / "kot-moonbrix.sqlite", KOT), Set()
    assert M.follow(mb, uset, Rpc(), KOT) == 1, "nothing before the activation is read"
    assert mb.height == H + 2 and mb.document()["tlds"]["moon"]["owner"] == A
    hashes[H + 1], hashes[H + 2] = "b2" * 32, "c2" * 32
    uset.blocks, uset.best = {H + 1: [claim_b.txid]}, hashes[H + 2]
    assert M.follow(mb, uset, Rpc(), KOT) == 1
    assert mb.document()["tlds"]["moon"]["owner"] == B


def test_kotia_and_only_kotia_reads_moonbrix():
    assert {n for n, p in NETWORKS.items() if p.moonbrix} == {"kot"}
    assert KOT.full_utxo_set, "the unspent set says which blocks carry data"


@pytest.mark.parametrize("amount,decimals,want", [(150_000_000, 8, "1.5"), (7, 0, "7"), (100, 2, "1")])
def test_units_write_whole_tokens(amount, decimals, want):
    assert M.units(amount, decimals) == want


def test_encode_and_decode_agree_on_every_op():
    samples = {"str": "moon", "u8": 1, "u32": 7, "var": 300, "id": "ab" * 32, "ids": ["cd" * 32], "opt": "ef" * 32,
               "rest": b"data"}
    for op, (name, kinds, _t) in M.OPS.items():
        values = [samples[k] for k in kinds]
        got_op, got = M.decode(E(name, *values))
        want = [v.encode() if k == "str" else v for k, v in zip(kinds, values)]
        assert (got_op, got) == (op, want), name


# --- the pages ---------------------------------------------------------------------------------------------------

from test_web import app_state, client                          # noqa: F401,E402


def test_the_moonbrix_page_and_its_data_show_the_state(client, monkeypatch, tmp_path):
    from arcade.web.state import ChainContext
    app, state = client
    kot = ChainContext(network="kot", role="ledger", label="Kotia", conf=pathlib.Path("/nonexistent/kotia.conf"))
    monkeypatch.setattr(state, "more", {"kot": kot}, raising=False)
    built = Chain(tmp_path)
    space(built)
    built.one(E("create property", "moon", HOME, "MBX", "MoonBrix", M.FIXED, 8, 150_000_000))
    built.one(E("name space", "moon", HOME, "<b>home</b>"))
    mb = state.moonbrix(kot)
    with mb.conn:
        mb._set(built.mb.height, "ab" * 32, built.doc)
    page = app.get("/moonbrix")
    assert page.status_code == 200
    assert "MBX · MoonBrix" in page.text and ".moon" in page.text and "19.55.66.132.moon" in page.text
    assert "<b>home</b>" not in page.text and "&lt;b&gt;home" in page.text
    data = app.get("/moonbrix.json").json()
    assert data["height"] == built.mb.height and list(data["properties"]) == ["MBX"]
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

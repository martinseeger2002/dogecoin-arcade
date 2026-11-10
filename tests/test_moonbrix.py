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
    assert chain.ok(E("claim tld", tld, M.WHITELIST, ""), by)
    assert chain.ok(E("claim space", tld, at), by)
    return M.space_key(tld, at)


# --- which transaction is a command ------------------------------------------------------------------------------

def test_only_magic_and_version_2_make_a_command_and_reading_starts_at_the_activation(chain):
    chain.mb.connect(H - 1, "00" * 32, [command_tx(E("claim tld", "early", 0, ""), FROM_A)], Coins())
    assert chain.doc["tlds"] == {}
    other = E("claim tld", "later", 0, "").replace(b"MB\x02", b"MB\x03", 1)
    prototype = bytes.fromhex("004d6f6f6e4272697800000000000000")         # the prototype's TLD claim
    assert chain.block(command_tx(other, FROM_A), command_tx(prototype, FROM_A)) == []
    assert chain.doc["tlds"] == {}


def test_every_command_of_a_block_counts_in_block_order(chain):
    log = chain.block(command_tx(E("claim tld", "moon", 0, ""), FROM_A),
                      command_tx(E("claim tld", "moon", 0, ""), FROM_B),
                      command_tx(E("claim tld", "sun", 0, ""), FROM_B))
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
    good = M.data_scripts(E("claim tld", "moon", 0, ""))[0]
    entry, _t = chain.one(b"", scripts=[good, b"\x6a\x51"])                  # OP_RETURN OP_1
    assert not entry["ok"] and "data pushes" in entry["why"] and chain.doc["tlds"] == {}


def test_the_target_is_the_first_output_paying_someone_but_the_sender(chain):
    chain.one(E("claim tld", "moon", 0, ""))
    entry, _t = chain.one(E("give tld", "moon"), pays=(B, C), change_first=True)
    assert entry["ok"] and chain.doc["tlds"]["moon"]["owner"] == B
    entry, _t = chain.one(E("give tld", "moon"), FROM_B)
    assert not entry["ok"] and entry["why"] == "no output pays an address other than the sender's"
    assert chain.doc["tlds"]["moon"]["owner"] == B


@pytest.mark.parametrize("data,why", [
    (E("claim tld", "moon", 0, "") + b"\x00", "bytes after the last field"),
    (E("claim tld", "MoonBrix", 0, ""), "a TLD is"),
    (E("claim tld", "moon", 2, ""), "a list mode is"),
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
    chain.one(E("claim tld", "moon", M.WHITELIST, ""))
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
    chain.one(E("claim tld", "moon", 0, ""))
    assert not chain.ok(E("list add", "moon"), FROM_B, pays=(C,))
    assert chain.ok(E("list add", "moon"), pays=(C,))
    assert not chain.ok(E("list add", "moon"), pays=(C,)), "on the list already"
    assert chain.ok(E("list remove", "moon"), pays=(C,))
    assert chain.doc["tlds"]["moon"]["list"] == []


# --- properties --------------------------------------------------------------------------------------------------

def test_only_the_spaces_owner_creates_a_property_and_a_fixed_one_is_never_minted(chain):
    key = space(chain)
    assert not chain.ok(E("create property", "moon", HOME, "MBX", "MoonBrix", M.FIXED, 8, 1000, None), FROM_B)
    assert chain.ok(E("create property", "moon", HOME, "MBX", "MoonBrix", M.FIXED, 8, 1000, None))
    prop = chain.doc["properties"]["MBX"]
    assert prop["space"] == key and prop["holders"] == {A: 1000} and prop["locked"]
    entry = chain.one(E("mint", "MBX", 5))[0]
    assert not entry["ok"] and "only a managed property is minted" in entry["why"]
    assert not chain.ok(E("create property", "moon", HOME, "MBX", "Again", M.MANAGED, 0, 0, None)), "had once"
    assert not chain.ok(E("create property", "moon", HOME, "ZZZ", "Zero", M.FIXED, 0, 0, None))
    assert not chain.ok(E("create property", "moon", HOME, "NFTX", "Bad", M.NFT, 2, 0, None))


def test_a_managed_property_is_minted_by_its_owner_until_locked(chain):
    space(chain)
    chain.one(E("create property", "moon", HOME, "ORE", "Iron ore", M.MANAGED, 0, 0, None))
    assert chain.ok(E("mint", "ORE", 50), pays=(B,))
    assert chain.ok(E("mint", "ORE", 7)), "no target: to the sender"
    assert not chain.ok(E("mint", "ORE", 1), FROM_B), "only the owner mints"
    assert chain.ok(E("lock property", "ORE"))
    assert not chain.ok(E("mint", "ORE", 1))
    prop = chain.doc["properties"]["ORE"]
    assert prop["supply"] == 57 and prop["holders"] == {B: 50, A: 7}


def test_tokens_are_sent_and_burnt_only_from_what_the_sender_holds(chain):
    space(chain)
    chain.one(E("create property", "moon", HOME, "MBX", "MoonBrix", M.FIXED, 0, 100, None))
    assert chain.ok(E("send", "MBX", 60), pays=(B,))
    assert not chain.ok(E("send", "MBX", 41), pays=(B,))
    assert not chain.ok(E("send", "MBX", 0), pays=(B,))
    assert chain.ok(E("burn", "MBX", 10), FROM_B)
    prop = chain.doc["properties"]["MBX"]
    assert prop["holders"] == {A: 40, B: 50} and prop["supply"] == 90


def test_an_nft_has_numbered_items_that_move_whole_and_are_never_burnt(chain):
    space(chain)
    picture = chain.one(E("inscribe", "image/png", "sword", b"\x89PNG..."))[1].txid
    chain.one(E("create property", "moon", HOME, "SWORD", "Swords", M.NFT, 0, 0, None))
    assert chain.ok(E("mint nft", "SWORD", ("", picture)), pays=(B,))
    assert chain.ok(E("mint nft", "SWORD", None))
    assert not chain.ok(E("mint nft", "SWORD", ("", "ab" * 32))), "an inscription that exists"
    assert not chain.ok(E("send nft", "SWORD", 1), pays=(C,)), "A does not hold #1"
    assert chain.ok(E("send nft", "SWORD", 1), FROM_B, pays=(C,))
    assert not chain.ok(E("burn", "SWORD", 1), FROM_C)
    items = chain.doc["properties"]["SWORD"]["items"]
    assert items["1"] == {"holder": C, "grant": {"ref": {"chain": "kot", "txid": picture, "checked": True}},
                          "issuer": None, "holder_data": None}
    assert items["2"]["holder"] == A and items["2"]["grant"] is None
    assert M.balances(chain.doc, C) == [{"ticker": "SWORD", "name": "Swords", "kind": "nft", "items": [1]}]


def test_a_tld_keeps_the_message_it_was_claimed_with(chain):
    assert chain.ok(E("claim tld", "moon", 0, "Mono was here"))
    assert chain.doc["tlds"]["moon"]["message"] == "Mono was here"
    assert not chain.ok(E("claim tld", "sun", 0, "x" * 81))


def test_grant_data_is_set_once_at_creation_and_can_point_to_another_chain(chain):
    space(chain)
    logo = chain.one(E("inscribe", "image/png", "logo", b"\x89PNG"))[1].txid
    assert chain.ok(E("create property", "moon", HOME, "MBX", "MoonBrix", M.FIXED, 0, 1, ("", logo)))
    assert chain.ok(E("create property", "moon", HOME, "ORE", "Ore", M.MANAGED, 0, 0, "line one\nline two"))
    assert chain.ok(E("create property", "moon", HOME, "LTCX", "Over there", M.MANAGED, 0, 0, ("ltc", "ab" * 32)))
    assert not chain.ok(E("create property", "moon", HOME, "NOPE", "Missing", M.MANAGED, 0, 0, ("kot", "ab" * 32)))
    assert not chain.ok(E("create property", "moon", HOME, "BAD", "Bad chain", M.MANAGED, 0, 0, ("L", "ab" * 32)))
    props = chain.doc["properties"]
    assert props["MBX"]["grant"] == {"ref": {"chain": "kot", "txid": logo, "checked": True}}
    assert props["ORE"]["grant"] == {"text": "line one\nline two"}
    assert props["LTCX"]["grant"] == {"ref": {"chain": "ltc", "txid": "ab" * 32, "checked": False}}
    assert not any(name.startswith("set") and "grant" in name for name in M.CODES), "no command changes grant data"


def test_an_nft_item_has_grant_issuer_and_holder_data_and_keeps_them_when_it_moves(chain):
    space(chain)
    chain.one(E("create property", "moon", HOME, "SWORD", "Swords", M.NFT, 0, 0, None))
    assert chain.ok(E("mint nft", "SWORD", "steel"), pays=(B,))
    assert chain.ok(E("set issuer data", "SWORD", 1, "damage 12"))
    assert not chain.ok(E("set issuer data", "SWORD", 1, "damage 99"), FROM_B), "only the property's owner"
    assert chain.ok(E("set holder data", "SWORD", 1, "Biter"), FROM_B)
    assert not chain.ok(E("set holder data", "SWORD", 1, "Mine"), FROM_A), "only the item's holder"
    assert chain.ok(E("send nft", "SWORD", 1), FROM_B, pays=(C,))
    item = chain.doc["properties"]["SWORD"]["items"]["1"]
    assert item == {"holder": C, "grant": {"text": "steel"}, "issuer": {"text": "damage 12"},
                    "holder_data": {"text": "Biter"}}
    assert chain.ok(E("set holder data", "SWORD", 1, None), FROM_C)
    assert chain.doc["properties"]["SWORD"]["items"]["1"]["holder_data"] is None


def test_a_space_name_is_lowercase_and_had_once_in_its_tld(chain):
    space(chain)
    chain.one(E("claim space", "moon", 7))
    assert not chain.ok(E("name space", "moon", HOME, "RPG")), "lowercase only"
    assert not chain.ok(E("name space", "moon", HOME, "my rpg"))
    assert chain.ok(E("name space", "moon", HOME, "rpg"))
    assert not chain.ok(E("name space", "moon", 7, "rpg")), "rpg.moon is taken"
    assert chain.ok(E("name space", "moon", HOME, ""))
    assert chain.ok(E("name space", "moon", 7, "rpg")), "free again"


def test_folder_and_file_names_are_lowercase(chain):
    space(chain)
    assert not chain.ok(E("make folder", "moon", HOME, "WWW"))
    assert not chain.ok(E("make folder", "moon", HOME, "my site"))
    assert chain.ok(E("make folder", "moon", HOME, "my-site_2.0"))


def test_find_names_a_tld_a_space_a_path_a_ticker_or_an_address(chain):
    key = space(chain, tld="lwq")
    chain.one(E("name space", "lwq", HOME, "rpg"))
    page = chain.one(E("inscribe", "text/html", "index", b"<p>hi"))[1].txid
    chain.one(E("make folder", "lwq", HOME, "www"))
    chain.one(E("put file", "lwq", HOME, "www/index.html", ("", page)))
    chain.one(E("create property", "lwq", HOME, "LWQ", "Coins", M.FIXED, 0, 10, None))
    doc = chain.doc
    kinds = lambda q: [(f["kind"], f["id"]) for f in M.find(doc, q)]
    assert kinds("lwq") == [("tld", "lwq"), ("property", "LWQ")]
    assert M.find(doc, "LWQ")[0]["spaces"][0][0] == key
    assert kinds("rpg.lwq") == kinds(" RPG.LWQ ") == kinds("19.55.66.132.lwq") == [("space", "rpg.lwq")]
    assert kinds("rpg.lwq/www/index.html") == [("path", "rpg.lwq/www/index.html")]
    assert M.find(doc, "rpg.lwq/www")[0]["it"] == {"dir": {"index.html": {"file": {
        "chain": "kot", "txid": page, "checked": True}}}}
    assert kinds(page) == [("inscription", page)]
    assert kinds(A) == [("address", A)] and M.find(doc, A)[0]["owns"]["spaces"] == [key]
    assert kinds("nope.lwq") == [] and kinds("rpg.lwq/nope") == []


# --- inscriptions, folders and files -----------------------------------------------------------------------------

def test_an_assembled_inscription_needs_parts_from_before_of_the_size_and_hash_it_names(chain):
    one, two = b"first half,", b" second half"
    p1 = chain.one(E("part", one))[1].txid
    p2 = chain.one(E("part", two), FROM_B)[1].txid
    whole = one + two + one
    sha = hashlib.sha256(whole).hexdigest()
    assert not chain.ok(E("assemble", "text/plain", "x", len(whole), "00" * 32, [("", p1), ("", p2), ("", p1)]))
    assert not chain.ok(E("assemble", "text/plain", "x", len(whole) + 1, sha, [("", p1), ("", p2), ("", p1)]))
    late = command_tx(E("part", b"late"), FROM_C)
    late_sha = hashlib.sha256(b"late").hexdigest()
    early = command_tx(E("assemble", "text/plain", "x", 4, late_sha, [("", late.txid)]), FROM_A)
    assert [e["ok"] for e in chain.block(early, late)] == [False, True], "a part comes before its assemble"
    entry, t = chain.one(E("assemble", "text/plain", "story", len(whole), sha, [("", p1), ("", p2), ("", p1)]))
    assert entry["ok"]
    assert M.content(chain.doc, chain.txs.__getitem__, t.txid) == ("text/plain", whole)


def test_folders_and_files_are_never_overwritten_but_by_a_replace(chain):
    key = space(chain)
    a = chain.one(E("inscribe", "text/html", "a", b"<p>a"))[1].txid
    b = chain.one(E("inscribe", "text/html", "b", b"<p>b"))[1].txid
    assert chain.ok(E("make folder", "moon", HOME, "www"))
    assert not chain.ok(E("make folder", "moon", HOME, "www"))
    assert not chain.ok(E("put file", "moon", HOME, "nope/index.html", ("", a))), "a parent folder that exists"
    assert chain.ok(E("put file", "moon", HOME, "www/index.html", ("", a)))
    assert not chain.ok(E("put file", "moon", HOME, "www/index.html", ("", b)))
    assert chain.ok(E("replace file", "moon", HOME, "www/index.html", ("", b)))
    assert not chain.ok(E("remove", "moon", HOME, "www")), "not empty"
    assert not chain.ok(E("move", "moon", HOME, "www", "www/inner"))
    assert chain.ok(E("move", "moon", HOME, "www", "site"))
    assert not chain.ok(E("put file", "moon", HOME, "x", ("", a)), FROM_B), "only the space's owner"
    assert not chain.ok(E("make folder", "moon", HOME, "a/../b"))
    assert chain.doc["spaces"][key]["root"] == {
        "site": {"dir": {"index.html": {"file": {"chain": "kot", "txid": b, "checked": True}}}}}


def test_a_join_moves_a_folders_contents_into_another_without_overwriting(chain):
    key = space(chain)
    a = chain.one(E("inscribe", "text/plain", "a", b"a"))[1].txid
    for path in ("old", "new", "old/sub"):
        assert chain.ok(E("make folder", "moon", HOME, path))
    assert chain.ok(E("put file", "moon", HOME, "old/a.txt", ("", a)))
    assert chain.ok(E("put file", "moon", HOME, "new/a.txt", ("", a)))
    assert not chain.ok(E("join", "moon", HOME, "old", "new")), "a.txt is in both"
    assert chain.ok(E("remove", "moon", HOME, "new/a.txt"))
    assert not chain.ok(E("join", "moon", HOME, "old", "old/sub")), "not into itself"
    assert chain.ok(E("join", "moon", HOME, "old", "new"))
    assert set(chain.doc["spaces"][key]["root"]) == {"new"}
    assert set(chain.doc["spaces"][key]["root"]["new"]["dir"]) == {"a.txt", "sub"}


def test_parts_on_another_chain_are_not_read_here_yet(chain):
    entry = chain.one(E("assemble", "text/plain", "x", 1, "00" * 32, [("ltc", "ab" * 32)]))[0]
    assert not entry["ok"] and "another chain" in entry["why"]


# --- the DEX -----------------------------------------------------------------------------------------------------

def offered(chain, amount=30, price=3_000_000, window=10, deposit=300_000):
    space(chain)
    chain.one(E("create property", "moon", HOME, "MBX", "MoonBrix", M.FIXED, 0, 100, None))
    return chain.one(E("offer", "MBX", amount, price, window, deposit))[1].txid


def test_a_reservation_holds_the_tokens_for_the_buyer_until_paid(chain):
    offer = offered(chain)
    assert chain.doc["properties"]["MBX"]["holders"] == {A: 70}
    assert not chain.ok(E("reserve", offer, 10), FROM_B, pays=((A, 99_999),)), "short of the deposit's share"
    rid = chain.one(E("reserve", offer, 10), FROM_B, pays=((A, 100_000),))[1].txid
    assert chain.doc["offers"][offer]["left"] == 20
    assert chain.doc["reservations"][rid]["due"] == 1_000_000
    assert chain.ok(E("cancel", offer)), "the seller cancels the rest"
    assert chain.doc["properties"]["MBX"]["holders"] == {A: 90}, "but not what B reserved"
    assert not chain.ok(E("reserve", offer, 1), FROM_C, pays=((A, 1_000_000),)), "cancelled"
    assert not chain.ok(E("pay", rid), FROM_C, pays=((A, 900_000),)), "only the buyer pays"
    assert not chain.ok(E("pay", rid), FROM_B, pays=((A, 899_999),)), "what is due"
    assert chain.ok(E("pay", rid), FROM_B, pays=((A, 900_000),))
    assert chain.doc["properties"]["MBX"]["holders"] == {A: 90, B: 10}
    assert chain.doc["offers"] == {} and chain.doc["reservations"] == {}


def test_a_reservation_not_paid_in_its_window_goes_back_and_its_deposit_stays(chain):
    offer = offered(chain, window=2)
    rid = chain.one(E("reserve", offer, 30), FROM_B, pays=((A, 300_000),))[1].txid
    assert not chain.ok(E("reserve", offer, 1), FROM_C, pays=((A, 100_000),)), "nothing left"
    chain.block()                                                      # h+1
    reserved_at = chain.doc["reservations"][rid]["height"]
    chain.height = reserved_at + 2
    log = chain.block(command_tx(E("pay", rid), FROM_B, pays=((A, 2_700_000),)))
    assert log[0]["op"] == "expiry" and not log[1]["ok"], "the window closed after block h+2"
    assert chain.doc["offers"][offer]["left"] == 30
    assert chain.ok(E("reserve", offer, 30), FROM_C, pays=((A, 3_000_000),)), "paid in full at once"
    assert chain.doc["properties"]["MBX"]["holders"] == {A: 70, C: 30}


def test_a_payment_in_the_last_block_of_the_window_counts(chain):
    offer = offered(chain, window=2, deposit=0)
    rid = chain.one(E("reserve", offer, 30), FROM_B)[1].txid
    chain.height += 1
    assert chain.ok(E("pay", rid), FROM_B, pays=((A, 3_000_000),))
    assert chain.doc["properties"]["MBX"]["holders"] == {A: 70, B: 30}


@pytest.mark.parametrize("fields,why", [
    ((30, 0, 10, 0), "a price is"), ((30, 10, 0, 0), "a payment window"), ((30, 10, 10, 11), "a deposit is"),
    ((101, 10, 10, 0), "holds"),
])
def test_an_offer_has_a_price_a_window_and_a_deposit_within_it(chain, fields, why):
    space(chain)
    chain.one(E("create property", "moon", HOME, "MBX", "MoonBrix", M.FIXED, 0, 100, None))
    entry = chain.one(E("offer", "MBX", *fields))[0]
    assert not entry["ok"] and why in entry["why"]


def test_an_nft_offer_is_reserved_whole_and_its_cancel(chain):
    space(chain)
    chain.one(E("create property", "moon", HOME, "SWORD", "Swords", M.NFT, 0, 0, None))
    chain.one(E("mint nft", "SWORD", None))
    offer = chain.one(E("offer", "SWORD", 1, 100, 5, 0))[1].txid
    assert not chain.ok(E("send nft", "SWORD", 1), pays=(B,)), "the offer holds it"
    assert not chain.ok(E("reserve", offer, 2), FROM_B)
    assert not chain.ok(E("cancel", offer), FROM_B)
    assert chain.ok(E("cancel", offer))
    assert chain.doc["properties"]["SWORD"]["items"]["1"]["holder"] == A and chain.doc["offers"] == {}


# --- following the chain -----------------------------------------------------------------------------------------

def test_a_reorg_rolls_the_document_back(chain):
    chain.one(E("claim tld", "moon", 0, ""))
    chain.mb.roll_back(H - 1, "00" * 32)
    assert chain.doc["tlds"] == {} and chain.mb.height == H - 1
    chain.height = H - 1
    chain.one(E("claim tld", "moon", 0, ""), FROM_B)
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
    assemble = command_tx(E("assemble", "text/plain", "x", 4, hashlib.sha256(b"data").hexdigest(), [("", part.txid)]),
                          FROM_A)
    before = chain.mb.height
    with pytest.raises(M.MoonBrixError):
        chain.mb.connect(before + 1, "ff" * 32, [assemble], Coins())
    assert chain.mb.height == before


def test_follow_reads_the_sets_data_blocks_and_rolls_back_a_reorg(tmp_path):
    claim_a = command_tx(E("claim tld", "moon", 0, ""), FROM_A)
    claim_b = command_tx(E("claim tld", "moon", 0, ""), FROM_B)
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
    samples = {"str": "moon", "u8": 1, "u32": 7, "var": 300, "id": "ab" * 32, "ref": ("ltc", "cd" * 32),
               "refs": [("", "cd" * 32)], "data": "hi", "rest": b"data"}
    decoded = {"str": b"moon", "ref": (b"ltc", "cd" * 32), "refs": [(b"", "cd" * 32)], "data": ("text", b"hi")}
    for op, (name, kinds, _t) in M.OPS.items():
        values = [samples[k] for k in kinds]
        got_op, got = M.decode(E(name, *values))
        want = [decoded.get(k, v) for k, v in zip(kinds, values)]
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
    built.one(E("create property", "moon", HOME, "MBX", "<b>MoonBrix</b>", M.FIXED, 8, 150_000_000, None))
    assert built.ok(E("name space", "moon", HOME, "rpg"))
    assert built.ok(E("make folder", "moon", HOME, "www"))
    mb = state.moonbrix(kot)
    with mb.conn:
        mb._set(built.mb.height, "ab" * 32, built.doc)
    page = app.get("/moonbrix")
    assert page.status_code == 200
    assert "MBX · &lt;b&gt;MoonBrix" in page.text and "<b>MoonBrix" not in page.text
    assert ".moon" in page.text and "rpg.moon" in page.text
    found = app.get("/moonbrix", params={"q": "RPG.Moon"}).text
    assert "Space 19.55.66.132.moon" in found and "MBX" in found and "www/" in found
    assert "rpg.moon" in app.get("/moonbrix", params={"q": "moon"}).text
    assert "Nothing on MoonBrix is called" in app.get("/moonbrix", params={"q": "nope.moon"}).text
    data = app.get("/moonbrix.json").json()
    assert data["height"] == built.mb.height and list(data["properties"]) == ["MBX"]
    assert app.get("/moonbrix/content/" + "zz" * 32).status_code in (400, 404)
    assert app.get("/moonbrix/content/" + "ab" * 32).status_code == 404, "only an inscription MoonBrix has"
    kot.borrowed = True
    assert "borrowed" in app.get("/moonbrix").text
    assert app.get("/moonbrix.json").status_code == 404


def test_the_explorer_is_a_page_of_its_own_and_asks_find_json(client, monkeypatch, tmp_path):
    from arcade.web.state import ChainContext
    app, state = client
    kot = ChainContext(network="kot", role="ledger", label="Kotia", conf=pathlib.Path("/nonexistent/kotia.conf"))
    monkeypatch.setattr(state, "more", {"kot": kot}, raising=False)
    built = Chain(tmp_path)
    space(built, tld="lwq")
    built.one(E("name space", "lwq", HOME, "rpg"))
    mb = state.moonbrix(kot)
    with mb.conn:
        mb._set(built.mb.height, "ab" * 32, built.doc)
    page = app.get("/moonbrix/explorer")
    assert page.status_code == 200 and "MoonBrix Explorer" in page.text
    assert "fonts.googleapis" not in page.text, "no visitor's address goes to a font host"
    assert "/moonbrix/find.json" in page.text and "esc(" in page.text
    found = app.get("/moonbrix/find.json", params={"q": "RPG.lwq"}).json()
    assert [(f["kind"], f["id"]) for f in found["found"]] == [("space", "rpg.lwq")]
    assert app.get("/moonbrix/find.json").json()["found"] == []
    kot.borrowed = True
    assert app.get("/moonbrix/find.json", params={"q": "lwq"}).status_code == 404


def test_moonbrix_is_public_and_claimed_by_the_tokens_module():
    from arcade import modules
    from arcade.web import door
    for path in ("/moonbrix", "/moonbrix.json", "/moonbrix/content/" + "ab" * 32, "/moonbrix/explorer",
                 "/moonbrix/find.json"):
        assert door.public_path(path), path
    assert not door.public_path("/moonbrix/content/../admin")
    claimed = {r for m in modules._MODULES if m.id == "tokens" for r in m.routes}
    assert {"/moonbrix", "/moonbrix.json", "/account/moonbrix", "/moonbrix/explorer", "/moonbrix/find.json"} <= claimed

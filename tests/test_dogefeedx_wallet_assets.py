"""Index holdings labeled from the arcade's own collections.

Ownership is a fixture shaped like this node's indexes. The test does not
import a key, ask any other server anything, or write anywhere.
"""
import json
from pathlib import Path

from arcade.web.dogefeedx.wallet_assets import (
    build_holdings,
    cross_reference,
    empty_holdings,
    index_owned,
    inscriptions_from_utxos,
    merge_holdings,
    parse_balance,
    parse_drc20,
    parse_names,
    public_view,
    valid_address,
)

ADDR = "DBMBjmyKSAKboj5NrXeER2rYWkpxETvUMn"
ROOT = Path(__file__).resolve().parents[1]
MODULE = (ROOT / "arcade" / "web" / "dogefeedx" / "wallet_assets.py").read_text()

def _piece(iid, slug, edition, title):
    return {"inscription_id": iid, "slug": slug, "edition": edition, "title": title, "image": f"/content/{iid}",
            "href": f"/exchange/ord/{slug}"}


# What the index says an address holds (wallet_assets._pieces_from_index's shape).
HELD = [
    _piece("301db49a62fa817a1bbcbb0c0e00ce7b14f1b18acee23c40da2d5748e2c99b5ei0", "anti-doginal-dogs", "9730", "Anti Doginal Dog #9730"),
    _piece("c20693d06282a666126a5660cd11ed9e7556da5fbfa2bd609837f704bbd6f842i0", "pixel-pepes", "689", "Pixel Pepes #689"),
    _piece("888d4748f672ef5b20bac4ba3bc80a3a779f89cb26af7fac8fd98ae4747f9cd7i0", "doginal-frogs", "10040", "Doginal Frog #10040"),
    _piece("992dbf195dae9e43fdf5540afb574bc03c8d435ef1c0572c5d74b68f9e806d2bi0", "doginal-maxi-biz", "29", "DMB #29"),
]
PEPE_COVER = "/content/" + "c" * 64 + "i0"

# The arcade's collections (apps.LOCAL.market rows): covers are its own /content/, anything else is dropped.
CATALOG = [
    {"slug": "pixel-pepes", "name": "Pixel Pepes", "image": PEPE_COVER, "floor": "69"},
    {"slug": "doginal-maxi-biz", "name": "Doge Maxi Biz", "image": "https://evil.example/cover.png", "floor": "10"},
    {"slug": "dogebuds", "name": "Doge Buds", "image": "https://evil.example/x.png", "floor": "1"},
]


def test_cross_reference_uses_the_arcades_collection_name_not_the_card_nickname():
    held = list(HELD)
    groups = {row["slug"]: row for row in cross_reference(held, CATALOG)}
    assert groups["pixel-pepes"]["known"] is True
    assert groups["pixel-pepes"]["name"] == "Pixel Pepes"
    assert groups["pixel-pepes"]["floor"] == "69"
    assert groups["pixel-pepes"]["cover"] == PEPE_COVER
    assert groups["pixel-pepes"]["href"] == "/exchange/ord/pixel-pepes"
    assert groups["pixel-pepes"]["pieces"][0]["edition"] == "689"
    assert groups["doginal-maxi-biz"]["name"] == "Doge Maxi Biz"
    assert groups["doginal-maxi-biz"]["cover"] == ""          # a picture from anywhere but this arcade is dropped
    assert "dogebuds" not in groups
    assert groups["anti-doginal-dogs"]["known"] is False
    assert groups["anti-doginal-dogs"]["name"] == "Anti Doginal Dog"
    blob = json.dumps(groups)
    assert "scriptPubKey" not in blob and "evil.example" not in blob and "https://" not in blob


def test_utxo_rows_keep_the_inscription_and_drop_the_coin():
    rows = [{
        "txid": "ab" * 32,
        "vout": 1,
        "scriptPubKeyHex": "76a914",
        "value_koinu": 100000,
        "inscriptions": [{
            "id": "c" * 64 + "i0",
            "mime": "image/png",
            "scriptPubKey": "76a914",
        }],
    }]
    got = inscriptions_from_utxos(rows)
    assert got == [{"inscription_id": "c" * 64 + "i0", "content_type": "image/png"}]
    assert "scriptPubKey" not in json.dumps(got)
    assert "txid" not in json.dumps(got)


def test_balance_uses_balance_doge_and_names_must_match_the_address():
    assert parse_balance({"balance": 33303308736, "balance_doge": "333.03308736"}) == "333.03308736"
    assert parse_balance({"balance": 1}) is None
    names = parse_names({
        "address": ADDR,
        "names": [
            {"name": "billy.dogefeed", "held": True, "verified": True},
            {"name": "015.dogecoin", "held": True, "verified": True},
            {"name": "sold.doge", "held": False},
        ],
    }, ADDR)
    assert [row["name"] for row in names] == ["billy.dogefeed", "015.dogecoin"]
    assert parse_names({"address": "DSomeoneElse111111111111111111111", "names": [
        {"name": "billy.dogefeed", "held": True},
    ]}, ADDR) == []
    built = build_holdings(ADDR, {
        "balance_doge": "333.03308736",
        "names": [{"name": "billy.dogefeed"}],
        "tokens": [{"tick": "$DON", "balance": "230000"}],
        "pieces": list(HELD),
        "utxo_count": 15409,
        "scriptPubKeyHex": "nope",
    }, CATALOG, imported=True)
    assert built["profile"] == "billy.dogefeed"
    assert built["imported"] is True
    assert built["balance_doge"] == "333.03308736"
    assert built["tokens"] == [{"tick": "$DON", "balance": "230000"}]
    assert "utxo_count" not in built and "scriptPubKeyHex" not in built
    assert empty_holdings()["empty"].startswith("Import a watch-only address")
    try:
        valid_address("5Kprivkeynotanaddress")
    except ValueError:
        pass
    else:
        raise AssertionError("a key-shaped string was accepted")


def test_the_reader_never_lists_coins_or_asks_for_a_key():
    for banned in ("listunspent", "importprivkey", "importmulti", "signrawtransaction", "dumpprivkey"):
        assert banned not in MODULE
    for banned in (
        "dogecoinnames.com",
        "explorer.dogesoft.io",
        "/wallet/address/",
        "/u/",
        "tab=assets",
    ):
        assert banned not in MODULE
    apps = (ROOT / "arcade" / "web" / "dogefeedx" / "apps.py").read_text()
    for name in ("api_balance", "api_address_inscriptions", "api_address_tokens", "api_wallet_holdings", "api_wallet_addresses", "api_lookup"):
        start = apps.index(f"async def {name}")
        nxt = apps.find("\n@router.", start + 10)
        body = apps[start:nxt if nxt > start else start + 2500]
        assert "/wallet/address/" not in body
        assert "dogesoft" not in body
        assert "dogefeed.app" not in body


def test_index_rows_are_labeled_by_the_catalog_and_coins_stay_out():
    """Pieces as the index reports them pick up the arcade's collection names."""
    pieces = [dict(p) for p in HELD]
    pieces[1]["group_count"] = 24
    owned = {
        "balance_doge": "1.5",
        "names": [{"name": "billy.dogefeed"}, {"name": "015.dogecoin"}],
        "tokens": [{"tick": "$DON", "balance": "230000"}],
        "pieces": pieces,
        "txid": "ab" * 32,
        "vout": 0,
        "scriptPubKey": "76a914",
        "coins": [{"txid": "ab" * 32, "vout": 1}],
    }
    built = build_holdings(ADDR, owned, CATALOG, imported=True)
    pepe = next(row for row in built["collections"] if row["slug"] == "pixel-pepes")
    assert pepe["name"] == "Pixel Pepes" and pepe["known"] is True and pepe["floor"] == "69"
    assert pepe["count"] == 24
    assert any(piece["edition"] == "689" for piece in pepe["pieces"])
    assert built["profile"] == "billy.dogefeed"
    assert built["source"] == "node"
    dumped = json.dumps(built)
    assert "scriptPubKey" not in dumped and "txid" not in dumped and '"coins"' not in dumped
    assert "utxo" not in dumped
    assert built["reading"]["via"] == "local"
    assert "utxo" not in json.dumps(built["reading"])


class _Chain:
    def __init__(self, *, borrowed=False, rpc=None):
        self.borrowed = borrowed
        self.network = "doge"
        self.is_doge = True
        self._rpc = rpc

    def rpc(self):
        if self._rpc is None:
            raise AssertionError("this Core must not open a peer")
        return self._rpc


class _Rpc:
    def __init__(self, answer=None):
        self.answer = answer
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def batch(self, calls):
        self.calls.extend(calls)
        return self.answer


def _patch_chain(monkeypatch, chain, *, behind, sync):
    from arcade import core_catchup
    from arcade.web.dogefeedx import wallet_assets as wa

    monkeypatch.setattr(wa, "_doge_chain", lambda: chain)
    monkeypatch.setattr(core_catchup, "borrowing", lambda _chain: behind)
    monkeypatch.setattr(core_catchup, "say", lambda _chain, mesh_on=True: sync)


def _patch_indexes(monkeypatch, *, ordix, token):
    import arcade.web.state as state

    monkeypatch.setattr(state, "ord_index", lambda net, wait=0: ordix, raising=False)
    monkeypatch.setattr(state, "token_index", lambda chain: token, raising=False)


class _Ord:
    def __init__(self, answers):
        self.answers = answers
        self.direct = []

    def lookup(self, question):
        what = question.get("what")
        if what not in self.answers:
            raise AssertionError(what)
        return self.answers[what]

    def answer(self, question):
        return self.lookup(question)

    def balances(self, address):
        self.direct.append("balances")
        raise AssertionError("direct balances")

    def names_on(self, coins):
        self.direct.append("names")
        raise AssertionError("direct names")

    def name(self, name):
        self.direct.append("name")
        raise AssertionError("direct name")


def test_a_synced_core_does_not_ask_a_peer(monkeypatch):
    _patch_chain(monkeypatch, _Chain(), behind=False, sync=None)

    class Index:
        def open(self):
            raise RuntimeError("no local index in this test")

    _patch_indexes(monkeypatch, ordix=None, token=Index())
    owned = index_owned(ADDR)
    assert owned["balance_doge"] is None
    assert owned["reading"] == {"via": "local", "sync": "this Core is at the tip", "node": "this arcade"}
    assert "utxo" not in json.dumps(owned)


def test_a_behind_core_reads_the_lent_set_and_the_ord_walker(monkeypatch):
    from arcade import utxoset

    fetched = []

    class Lent:
        def unspent(self, address, wallet_only=False):
            assert address == ADDR
            return [{"txid": "ab" * 32, "vout": 0, "value": 100_000, "height": 3, "kind": "wallet"}]

    def fetch(rpc, addresses):
        fetched.append(list(addresses))
        return Lent()

    monkeypatch.setattr(utxoset.LentSet, "fetch", fetch)
    _patch_chain(
        monkeypatch, _Chain(rpc=_Rpc()), behind=True,
        sync="syncing: borrowed from arcade abcdef123456 until it catches up (block 1 of 9)",
    )
    iid = "cd" * 32 + "i0"
    ordix = _Ord({
        "balances": {"from": "arcade abcdef123456", "balances": [{"tick": "wow", "units": "5", "decimals": 0}]},
        "names_on": {"from": "arcade abcdef123456", "names_on": [{"name": "billy.doge"}]},
        "on": {"from": "arcade abcdef123456", "on": [{"id": iid, "offset": 0, "ctype": "image/png"},
                                                     {"id": "ee" * 32 + "i0", "offset": 1, "ctype": "text/plain"}]},
    })
    _patch_indexes(monkeypatch, ordix=ordix, token=None)
    owned = index_owned(ADDR)
    assert fetched == [[ADDR]]
    assert ordix.direct == []
    assert owned["balance_doge"] == "0.001"
    assert owned["tokens"] == [{"tick": "wow", "balance": "5"}]
    assert owned["names"] == [{"name": "billy.doge"}]
    assert [row["inscription_id"] for row in owned["pieces"]] == [iid]
    assert owned["pieces"][0]["image"] == "/content/" + "cd" * 32
    assert owned["reading"]["via"] == "peer"
    assert owned["reading"]["node"] == "arcade abcdef123456"
    assert "block 1 of 9" in owned["reading"]["sync"]
    blob = json.dumps(owned)
    assert "txid" not in blob and "scriptPubKey" not in blob and "utxo" not in blob and '"coins"' not in blob
    built = build_holdings(ADDR, owned, [], imported=True)
    assert built["source"] == "node" and built["reading"]["via"] == "peer"
    assert "txid" not in json.dumps(built) and "utxo" not in json.dumps(built)


def test_no_lent_set_uses_the_coin_scan_and_not_a_third_party(monkeypatch):
    from arcade import coinscan, utxoset

    def fetch(rpc, addresses):
        raise utxoset.UtxoSetError("no unspent set of Dogecoin here")

    monkeypatch.setattr(utxoset.LentSet, "fetch", fetch)
    rpc = _Rpc(answer=[{"coins": {ADDR: [["ef" * 32, 1, 50_000_000, 4]]}, "waiting": [], "height": {ADDR: 4}}])
    _patch_chain(monkeypatch, _Chain(borrowed=True, rpc=rpc), behind=False, sync=None)
    ordix = _Ord({
        "balances": {"from": "this arcade", "balances": []},
        "on": {"from": "this arcade", "on": []},
    })
    _patch_indexes(monkeypatch, ordix=ordix, token=None)
    owned = index_owned(ADDR)
    assert rpc.calls and rpc.calls[0][0] == coinscan.SCAN_METHOD
    assert "scantxoutset" not in json.dumps(rpc.calls)
    assert owned["balance_doge"] == "0.5"
    assert owned["reading"]["via"] == "peer"
    assert owned["reading"]["node"] == "a peer on the arcade network"
    assert "no Core of its own" in owned["reading"]["sync"]
    assert "No coin set was lent" not in owned["reading"]["sync"]
    blob = json.dumps(owned)
    for banned in ("dogecoinnames.com", "explorer.dogesoft.io", "dogefeed.app", "scriptPubKey", "txid", "utxo"):
        assert banned not in blob


def test_a_scan_still_waiting_falls_back_to_this_index(monkeypatch):
    from arcade import utxos as utxoslib
    from arcade import utxoset

    def fetch(rpc, addresses):
        raise utxoset.UtxoSetError("no set")

    monkeypatch.setattr(utxoset.LentSet, "fetch", fetch)
    monkeypatch.setattr(utxoslib, "balance", lambda db, address: 100_000_000)
    monkeypatch.setattr(utxoslib, "unspent", lambda db, address: [])
    rpc = _Rpc(answer=[{"coins": {}, "waiting": [ADDR]}])
    _patch_chain(
        monkeypatch, _Chain(rpc=rpc), behind=True,
        sync="syncing: borrowed from arcade abcdef123456 until it catches up (block 2 of 9)",
    )

    class Index:
        def open(self):
            return self

        def close(self):
            return None

        def held_collections(self, address, cover=1):
            return []

    _patch_indexes(monkeypatch, ordix=None, token=Index())
    owned = index_owned(ADDR)
    assert rpc.calls and rpc.calls[0][0] == "arcade_coinscan"
    assert owned["balance_doge"] == "1"
    assert owned["reading"]["via"] == "local"
    assert "No coin set was lent" in owned["reading"]["sync"]
    assert "block 2 of 9" in owned["reading"]["sync"]
    assert "utxo" not in json.dumps(owned)


def test_merge_prefers_a_peer_reading_and_the_pages_show_it():
    other = "D" + "B" * 33
    local = build_holdings(ADDR, {
        "balance_doge": "1",
        "reading": {"via": "local", "sync": "this Core is at the tip", "node": "this arcade", "txid": "ab" * 32},
    }, [], imported=True)
    peer = build_holdings(other, {
        "balance_doge": "2",
        "reading": {"via": "peer", "sync": "syncing: borrowed from arcade abcdef123456", "node": "arcade abcdef123456"},
    }, [], imported=True)
    assert "txid" not in json.dumps(local)
    merged = merge_holdings([local, peer])
    assert merged["reading"]["via"] == "peer"
    assert merged["source"] == "node"
    assert "txid" not in json.dumps(merged) and "utxo" not in json.dumps(merged)
    shown = public_view(merged)
    assert shown["reading"]["via"] == "peer"
    templates = ROOT / "arcade" / "web" / "templates" / "dogefeedx"
    for name in ("wallet.html", "files.html", "nfts.html", "profile.html"):
        text = (templates / name).read_text()
        assert "data.reading" in text, name
        assert "r.via === 'peer'" in text, name
        assert "Local sync:" in text, name
        assert "a peer on the arcade network" in text, name
    assert "scantxoutset" not in MODULE
    apps = (ROOT / "arcade" / "web" / "dogefeedx" / "apps.py").read_text()
    for name in ("api_balance", "api_address_inscriptions", "api_address_tokens"):
        start = apps.index(f"async def {name}")
        body = apps[start:apps.find("\n@router.", start + 10)]
        assert '"reading"' in body, name

"""Index holdings labeled from DogeFeed's public collection catalog.

Ownership is a fixture shaped like this node's indexes. The test does not
import a key, ask dogefeed.app who owns a coin, or write anywhere.
"""
import json
from pathlib import Path

from arcade.web.dogefeedx.wallet_assets import (
    build_holdings,
    cross_reference,
    empty_holdings,
    inscriptions_from_utxos,
    name_points_at,
    parse_balance,
    parse_drc20,
    parse_names,
    parse_profile_assets,
    valid_address,
)

ADDR = "DBMBjmyKSAKboj5NrXeER2rYWkpxETvUMn"
ROOT = Path(__file__).resolve().parents[1]
MODULE = (ROOT / "arcade" / "web" / "dogefeedx" / "wallet_assets.py").read_text()

ASSETS = """
<div data-testid="profile-nfts"><div data-testid="doginal-grid">
<div data-inscription="301db49a62fa817a1bbcbb0c0e00ce7b14f1b18acee23c40da2d5748e2c99b5ei0">
<a href="/market/anti-doginal-dogs/9730"><img src="/doginal-img/a" alt="Anti Doginal Dog #9730"></a></div>
<div data-inscription="c20693d06282a666126a5660cd11ed9e7556da5fbfa2bd609837f704bbd6f842i0">
<a href="/market/pixel-pepes/689"><img src="/doginal-img/b" alt="Pixel Pepes #689"></a></div>
<div data-inscription="888d4748f672ef5b20bac4ba3bc80a3a779f89cb26af7fac8fd98ae4747f9cd7i0">
<a href="/market/doginal-frogs/10040"><img src="/doginal-img/c" alt="Doginal Frog #10040"></a></div>
<div data-inscription="992dbf195dae9e43fdf5540afb574bc03c8d435ef1c0572c5d74b68f9e806d2bi0">
<a href="/market/doginal-maxi-biz/29"><img src="/doginal-img/d" alt="DMB #29"></a></div>
<div data-inscription="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaai0">
<a href="/market/doginal-cowz/395"><img src="/doginal-img/e" alt="Doginal Cowz #395"></a></div>
<div data-inscription="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbi0">
<a href="/market/spooksociety/350"><img src="/doginal-img/f" alt="The Spook Society #350"></a></div>
</div></div>
Show more
<a href="/market/dogebuds/1"><img src="/doginal-img/nope" alt="Not held #1">
"""

CATALOG = [
    {"slug": "pixel-pepes", "name": "Pixel Pepes", "image": "https://dogefeed.app/pepe.png", "floor": "69"},
    {"slug": "doginal-maxi-biz", "name": "Doge Maxi Biz", "image": "/doginal-img/cover", "floor": "10"},
    {"slug": "dogebuds", "name": "Doge Buds", "image": "https://evil.example/x.png", "floor": "1"},
]


def test_cross_reference_uses_the_public_catalog_name_not_the_card_nickname():
    held = parse_profile_assets(ASSETS)
    assert [row["slug"] for row in held] == [
        "anti-doginal-dogs", "pixel-pepes", "doginal-frogs",
        "doginal-maxi-biz", "doginal-cowz", "spooksociety",
    ]
    assert ("dogebuds", "1") not in {(row["slug"], row["edition"]) for row in held}
    groups = {row["slug"]: row for row in cross_reference(held, CATALOG)}
    assert groups["pixel-pepes"]["known"] is True
    assert groups["pixel-pepes"]["name"] == "Pixel Pepes"
    assert groups["pixel-pepes"]["floor"] == "69"
    assert groups["pixel-pepes"]["cover"] == "https://dogefeed.app/pepe.png"
    assert groups["pixel-pepes"]["pieces"][0]["edition"] == "689"
    assert groups["doginal-maxi-biz"]["name"] == "Doge Maxi Biz"
    assert groups["doginal-maxi-biz"]["cover"].startswith("https://dogefeed.app/doginal-img/")
    assert "dogebuds" not in groups
    assert groups["anti-doginal-dogs"]["known"] is False
    assert groups["anti-doginal-dogs"]["name"] == "Anti Doginal Dog"
    blob = json.dumps(groups)
    assert "scriptPubKey" not in blob and "evil.example" not in blob


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
    assert name_points_at(
        {"records": [{"address": ADDR, "dns": "billy.dogefeed"}]}, ADDR, "billy.dogefeed",
    )
    built = build_holdings(ADDR, {
        "balance_doge": "333.03308736",
        "names": [{"name": "billy.dogefeed"}],
        "tokens": [{"tick": "$DON", "balance": "230000"}],
        "pieces": parse_profile_assets(ASSETS),
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
    for name in ("api_balance", "api_address_inscriptions", "api_address_tokens", "api_wallet_holdings", "api_lookup"):
        start = apps.index(f"async def {name}")
        nxt = apps.find("\n@router.", start + 10)
        body = apps[start:nxt if nxt > start else start + 2500]
        assert "/wallet/address/" not in body
        assert "dogesoft" not in body
        assert "dogefeed.app" not in body or name == "api_wallet_holdings"


def test_index_rows_are_labeled_by_the_catalog_and_coins_stay_out():
    """The public billy.dogefeed pieces, as the index would report them, pick up DogeFeed's names."""
    pieces = parse_profile_assets(ASSETS)
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

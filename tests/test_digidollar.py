"""The DigiDollar pilot node module (examples/packages/digidollar): a package this arcade reads, and a mint built as
DigiByte Core 9.26.7 builds one. A mint on a real DigiByte regtest is in test_digidollar_regtest.py."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_module_host import _key  # noqa: E402

from arcade import packages  # noqa: E402

PILOT = Path(__file__).parent.parent / "examples" / "packages" / "digidollar"


def test_the_pilot_package_is_a_module_this_arcade_reads():
    manifest, files = packages.read(PILOT)
    assert manifest["kind"] == "module" and set(manifest["commands"]) == {
        "ddstats", "ddcollateral", "ddgather", "ddmint", "ddvaults", "ddredeem", "ddmainnet"}
    shown = [n for n, c in manifest["commands"].items() if "form" in c]
    assert shown == ["ddstats", "ddcollateral", "ddgather", "ddmint", "ddvaults", "ddmainnet"], \
        "a control for every step; a redeem is the vault list's own button"
    assert manifest["commands"]["ddmainnet"]["audience"] == "operator"
    assert "lib/taproot.py" in files and not any("wallet" in m for m in manifest["needs"]["chains"]["dgb"])
    assert manifest["needs"]["chains"]["dgbtest"] == manifest["needs"]["chains"]["dgb"]


def test_the_pilot_mints_as_digibyte_core_builds_a_mint():
    sys.path.insert(0, str(PILOT))
    try:
        import importlib
        tr = importlib.import_module("lib.taproot")
        pilot = importlib.import_module("module")
    finally:
        sys.path.remove(str(PILOT))
    # BIP-341's wallet test vectors: a key-path-only output, and one with a single leaf.
    assert tr.tweak(bytes.fromhex("d6889cb081036e0faefa3a35157ad71086b123b2b144b649798b494c300a961d")).hex() \
        == "53a1f6e454df1aa2776a2814a721372d6258050de330b3c6d10ee8f4e0dda343"
    root = tr.leaf(bytes.fromhex("20d85a959b0290bf19bb89ed43c916be835475d013da4b362117393e25a48229b8ac"))
    assert root.hex() == "5b75adecf53548f3ec6ad7d78383bf84cc57b55a3127c72b9a2481752dd88b21"
    assert tr.tweak(bytes.fromhex("187791b6f712a8ea41c8ecdd0ee77fab3e85263b37e1ec18a3651926b3a6cf27"), root).hex() \
        == "147c9c57132f6e7ecddba9800bb0c4449251c92a1e60371ee77557b6620f3ea3"
    assert [tr.num(n).hex() for n in (0, 1, 127, 128, 255, 10000, -1)] == ["", "01", "7f", "8000", "ff00", "1027", "81"]
    assert tr.push_int(7) == b"\x57" and tr.push_int(400) == b"\x02\x90\x01"

    pub, _ = _key()
    change = b"\x76\xa9\x14" + bytes(20) + b"\x88\xac"
    coins = [{"txid": "11" * 32, "vout": 1, "value": 5 * 10 ** 8}, {"txid": "22" * 32, "vout": 0, "value": 10 ** 12}]
    made = pilot.mint(pub[1:], 10000, 0, 1000, 0, 2 * 10 ** 11, coins, change)
    assert made["version"] == 0x01000770 and made["inputs"] == [{"txid": "22" * 32, "vout": 0}], "the larger coin"
    (vault, vs), (dd, ds), (rec, rs), (back, bs) = [(v, bytes.fromhex(s)) for v, s in made["outputs"]]
    assert vault == 2 * 10 ** 11 and vs[:2] == b"\x51\x20" and dd == 0 and ds == tr.p2tr(tr.tweak(pub[1:]))
    lock = 1000 + 240 + 100
    assert rec == 0 and rs == (b"\x6a\x02DD\x01\x01\x02\x10\x27" + tr.push(tr.num(lock)) + b"\x00\x20" + pub[1:])
    assert bs == change and 10_000_000 < 10 ** 12 - vault - back < 13_000_000, "0.35 DGB a kB, over the 0.1 floor"
    with pytest.raises(ValueError, match="needs"):
        pilot.mint(pub[1:], 10000, 0, 1000, 0, 2 * 10 ** 12, coins, change)


def test_the_pilot_says_its_core_is_still_syncing_rather_than_not_active():
    """A Core whose headers are past DigiDollar's first block but whose checked blocks are not answers every DigiDollar
    read "not yet active" (seen on the Lite test node): the module says why instead."""
    sys.path.insert(0, str(PILOT))
    try:
        import importlib
        pilot = importlib.import_module("module")
    finally:
        sys.path.remove(str(PILOT))

    class Ctx:
        caller = {}
        def rpc(self, chain, method, *params):
            # While the Core catches up, the arcade borrows getblockchaininfo from a sharer further on.
            return {"getblockchaininfo": {"chain": "main", "blocks": 24_505_510, "headers": 24_505_510},
                    "getdigidollardeploymentinfo": {"enabled": False, "activation_height": 23_869_440,
                                                    "thaw_day": {"tip_height": 23_552_457}}}[method]
    for run in (lambda: pilot.command_ddstats(Ctx()), lambda: pilot.command_ddstats(Ctx(), "main"),
                lambda: pilot.command_ddcollateral(Ctx(), 100), lambda: pilot.command_ddmint(Ctx(), 100),
                lambda: pilot.command_ddvaults(Ctx()), lambda: pilot.command_ddredeem(Ctx(), "ab")):
        with pytest.raises(ValueError, match="starts at block 23,869,440, and this arcade's DigiByte Core has checked "
                                             "blocks to 23,552,457: it is still syncing"):
            run()


def _pilot():
    sys.path.insert(0, str(PILOT))
    try:
        import importlib
        return importlib.import_module("lib.taproot"), importlib.import_module("module")
    finally:
        sys.path.remove(str(PILOT))


def test_the_pilot_redeems_a_vault_by_its_normal_path_and_burns_what_it_minted():
    from arcade import taproot
    tr, pilot = _pilot()
    pub, _ = _key()
    owner, pay = pub[1:], b"\x76\xa9\x14" + bytes(20) + b"\x88\xac"
    made = pilot.mint(owner, 10000, 0, 1000, 0, 2 * 10 ** 11, [{"txid": "22" * 32, "vout": 0, "value": 10 ** 12}], pay)
    vault_script = bytes.fromhex(made["outputs"][0][1])
    vault = {"txid": "aa" * 32, "cents": 10000, "lock": 1340, "tier": 0, "collateral": 2 * 10 ** 11}
    held = [{"txid": "bb" * 32, "vout": 1, "cents": 2500}, {"txid": "aa" * 32, "vout": 1, "cents": 10000}]
    fee_coin = [{"txid": "cc" * 32, "vout": 3, "value": 10 ** 8}]
    said = pilot.redeem(vault, owner, held, fee_coin, pay)
    assert said["version"] == 0x03000770 and said["locktime"] == 1340
    (v, *dd), fees = said["inputs"][:-1], said["inputs"][-1]
    assert v["txid"] == "aa" * 32 and v["vout"] == 0 and v["sequence"] == 0xFFFFFFFE
    assert taproot.leaf_mine(vault_script, bytes.fromhex(v["taproot"]["leaf"]), bytes.fromhex(v["taproot"]["control"]),
                             pub), "the leaf and control block open the vault the mint made, for this key"
    assert [(c["txid"], c["taproot"]) for c in dd] == [("aa" * 32, {"key": True})], "the mint's own dollars, enough"
    assert fees == {"txid": "cc" * 32, "vout": 3}
    (back, bs), (change, cs) = [(o[0], bytes.fromhex(o[1])) for o in said["outputs"]]
    assert back == 2 * 10 ** 11 and bs == pay and cs == pay and 10_000_000 <= 10 ** 8 - change <= 30_000_000

    said = pilot.redeem({**vault, "cents": 11000}, owner, held, fee_coin, pay)
    outs = [(o[0], bytes.fromhex(o[1])) for o in said["outputs"]]
    assert len(said["inputs"]) == 4 and outs[1] == (0, tr.p2tr(tr.tweak(owner))), "over the vault's: change in DD"
    assert outs[2] == (0, b"\x6a\x02DD\x01\x03" + tr.push(tr.num(1500)))
    with pytest.raises(ValueError, match="burns at least what the vault minted"):
        pilot.redeem({**vault, "cents": 20000}, owner, held, fee_coin, pay)


def test_the_pilot_spends_mined_coins_only_once_ripe_and_asks_to_gather_many_small_ones():
    _, pilot = _pilot()

    class Ctx:
        caller = {}
        def rpc(self, chain, method, *params):
            return {"height": 1000, "unspents": [
                {"txid": "aa" * 32, "vout": 0, "amount": 1043, "coinbase": True, "height": 950, "scriptPubKey": "76a9"},
                {"txid": "bb" * 32, "vout": 0, "amount": 1043, "coinbase": True, "height": 901, "scriptPubKey": "76a9"},
                {"txid": "cc" * 32, "vout": 1, "amount": 5, "coinbase": False, "height": 999, "scriptPubKey": "76a9"}]}
    coins, _ = pilot._coins(Ctx(), "dgbtest", "s-mine")
    assert [c["txid"] for c in coins] == ["bb" * 32, "cc" * 32], "a block mined 50 deep is not ripe; 100 deep is"
    many = [{"txid": f"{n:064x}", "vout": 0, "value": 1043 * 10 ** 8} for n in range(300)]
    with pytest.raises(ValueError, match="gather them first"):
        pilot.mint(_key()[0][1:], 10000, 0, 1000, 0, 319_015 * 10 ** 8, many, b"\x76")
    # The fee caps a DigiDollar spend at a few coins (0.05 DGB each at its rate; the arcade allows the module 0.5).
    made = pilot.mint(_key()[0][1:], 10000, 0, 1000, 0, 7 * 1043 * 10 ** 8, many, b"\x76")
    assert len(made["inputs"]) == 8
    with pytest.raises(ValueError, match="gather them first"):
        pilot.mint(_key()[0][1:], 10000, 0, 1000, 0, 9 * 1043 * 10 ** 8, many, b"\x76")


def test_a_gather_puts_up_to_fifty_of_the_smallest_coins_into_one_at_the_relay_fee():
    _, pilot = _pilot()
    coins = [{"txid": f"{n:064x}", "vout": n % 3, "value": (n + 1) * 10 ** 8} for n in range(120)]
    script = b"\x76\xa9\x14" + bytes(20) + b"\x88\xac"
    made = pilot.gather(coins, script, "dgb")
    assert made["chain"] == "dgb" and made["version"] == 2 and len(made["inputs"]) == 50
    assert {i["txid"] for i in made["inputs"]} == {f"{n:064x}" for n in range(50)}, "the smallest first"
    (value, out), = made["outputs"]
    fee = sum(range(1, 51)) * 10 ** 8 - value
    assert bytes.fromhex(out) == script and 1_000_000 < fee < 2_000_000, "about 0.015 DGB for 50 coins"
    assert "70 more after this" in made["what"]
    with pytest.raises(ValueError, match="one coin already"):
        pilot.gather(coins[:1], script)


class _Chain:
    """A DigiByte Core for the module's commands: `chain` says which network it answers as."""
    def __init__(self, pub, chain="test", height=2000, coins=(), outs=()):
        self.pub, self.chain, self.height, self.coins, self.outs = pub, chain, height, list(coins), dict(outs)

    def rpc(self, net, method, *params):
        if method == "getdigidollardeploymentinfo":
            return {"enabled": True}
        if method == "getblockchaininfo":
            return {"chain": self.chain, "blocks": self.height}
        if method == "getblockcount":
            return self.height
        if method == "estimatecollateral":
            return {"lock_days": 0 if params[1] == 0 else 30, "wallet_collateral_dgb": 1000, "effective_ratio": 500}
        if method == "scantxoutset":
            return {"height": self.height, "unspents": self.coins}
        if method == "gettxout":
            return self.outs.get((params[0], params[1]))
        raise AssertionError(method)


def _ctx(pub, tmp_path, audience="user", **chain):
    from types import SimpleNamespace
    core = _Chain(pub, **chain)
    caller = {"audience": audience, "address": {"dgb": "D-mine", "dgbtest": "s-mine"},
              "pubkey": {"dgb": pub.hex(), "dgbtest": pub.hex()}}
    return SimpleNamespace(rpc=core.rpc, caller=caller, data=tmp_path)


def test_mainnet_mints_wait_for_the_operator_and_keep_mainnet_s_rules(tmp_path):
    _, pilot = _pilot()
    pub, _ = _key()
    coin = [{"txid": "aa" * 32, "vout": 0, "amount": 5000, "coinbase": False, "height": 10, "scriptPubKey": "76a9"}]
    main = _ctx(pub, tmp_path, chain="main", coins=coin)
    with pytest.raises(ValueError, match="mainnet mints are off on this arcade"):
        pilot.command_ddmint(main, 100, 1, "main")
    with pytest.raises(ValueError, match="only this arcade's operator"):
        pilot.command_ddmainnet(main, "on", "yes")
    op = _ctx(pub, tmp_path, audience="operator", chain="main")
    assert pilot.command_ddmainnet(op)["mainnet_mints"] == "off"
    with pytest.raises(ValueError, match="ddmainnet on yes"):
        pilot.command_ddmainnet(op, "on")
    assert pilot.command_ddmainnet(op, "on", "yes")["mainnet_mints"] == "on"
    with pytest.raises(ValueError, match="tier 0, one hour, is for the test chain"):
        pilot.command_ddmint(main, 100, 0, "main")
    for dollars in (99.99, 100_000.01):
        with pytest.raises(ValueError, match=r"\$100 to \$100,000"):
            pilot.command_ddmint(main, dollars, 1, "main")
    made = pilot.command_ddmint(main, 100, None, "main")["prepared"]
    assert made["chain"] == "dgb" and "until block" in made["what"], "tier 1 unless said, on mainnet"
    assert f"{2000 + 30 * 5760 + 100:,}" in made["what"]
    with pytest.raises(ValueError, match="answers as test"):
        pilot.command_ddmint(_ctx(pub, tmp_path, chain="test", coins=coin), 100, 1, "main")
    with pytest.raises(ValueError, match="answers as main"):
        pilot.command_ddmint(_ctx(pub, tmp_path, chain="main", coins=coin), 100, 0, "test")
    assert pilot.command_ddmainnet(op, "off")["mainnet_mints"] == "off"
    with pytest.raises(ValueError, match="mainnet mints are off"):
        pilot.command_ddmint(main, 100, 1, "main")


def test_an_unlocked_vault_carries_its_redeem_button(tmp_path, monkeypatch):
    _, pilot = _pilot()
    pub, _ = _key()
    vaults = [{"txid": "aa" * 32, "cents": 10000, "lock": 1990, "tier": 0, "collateral": 10 ** 11},
              {"txid": "bb" * 32, "cents": 10000, "lock": 2100, "tier": 0, "collateral": 10 ** 11}]
    monkeypatch.setattr(pilot, "_holdings", lambda ctx, net, owner: [{"txid": "aa" * 32, "vout": 1, "cents": 20000}])
    monkeypatch.setattr(pilot, "_vaults", lambda ctx, net, owner, held: vaults)
    said = pilot.command_ddvaults(_ctx(pub, tmp_path), "test")
    (now, later) = said["vaults"]
    assert now["redeem"] == "now" and now["actions"] == [{"label": "Redeem", "line": f"ddredeem {'aa' * 32} test"}]
    assert later["redeem"] == "in 100 blocks (about 25 minutes)" and "actions" not in later

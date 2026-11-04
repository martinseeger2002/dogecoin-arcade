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
    assert manifest["kind"] == "module" and set(manifest["commands"]) == {"ddstats", "ddcollateral", "ddmint"}
    assert "lib/taproot.py" in files and not any("wallet" in m for m in manifest["needs"]["chains"]["dgb"])



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
    for run in (lambda: pilot.command_ddstats(Ctx()), lambda: pilot.command_ddcollateral(Ctx(), 100),
                lambda: pilot.command_ddmint(Ctx(), 100)):
        with pytest.raises(ValueError, match="starts at block 23,869,440, and this arcade's DigiByte Core has checked "
                                             "blocks to 23,552,457: it is still syncing"):
            run()

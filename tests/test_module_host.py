"""Node modules' host (arcade/modhost.py): a module answers in its own confined process, asks the chain only the reads
its manifest names, and what it prepares to spend is checked against the caller's own coins before any wallet sees it."""
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

sys.path.insert(0, str(Path(__file__).parent))
from test_module_sandbox import _module, needs_sandbox  # noqa: E402

from arcade import funding, modhost, packages  # noqa: E402
from arcade.config import NETWORKS  # noqa: E402
from arcade.script import b58check_encode, hash160  # noqa: E402
from arcade.txbuild import p2pkh_script  # noqa: E402

PEP = NETWORKS["main"]


def _key():
    key = ec.generate_private_key(ec.SECP256K1())
    pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint)
    return pub, b58check_encode(PEP.pubkeyhash_version, hash160(pub))


class Chain:
    """A Core that answers the reads these tests ask, and says which it was asked."""
    def __init__(self, address=""):
        self.address, self.asked = address, []
        self.outs = {}                                     # more coins: (txid, vout) -> (value, script)

    def __call__(self, network):
        if network != "main":
            raise ValueError(f"no {network} chain")
        return PEP, self.call

    def call(self, method, params):
        self.asked.append(method)
        if method == "getblockcount":
            return 812
        if method == "gettxout" and (params[0], params[1]) in self.outs:
            value, script = self.outs[params[0], params[1]]
            return {"value": value / 100_000_000, "scriptPubKey": {"hex": script.hex()}}
        if method == "gettxout":
            return {"value": 1.5, "scriptPubKey": {"hex": p2pkh_script(self.address).hex()}} \
                if params[0] == "aa" * 32 else None
        raise ValueError(f"{method} is not served here")



@pytest.fixture
def installed(tmp_path):
    """A home with the test module installed, and its host."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text('{"secret": "the keys"}')
    pid, row = packages.install(home, _module(tmp_path))
    chain = Chain()
    host = modhost.Host(home, chain)
    yield host, {"packages": {pid: row}}, chain
    host.stop()


@needs_sandbox
def test_a_module_answers_from_its_own_process_and_reads_only_what_it_named(installed):
    host, settings, chain = installed
    hello = host.ask(settings, "hello", {"call": "command", "name": "hello", "args": ["you"]}, {"audience": "anyone"})
    assert hello == {"hello": "you", "height": 812, "audience": "anyone"}
    with pytest.raises(modhost.ModuleFailed, match="may not ask main for dumpprivkey"):
        host.ask(settings, "hello", {"call": "command", "name": "sneak", "args": ["rpc"]}, {})
    with pytest.raises(modhost.ModuleFailed, match="PermissionError"):
        host.ask(settings, "hello", {"call": "command", "name": "sneak", "args": ["home"]}, {})
    assert chain.asked == ["getblockcount"]
    assert host.ask(settings, "hello", {"call": "page", "method": "GET", "path": "x", "query": {"q": "1"}}, {}) \
        == {"type": "text/html", "body": "<p>GET x 1</p>"}
    assert host.running["hello"][1].alive()
    host.stop("hello")
    assert "hello" not in host.running


def test_a_module_changed_on_disk_does_not_run(installed):
    host, settings, _ = installed
    pid = next(iter(settings["packages"]))
    (packages.home_of(host.home, pid) / "module.py").write_text("raise SystemExit")
    with pytest.raises(modhost.ModuleFailed, match="changed on disk"):
        host.ask(settings, "hello", {"call": "command", "name": "hello", "args": []}, {})


def test_a_prepared_transaction_spends_only_the_callers_coins_within_its_fee(installed):
    host, settings, chain = installed
    pub, chain.address = _key()
    coin = {"txid": "aa" * 32, "vout": 0}
    out = [[150_000_000 - 1000, "6a0474657374"]]
    network, unsigned = host.prepare(settings, "hello", {"chain": "main", "inputs": [coin], "outputs": out,
                                                         "what": "a test", "version": 7}, chain.address)
    spent = {**coin, "value": 150_000_000, "address": chain.address}
    assert network == "main" and unsigned.inputs == [spent] and unsigned.fee == 1000 and unsigned.version == 7
    assert unsigned.sighashes == [funding.sighash([spent], unsigned.outputs, 0, p2pkh_script(chain.address),
                                                  version=7).hex()]
    assert unsigned.raw.startswith("07000000") and unsigned.what == "Hello: a test"
    for prepared, said in (({"chain": "main", "inputs": [{"txid": "bb" * 32, "vout": 0}], "outputs": out},
                            "not an unspent coin of yours"),
                           ({"chain": "main", "inputs": [coin], "outputs": [[150_000_000 - 60_000, "6a"]]},
                            "more than it may"),
                           ({"chain": "doge", "inputs": [coin], "outputs": out}, "which it does not name")):
        with pytest.raises(modhost.ModuleFailed, match=said):
            host.prepare(settings, "hello", prepared, chain.address)
    with pytest.raises(modhost.ModuleFailed, match="not an unspent coin of yours"):
        host.prepare(settings, "hello", {"chain": "main", "inputs": [coin], "outputs": out}, _key()[1])


def test_a_prepared_transaction_assembles_with_its_own_version():
    unsigned = funding.Unsigned("", [{"txid": "aa" * 32, "vout": 0, "value": 1, "address": "x"}], [(0, b"\x6a")],
                                version=0x01000770)
    raw = funding.assemble(unsigned, ["30" + "00" * 8], bytes(33))
    assert raw.startswith("70070001")


def test_the_module_sign_box_says_every_payment_and_the_prepared_spend_is_bounded():
    """2026-11-05 review: the box showed only the module's own words, the fee and the change, so a module could pay
    itself the wallet's coins under any description; and a prepared spend had no limit on its coins."""
    import pathlib
    from arcade import modhost
    box = (pathlib.Path(__file__).resolve().parent.parent / "arcade/lite/templates/_wallet.html").read_text()
    assert "This transaction pays:" in box and "read.pays" in box and "The module says:" in box
    coins = (pathlib.Path(__file__).resolve().parent.parent / "arcade/web/templates/coins.js").read_text()
    assert "a Taproot output" in coins, "a vault is said as a payment, not as bytes"
    assert modhost.MOST_INPUTS <= 100 and modhost.BUSY_WAIT < modhost.TIMEOUT


def redeem_offer(host, settings, chain):
    """A DigiDollar redeem as the pilot prepares it, and the arcade's offer for it: the vault by its normal path, the
    DigiDollars by their key path, and a DGB coin of the caller's for the fee. (pubkey, secret, prepared, unsigned)"""
    import importlib
    from test_taproot import keypair
    pilot_dir = str(Path(__file__).parent.parent / "examples" / "packages" / "digidollar")
    sys.path.insert(0, pilot_dir)
    try:
        tr, pilot = importlib.import_module("lib.taproot"), importlib.import_module("module")
    finally:
        sys.path.remove(pilot_dir)
    pub, secret = keypair()
    # The test module's fee cap is a test's; a redeem pays about 0.2 DGB, under the pilot's 0.5.
    found = host.installed
    host.installed = lambda s: {k: (v[0], {**v[1], "needs": {**v[1]["needs"], "max_fee": 50_000_000}})
                                for k, v in found(s).items()}
    chain.address = b58check_encode(PEP.pubkeyhash_version, hash160(pub))
    pay = p2pkh_script(chain.address)
    made = pilot.mint(pub[1:], 10000, 0, 1000, 0, 2 * 10 ** 9, [{"txid": "22" * 32, "vout": 0, "value": 10 ** 11}], pay)
    chain.outs = {("cc" * 32, 0): (2 * 10 ** 9, bytes.fromhex(made["outputs"][0][1])),
                  ("cc" * 32, 1): (0, bytes.fromhex(made["outputs"][1][1]))}
    vault = {"txid": "cc" * 32, "cents": 10000, "lock": 1340, "tier": 0, "collateral": 2 * 10 ** 9}
    prepared = {**pilot.redeem(vault, pub[1:], [{"txid": "cc" * 32, "vout": 1, "cents": 10000}],
                               [{"txid": "aa" * 32, "vout": 0, "value": 150_000_000}], pay), "chain": "main"}
    return pub, secret, prepared, host.prepare(settings, "hello", prepared, chain.address, pub)[1]


def test_a_prepared_transaction_spends_the_callers_taproot_coins_by_their_key_or_a_leaf_for_it(installed):
    from arcade import taproot
    from test_taproot import keypair, schnorr, tweaked_secret
    host, settings, chain = installed
    pub, secret, prepared, unsigned = redeem_offer(host, settings, chain)
    assert unsigned.version == 0x03000770 and unsigned.locktime == 1340 and unsigned.raw.endswith("3c050000")
    (vault, dd, fee) = unsigned.inputs
    assert vault["taproot"]["leaf"] and dd["taproot"] == {"key": True} and "taproot" not in fee
    assert vault["sequence"] == dd["sequence"] == 0xFFFFFFFE and "sequence" not in fee
    assert unsigned.fee == 150_000_000 - unsigned.outputs[1][0] and unsigned.outputs[0][0] == 2 * 10 ** 9
    shown = unsigned.as_json()["inputs"]
    assert shown[0]["spent"] == chain.outs["cc" * 32, 0][1].hex() and "spent" not in shown[2]
    # The digests are BIP-341's over every coin: signed by the key (leaf) and the tweaked key (key path), they verify.
    for h, by, key in ((unsigned.sighashes[0], secret, pub[1:]),
                       (unsigned.sighashes[1], tweaked_secret(secret), chain.outs["cc" * 32, 1][1][2:])):
        assert taproot.verify(key, bytes.fromhex(h), schnorr(by, bytes.fromhex(h)))
    raw = funding.assemble(unsigned, [schnorr(secret, bytes.fromhex(unsigned.sighashes[0])).hex(),
                                      schnorr(tweaked_secret(secret), bytes.fromhex(unsigned.sighashes[1])).hex(),
                                      "30" + "00" * 8], pub)
    assert raw.startswith("700700030001") and raw.endswith("3c050000"), "segwit, and the vault's lock time"

    other, _ = keypair()
    with pytest.raises(modhost.ModuleFailed, match="not a Taproot coin of your key"):
        host.prepare(settings, "hello", prepared, chain.address, other)
    with pytest.raises(modhost.ModuleFailed, match="open your wallet"):
        host.prepare(settings, "hello", prepared, chain.address)
    sneaky = {**prepared, "inputs": prepared["inputs"][:2] + [{**prepared["inputs"][2], "sequence": 0}]}
    with pytest.raises(modhost.ModuleFailed, match="does not wait"):
        host.prepare(settings, "hello", sneaky, chain.address, pub)
    keyless = {**prepared, "inputs": [{**prepared["inputs"][0], "taproot": {"key": True}}] + prepared["inputs"][1:]}
    with pytest.raises(modhost.ModuleFailed, match="not a Taproot coin of your key"):
        host.prepare(settings, "hello", keyless, chain.address, pub)


def _guard(tmp_path, inscribed: set):
    """The arcade's real inscription guard (arcade/doginals.Doginals), its source answering which transactions carry
    an inscription on output 0."""
    from arcade import doginals

    def fetch(txid):
        return {"txid": txid, "inscriptions": [{"vout": 0, "inscription_id": txid + "i0", "sat_offset": 0}]
                if txid in inscribed else []}
    return doginals.Doginals(tmp_path / "guard.sqlite", fetch=fetch)


def test_a_modules_coin_reads_leave_out_coins_carrying_an_inscription(tmp_path, monkeypatch):
    """SadFrog, 2026-11-05: "make module transactions inscription-aware". On a chain with an inscription guard, a
    module's scantxoutset lists only what an ordinary send would spend; a DigiDollar (no value) is not ordinals'."""
    monkeypatch.setattr(funding, "GUARD_BY_NET", {"main": _guard(tmp_path, {"aa" * 32})})
    said = {"success": True, "height": 900, "total_amount": 3.0, "unspents": [
        {"txid": "aa" * 32, "vout": 0, "amount": 1.0, "scriptPubKey": "76a9"},       # an inscription on it
        {"txid": "bb" * 32, "vout": 0, "amount": 2.0, "scriptPubKey": "76a9"},       # clean
        {"txid": "cc" * 32, "vout": 1, "amount": 0, "scriptPubKey": "5120" + "11" * 32}]}  # a DigiDollar
    kept = modhost.without_inscriptions("main", "scantxoutset", said)
    assert [u["txid"] for u in kept["unspents"]] == ["bb" * 32, "cc" * 32] and kept["total_amount"] == 2.0
    assert modhost.without_inscriptions("dgbtest", "scantxoutset", said) is said, "no guard there: as read"
    assert modhost.without_inscriptions("main", "getblockcount", 812) == 812


def test_a_prepared_spend_of_a_coin_carrying_an_inscription_is_refused(installed, tmp_path, monkeypatch):
    host, settings, chain = installed
    pub, chain.address = _key()
    out = [[150_000_000 - 1000, "6a0474657374"]]
    prepared = {"chain": "main", "inputs": [{"txid": "aa" * 32, "vout": 0}], "outputs": out, "what": "a test"}
    monkeypatch.setattr(funding, "GUARD_BY_NET", {"main": _guard(tmp_path, {"aa" * 32})})
    with pytest.raises(modhost.ModuleFailed, match="carries an inscription"):
        host.prepare(settings, "hello", prepared, chain.address)
    (tmp_path / "x").mkdir()
    monkeypatch.setattr(funding, "GUARD_BY_NET", {"main": _guard(tmp_path / "x", set())})
    assert host.prepare(settings, "hello", prepared, chain.address)[1].fee == 1000, "a clean coin goes"

    class Down:
        def spendable(self, coins):
            raise OSError("the index is not answering")
    monkeypatch.setattr(funding, "GUARD_BY_NET", {"main": Down()})
    with pytest.raises(modhost.ModuleFailed, match="could not be checked for inscriptions"):
        host.prepare(settings, "hello", prepared, chain.address)

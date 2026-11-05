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

    def __call__(self, network):
        if network != "main":
            raise ValueError(f"no {network} chain")
        return PEP, self.call

    def call(self, method, params):
        self.asked.append(method)
        if method == "getblockcount":
            return 812
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

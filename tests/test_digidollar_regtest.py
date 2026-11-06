"""The DigiDollar pilot node module (examples/packages/digidollar) against a real DigiByte Core 9.26.7 on regtest.

A user's DGB, mined to a key no Core holds, is minted into DigiDollars by the module running confined in the arcade's
home: the module prepares the mint, the arcade checks the coins are the user's and makes it an offer, the key signs it
as the user's browser would, and the Core takes it into a block. Once the vault unlocks, the module prepares its
redeem, the key signs the vault and the DigiDollars with Schnorr (BIP-340) and the DGB coin with ECDSA, and the Core
takes that too: the DigiDollars are burned and the DGB comes back. Skipped where `digibyted` is not installed."""
import dataclasses
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_funding import N, _pubkey, _sign                         # noqa: E402,F401
from test_taproot import schnorr, tweaked_secret                   # noqa: E402
from test_web import app_state  # noqa: F401,E402

from arcade import funding, modhost, sandbox                      # noqa: E402
from arcade.config import NETWORKS                                 # noqa: E402
from arcade.lite import commands as cmdlib                         # noqa: E402
from arcade.script import b58check_encode, hash160                 # noqa: E402
from arcade.web.state import ChainContext                          # noqa: E402

PILOT = Path(__file__).parent.parent / "examples" / "packages" / "digidollar"
#: DigiByte regtest: P2PKH addresses on version 126, bech32 "dgbrt" (chainparams.cpp, CRegTestParams).
DGB_REGTEST = dataclasses.replace(NETWORKS["dgb"], pubkeyhash_version=126, bech32_hrp="dgbrt", real_coins=False)

pytestmark = [pytest.mark.skipif(shutil.which("digibyted") is None, reason="digibyted is not installed here"),
              pytest.mark.skipif(bool(sandbox.available()), reason="no module sandbox on this machine")]


@pytest.fixture(scope="module")
def dgb():
    from arcade.regtest import RegtestNode
    with RegtestNode(binary="digibyted", cli="digibyte-cli", extra_args=("-digidollar=1",)) as node:
        yield node


def test_a_user_mints_digidollars_through_the_module_and_signs_with_their_own_key(app_state, dgb, tmp_path):
    import secrets
    secret = secrets.randbelow(N - 1) + 1
    pub = _pubkey(secret)
    address = b58check_encode(DGB_REGTEST.pubkeyhash_version, hash160(pub))
    rpc = dgb.rpc
    rpc.call("generatetoaddress", 700, address)            # past regtest's oracle gate (650), and coins to spend
    rpc.call("setmockoracleprice", 6500)                   # $0.0065 a DGB
    rpc.call("generatetoaddress", 2, address)

    app_state.more = {"dgbtest": ChainContext(network="dgbtest", role="addon", label="DigiByte testnet",
                                              datadir=Path("/nonexistent"))}
    app_state.module_host = modhost.Host(
        app_state.home, lambda net: (DGB_REGTEST, lambda method, params: rpc.call(method, *params)))
    op = cmdlib.Caller(app_state, "operator", local=True)
    cmdlib.run_line(op, f"installmodule {PILOT}")
    cmdlib.run_line(op, "install digidollar")
    try:
        before = cmdlib.run_line(cmdlib.Caller(app_state), "ddstats")
        assert before["chain"] == "DigiByte testnet" and before["supply"] == "$0.00 in DigiDollars"

        offered = []
        user = cmdlib.Caller(app_state, "user", "ab" * 32, address=lambda chain: address,
                             pubkeys={"dgbtest": pub.hex()},
                             offer=lambda net, unsigned: offered.append(unsigned) or {"offer": "o1"})
        assert cmdlib.run_line(user, "ddmint 100 0") == {"sign": {"offer": "o1"},
                                                          "actions": [{"label": "My vaults", "line": "ddvaults test"}]}
        unsigned = offered[0]
        assert unsigned.version == 0x01000770 and unsigned.what.startswith("DigiDollar: mint $100.00 DigiDollar")

        # What the browser's wallet does with the offer: sign each sighash, and the arcade assembles it.
        raw = funding.assemble(unsigned, [_sign(secret, bytes.fromhex(h)).hex() for h in unsigned.sighashes], pub)
        txid = rpc.call("sendrawtransaction", raw)
        rpc.call("generatetoaddress", 1, address)
        assert rpc.call("getrawtransaction", txid, True)["confirmations"] == 1

        after = cmdlib.run_line(cmdlib.Caller(app_state), "ddstats")
        assert after["supply"] == "$100.00 in DigiDollars" and after["vaults_open"] == "1"
        held = cmdlib.run_line(user, "ddvaults")
        (vault,) = held["vaults"]
        assert held["you_hold"] == "$100.00 in DigiDollars" and txid.startswith(vault["vault"])
        assert vault["dollars"] == "$100.00" and vault["status"].startswith("locked: ") and "actions" not in vault
        with pytest.raises(cmdlib.CommandError, match="that vault unlocks at block"):
            cmdlib.run_line(user, f"ddredeem {txid[:12]}")

        # The lock: tier 0 is 240 blocks, and the mint's buffer 100 more.
        rpc.call("generatetoaddress", _unlocks(vault) - rpc.call("getblockcount"), address)
        rpc.call("setmockoracleprice", 6500)
        rpc.call("generatetoaddress", 1, address)
        assert cmdlib.run_line(user, f"ddredeem {txid[:12]}")["sign"] == {"offer": "o1"}
        unsigned = offered[1]
        assert unsigned.version == 0x03000770 and unsigned.locktime == _unlocks(vault)
        kinds = [c.get("taproot") and ("leaf" if c["taproot"].get("leaf") else "key") for c in unsigned.inputs]
        assert kinds[:2] == ["leaf", "key"] and not any(kinds[2:])
        tweaked = tweaked_secret(secret)
        signatures = [(schnorr(secret, bytes.fromhex(h)) if kind == "leaf" else
                       schnorr(tweaked, bytes.fromhex(h)) if kind == "key" else
                       _sign(secret, bytes.fromhex(h))).hex() for kind, h in zip(kinds, unsigned.sighashes)]
        raw = funding.assemble(unsigned, signatures, pub)
        redeemed = rpc.call("sendrawtransaction", raw)
        rpc.call("generatetoaddress", 1, address)
        assert rpc.call("getrawtransaction", redeemed, True)["confirmations"] == 1

        gone = cmdlib.run_line(cmdlib.Caller(app_state), "ddstats")
        assert gone["supply"] == "$0.00 in DigiDollars" and gone["vaults_open"] == "0"
        assert cmdlib.run_line(user, "ddvaults")["vaults"] == "none yet: Mint makes one"
        out = rpc.call("getrawtransaction", redeemed, True)["vout"][0]
        assert f"{out['value']:,.2f} DGB" == vault["locked"], "every DGB the vault locked"
        assert out["scriptPubKey"]["address"] == address, "back to the user's own address"
    finally:
        app_state.module_host.stop()


def _signed(unsigned, secret, pub):
    """What a user's wallet sends back for an offer: Schnorr by the key (a vault's leaf) or the tweaked key (a
    DigiDollar's key path), ECDSA for a DGB coin; the arcade assembles it."""
    kinds = [c.get("taproot") and ("leaf" if c["taproot"].get("leaf") else "key") for c in unsigned.inputs]
    tweaked = tweaked_secret(secret)
    return funding.assemble(unsigned, [(schnorr(secret, bytes.fromhex(h)) if kind == "leaf" else
                                        schnorr(tweaked, bytes.fromhex(h)) if kind == "key" else
                                        _sign(secret, bytes.fromhex(h))).hex()
                                       for kind, h in zip(kinds, unsigned.sighashes)], pub)


def test_digidollars_go_to_another_key_and_back_and_then_redeem_their_vault(app_state, tmp_path):
    """2026-11-05, send and receive: one user mints $100, sends $40 to another user's DigiDollar address, who sends it
    back; the Core takes each send, the supply stays $100, and the vault redeems with the two coins its owner holds."""
    import secrets
    from arcade.regtest import RegtestNode
    with RegtestNode(binary="digibyted", cli="digibyte-cli", extra_args=("-digidollar=1",)) as node:
        _round_trip(app_state, node.rpc, secrets)


def _round_trip(app_state, rpc, secrets):
    users = []
    for _ in range(2):
        secret = secrets.randbelow(N - 1) + 1
        pub = _pubkey(secret)
        users.append((secret, pub, b58check_encode(DGB_REGTEST.pubkeyhash_version, hash160(pub))))
    (sa, pa, aa), (sb, pb, ab) = users
    rpc.call("generatetoaddress", 700, aa)                 # a fresh chain's large early coins, past the oracle gate
    rpc.call("generatetoaddress", 101, ab)                 # the payee's DGB, ripe, for a send's fee
    rpc.call("setmockoracleprice", 6500)
    rpc.call("generatetoaddress", 2, aa)

    app_state.more = {"dgbtest": ChainContext(network="dgbtest", role="addon", label="DigiByte testnet",
                                              datadir=Path("/nonexistent"))}
    app_state.module_host = modhost.Host(
        app_state.home, lambda net: (DGB_REGTEST, lambda method, params: rpc.call(method, *params)))
    op = cmdlib.Caller(app_state, "operator", local=True)
    cmdlib.run_line(op, f"installmodule {PILOT}")
    cmdlib.run_line(op, "install digidollar")
    offered = []

    def caller(address, pub):
        return cmdlib.Caller(app_state, "user", hash160(pub).hex() * 2, address=lambda chain: address,
                             pubkeys={"dgbtest": pub.hex()},
                             offer=lambda net, unsigned: offered.append(unsigned) or {"offer": len(offered)})

    def run(user, line, secret, pub):
        assert "sign" in cmdlib.run_line(user, line)
        txid = rpc.call("sendrawtransaction", _signed(offered[-1], secret, pub))
        rpc.call("generatetoaddress", 1, aa)
        return txid

    a, b = caller(aa, pa), caller(ab, pb)
    try:
        start = cmdlib.run_line(op, "ddstats")["supply"]
        # A miner's wallet: many coins, more than one DigiDollar transaction may spend. Gather until it is one.
        gathered = run(a, "ddgather", sa, pa)
        assert offered[-1].version == 2 and len(offered[-1].inputs) > 8
        assert rpc.call("getrawtransaction", gathered, True)["confirmations"] == 1
        minted = run(a, "ddmint 100 0", sa, pa)
        to_b = cmdlib.run_line(b, "ddreceive")["address"]
        assert to_b.startswith("TD") and cmdlib.run_line(b, "ddreceive")["digidollars"] == 0
        # The address is DigiByte Core's own encoding: the same key in regtest's version is one Core takes.
        regtest_form = tr_base58check(bytes([0xa3, 0xa4]) + pilot_key(to_b))
        rpc.call("createwallet", "only-to-check-addresses")   # the RPC asks for a wallet; it holds none of our keys
        assert rpc.call("validateddaddress", regtest_form)["isvalid"] is True

        sent = run(a, f"ddsend {to_b} 40", sa, pa)
        assert offered[-1].version == 0x02000770
        assert cmdlib.run_line(b, "ddreceive")["digidollars"] == 40
        assert cmdlib.run_line(a, "ddreceive")["digidollars"] == 60
        assert _dollars(cmdlib.run_line(op, "ddstats")["supply"]) == _dollars(start) + 100, "a send makes no dollars"

        back = run(b, f"ddsend {cmdlib.run_line(a, 'ddreceive')['address']} 40", sb, pb)
        assert cmdlib.run_line(b, "ddreceive")["digidollars"] == 0
        held = cmdlib.run_line(a, "ddreceive")
        assert held["digidollars"] == 100 and sorted(c["from"] for c in held["coins"]) == ["a send", "a send"]
        assert {sent, back} <= {c["txid"] for c in held["coins"]} | {sent}

        (vault,) = cmdlib.run_line(a, "ddvaults")["vaults"]
        assert minted.startswith(vault["vault"]), "found by the script kept at its mint, though its own dollars moved"
        rpc.call("generatetoaddress", _unlocks(vault) - rpc.call("getblockcount"), aa)
        rpc.call("setmockoracleprice", 6500)
        rpc.call("generatetoaddress", 1, aa)
        (vault,) = cmdlib.run_line(a, "ddvaults")["vaults"]
        assert vault["actions"] == [{"label": "Redeem", "line": f"ddredeem {minted[:12]} test"}]
        assert vault["status"] == "unlocked: ready to redeem"
        redeemed = run(a, vault["actions"][0]["line"], sa, pa)
        burned = [c for c in offered[-1].inputs if (c.get("taproot") or {}).get("key")]
        assert len(burned) == 2, "the two coins that came back by sends, burned for the vault"
        assert cmdlib.run_line(a, "ddreceive")["digidollars"] == 0
        assert cmdlib.run_line(a, "ddvaults")["vaults"] == "none yet: Mint makes one"
        assert cmdlib.run_line(op, "ddstats")["supply"] == start
        assert rpc.call("getrawtransaction", redeemed, True)["confirmations"] == 1
    finally:
        app_state.module_host.stop()
    assert minted


def tr_base58check(body):
    sys.path.insert(0, str(PILOT))
    try:
        import importlib
        return importlib.import_module("lib.taproot").base58check(body)
    finally:
        sys.path.remove(str(PILOT))


def pilot_key(address):
    sys.path.insert(0, str(PILOT))
    try:
        import importlib
        return importlib.import_module("module").dd_key("dgbtest", address)
    finally:
        sys.path.remove(str(PILOT))


def _unlocks(vault: dict) -> int:
    """The block a vault unlocks at, from ddvaults' words ("block 602,001")."""
    return int(vault["unlocks"].split()[-1].replace(",", ""))


def _dollars(said: str) -> float:
    return float(said.split()[0].lstrip("$").replace(",", ""))

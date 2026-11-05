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
        assert before["chain"] == "regtest" and before["supply_usd"] == 0

        offered = []
        user = cmdlib.Caller(app_state, "user", "ab" * 32, address=lambda chain: address,
                             pubkeys={"dgbtest": pub.hex()},
                             offer=lambda net, unsigned: offered.append(unsigned) or {"offer": "o1"})
        assert cmdlib.run_line(user, "ddmint 100 0") == {"sign": {"offer": "o1"}}
        unsigned = offered[0]
        assert unsigned.version == 0x01000770 and unsigned.what.startswith("DigiDollar: mint $100.00 DigiDollar")

        # What the browser's wallet does with the offer: sign each sighash, and the arcade assembles it.
        raw = funding.assemble(unsigned, [_sign(secret, bytes.fromhex(h)).hex() for h in unsigned.sighashes], pub)
        txid = rpc.call("sendrawtransaction", raw)
        rpc.call("generatetoaddress", 1, address)
        assert rpc.call("getrawtransaction", txid, True)["confirmations"] == 1

        after = cmdlib.run_line(cmdlib.Caller(app_state), "ddstats")
        assert after["supply_usd"] == 100 and after["positions"] == 1
        held = cmdlib.run_line(user, "ddvaults")
        (vault,) = held["vaults"]
        assert held["digidollars"] == 100 and vault["vault"] == txid and vault["dollars"] == 100
        with pytest.raises(cmdlib.CommandError, match="that vault unlocks at block"):
            cmdlib.run_line(user, f"ddredeem {txid[:12]}")

        # The lock: tier 0 is 240 blocks, and the mint's buffer 100 more.
        rpc.call("generatetoaddress", vault["unlocks_at"] - rpc.call("getblockcount"), address)
        rpc.call("setmockoracleprice", 6500)
        rpc.call("generatetoaddress", 1, address)
        assert cmdlib.run_line(user, f"ddredeem {txid[:12]}") == {"sign": {"offer": "o1"}}
        unsigned = offered[1]
        assert unsigned.version == 0x03000770 and unsigned.locktime == vault["unlocks_at"]
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
        assert gone["supply_usd"] == 0 and gone["positions"] == 0
        assert cmdlib.run_line(user, "ddvaults")["vaults"] == []
        out = rpc.call("getrawtransaction", redeemed, True)["vout"][0]
        assert round(out["value"] * 10 ** 8) == round(vault["locks_dgb"] * 10 ** 8), "every DGB the vault locked"
        assert out["scriptPubKey"]["address"] == address, "back to the user's own address"
    finally:
        app_state.module_host.stop()

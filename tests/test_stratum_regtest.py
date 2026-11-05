"""The stratum server (arcade/stratum.py) against a real DigiByte Core 9.26.7 on regtest: a simulated ASIC mines a
scrypt block and a SHA-256 block over stratum, each from that algorithm's own template, with SegWit's commitment in the
coinbase and BIP34's height in it, and the node takes both into its chain, paying the address given. Skipped where `digibyted` is not
installed."""
import dataclasses
import shutil
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_stratum import Asic  # noqa: E402

from arcade import stratum  # noqa: E402
from arcade.config import NETWORKS  # noqa: E402
from arcade.script import b58check_encode  # noqa: E402

DGB_REGTEST = dataclasses.replace(NETWORKS["dgb"], name="dgbreg", pubkeyhash_version=126, bech32_hrp="dgbrt",
                                  real_coins=False)

pytestmark = pytest.mark.skipif(shutil.which("digibyted") is None, reason="digibyted is not installed here")


@pytest.fixture(scope="module")
def dgb():
    from arcade.regtest import RegtestNode
    with RegtestNode(binary="digibyted", cli="digibyte-cli") as node:
        yield node


@pytest.mark.parametrize("algo", ["scrypt", "sha256d"])
def test_an_asic_mines_a_digibyte_block_over_stratum_that_its_node_takes(dgb, algo, request):
    rpc = dgb.rpc
    address = b58check_encode(DGB_REGTEST.pubkeyhash_version, bytes(range(20)))
    have = rpc.call("getblockcount")                       # out of initial block download, and past height 100,
    if have < 100:                                         # where DigiByte regtest's SHA-256 work starts
        rpc.call("generatetoaddress", 100 - have, address)
    before = rpc.call("getblockcount")
    server = stratum.Server(lambda m, p: rpc.call(m, *p), DGB_REGTEST, algo, address, port=0, host="127.0.0.1",
                            difficulty=1e-9).start()
    try:
        port = server._server.sockets[0].getsockname()[1]
        asic = Asic(port, algo)
        request.addfinalizer(asic.sock.close)
        asic.extranonce1 = bytes.fromhex(asic.ask("mining.subscribe", [])["result"][1])
        asic.ask("mining.authorize", ["rig", "x"])
        asic.wait_job()
        assert asic.ask("mining.submit", ["rig", *asic.solve(b"\x00\x00\x00\x07")])["result"] is True
        for _ in range(200):
            if rpc.call("getblockcount") > before:
                break
            time.sleep(0.05)
        assert rpc.call("getblockcount") == before + 1, server.blocks
        block = rpc.call("getblock", rpc.call("getbestblockhash"), 2)
        assert block.get("pow_algo") == algo
        paid = block["tx"][0]["vout"][0]["scriptPubKey"]
        assert paid.get("address") == address or address in paid.get("addresses", [])
        assert server.blocks[-1]["result"] == "accepted"
    finally:
        server.stop()

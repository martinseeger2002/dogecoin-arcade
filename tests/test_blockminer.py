"""Mining a testnet block on this CPU with work from a Core (arcade/blockminer.py; 2026-10-18: "use my
CPU ... to help sad frog mine some testnet LTC using his shared core", and "10% of the revenue should go to the
operator of the shared node"). Against a real regtest litecoind 0.21: a plain block, one carrying a SegWit-era
transaction from the mempool, and one after MWEB is active with a peg-in in it -- each accepted by the node, each
paying a tenth of its reward to the sharer's address in the same coinbase."""

import dataclasses
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from arcade import blockminer as M  # noqa: E402
from arcade.config import NETWORKS  # noqa: E402


def test_the_coinbase_pays_the_miner_and_a_tenth_to_the_sharer():
    params = NETWORKS["ltctest"]
    template = {"height": 1000, "coinbasevalue": 25 * 10 ** 8, "version": 0x20000000, "curtime": 1_700_000_000,
                "bits": "1e0fffff", "previousblockhash": "11" * 32, "transactions": []}
    me = "mipcBbFg9gMiCh81Kj8tqqdgoZub1ZJRfn"          # a testnet pubkey-hash address (version 111)
    work = M.build(template, M.output_script(me, params), 7, (M.output_script(me, params), 10))
    assert work.paid == {"share": 25 * 10 ** 7, "miner": 225 * 10 ** 7}
    assert work.target == M.target_of(0x1E0FFFFF)
    tiny = M.build({**template, "coinbasevalue": 1000}, b"\x51", 7, (b"\x52", 10))
    assert "share" not in tiny.paid, "a share under the dust limit is not paid"


def test_real_coins_and_merge_mined_chains_are_refused():
    for net in ("main", "doge", "ltc"):
        with pytest.raises(M.MiningError):
            M.mine(lambda m, p: None, NETWORKS[net], "x")


def test_an_address_of_another_chain_is_refused():
    with pytest.raises(ValueError):
        M.output_script("DHBKqCfLs1oS6ZNvt4pT3oeC5aYzk7z1BM", NETWORKS["ltctest"])


needs_litecoind = pytest.mark.skipif(shutil.which("litecoind") is None, reason="litecoind is not installed here")


@needs_litecoind
def test_blocks_mined_here_are_accepted_by_litecoind_with_the_sharer_paid():
    from arcade.regtest import LitecoinRegtestNode
    with LitecoinRegtestNode() as node:
        rpc = node.rpc
        params = node.params
        node.generate(110)
        me, sharer = rpc.call("getnewaddress"), rpc.call("getnewaddress")

        def mine_one():
            p = M.mine(lambda m, a: rpc.call(m, *a), params, me, share_to=lambda: sharer, blocks=1)
            found = p.found[0]
            assert found["result"] == "accepted", found
            assert rpc.call("getbestblockhash") == found["hash"]
            block = rpc.call("getblock", found["hash"], 2)
            paid = {a: o["value"] for o in block["tx"][0]["vout"]
                    for a in o["scriptPubKey"].get("addresses", [])}
            assert paid[sharer] * 9 == pytest.approx(paid[me]), paid
            return block

        # MWEB is on early in regtest, and its first block needs a peg-in for the HogEx to start from.
        pegin = rpc.call("sendtoaddress", rpc.call("getnewaddress", "", "mweb"), 5)
        txid = rpc.call("sendtoaddress", rpc.call("getnewaddress", "", "bech32"), 1)
        block = mine_one()                                            # a peg-in, a SegWit spend, the HogEx, MWEB
        # The wallet names a peg-in by its MWEB id; in the block its ordinary part has its own. Both went in.
        assert txid in {t["txid"] for t in block["tx"]} and pegin and rpc.call("getrawmempool") == []
        assert block["tx"][-1]["vin"], "its HogEx"
        mine_one()                                                    # and one of the coinbase and HogEx alone


def test_several_processes_find_a_block_too():
    """The hashing spread over processes (spawned, as in the web server) finds what one process finds."""
    calls = []
    template = {"height": 5, "coinbasevalue": 10 ** 9, "version": 0x20000000, "curtime": 1_700_000_000,
                "bits": "207fffff", "previousblockhash": "22" * 32, "transactions": []}

    def call(method, args):
        calls.append(method)
        return template if method == "getblocktemplate" else None

    params = NETWORKS["ltctest"]
    p = M.mine(call, params, "mipcBbFg9gMiCh81Kj8tqqdgoZub1ZJRfn", workers=2, blocks=1)
    assert p.found and p.found[0]["result"] == "accepted" and calls == ["getblocktemplate", "submitblock"]
    header = bytes.fromhex(p.found[0]["hash"])[::-1]
    assert header and p.tries >= 1

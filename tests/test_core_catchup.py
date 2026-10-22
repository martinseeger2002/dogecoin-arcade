"""A Core that is behind borrows the chain until it catches up (2026-10-20: "If the block chain is being
re-scanned or being synced on any arcade, it should automatically switch over to borrowing somebody else's core
until the re-scan is finished"). arcade/core_catchup.py; the switch lives in ChainContext.rpc()."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_web import app_state, client  # noqa: F401,E402
from test_admin import LOCAL, public  # noqa: F401,E402

from arcade import core_catchup  # noqa: E402
from arcade.config import CHAIN_NAMES, NETWORKS  # noqa: E402
from arcade.mesh import coreshare  # noqa: E402
from arcade.mesh.coreshare import BorrowedRpc  # noqa: E402
from arcade.rpc import RpcClient, RpcError  # noqa: E402
from arcade.web.state import ChainContext  # noqa: E402

PEER = "a1b2c3d4e5f6a7b8c9d0"
TIP = 6000
SYNCING = {"chain": "test", "blocks": 100, "headers": TIP, "verificationprogress": 0.02,
           "initialblockdownload": True}
AT_TIP = {"chain": "test", "blocks": TIP, "headers": TIP, "verificationprogress": 1.0,
          "initialblockdownload": False}


class FakeCore(RpcClient):
    """This arcade's own Core: answers from `said`, an RpcError raised as the node would, and records every call."""

    def __init__(self, said: dict):
        import itertools
        self.said, self.asked = dict(said), []
        self._ids, self._creds, self._timeout = itertools.count(1), None, 1.0

    def close(self):
        pass

    def _one(self, item):
        method = item["method"]
        self.asked.append(method)
        got = self.said.get(method, f"local {method}")
        if isinstance(got, RpcError):
            return {"id": item.get("id"), "result": None, "error": {"code": got.code, "message": got.message}}
        return {"id": item.get("id"), "result": got, "error": None}

    def _post(self, payload):
        if isinstance(payload, dict):
            return self._one(payload)
        return [self._one(i) for i in payload]


class FakeShare:
    """A CoreShare with one linked arcade sharing `nets`, at TIP."""

    def __init__(self, net="test", peers=(PEER,)):
        self.net, self.peers, self.asked = net, list(peers), []
        self.pools, self.failures = {}, {}

    def sharers(self, net):
        return list(self.peers) if net == self.net else []

    def tips(self, net):
        return {p: {"h": TIP, "b": "00" * 32} for p in self.sharers(net)}

    def call_from(self, net, calls, timeout=60, avoid=frozenset()):
        out = []
        for method, params in calls:
            self.asked.append(method)
            if method == "getblockchaininfo":
                out.append({"r": dict(AT_TIP, chain=CHAIN_NAMES[net])})
            elif method == "getblockhash" and params == [0]:
                out.append({"r": NETWORKS[net].genesis})
            elif method == "getblockcount":
                out.append({"r": TIP})
            elif method == "sendrawtransaction":
                out.append({"r": "ab" * 32})
            else:
                out.append({"r": f"shared {method}"})
        return self.peers[0], out


@pytest.fixture
def setup(monkeypatch):
    """A Pepecoin testnet chain run here, its Core `core` (swap its answers), and a linked sharer `share`."""
    core_catchup.forget()
    monkeypatch.setattr(core_catchup, "CHECK_SECONDS", 3600.0)       # checks only when a test asks for one
    share = FakeShare()
    monkeypatch.setattr(coreshare, "CURRENT", share)
    core = FakeCore({"getblockchaininfo": SYNCING})
    monkeypatch.setattr(core_catchup, "_local_client", lambda chain: core)
    chain = ChainContext(network="test", role="messaging", label="Testnet", datadir=Path("/nonexistent"))
    monkeypatch.setattr(chain, "local_rpc", lambda: core)
    yield SimpleNamespace(chain=chain, core=core, share=share)
    core_catchup.forget()


def test_a_syncing_core_reads_the_chain_from_a_sharer(setup):
    st = core_catchup.check(setup.chain)
    assert st.borrowing and st.reading.why == "syncing"
    with setup.chain.rpc() as rpc:
        assert isinstance(rpc, core_catchup.CatchingUpRpc)
        assert rpc.call("getblockcount") == TIP, "the sharer's tip, not this Core's block 100"
        assert rpc.get_blockchain_info()["blocks"] == TIP
        assert rpc.call("sendrawtransaction", "00") == "ab" * 32, "a broadcast goes through the sharer"
    assert "getblockcount" in setup.share.asked and "sendrawtransaction" in setup.share.asked


def test_wallet_calls_stay_on_this_cores_own_wallet(setup):
    core_catchup.check(setup.chain)
    setup.core.asked.clear()
    with setup.chain.rpc() as rpc:
        for method in ("getbalance", "listunspent", "sendtoaddress", "signrawtransactionwithwallet",
                       "walletprocesspsbt", "getnewaddress", "getwalletinfo", "validateaddress"):
            assert rpc.call(method) == f"local {method}"
        # One batch, split: the reads to the sharer, the wallet here, the answers in the order asked.
        assert rpc.batch([("getblockcount", []), ("getbalance", []), ("getbestblockhash", [])]) == [
            TIP, "local getbalance", "shared getbestblockhash"]
        with pytest.raises(RpcError) as refused:
            rpc.call("scantxoutset", "start", [])
        assert refused.value.code == -28, "a part-synced Core's coin set is not complete"
    wallet = {"getbalance", "listunspent", "sendtoaddress", "signrawtransactionwithwallet", "walletprocesspsbt",
              "getnewaddress", "getwalletinfo", "validateaddress"}
    assert not wallet & set(setup.share.asked), "no wallet call is ever asked of a sharer"
    assert wallet <= set(setup.core.asked) and "getblockcount" not in setup.core.asked


def test_with_no_sharer_this_core_is_used_as_it_is_and_the_panel_says_why(setup):
    setup.share.peers = []
    st = core_catchup.check(setup.chain)
    assert not st.borrowing and st.reading.behind
    with setup.chain.rpc() as rpc:
        assert rpc is setup.core
        assert rpc.call("getblockcount") == "local getblockcount"
    line = core_catchup.say(setup.chain)
    assert line == f"syncing (block 100 of {TIP:,}); no linked arcade shares this chain, so this Core is used as it is"
    assert "the mesh is off" in core_catchup.say(setup.chain, mesh_on=False)


def test_caught_up_comes_back_only_after_two_checks_in_a_row(setup, caplog):
    caplog.set_level("WARNING", logger="arcade.cores")
    assert core_catchup.check(setup.chain).borrowing
    assert core_catchup.say(setup.chain).startswith("syncing: borrowed from arcade a1b2c3d4e5f6 until it catches up")
    setup.core.said["getblockchaininfo"] = AT_TIP
    setup.core.said["getwalletinfo"] = {"scanning": False}
    assert core_catchup.check(setup.chain).borrowing, "one caught-up check is not enough"
    assert core_catchup.say(setup.chain).startswith(f"caught up (block {TIP:,} of {TIP:,}); reads come back")
    setup.core.said["getblockchaininfo"] = SYNCING                   # fell behind again: the count starts over
    assert core_catchup.check(setup.chain).borrowing
    setup.core.said["getblockchaininfo"] = AT_TIP
    assert core_catchup.check(setup.chain).borrowing
    assert not core_catchup.check(setup.chain).borrowing
    assert setup.chain.rpc() is setup.core and core_catchup.say(setup.chain) is None
    switches = [r.message for r in caplog.records]
    assert len(switches) == 2 and "reading the chain from arcade" in switches[0] and "caught up" in switches[1], \
        "one line per switch, none for the checks in between"


@pytest.mark.parametrize("message", ["Loading block index...", "Verifying blocks...", "Rescanning..."])
def test_error_28_while_reindexing_or_rescanning_counts_as_behind(setup, message):
    setup.core.said["getblockchaininfo"] = RpcError(-28, message)
    st = core_catchup.check(setup.chain)
    assert st.borrowing and st.reading.behind
    assert core_catchup.say(setup.chain).startswith(f"starting up ({message[:-3].lower()}): borrowed from arcade")
    with setup.chain.rpc() as rpc:
        assert rpc.call("getblockcount") == TIP


def test_a_wallet_rescan_and_a_catch_up_after_downtime_count_as_behind(setup):
    setup.core.said["getblockchaininfo"] = AT_TIP
    setup.core.said["getwalletinfo"] = {"scanning": {"duration": 40, "progress": 0.3}}
    assert core_catchup.check(setup.chain).reading.why == "rescanning its wallet"
    core_catchup.forget()
    setup.core.said["getwalletinfo"] = {"scanning": False}
    setup.core.said["getblockchaininfo"] = dict(AT_TIP, blocks=TIP - 7)
    assert core_catchup.check(setup.chain).reading.why == "catching up", "7 blocks short of its headers"
    core_catchup.forget()
    setup.core.said["getblockchaininfo"] = dict(AT_TIP, blocks=TIP - 6)
    assert not core_catchup.check(setup.chain).borrowing, "6 short: a block on its way, not a catch-up"


def test_a_quiet_test_chain_at_the_sharers_tip_is_not_behind(setup):
    """verificationprogress is an estimate from time: a test chain with no block for a while sits below 0.9999."""
    setup.core.said["getblockchaininfo"] = dict(AT_TIP, verificationprogress=0.9990)
    assert not core_catchup.check(setup.chain).reading.behind
    setup.core.said["getblockchaininfo"] = dict(AT_TIP, blocks=TIP - 5, headers=TIP - 5, verificationprogress=0.99)
    assert core_catchup.check(setup.chain).reading.behind, "5 behind the sharers: really behind"


def test_a_chain_set_to_borrow_in_admin_is_borrowed_as_before(setup):
    chain = ChainContext(network="test", role="messaging", label="Testnet", borrowed=True)
    with chain.rpc() as rpc:
        assert type(rpc) is BorrowedRpc
    assert not setup.core.asked, "its Core (none) is never checked"
    assert core_catchup._STATES.get("test") is None


def test_a_core_behind_is_not_lent_and_serves_from_itself(setup):
    from arcade.mesh.service import shared_cores
    state = SimpleNamespace(all_chains=[setup.chain], settings=lambda: {}, chain_ready=lambda c: True,
                            payout_address=lambda c: "")
    cs = shared_cores(state)
    core_catchup.check(setup.chain)
    assert cs.shared() == {}, "a Core catching up borrows; it is not offered to other arcades"
    assert cs.serve("test", [("getblockcount", [])]) == [{"r": "local getblockcount"}], "never a sharer's answer"


def test_admin_chains_shows_the_line(public, monkeypatch):
    app, state = public
    core_catchup.forget()
    net = state.messaging.network
    share = FakeShare(net)
    monkeypatch.setattr(coreshare, "CURRENT", share)
    monkeypatch.setattr(core_catchup, "_local_client", lambda chain: FakeCore({"getblockchaininfo": dict(SYNCING, chain=CHAIN_NAMES[net])}))
    try:
        rows = {r["net"]: r for r in app.get("/admin/api/state", headers=LOCAL).json()["chains"]["rows"]}
        assert rows[net]["catchup"] == f"syncing: borrowed from arcade {PEER[:12]} until it catches up " \
                                       f"(block 100 of {TIP:,})"
        assert rows["main"]["catchup"] == f"syncing (block 100 of {TIP:,}); the mesh is off, so this Core is used " \
                                          "as it is", "no sharer of Pepecoin main: kept, and the reason said"
        assert "r.catchup" in app.get("/admin", headers=LOCAL).text
    finally:
        core_catchup.forget()

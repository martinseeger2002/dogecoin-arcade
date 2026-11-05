"""Mining a test chain to the operator's account from Lite's console (arcade/testmine.py, `mine` and `mining`): the
node mines each of a chain's algorithms side by side until stopped, a chain of real coins or a borrowed one is
refused, and one run at a time."""
import contextlib
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_web import app_state, client  # noqa: F401,E402

from arcade import testmine  # noqa: E402
from arcade.config import NETWORKS  # noqa: E402
from arcade.lite import commands as cmdlib  # noqa: E402


class Node:
    """A test chain's node whose `generatetoaddress` finds a block every third call, per algorithm."""
    def __init__(self):
        self.calls, self.lock = [], threading.Lock()

    def call(self, method, *params):
        if method == "generatetoaddress":
            with self.lock:
                self.calls.append(params)
                mine = [p for p in self.calls if p[3:] == params[3:]]
            time.sleep(0.01)
            return [f"{params[3] if len(params) > 3 else 'one'}-{len(mine)}"] if len(mine) % 3 == 0 else []
        raise ValueError(method)

    def get_block_count(self):
        return 595_901


class Chain:
    def __init__(self, network="dgbtest", borrowed=False):
        self.network, self.params, self.borrowed, self.node = network, NETWORKS[network], borrowed, Node()
        self.label = self.params.label

    @contextlib.contextmanager
    def rpc(self):
        yield self.node


def _wait(state, until=5.0):
    end = time.time() + until
    while state.mining is not None and time.time() < end:
        time.sleep(0.02)


def test_digibyte_testnet_is_mined_with_each_of_its_algorithms_until_enough_blocks(app_state):
    chain = Chain()
    said = testmine.start(app_state, chain, "s" + "x" * 33, blocks=4)
    assert said["mining"] and said["chain"] == "DigiByte testnet"
    with pytest.raises(testmine.MineError, match="mining already"):
        testmine.start(app_state, chain, "s" + "x" * 33)
    _wait(app_state)
    assert app_state.mining is None and app_state.last_mined["outcome"] == "found"
    assert len(app_state.last_mined["blocks"]) >= 4
    assert {c[3] for c in chain.node.calls} == {"odo", "qubit", "skein"}, "each algorithm, by name"
    assert all(c[1] == "s" + "x" * 33 for c in chain.node.calls), "to the address given"


def test_a_run_stops_when_told_and_says_how_it_went(app_state):
    chain = Chain()
    testmine.start(app_state, chain, "s" + "y" * 33)
    time.sleep(0.2)
    going = testmine.status(app_state)
    assert going["mining"] and set(going["hashes_per_second"]) <= {"odo", "qubit", "skein"}
    assert testmine.stop(app_state)
    _wait(app_state)
    assert testmine.status(app_state)["last"]["outcome"] == "stopped" and not testmine.stop(app_state)


def test_real_coins_and_borrowed_chains_are_not_mined(app_state):
    from arcade.messaging.miner import MiningError
    with pytest.raises(testmine.MineError, match="borrowed"):
        testmine.start(app_state, Chain(borrowed=True), "s" + "x" * 33)
    with pytest.raises(Exception, match="test chains"):
        testmine.start(app_state, Chain("dgb"), "D" + "x" * 33)
    assert app_state.mining is None and MiningError


def test_the_operator_mines_to_their_own_account_from_the_console(app_state, monkeypatch):
    chain = Chain()
    monkeypatch.setattr(type(app_state), "all_chains", property(lambda s: [chain]))
    op = cmdlib.Caller(app_state, "operator", "ab" * 32, address=lambda c: "s-mine", local=True)
    said = cmdlib.run_line(op, "mine dgbtest 3")
    assert said["to"] == "s-mine" and said["until"] == 3
    _wait(app_state)
    assert cmdlib.run_line(op, "mining")["last"]["outcome"] == "found"
    with pytest.raises(cmdlib.CommandError, match="no ltctest chain"):
        cmdlib.run_line(op, "mine ltctest")
    with pytest.raises(cmdlib.CommandError):
        cmdlib.run_line(cmdlib.Caller(app_state, "user", "cd" * 32), "mine dgbtest")
    assert cmdlib.run_line(op, "mine stop") == {"stopping": False}


def test_arcade_cli_mines_to_the_operators_account_once_its_wallet_said_its_address(app_state, monkeypatch):
    chain = Chain()
    monkeypatch.setattr(type(app_state), "all_chains", property(lambda s: [chain]))
    monkeypatch.setattr(type(app_state), "operator", property(lambda s: "ef" * 32))
    cli = cmdlib.Caller(app_state, "operator", local=True)
    with pytest.raises(cmdlib.CommandError, match="no DigiByte testnet address"):
        cmdlib.run_line(cli, "mine dgbtest 1")
    app_state.set_setting(f"address:dgbtest:{'ef' * 32}", "s-operator")
    assert cmdlib.run_line(cli, "mine dgbtest 1")["to"] == "s-operator"
    _wait(app_state)


def test_admin_mine_has_digibyte_testnets_own_node_mine_it_by_algorithm_to_the_address_given(client, monkeypatch):
    """Admin -> Chains -> Mine on a chain with mine_algos runs testmine, not the panel's scrypt in Python."""
    from arcade import blockminer
    app, state = client
    chain = Chain()
    monkeypatch.setattr(type(state), "all_chains", property(lambda s: [chain]))
    monkeypatch.setattr(blockminer, "mine", lambda *a, **k: pytest.fail("not the Python scrypt miner"))
    local, write = {"host": "127.0.0.1:8420"}, {"x-arcade-admin": "1"}
    to = "stz68hi2MaAvriRgHbzpFzTQPf6NE9kQzN"
    assert [c["net"] for c in app.get("/admin/api/mine", headers=local).json()["chains"]] == ["dgbtest"]
    said = app.post("/admin/api/mine", headers={**local, **write}, json={"net": "dgbtest", "address": to})
    assert said.status_code == 200, said.text
    assert said.json()["running"] and said.json()["algos"] == ["odo", "qubit", "skein"] and said.json()["address"] == to
    assert app.post("/admin/api/mine", headers={**local, **write}, json={"net": "dgbtest"}).status_code == 409
    time.sleep(0.2)
    app.post("/admin/api/mine", headers={**local, **write}, json={"stop": True})
    _wait(state)
    assert not app.get("/admin/api/mine", headers=local).json()["running"]
    assert {c[1] for c in chain.node.calls} == {to}
    refused = app.post("/admin/api/mine", headers={**local, **write}, json={"net": "dgbtest", "pay": "faucet"})
    assert refused.status_code == 400 and "not to the faucet" in refused.text


def test_the_console_mines_to_an_address_it_is_given(app_state, monkeypatch):
    chain = Chain()
    monkeypatch.setattr(type(app_state), "all_chains", property(lambda s: [chain]))
    cli = cmdlib.Caller(app_state, "operator", local=True)
    assert cmdlib.run_line(cli, "mine dgbtest 1 stz68hi2MaAvriRgHbzpFzTQPf6NE9kQzN")["to"] == \
        "stz68hi2MaAvriRgHbzpFzTQPf6NE9kQzN"
    _wait(app_state)
    with pytest.raises(cmdlib.CommandError, match="not a DigiByte testnet address"):
        cmdlib.run_line(cli, "mine dgbtest 1 DPepeIsNotHere")


def test_a_run_a_restart_cut_short_picks_up_again_and_one_that_ended_does_not(app_state, monkeypatch):
    chain = Chain()
    monkeypatch.setattr(type(app_state), "all_chains", property(lambda s: [chain]))
    testmine.start(app_state, chain, "s-kept", blocks=2)
    assert app_state.setting(testmine.SETTING)["address"] == "s-kept", "kept while it runs"
    _wait(app_state)
    assert app_state.setting(testmine.SETTING) is None, "a run that ended is not picked up again"
    # A restart kills the threads where they stand: what was kept is still there, and the next start mines on.
    app_state.set_setting(testmine.SETTING, {"net": "dgbtest", "address": "s-kept", "want": 1, "until": None})
    assert testmine.resume(app_state, wait=5)
    _wait(app_state)
    assert app_state.last_mined["outcome"] == "found" and app_state.setting(testmine.SETTING) is None
    app_state.set_setting(testmine.SETTING, {"net": "dgbtest", "address": "s-kept", "want": 0, "until": 1.0})
    assert not testmine.resume(app_state, wait=1), "a run whose hours are over is not"


def test_the_console_says_how_each_chain_is_mined_and_drives_the_stratum_server(app_state, monkeypatch):
    chain = Chain()
    monkeypatch.setattr(type(app_state), "all_chains", property(lambda s: [chain]))
    op = cmdlib.Caller(app_state, "operator", local=True)
    (row,) = cmdlib.run_line(op, "listmining")
    assert row["net"] == "dgbtest" and row["cpu"]["by"] == "node" and row["asic"]["algos"] == ["sha256d", "scrypt"]
    assert cmdlib.run_line(op, "stratum")["running"] is False
    with pytest.raises(cmdlib.CommandError, match="stratum start <chain> <algo> <address>"):
        cmdlib.run_line(op, "stratum start dgbtest")
    with pytest.raises(cmdlib.CommandError, match="not mined with odo"):
        cmdlib.run_line(op, "stratum start dgbtest odo stz68hi2MaAvriRgHbzpFzTQPf6NE9kQzN 3399")
    assert cmdlib.run_line(op, "stratum stop")["on"] is False

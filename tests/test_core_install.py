"""Installing a chain's node from Admin -> Coins & chains, and what each chain costs on disk (2026-10-27: "From
the chains and coins tab, the operator should be able to decide to install a block chain node on the computer. The
storage cost should also be displayed for chains"; "The size of all indexes should be broadcast to the network")."""
import hashlib
import io
import pathlib
import sys
import tarfile
import zipfile

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_admin import LOCAL, WRITE, public                     # noqa: F401,E402

from arcade import core_install, disk_sizes                      # noqa: E402
from arcade.config import NETWORKS                               # noqa: E402
from arcade.mesh.operators import OperatorsShare                 # noqa: E402


def test_every_add_on_chain_is_offered_or_says_why():
    for net, params in NETWORKS.items():
        if not params.addon:
            continue
        for mach in ("x86_64", "aarch64"):
            got = core_install.offer(net, mach)
            assert got["available"] or got["why"], net
            if got["available"]:
                rel = core_install.RELEASES[net]
                name, sha, size = rel.assets[mach]
                assert len(sha) == 64 and size > 1_000_000 and name in rel.url + name
    assert not core_install.offer("kot", "x86_64")["available"], "Kotia: not checked against its new release yet"
    assert core_install.offer("doge", "riscv64")["available"] is False


def test_a_peers_disk_word_is_kept_only_in_its_shape():
    said = {"doge": {"core": 230 * 10 ** 9, "ord": 31 * 10 ** 9}, "ltc": {"core": -5, "ord": "big"},
            "x" * 40: {"core": 1}, "dgb": {"core": True}, "bel": {"core": 10 ** 20}}
    assert disk_sizes.clean(said) == {"doge": {"core": 230 * 10 ** 9, "ord": 31 * 10 ** 9}}
    assert disk_sizes.clean("nonsense") == {}


def test_the_estimate_is_this_arcades_own_or_the_largest_a_linked_arcade_said():
    peers = {"a1b2c3d4e5f60000": {"tag": "strawberry", "disk": {"doge": {"core": 200, "ord": 30}}},
             "f0f0f0f0f0f00000": {"tag": "", "disk": {"doge": {"core": 150}}}}
    est = disk_sizes.estimate("doge", {}, peers)
    assert est["core"] == {"bytes": 200, "who": "@strawberry"} and est["ord"]["bytes"] == 30
    assert disk_sizes.estimate("doge", {"doge": {"core": 99}}, peers)["core"] == {"bytes": 99, "who": "this arcade"}
    assert disk_sizes.estimate("ltc", {}, peers) == {}


def test_the_operator_hello_carries_the_disk_sizes():
    import asyncio
    asyncio.run(_hello())


async def _hello():
    class Link:
        peer_id = "p" * 16
        sent = []

        async def send(self, frame):
            self.sent.append(frame)
    share = OperatorsShare(lambda: {"name": "x", "disk": {"doge": {"core": 5 * 10 ** 9}}})
    link = Link()
    await share._say(link)
    assert link.sent[-1]["disk"] == {"doge": {"core": 5 * 10 ** 9}}
    await share.on_frame(link, {"op": "me", "tag": "strawberry", "disk": {"doge": {"core": 7, "ord": 3}, "bad": 1}}, b"")
    assert share.peers[link.peer_id]["disk"] == {"doge": {"core": 7, "ord": 3}}


def test_folder_and_index_sizes(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "blk00000.dat").write_bytes(b"x" * 1000)
    (tmp_path / "a" / "b").mkdir()
    (tmp_path / "a" / "b" / "c").write_bytes(b"y" * 24)
    assert disk_sizes.folder_bytes(tmp_path / "a") == 1024
    (tmp_path / "ord-doge.sqlite").write_bytes(b"z" * 100)
    (tmp_path / "ord-doge.sqlite-wal").write_bytes(b"z" * 10)
    (tmp_path / "ord-doge.sqlite.backfill").write_bytes(b"z" * 5000)
    assert disk_sizes.index_bytes(tmp_path, "doge") == 110, "a backfill is temporary, not what keeping costs"


def _fake_release(tmp_path, kind="tar"):
    """An archive with a daemon and a cli, and a Release pinning it."""
    buf = io.BytesIO()
    if kind == "tar":
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            for name in ("fake-1.0/bin/faked", "fake-1.0/bin/fake-cli"):
                data = b"#!/bin/sh\nexit 0\n"
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(data), 0o755
                t.addfile(info, io.BytesIO(data))
        name = "fake-1.0-x86_64-linux-gnu.tar.gz"
    else:
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("faked-linux-x86_64", "#!/bin/sh\nexit 0\n")
            z.writestr("fake-cli-linux-x86_64", "#!/bin/sh\nexit 0\n")
        name = "fake-1.0-linux-x86_64.zip"
    raw = buf.getvalue()
    (tmp_path / "srv").mkdir(exist_ok=True)
    (tmp_path / "srv" / name).write_bytes(raw)
    daemon, cli = (("fake-1.0/bin/faked", "fake-1.0/bin/fake-cli") if kind == "tar"
                   else ("faked-linux-{m}", "fake-cli-linux-{m}"))
    return core_install.Release("fake", "1.0", (tmp_path / "srv").as_uri() + "/",
                                {"x86_64": (name, hashlib.sha256(raw).hexdigest(), len(raw))}, daemon, cli), raw


@pytest.mark.parametrize("kind", ["tar", "zip"])
def test_an_install_checks_the_pin_writes_the_config_and_starts_the_node(tmp_path, monkeypatch, client, kind):
    app, state = client
    rel, _ = _fake_release(tmp_path, kind)
    monkeypatch.setitem(core_install.RELEASES, "dgb", rel)
    monkeypatch.setattr(core_install, "machine", lambda: "x86_64")
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    started, reloaded = [], []
    monkeypatch.setattr(core_install, "start_one", lambda installed: started.append(installed) or True)
    monkeypatch.setattr(core_install, "reload_arcade", lambda home: reloaded.append(home))
    inst = core_install.Installer(state)
    inst.start("dgb", NETWORKS["dgb"], free_bytes=10 ** 12, need_bytes=10 ** 9)
    for _ in range(200):
        if inst.jobs["dgb"]["state"] != "working":
            break
        __import__("time").sleep(0.02)
    assert inst.jobs["dgb"]["state"] == "done", inst.jobs["dgb"]
    conf = tmp_path / NETWORKS["dgb"].node_dir / NETWORKS["dgb"].node_conf
    text = conf.read_text()
    assert "txindex=1" in text and "rpcallowip=127.0.0.1" in text and "rpcpassword=" in text
    assert oct(conf.stat().st_mode & 0o777) == "0o600"
    kept = state.setting("core_installed:dgb")
    assert pathlib.Path(kept["daemon"]).is_file() and started and reloaded
    assert state.setting("core:dgb") == "here"


def test_a_download_that_is_not_the_pinned_one_is_refused(tmp_path, monkeypatch, client):
    app, state = client
    rel, raw = _fake_release(tmp_path)
    bad = core_install.Release(rel.family, rel.version, rel.url, {"x86_64": (rel.assets["x86_64"][0], "0" * 64,
                                                                             len(raw))}, rel.daemon, rel.cli)
    monkeypatch.setitem(core_install.RELEASES, "dgb", bad)
    monkeypatch.setattr(core_install, "machine", lambda: "x86_64")
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    inst = core_install.Installer(state)
    inst.start("dgb", NETWORKS["dgb"], free_bytes=10 ** 12, need_bytes=None)
    for _ in range(200):
        if inst.jobs["dgb"]["state"] != "working":
            break
        __import__("time").sleep(0.02)
    assert inst.jobs["dgb"]["state"] == "failed" and "SHA-256" in inst.jobs["dgb"]["step"]
    assert not (tmp_path / NETWORKS["dgb"].node_dir / NETWORKS["dgb"].node_conf).exists()
    assert state.setting("core_installed:dgb") is None


def test_an_existing_config_or_too_little_disk_is_refused(tmp_path, monkeypatch, client):
    app, state = client
    monkeypatch.setattr(core_install, "machine", lambda: "x86_64")
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    inst = core_install.Installer(state)
    with pytest.raises(ValueError, match="free"):
        inst.start("doge", NETWORKS["doge"], free_bytes=10 ** 9, need_bytes=200 * 10 ** 9)
    folder = tmp_path / NETWORKS["doge"].node_dir
    folder.mkdir()
    (folder / NETWORKS["doge"].node_conf).write_text("rpcuser=mine\n")
    with pytest.raises(ValueError, match="already there"):
        inst.start("doge", NETWORKS["doge"], free_bytes=10 ** 13, need_bytes=None)
    assert (folder / NETWORKS["doge"].node_conf).read_text() == "rpcuser=mine\n", "never overwritten"


def test_the_admin_routes(public):
    app, state = public
    got = app.get("/admin/api/cores/install", headers=LOCAL).json()
    assert "free" in got and "doge" in got["rows"] and "main" not in got["rows"]
    assert app.post("/admin/api/cores/install", headers=LOCAL, json={"net": "doge"}).status_code == 403
    assert app.post("/admin/api/cores/install", headers={**LOCAL, **WRITE}, json={"net": "main"}).status_code == 400
    assert app.post("/admin/api/cores/install", headers={**LOCAL, **WRITE}, json={"net": "kot"}).status_code == 400
    page = app.get("/admin", headers=LOCAL).text
    assert "function loadNodes()" in page


def test_a_folder_with_only_a_config_is_not_counted_as_the_chain(tmp_path):
    """.25 (2026-10-27): ~/.dogecoin held only dogecoin.conf; its node's data lives in /var/lib/dogecoind. Only a folder
    with blocks/ is a Core's data folder, and a config's datadir= is followed."""
    from types import SimpleNamespace
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    data = tmp_path / "data"
    (data / "blocks").mkdir(parents=True)
    (conf_dir / "dogecoin.conf").write_text("rpcuser=x\n")
    chain = SimpleNamespace(datadir=None, conf=conf_dir / "dogecoin.conf", params=NETWORKS["doge"])
    assert disk_sizes.node_folder(chain) in (None,) or disk_sizes.node_folder(chain) != conf_dir
    (conf_dir / "dogecoin.conf").write_text(f"datadir={data}\n")
    assert disk_sizes.node_folder(chain) == data


def test_an_installed_core_on_a_chain_set_to_borrow_or_off_is_not_started(monkeypatch):
    """2026-11-03, the ThinkPad: DigiByte, set to borrow after its node ran the machine out of memory, was started again
    at the arcade's next start because start_installed looked only at what was installed. Only chains run here start;
    an installed chain with no setting is run here, as before."""
    started = []
    monkeypatch.setattr(core_install, "start_one", lambda installed: started.append(installed["net"]) or True)

    class State:
        def settings(self):
            return {"core_installed:dgb": {"net": "dgb"}, "core:dgb": "borrow",
                    "core_installed:lky": {"net": "lky"}, "core:lky": "off",
                    "core_installed:wjk": {"net": "wjk"}, "core:wjk": "here",
                    "core_installed:flop": {"net": "flop"}}
    assert sorted(core_install.start_installed(State())) == ["flop", "wjk"]
    assert sorted(started) == ["flop", "wjk"]


# --- what the operator is told while a node installs (2026-11-04: "there is no indication of what is happening or
# what to do next"; chose the steps in the chain's row and notifications that follow him around Admin) -------------

def _states(got):
    return [s["state"] for s in got["steps"]]


def test_the_steps_say_where_an_install_is_how_far_each_is_and_whether_it_is_still_going_on():
    """Seven steps, each with how far it is (the line to it fills: 2026-11-04 chose that mockup)."""
    S = core_install.steps
    down = S({"state": "working", "phase": "download", "done": 5e6, "total": 10e6}, None, False)
    assert _states(down) == ["now", "todo", "todo", "todo", "todo", "todo", "todo"] and down["active"]
    assert down["steps"][0]["p"] == 0.5 and down["steps"][0]["num"] == "5 MB / 10 MB"
    failed = S({"state": "failed", "phase": "check", "step": "SHA-256"}, None, False)
    assert _states(failed)[:2] == ["done", "failed"] and not failed["active"]
    node = {"blocks": 900, "headers": 183_016}
    asked = S(None, {"phase": "asked", "from": "@think", "node": node}, True)
    assert _states(asked) == ["done", "done", "done", "now", "todo", "todo", "todo"]
    heads = S(None, {"phase": "headers", "from": "@think", "target": 915_080, "copy_bytes": 3.1e9, "node": node,
                     "eta_headers": 400}, True)
    assert _states(heads) == ["done", "done", "done", "done", "now", "todo", "todo"]
    h = heads["steps"][4]
    assert h["label"] == "Learn headers" and h["num"] == "183,016 / 915,080" and abs(h["p"] - 0.2) < 0.001 and h["eta"] == 400
    assert heads["steps"][3]["num"] == "@think · 3.1 GB" and heads["steps"][5]["num"] == "0 / 3.1 GB"
    assert heads["pct"] == 18 and heads["eta"] == 400
    copying = S(None, {"phase": "copying", "from": "@think", "target": 915_080, "done": 1.55e9, "total": 3.1e9,
                       "eta": 200, "node": {"blocks": 900, "headers": 915_080}}, True)
    assert _states(copying) == ["done"] * 5 + ["now", "todo"] and copying["steps"][5]["p"] == 0.5
    assert copying["steps"][5]["label"] == "Copy data" and copying["steps"][4]["p"] == 1
    checking = S(None, {"phase": "checking", "from": "@think", "node": node}, True)
    assert _states(checking) == ["done"] * 6 + ["now"]
    synced = {"blocks": 915_000, "headers": 915_001}
    done = S(None, {"phase": "done", "from": "@think", "node": synced}, True)
    assert _states(done) == ["done"] * 7 and not done["active"] and done["pct"] == 100
    last = S(None, {"phase": "done", "node": {"blocks": 914_000, "headers": 915_000}}, True)
    assert _states(last)[-1] == "now" and "1,000" in last["steps"][-1]["num"]
    own = S(None, {"phase": "none", "target": 915_080, "node": {"blocks": 900, "headers": 400_000}}, True)
    assert _states(own) == ["done", "done", "done", "skip", "now", "todo", "todo"]
    assert own["steps"][5]["label"] == "Sync chain" and own["steps"][3]["num"] == "none offered"
    syncing = S(None, {"phase": "own", "node": {"blocks": 300_000, "headers": 915_080}}, True)
    assert _states(syncing) == ["done", "done", "done", "skip", "done", "now", "todo"]
    assert syncing["steps"][5]["num"] == "300,000 / 915,080" and syncing["steps"][3]["num"] == "you chose: none"
    starting = S(None, None, True)
    assert _states(starting) == ["done", "done", "now", "skip", "todo", "todo", "todo"], "starting: no word yet"
    assert S(None, None, False) == {"steps": [], "active": False, "pct": 0}, "nothing installed: no steps"


def test_a_cancelled_install_keeps_the_wallet_borrows_again_and_can_be_installed_again(tmp_path, monkeypatch, client):
    from arcade import core_copy
    app, state = client
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    folder, conf = core_install.node_paths(NETWORKS["flop"])
    for top in ("blocks", "chainstate", "indexes"):
        (folder / top).mkdir(parents=True)
        (folder / top / "x").write_text("data")
    (folder / "wallet.dat").write_text("keys")
    conf.write_text(core_install.CONF_MARK + " (Admin -> Coins & chains -> Install a node).\nserver=1\n")
    (folder.with_name(folder.name + ".arcade-copy")).mkdir()
    state.set_setting("core_installed:flop", {"datadir": str(folder), "conf": str(conf), "daemon": "/x"})
    state.set_setting("core:flop", "here")
    stopped, reloaded = [], []
    monkeypatch.setattr(core_copy.Core, "stop", lambda self: stopped.append(1))
    monkeypatch.setattr(core_install, "reload_arcade", lambda home: reloaded.append(home))
    said = app.post("/admin/api/cores/copy", headers={**LOCAL, **WRITE}, json={"net": "flop", "action": "cancel"})
    assert said.status_code == 200, said.text
    assert stopped and reloaded
    assert not any((folder / t).exists() for t in ("blocks", "chainstate", "indexes"))
    assert (folder / "wallet.dat").read_text() == "keys" and conf.exists(), "never a wallet, never its config"
    assert not folder.with_name(folder.name + ".arcade-copy").exists()
    assert state.setting("core_installed:flop") is None and state.setting("core:flop") == "borrow"
    # Its config is the arcade's own: installing again is not refused as "a node of yours".
    monkeypatch.setattr(core_install, "offer", lambda net, mach=None: {"available": True, "download": 1})
    monkeypatch.setattr(threading_mod(), "Thread", _NoThread)
    core_install.Installer(state).start("flop", NETWORKS["flop"], free_bytes=10 ** 12, need_bytes=None)
    events = app.get("/admin/api/cores/events", headers=LOCAL).json()["events"]
    assert any("node removed" in e["text"] and e["net"] == "flop" for e in events)
    later = app.get(f"/admin/api/cores/events?since={events[-1]['at']}", headers=LOCAL).json()["events"]
    assert later == []


def threading_mod():
    return core_install.threading


class _NoThread:
    def __init__(self, *a, **kw):
        pass

    def start(self):
        pass


def test_a_node_of_the_operator_s_own_is_never_cancelled_and_sync_on_its_own_is_kept(client):
    app, state = client
    refused = app.post("/admin/api/cores/copy", headers={**LOCAL, **WRITE}, json={"net": "doge", "action": "cancel"})
    assert refused.status_code == 400, "not installed by this arcade: nothing to cancel"
    state.set_setting("core_installed:flop", {"datadir": "/nonexistent", "conf": "/nonexistent/c", "daemon": "/x"})
    own = app.post("/admin/api/cores/copy", headers={**LOCAL, **WRITE}, json={"net": "flop", "action": "own"})
    assert own.status_code == 200 and state.setting("core_copy_own:flop") is True
    assert app.post("/admin/api/cores/copy", headers=LOCAL, json={"net": "flop", "action": "own"}).status_code in (401, 403)


def test_a_test_chain_beside_its_mainnet_is_measured_in_its_own_folder(tmp_path, monkeypatch):
    """2026-11-05, a test node: DigiByte testnet (testnet26/blocks under ~/.digibyte-testnet) was said to take 51.6 GB, the
    mainnet node's folder, found by the running digibyted's -datadir=. A testnet26 folder holds the chain, and only a
    daemon started with this chain's own -conf= is asked."""
    from types import SimpleNamespace
    main, test = tmp_path / "main", tmp_path / "test"
    (main / "blocks").mkdir(parents=True)
    (test / "testnet26" / "blocks").mkdir(parents=True)
    (test / "digibyte.conf").write_text("testnet=1\n")
    chain = SimpleNamespace(datadir=None, conf=test / "digibyte.conf", params=NETWORKS["dgbtest"])
    procs = tmp_path / "proc"
    for pid, conf, data in (("10", main / "digibyte.conf", main), ("11", test / "digibyte.conf", test)):
        (procs / pid).mkdir(parents=True)
        (procs / pid / "cmdline").write_bytes(b"\0".join([b"/x/digibyted", f"-datadir={data}".encode(),
                                                          f"-conf={conf}".encode()]))
    monkeypatch.setattr(disk_sizes, "PROC", procs)
    assert disk_sizes._process_datadir("digibyted", chain.conf) == test
    assert disk_sizes.node_folder(chain) == test

"""A chain's Core data copied from a linked arcade, instead of synced block by block (2026-11-04).

The operator: the ThinkPad's DigiByte node had read 434k of 24.5M blocks after hours, CPU and memory bound, weeks at that
rate. He chose "through the arcade mesh", then: "The bootstrap should work automatically from the coins and chains tab
just like the installer does now", from any linked arcade (asked which: "Any linked arcade").

How it goes:

* MAKING (an arcade whose Core it installed itself runs here, is shared -- `share:<net>`, the Chains tab's checkbox --
  and is synced). Asked by a linked arcade far behind ("sq", arcade/mesh/coreboot.py), it takes a consistent copy of
  its Core's chain data with seconds of downtime: networking off (no new block), the coin set's summary
  (gettxoutsetinfo) at the tip, `stop`; then every file that never changes again is HARD-LINKED into the copy (LevelDB
  tables, block and undo files below the newest two) and the rest copied (LevelDB logs and manifests, the newest block
  files); the Core starts again. Then, on this thread, each file is cut into PIECE-sized pieces and hashed. Only
  blocks/, chainstate/ and indexes/ (indexes/ whole: DigiByte 9.x keeps digidollarstats/ there by itself) -- never a
  wallet, the config, the cookie or the peers. A copy nobody fetched from for SNAP_IDLE is deleted.
* FETCHING (an arcade whose installed Core runs here and is at least WORTH_BLOCKS behind its own header chain): the
  linked arcade with the highest copy, or able to make one, on the same Core version and genesis; its manifest, then
  its pieces, a few at a time, each checked against the manifest, written into <chain folder>.arcade-copy/ (kept
  across restarts: a fetch goes on where it stopped).
* CHECKING. What can be checked, and what cannot, said plainly (exchange-safety.md, "Core data copied from a linked
  arcade"):
  - every piece is the manifest's (the transfer only);
  - the copy's tip, at its height, is the block this node's OWN header chain has there (headers whose work this node
    checked itself): the copy is of the real chain;
  - installed, the Core started with no peers: its tip is the manifest's, its coin-set summary is the manifest's,
    and `verifychain` (level 3) re-checks the last VERIFY_BLOCKS blocks against the coin set;
  - NOT checked: that the maker's coin set is the one the whole chain makes -- that needs every block replayed
    (-reindex-chainstate, what the copy saves). The maker is trusted for it, and named on the Coins & chains tab.
* INSTALLING: the local Core stopped; its blocks/ chainstate/ indexes/ moved aside into <chain folder>.arcade-old/;
  the copy's moved in; the checks above; then the Core started as usual and the old set deleted. Anything off: the old
  set back, the Core started as before, and that maker not tried again.
"""
from __future__ import annotations

import contextlib
try:
    import fcntl
except ImportError:      # Windows (2026-11-06): arcade/winlock.py instead; Linux and macOS unchanged
    fcntl = None
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger("arcade.corecopy")

PIECE = 8 * 1024 * 1024
LAYOUT = 1
FOLDERS = ("blocks", "chainstate", "indexes")
#: LevelDB's lock and its own logs: never copied.
SKIP = {"LOCK", "LOG", "LOG.old", ".lock"}
#: LevelDB tables: written once, never changed, deleted when compacted (a hard link keeps the copy's).
TABLES = (".ldb", ".sst")
#: A copy is worth fetching only this far behind (blocks).
WORTH_BLOCKS = 20_000
#: A copy is made again only when the last is this old.
SNAP_EVERY = 6 * 3600
#: A copy nobody fetched from for this long is deleted.
SNAP_IDLE = 2 * 3600
#: How many of the last blocks verifychain re-checks after an install.
VERIFY_BLOCKS = 288
#: A Core given this long to stop, and to answer again after it starts (a big block index loads for minutes).
STOP_SECONDS = 900
START_SECONDS = 3600
#: Pieces asked at once, and how long one may take.
IN_FLIGHT = 4
PIECE_SECONDS = 120
#: A fetch that got nothing for this long lets that maker go.
FETCH_STALL = 900
#: The arguments a copied Core is checked under: no peers, so nothing moves its tip; no wallet, so the check does not
#: wait on the wallet catching up (it does that once at the next ordinary start: it is the operator's, never touched).
ALONE = ("-maxconnections=0", "-listen=0", "-dnsseed=0", "-disablewallet")
_NUM = re.compile(r"^(blk|rev)(\d+)\.dat$")


class CopyError(ValueError):
    pass


def manifest_id(manifest: dict) -> str:
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def chain_dir(installed: dict, params) -> Path:
    """The folder the Core keeps this chain's data in: its datadir, or the network's folder under it."""
    base = Path(installed["datadir"])
    sub = getattr(params, "datadir_subdir", "") or ""
    return base / sub if sub else base


def _beside(folder: Path, what: str) -> Path:
    return folder.with_name(folder.name + what)


def conf_txindex(installed: dict) -> bool:
    try:
        for line in Path(installed.get("conf") or "").read_text().splitlines():
            k, _, v = line.strip().partition("=")
            if k.strip() == "txindex":
                return v.strip() == "1"
    except OSError:
        pass
    return False


# ------------------------------------------------------------------ the Core

class Core:
    """The installed Core of one chain: its RPC, and stopping and starting it (core_install's way). A test passes its
    own (a regtest node)."""

    def __init__(self, installed: dict, params):
        self.installed, self.params = installed, params

    def rpc(self, timeout: float = 120.0):
        from .config import load_rpc_credentials
        from .rpc import RpcClient
        # Its datadir's .cookie first (the installer's nodes log in by cookie), then rpcuser/rpcpassword in its config.
        return RpcClient(load_rpc_credentials(self.params, conf_path=Path(self.installed["conf"]),
                                              datadir=Path(self.installed["datadir"])), timeout=timeout)

    def call(self, method: str, *args, timeout: float = 120.0):
        with self.rpc(timeout) as r:
            return r.call(method, *args)

    def running(self) -> bool:
        from .core_install import _running
        return _running(self.installed)

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            self.call("stop", timeout=30)
        deadline = time.time() + STOP_SECONDS
        while time.time() < deadline:
            if not self.running():
                return
            time.sleep(1)
        raise CopyError("the node did not stop")

    def start(self, extra: Iterable[str] = ()) -> None:
        from .core_install import start_one
        start_one(self.installed, tuple(extra))

    def wait(self, timeout: float = START_SECONDS) -> None:
        """Until the Core answers (past its warm-up)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.call("getblockcount", timeout=30)
                return
            except Exception:                                  # noqa: BLE001 -- starting, warming up
                time.sleep(2)
        raise CopyError("the node did not answer after it started")


#: The systemd user units the installer writes for a chain's node (installer/install.py, SYSTEMD_UNIT): the arcade
#: stops and starts those through systemd, never behind its back (Restart=always would start one again at once).
SERVICE_UNITS = {"test": "pepecoind-testnet.service", "main": "pepecoind-mainnet.service"}


def service_node(net: str, unit_dir: Path | None = None) -> dict | None:
    """A chain's node the installer runs as a systemd user unit, as an installed-Core record with its `unit`, or None.
    Read from the unit file itself: `ExecStart=<binary> -datadir=<datadir>`."""
    name = SERVICE_UNITS.get(net)
    unit = (unit_dir or Path.home() / ".config/systemd/user") / name if name else None
    try:
        text = unit.read_text() if unit else ""
    except OSError:
        return None
    m = re.search(r"^ExecStart=(\S+)\s+-datadir=(\S+)\s*$", text, re.M)
    if not m:
        return None
    binary, datadir = Path(m.group(1)), Path(m.group(2))
    conf = next((datadir / n for n in ("pepecoin.conf", "dogecoin.conf") if (datadir / n).exists()), datadir / "pepecoin.conf")
    cli = binary.parent / (binary.name[:-1] + "-cli" if binary.name.endswith("d") else binary.name + "-cli")
    return {"daemon": str(binary), "cli": str(cli), "datadir": str(datadir), "conf": str(conf), "unit": name}


class ServiceCore(Core):
    """A node the installer runs as a systemd user unit: stopped and started by systemctl. The check after a copy runs
    the daemon itself, with no peers, while its unit is stopped."""

    def __init__(self, installed: dict, params):
        super().__init__(installed, params)
        self._alone = None

    def _ctl(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run(["systemctl", "--user", *args, self.installed["unit"]], capture_output=True, text=True,
                              timeout=STOP_SECONDS)

    def running(self) -> bool:
        if self._alone is not None and self._alone.poll() is None:
            return True
        try:
            return self._ctl("is-active").stdout.strip() in ("active", "activating", "deactivating")
        except Exception:                                      # noqa: BLE001
            return False

    def stop(self) -> None:
        if self._alone is not None and self._alone.poll() is None:
            with contextlib.suppress(Exception):
                self.call("stop", timeout=30)
            try:
                self._alone.wait(STOP_SECONDS)
            except subprocess.TimeoutExpired:
                raise CopyError("the node did not stop")
            self._alone = None
        self._ctl("stop")                                      # waits for it (TimeoutStopSec)
        if self.running():
            raise CopyError("the node did not stop")

    def start(self, extra: Iterable[str] = ()) -> None:
        extra = tuple(extra)
        if not extra:
            self._ctl("start")
            return
        self._alone = subprocess.Popen([self.installed["daemon"], f"-datadir={self.installed['datadir']}", *extra],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, start_new_session=True)


def core_for(installed: dict, params) -> Core:
    return ServiceCore(installed, params) if installed.get("unit") else Core(installed, params)


def _utxo_summary(got: dict) -> dict:
    """The parts of gettxoutsetinfo two copies of one coin set agree on (names differ by Core version)."""
    keep = {k: got[k] for k in ("height", "bestblock", "txouts", "total_amount") if k in got}
    for k, v in got.items():
        if k.startswith("hash_serialized") or k == "muhash":
            keep[k] = v
    return keep


# ------------------------------------------------------------------ making a copy

def _files(folder: Path) -> list[Path]:
    """Every file a copy takes, relative to the chain folder, in a fixed order."""
    out = []
    for top in FOLDERS:
        root = folder / top
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for name in sorted(filenames):
                p = Path(dirpath) / name
                rel = p.relative_to(folder)
                if name in SKIP or name.lower().startswith("wallet") or not p.is_file() or p.is_symlink():
                    continue
                if any(part.lower().startswith("wallet") for part in rel.parts):
                    continue
                out.append(rel)
    return out


def _linked(rel: Path, newest: dict[str, int]) -> bool:
    """Whether a file never changes again once written (a hard link is enough), else it is copied."""
    if rel.suffix in TABLES:
        return True
    m = _NUM.match(rel.name)
    if m and rel.parent == Path("blocks"):
        return int(m.group(2)) < newest.get(m.group(1), 0) - 1
    return False


def take(core: Core, folder: Path, into: Path, say: Callable[[str], None] = lambda s: None) -> dict:
    """Stop the Core, link or copy its chain data into `into`, start it again. Returns what the copy is (the
    manifest's head). `into` must be on the chain folder's disk."""
    info = core.call("getblockchaininfo")
    if info.get("pruned"):
        raise CopyError("a pruned node has no whole chain to copy")
    if int(info.get("blocks", 0)) < int(info.get("headers", 0)) - 2 or float(info.get("verificationprogress", 0)) < 0.999:
        raise CopyError("the node is not synced")
    net = core.call("getnetworkinfo")
    genesis = core.call("getblockhash", 0)
    say("pausing the node's network")
    core.call("setnetworkactive", False)
    try:
        say("reading the coin set's summary")
        utxo = _utxo_summary(core.call("gettxoutsetinfo", timeout=3600))
        if core.call("getbestblockhash") != utxo.get("bestblock"):
            raise CopyError("the tip moved while the coin set was read")
    except Exception:
        with contextlib.suppress(Exception):
            core.call("setnetworkactive", True)
        raise
    say("stopping the node")
    core.stop()
    try:
        shutil.rmtree(into, ignore_errors=True)
        into.mkdir(parents=True)
        files = _files(folder)
        newest: dict[str, int] = {}
        for rel in files:
            m = _NUM.match(rel.name)
            if m and rel.parent == Path("blocks"):
                newest[m.group(1)] = max(newest.get(m.group(1), 0), int(m.group(2)))
        for rel in files:
            src, dst = folder / rel, into / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if _linked(rel, newest):
                try:
                    os.link(src, dst)
                except OSError as exc:
                    raise CopyError(f"cannot hard-link {rel}: the copy must be on the node's own disk ({exc})")
            else:
                shutil.copy2(src, dst)
    finally:
        say("starting the node again")
        core.start()
    return {"genesis": genesis, "version": int(net.get("version", 0)), "subversion": str(net.get("subversion", "")),
            "height": int(utxo.get("height", 0)), "hash": utxo.get("bestblock"), "utxo": utxo}


def hash_files(into: Path) -> tuple[list[dict], dict]:
    """Each file of a copy cut into pieces and hashed: (the manifest's files, {path: [size, mtime_ns]})."""
    files, stat = [], {}
    for rel in _files(into):
        p = into / rel
        st = p.stat()
        shas = []
        with open(p, "rb") as f:
            while True:
                b = f.read(PIECE)
                if not b:
                    break
                shas.append(hashlib.sha256(b).hexdigest())
        files.append({"p": rel.as_posix(), "n": st.st_size, "s": shas})
        stat[rel.as_posix()] = [st.st_size, st.st_mtime_ns]
    return files, stat


class Snapshots:
    """The copy this arcade keeps of one chain's Core data for linked arcades (<chain folder>.arcade-snapshot/)."""

    def __init__(self, net: str, params, installed: dict, core: Core | None = None):
        self.net, self.params, self.installed = net, params, installed
        self.core = core or Core(installed, params)
        self.folder = chain_dir(installed, params)
        self.root = _beside(self.folder, ".arcade-snapshot")
        self._lock = threading.Lock()
        self.making = False
        self.said = ""
        self._cache: tuple[str, dict, dict] | None = None
        self.served_at = 0.0

    def current(self) -> tuple[str, dict, dict] | None:
        """(id, manifest, stat) of the finished copy, or None."""
        if self._cache is not None and (self.root / self._cache[0]).is_dir():
            return self._cache
        self._cache = None
        if not self.root.is_dir():
            return None
        for d in sorted(self.root.iterdir()):
            try:
                manifest = json.loads((d / "manifest.json").read_text())
                stat = json.loads((d / "stat.json").read_text())
            except (OSError, ValueError):
                continue
            if manifest_id(manifest) == d.name:
                self._cache = (d.name, manifest, stat)
                self.served_at = max(self.served_at, (d / "manifest.json").stat().st_mtime)
                return self._cache
        return None

    def offer(self) -> dict | None:
        got = self.current()
        if got is None:
            return None
        sid, m, _ = got
        return {"id": sid, "h": m["height"], "bytes": m["bytes"], "n": sum(len(f["s"]) for f in m["files"]),
                "maker": m.get("maker", "")}

    def make(self, maker: str = "") -> dict:
        """Make a copy (on the caller's thread: minutes of hashing; the Core is down for seconds). `maker`: this
        arcade's mesh id, said in the manifest: whoever serves a piece of it, the coin set is the maker's word."""
        with self._lock:
            if self.making:
                raise CopyError("a copy is being made")
            self.making = True
        try:
            work = self.root / "work"
            head = take(self.core, self.folder, work / "data", self._say)
            self._say("hashing the copy")
            files, stat = hash_files(work / "data")
            manifest = {"v": LAYOUT, "net": self.net, **head, "txindex": conf_txindex(self.installed), "maker": maker,
                        "piece": PIECE, "files": files, "bytes": sum(f["n"] for f in files)}
            sid = manifest_id(manifest)
            (work / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")))
            (work / "stat.json").write_text(json.dumps(stat))
            for d in list(self.root.iterdir()):
                if d.name != "work":
                    shutil.rmtree(d, ignore_errors=True)
            os.replace(work, self.root / sid)
            self._cache = None
            self.served_at = time.time()
            self._say("")
            log.info("core copy %s made at %s (%d files, %.1f GB)", self.net, head["height"], len(files),
                     manifest["bytes"] / 1e9)
            return manifest
        finally:
            self.making = False

    def _say(self, s: str) -> None:
        self.said = s

    def manifest(self, sid: str) -> dict | None:
        got = self.current()
        return got[1] if got and got[0] == sid else None

    def piece(self, sid: str, f: int, i: int) -> bytes | None:
        """Piece i of file f, unless the file changed since it was hashed (then the copy is no good: dropped)."""
        got = self.current()
        if not got or got[0] != sid or not (0 <= f < len(got[1]["files"])):
            return None
        entry = got[1]["files"][f]
        if not (0 <= i < len(entry["s"])):
            return None
        path = self.root / sid / "data" / entry["p"]
        try:
            st = path.stat()
            if [st.st_size, st.st_mtime_ns] != got[2].get(entry["p"]):
                log.warning("core copy %s: %s changed since it was hashed; the copy is dropped", self.net, entry["p"])
                self.drop()
                return None
            with open(path, "rb") as fh:
                fh.seek(i * PIECE)
                data = fh.read(PIECE)
        except OSError:
            return None
        self.served_at = time.time()
        return data

    def drop(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        self._cache = None

    def tidy(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if not self.making and self.current() is not None and now - self.served_at > SNAP_IDLE:
            log.info("core copy %s: nobody fetched it for %d h; deleted", self.net, SNAP_IDLE // 3600)
            self.drop()

    def fresh(self, now: float | None = None) -> bool:
        got = self.current()
        if got is None:
            return False
        made = (self.root / got[0] / "manifest.json").stat().st_mtime
        return (time.time() if now is None else now) - made < SNAP_EVERY


# ------------------------------------------------------------------ the receiver's checks

def header_check(core: Core, manifest: dict) -> str | None:
    """None when the copy's tip is on this node's own best header chain at its height, else why not."""
    if core.call("getblockhash", 0) != manifest.get("genesis"):
        return "another chain (its first block is not this node's)"
    if int(core.call("getnetworkinfo").get("version", -1)) != int(manifest.get("version", -2)):
        return "made by another Core version"
    h, want = int(manifest["height"]), manifest["hash"]
    tips = [t for t in core.call("getchaintips")
            if t.get("status") in ("active", "headers-only", "valid-headers", "valid-fork")]
    if not tips:
        return "this node has no header chain yet"
    best = max(tips, key=lambda t: int(t["height"]))
    if int(best["height"]) < h:
        return f"this node's headers are at {best['height']}, the copy at {h}: waiting for its headers"
    at, height = best["hash"], int(best["height"])
    if height - h > 50_000:
        return "the copy is far below this node's headers"
    while height > h:
        hdr = core.call("getblockheader", at)
        at, height = hdr["previousblockhash"], height - 1
    if at != want:
        return f"the copy's block {h} is not the one on this node's header chain"
    return None


class Fetch:
    """A copy being fetched into <chain folder>.arcade-copy/ (resumable), checked and installed."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.root = _beside(folder, ".arcade-copy")
        self.data = self.root / "data"
        self.state: dict = {}
        self._pieces: list[tuple[int, int, int, int, str]] = []
        self._done: set[int] = set()
        self._lock = threading.Lock()
        self._saved = 0.0
        self.load()

    # state.json: {id, maker, phase: fetching|swapping|checking, done: [n...], manifest}
    def load(self) -> None:
        try:
            self.state = json.loads((self.root / "state.json").read_text())
        except (OSError, ValueError):
            self.state = {}
        m = self.state.get("manifest")
        if m and manifest_id(m) == self.state.get("id"):
            self._index(m)
            self._done = set(int(n) for n in self.state.get("done", []))
        else:
            self.state = {}

    def _index(self, m: dict) -> None:
        self._pieces = []
        for fi, f in enumerate(m["files"]):
            for pi, sha in enumerate(f["s"]):
                off = pi * int(m["piece"])
                self._pieces.append((fi, pi, off, min(int(m["piece"]), int(f["n"]) - off), sha))

    @property
    def active(self) -> bool:
        return bool(self.state)

    def save(self, force: bool = False) -> None:
        if not self.state or (not force and time.time() - self._saved < 15):
            return
        self.state["done"] = sorted(self._done)
        tmp = self.root / "state.json.tmp"
        tmp.write_text(json.dumps(self.state))
        os.replace(tmp, self.root / "state.json")
        self._saved = time.time()

    def begin(self, sid: str, maker: str, manifest: dict) -> None:
        if manifest_id(manifest) != sid:
            raise CopyError("the manifest is not the copy offered")
        names = [f["p"] for f in manifest.get("files", [])]
        for p in names:
            rel = Path(p)
            if rel.is_absolute() or ".." in rel.parts or rel.parts[0] not in FOLDERS or \
                    any(x.lower().startswith("wallet") for x in rel.parts) or rel.name in SKIP:
                raise CopyError(f"the copy names a file it may not: {p}")
        shutil.rmtree(self.root, ignore_errors=True)
        self.data.mkdir(parents=True)
        for f in manifest["files"]:
            dst = self.data / f["p"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            with open(dst, "wb") as fh:
                fh.truncate(int(f["n"]))
        self.state = {"id": sid, "maker": maker, "phase": "fetching", "manifest": manifest, "done": [],
                      "at": int(time.time())}
        self._index(manifest)
        self._done = set()
        self.save(force=True)

    def missing(self) -> list[int]:
        return [n for n in range(len(self._pieces)) if n not in self._done]

    def where(self, n: int) -> tuple[int, int]:
        fi, pi, *_ = self._pieces[n]
        return fi, pi

    def number(self, fi: int, pi: int) -> int | None:
        for n, p in enumerate(self._pieces):
            if p[0] == fi and p[1] == pi:
                return n
        return None

    def got(self, n: int, data: bytes) -> bool:
        """A piece: written if it is the manifest's, else False."""
        fi, _, off, size, sha = self._pieces[n]
        if len(data) != size or hashlib.sha256(data).hexdigest() != sha:
            return False
        path = self.data / self.state["manifest"]["files"][fi]["p"]
        with self._lock:
            fd = os.open(path, os.O_WRONLY)
            try:
                os.pwrite(fd, data, off)
            finally:
                os.close(fd)
            self._done.add(n)
        self.save(force=len(self._done) == len(self._pieces))
        return True

    def progress(self) -> tuple[int, int]:
        m = self.state.get("manifest") or {}
        have = sum(self._pieces[n][3] for n in self._done if n < len(self._pieces))
        return have, int(m.get("bytes", 0))

    def drop(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        self.state, self._pieces, self._done = {}, [], set()

    # ------------------------------------------------------------ installing
    def _phase(self, phase: str) -> None:
        self.state["phase"] = phase
        self.save(force=True)

    def _swap_in(self) -> None:
        """The copy's folders in place of the node's, the node's moved aside (idempotent: a restart goes on)."""
        old = _beside(self.folder, ".arcade-old")
        old.mkdir(exist_ok=True)
        for top in FOLDERS:
            new, live, aside = self.data / top, self.folder / top, old / top
            if not new.exists():
                continue                                       # moved in already
            if live.exists():
                if aside.exists():
                    shutil.rmtree(live)                        # made by a Core started mid-swap: nothing of value
                else:
                    os.replace(live, aside)
            os.replace(new, live)

    def _swap_back(self) -> None:
        old = _beside(self.folder, ".arcade-old")
        for top in FOLDERS:
            live, aside = self.folder / top, old / top
            if aside.exists():
                shutil.rmtree(live, ignore_errors=True)
                os.replace(aside, live)
            elif live.exists() and top != "blocks":
                shutil.rmtree(live, ignore_errors=True)        # the copy's, where the node had none
        shutil.rmtree(old, ignore_errors=True)

    def install(self, core: Core, say: Callable[[str], None] = lambda s: None) -> dict:
        """Stop the node, swap the copy in, check it alone, start the node as usual. On any failure the node's own
        data goes back. Returns the manifest's tip."""
        m = self.state["manifest"]
        if self.missing():
            raise CopyError("the copy is not complete")
        say("stopping the node")
        core.stop()
        try:
            if self.state.get("phase") == "fetching":
                self._phase("swapping")
            say("moving the copy in")
            self._swap_in()
            # Kept to share on before the node runs on it: its files are exactly the copy's only now.
            try:
                seed(self.folder, m)
            except Exception:                                  # noqa: BLE001 -- sharing it on is a nicety
                log.warning("core copy: could not keep the copy to share", exc_info=True)
            self._phase("checking")
            say("checking the copy (the node runs with no peers)")
            core.start(ALONE)
            core.wait()
            why = self._check(core, m)
            if why:
                raise CopyError(why)
            core.stop()
        except Exception:
            log.warning("core copy: the copy failed its checks; the node's own data goes back", exc_info=True)
            with contextlib.suppress(Exception):
                core.stop()
            self._swap_back()
            shutil.rmtree(_beside(self.folder, ".arcade-snapshot"), ignore_errors=True)   # never share a refused copy
            self.drop()
            core.start()
            raise
        shutil.rmtree(_beside(self.folder, ".arcade-old"), ignore_errors=True)
        self.drop()
        say("starting the node")
        core.start()
        return {"height": m["height"], "hash": m["hash"]}

    @staticmethod
    def _check(core: Core, m: dict) -> str | None:
        if core.call("getbestblockhash") != m["hash"]:
            return "the copy's tip is not the manifest's"
        got = _utxo_summary(core.call("gettxoutsetinfo", timeout=3600))
        want = m.get("utxo") or {}
        for k in want:
            if got.get(k) != want[k]:
                return f"the copy's coin set is not the manifest's ({k})"
        if core.call("verifychain", 3, VERIFY_BLOCKS, timeout=3600) is not True:
            return f"verifychain refused the last {VERIFY_BLOCKS} blocks"
        return None


def busy(installed: dict, params) -> bool:
    """Whether a copy is being swapped in or checked: the node is then started by the copy, not by the arcade."""
    try:
        st = json.loads((_beside(chain_dir(installed, params), ".arcade-copy") / "state.json").read_text())
    except (OSError, ValueError):
        return False
    return st.get("phase") in ("swapping", "checking")


@contextlib.contextmanager
def held(path: Path):
    """One process at a time (two workers overlap for a moment in a zero-downtime reload)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a")
    if fcntl is None:                                   # Windows (arcade/winlock.py)
        from arcade import winlock
        if not winlock.lock(fh, wait=False):
            fh.close()
            yield False
            return
        try:
            yield True
        finally:
            winlock.unlock(fh)
            fh.close()
        return
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        yield False
        return
    try:
        yield True
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def seed(folder: Path, manifest: dict) -> str:
    """Keep the copy just installed, to share with linked arcades (torrent-style: every arcade that has it serves its
    pieces): the node's own files as they are now, before it runs again, linked where they never change and copied
    where they do, under <chain folder>.arcade-snapshot/<id>/ beside a manifest and the files' sizes. The same id as the
    maker's, so a fetcher takes pieces from both. Deleted like any copy nobody fetched (Snapshots.tidy)."""
    sid = manifest_id(manifest)
    root = _beside(folder, ".arcade-snapshot")
    work = root / "work-seed"
    shutil.rmtree(work, ignore_errors=True)
    newest: dict[str, int] = {}
    for f in manifest["files"]:
        rel = Path(f["p"])
        m = _NUM.match(rel.name)
        if m and rel.parent == Path("blocks"):
            newest[m.group(1)] = max(newest.get(m.group(1), 0), int(m.group(2)))
    stat = {}
    for f in manifest["files"]:
        rel = Path(f["p"])
        src, dst = folder / rel, work / "data" / rel
        if not src.is_file() or src.stat().st_size != int(f["n"]):
            raise CopyError(f"{rel} is not the copy's any more")
        dst.parent.mkdir(parents=True, exist_ok=True)
        if _linked(rel, newest):
            os.link(src, dst)
        else:
            shutil.copy2(src, dst)
        st = dst.stat()
        stat[rel.as_posix()] = [st.st_size, st.st_mtime_ns]
    (work / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")))
    (work / "stat.json").write_text(json.dumps(stat))
    for d in list(root.iterdir()):
        if d.name != work.name:
            shutil.rmtree(d, ignore_errors=True)
    os.replace(work, root / sid)
    return sid

"""Installing a chain's node from Admin -> Coins & chains (2026-10-27: "From the chains and coins tab, the operator
should be able to decide to install a block chain node on the computer. The storage cost should also be displayed for
chains"; asked how: the arcade installs it).

One click downloads the chain's official Core release for this machine, checks it against the SHA-256 pinned here
(each one compared with the release's own published sums when this file was written, 2026-10-27; Bellscoin publishes
none, and Luckycoin none for x86_64: those are pinned as downloaded that day; Flopcoin
publishes none either, pinned as downloaded 2026-11-03), unpacks it under
~/.dogecoinarcade/cores/<release>/, writes the chain's config where the arcade finds a Core by itself
(~/<node_dir>/<node_conf>: server, txindex, no pruning, RPC on this machine only with its own password), starts the
daemon, and reloads the arcade (the supervisor's zero-downtime HUP) so the chain is read from its new Core; then the
chain's data is copied from a linked arcade that has it, by itself, instead of synced (arcade/core_copy.py). No
sudo, no system service: an arcade that installed a Core starts it again whenever the arcade starts
(`start_installed`), and the setting `core_installed:<net>` says where it is.

A config that is already there is never overwritten (the operator's own node stays the operator's). Before it starts,
the page shows the download size, the storage the chain takes (arcade/disk_sizes.py: this arcade's figure or what a
linked arcade reports) and this machine's free disk; an install with less free disk than that is refused.
"""
from __future__ import annotations

import hashlib
import logging
import os
import platform
import secrets
import shutil
import signal
import subprocess
import tarfile
import threading
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("arcade.coreinstall")


@dataclass(frozen=True)
class Release:
    """One chain's official Core build, per machine."""
    family: str                          # the Core it is ("dogecoin", "litecoin", ...)
    version: str
    url: str                             # the release's download folder (ends with /)
    assets: dict                         # machine ("x86_64" | "aarch64") -> (file name, sha256, bytes)
    daemon: str                          # the daemon's path inside the archive, with {m} for the machine
    cli: str
    extra: tuple = field(default_factory=tuple)   # config lines besides the common ones (testnet=1, ...)
    #: The section a test chain's network-only RPC lines go under ("test"): DigiByte Core 9.26.7 will not start on its
    #: test chain with rpcbind outside it. Its user and password stay above it, where the arcade reads them.
    section: str = ""


_GH = "https://github.com/{}/releases/download/{}/"

RELEASES: dict[str, Release] = {
    "doge": Release("dogecoin", "1.14.9", _GH.format("dogecoin/dogecoin", "v1.14.9"), {
        "x86_64": ("dogecoin-1.14.9-x86_64-linux-gnu.tar.gz",
                   "4f227117b411a7c98622c970986e27bcfc3f547a72bef65e7d9e82989175d4f8", 35518706),
        "aarch64": ("dogecoin-1.14.9-aarch64-linux-gnu.tar.gz",
                    "6928c895a20d0bcb6d5c7dcec753d35c884a471aaf8ad4242a89a96acb4f2985", 16285495)},
        "dogecoin-1.14.9/bin/dogecoind", "dogecoin-1.14.9/bin/dogecoin-cli"),
    "ltc": Release("litecoin", "0.21.5.8", _GH.format("litecoin-project/litecoin", "v0.21.5.8"), {
        "x86_64": ("litecoin-0.21.5.8-x86_64-linux-gnu.tar.gz",
                   "43200c9f9d65ebc126ea5833ca9429e144c4b3273da6bb9f4e89fd7450ab1be9", 36951482),
        "aarch64": ("litecoin-0.21.5.8-aarch64-linux-gnu.tar.gz",
                    "129f55defb9045d5635566382c7b9f6fd227d4106a6fc07847a38b25ff9b29a1", 35649211)},
        "litecoin-0.21.5.8/bin/litecoind", "litecoin-0.21.5.8/bin/litecoin-cli"),
    "bel": Release("bells", "3.0.0-rc2", _GH.format("BellsChain/bellscoin", "v3.0.0-rc2"), {
        "x86_64": ("bells-3.0.0-rc2-x86_64-linux-gnu.tar.gz",
                   "9c1912d7aee1b28b8485dd30efde76aed15695b3d881c9bac1b0032465e147cc", 48855796),
        "aarch64": ("bells-3.0.0-rc2-aarch64-linux-gnu.tar.gz",
                    "3e0c78a8c4f33a49462978500950361190fb2f0fba564e3838ddebe19d1ccf7c", 46870021)},
        "bells-3.0.0-rc2/bin/bellsd", "bells-3.0.0-rc2/bin/bells-cli"),
    "lky": Release("luckycoin", "5.0.1", _GH.format("LuckycoinFoundation/Luckycoin", "v5.0.1"), {
        "x86_64": ("luckycoin-5.0.1-x86_64-linux-gnu.tar.gz",
                   "1e2a177f9633686e2d7bd2cae3e887e5f5c6c556062c9a743f551e15dbb84d3b", 36087041),
        "aarch64": ("luckycoin-5.0.1-aarch64-linux-gnu.tar.gz",
                    "64e39839309cc9468bf43690ffddb230ef7f193acf7212451bf7a9ec11a2a167", 16389261)},
        "luckycoin-5.0.1/bin/luckycoind", "luckycoin-5.0.1/bin/luckycoin-cli"),
    "wjk": Release("wojakcoin", "1.21.3.0", _GH.format("WojakCoinProj/wojakcore", "1.21.3.0"), {
        "x86_64": ("wojakcore-1.21.3.0-linux-x86_64.zip",
                   "b40a2abc90847676121799920c21a614ee44e42d555277cbc9fc087dd1fcd79b", 23771988),
        "aarch64": ("wojakcore-1.21.3.0-linux-aarch64.zip",
                    "e6c73fad53143a9fadfa98ad5a108cf25c8ac1b49976081a5c625897f17c3c44", 23040353)},
        "wojakcoind-linux-{m}", "wojakcoin-cli-linux-{m}"),
    "dgb": Release("digibyte", "9.26.7", _GH.format("DigiByte-Core/digibyte", "v9.26.7"), {
        "x86_64": ("digibyte-9.26.7-x86_64-linux-gnu.tar.gz",
                   "85f1a587e7d45fc63ef65cbbd0e5fa56f6d94f2f5cd7657a8d02ba66638cd8d6", 69766299),
        "aarch64": ("digibyte-9.26.7-aarch64-linux-gnu.tar.gz",
                    "e194e267306fbf12953812d402c1cd49ccc3ed32186a754e5b8683325dfd754e", 66876600)},
        "digibyte-9.26.7/bin/digibyted", "digibyte-9.26.7/bin/digibyte-cli"),
    # Flopcoin (2026-11-03): one Linux build, x86_64, the binaries at the archive's top level. The release publishes
    # no sums, so this one is pinned as downloaded that day.
    "flop": Release("flopcoin", "2.0.0.0", _GH.format("Flopcoin/Flopcoin", "v2.0.0.0"), {
        "x86_64": ("flopcoin-v2.0.0.0-linux.tar.gz",
                   "7e78da4a8e8c993a90f855c43cbce186bf84269d139c3695ad15606ae3d9905e", 29600108)},
        "flopcoind", "flopcoin-cli"),
}
# Litecoin testnet: the same Core, its own folder, testnet=1.
RELEASES["ltctest"] = Release("litecoin", "0.21.5.8", RELEASES["ltc"].url, RELEASES["ltc"].assets,
                              RELEASES["ltc"].daemon, RELEASES["ltc"].cli, extra=("testnet=1",))
# DigiByte testnet26: the same Core, its own folder, and its RPC address in a [test] section, without which DigiByte
# Core 9.26.7 will not start on its test chain ("-rpcbind only applied on test network when in [test] section").
# Sixteen RPC threads: `mine` holds one per algorithm while it hashes, and the arcade's own reads need the rest.
RELEASES["dgbtest"] = Release("digibyte", "9.26.7", RELEASES["dgb"].url, RELEASES["dgb"].assets,
                              RELEASES["dgb"].daemon, RELEASES["dgb"].cli, extra=("testnet=1", "rpcthreads=16"),
                              section="test")
#: Chains with no install here yet, and why (shown on their row).
NOT_OFFERED = {
    "kot": "Kotia publishes an x86_64 build only (v2.4), newer than the Kotia 1.0.0 this arcade was written against: "
           "not offered until it has been checked",
}


def machine() -> str | None:
    """"x86_64" | "aarch64" for a Linux machine a release has a build for, else None."""
    if platform.system() != "Linux":
        return None
    m = platform.machine().lower()
    return {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(m)


def offer(net: str, mach: str | None = None) -> dict:
    """What the page says of installing `net` here: {available, why, version, download}."""
    mach = mach if mach is not None else machine()
    if net in NOT_OFFERED:
        return {"available": False, "why": NOT_OFFERED[net]}
    rel = RELEASES.get(net)
    if rel is None:
        return {"available": False, "why": "no official build is listed for this chain"}
    if mach is None or mach not in rel.assets:
        return {"available": False, "why": f"no {rel.family} Core build for this machine "
                                           f"({platform.system()} {platform.machine()})"}
    return {"available": True, "version": rel.version, "download": rel.assets[mach][2]}


def _conf_lines(params, rel: Release) -> list[str]:
    return ["# Written by DogecoinArcade (Admin -> Coins & chains -> Install a node).",
            "server=1", "txindex=1", "prune=0", "listen=1", *rel.extra,
            f"rpcuser=arcade{secrets.token_hex(4)}", f"rpcpassword={secrets.token_urlsafe(24)}",
            *([f"[{rel.section}]"] if rel.section else []),
            "rpcbind=127.0.0.1", "rpcallowip=127.0.0.1",
            *([f"rpcport={params.rpcport}"] if getattr(params, "rpcport", None) else [])]


def node_paths(params) -> tuple[Path, Path]:
    """(the chain's data folder, its config) where the arcade finds a Core by itself."""
    folder = Path.home() / params.node_dir
    return folder, folder / (getattr(params, "node_conf", "") or "")


class Installer:
    """One install at a time, on a thread of its own; `status()` for the page."""

    def __init__(self, state):
        self.state = state
        self.jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def status(self) -> dict:
        return {net: dict(job) for net, job in self.jobs.items()}

    def start(self, net: str, params, free_bytes: int, need_bytes: int | None) -> dict:
        got = offer(net)
        if not got["available"]:
            raise ValueError(got["why"])
        folder, conf = node_paths(params)
        if conf.exists() and not (self.state.setting(f"core_installed:{net}") or {}) and not _ours(conf):
            raise ValueError(f"{conf} is already there: a node of yours is set up for this chain; turn it on "
                             f"with Runs here instead")
        if need_bytes and free_bytes < need_bytes:
            raise ValueError(f"this chain takes about {need_bytes / 1e9:.0f} GB and this disk has "
                             f"{free_bytes / 1e9:.0f} GB free")
        with self._lock:
            if any(j.get("state") == "working" for j in self.jobs.values()):
                raise ValueError("another node is being installed: one at a time")
            self.jobs[net] = {"state": "working", "step": "starting", "phase": "download", "done": 0,
                              "total": got["download"]}
        threading.Thread(target=self._run, args=(net, params), name=f"core-install-{net}", daemon=True).start()
        return self.jobs[net]

    def _say(self, net: str, **kw) -> None:
        self.jobs[net] = {**self.jobs.get(net, {}), **kw}

    def _run(self, net: str, params) -> None:
        try:
            rel, mach = RELEASES[net], machine()
            name, want, size = rel.assets[mach]
            home = Path(self.state.home)
            cores = home / "cores"
            cores.mkdir(parents=True, exist_ok=True)
            archive = cores / (name + ".part")
            self._say(net, step="downloading", phase="download", total=size)
            h = hashlib.sha256()
            with urllib.request.urlopen(rel.url + name, timeout=60) as r, open(archive, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    self._say(net, done=self.jobs[net].get("done", 0) + len(chunk))
            if h.hexdigest() != want:
                archive.unlink(missing_ok=True)
                raise ValueError(f"the download's SHA-256 is not the pinned one ({h.hexdigest()[:16]}...): "
                                 f"refused, nothing installed")
            note(self.state, net, f"{_label(params)} Core {rel.version}: downloaded ({size / 1e6:.1f} MB), its "
                                  f"SHA-256 checked")
            self._say(net, step="unpacking", phase="check")
            target = cores / f"{rel.family}-{rel.version}-{mach}"
            if not target.exists():
                tmp = cores / (target.name + ".tmp")
                shutil.rmtree(tmp, ignore_errors=True)
                tmp.mkdir()
                _unpack(archive, tmp)
                os.replace(tmp, target)
            archive.unlink(missing_ok=True)
            daemon = target / rel.daemon.format(m=mach)
            cli = target / rel.cli.format(m=mach)
            for b in (daemon, cli):
                if not b.is_file():
                    raise ValueError(f"{b.name} is not in the release archive")
                b.chmod(b.stat().st_mode | 0o755)
            folder, conf = node_paths(params)
            folder.mkdir(parents=True, exist_ok=True)
            if not conf.exists():
                conf.write_text("\n".join(_conf_lines(params, rel)) + "\n")
                conf.chmod(0o600)
            self.state.set_setting(f"core_installed:{net}", {"daemon": str(daemon), "cli": str(cli),
                                                             "datadir": str(folder), "conf": str(conf),
                                                             "version": rel.version, "at": int(time.time())})
            self._say(net, step="starting the node", phase="start")
            start_one(self.state.setting(f"core_installed:{net}"))
            self.state.set_setting(f"core:{net}", "here")
            self._say(net, state="done", phase="start", step="installed; the arcade reloads to read it, then copies "
                                                             "the chain's data from a linked arcade if one has it "
                                                             "(else the node syncs on its own)")
            note(self.state, net, f"{_label(params)} node installed and started. Next: its chain's data, copied from "
                                  f"a linked arcade if one has it", "ok")
            reload_arcade(home)
        except Exception as exc:                               # noqa: BLE001 -- said on the page
            log.warning("core install %s failed", net, exc_info=True)
            self._say(net, state="failed", step=str(exc)[:300])
            note(self.state, net, f"{_label(params)} install failed: {str(exc)[:200]}", "bad")


def _unpack(archive: Path, into: Path) -> None:
    """A .tar.gz or .zip, refusing any member that would land outside `into`."""
    root = into.resolve()
    if archive.name.endswith(".zip.part") or archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as z:
            for m in z.namelist():
                if not (root / m).resolve().is_relative_to(root):
                    raise ValueError(f"the archive holds a path outside its folder: {m}")
            z.extractall(into)
    else:
        with tarfile.open(archive, "r:gz") as t:
            for m in t.getmembers():
                if not (root / m.name).resolve().is_relative_to(root) or m.issym() or m.islnk():
                    if not (m.issym() or m.islnk()) or not (root / m.name).parent.resolve().is_relative_to(root):
                        raise ValueError(f"the archive holds a path outside its folder: {m.name}")
            try:
                t.extractall(into, filter="data")
            except TypeError:                                  # a Python without extraction filters
                t.extractall(into)


def _unit(installed: dict) -> str:
    return "arcade-core-" + Path(installed["datadir"]).name.lstrip(".")


def _user_systemd() -> bool:
    """A user service manager that outlives logins (systemd-run --user, linger on): the Core then has a unit of its
    own, so stopping or restarting the arcade -- a system service on .25 -- never stops the node with it."""
    if not shutil.which("systemd-run"):
        return False
    try:
        out = subprocess.run(["systemctl", "--user", "is-system-running"], capture_output=True, text=True, timeout=10)
        return out.stdout.strip() in ("running", "degraded")
    except Exception:                                          # noqa: BLE001
        return False


def _running(installed: dict) -> bool:
    try:
        if _user_systemd():
            out = subprocess.run(["systemctl", "--user", "is-active", _unit(installed)], capture_output=True,
                                 text=True, timeout=10)
            if out.stdout.strip() in ("active", "activating"):
                return True
        # pgrep's own command line holds no "-conf=": the pattern names the daemon's argument, so it never matches it.
        out = subprocess.run(["pgrep", "-f", "--", "^" + installed["daemon"] + " "], capture_output=True, text=True,
                             timeout=10)
        return bool(out.stdout.strip())
    except Exception:                                          # noqa: BLE001
        return False


def start_one(installed: dict, extra: tuple = ()) -> bool:
    """Start an installed Core unless it is running: as a user unit of its own where there is a user service manager
    (restarted if it fails), else as a daemon of its own. True when started now. `extra`: more arguments (a copied
    chain's data is checked with no peers, arcade/core_copy.py)."""
    if not installed or not Path(installed.get("daemon", "")).is_file():
        return False
    if _running(installed):
        return False
    args = [installed["daemon"], f"-datadir={installed['datadir']}", f"-conf={installed['conf']}", *extra]
    if _user_systemd():
        subprocess.run(["systemctl", "--user", "reset-failed", _unit(installed)], capture_output=True, timeout=10)
        subprocess.run(["systemd-run", "--user", f"--unit={_unit(installed)}", "--collect",
                        "-p", "Restart=on-failure", "-p", "TimeoutStopSec=600", *args],
                       check=True, capture_output=True, timeout=30)
    else:
        subprocess.Popen([*args, "-daemon"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    return True


def start_installed(state) -> list[str]:
    """At the arcade's start: every Core this arcade installed, started if it is not running -- unless its chain is set
    to borrow or off in Admin -> Chains (2026-11-03, the ThinkPad: DigiByte set to borrow after it ran the machine out
    of memory was started again at the next restart, because this looked only at what was installed)."""
    from .cores import HERE, mode
    settings = state.settings() or {}
    started = []
    for k, v in list(settings.items()):
        if k.startswith("core_installed:") and isinstance(v, dict):
            if mode(settings, k.split(":", 1)[1]) != HERE:
                continue                                       # installed, but borrowed or off: not run
            try:
                from .config import NETWORKS
                from .core_copy import busy
                if busy(v, NETWORKS[k.split(":", 1)[1]]):
                    continue                                   # a copy of its chain data is being swapped in
            except Exception:                                  # noqa: BLE001
                pass
            try:
                if start_one(v):
                    started.append(k.split(":", 1)[1])
            except Exception:                                  # noqa: BLE001 -- tried again next start
                log.warning("could not start the installed %s node", k, exc_info=True)
    return started


def reload_arcade(home: Path) -> None:
    """The zero-downtime reload (web/supervisor.py): the new worker finds the chain's Core by its config."""
    try:
        pid = int((Path(home) / "web-supervisor.pid").read_text().strip())
        os.kill(pid, signal.SIGHUP)
    except (OSError, ValueError):
        log.info("core install: no supervisor to reload; the chain is read at the next restart")


def free_bytes(home: Path) -> int:
    try:
        return shutil.disk_usage(Path.home()).free
    except OSError:
        return 0


def summary(state, nets: list, params_of) -> dict:
    """For the Coins & chains tab: per chain {offer, storage estimate, installed, job}, and this disk's free space."""
    jobs = installer(state).status()
    boot = getattr(getattr(state, "mesh", None), "coreboot", None)
    try:
        copies = boot.status() if boot is not None else {}
    except Exception:                                          # noqa: BLE001
        copies = {}
    rows = {}
    # The installer's own nodes (Pepecoin testnet and mainnet, run as user units) take copies too: their rows say so.
    try:
        served = set(boot.nets()) if boot is not None else set()
    except Exception:                                          # noqa: BLE001
        served = set()
    nets = list(nets) + [n for n in served if n not in nets]
    for net in nets:
        est = state.disk_estimate(net) if hasattr(state, "disk_estimate") else {}
        rows[net] = {**offer(net), "storage": est.get("core"), "ord": est.get("ord"),
                     "installed": bool(state.setting(f"core_installed:{net}")) or net in served, "job": jobs.get(net),
                     "copy": copies.get(net)}
        rows[net].update(steps(jobs.get(net), copies.get(net), rows[net]["installed"]))
    return {"free": free_bytes(Path(state.home)), "rows": rows}


def installer(state) -> Installer:
    inst = state.__dict__.get("_core_installer")
    if inst is None:
        inst = state._core_installer = Installer(state)
    return inst


# ---------------------------------------------------------------- what the operator is told (2026-11-04: "there
# is no indication of what is happening or what to do next"; chose the mockup with the steps in the chain's row and
# notifications that follow him around Admin)

CONF_MARK = "# Written by DogecoinArcade"
EVENTS_KEPT = 60


def _label(params) -> str:
    return getattr(params, "label", "") or getattr(params, "name", "")


def _ours(conf: Path) -> bool:
    """A config this arcade wrote (Install a node), so a node of its own installed again after a cancel."""
    try:
        return conf.read_text().startswith(CONF_MARK)
    except OSError:
        return False


def note(state, net: str, text: str, level: str = "info") -> None:
    """A notification for Admin: kept (the last EVENTS_KEPT) so a page opened later still shows what happened."""
    try:
        events = list(state.setting("core_events", []) or [])[-(EVENTS_KEPT - 1):]
        events.append({"at": round(time.time(), 3), "net": net, "text": str(text)[:300], "level": level})
        state.set_setting("core_events", events)
    except Exception:                                          # noqa: BLE001 -- a note never stops the work
        log.debug("core note", exc_info=True)


def events(state, since: float = 0.0) -> list[dict]:
    return [e for e in (state.setting("core_events", []) or []) if isinstance(e, dict) and e.get("at", 0) > since]


STEP_KEYS = ("download", "check", "start", "find", "headers", "data", "ready")
#: How much of the whole install each step is, for the row's one percentage.
STEP_WEIGHT = {"download": 5, "check": 2, "start": 3, "find": 5, "headers": 15, "data": 60, "ready": 10}


def _frac(have, total) -> float | None:
    try:
        return max(0.0, min(1.0, float(have) / float(total))) if total else None
    except (TypeError, ValueError):
        return None


def steps(job: dict | None, copy: dict | None, installed: bool) -> dict:
    """An install's seven steps as the chain's row draws them (2026-11-04 chose the mockup where the line
    through the steps is the progress bar): each done / now / todo / skip / failed, how far it is (`p`, 0..1, or
    None), the numbers under it (`num`) and its time left (`eta`, seconds); the whole install's percentage; and
    whether anything is still going on (the page then keeps asking)."""
    job, copy = job or {}, copy or {}
    label = {"download": "Download", "check": "Check", "start": "Start node", "find": "Find a copy",
             "headers": "Learn headers", "data": "Copy data", "ready": "Check & ready"}
    st = {k: "todo" for k in STEP_KEYS}
    num = {k: "" for k in STEP_KEYS}
    p: dict = {k: None for k in STEP_KEYS}
    eta: dict = {k: None for k in STEP_KEYS}

    def gb(b) -> str:
        b = float(b or 0)
        return f"{b / 1e9:.1f} GB" if b >= 1e9 else f"{max(1, round(b / 1e6))} MB"

    def upto(key, state="now"):
        for k in STEP_KEYS[:STEP_KEYS.index(key)]:
            if st[k] != "skip":
                st[k] = "done"
        st[key] = state

    node = copy.get("node") or {}
    blocks, headers = int(node.get("blocks", 0) or 0), int(node.get("headers", 0) or 0)
    synced = headers > 0 and headers - blocks <= 2
    target = int(copy.get("target") or 0)
    phase = copy.get("phase") or ""
    who = copy.get("from") or ""
    if not installed:
        if job.get("state") not in ("working", "failed"):
            return {"steps": [], "active": False, "pct": 0}
        upto(job.get("phase") or "download", "failed" if job.get("state") == "failed" else "now")
        if job.get("total"):
            p["download"] = _frac(job.get("done", 0), job["total"])
            num["download"] = f"{gb(job.get('done', 0))} / {gb(job['total'])}"
    else:
        num["download"], num["check"], p["download"], p["check"], p["start"] = "done", "SHA-256 ok", 1, 1, 1
        copying = phase in ("looking", "asked", "headers", "copying", "linking", "checking", "done")
        if copying:
            num["find"] = who or "looking"
            if copy.get("copy_bytes") or copy.get("total"):
                num["find"] += " · " + gb(copy.get("copy_bytes") or copy.get("total"))
            num["headers"] = f"{headers:,} / {target:,}" if target else (f"{headers:,}" if headers else "")
            p["headers"] = _frac(headers, target)
            eta["headers"] = copy.get("eta_headers")
            if copy.get("total"):
                num["data"] = f"{gb(copy.get('done', 0))} / {gb(copy['total'])}"
                if int(copy.get("sources") or 0) > 1:
                    num["data"] += f" · {int(copy['sources'])} arcades"
                p["data"] = _frac(copy.get("done", 0), copy["total"])
                eta["data"] = copy.get("eta")
            elif copy.get("copy_bytes"):
                num["data"] = f"0 / {gb(copy['copy_bytes'])}"
            if phase in ("looking", "asked"):
                upto("find")
            elif phase == "headers":
                upto("headers")
            elif phase in ("copying", "linking"):
                upto("data")
                p["headers"] = 1
            elif phase == "checking":
                upto("ready")
                p["headers"], p["data"], num["ready"] = 1, 1, "checking with no peers"
            else:                                              # done
                p["headers"], p["data"] = 1, 1
                upto("ready", "done" if synced else "now")
                num["ready"] = "checked here" if synced else f"last {max(0, headers - blocks):,} blocks"
                p["ready"] = 1 if synced else _frac(blocks, headers)
                eta["ready"] = None if synced else copy.get("eta_blocks")
            num["start"] = "running"
        else:
            # The node syncs the chain itself: no copy offered, the operator chose it, or a copy was refused.
            label["data"], label["ready"] = "Sync chain", "Ready"
            st["find"] = "skip"
            num["find"] = {"own": "you chose: none", "failed": "copy refused"}.get(phase, "none offered")
            num["start"] = "running" if headers else "starting"
            if not headers:
                upto("start")
                st["find"] = "skip"
            elif target and headers < target - 2:
                upto("headers")
                st["find"] = "skip"
                num["headers"], p["headers"], eta["headers"] = f"{headers:,} / {target:,}", _frac(headers, target), \
                    copy.get("eta_headers")
            else:
                num["headers"], p["headers"] = f"{headers:,}", 1
                num["data"] = f"{blocks:,} / {headers:,}"
                p["data"], eta["data"] = _frac(blocks, headers), copy.get("eta_blocks")
                if synced:
                    upto("ready", "done")
                    p["data"], num["ready"] = 1, "synced"
                else:
                    upto("data")
                st["find"] = "skip"
    for k in STEP_KEYS:
        if st[k] == "done":
            p[k] = 1
        elif st[k] in ("todo", "skip") and p[k] is None:
            p[k] = 0
    total = sum(STEP_WEIGHT.values())
    done = sum(STEP_WEIGHT[k] * (1 if st[k] in ("done", "skip") else (p[k] or 0) if st[k] == "now" else 0)
               for k in STEP_KEYS)
    left = [e for k, e in eta.items() if e and st[k] in ("now", "todo")]
    return {"steps": [{"key": k, "label": label[k], "state": st[k], "num": num[k], "p": p[k], "eta": eta[k]}
                      for k in STEP_KEYS],
            "active": any(v == "now" for v in st.values()), "pct": int(100 * done / total),
            "eta": sum(left) if left else None}


def cancel(state, net: str, params) -> None:
    """Cancel an install (or remove a node this arcade installed): its node stopped, its chain data and any copy
    deleted, and the chain set back to borrow. Never a wallet: the folder keeps the wallet and the config."""
    from .core_copy import FOLDERS, Core, chain_dir
    installed = state.setting(f"core_installed:{net}") or {}
    if not installed:
        raise ValueError("this arcade did not install a node for that chain")
    Core(installed, params).stop()
    folder = chain_dir(installed, params)
    for top in FOLDERS:
        shutil.rmtree(folder / top, ignore_errors=True)
    for what in (".arcade-copy", ".arcade-old", ".arcade-snapshot", ".arcade-copy.lock", ".arcade-snapshot.lock"):
        p = folder.with_name(folder.name + what)
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)
    for key in (f"core_installed:{net}", f"core_copied:{net}", f"core_copy_refused:{net}", f"core_copy_own:{net}"):
        state.set_setting(key, None)
    state.set_setting(f"core:{net}", "borrow")
    jobs = installer(state).jobs
    jobs.pop(net, None)
    note(state, net, f"{_label(params)} node removed: its chain data deleted, the chain borrowed again (its wallet "
                     f"file is kept)", "info")
    reload_arcade(Path(state.home))

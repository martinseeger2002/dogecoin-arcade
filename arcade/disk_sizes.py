"""What each chain costs on disk, here and on the arcades linked to this one (2026-10-27: "The size of all indexes
should be broadcast to the network so that users can decide based on what the storage cost will be"; "The storage cost
should also be displayed for chains").

This arcade measures, for every chain it runs, its Core's data folder (blocks, chain state, indexes: ~/<node_dir>) and
its ord index (ord-<net>.sqlite and the files beside it), on a thread of its own every MEASURE_EVERY -- walking a data
folder of hundreds of thousands of files is never done while a page waits. What it measured goes out in its operator
hello (mesh/operators.py "me" `disk`: {net: {"core": bytes, "ord": bytes}}), and what linked arcades said comes back
the same way. `estimate(net)` is what the Admin tabs show before an operator installs a node or keeps an index: the
largest a linked arcade reports (the one most nearly synced), with its tag.

A peer's word is only its word: it is shown as "about", never used for anything but this estimate.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("arcade.disk")

#: How often this arcade measures its own folders (seconds).
MEASURE_EVERY = 6 * 3600
#: Where the running processes are read from (Linux); a test points it elsewhere.
PROC = Path("/proc")
#: The largest a peer's figure may be and still be taken (a typo or a lie bigger than any chain is dropped).
MOST = 50 * 10 ** 12


def folder_bytes(path: Path) -> int:
    """Every file's size under `path` (0 when it is not there); symbolic links are not followed."""
    total = 0
    try:
        for root, dirs, files in os.walk(path, followlinks=False):
            for name in files:
                try:
                    total += os.lstat(os.path.join(root, name)).st_size
                except OSError:
                    pass
    except OSError:
        return 0
    return total


def index_bytes(home: Path, net: str) -> int:
    """An ord index with what lives beside it (its write log, coin values, coin filter)."""
    total = 0
    for p in Path(home).glob(f"ord-{net}.sqlite*"):
        if p.name.endswith(".backfill") or ".backfill" in p.name:
            continue                                    # a token backfill is temporary, not what keeping costs
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total


def clean(said: Any) -> dict:
    """A peer's `disk` as kept: {net: {"core": int, "ord": int}}, every number whole, non-negative and bounded;
    anything else dropped."""
    out: dict[str, dict[str, int]] = {}
    if not isinstance(said, dict):
        return out
    for net, sizes in list(said.items())[:40]:
        if not isinstance(net, str) or not net.isalnum() or len(net) > 12 or not isinstance(sizes, dict):
            continue
        row = {}
        for k in ("core", "ord"):
            v = sizes.get(k)
            if isinstance(v, int) and not isinstance(v, bool) and 0 < v <= MOST:
                row[k] = v
        if row:
            out[net] = row
    return out


class Sizes:
    """This arcade's own measurements, refreshed behind the pages."""

    def __init__(self, state):
        self.state = state
        self.mine: dict[str, dict[str, int]] = {}
        self.at = 0.0
        self._lock = threading.Lock()

    def own(self) -> dict:
        """What this arcade says (and shows) of itself; starts a measurement when the last one is old."""
        if time.time() - self.at > MEASURE_EVERY and self._lock.acquire(blocking=False):
            self.at = time.time()
            threading.Thread(target=self._measure, name="disk-sizes", daemon=True).start()
        return dict(self.mine)

    def _measure(self) -> None:
        try:
            out: dict[str, dict[str, int]] = {}
            home = Path(getattr(self.state, "home", "") or ".")
            for chain in getattr(self.state, "all_chains", ()):
                if getattr(chain, "borrowed", False):
                    continue
                row = {}
                folder = node_folder(chain)
                if folder is not None:
                    core = folder_bytes(folder)
                    if core:
                        row["core"] = core
                ords = index_bytes(home, chain.network)
                if ords:
                    row["ord"] = ords
                if row:
                    out[chain.network] = row
            self.mine = out
        except Exception:                                  # noqa: BLE001 -- measured again next time
            log.warning("disk sizes: could not measure", exc_info=True)
        finally:
            self._lock.release()


def _holds_chain(folder: Path) -> bool:
    """A Core's data folder holds its blocks (a folder with only a config in it is not one: 2026-10-27, .25's
    ~/.dogecoin held only dogecoin.conf, its node's data in /var/lib/dogecoind, and Dogecoin was said to take 278 B)."""
    try:
        return any((folder / sub / "blocks").is_dir() for sub in ("", "testnet4", "testnet3", "testnet26"))
    except OSError:
        return False


def _conf_datadir(conf: Path) -> Path | None:
    try:
        for line in Path(conf).read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() == "datadir" and v.strip():
                return Path(v.strip()).expanduser()
    except OSError:
        pass
    return None


def _process_datadir(daemon: str, conf: Path | None = None) -> Path | None:
    """The -datadir= of a running `daemon` (Linux /proc): a system install keeps its data where its service says. With
    `conf`, only a daemon started with that -conf= (2026-11-05: DigiByte's testnet node runs beside its mainnet one,
    and the testnet was said to take the mainnet's 51.6 GB)."""
    try:
        for proc in PROC.iterdir():
            if not proc.name.isdigit():
                continue
            try:
                argv = (proc / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if not argv or Path(argv[0].decode("utf-8", "replace")).name != daemon:
                continue
            args = [a.decode("utf-8", "replace") for a in argv[1:]]
            confs = [Path(a.split("=", 1)[1]).expanduser() for a in args if a.startswith("-conf=")]
            if conf is not None and confs and confs[0] != Path(conf).expanduser():
                continue
            for a in args:
                if a.startswith("-datadir="):
                    return Path(a.split("=", 1)[1]).expanduser()
    except OSError:
        pass
    return None


def node_folder(chain) -> Path | None:
    """The Core's data folder of `chain` on this machine, and only one that holds its blocks: the chain's datadir, the
    config's datadir=, the running daemon's -datadir=, the config's folder, or ~/<node_dir>."""
    params = getattr(chain, "params", None)
    conf = getattr(chain, "conf", None)
    stem = Path(getattr(params, "node_conf", "") or "").stem
    candidates = [getattr(chain, "datadir", None), _conf_datadir(conf) if conf else None,
                  _process_datadir(stem + "d", conf) if stem else None,
                  Path(conf).parent if conf else None,
                  (Path.home() / params.node_dir) if getattr(params, "node_dir", "") else None]
    for c in candidates:
        if c and Path(c).is_dir() and _holds_chain(Path(c)):
            return Path(c)
    return None


def estimate(net: str, mine: dict, peers: dict) -> dict:
    """{core, ord: (bytes, who)} for `net`: this arcade's own figure when it has one, else the largest a linked arcade
    said (most nearly synced), with that arcade's tag or id; absent when nobody said."""
    out: dict[str, Any] = {}
    for k in ("core", "ord"):
        if (mine.get(net) or {}).get(k):
            out[k] = {"bytes": mine[net][k], "who": "this arcade"}
            continue
        best = None
        for peer, said in (peers or {}).items():
            v = ((said.get("disk") or {}).get(net) or {}).get(k)
            if v and (best is None or v > best[0]):
                best = (v, said.get("tag") or peer[:12])
        if best:
            out[k] = {"bytes": best[0], "who": "@" + best[1] if not best[1].isalnum() or len(best[1]) != 12 else best[1]}
    return out

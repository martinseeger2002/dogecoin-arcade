"""The Core's own word on a rescan it is running (2026-10-22: "Can we include rescan % to Arcade from RPC?").

Dogecoin Core 1.14 -- and the Cores built like it -- says nothing of a rescan over RPC: the rescan holds the locks the
calls need, and no call reports how far it is. What it does say, once a minute, is a line in its debug.log:

    2026-10-22 14:15:24 Still rescanning. At block 5471263. Progress=0.597658

That line is read here, from the tail of the log, wherever the arcade's user may read it (a Core run as its own system
user keeps its log to itself until the operator lets the arcade's user read it: README, "Rescan progress"). Unreadable,
or no fresh line: None, and the arcade says its own estimate as before.
"""
from __future__ import annotations

import calendar
import re
import threading
import time
from pathlib import Path

from .config import Params, read_node_conf

LINE = re.compile(rb"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) Still rescanning\. At block (\d+)\. Progress=([0-9.]+)",
                  re.M)
#: Bytes read from the end of the log: a busy Core writes a few hundred lines a minute.
TAIL = 512 * 1024
#: A line older than this is a rescan that ended (the Core says one each minute while it runs).
FRESH = 180
#: An answer kept this long: the page asks every few seconds, the log changes once a minute.
KEEP = 20.0

_kept: dict[str, tuple[float, dict | None]] = {}
_lock = threading.Lock()


def log_paths(params: Params) -> list[Path]:
    """Where this chain's Core may keep its debug.log on this machine, likeliest first: the datadir its config names,
    its folder in the home directory, and /var/lib/<coin>d (a Core installed as a system service)."""
    folder = params.node_dir or ".pepecoin"
    conf = Path.home() / folder / (params.node_conf or "pepecoin.conf")
    dirs: list[Path] = []
    try:
        named = read_node_conf(conf).get("datadir") if conf.exists() else None
    except OSError:
        named = None
    if named:
        dirs.append(Path(named).expanduser())
    dirs += [Path.home() / folder, Path("/var/lib") / (folder.lstrip(".") + "d")]
    sub = params.datadir_subdir
    out: list[Path] = []
    for d in dirs:
        p = (d / sub / "debug.log") if sub else (d / "debug.log")
        if p not in out:
            out.append(p)
    return out


def read_progress(path: Path, now: float | None = None) -> dict | None:
    """{"block", "progress" (0-1), "eta" (seconds, or None)} from a log's last rescan line, when it is fresh."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - TAIL))
            tail = f.read()
    except OSError:
        return None
    rows = []
    for m in LINE.finditer(tail):
        try:
            at = calendar.timegm(time.strptime(m.group(1).decode(), "%Y-%m-%d %H:%M:%S"))   # the Core logs UTC
            rows.append((at, int(m.group(2)), float(m.group(3))))
        except ValueError:
            continue
    if not rows:
        return None
    now = time.time() if now is None else now
    at, block, progress = rows[-1]
    if now - at > FRESH:
        return None
    # The pace from the lines of the last half hour (one a minute), so the countdown follows the disk as it is now.
    recent = [r for r in rows if at - r[0] <= 1800 and r[2] <= progress]
    eta = None
    if len(recent) >= 2 and recent[-1][0] > recent[0][0] and progress > recent[0][2]:
        rate = (progress - recent[0][2]) / (recent[-1][0] - recent[0][0])
        eta = max(0, int((1.0 - progress) / rate - (now - at)))
    return {"block": block, "progress": min(max(progress, 0.0), 1.0), "eta": eta}


def rescan_progress(params: Params, now: float | None = None) -> dict | None:
    """This chain's Core's rescan as it says it in its log, or None (no rescan running, or no log to read)."""
    key = params.name
    t = time.monotonic()
    with _lock:
        kept = _kept.get(key)
        if kept is not None and t - kept[0] < KEEP:
            return kept[1]
    said = None
    for path in log_paths(params):
        said = read_progress(path, now)
        if said is not None:
            break
    with _lock:
        _kept[key] = (t, said)
    return said

"""Which chains this node hosts: the operator's choice, one switch per chain.

SadFrog's side, 2026-10-17: "everything should work as a module but additive system --
allowing operators to choose only what nodes they host". The two Pepecoin chains are the
arcade itself (the ledger and the Messenger) and are always hosted; every other chain is an
add-on the operator turns on or off in Admin -> "Face and chains":

* Dogecoin -- read at start-up (``web/__main__._doge_context``), so a change applies when
  the arcade restarts; the panel offers the restart.
* Litecoin -- its node's user service (arcade/litecoin.py), enabled and started, or stopped
  and disabled, at once. Not an arcade network yet: hosting it runs the node.

Kept in settings.json (``chains:<key>``) beside every other switch, and read from the file
directly at start-up, before the application state exists.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

#: Always hosted: they are what the arcade is.
REQUIRED = (("main", "Pepecoin mainnet"), ("test", "Pepecoin testnet"))

#: Add-ons: key -> (label, hosted when never chosen, when a change applies)
OPTIONAL = {
    "doge": ("Dogecoin", True, "restart"),
    "ltc": ("Litecoin", False, "now"),
}


def setting_key(key: str) -> str:
    return f"chains:{key}"


def hosted(settings: dict, key: str) -> bool:
    """Whether the operator hosts `key` (its default until they choose)."""
    if key not in OPTIONAL:
        raise KeyError(key)
    value = settings.get(setting_key(key))
    return OPTIONAL[key][1] if value is None else bool(value)


def read_settings(home: Path | str) -> dict:
    """settings.json as it is on disk, for start-up; {} when it cannot be read."""
    try:
        data = json.loads((Path(home) / "settings.json").read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def doge_node_present() -> bool:
    """Whether the installer's Dogecoin node is on this machine (what start-up looks for)."""
    home = Path.home() / ".dogecoin"
    return (home / "dogecoin.conf").exists() or (home / ".cookie").exists()


def restart_self(unit: str = "arcade-web") -> tuple[bool, str]:
    """Ask systemd to restart this arcade's own user service, without waiting for it
    (the request that asked would otherwise be cut off mid-answer). Only when this
    process IS that service, so a hand-started arcade is never restarted underneath."""
    try:
        main = subprocess.run(["systemctl", "--user", "show", unit, "-p", "MainPID", "--value"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if main != str(os.getpid()):
        return False, f"this arcade is not running as the {unit} user service; restart it by hand"
    said = subprocess.run(["systemctl", "--user", "--no-block", "restart", unit],
                          capture_output=True, text=True, timeout=10)
    return said.returncode == 0, (said.stderr or said.stdout).strip()[:200]


def overview(settings: dict, doge_running: bool, ltc: dict | None,
             doge_available: bool = True) -> list[dict[str, Any]]:
    """Every chain for the admin panel: required or optional, hosted or not, and whether
    what runs now matches the choice (a Dogecoin change waits for a restart)."""
    out: list[dict[str, Any]] = [{"key": k, "label": label, "required": True, "hosted": True,
                                  "running": True, "applies": "always"}
                                 for k, label in REQUIRED]
    for key, (label, _default, applies) in OPTIONAL.items():
        want = hosted(settings, key)
        if key == "doge":
            running = doge_running
        else:
            running = bool(ltc and ltc.get("service") in ("active", "activating"))
        row = {"key": key, "label": label, "required": False, "hosted": want,
               "running": running, "applies": applies}
        if key == "ltc" and ltc is not None:
            row["installed"] = bool(ltc.get("installed"))
        if key == "doge":
            row["installed"] = doge_available
        # A restart helps only when it would change something: a chain switched off that
        # still runs, or one switched on whose node is there to be found.
        row["restart_needed"] = applies == "restart" and (
            (running and not want) or (want and not running and row.get("installed", True)))
        out.append(row)
    return out

"""Litecoin on this node: is its Core running, how far has it synced, start it or stop it.

SadFrog's side, 2026-10-17 (LITECOIN-PLAN, phase 1): "get the node running ... and add toggle
buttons needed for starting". Litecoin is not one of the arcade's networks yet -- nothing here
reads, sends or indexes anything on it. This is the operator's switch and gauge for the node
that will carry it: the admin panel shows the sync and turns the node's user service on and off.

An optional, additive module (SadFrog's decision, 2026-10-17): each operator hosts only the
chains they choose, and a node without Litecoin simply shows it as not hosted. The
Taproot-witness inscription adapter of the later phases is Litecoin-only too, never in
doginals.py.

The node is a user service (`litecoind-mainnet`) running as the same user as the arcade, so its
0600 `.cookie` is readable here and `systemctl --user` reaches it -- the same way
arcade/update.py restarts arcade-web. A node that is not installed is reported, never assumed.
"""

from __future__ import annotations

import base64
import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

#: Said wherever Litecoin trades or inscriptions are offered (docs/litecoin.md says it in full).
CROSS_CHAIN_NOTE = (
    "A swap across chains is two lock-script trades tied by one secret: you pay a network fee on "
    "each chain you lock on, a lock refunds only after its time has passed (Litecoin's 2.5-minute "
    "blocks can add about 15 minutes), and a trade is done when both claims are in blocks on both "
    "chains. Never send to an ltcmweb1... address.")

UNIT = "litecoind-mainnet"
RPC_URL = "http://127.0.0.1:9332/"
DATADIR = Path.home() / ".litecoin"


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True,
                          timeout=30)


def installed() -> bool:
    """Whether this machine has the node's user service at all."""
    try:
        said = _systemctl("show", UNIT, "-p", "LoadState", "--value")
    except (OSError, subprocess.TimeoutExpired):
        return False
    return said.returncode == 0 and said.stdout.strip() == "loaded"


def service_state() -> str:
    """systemd's word for the service: active, inactive, activating, failed..."""
    try:
        return _systemctl("is-active", UNIT).stdout.strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def _rpc(method: str, *params: Any) -> Any:
    cookie = (DATADIR / ".cookie").read_text().strip()
    request = urllib.request.Request(
        RPC_URL, data=json.dumps({"jsonrpc": "1.0", "id": "arcade", "method": method,
                                  "params": list(params)}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Basic " + base64.b64encode(cookie.encode()).decode()})
    with urllib.request.urlopen(request, timeout=5) as answer:
        reply = json.load(answer)
    if reply.get("error"):
        raise RuntimeError(reply["error"].get("message", "error"))
    return reply["result"]


def status() -> dict:
    """What the admin panel shows. Never raises: a node that cannot be asked says why."""
    out: dict[str, Any] = {"installed": installed(), "unit": UNIT}
    out["service"] = service_state() if out["installed"] else "not installed"
    try:
        chain = _rpc("getblockchaininfo")
        out.update(answering=True, chain=chain.get("chain"), blocks=chain.get("blocks"),
                   headers=chain.get("headers"),
                   progress=round(float(chain.get("verificationprogress") or 0) * 100, 2),
                   synced=not chain.get("initialblockdownload", True),
                   peers=_rpc("getconnectioncount"),
                   version=_rpc("getnetworkinfo").get("subversion", ""))
    except FileNotFoundError:
        out.update(answering=False, note="no .cookie in ~/.litecoin: the node is not running")
    except (OSError, urllib.error.URLError, RuntimeError, ValueError) as exc:
        # Includes "Loading block index..." while it starts (RPC answers -28).
        out.update(answering=False, note=str(exc)[:200])
    return out


def set_running(on: bool) -> dict:
    """Start or stop the node's service, then say how it is. Stopping is a clean
    shutdown that can take minutes (the unit's TimeoutStopSec), so the request is
    queued (--no-block) and the panel watches the service say "deactivating"."""
    if not installed():
        raise RuntimeError(f"{UNIT} is not installed on this machine")
    # `enable --now` / `disable --now`: hosting Litecoin also survives a reboot.
    said = _systemctl("--no-block", "enable" if on else "disable", "--now", UNIT)
    if said.returncode != 0:
        raise RuntimeError((said.stderr or said.stdout).strip()[:200] or "systemctl refused")
    return status()

"""A bare address's coins, borrowed from a synced arcade in the shared mesh (2026-11-05).

Why: the operator, 2026-11-05 -- a wallet should only borrow chain data from a node inside the shared arcade
network, never a third-party site. `/signin/{tag}/coins` already lends an *account's* coins, but a bare address
(a Core wallet's receive address, a watched address) has no tag, so an arcade whose own Core is still catching up
showed 0 for it. This is the door for that: one address, its unspent coins and balance, from an arcade whose Core
is at the tip, asked only by another arcade that signs with the mesh key it announced on chain.

Speed first: the coin set is what says what a wallet holds (each small coin may carry an inscription); collection
JSONs and the CDN only fill names and pictures in afterwards. So the answer is the coin rows and nothing heavier,
kept a short while on both sides.
"""
from __future__ import annotations

import time
import urllib.parse
from typing import Any, Callable, Iterable

import nacl.exceptions
import nacl.signing

PURPOSE = "arcade-peer-address-coins"
FRESH = 120.0            # seconds a signed ask stays good
SERVE_KEEP = 30.0        # seconds an answer is kept for the next asker (same address, same tip)
ASK_KEEP = 60.0          # seconds the asking side keeps a peer's answer
MAX_COINS = 3000         # as wallet_assets reads from its own index
SYNC_SLACK = 2           # headers - blocks still counted as at the tip (as Admin -> Chains)

_served: dict[str, tuple[float, dict]] = {}
_asked: dict[str, tuple[float, dict]] = {}


def message(address: str, ts: int) -> bytes:
    """The exact words an asking arcade's mesh key signs."""
    return f"{PURPOSE}\n{address}\n{int(ts)}".encode()


def sign_ask(key: nacl.signing.SigningKey, address: str, now: float | None = None) -> dict[str, str]:
    """Headers for one ask: the mesh key, the time, and the signature over both and the address."""
    ts = int(now if now is not None else time.time())
    return {"X-Arcade-Mesh-Key": key.verify_key.encode().hex(), "X-Arcade-Ts": str(ts),
            "X-Arcade-Sig": key.sign(message(address, ts)).signature.hex()}


def check_ask(headers, address: str, known_keys: Iterable[str], now: float | None = None) -> str | None:
    """None when the ask is from a known mesh peer and fresh; otherwise why not (the refusal's words)."""
    key = str(headers.get("X-Arcade-Mesh-Key") or "").lower()
    sig = str(headers.get("X-Arcade-Sig") or "")
    try:
        ts = int(headers.get("X-Arcade-Ts") or 0)
    except (TypeError, ValueError):
        ts = 0
    if not key or not sig or not ts:
        return "only arcades in the mesh may ask; sign with your mesh key"
    if abs((now if now is not None else time.time()) - ts) > FRESH:
        return "that ask is stale"
    try:
        nacl.signing.VerifyKey(bytes.fromhex(key)).verify(message(address, ts), bytes.fromhex(sig))
    except (ValueError, TypeError, nacl.exceptions.BadSignatureError, nacl.exceptions.ValueError):
        return "bad signature"
    if key not in {str(k).lower() for k in known_keys}:
        return "that mesh key is not a known arcade"
    return None


def synced(info: dict | None) -> bool:
    """getblockchaininfo says this Core is at the tip."""
    if not isinstance(info, dict):
        return False
    try:
        blocks, headers = int(info.get("blocks", 0)), int(info.get("headers", 0))
    except (TypeError, ValueError):
        return False
    return headers > 0 and headers - blocks <= SYNC_SLACK and not info.get("initialblockdownload")


def answer(address: str, height: int, coins: list[dict], marks: dict | None = None,
           now: float | None = None) -> dict:
    """What a synced arcade says about one address: coins (txid, vout, value, height), the total, and which coins
    carry an inscription as far as this arcade already knows (nothing new is read for it)."""
    key = f"{address}@{int(height)}"
    t = now if now is not None else time.time()
    kept = _served.get(key)
    if kept and t - kept[0] < SERVE_KEEP:
        return kept[1]
    rows = []
    for c in coins[:MAX_COINS]:
        row = {k: c.get(k) for k in ("txid", "vout", "value", "height")}
        mark = (marks or {}).get((c.get("txid"), int(c.get("vout") or 0)))
        if mark:
            row["ins"] = mark
        rows.append(row)
    said = {"address": address, "height": int(height), "synced": True,
            "balance_koinu": sum(int(c.get("value") or 0) for c in coins),
            "coins": rows, "complete": len(coins) <= MAX_COINS}
    _served[key] = (t, said)
    return said


def ask_peers(address: str, hosts: list[str], ask: Callable[..., Any], key: nacl.signing.SigningKey,
              now: float | None = None) -> dict | None:
    """The first synced arcade's answer for `address`, with `from` set to its host; None when none could say.
    `ask` is app._ask_arcade (public addresses only, no redirects)."""
    t = now if now is not None else time.time()
    kept = _asked.get(address)
    if kept and t - kept[0] < ASK_KEEP:
        return kept[1]
    for host in hosts[:6]:
        try:
            r = ask(f"https://{host}/peer/address/{urllib.parse.quote(address)}/coins", timeout=10,
                    headers=dict(sign_ask(key, address, t), **{"User-Agent": "arcade-node"}))
            if r.status_code != 200:
                continue
            said = r.json()
        except Exception:                                   # noqa: BLE001 -- that arcade did not answer
            continue
        if not (isinstance(said, dict) and said.get("synced") and said.get("address") == address
                and isinstance(said.get("coins"), list)):
            continue
        said = dict(said, **{"from": host})
        _asked[address] = (t, said)
        return said
    return None


#: Set by the web app at start (app.py): () -> (hosts, mesh key). wallet_assets asks through it.
PEERS: Callable[[], tuple[list[str], Any, Callable[..., Any]]] | None = None

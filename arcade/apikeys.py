"""API keys: the arcade by program, for an operator's websites and for users trying an arcade out (2026-11-04).

SadFrog: "making everything available completely via api locked to node operator provided keys", so that people can
"build and wire up available modules exactly as they'd like while still being an arcade fundamentally". And:
  1. "keys should be able to do everything an operator would be able to do. configuration should be on their end."
  2. "operators can issue keys for node operation for websites they are using the arcade as a back bone - and users
     should get api keys for testing", to bring them to run an arcade of their own.
  3. Where a key may be used from is the operator's to configure.

**Two kinds of key, one header.** `Authorization: Bearer ak_...` on any request.
- An operator key acts as the operator at the machine: every page and action the operator has, spending included,
  unless the operator narrowed it when making it (modules, read only, where from, an end date).
- A user key acts as that user's own account, from outside, exactly as the account signed in would: what the door
  serves the public and the account's own actions, never the node's. The operator says whether users may make keys,
  how many each, and how many calls a minute they get.

**Kept as hashes.** A key is shown once, when it is made; the store keeps its SHA-256, a short id to name it by, and
what it may do. Switching one off or deleting it takes effect on the next call.

Who is calling travels with the request in `CURRENT` (a context variable set by the middleware, web/apikeys_web.py),
so the few places that decide who is asking -- the door's "from outside", the operator check, the form token and
sign-in -- each ask it once, and no route needs to know keys exist.
"""
from __future__ import annotations

import contextvars
import hashlib
import ipaddress
import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

PREFIX = "ak_"
FILE = "apikeys.sqlite"
SETTING = "api_keys"                  # settings.json: {"users": bool, "per_user": int, "per_minute": int}
DEFAULTS = {"users": True, "per_user": 3, "per_minute": 60}
WHERE = ("anywhere", "lan", "machine")
MOST_KEYS = 200
NAME_MOST = 40

#: The key the request in hand came with, once the middleware has checked it; None for every other request.
CURRENT: contextvars.ContextVar["Key | None"] = contextvars.ContextVar("arcade_api_key", default=None)


class KeyRefused(ValueError):
    """What is wrong with a key or with making one, in words for whoever asked."""


@dataclass
class Key:
    id: str                                   # the first 8 characters after ak_, to name it by
    name: str
    owner: str                                # "node" for the operator's, else the account's public key
    modules: list[str] | None = None          # None: every module
    write: bool = True                        # False: GET and HEAD only
    where: str = "anywhere"                   # anywhere | lan | machine | "ip,ip/24,..."
    expires: float | None = None
    made: float = 0.0
    used: float | None = None
    off: bool = False

    @property
    def operator(self) -> bool:
        return self.owner == "node"

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "owner": "operator" if self.operator else "account",
                "modules": self.modules, "write": self.write, "where": self.where, "expires": self.expires,
                "made": self.made, "used": self.used, "off": self.off}


def settings_of(settings: dict) -> dict:
    said = settings.get(SETTING)
    out = dict(DEFAULTS)
    if isinstance(said, dict):
        if isinstance(said.get("users"), bool):
            out["users"] = said["users"]
        for k in ("per_user", "per_minute"):
            if isinstance(said.get(k), int) and not isinstance(said.get(k), bool) and 0 <= said[k] <= 100_000:
                out[k] = said[k]
    return out


def bearer(headers) -> str:
    """The key in an Authorization header, or ""."""
    said = headers.get("authorization", "") or ""
    if said[:7].lower() != "bearer ":
        return ""
    key = said[7:].strip()
    return key if key.startswith(PREFIX) else ""


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


#: Days a key made with no "days" lasts (2026-11-08, security review).
DEFAULT_DAYS = 90


def caller_ip(headers, peer: str | None) -> tuple[str, bool]:
    """(the caller's address, whether it came through an edge such as a tunnel). An edge's header names the caller;
    without one the TCP peer is the caller."""
    # An edge's header only from this machine itself (the arcade's own tunnel or proxy, which connects from loopback):
    # from anybody else it is the caller's own word, and a key limited to an address could be used from anywhere by
    # naming it (2026-11-05 review), or a per-address limit dodged with a new one each time (Cf-Connecting-Ip too,
    # 2026-11-08 security review).
    if peer not in ("127.0.0.1", "::1"):
        return peer or "", False
    edge = (headers.get("cf-connecting-ip") or (headers.get("x-forwarded-for") or "").split(",")[0]).strip()
    return (edge, True) if edge else (peer or "", False)


def client_ip(request) -> str:
    """The caller's address for a per-address limit: caller_ip's, from a request."""
    return caller_ip(request.headers, request.client.host if request.client else None)[0]


def where_ok(where: str, ip: str, edge: bool) -> bool:
    """Whether a call from `ip` may use a key limited to `where`."""
    if where == "anywhere":
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if where == "machine":
        return not edge and addr.is_loopback
    if where == "lan":
        return not edge and (addr.is_loopback or addr.is_private)
    for net in where.split(","):
        try:
            if addr in ipaddress.ip_network(net.strip(), strict=False):
                return True
        except ValueError:
            continue
    return False


def clean_where(where: str) -> str:
    where = (where or "anywhere").strip().lower()
    if where in WHERE:
        return where
    nets = [n.strip() for n in where.split(",") if n.strip()]
    if not nets or len(nets) > 20:
        raise KeyRefused("where is anywhere, lan, machine, or up to 20 addresses (1.2.3.4 or 10.0.0.0/8)")
    for n in nets:
        try:
            ipaddress.ip_network(n, strict=False)
        except ValueError:
            raise KeyRefused(f"{n} is not an address") from None
    return ",".join(nets)


class KeyStore:
    """The arcade's keys, in its home (apikeys.sqlite)."""

    def __init__(self, home: Path):
        self.path = Path(home) / FILE
        self._lock = threading.Lock()
        self._calls: dict[str, list[float]] = {}
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS apikey (hash TEXT PRIMARY KEY, id TEXT NOT NULL, name TEXT NOT NULL,
                          owner TEXT NOT NULL, modules TEXT, write INTEGER NOT NULL, place TEXT NOT NULL,
                          expires REAL, made REAL NOT NULL, used REAL, off INTEGER NOT NULL DEFAULT 0)""")
            db.execute("CREATE INDEX IF NOT EXISTS apikey_owner ON apikey(owner)")

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _key(row) -> Key:
        return Key(id=row["id"], name=row["name"], owner=row["owner"],
                   modules=json.loads(row["modules"]) if row["modules"] else None, write=bool(row["write"]),
                   where=row["place"], expires=row["expires"], made=row["made"], used=row["used"], off=bool(row["off"]))

    def make(self, owner: str, name: str, modules: list[str] | None = None, write: bool = True,
             where: str = "anywhere", expires: float | None = None) -> tuple[str, Key]:
        """(the key itself, shown once; what is kept of it)."""
        name = " ".join(str(name or "").split())[:NAME_MOST]
        if not name:
            raise KeyRefused("give the key a name, to know it by")
        if modules is not None and (not isinstance(modules, list) or not all(isinstance(m, str) for m in modules)):
            raise KeyRefused("modules is a list of module ids, or nothing for every module")
        if expires is not None and expires <= time.time():
            raise KeyRefused("a key cannot end in the past")
        where = clean_where(where)
        with self._lock, self._db() as db:
            if db.execute("SELECT COUNT(*) FROM apikey").fetchone()[0] >= MOST_KEYS:
                raise KeyRefused(f"this arcade holds at most {MOST_KEYS} keys; delete one first")
            key = PREFIX + secrets.token_hex(24)
            kid = key[len(PREFIX):len(PREFIX) + 8]
            now = time.time()
            db.execute("INSERT INTO apikey VALUES (?,?,?,?,?,?,?,?,?,NULL,0)",
                       (_hash(key), kid, name, owner, json.dumps(sorted(set(modules))) if modules is not None else None,
                        int(bool(write)), where, expires, now))
        return key, Key(kid, name, owner, sorted(set(modules)) if modules is not None else None, bool(write), where,
                        expires, now)

    def count(self, owner: str) -> int:
        with self._db() as db:
            return db.execute("SELECT COUNT(*) FROM apikey WHERE owner=?", (owner,)).fetchone()[0]

    def list(self, owner: str | None = None) -> list[Key]:
        """An owner's keys, or every key (owner None), newest first."""
        with self._db() as db:
            rows = (db.execute("SELECT * FROM apikey ORDER BY made DESC") if owner is None
                    else db.execute("SELECT * FROM apikey WHERE owner=? ORDER BY made DESC", (owner,)))
            return [self._key(r) for r in rows]

    def find(self, key: str) -> Key | None:
        if not key.startswith(PREFIX) or len(key) > 100:
            return None
        with self._db() as db:
            row = db.execute("SELECT * FROM apikey WHERE hash=?", (_hash(key),)).fetchone()
        return self._key(row) if row else None

    def touch(self, key: str) -> None:
        with self._db() as db:
            db.execute("UPDATE apikey SET used=? WHERE hash=?", (time.time(), _hash(key)))

    def set_off(self, owner: str | None, kid: str, off: bool) -> bool:
        """Switch a key off or on again; `owner` None for any owner's (the operator's say over every key)."""
        with self._db() as db:
            if owner is None:
                return db.execute("UPDATE apikey SET off=? WHERE id=?", (int(off), kid)).rowcount > 0
            return db.execute("UPDATE apikey SET off=? WHERE id=? AND owner=?", (int(off), kid, owner)).rowcount > 0

    def off_for(self, owner: str) -> int:
        """Switch off every key an account made (its "sign out everywhere", 2026-11-08): a key outlived the sessions
        before, so a key made by whoever held a session went on acting as the account. Off, not deleted: the owner sees
        them and switches back on the ones that are theirs."""
        with self._db() as db:
            return db.execute("UPDATE apikey SET off=1 WHERE owner=? AND off=0", (owner,)).rowcount

    def delete(self, owner: str | None, kid: str) -> bool:
        with self._db() as db:
            if owner is None:
                return db.execute("DELETE FROM apikey WHERE id=?", (kid,)).rowcount > 0
            return db.execute("DELETE FROM apikey WHERE id=? AND owner=?", (kid, owner)).rowcount > 0

    def too_many(self, kid: str, per_minute: int) -> bool:
        """Count a call; whether the key is over its calls a minute (0: no limit)."""
        if per_minute <= 0:
            return False
        now = time.time()
        with self._lock:
            recent = [t for t in self._calls.get(kid, ()) if now - t < 60]
            if len(recent) >= per_minute:
                self._calls[kid] = recent
                return True
            recent.append(now)
            self._calls[kid] = recent
        return False

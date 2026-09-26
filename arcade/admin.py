"""The operator's admin panel: who may open it, and for how long.

2026-09-25: one clean place for everything a node's operator does --
seats and what each may do, the node's own coins, the faucet, screening,
notifications, updates -- and a window on the node machine that points at it.

**Two ways in, and they are deliberately different.**

* **On the node machine itself** (a request the door does not call public:
  loopback or the LAN name, with no edge header) the panel opens with no
  password. Being at the machine is already more than any password proves: the
  wallet file is right there. It is also the only place the remote-access
  password can be set, and the only place the operator account can be named.

* **From anywhere else** it takes two things: being signed in as the account
  this node names as its operator, AND the admin password (the operator
  credential of D-154, checked with its scrypt hash and its attempt limits).
  The password opens an admin session that lasts `ADMIN_HOURS`; a stolen
  account cookie alone opens nothing here, and sending the node's coins asks
  for the password again on every send.

The admin session is its own cookie, HttpOnly and SameSite=Strict, and every
admin write also requires the `X-Arcade-Admin` header -- a header a page on
another site cannot add without a CORS preflight this node never answers -- so
a page the operator happens to visit cannot drive the panel on the machine
where it needs no password.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
from pathlib import Path

ADMIN_COOKIE = "arcade_admin"
ADMIN_HEADER = "x-arcade-admin"
#: How long a remote admin session lasts before the password is asked again.
ADMIN_HOURS = 12

SCHEMA = """
CREATE TABLE IF NOT EXISTS admin_session (
    token_hash TEXT PRIMARY KEY,
    pubkey     TEXT NOT NULL,          -- the operator account it was opened for
    made       INTEGER NOT NULL,
    expires    INTEGER NOT NULL
);
"""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class AdminSessions:
    """Remote admin sessions: opened by the password, tied to the operator."""

    def __init__(self, home: Path):
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(Path(home) / "admin.sqlite", isolation_level=None,
                                    check_same_thread=False)
        self.conn.executescript(SCHEMA)

    def open(self, pubkey: str, now: int | None = None) -> str:
        now = int(now or time.time())
        token = secrets.token_urlsafe(32)
        with self._lock:
            self.conn.execute("DELETE FROM admin_session WHERE expires < ?", (now,))
            self.conn.execute(
                "INSERT INTO admin_session (token_hash, pubkey, made, expires) VALUES (?,?,?,?)",
                (_hash(token), pubkey.lower(), now, now + ADMIN_HOURS * 3600))
        return token

    def valid(self, token: str, operator: str, now: int | None = None) -> bool:
        """Is this an unexpired admin session for the CURRENT operator?"""
        if not token or not operator:
            return False
        now = int(now or time.time())
        with self._lock:
            row = self.conn.execute(
                "SELECT pubkey, expires FROM admin_session WHERE token_hash = ?",
                (_hash(token),)).fetchone()
        return bool(row) and row[0] == operator.lower() and row[1] > now

    def close(self, token: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM admin_session WHERE token_hash = ?",
                              (_hash(token or ""),))

    def close_all(self) -> int:
        with self._lock:
            before = self.conn.total_changes
            self.conn.execute("DELETE FROM admin_session")
            return self.conn.total_changes - before


def username_for(credentials, pubkey: str) -> str | None:
    """The credential name that signs in as this account, if it has one."""
    row = credentials.conn.execute(
        "SELECT username FROM credential WHERE pubkey = ? ORDER BY made DESC LIMIT 1",
        ((pubkey or "").lower(),)).fetchone()
    return row[0] if row else None

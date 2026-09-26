"""Push notifications for the Messenger: "you have a new message", and nothing else.

2026-09-25. A phone that installed the arcade (it is a PWA) can be told
when a message arrives without the page being open.

**What the node knows, and what it does not.** A message is one transaction
that carries the sealed text AND pays the recipient's address the 0.01 that
tells them it came (`/account/write`). That payment is public: anybody reading
the chain sees that an address received a message transaction. This module
uses exactly that -- a new message candidate whose transaction pays the
address of an account on this node -- and nothing more. It never reads a
message, and it could not: the key is in the recipient's browser.

**What a push carries: nothing.** The push services (Google's, Apple's,
Mozilla's) relay an EMPTY push -- a wake-up. The service worker then asks this
node, with the person's own session, "what is new for me?" and draws the
notification from the answer: who it is from, never what it says. So a push
service learns that a device woke up, not what woke it.

**It is optional, twice over.** The operator's node needs `cryptography`
(the VAPID signature is ECDSA over P-256, which neither PyNaCl nor the
standard library provides): `pip install .[push]`. Without it, `available()`
is False and the page does not offer notifications. And each person turns it
on per device; a device that is gone (the push service answers 404 or 410) is
forgotten on the spot.
"""

from __future__ import annotations

import base64
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

import requests

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS device (
    endpoint   TEXT PRIMARY KEY,     -- the push service's URL for one browser
    pubkey     TEXT NOT NULL,        -- the account it belongs to
    p256dh     TEXT NOT NULL DEFAULT '',
    auth       TEXT NOT NULL DEFAULT '',
    created    INTEGER NOT NULL,
    failures   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS device_pubkey ON device(pubkey);
CREATE TABLE IF NOT EXISTS news (
    pubkey     TEXT NOT NULL,
    txid       TEXT NOT NULL,
    sender     TEXT NOT NULL,        -- the sending address, as the chain says
    at         INTEGER NOT NULL,
    told       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (pubkey, txid)
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""

#: How long a push service keeps a wake-up for a phone that is off: a day.
TTL = 24 * 3600
#: A device that failed this many times in a row (not 404/410) is dropped.
MAX_FAILURES = 20
#: Old news is not news: rows older than this are cleared.
KEEP = 7 * 24 * 3600


def available() -> bool:
    """Can this node sign a push at all? (`cryptography` installed.)"""
    try:
        import cryptography.hazmat.primitives.asymmetric.ec  # noqa: F401
        return True
    except Exception:                                  # noqa: BLE001
        return False


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Push:
    """Which devices belong to which account, the node's key, and the sending."""

    def __init__(self, home: Path, contact: str = "https://app.dogecoinarcade.com"):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.contact = contact
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.home / "push.sqlite", isolation_level=None,
                                    check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._key = None

    # --- the node's own key (VAPID) ---------------------------------------------

    def _private(self):
        """The P-256 key this node signs its pushes with; made once, kept."""
        if self._key is not None:
            return self._key
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        path = self.home / "push-vapid.pem"
        with self._lock:
            if path.exists():
                key = serialization.load_pem_private_key(path.read_bytes(), None)
            else:
                key = ec.generate_private_key(ec.SECP256R1())
                path.write_bytes(key.private_bytes(
                    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption()))
                path.chmod(0o600)
            self._key = key
        return key

    def public_key(self) -> str:
        """What a browser subscribes with (`applicationServerKey`), base64url."""
        from cryptography.hazmat.primitives import serialization
        raw = self._private().public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        return _b64(raw)

    def _vapid(self, endpoint: str, now: int) -> str:
        """The Authorization header for one push service (RFC 8292)."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        parts = urlsplit(endpoint)
        head = _b64(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
        body = _b64(json.dumps({"aud": f"{parts.scheme}://{parts.netloc}",
                                "exp": now + 12 * 3600,
                                "sub": self.contact}).encode())
        signing = f"{head}.{body}".encode()
        r, s = decode_dss_signature(self._private().sign(signing, ec.ECDSA(hashes.SHA256())))
        jwt = f"{head}.{body}.{_b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
        return f"vapid t={jwt}, k={self.public_key()}"

    # --- devices -----------------------------------------------------------------

    def subscribe(self, pubkey: str, endpoint: str, p256dh: str = "", auth: str = "",
                  now: int | None = None) -> None:
        endpoint = str(endpoint or "").strip()
        if not endpoint.startswith("https://") or len(endpoint) > 2000:
            raise ValueError("that is not a push address a browser gives out")
        with self._lock:
            self.conn.execute(
                "INSERT INTO device (endpoint, pubkey, p256dh, auth, created) VALUES (?,?,?,?,?)"
                " ON CONFLICT(endpoint) DO UPDATE SET pubkey=excluded.pubkey,"
                " p256dh=excluded.p256dh, auth=excluded.auth, failures=0",
                (endpoint, pubkey, str(p256dh)[:200], str(auth)[:200],
                 int(now or time.time())))

    def unsubscribe(self, pubkey: str, endpoint: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM device WHERE endpoint = ? AND pubkey = ?",
                              (str(endpoint or ""), pubkey))

    def devices(self, pubkey: str) -> list[str]:
        with self._lock:
            return [r["endpoint"] for r in self.conn.execute(
                "SELECT endpoint FROM device WHERE pubkey = ?", (pubkey,))]

    def subscribed(self) -> set[str]:
        with self._lock:
            return {r["pubkey"] for r in self.conn.execute(
                "SELECT DISTINCT pubkey FROM device")}

    # --- news ----------------------------------------------------------------------

    def news(self, pubkey: str, now: int | None = None) -> list[dict]:
        """What arrived for this account and has not been shown; marks it shown."""
        now = int(now or time.time())
        with self._lock:
            rows = [dict(r) for r in self.conn.execute(
                "SELECT txid, sender, at FROM news WHERE pubkey = ? AND told = 0"
                " ORDER BY at", (pubkey,))]
            self.conn.execute("UPDATE news SET told = 1 WHERE pubkey = ? AND told = 0",
                              (pubkey,))
            self.conn.execute("DELETE FROM news WHERE at < ?", (now - KEEP,))
        return rows

    def _note(self, pubkey: str, txid: str, sender: str, now: int) -> bool:
        with self._lock:
            before = self.conn.total_changes
            self.conn.execute("INSERT OR IGNORE INTO news (pubkey, txid, sender, at)"
                              " VALUES (?,?,?,?)", (pubkey, txid, sender, now))
            return self.conn.total_changes > before

    # --- sending -------------------------------------------------------------------

    def send(self, endpoint: str, now: int | None = None, post=requests.post) -> bool:
        """One empty push to one device. False if it did not go; a device the push
        service says is gone is forgotten."""
        now = int(now or time.time())
        try:
            answer = post(endpoint, data=b"", timeout=10, headers={
                "Authorization": self._vapid(endpoint, now),
                "TTL": str(TTL), "Urgency": "high", "Content-Length": "0"})
            status = answer.status_code
        except Exception as exc:                        # noqa: BLE001 -- offline, DNS
            log.info("push to %s failed: %s", urlsplit(endpoint).netloc, exc)
            status = 0
        with self._lock:
            if status in (404, 410):
                self.conn.execute("DELETE FROM device WHERE endpoint = ?", (endpoint,))
                return False
            if 200 <= status < 300:
                self.conn.execute("UPDATE device SET failures = 0 WHERE endpoint = ?",
                                  (endpoint,))
                return True
            self.conn.execute("UPDATE device SET failures = failures + 1 WHERE endpoint = ?",
                              (endpoint,))
            self.conn.execute("DELETE FROM device WHERE failures >= ?", (MAX_FAILURES,))
        log.info("push to %s answered %s", urlsplit(endpoint).netloc, status)
        return False

    def tell(self, pubkey: str, txid: str, sender: str, now: int | None = None,
             post=requests.post) -> int:
        """Note one arrival for an account and wake its devices. How many woke."""
        now = int(now or time.time())
        if not self._note(pubkey, txid, sender, now):
            return 0                                    # already told about this one
        return sum(1 for endpoint in self.devices(pubkey) if self.send(endpoint, now, post))

    # --- watching the chain for arrivals -------------------------------------------

    def cursor(self) -> int | None:
        with self._lock:
            row = self.conn.execute("SELECT v FROM meta WHERE k = 'cursor'").fetchone()
        return int(row["v"]) if row else None

    def set_cursor(self, value: int) -> None:
        with self._lock:
            self.conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('cursor', ?)",
                              (str(int(value)),))

    def watch(self, candidates: Callable[[int], list], newest: Callable[[], int],
              paid: Callable[[str], Iterable[str]], owner_of: Callable[[str], str | None],
              now: int | None = None, post=requests.post) -> int:
        """One pass: every message candidate since the last pass, and who it paid.

        `candidates(after)` gives the store's candidate rows (cursor, txid,
        sender_addr); `paid(txid)` the addresses a transaction pays; `owner_of`
        which account on this node an address is. The first pass only notes where
        the store is: a node that just turned push on does not wake everybody
        for a week of old mail."""
        start = self.cursor()
        if start is None:
            self.set_cursor(newest())
            return 0
        if not self.subscribed():
            self.set_cursor(newest())
            return 0
        woke = 0
        rows = candidates(start)
        for row in rows:
            sender = row["sender_addr"]
            told = set()
            try:
                addresses = list(paid(row["txid"]))
            except Exception as exc:                    # noqa: BLE001 -- node away
                log.info("push could not read %s: %s", row["txid"][:16], exc)
                break
            for address in addresses:
                if address == sender:
                    continue                            # change back to the sender
                pubkey = owner_of(address)
                if pubkey and pubkey not in told:
                    told.add(pubkey)
                    woke += self.tell(pubkey, row["txid"], sender, now, post)
            self.set_cursor(row["cursor"])
        return woke

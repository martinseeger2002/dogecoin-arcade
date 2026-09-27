"""Seats: who may use this node, and how they prove it.

A node is one machine with one chain connection, and the plan
(docs/multi-user.md) puts a number on how many people it serves before it
stops serving any of them well: **fifty**. This module is the register of
those fifty -- who holds a seat, when they were last here, and the seat
coming back after ninety days of silence.

What it deliberately is NOT:

* **Not a credential store.** There is no password here, hashed or
  otherwise. An account is a public key; proving you own it is a signature
  over a nonce this node issued. A stolen copy of this file lets somebody
  read a list of public keys and nothing else -- no key is derivable from
  it, and no session in it can be resumed, because sessions are stored as
  hashes of the token rather than the token.
* **Not ownership of anything.** A seat is permission to use this node's
  index and its transaction builder. The coins are on the chain, the @tag
  is on the chain, and the posts are on the chain. Losing a seat loses the
  seat; a person walks to another node, or their own, and finds everything
  of theirs still there. That is the whole reason the cap is allowed to be
  a hard number rather than an apology.
* **Not chain state.** The floor is set once at launch and does not move
  again (D-151), and even if it did, a seat would survive it: the browser
  key holding one does not know what height anything is read from. No name
  is kept in this file at all, so there is nothing in it that a chain can
  make wrong.

The signature scheme is **Ed25519**, because both halves can do it with no
new dependency: PyNaCl here, and `crypto.subtle` with `{name:'Ed25519'}` in
the browser. The browser's raw public key export is the same 32 bytes
PyNaCl's `VerifyKey` takes, and its signature is the same 64.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

#: How many people one node seats. A setting, never a constant typed into a
#: page: the number on the splash is counted from this table so it cannot
#: drift from the truth (docs/multi-user.md §1c).
SEATS = 50

#: Silence that gives a seat back. No login, no post, no transaction.
IDLE_DAYS = 90

#: How long a session lasts without being used. Shorter than the seat by an
#: order of magnitude: a forgotten browser tab is not a claim on anything.
SESSION_DAYS = 30

#: A challenge is answered in the same breath it is asked for. Two minutes
#: is generous for a phone that has to wake a key up first, and short
#: enough that the table of unspent ones never grows.
CHALLENGE_SECONDS = 120

#: Rate limit on login attempts, per IP and per key, over a rolling window.
#: Not because guessing a Ed25519 key is plausible, but because an endpoint
#: that does public-key arithmetic on demand is a way to spend somebody
#: else's CPU.
ATTEMPTS = 20
ATTEMPT_WINDOW = 300

# --- what one account may do on a node everybody shares ------------------------
#
# One machine, one chain connection, and up to `SEATS` people on it. Without a
# number on each action, the person who writes a loop is a denial of service
# with a byline -- and it is the operator who pays for the blocks and the disk
# the loop fills (docs/multi-user.md §6). So there is a number, it is said on
# the page before it is reached rather than after, and the operator can move it
# on their own Overview. What is not movable is that there is one.
#
# The sizes come out of how fast the chain is, not out of a hunch. A block
# arrives about a minute after the last one and every one of these actions is
# one transaction, so thirty an hour is one every two minutes held all day:
# more than anybody writes prose at, and less than a bot needs. A reaction is
# the same transaction with less inside it, and scrolling and liking genuinely
# is faster than writing, so the like gets twice the room.

HOUR = 3600
DAY = 86400

#: Actions counted per hour, with the default for each. `name` covers every
#: way an account puts a statement about ITSELF on the chain: the key
#: announcement, the name claim, and the profile published on the back of
#: either. A person does each of those once in a while, and something doing
#: them five times an hour is not a person.
#: `inscribe` and `issue` are the ten. Everything else here can be spoken
#: over -- a post is followed by another post, a send is answered by sending
#: it back. An inscription is a piece of the chain that stays, and this node
#: carries it forever, so it is not the same kind of thing at thirty an hour.
#: A token issuance is counted the same way for the same reason, and is its
#: own dial rather than borrowing the inscription one because the two are
#: spent by different people: a run of a hundred pieces should not cost
#: somebody their one go at a token name, and a token should not quietly
#: consume the allowance for the thing whose bytes this node stores. The
#: bytes below still bound how much of it a day adds up to; this bounds how
#: fast.
#: `trade` is the asking-a-trade dial, and it is one dial rather than one per
#: surface, because the surfaces are one gesture seen from different pages: an
#: offer made on somebody's piece is the same transaction as an order put on
#: the book, and both say "I would trade on these terms" in one OP_RETURN. An
#: account that offered thirty times and then ordered ten times an hour would
#: not be trading, it would be spraying, and two dials would have let it call
#: that sixty.
PER_HOUR = {"post": 30, "react": 60, "message": 30, "send": 30, "list": 30,
            "trade": 30, "name": 5, "inscribe": 10, "issue": 10}

#: What each is called in a sentence, so the refusal and the page cannot
#: disagree about what ran out.
LABELS = {"post": "posts", "react": "reactions", "message": "messages",
          "send": "sends", "list": "listings", "trade": "trades",
          "inscribe": "inscriptions", "issue": "token issuances",
          "name": "name and key claims"}

#: Bytes an account may push onto the chain in a day, whatever carried them --
#: the ceiling on how much of the chain one person can make this node store
#: and carry forever. Fifty accounts at this ceiling is about twelve megabytes
#: a day, which is the number the operator is actually buying with the dial.
#: A post is already capped at one transaction by the feed's own rule, so this
#: is mostly about messages, which can be as big as a person seals them.
BYTES_PER_DAY = 250_000

#: How many unsigned offers one account may have standing at once. This is §6's
#: "one active run at a time" translated into what an account can do today,
#: which is build transactions rather than inscribe them: building is free and
#: an offer is a thing held in memory naming coins, so a page that builds and
#: never brings a signature back would otherwise pile them up without limit.
#: Four, rather than one, because a person who changed their mind should not
#: have to wait out an expired offer to ask for the next one.
OFFERS_WAITING = 4

#: The largest allowance that means anything. Past these a number is not a
#: limit somebody set but a limit they stopped believing in, and a form is a
#: place for a stray digit to arrive.
CEILING = 100_000
BYTE_CEILING = 50_000_000


def _number(said: dict, key: str, default: int, ceiling: int) -> int:
    """One allowance out of the settings, clamped to what a person can mean.

    Clamped rather than trusted: the page cannot write anything else, but a
    settings.json edited in a text file can hold a negative -- which would
    mean "refuse everything" by accident -- or a nine-digit number, which
    would mean "no limit" by accident. An allowance that goes wrong goes
    wrong by being small.
    """
    try:
        value = int(said.get(key, default))
    except (TypeError, ValueError):
        return int(default)
    return max(0, min(value, ceiling))


def limits(overrides: dict | None = None) -> dict:
    """The allowance for this node: the defaults, with the operator's numbers over them.

    Read per request rather than cached, so a change on the Overview applies
    to the next action and not to the next restart. A `0` means zero -- an
    operator closing posts out is a decision, not a missing value, so it is
    never replaced by the default.
    """
    said = overrides or {}
    return {"hour": {kind: _number(said, f"quota:{kind}", cap, CEILING)
                     for kind, cap in PER_HOUR.items()},
            "bytes": _number(said, "quota:bytes", BYTES_PER_DAY, BYTE_CEILING)}


SCHEMA = """
-- One row per account ever seated here. `released` is when the seat was
-- given back (idle sweep, or the person asked); a released row is kept
-- rather than deleted so that coming back restores the name and the join
-- date instead of looking like a stranger.
--
-- There is deliberately no @tag column. A tag is chain state: it can move,
-- it is reissued after a floor move, and a copy here would be a name that
-- is wrong rather than a name that is missing. The feed already follows
-- this rule for post bylines (messaging/store.py), and it is the same rule.
-- When a page needs to show whose seat this is, it resolves the name from
-- the chain the way every other page does.
CREATE TABLE IF NOT EXISTS account (
    pubkey   TEXT PRIMARY KEY,           -- 64 hex characters, Ed25519
    created  INTEGER NOT NULL,
    seen     INTEGER NOT NULL,
    released INTEGER
);
CREATE INDEX IF NOT EXISTS account_seen ON account(seen DESC);

-- Nonces handed out and not yet spent. Single use: the row is deleted by
-- the login that answers it, so a replayed signature finds nothing to
-- match.
CREATE TABLE IF NOT EXISTS challenge (
    nonce   TEXT PRIMARY KEY,
    origin  TEXT NOT NULL DEFAULT '',
    made    INTEGER NOT NULL,
    expires INTEGER NOT NULL
);

-- Live sessions, by sha256 of the token. The token itself is never
-- written: a cookie is a bearer credential, and a backup of this file
-- should not be a way into somebody's account.
CREATE TABLE IF NOT EXISTS session (
    token_hash TEXT PRIMARY KEY,
    pubkey     TEXT NOT NULL,
    made       INTEGER NOT NULL,
    expires    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS session_key ON session(pubkey);

-- Login attempts, for the rate limit. Swept as they expire; never a log.
CREATE TABLE IF NOT EXISTS attempt (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    who   TEXT NOT NULL,
    at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS attempt_who ON attempt(who, at);

-- What each account did, for the allowances above. Rows exist to be counted
-- and then deleted when their window closes -- this is not a history, and
-- there is deliberately nothing in it that says WHAT a post said, WHO a
-- message went to or WHICH coins a send moved. The chain holds all of that
-- and this node already indexes it; a copy here would be a second place for
-- the same facts to leak from.
CREATE TABLE IF NOT EXISTS deed (
    pubkey TEXT NOT NULL,
    kind   TEXT NOT NULL,
    at     INTEGER NOT NULL,
    bytes  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS deed_who ON deed(pubkey, kind, at);
"""


class AccountError(Exception):
    """Something a person did that cannot be done. The message is shown."""


class SeatsFull(AccountError):
    """Every seat on this node is taken, and none is idle enough to reclaim."""


@dataclass(frozen=True)
class Account:
    pubkey: str
    created: int
    seen: int
    released: int | None = None

    @property
    def seated(self) -> bool:
        return self.released is None


def _row(row: sqlite3.Row | None) -> Account | None:
    if row is None:
        return None
    return Account(row["pubkey"], row["created"], row["seen"],
                   row["released"])


def is_pubkey(text: str) -> bool:
    """32 bytes of hex, and nothing else. Checked before any storage."""
    if not isinstance(text, str) or len(text) != 64:
        return False
    try:
        bytes.fromhex(text)
    except ValueError:
        return False
    return True


def login_message(origin: str, nonce: str) -> bytes:
    """Exactly what gets signed, byte for byte.

    The origin is in it because a signature is otherwise portable: a nonce
    signed for one node would open a seat on another node that happened to
    issue the same bytes. Naming the node inside the signed message makes a
    signature useless anywhere but where it was asked for -- and makes the
    browser's confirmation honest, because it can show the person which
    site they are signing into.

    The first line is a constant so that a signature made here can never be
    mistaken for a signature over anything else this key ever signs.
    """
    return f"DogecoinArcade login\n{origin}\n{nonce}".encode("utf-8")


class _Rows:
    """A statement's answer, read in full while the lock was held."""

    def __init__(self, rows: list, rowcount: int = -1):
        self._rows = rows
        # What an UPDATE or DELETE touched, as sqlite3's cursor says it. Without
        # it `replace_blob` read 0 after every successful restore and the page
        # said "this node keeps no wallet for these words" (a tester, 2026-09-27).
        self.rowcount = rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list:
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


class _Serialised:
    """One connection, one statement at a time.

    The register's connection is shared by every request thread (and by the
    credentials, the vault and the faucet, which borrow it). sqlite3 does not
    serialise a shared connection for us: two threads stepping statements on it
    at once is what the live node logged as "bad parameter or other API misuse",
    and as a COUNT(*) that came back None -- a 500 on the account page
    (2026-09-25). Each statement now runs and is read to the end under a lock.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._lock = threading.RLock()

    def execute(self, sql: str, params=()) -> _Rows:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return _Rows(cur.fetchall(), cur.rowcount)

    def executescript(self, script: str) -> None:
        with self._lock:
            self._conn.executescript(script)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class Accounts:
    """The seat register. One file, opened per process like the other stores."""

    def __init__(self, path: Path | str, seats: int = SEATS,
                 idle_days: int = IDLE_DAYS):
        self.path = str(path)
        self.seats = int(seats)
        self.idle_days = int(idle_days)
        parent = Path(self.path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(self.path, isolation_level=None,
                              check_same_thread=False)
        raw.row_factory = sqlite3.Row
        self.conn = _Serialised(raw)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # --- seats ----------------------------------------------------------------

    def sweep(self, now: int | None = None) -> list[str]:
        """Give back every seat that has been silent too long.

        Called before anything that depends on the count, rather than on a
        timer: a node nobody is using does not need a thread to notice that
        nobody is using it, and a count is only ever wrong in the direction
        of refusing somebody a seat that is free.
        """
        now = int(now if now is not None else time.time())
        cutoff = now - self.idle_days * 86400
        rows = self.conn.execute(
            "SELECT pubkey FROM account WHERE released IS NULL AND seen < ?",
            (cutoff,)).fetchall()
        if rows:
            self.conn.execute(
                "UPDATE account SET released = ? "
                "WHERE released IS NULL AND seen < ?", (now, cutoff))
        self.conn.execute("DELETE FROM challenge WHERE expires < ?", (now,))
        self.conn.execute("DELETE FROM session WHERE expires < ?", (now,))
        self.conn.execute("DELETE FROM attempt WHERE at < ?",
                          (now - ATTEMPT_WINDOW,))
        # A deed older than the longest window says nothing that is still
        # worth counting, and a table nobody deletes would be a file that
        # only grows on a machine whose whole job is to be shared.
        self.conn.execute("DELETE FROM deed WHERE at < ?", (now - DAY,))
        return [row["pubkey"] for row in rows]

    def taken(self, now: int | None = None) -> int:
        self.sweep(now)
        return self.conn.execute(
            "SELECT COUNT(*) FROM account WHERE released IS NULL").fetchone()[0]

    def free(self, now: int | None = None) -> int:
        return max(0, self.seats - self.taken(now))

    def full(self, now: int | None = None) -> bool:
        return self.free(now) == 0

    def account(self, pubkey: str) -> Account | None:
        return _row(self.conn.execute(
            "SELECT * FROM account WHERE pubkey = ?", (pubkey,)).fetchone())

    def seated(self) -> list[Account]:
        """Everyone holding a seat, longest silent last."""
        return [_row(row) for row in self.conn.execute(
            "SELECT * FROM account WHERE released IS NULL ORDER BY seen DESC")]

    def join(self, pubkey: str, now: int | None = None) -> Account:
        """Seat a key, or re-seat one that was released. Raises `SeatsFull`.

        Joining twice is not an error -- a browser that lost its session and
        signed in again is the ordinary case -- it just touches the seat.
        """
        if not is_pubkey(pubkey):
            raise AccountError("that is not a public key")
        now = int(now if now is not None else time.time())
        existing = self.account(pubkey)
        if existing is not None and existing.seated:
            return self.touch(pubkey, now)
        if self.full(now):
            raise SeatsFull(
                f"all {self.seats} seats on this node are taken. Nothing is "
                f"lost by going elsewhere: your name, your coins and your "
                f"posts are on the chain, so any node will do -- including "
                f"one you run yourself.")
        if existing is not None:
            self.conn.execute(
                "UPDATE account SET released = NULL, seen = ? WHERE pubkey = ?",
                (now, pubkey))
            return self.account(pubkey)
        self.conn.execute(
            "INSERT INTO account (pubkey, created, seen) VALUES (?,?,?)",
            (pubkey, now, now))
        return self.account(pubkey)

    def touch(self, pubkey: str, now: int | None = None) -> Account | None:
        """Mark an account as used. This is what keeps the seat."""
        now = int(now if now is not None else time.time())
        self.conn.execute(
            "UPDATE account SET seen = ? WHERE pubkey = ?", (now, pubkey))
        return self.account(pubkey)

    def release(self, pubkey: str, now: int | None = None) -> None:
        """Give a seat back deliberately, and end that account's sessions."""
        now = int(now if now is not None else time.time())
        self.conn.execute("UPDATE account SET released = ? WHERE pubkey = ?",
                          (now, pubkey))
        self.conn.execute("DELETE FROM session WHERE pubkey = ?", (pubkey,))

    # --- the allowance, because the node is shared ----------------------------

    def used(self, pubkey: str, kind: str, *, now: int | None = None) -> int:
        """How many of one thing this account did in the last hour."""
        now = int(now if now is not None else time.time())
        return self.conn.execute(
            "SELECT COUNT(*) FROM deed WHERE pubkey = ? AND kind = ? AND at > ?",
            (pubkey, kind, now - HOUR)).fetchone()[0]

    def pushed(self, pubkey: str, *, now: int | None = None) -> int:
        """Bytes this account put on the chain in the last day."""
        now = int(now if now is not None else time.time())
        return self.conn.execute(
            "SELECT COALESCE(SUM(bytes), 0) FROM deed WHERE pubkey = ? AND at > ?",
            (pubkey, now - DAY)).fetchone()[0]

    def charge(self, pubkey: str, kind: str, nbytes: int = 0, *,
               caps: dict | None = None, now: int | None = None) -> dict:
        """Count one thing an account did, or refuse it and count nothing.

        Called by the node AFTER it built the transaction and BEFORE it hands
        the offer to the browser. Before the build, and the refusal would be
        a lie about a thing that costs nothing to attempt; after the offer,
        and a build that failed for an unrelated reason -- no coins, a name
        not claimed yet -- would have spent an allowance on something that
        never went near the chain.

        Refusing counts nothing, which matters for the honest sentence on the
        page: `used` says what actually reached the chain, and a loop that is
        being refused does not inflate it.
        """
        now = int(now if now is not None else time.time())
        caps = caps or limits()
        cap = caps["hour"].get(kind)
        if cap is not None:
            already = self.used(pubkey, kind, now=now)
            if already >= int(cap):
                if not int(cap):
                    # Zero is a decision somebody made on their own page, not
                    # a number this account ran out of. Say which it is.
                    raise AccountError(
                        f"this node is not taking {LABELS.get(kind, kind)} "
                        f"from accounts right now -- its operator closed "
                        f"them. Nothing of yours is lost; try another node, "
                        f"or your own.")
                # When the oldest of them leaves the window is when room
                # appears, so this is the only honest number to give: not
                # "try later", but the minute the page can say.
                oldest = self.conn.execute(
                    "SELECT MIN(at) FROM deed WHERE pubkey = ? AND kind = ? "
                    "AND at > ?", (pubkey, kind, now - HOUR)).fetchone()[0]
                raise AccountError(
                    f"that is {int(cap)} {LABELS.get(kind, kind)} in an hour "
                    f"on this node, and this account has used them. Room "
                    f"appears again in "
                    f"{max(1, round(((oldest or now) + HOUR - now) / 60))} "
                    f"minutes.")
        if nbytes:
            room = caps["bytes"] - self.pushed(pubkey, now=now)
            if int(nbytes) > room:
                raise AccountError(
                    f"that is more than the {caps['bytes']:,} bytes a day one "
                    f"account may put on the chain here, and "
                    f"{max(room, 0):,} of them are left.")
        self.conn.execute(
            "INSERT INTO deed (pubkey, kind, at, bytes) VALUES (?,?,?,?)",
            (pubkey, kind, now, int(nbytes or 0)))
        return self.room(pubkey, caps=caps, now=now)

    def room(self, pubkey: str, *, caps: dict | None = None,
             now: int | None = None) -> dict:
        """What the page shows: the numbers, what is used, and nothing else.

        Said rather than enforced silently is the whole point of §6. A person
        who finds a wall they were never told about concludes the node is
        broken, and the operator gets the support message for it.
        """
        now = int(now if now is not None else time.time())
        caps = caps or limits()
        return {
            "hour": HOUR, "day": DAY,
            "actions": [{"kind": kind, "label": LABELS.get(kind, kind),
                         "limit": int(cap),
                         "used": self.used(pubkey, kind, now=now)}
                        for kind, cap in caps["hour"].items()],
            "bytes": {"limit": int(caps["bytes"]),
                      "used": self.pushed(pubkey, now=now)},
        }

    # --- proving who you are --------------------------------------------------

    def challenge(self, origin: str = "", now: int | None = None) -> dict:
        """A nonce to sign. Single use, short-lived, bound to this node."""
        now = int(now if now is not None else time.time())
        self.sweep(now)
        nonce = secrets.token_hex(32)
        expires = now + CHALLENGE_SECONDS
        self.conn.execute(
            "INSERT INTO challenge (nonce, origin, made, expires) "
            "VALUES (?,?,?,?)", (nonce, origin or "", now, expires))
        return {"nonce": nonce, "origin": origin or "", "expires": expires,
                "seconds": CHALLENGE_SECONDS}

    def _rate_limited(self, who: str, now: int) -> bool:
        if not who:
            return False
        count = self.conn.execute(
            "SELECT COUNT(*) FROM attempt WHERE who = ? AND at > ?",
            (who, now - ATTEMPT_WINDOW)).fetchone()[0]
        return count >= ATTEMPTS

    def _attempt(self, who: str, now: int) -> None:
        if who:
            self.conn.execute("INSERT INTO attempt (who, at) VALUES (?,?)",
                              (who, now))

    def login(self, pubkey: str, nonce: str, signature: str, *,
              origin: str = "", ip: str = "", join: bool = False,
              now: int | None = None) -> str:
        """Check a signed challenge and open a session. Returns the token.

        The token is returned rather than stored: this is the only moment it
        exists in a readable form, and the caller's job is to put it in a
        cookie and forget it.

        `join=True` seats a key that has none -- signup, which is the same
        act as logging in for the first time, because there is nothing to
        set up. Without it an unknown key is told to sign up rather than
        seated silently, so that "am I on this node" has a clear answer.
        """
        now = int(now if now is not None else time.time())
        # The challenge is read BEFORE the sweep, because the sweep is what
        # deletes expired ones -- and a person whose phone took three
        # minutes to wake the key up should be told their challenge went
        # stale, not that this node has never heard of it. The two
        # refusals are equally safe and very differently useful.
        row = self.conn.execute(
            "SELECT * FROM challenge WHERE nonce = ?", (nonce or "",)).fetchone()
        self.sweep(now)
        for who in (ip, pubkey):
            if self._rate_limited(who, now):
                raise AccountError(
                    "too many attempts in a row. Wait a few minutes and try "
                    "again.")
        self._attempt(ip, now)
        self._attempt(pubkey, now)

        if not is_pubkey(pubkey):
            raise AccountError("that is not a public key")
        if row is None:
            raise AccountError(
                "that challenge is not one this node is waiting for. Ask for "
                "a new one and sign that.")
        # Spend it first, whatever happens next: a nonce that survives a
        # failed signature is a nonce somebody may try again against.
        self.conn.execute("DELETE FROM challenge WHERE nonce = ?", (nonce,))
        if row["expires"] < now:
            raise AccountError("that challenge has expired. Try again.")
        want = origin or row["origin"]
        if row["origin"] and origin and row["origin"] != origin:
            raise AccountError("that challenge was issued for another address")
        try:
            VerifyKey(bytes.fromhex(pubkey)).verify(
                login_message(want, nonce), bytes.fromhex(signature or ""))
        except (BadSignatureError, ValueError):
            raise AccountError(
                "that signature does not match the key. Nothing was opened.")

        account = self.account(pubkey)
        if account is None or not account.seated:
            if not join:
                raise AccountError(
                    "that key does not hold a seat on this node. Sign up, and "
                    "if there is a space it is yours again -- the name and "
                    "the coins were never here to lose.")
            account = self.join(pubkey, now=now)     # raises SeatsFull
        else:
            self.touch(pubkey, now)

        token = secrets.token_urlsafe(32)
        self.conn.execute(
            "INSERT INTO session (token_hash, pubkey, made, expires) "
            "VALUES (?,?,?,?)",
            (_hash(token), pubkey, now, now + SESSION_DAYS * 86400))
        # A successful login is not an attempt worth counting against the
        # person who made it.
        self.conn.execute("DELETE FROM attempt WHERE who IN (?,?)", (ip, pubkey))
        return token

    def session(self, token: str, now: int | None = None) -> Account | None:
        """Who a cookie belongs to, or None. Touches the seat."""
        if not token:
            return None
        now = int(now if now is not None else time.time())
        row = self.conn.execute(
            "SELECT * FROM session WHERE token_hash = ?",
            (_hash(token),)).fetchone()
        if row is None or row["expires"] < now:
            return None
        account = self.account(row["pubkey"])
        if account is None or not account.seated:
            return None
        # Sliding expiry: using the wallet keeps you signed in, and the seat
        # with it. Written only when it would move by a day, so reading a
        # page is not a write on every request.
        if row["expires"] - now < (SESSION_DAYS - 1) * 86400:
            self.conn.execute(
                "UPDATE session SET expires = ? WHERE token_hash = ?",
                (now + SESSION_DAYS * 86400, row["token_hash"]))
        if now - account.seen > 3600:
            self.touch(account.pubkey, now)
            account = self.account(account.pubkey)
        return account

    def logout(self, token: str) -> None:
        if token:
            self.conn.execute("DELETE FROM session WHERE token_hash = ?",
                              (_hash(token),))


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --- the operator's own way in -------------------------------------------------
#
# Everything above is the non-custodial half: a public key made in a
# browser, proving itself with a signature, and a node that holds no
# password because a password database is an asset worth stealing that
# protects nothing it does not already hold.
#
# That rule is about OTHER PEOPLE's keys. It has nothing to say about the
# person who runs the node reaching their own wallet. Their coins are
# already on that machine, in a wallet file the node can spend from; a
# password guarding the pages creates no asset that is not already there,
# and demanding twenty-four words to open your own arcade is applying a
# rule to the one person it was not written for.
#
# So the operator gets a username and a password, hashed with **scrypt** --
# memory-hard, in the standard library, no new dependency. The parameters
# are stored beside the hash so they can be raised later without locking
# anybody out of a wallet they still know the password to.

#: ~32 MB and something like a tenth of a second. Tuned so that raising it
#: is a change to this line and old rows keep working, because every row
#: carries the parameters it was made with.
SCRYPT_N = 1 << 15
SCRYPT_R = 8
SCRYPT_P = 1

#: One character. Not a recommendation -- a decision (D-157).
#:
#: A floor stops the person who would have chosen something short and
#: stops nobody else: an attacker does not type passwords into a form,
#: they take what is stored and try it offline. What a floor does reliably
#: is refuse somebody their own wallet on their own node.
#:
#: So the rule is replaced by a sentence. Both pages say exactly what a
#: weak password means where it is typed -- and they differ, because the
#: consequences differ: the operator's guards a wallet on their own
#: machine, and an account's guards a blob this node hands to whoever asks
#: for that name.
MIN_PASSWORD = 1

CREDENTIAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS credential (
    username TEXT PRIMARY KEY,           -- lower case, the name typed in
    pubkey   TEXT NOT NULL,              -- the account it signs in as
    salt     TEXT NOT NULL,
    hash     TEXT NOT NULL,
    n        INTEGER NOT NULL,
    r        INTEGER NOT NULL,
    p        INTEGER NOT NULL,
    made     INTEGER NOT NULL,
    used     INTEGER
);
"""


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> str:
    # `maxmem` has to be said out loud: OpenSSL's default ceiling is 32 MB
    # and these parameters need a little over it, so the call fails with
    # "memory limit exceeded" rather than doing less work. Being told the
    # limit is right -- a KDF that quietly used less memory than it was
    # asked for would be weaker than the number beside it claims.
    return hashlib.scrypt(password.encode("utf-8"), salt=salt,
                          n=n, r=r, p=p, dklen=32,
                          maxmem=136 * n * r).hex()


def name_of(username: str) -> str:
    """One spelling of a name, so `the operator` and `robin` are one account."""
    return (username or "").strip().lstrip("@").lower()


class Credentials:
    """Username and password for the account a node belongs to."""

    def __init__(self, accounts: "Accounts"):
        self.conn = accounts.conn
        self.accounts = accounts
        self.conn.executescript(CREDENTIAL_SCHEMA)

    def anybody(self) -> bool:
        return bool(self.conn.execute(
            "SELECT 1 FROM credential LIMIT 1").fetchone())

    def named(self, username: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM credential WHERE username = ?",
                                (name_of(username),)).fetchone()
        return dict(row) if row else None

    def set(self, username: str, password: str, pubkey: str,
            now: int | None = None) -> str:
        """Write down a name and a password for an account.

        Refuses a short password rather than accepting one and hoping: this
        is the only thing between somebody who found the address and a
        wallet, and the page says the rule before it is broken.
        """
        name = name_of(username)
        if not name or not name.replace("_", "").replace("-", "").isalnum():
            raise AccountError(
                "a username is letters, digits, - and _, with no spaces")
        if not (password or ""):
            raise AccountError("a password, please -- even a short one")
        if not is_pubkey(pubkey):
            raise AccountError("that is not a public key")
        now = int(now if now is not None else time.time())
        salt = secrets.token_bytes(16)
        self.conn.execute(
            "INSERT INTO credential (username, pubkey, salt, hash, n, r, p, made) "
            "VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(username) DO UPDATE SET pubkey=excluded.pubkey, "
            "salt=excluded.salt, hash=excluded.hash, n=excluded.n, "
            "r=excluded.r, p=excluded.p, made=excluded.made",
            (name, pubkey.lower(), salt.hex(),
             _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P),
             SCRYPT_N, SCRYPT_R, SCRYPT_P, now))
        return name

    def check(self, username: str, password: str, *, ip: str = "",
              now: int | None = None) -> str:
        """The account this name and password open, or a refusal.

        The same answer for a name that does not exist and a password that
        is wrong, and the same work done either way -- a login that is
        faster when the name is unknown tells somebody which names exist.
        """
        now = int(now if now is not None else time.time())
        self.accounts.sweep(now)
        name = name_of(username)
        for who in (ip, f"pw:{name}"):
            if self.accounts._rate_limited(who, now):
                raise AccountError(
                    "too many attempts in a row. Wait a few minutes and try "
                    "again.")
        self.accounts._attempt(ip, now)
        self.accounts._attempt(f"pw:{name}", now)

        row = self.named(name)
        if row is None:
            # Do the work anyway, against a salt nobody holds, so the time
            # taken says nothing about whether the name exists.
            _scrypt(password or "", secrets.token_bytes(16),
                    SCRYPT_N, SCRYPT_R, SCRYPT_P)
            raise AccountError("that name and password do not open anything.")
        offered = _scrypt(password or "", bytes.fromhex(row["salt"]),
                          row["n"], row["r"], row["p"])
        if not secrets.compare_digest(offered, row["hash"]):
            raise AccountError("that name and password do not open anything.")
        self.conn.execute("UPDATE credential SET used = ? WHERE username = ?",
                          (now, name))
        self.conn.execute("DELETE FROM attempt WHERE who IN (?,?)",
                          (ip, f"pw:{name}"))
        return row["pubkey"]

    def open_session(self, username: str, password: str, *, ip: str = "",
                     now: int | None = None) -> str:
        """Check a name and password, and hand back a session token."""
        now = int(now if now is not None else time.time())
        pubkey = self.check(username, password, ip=ip, now=now)
        account = self.accounts.account(pubkey)
        if account is None or not account.seated:
            self.accounts.join(pubkey, now=now)
        else:
            self.accounts.touch(pubkey, now)
        token = secrets.token_urlsafe(32)
        self.conn.execute(
            "INSERT INTO session (token_hash, pubkey, made, expires) "
            "VALUES (?,?,?,?)",
            (_hash(token), pubkey, now, now + SESSION_DAYS * 86400))
        return token

    def forget(self, username: str) -> None:
        self.conn.execute("DELETE FROM credential WHERE username = ?",
                          (name_of(username),))


# --- the vault: an encrypted wallet the node cannot read ------------------------
#
# Somebody types a name and a password and gets a wallet. That is the whole
# of what a person should have to do, and it is possible without the node
# ever holding a key -- because the password never reaches it.
#
#   signing up   the browser makes twelve words, derives the keys, encrypts
#                the seed under the password and hands over: a @tag, a
#                public key, an address, and a blob of bytes.
#   signing in   fetch the blob for that @tag, decrypt it in the browser,
#                derive the same key, sign the challenge.
#
# The node stores ciphertext and the parameters it was made with. It cannot
# read it, cannot check the password, and cannot tell a wrong password from
# a right one -- only the browser can, by whether what comes out is a seed.
#
# **What this costs, stated rather than implied.** The blob is handed to
# whoever asks for that @tag, so a weak password can be attacked offline at
# whatever speed somebody's hardware allows. That is the price of being
# able to sign in on a device that has never seen your words, and it is why
# the browser's KDF matters, why there is a floor on the password, and why
# the twelve words are shown as the real backup. A wallet that can only be
# opened where it was made is safer and is not what anybody wants.

VAULT_SCHEMA = """
CREATE TABLE IF NOT EXISTS vault (
    tag     TEXT PRIMARY KEY,            -- lower case, the name typed in
    pubkey  TEXT NOT NULL,               -- the account it signs in as
    address TEXT NOT NULL DEFAULT '',    -- where its coins live
    blob    TEXT NOT NULL,               -- ciphertext and KDF parameters
    made    INTEGER NOT NULL,
    claimed TEXT NOT NULL DEFAULT ''     -- the txid of the @tag claim
);
CREATE INDEX IF NOT EXISTS vault_pubkey ON vault(pubkey);
"""


class Vault:
    """Encrypted wallets, by @tag. Bytes this node cannot read."""

    def __init__(self, accounts: "Accounts"):
        self.conn = accounts.conn
        self.accounts = accounts
        self.conn.executescript(VAULT_SCHEMA)

    def get(self, tag: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM vault WHERE tag = ?",
                                (name_of(tag),)).fetchone()
        return dict(row) if row else None

    def by_pubkey(self, pubkey: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM vault WHERE pubkey = ?",
                                ((pubkey or "").lower(),)).fetchone()
        return dict(row) if row else None

    def taken(self, tag: str) -> bool:
        return self.get(tag) is not None

    def put(self, tag: str, pubkey: str, address: str, blob: str,
            now: int | None = None) -> dict:
        """Write down a new wallet. Refuses a name this node already holds.

        Only a refusal about THIS node: the chain decides who holds a name,
        first claim wins, and a name free here may be claimed elsewhere in
        the same minute. The claim says so when it settles; this only stops
        two people on one node colliding before it does.
        """
        name = name_of(tag)
        if not name:
            raise AccountError("a name, please")
        if not is_pubkey(pubkey):
            raise AccountError("that is not a public key")
        if self.taken(name):
            raise AccountError(
                f"@{name} is already signed up on this node. If it is yours, "
                f"sign in; if it is not, choose another.")
        now = int(now if now is not None else time.time())
        self.conn.execute(
            "INSERT INTO vault (tag, pubkey, address, blob, made) "
            "VALUES (?,?,?,?,?)", (name, pubkey.lower(), address, blob, now))
        return self.get(name)

    def replace_blob(self, pubkey: str, blob: str) -> bool:
        """Put a newly encrypted wallet in place of the old one (the Restore page:
        the same words sealed under a new password in the browser). False when
        this node keeps no wallet for that key."""
        if not blob or len(blob) > 20000:
            raise AccountError("that is not an encrypted wallet")
        cur = self.conn.execute("UPDATE vault SET blob = ? WHERE pubkey = ?",
                                (blob, (pubkey or "").lower()))
        return bool(getattr(cur, "rowcount", 0))

    def note_claim(self, tag: str, txid: str) -> None:
        self.conn.execute("UPDATE vault SET claimed = ? WHERE tag = ?",
                          (txid, name_of(tag)))

    def rename(self, pubkey: str, tag: str) -> None:
        """Move a wallet to another name, once the chain says it is theirs."""
        self.conn.execute("UPDATE vault SET tag = ? WHERE pubkey = ?",
                          (name_of(tag), (pubkey or "").lower()))


MAILBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS mailbox (
    pubkey  TEXT PRIMARY KEY,            -- the account it belongs to
    blob    TEXT NOT NULL,               -- ciphertext only
    updated INTEGER NOT NULL
);
"""

#: The most one account's message history may take here, as stored text.
MAILBOX_MAX = 4 * 1024 * 1024


class Mailbox:
    """One encrypted copy of each account's own message history.

    A browser rebuilds from the chain only what was sealed TO it; what it sent
    is sealed to the other person and cannot be read back. So the browser keeps
    its sent letters and its group keys here, sealed with a key derived from its
    own words (messaging.js `syncMailbox`), and a new browser restores them
    (2026-09-26: "I lost my chat history" after changing browsers).
    This node stores bytes it cannot open, exactly as it does the vault.
    """

    def __init__(self, accounts: "Accounts"):
        self.conn = accounts.conn
        self.conn.executescript(MAILBOX_SCHEMA)

    def get(self, pubkey: str) -> dict | None:
        row = self.conn.execute("SELECT blob, updated FROM mailbox WHERE pubkey = ?",
                                ((pubkey or "").lower(),)).fetchone()
        return {"blob": row[0], "updated": int(row[1])} if row else None

    def put(self, pubkey: str, blob: str, base: int, now: int | None = None) -> dict:
        """Replace the copy, if it has not changed since the browser read it.

        `base` is the `updated` the browser merged from. A newer copy written
        by another of the account's browsers in between is refused rather than
        overwritten, and the browser merges again -- so two devices never lose
        each other's letters.
        """
        blob = str(blob or "")
        if len(blob) > MAILBOX_MAX:
            raise AccountError(
                f"your message history is {len(blob):,} bytes and this node keeps "
                f"at most {MAILBOX_MAX:,} per account")
        now = int(now if now is not None else time.time())
        have = self.get(pubkey)
        if have and int(base or 0) != have["updated"]:
            raise MailboxMoved(have["updated"])
        stamp = max(now, (have or {}).get("updated", 0) + 1)
        self.conn.execute(
            "INSERT INTO mailbox (pubkey, blob, updated) VALUES (?,?,?) "
            "ON CONFLICT(pubkey) DO UPDATE SET blob = excluded.blob, "
            "updated = excluded.updated", ((pubkey or "").lower(), blob, stamp))
        return {"updated": stamp}


class MailboxMoved(AccountError):
    """Another browser of the same account wrote the copy first."""

    def __init__(self, updated: int):
        super().__init__("your message history changed on another device; "
                         "merge and try again")
        self.updated = updated

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
        self.conn = sqlite3.connect(self.path, isolation_level=None,
                                    check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
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

#: Twelve, not eight. What this password opens is pages that can spend the
#: node's wallet -- docs/multi-user.md §2 says so plainly rather than
#: implying a breach would expose "hashes and nothing else" -- and a floor
#: chosen for a forum is the wrong floor for that. Said on the page, with
#: what it guards, rather than silently enforced.
MIN_PASSWORD = 12

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
        if len(password or "") < MIN_PASSWORD:
            raise AccountError(
                f"a password of at least {MIN_PASSWORD} characters. This is "
                f"the whole of what stands between somebody who found the "
                f"address and your wallet.")
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

    def note_claim(self, tag: str, txid: str) -> None:
        self.conn.execute("UPDATE vault SET claimed = ? WHERE tag = ?",
                          (txid, name_of(tag)))

    def rename(self, pubkey: str, tag: str) -> None:
        """Move a wallet to another name, once the chain says it is theirs."""
        self.conn.execute("UPDATE vault SET tag = ? WHERE pubkey = ?",
                          (name_of(tag), (pubkey or "").lower()))

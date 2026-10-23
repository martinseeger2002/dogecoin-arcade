"""LLM service on the exchange: an operator sells their own model's tokens, priced per token in any coin or token the
exchange trades (2026-10-23: "There should be a admin LLM tab, where I can enter my local LLM server and decide
how much I want to charge per LLM token ... the bid would always be LLM service ... if they want to be paid in Doge,
they could ask .01 Doge per LLM token ... they could even accept Omni and PRC and DRC 20 tokens").

The operator's choices (2026-10-23): apps and players on every arcade buy (over the mesh); prepaid credit; an input price and
an output price per payment kind; a shared "LLM service" book the buyer chooses from; top-ups paid straight to the
selling operator, whose node keeps the balance. And "a purchaser ... should be able to directly connect their local
harness to the Dogecoin arcade LLM api": an OpenAI-compatible /v1 on the buyer's own arcade, with API keys.

Who a buyer is, at the seller: their address on the messaging chain (the one their @tag points at), proved by
`grant_words` signed with that address's key -- bound to ONE selling arcade and an expiry, so the buyer's own arcade can
carry it for them (their browser signs once a month per seller) and no other arcade can use it. A top-up is proved by
`topup_words` signed with the key that paid: the seller checks the payment reached its address, that the signer's key
is one the payment spent from, and that no one claimed the txid before. Signatures are checked here, from the bytes
(`recover_hash160`), so no Core is needed for either; only the payment itself is read from the chain.

Prices are kept as decimal text in whole coins (or whole tokens) per LLM token; balances and costs as integer units of
the payment kind (hundred-millionths for coins and divisible tokens, whole units for an indivisible token). A reply is
charged after it ends, from the usage the model server reports, rounded up to a unit; its length is capped beforehand
to what the credit pays for, so a balance never goes below zero.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator

import requests

SETTING = "llm"
#: The magic a buyer's key signs with: one for every chain, since the signature is checked here from the bytes.
MAGIC = "Pepecoin Signed Message:\n"
GRANT_DAYS_MOST = 90
COIN = 100_000_000
LEG = re.compile(r"^([a-z0-9]{2,16})-(coin|token-(\d{1,10}))$")
AT_ONCE_MOST = 32
MAX_OUT_MOST = 65536
REQUEST_SECONDS = 600
#: Real-coin payments count once in a block; test coins at once (mempool).
REAL_CONFIRMATIONS = 1


class LlmError(ValueError):
    """Said to whoever asked: a refusal, not a crash."""


# ------------------------------------------------------------------------------------------------ words that get signed

def grant_words(seller: str, address: str, expires: int) -> str:
    """What a buyer's key signs so that `seller` serves `address` until `expires` (unix seconds)."""
    return f"arcade-llm\n{seller}\n{address}\n{int(expires)}"


def topup_words(network: str, txid: str, seller: str, address: str) -> str:
    """What the key that paid signs, so the payment counts as `address`'s credit at `seller`."""
    return f"arcade-llm-topup\n{network}\n{txid}\n{seller}\n{address}"


# ------------------------------------------------------------------------------------------------ signatures

_P = 2 ** 256 - 2 ** 32 - 977
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
      0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0] and (a[1] + b[1]) % _P == 0:
        return None
    if a == b:
        lam = 3 * a[0] * a[0] * pow(2 * a[1], -1, _P)
    else:
        lam = (b[1] - a[1]) * pow(b[0] - a[0], -1, _P)
    x = (lam * lam - a[0] - b[0]) % _P
    return x, (lam * (a[0] - x) - a[1]) % _P


def _mul(k: int, point):
    out = None
    while k:
        if k & 1:
            out = _add(out, point)
        point, k = _add(point, point), k >> 1
    return out


def _varstr(b: bytes) -> bytes:
    return (bytes([len(b)]) if len(b) < 0xFD else b"\xfd" + len(b).to_bytes(2, "little")) + b


def message_hash(message: str, magic: str = MAGIC) -> bytes:
    body = _varstr(magic.encode()) + _varstr(message.encode())
    return hashlib.sha256(hashlib.sha256(body).digest()).digest()


def hash160(data: bytes) -> bytes:
    return hashlib.new("ripemd160", hashlib.sha256(data).digest()).digest()


def recover_hash160(signature: str, message: str, magic: str = MAGIC) -> bytes | None:
    """The hash160 of the key that made a Core-style compact signature (signmessage's base64), or None."""
    try:
        raw = base64.b64decode(signature, validate=True)
    except Exception:                                           # noqa: BLE001
        return None
    if len(raw) != 65 or not 27 <= raw[0] <= 34:
        return None
    flag = raw[0] - 27
    compressed, recid = flag >= 4, flag & 3
    r, s = int.from_bytes(raw[1:33], "big"), int.from_bytes(raw[33:], "big")
    if not (0 < r < _N and 0 < s < _N):
        return None
    x = r + (recid // 2) * _N
    if x >= _P:
        return None
    y = pow((x * x * x + 7) % _P, (_P + 1) // 4, _P)
    if (y * y - x * x * x - 7) % _P:
        return None
    if (y & 1) != (recid & 1):
        y = _P - y
    e = int.from_bytes(message_hash(message, magic), "big")
    q = _mul(pow(r, -1, _N), _add(_mul(s, (x, y)), _mul((-e) % _N, _G)))
    if q is None:
        return None
    key = (bytes([2 + (q[1] & 1)]) + q[0].to_bytes(32, "big")) if compressed \
        else b"\x04" + q[0].to_bytes(32, "big") + q[1].to_bytes(32, "big")
    return hash160(key)


def address_hash(address: str) -> bytes | None:
    """The key hash an address pays (base58 P2PKH, or bech32 P2WPKH), whatever chain it is on; None for others."""
    from .script import b58check_decode, bech32_decode, segwit_decode
    try:
        version, payload = b58check_decode(address)
        return payload if len(payload) == 20 else None
    except Exception:                                           # noqa: BLE001
        pass
    try:
        hrp = bech32_decode(address)[0]
        if hrp:
            ver, prog = segwit_decode(hrp, address)
            return prog if ver == 0 and prog is not None and len(prog) == 20 else None
    except Exception:                                           # noqa: BLE001
        return None
    return None


def signed_by(signature: str, message: str, address: str) -> bool:
    want = address_hash(address)
    return want is not None and recover_hash160(signature, message) == want


# ------------------------------------------------------------------------------------------------ prices and units

def leg_parts(leg: str) -> tuple[str, str, int]:
    """("ltc", "coin", 0) or ("main", "token", 3) for a payment kind's key (trade_orders.leg_key's own form)."""
    m = LEG.match(leg or "")
    if not m:
        raise LlmError(f"not a payment kind: {leg!r}")
    return m.group(1), "token" if m.group(3) else "coin", int(m.group(3) or 0)


def price(text: Any) -> Decimal:
    try:
        d = Decimal(str(text).strip() or "0")
    except InvalidOperation:
        raise LlmError(f"not a price: {text!r}") from None
    if not d.is_finite() or d < 0:
        raise LlmError(f"not a price: {text!r}")
    if d > 0 and d < Decimal("1e-12"):
        raise LlmError("a price below 0.000000000001 rounds to nothing")
    return d.normalize() if d else Decimal(0)


def cost_units(prices: dict, tokens_in: int, tokens_out: int, scale: int) -> int:
    """What a reply costs in units of the payment kind (rounded up, so a tiny reply is never free)."""
    whole = price(prices.get("in", 0)) * int(tokens_in) + price(prices.get("out", 0)) * int(tokens_out)
    return int(math.ceil(whole * scale)) if whole > 0 else 0


def affordable_out(prices: dict, balance: int, tokens_in: int, scale: int) -> int:
    """How many output tokens `balance` units still pay for once the prompt is paid (-1: unlimited, a free price)."""
    p_in, p_out = price(prices.get("in", 0)), price(prices.get("out", 0))
    left = Decimal(balance) / scale - p_in * int(tokens_in)
    if left < 0:
        return 0
    if p_out == 0:
        return -1
    return int(left / p_out)


def fmt(units: int, scale: int) -> str:
    if scale == 1:
        return f"{units:,}"
    whole, frac = divmod(int(units), scale)
    text = f"{whole:,}.{frac:08d}".rstrip("0")
    return text[:-1] if text.endswith(".") else text


# ------------------------------------------------------------------------------------------------ the operator's offer

DEFAULTS = {"on": False, "url": "", "key": "", "models": [], "at_once": 2, "max_out": 4096, "prices": [],
            "name": "", "free_for": []}


def is_free(cfg: dict) -> bool:
    """No asks: free for everyone (2026-10-23: "if I offer my model for free, it should have all the same
    capabilities as a paid model. But I should be able to change it from free to pay at any time by adding an ask")."""
    for p in cfg.get("prices") or []:
        try:
            if price(p.get("in", 0)) > 0 or price(p.get("out", 0)) > 0:
                return False
        except LlmError:
            return False
    return True                                         # no asks, or only asks of 0 in and 0 out


def config(state: Any) -> dict:
    got = state.setting(SETTING) or {}
    out = dict(DEFAULTS)
    if isinstance(got, dict):
        out.update({k: v for k, v in got.items() if k in DEFAULTS})
    return out


def clean(said: dict, kept: dict) -> dict:
    """The admin panel's form, checked: what is saved (the API key kept when the form leaves it blank)."""
    out = dict(kept)
    if "on" in said:
        out["on"] = bool(said["on"])
    if "url" in said:
        url = str(said.get("url") or "").strip().rstrip("/")
        if url.endswith("/v1"):
            url = url[:-3]
        if url and not re.match(r"^https?://[^\s/]+(:\d+)?(/[^\s]*)?$", url):
            raise LlmError("the server's address starts with http:// or https://")
        out["url"] = url
    if said.get("key"):
        out["key"] = str(said["key"]).strip()[:400]
    if "free_for" in said:
        # Operators who use this arcade's models free (2026-10-23: "share my model with Bigchiefenergy if I
        # choose for free while simultaneously having it listed"), already resolved to their addresses by the web layer.
        rows, seen = [], set()
        for row in said.get("free_for") or []:
            if isinstance(row, dict) and row.get("address") and row["address"] not in seen:
                seen.add(row["address"])
                rows.append({"tag": str(row.get("tag") or "")[:24], "address": str(row["address"])[:100]})
        out["free_for"] = rows[:100]
    if said.get("clear_key"):
        out["key"] = ""
    if "name" in said:
        out["name"] = str(said.get("name") or "").strip()[:60]
    if "models" in said:
        models = [str(m).strip()[:200] for m in (said.get("models") or []) if str(m).strip()]
        out["models"] = list(dict.fromkeys(models))[:20]
    if "at_once" in said:
        out["at_once"] = max(1, min(AT_ONCE_MOST, int(said.get("at_once") or 1)))
    if "max_out" in said:
        out["max_out"] = max(16, min(MAX_OUT_MOST, int(said.get("max_out") or 16)))
    if "prices" in said:
        rows, seen = [], set()
        for row in said.get("prices") or []:
            if not isinstance(row, dict):
                continue
            leg = str(row.get("leg") or "").strip()
            leg_parts(leg)
            if leg in seen:
                raise LlmError("each coin or token once")
            seen.add(leg)
            p_in, p_out = price(row.get("in", 0)), price(row.get("out", 0))
            rows.append({"leg": leg, "label": str(row.get("label") or leg)[:80],
                         "in": format(p_in, "f"), "out": format(p_out, "f")})
        out["prices"] = rows[:40]
    out.pop("free", None)
    if out.get("on") and not (out.get("url") and out.get("models")):
        raise LlmError("to offer it, name the server and at least one model")
    return out


# ------------------------------------------------------------------------------------------------ the model server

class Server:
    """An OpenAI-compatible server (vLLM, llama.cpp, Ollama's /v1 ...), as the operator named it."""

    def __init__(self, url: str, key: str = ""):
        self.url, self.key = url.rstrip("/"), key

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.key}"} if self.key else {}

    def models(self) -> list[dict]:
        r = requests.get(f"{self.url}/v1/models", headers=self._headers(), timeout=10)
        r.raise_for_status()
        out = []
        for m in (r.json() or {}).get("data") or []:
            if isinstance(m, dict) and m.get("id"):
                out.append({"id": str(m["id"]), "ctx": int(m.get("max_model_len") or m.get("context_length") or 0)})
        return out

    def count(self, text: str, model: str) -> int:
        """How many tokens `text` is to this server (vLLM's /tokenize), or 0 when it cannot say."""
        try:
            r = requests.post(f"{self.url}/tokenize", json={"model": model, "prompt": text}, headers=self._headers(),
                              timeout=10)
            if r.status_code < 400:
                return int((r.json() or {}).get("count") or 0)
        except Exception:                                       # noqa: BLE001
            pass
        return 0

    def chat(self, body: dict) -> Iterator[dict]:
        """Stream a chat completion: each OpenAI chunk as it comes, the last one carrying `usage`."""
        body = {**body, "stream": True, "stream_options": {"include_usage": True}}
        with requests.post(f"{self.url}/v1/chat/completions", json=body, headers=self._headers(), stream=True,
                           timeout=(10, REQUEST_SECONDS)) as r:
            if r.status_code >= 400:
                try:
                    said = r.json()
                    msg = (said.get("error") or {}).get("message") if isinstance(said.get("error"), dict) \
                        else said.get("detail") or said.get("error") or said.get("message")
                except Exception:                               # noqa: BLE001
                    msg = r.text[:300]
                raise LlmError(f"the model server said: {str(msg)[:300]}")
            for line in r.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    yield json.loads(data)
                except ValueError:
                    continue


# ------------------------------------------------------------------------------------------------ the books

SCHEMA = """
CREATE TABLE IF NOT EXISTS credit (buyer TEXT NOT NULL, leg TEXT NOT NULL, units INTEGER NOT NULL DEFAULT 0,
                                   PRIMARY KEY (buyer, leg));
CREATE TABLE IF NOT EXISTS topup (txid TEXT NOT NULL, leg TEXT NOT NULL, buyer TEXT NOT NULL, units INTEGER NOT NULL,
                                  at REAL NOT NULL, PRIMARY KEY (txid, leg));
CREATE TABLE IF NOT EXISTS usage (at REAL NOT NULL, buyer TEXT NOT NULL, model TEXT NOT NULL, leg TEXT NOT NULL,
                                  tokens_in INTEGER NOT NULL, tokens_out INTEGER NOT NULL, units INTEGER NOT NULL,
                                  via TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS usage_at ON usage (at);
CREATE TABLE IF NOT EXISTS grant_held (account TEXT NOT NULL, seller TEXT NOT NULL, address TEXT NOT NULL,
                                       expires INTEGER NOT NULL, sig TEXT NOT NULL, PRIMARY KEY (account, seller));
CREATE TABLE IF NOT EXISTS apikey (hash TEXT PRIMARY KEY, account TEXT NOT NULL, address TEXT NOT NULL,
                                   seller TEXT NOT NULL, leg TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
                                   tail TEXT NOT NULL DEFAULT '', made REAL NOT NULL, used REAL);
"""


class Books:
    """llm.sqlite in the arcade's home. The seller's side: credit, top-ups, usage. The buyer's side (the buyer's own
    arcade): the grants its accounts' browsers signed, and their API keys."""

    def __init__(self, home: Path | str):
        self.path = Path(home) / "llm.sqlite"
        self._lock = threading.Lock()
        with self._db() as db:
            db.executescript(SCHEMA)
            # How long each reply took, its first piece, and whether it failed (2026-10-23: the admin's analytics page
            # and the load the book shows). Added to a usage table from before them.
            have = {r[1] for r in db.execute("PRAGMA table_info(usage)")}
            for col, kind in (("ms", "INTEGER NOT NULL DEFAULT 0"), ("first_ms", "INTEGER NOT NULL DEFAULT 0"),
                              ("ok", "INTEGER NOT NULL DEFAULT 1"), ("images", "INTEGER NOT NULL DEFAULT 0")):
                if col not in have:
                    db.execute(f"ALTER TABLE usage ADD COLUMN {col} {kind}")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    # --- seller
    def balance(self, buyer: str, leg: str) -> int:
        with self._db() as db:
            row = db.execute("SELECT units FROM credit WHERE buyer=? AND leg=?", (buyer, leg)).fetchone()
        return int(row["units"]) if row else 0

    def balances(self, buyer: str) -> dict[str, int]:
        with self._db() as db:
            return {r["leg"]: int(r["units"]) for r in db.execute("SELECT leg, units FROM credit WHERE buyer=?",
                                                                   (buyer,))}

    def credited(self, txid: str, leg: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM topup WHERE txid=? AND leg=?", (txid, leg)).fetchone()
        return dict(row) if row else None

    def credit(self, txid: str, leg: str, buyer: str, units: int) -> bool:
        """Add a top-up once; False when that payment was already counted (for anybody)."""
        with self._lock, self._db() as db:
            try:
                db.execute("INSERT INTO topup VALUES (?,?,?,?,?)", (txid, leg, buyer, int(units), time.time()))
            except sqlite3.IntegrityError:
                return False
            db.execute("INSERT INTO credit VALUES (?,?,?) ON CONFLICT(buyer, leg) DO UPDATE SET units=units+?",
                       (buyer, leg, int(units), int(units)))
        return True

    def charge(self, buyer: str, leg: str, model: str, tokens_in: int, tokens_out: int, units: int,
               via: str = "", ms: int = 0, first_ms: int = 0, ok: bool = True, images: int = 0) -> int:
        """Take a reply's cost (never below zero) and log it; the balance left."""
        with self._lock, self._db() as db:
            db.execute("INSERT INTO usage (at, buyer, model, leg, tokens_in, tokens_out, units, via, ms, first_ms, ok, "
                       "images) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (time.time(), buyer, model, leg, int(tokens_in), int(tokens_out), int(units), via, int(ms),
                        int(first_ms), 1 if ok else 0, int(images)))
            db.execute("INSERT INTO credit VALUES (?,?,0) ON CONFLICT(buyer, leg) DO "
                       "UPDATE SET units=MAX(0, units-?)", (buyer, leg, int(units)))
            row = db.execute("SELECT units FROM credit WHERE buyer=? AND leg=?", (buyer, leg)).fetchone()
        return int(row["units"]) if row else 0

    def earned(self, since: float = 0.0) -> list[dict]:
        """Per payment kind: replies, tokens and units since `since` (the admin panel's summary)."""
        with self._db() as db:
            return [dict(r) for r in db.execute(
                "SELECT leg, COUNT(*) AS replies, SUM(tokens_in) AS tokens_in, SUM(tokens_out) AS tokens_out, "
                "SUM(units) AS units, COUNT(DISTINCT buyer) AS buyers FROM usage WHERE at>=? GROUP BY leg", (since,))]

    def recently(self, since: float, kind: str = "text") -> dict:
        """Replies (or pictures), failures, output tokens and time spent since `since` (the load the book shows)."""
        which = "model LIKE 'image:%'" if kind == "image" else "model NOT LIKE 'image:%'"
        with self._db() as db:
            r = db.execute("SELECT COUNT(*) AS n, SUM(1-ok) AS bad, SUM(tokens_out) AS out, SUM(ms) AS ms, "
                           f"SUM(first_ms) AS first FROM usage WHERE at>=? AND {which}", (since,)).fetchone()
        return {k: int(r[k] or 0) for k in ("n", "bad", "out", "ms", "first")}

    def analytics(self, since: float, bucket: int = 3600) -> dict:
        """Everything the admin's usage page draws, from `since`: a series per `bucket` seconds and the totals by
        model, by buyer, by payment kind and by where it came from."""
        q = ("SELECT COUNT(*) AS replies, SUM(1-ok) AS failed, SUM(tokens_in) AS tokens_in, SUM(tokens_out) AS "
             "tokens_out, SUM(units) AS units, AVG(CASE WHEN ok=1 AND ms>0 THEN ms END) AS avg_ms, "
             "AVG(CASE WHEN ok=1 AND first_ms>0 THEN first_ms END) AS avg_first_ms, "
             "SUM(CASE WHEN ok=1 AND images=0 THEN ms END) AS ms_sum, COUNT(DISTINCT buyer) AS buyers, "
             "SUM(images) AS images")
        with self._db() as db:
            total = dict(db.execute(q + " FROM usage WHERE at>=?", (since,)).fetchone())
            series = [dict(r) for r in db.execute(
                f"SELECT CAST(at/{int(bucket)} AS INTEGER)*{int(bucket)} AS t, " + q[7:] +
                " FROM usage WHERE at>=? GROUP BY t ORDER BY t", (since,))]
            by = {}
            for name, col in (("model", "model"), ("buyer", "buyer"), ("leg", "leg"), ("via", "via")):
                by[name] = [dict(r) for r in db.execute(
                    f"SELECT {col} AS key, " + q[7:] + f" FROM usage WHERE at>=? GROUP BY {col} "
                    "ORDER BY replies DESC LIMIT 50", (since,))]
            last = [dict(r) for r in db.execute(
                "SELECT at, buyer, model, leg, tokens_in, tokens_out, units, via, ms, first_ms, ok FROM usage "
                "WHERE at>=? ORDER BY at DESC LIMIT 50", (since,))]
        return {"total": total, "series": series, "by": by, "last": last}

    def recent(self, buyer: str, limit: int = 20) -> list[dict]:
        with self._db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM usage WHERE buyer=? ORDER BY at DESC LIMIT ?",
                                                (buyer, int(limit)))]

    # --- buyer's arcade
    def keep_grant(self, account: str, seller: str, address: str, expires: int, sig: str) -> None:
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO grant_held VALUES (?,?,?,?,?)",
                       (account, seller, address, int(expires), sig))

    def grant(self, account: str, seller: str) -> dict | None:
        """A grant that still has a day to run, or None (then the browser signs a new one)."""
        with self._db() as db:
            row = db.execute("SELECT * FROM grant_held WHERE account=? AND seller=?", (account, seller)).fetchone()
        if row is None or int(row["expires"]) < time.time() + 86400:
            return None
        return dict(row)

    def new_key(self, account: str, address: str, seller: str, leg: str, name: str = "") -> str:
        key = "sk-arcade-" + secrets.token_urlsafe(32)
        with self._db() as db:
            db.execute("INSERT INTO apikey VALUES (?,?,?,?,?,?,?,?,NULL)",
                       (self.key_hash(key), account, address, seller, leg, name[:60], key[-4:], time.time()))
        return key

    @staticmethod
    def key_hash(key: str) -> str:
        return hashlib.sha256(key.encode()).hexdigest()

    def key(self, key: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM apikey WHERE hash=?", (self.key_hash(key),)).fetchone()
            if row is not None:
                db.execute("UPDATE apikey SET used=? WHERE hash=?", (time.time(), row["hash"]))
        return dict(row) if row else None

    def keys_of(self, account: str) -> list[dict]:
        with self._db() as db:
            return [{k: r[k] for k in ("hash", "seller", "leg", "name", "tail", "made", "used")}
                    for r in db.execute("SELECT * FROM apikey WHERE account=? ORDER BY made DESC", (account,))]

    def drop_key(self, account: str, key_hash: str) -> bool:
        with self._db() as db:
            return db.execute("DELETE FROM apikey WHERE account=? AND hash=?", (account, key_hash)).rowcount > 0


# ------------------------------------------------------------------------------------------------ checking a payment

def _input_hashes(tx: dict) -> set[bytes]:
    """The key hashes a transaction spent from: each input's public key (P2PKH scriptSig, or P2WPKH witness)."""
    out: set[bytes] = set()
    for vin in tx.get("vin") or []:
        wit = vin.get("txinwitness") or []
        if len(wit) == 2 and len(wit[1]) in (66, 130):
            try:
                out.add(hash160(bytes.fromhex(wit[1])))
            except ValueError:
                pass
            continue
        sig = (vin.get("scriptSig") or {}).get("hex") or ""
        try:
            raw = bytes.fromhex(sig)
        except ValueError:
            continue
        pushes, i = [], 0
        while i < len(raw):
            n = raw[i]
            if 1 <= n <= 75:
                pushes.append(raw[i + 1:i + 1 + n])
                i += 1 + n
            elif n == 76 and i + 1 < len(raw):
                ln = raw[i + 1]
                pushes.append(raw[i + 2:i + 2 + ln])
                i += 2 + ln
            elif n == 77 and i + 2 < len(raw):
                ln = int.from_bytes(raw[i + 1:i + 3], "little")
                pushes.append(raw[i + 3:i + 3 + ln])
                i += 3 + ln
            else:
                i += 1
        if pushes and len(pushes[-1]) in (33, 65) and pushes[-1][0] in (2, 3, 4):
            out.add(hash160(pushes[-1]))
    return out


def _paid_to(tx: dict, address: str) -> int:
    total = 0
    for out in tx.get("vout") or []:
        spk = out.get("scriptPubKey") or {}
        names = spk.get("addresses") or ([spk["address"]] if spk.get("address") else [])
        if address in names:
            total += int(round(Decimal(str(out.get("value", 0))) * COIN))
    return total


def check_coin_payment(rpc: Any, txid: str, pay_to: str, signer_hash: bytes, real_coins: bool) -> int:
    """Units (satoshis) a coin payment brought to `pay_to`, refusing what does not count yet or at all."""
    if not re.fullmatch(r"[0-9a-f]{64}", txid or ""):
        raise LlmError("not a transaction id")
    try:
        tx = rpc.call("getrawtransaction", txid, 1)
    except Exception:                                           # noqa: BLE001
        raise LlmError("that payment is not on the chain yet; try again in a moment") from None
    if real_coins and int(tx.get("confirmations") or 0) < REAL_CONFIRMATIONS:
        raise LlmError("real coins count once the payment is in a block; try again after the next block")
    if signer_hash not in _input_hashes(tx):
        raise LlmError("that payment was not sent from the key that signed for it")
    units = _paid_to(tx, pay_to)
    if units <= 0:
        raise LlmError("that payment sends nothing to this arcade's address")
    return units


def check_token_payment(index: Any, txid: str, pay_to: str, property_id: int, signer_hash: bytes) -> int:
    """Units of an Omni token a send brought to `pay_to` (counted once the token index has read it)."""
    if not re.fullmatch(r"[0-9a-f]{64}", txid or ""):
        raise LlmError("not a transaction id")
    entry = index.transaction(txid)
    if entry is None:
        raise LlmError("the token index has not read that send yet; it counts once its block is read")
    if not entry.get("valid"):
        raise LlmError("the token index says that send was not valid")
    if int(entry.get("property_id") or -1) != int(property_id):
        raise LlmError("that send is of another token")
    if entry.get("got") != pay_to:
        raise LlmError("that send does not go to this arcade's address")
    if address_hash(str(entry.get("gave") or "")) != signer_hash:
        raise LlmError("that send was not made by the key that signed for it")
    units = int(entry.get("amount") or 0)
    if units <= 0:
        raise LlmError("that send carries nothing")
    return units


# ------------------------------------------------------------------------------------------------ the seller

#: What a buyer's request may carry through to the model server; everything else is dropped.
PASSED = ("messages", "max_tokens", "max_completion_tokens", "temperature", "top_p", "stop", "tools", "tool_choice",
          "response_format", "seed", "presence_penalty", "frequency_penalty", "n", "logit_bias", "user",
          "parallel_tool_calls", "top_k", "min_p", "repetition_penalty", "chat_template_kwargs")
MAX_MESSAGES = 1000
MAX_BODY = 4 * 1024 * 1024
WAIT_FOR_SLOT = 120.0


def _text_len(messages: list) -> int:
    n = 0
    for m in messages:
        c = m.get("content") if isinstance(m, dict) else ""
        if isinstance(c, str):
            n += len(c)
        elif isinstance(c, list):
            n += sum(len(str(p.get("text") or "")) for p in c if isinstance(p, dict))
        if isinstance(m, dict) and m.get("tool_calls"):
            n += len(json.dumps(m["tool_calls"]))
    return n


def guess_tokens(body: dict) -> int:
    """A prompt's tokens before the server counts them: about 3 characters each, rounded up (only to cap the reply)."""
    extra = len(json.dumps(body.get("tools"))) if body.get("tools") else 0
    return (_text_len(body.get("messages") or []) + extra) // 3 + 8


class Seller:
    """This arcade's side of a sale: its offer, a buyer's balance, a top-up, a reply metered and charged.

    `config()` is the admin panel's setting; `pay_to(leg)` this arcade's address for a payment kind; `scale(leg)` its
    units per whole coin/token; `verify(leg, txid, pay_to, signer_hash)` the units a payment brought (or LlmError);
    `free(buyer)` whether a buyer is this arcade's own operator (served free); `about()` what the offer says of who
    sells it ({tag, host})."""

    def __init__(self, config, books: Books, pay_to, scale, verify, free, about=lambda: {}):
        self.config, self.books, self.pay_to, self.scale, self.verify = config, books, pay_to, scale, verify
        self.free, self.about = free, about
        self._slots: threading.BoundedSemaphore | None = None
        self._slots_n = 0
        self._lock = threading.Lock()
        self.active = 0                                # replies being written now
        self.waiting = 0                               # replies waiting for a slot
        self._load: tuple[float, dict] | None = None

    def _slot(self) -> threading.BoundedSemaphore:
        n = int(self.config().get("at_once") or 1)
        with self._lock:
            if self._slots is None or n != self._slots_n:
                self._slots, self._slots_n = threading.BoundedSemaphore(n), n
            return self._slots

    def load(self, fresh: bool = False) -> dict:
        """How busy this arcade's model is (2026-10-23: "Users also need a way to know if one of the models is
        congested or being frequently used" -- "So should the admin"): replies running and waiting now, replies in the
        last 15 minutes and hour, output speed, and one word for it."""
        now = time.time()
        if not fresh and self._load and now - self._load[0] < 10:
            return {**self._load[1], "active": self.active, "waiting": self.waiting}
        cfg = self.config()
        q15, h1 = self.books.recently(now - 900), self.books.recently(now - 3600)
        at_once = int(cfg.get("at_once") or 1)
        tok_s = round(h1["out"] / (h1["ms"] / 1000), 1) if h1["ms"] else 0.0
        out = {"at_once": at_once, "last_15m": q15["n"], "last_hour": h1["n"], "failed_hour": h1["bad"],
               "tok_s": tok_s, "first_ms": round(h1["first"] / h1["n"]) if h1["n"] else 0}
        self._load = (now, out)
        return {**out, "active": self.active, "waiting": self.waiting}

    @staticmethod
    def word(load: dict) -> str:
        """idle | steady | busy | full: what the book's pill says."""
        if load.get("waiting"):
            return "full"
        if load.get("active", 0) >= max(1, load.get("at_once", 1)):
            return "busy"
        if load.get("active") or load.get("last_15m", 0) >= 3:
            return "steady"
        return "idle"

    def offer(self) -> dict | None:
        """What this arcade says on the LLM book, or None when it sells nothing."""
        cfg = self.config()
        free = is_free(cfg)
        pictures = self.images.part() if self.images is not None else None
        text = bool(cfg.get("on") and cfg.get("url") and cfg.get("models"))
        if not text and not pictures:
            return None
        prices = []
        for p in ([] if (free or not text) else cfg["prices"]):
            try:
                to = self.pay_to(p["leg"])
            except Exception:                                   # noqa: BLE001 -- a chain away: not offered now
                to = ""
            if to:
                prices.append({**p, "to": to})
        if text and not prices and not free:
            text = False                                        # its coins' chains are away just now
            if not pictures:
                return None
        about = self.about() or {}
        load = self.load()
        load["word"] = self.word(load)
        out = {"name": cfg.get("name") or (pictures or {}).get("name") or "",
               "models": [{"id": m} for m in cfg["models"]] if text else [], "free": free and text,
               "load": load if text else {}, "max_out": int(cfg.get("max_out") or 0) if text else 0,
               "prices": prices, "tag": str(about.get("tag") or ""), "host": str(about.get("host") or "")}
        if pictures:
            out["images"] = pictures
        return out

    images = None                                               # the arcade's imagegen.ImageSeller, set by the web layer

    def paid_legs(self) -> set[str]:
        """The payment kinds a top-up may be in: the model's asks and the pictures' (each only while not free)."""
        legs = set()
        if not is_free(self.config()):
            legs |= {p["leg"] for p in self.config().get("prices") or []}
        if self.images is not None:
            from .imagegen import is_free as pictures_free
            icfg = self.images.config()
            if icfg.get("on") and not pictures_free(icfg):
                legs |= {p["leg"] for p in icfg.get("prices") or []}
        return legs

    def _price(self, leg: str) -> dict:
        if is_free(self.config()):
            return {"leg": leg, "in": "0", "out": "0"}
        if leg == "free":
            raise LlmError("this seller charges for its model now; top up and use one of the coins it takes")
        for p in self.config().get("prices") or []:
            if p.get("leg") == leg:
                return p
        raise LlmError("this arcade does not take that coin or token for its model")

    def balance(self, buyer: str) -> dict:
        legs = self.books.balances(buyer)
        return {"free": bool(self.free(buyer)) or is_free(self.config()),
                "credit": {leg: {"units": u, "text": fmt(u, self.scale(leg))} for leg, u in legs.items()}}

    def topup(self, buyer: str, leg: str, txid: str, sig: str) -> dict:
        legs = self.paid_legs()
        if not legs:
            raise LlmError("this arcade's model is free: no top-up needed")
        if leg not in legs:
            raise LlmError("this arcade does not take that coin or token")
        txid = (txid or "").strip().lower()
        network = leg_parts(leg)[0]
        signer = recover_hash160(sig, topup_words(network, txid, self.node_id(), buyer))
        if signer is None:
            raise LlmError("the payment's signature does not check")
        done = self.books.credited(txid, leg)
        if done is not None:
            if done["buyer"] != buyer:
                raise LlmError("that payment was already counted for somebody else")
            return {"ok": True, "again": True, "units": done["units"], **self.balance(buyer)}
        to = self.pay_to(leg)
        units = int(self.verify(leg, txid, to, signer))
        if not self.books.credit(txid, leg, buyer, units):
            raise LlmError("that payment was already counted")
        return {"ok": True, "units": units, "text": fmt(units, self.scale(leg)), **self.balance(buyer)}

    node_id = staticmethod(lambda: "")                          # set by the web layer: this arcade's mesh id
    arcade_name = staticmethod(lambda: "DogecoinArcade")        # set by the web layer: this arcade's face's name

    def chat(self, buyer: str, leg: str, body: dict, via: str = "") -> Iterator[dict]:
        """One reply, streamed: the model server's OpenAI chunks, then {"arcade": {...}} with what it cost."""
        cfg = self.config()
        if not cfg.get("on"):
            raise LlmError("this arcade is not selling its model now")
        model = str(body.get("model") or "") or (cfg.get("models") or [""])[0]
        if model not in (cfg.get("models") or []):
            raise LlmError(f"this arcade does not sell {model!r}; it sells {', '.join(cfg.get('models') or [])}")
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages or len(messages) > MAX_MESSAGES:
            raise LlmError("a chat needs messages")
        if len(json.dumps(body)) > MAX_BODY:
            raise LlmError("that request is too large")
        # The DogecoinArcade expert (llmdocs.py, the Exchange's chat): this arcade adds the system prompt and the parts of
        # the public docs that bear on the question, and does not charge for them (2026-10-23).
        house = 0
        guess_own = guess_tokens(body)                          # the buyer's own words: what the credit must cover
        if body.get("docs") is True:
            from . import llmdocs
            sysmsg = llmdocs.system_message(messages, self.arcade_name())
            messages = [{"role": "system", "content": sysmsg}] + [
                m for m in messages if not (isinstance(m, dict) and m.get("role") == "system")]
            body = {**body, "messages": messages}
            house = Server(cfg["url"], cfg.get("key") or "").count(sysmsg, model) or len(sysmsg) // 4
        free = bool(self.free(buyer)) or is_free(cfg)
        prices = {"in": 0, "out": 0} if free else self._price(leg)
        scale = self.scale(leg) if not free else COIN
        balance = 0 if free else self.books.balance(buyer, leg)
        guess_in = guess_tokens(body)
        most = int(cfg.get("max_out") or 4096)
        if not free:
            can = affordable_out(prices, balance, guess_own, scale)
            if can == 0:
                raise LlmError("not enough credit for that; top up first")
            if can > 0:
                most = min(most, can)
        asked = body.get("max_tokens") or body.get("max_completion_tokens")
        if isinstance(asked, int) and asked > 0:
            most = min(most, asked)
        out = {k: body[k] for k in PASSED if k in body}
        out.pop("max_completion_tokens", None)
        out.update(model=model, max_tokens=most, n=1)
        slot = self._slot()
        with self._lock:
            self.waiting += 1
        try:
            got = slot.acquire(timeout=WAIT_FOR_SLOT)
        finally:
            with self._lock:
                self.waiting -= 1
        if not got:
            self.books.charge(buyer, leg if not free else "free", model, 0, 0, 0, via, ok=False)
            raise LlmError("the model is busy; try again in a minute")
        with self._lock:
            self.active += 1
        t0 = time.monotonic()
        first_ms = 0
        used_in = used_out = 0
        chars = 0
        heard = False
        cost = left = 0
        try:
            for chunk in Server(cfg["url"], cfg.get("key") or "").chat(out):
                heard = True
                usage = chunk.get("usage")
                if isinstance(usage, dict):
                    used_in = int(usage.get("prompt_tokens") or used_in)
                    used_out = int(usage.get("completion_tokens") or used_out)
                for ch in chunk.get("choices") or []:
                    d = ch.get("delta") or {}
                    if not first_ms and (d.get("content") or d.get("reasoning_content") or d.get("tool_calls")):
                        first_ms = int((time.monotonic() - t0) * 1000)
                    chars += len(d.get("content") or "") + len(d.get("reasoning_content") or "")
                    chars += len(json.dumps(d["tool_calls"])) if d.get("tool_calls") else 0
                yield chunk
        finally:
            slot.release()
            with self._lock:
                self.active -= 1
            ms = int((time.monotonic() - t0) * 1000)
            if heard:                                           # a server that never answered costs nothing
                if not used_in:
                    used_in = guess_in
                if not used_out and chars:
                    used_out = chars // 4 + 1
                cost = 0 if free else cost_units(prices, max(0, used_in - house), used_out, scale)
                left = self.books.charge(buyer, leg if not free else "free", model, used_in, used_out, cost, via,
                                         ms=ms, first_ms=first_ms)
            else:
                self.books.charge(buyer, leg if not free else "free", model, 0, 0, 0, via, ms=ms, ok=False)
        yield {"arcade": {"tokens_in": used_in, "tokens_out": used_out, "cost": cost, "docs_tokens": house,
                          "cost_text": fmt(cost, scale), "left": left, "left_text": fmt(left, scale),
                          "free": free, "leg": leg}}

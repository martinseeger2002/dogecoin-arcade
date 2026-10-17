"""A node finishing its accounts' swaps when their tabs are closed: pre-signed claims and
refunds, held and broadcast at the right moment (a watchtower).

2026-10-13: both sides' tabs "pre-sign" their claim and their refund and hand them
to their own node, so a swap finishes even if the tab is closed -- which is what lets a
lock stand only two hours (trade.LOCK_SECONDS).

Nothing here can move anybody's coins anywhere they did not sign for. A job is a lock
spend the tab built and signed itself (wallet.js tradePresign): one input, the lock; its
outputs paying the account (and, moving a token or NFT out, an OP_RETURN and what is left
of the lock's coin back into the lock). The signature covers those outputs; the node only
adds what the signature does not cover -- the secret, in the input script -- and decides
WHEN to send it:

* refund   once the chain's median time has passed the lock's own time and it is unspent;
* claim    with the secret given (the initiator's own claim) once the lock has `min_conf`
           confirmations and before `not_after`;
* claim    without it (the responder's) once the secret is on the chain: read out of the
           input that spent `secret_lock` -- the account's own lock, which the initiator
           claimed -- and before `not_after`.

A job ends when its lock is spent, by anyone; a send that fails, or that drops out of the
mempool, is sent again on a later pass.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

from . import htlc as H, txbuild

SCHEMA = """
CREATE TABLE IF NOT EXISTS watch_job (
    id          TEXT PRIMARY KEY,          -- network:txid:vout:kind
    account     TEXT NOT NULL,             -- the account's pubkey (who handed it in)
    trade       TEXT NOT NULL DEFAULT '',
    network     TEXT NOT NULL,
    txid        TEXT NOT NULL,             -- the lock coin this spends
    vout        INTEGER NOT NULL,
    kind        TEXT NOT NULL,             -- claim | refund
    redeem      TEXT NOT NULL,             -- the lock script, hex
    raw         TEXT NOT NULL,             -- the spend, its one input script empty
    signature   TEXT NOT NULL,             -- DER + sighash byte, hex
    secret      TEXT NOT NULL DEFAULT '',  -- given (initiator) or found (responder)
    secret_lock TEXT NOT NULL DEFAULT '',  -- JSON {network, txid, vout, redeem}: where it shows
    min_conf    INTEGER NOT NULL DEFAULT 0,
    not_after   INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'waiting',   -- waiting | sent | spent | expired
    sent_txid   TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    created     REAL NOT NULL,
    updated     REAL NOT NULL
);
"""

#: Jobs one account may hold at a time: a tab hands in two or three per swap.
MAX_JOBS_PER_ACCOUNT = 60


class WatchError(ValueError):
    """A job this node will not hold."""


def _varint_at(raw: bytes, at: int) -> tuple[int, int]:
    first = raw[at]
    if first < 0xFD:
        return first, at + 1
    size = {0xFD: 2, 0xFE: 4, 0xFF: 8}[first]
    return int.from_bytes(raw[at + 1:at + 1 + size], "little"), at + 1 + size


def put_scripts(raw: bytes, scripts: list[bytes]) -> bytes:
    """`raw` with each input's (empty) script replaced by `scripts`, in order -- one per input.
    A Doginal comes out of its lock with two inputs, the Doginal and its reserve, both under the
    lock and each with its own signature (htlc.doginal_out, 2026-10-15); everything else, one."""
    count, at = _varint_at(raw, 4)
    if count != len(scripts):
        raise WatchError(f"the spend has {count} inputs, and {len(scripts)} signatures came with it")
    out = bytearray(raw[:at])
    for script in scripts:
        out += raw[at:at + 36]                       # its outpoint
        length, after = _varint_at(raw, at + 36)
        if length != 0:
            raise WatchError("the spend already carries an input script")
        out += txbuild.varint(len(script)) + script
        out += raw[after:after + 4]                  # its sequence
        at = after + 4
    return bytes(out) + raw[at:]


def signatures_of(signature: str | bytes) -> list[bytes]:
    """A held job's signature(s): one hex string, or several joined by commas (one per input)."""
    if isinstance(signature, bytes):
        return [signature]
    return [bytes.fromhex(part) for part in str(signature).split(",") if part]


def complete(raw: bytes, signature: bytes | str, redeem: bytes, kind: str, secret: bytes = b"") -> bytes:
    """The spend with its input script(s) in place (as /account/trade/spend/sign does): one per
    input, each the lock's own refund or claim script with that input's signature."""
    scripts = []
    for sig in signatures_of(signature):
        scripts.append(H.refund_script_sig(sig, redeem) if kind == "refund"
                       else H.claim_script_sig(sig, secret, redeem))
    return put_scripts(raw, scripts)


class Watch:
    """This node's held spends, and the pass that sends them when their moment comes."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._scanned: dict[str, int] = {}     # how far blocks were searched, per lock
        with self._open() as conn:
            conn.executescript(SCHEMA)

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    # --- handing a job in ------------------------------------------------------------------

    def hold(self, *, account: str, trade: str, network: str, txid: str, vout: int, kind: str,
             redeem: str, raw: str, signature: str, secret: str = "", secret_lock: dict | None = None,
             min_conf: int = 0, not_after: int = 0) -> str:
        if kind not in ("claim", "refund"):
            raise WatchError("a held spend is a claim or a refund")
        if kind == "claim" and not secret and not secret_lock:
            raise WatchError("a claim needs the secret, or the lock it will show up in")
        lock = H.parse(bytes.fromhex(redeem))
        if lock is None:
            raise WatchError("that is not a lock script")
        if secret and H.hash_of(bytes.fromhex(secret)) != lock["secret_hash"]:
            raise WatchError("that secret does not open this lock")
        job = f"{network}:{txid}:{int(vout)}:{kind}"
        now = time.time()
        with self._open() as conn:
            count = conn.execute("SELECT COUNT(*) FROM watch_job WHERE account=? AND status='waiting'",
                                 (account,)).fetchone()[0]
            if count >= MAX_JOBS_PER_ACCOUNT:
                raise WatchError("this account already has as many swaps waiting as a node holds")
            conn.execute(
                "INSERT INTO watch_job(id, account, trade, network, txid, vout, kind, redeem, raw, "
                "signature, secret, secret_lock, min_conf, not_after, created, updated) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "raw=excluded.raw, signature=excluded.signature, secret=excluded.secret, "
                "secret_lock=excluded.secret_lock, min_conf=excluded.min_conf, "
                "not_after=excluded.not_after, updated=excluded.updated "
                "WHERE watch_job.status='waiting'",
                (job, account, trade, network, txid.lower(), int(vout), kind, redeem, raw, signature,
                 secret, json.dumps(secret_lock) if secret_lock else "", int(min_conf),
                 int(not_after), now, now))
        return job

    def jobs(self, account: str | None = None, status: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM watch_job WHERE 1=1", []
        if account is not None:
            sql, args = sql + " AND account=?", args + [account]
        if status is not None:
            sql, args = sql + " AND status=?", args + [status]
        with self._open() as conn:
            return [dict(r) for r in conn.execute(sql + " ORDER BY created DESC", args).fetchall()]

    def _set(self, job: str, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._open() as conn:
            conn.execute(f"UPDATE watch_job SET {cols}, updated=? WHERE id=?",
                         (*fields.values(), time.time(), job))

    # --- the pass --------------------------------------------------------------------------

    def tick(self, rpc_for: Callable[[str], Any], now: int | None = None) -> list[tuple[str, str]]:
        """One pass over the waiting jobs. `rpc_for(network)` -> a context manager giving
        that chain's RPC (ChainContext.rpc), or None when this node does not run it.
        Returns [(job id, what happened)] for what changed."""
        done: list[tuple[str, str]] = []
        # Sent jobs too: until the lock shows spent, a spend that was dropped (or undone by
        # a reorg) is sent again.
        for job in self.jobs(status="waiting") + self.jobs(status="sent"):
            try:
                said = self._one(job, rpc_for, int(now or time.time()))
            except Exception as exc:                       # noqa: BLE001 -- next pass
                said = ""
                self._set(job["id"], note=f"will try again: {exc}"[:300])
            if said:
                done.append((job["id"], said))
        return done

    def _one(self, job: dict, rpc_for, now: int) -> str:
        chain = rpc_for(job["network"])
        if chain is None:
            return ""
        with chain as rpc:
            out = rpc.call("gettxout", job["txid"], int(job["vout"]), True)
            if out is None:
                # Spent (by this job, the tab, or the other side) -- or never confirmed and gone.
                self._set(job["id"], status="spent", note="the lock is spent")
                return "spent"
            raw, redeem = bytes.fromhex(job["raw"]), bytes.fromhex(job["redeem"])
            sig = job["signature"]           # one hex signature, or one per input joined by commas
            if job["kind"] == "refund":
                lock = H.parse(redeem)
                median = int(rpc.call("getblockchaininfo").get("mediantime", 0))
                if median < int(lock["locktime"]):
                    return ""
                signed = complete(raw, sig, redeem, "refund")
            else:
                if job["not_after"] and now >= int(job["not_after"]):
                    self._set(job["id"], status="expired", note="too late to claim safely")
                    return "expired"
                if int(out.get("confirmations", 0)) < int(job["min_conf"]):
                    return ""
                secret = bytes.fromhex(job["secret"]) if job["secret"] else self._find_secret(job, rpc_for)
                if not secret:
                    return ""
                signed = complete(raw, sig, redeem, "claim", secret)
            sent = str(rpc.call("sendrawtransaction", signed.hex()))
        if job["status"] == "sent" and sent == job["sent_txid"]:
            return ""                                      # still on its way: nothing new
        self._set(job["id"], status="sent", sent_txid=sent, note=f"sent {job['kind']}")
        return f"sent {job['kind']}"

    def _find_secret(self, job: dict, rpc_for) -> bytes:
        """The secret, out of the input that spent the account's own lock (the initiator's
        claim of it), on whichever chain that lock is."""
        where = json.loads(job["secret_lock"] or "{}")
        chain = rpc_for(where.get("network", ""))
        if chain is None or not where:
            return b""
        from .node_trader import observe
        with chain as rpc:
            seen = observe(rpc, {"txid": where["txid"], "vout": int(where["vout"])}, set(), self._scanned)
        if seen is None or not seen.spent_by_script_sig:
            return b""
        secret = H.secret_from_script_sig(bytes.fromhex(seen.spent_by_script_sig),
                                          bytes.fromhex(where["redeem"]))
        if secret and H.hash_of(secret) == H.parse(bytes.fromhex(job["redeem"]))["secret_hash"]:
            self._set(job["id"], secret=secret.hex())
            return secret
        return b""

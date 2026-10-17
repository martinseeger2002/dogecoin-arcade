"""A game's bank on your own node: it sits in the game's room, hears what players are owed, and pays them in the game's
tokens straight through your node -- no browser, no web API in the write path, no offer waiting in anybody's memory.

    python examples/rpc_bank/bank.py --config bank.json            run it
    python examples/rpc_bank/bank.py --config bank.json --status   the queue
    python examples/rpc_bank/bank.py --config bank.json --retry 12 13   a held job back in the queue, after a person looked

How it works (README.md beside this file says more):

  The room.  `arcade.service.CertRoom` joins the game's room through your arcade's public name as the bank's own
  address, proved by a certificate signed with the bank's key. Players see the bank's @name. A player's game sends
  {"t": "dep", "id": "<unique>", "items": {"<item>": units}}; the bank answers at once {"t": "dep", "id", "to", "ok",
  "paid", "refused", "note"} and queues one job per item (in SQLite, so a restart never loses a promise; a request id is
  answered once). When a payout has gone out it says {"t": "paid", "to", "item", "units", "txid"}.

  The payout.  `arcade.publish.Publisher`: a token SEND from what the bank holds (custody first), a GRANT of new units
  only when custody cannot cover it and the token is a managed one the bank issued. Built from the arcade's own payload
  code, read back with the index's own reader before it is trusted, paid from confirmed coins only. Jobs of the same
  player and item that arrive together go as ONE transaction.

  Never twice.  Every payout is named by an INTENT made of its job ids. The signed bytes are journaled and fsynced
  before the broadcast; asked again, an intent returns its first txid, or sends the very same bytes if the node lost
  them. A job id that already went out under another intent is HELD for a person, never paid by itself.

  Sweep back.  Every 10 minutes the last two days of payouts are checked against the arcade's ledger index (read-only)
  and the node: one the chain refused (invalid) is paid once more under a new intent, a second refusal is HELD; one the
  node lost is sent again as the same bytes. Every hour the bank's small coins are swept into one.

  Trust.  The bank believes what a game says it is owed, within a per-player daily cap. That is fit for a TESTNET game
  and nothing else: on mainnet a bank must prove what it pays for (a referee), and this example refuses to run there.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # the arcade checkout this example lives in

from arcade import fees, publish                       # noqa: E402
from arcade.config import TESTNET, RpcCredentials     # noqa: E402
from arcade.rpc import RpcClient                       # noqa: E402
from arcade.service import CertRoom                    # noqa: E402

DEFAULTS = {"room": "bank", "hold_s": 45, "hold_max_s": 180, "per_minute": 40, "batch_cap": 10_000,
            "per_day": 5_000, "sweep_below": 0.05, "audit_every_s": 600, "sweep_every_s": 3600}


def log(*words) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(w) for w in words), flush=True)


class Bank:
    def __init__(self, cfg: dict, dry: bool = False):
        self.cfg = {**DEFAULTS, **cfg}
        if self.cfg.get("network", "test") != "test":
            raise SystemExit("this example trusts what a game says it is owed: testnet only (see the docstring)")
        self.home = Path(os.path.expanduser(self.cfg["home"]))
        self.home.mkdir(parents=True, exist_ok=True)
        self.dry = dry
        self.tokens: dict[str, int] = {k: int(v) for k, v in self.cfg["tokens"].items()}
        user, _, password = Path(os.path.expanduser(self.cfg["rpc_cookie"])).read_text().strip().partition(":")
        host, _, port = self.cfg["rpc_url"].rpartition("://")[2].rstrip("/").partition(":")
        self.rpc = RpcClient(RpcCredentials(host, int(port), user, password), timeout=120)
        self.key = publish.Key.from_file(os.path.expanduser(self.cfg["key"]))
        self.pub = publish.Publisher(self.rpc, TESTNET, self.key, self.home / "journal", say=log)
        self.address = self.pub.address
        self.ledger = os.path.expanduser(self.cfg["ledger"])
        self.state_path = self.home / "reclaim.json"
        try:
            self.state = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            self.state = {"dead": {}, "redo": {}}
        self.outbox: list[dict] = []

    # ------------------------------------------------------------ the queue
    def db(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.home / "bank.sqlite", timeout=30)
        c.row_factory = sqlite3.Row
        c.executescript("""
            create table if not exists reqs(addr text, id text, paid text, at real, primary key(addr, id));
            create table if not exists jobs(n integer primary key, addr text, item text, units integer, status text,
                tries integer default 0, txid text, err text, at real, done_at real, req text);""")
        return c

    def handle(self, c: sqlite3.Connection, addr: str, msg: dict) -> dict | None:
        """A player's 'dep': answered at once, queued in the same transaction."""
        if msg.get("t") != "dep":
            return None
        rid = str(msg.get("id") or "")[:40]
        if not rid:
            return None
        old = c.execute("select paid from reqs where addr=? and id=?", (addr, rid)).fetchone()
        if old:
            return {"t": "dep", "id": rid, "to": addr, "ok": True, "paid": json.loads(old["paid"]), "note": "Already on its way."}
        today = {r["item"]: r["u"] for r in c.execute(
            "select item, sum(units) u from jobs where addr=? and at>? group by item", (addr, time.time() - 86400))}
        paid, refused = {}, []
        for item, n in (msg.get("items") or {}).items():
            try:
                n = int(n)
            except (TypeError, ValueError):
                continue
            n = min(n, int(self.cfg["per_day"]) - today.get(item, 0))
            if item not in self.tokens or n <= 0:
                refused.append(item)
                continue
            paid[item] = n
            c.execute("insert into jobs(addr,item,units,status,at,req) values(?,?,?,'queued',?,?)", (addr, item, n, time.time(), rid))
        c.execute("insert into reqs values(?,?,?,?)", (addr, rid, json.dumps(paid), time.time()))
        c.commit()
        log("REQ", addr, rid, "paid", json.dumps(paid), "refused", refused)
        return {"t": "dep", "id": rid, "to": addr, "ok": bool(paid), "paid": paid, "refused": refused[:20],
                "note": "On its way to your wallet." if paid else "Nothing to pay (unknown item, or over today's limit)."}

    def take_batch(self, c: sqlite3.Connection) -> tuple[dict | None, list[int]]:
        """The oldest ready job and every other ready job of the same player and item: one payout. A job still
        receiving more of the same (a player mining, one unit at a time) waits up to hold_s for the rest."""
        now = time.time()
        ready = "(status='queued' or (status='retry' and at<?))"
        for job in c.execute(f"select * from jobs where {ready} order by n limit 60", (now,)).fetchall():
            span = c.execute(f"select min(at) a0, max(at) a1 from jobs where {ready} and addr=? and item=?",
                             (now, job["addr"], job["item"])).fetchone()
            if now - span["a1"] < self.cfg["hold_s"] and now - span["a0"] < self.cfg["hold_max_s"]:
                continue
            ids, units = [], 0
            for r in c.execute(f"select n, units from jobs where {ready} and addr=? and item=? order by n limit 200",
                               (now, job["addr"], job["item"])):
                if units + r["units"] > self.cfg["batch_cap"] and ids:
                    break
                ids.append(r["n"])
                units += r["units"]
            return {**dict(job), "units": units}, ids
        return None, []

    # ------------------------------------------------------------ one payout
    def redo_of(self, ids: list[int]) -> int:
        return max([self.state["redo"].get(str(j), 0) for j in ids] or [0])

    def intent(self, op: str, ids: list[int], *parts) -> str:
        redo = self.redo_of(ids)
        said = f"bank {op} jobs={','.join(map(str, sorted(ids)))} {' '.join(map(str, parts))}" + (f" redo={redo}" if redo else "")
        for rec in list(self.pub.transfers.by_key.values()):
            other = rec.get("intent") or ""
            if other == said or " jobs=" not in other or rec.get("txid") in self.state["dead"]:
                continue
            went = {int(x) for x in other.split(" jobs=", 1)[1].split(" ", 1)[0].split(",") if x.isdigit()}
            if went & set(ids):
                raise RuntimeError(f"HOLD: job(s) {sorted(went & set(ids))} already went out as {rec.get('txid')}")
        return said

    def custody(self, pid: int) -> tuple[int, dict]:
        """What the bank holds of a token, less what is already on its way out; and the token itself."""
        from arcade.ledger import LedgerIndex
        index = LedgerIndex(Path(self.ledger), TESTNET, rpc_factory=lambda: None)
        prop = index.property(pid) or {}
        return index.balance(self.address, pid) - index.pending_out(self.address, pid), prop

    def pay(self, job: dict, ids: list[int]) -> str:
        pid, units, to = self.tokens[job["item"]], int(job["units"]), job["addr"]
        free, prop = self.custody(pid)
        if not prop:
            raise RuntimeError(f"token {pid} is not on the chain")
        if prop.get("divisible"):
            raise RuntimeError(f"HOLD: token {pid} is divisible; this bank pays whole units")
        if free >= units:
            out = self.pub.transfer_token(pid, units, to, self.intent("send", ids, pid, units, to), dry=self.dry)
        elif prop.get("managed") and prop.get("issuer") == self.address:
            out = self.pub.grant_token(pid, units, to, self.intent("grant", ids, pid, units, to), dry=self.dry)
        else:
            raise RuntimeError(f"custody has only {free} of token {pid}: not enough, and it cannot be granted")
        return out["txid"]

    # ------------------------------------------------------------ sweep back
    def chain_says(self, txid: str) -> str:
        db = sqlite3.connect(f"file:{self.ledger}?mode=ro", uri=True, timeout=10)
        try:
            row = db.execute("select valid, invalid_reason from arcade_tx where txid=?", (txid,)).fetchone()
        finally:
            db.close()
        if row:
            return "valid" if row[0] else f"invalid: {row[1] or 'no reason given'}"
        return "lost" if self.pub._confirmations(txid) is None else "pending"

    def reclaim(self, c: sqlite3.Connection) -> None:
        now = time.time()
        for r in c.execute("select txid, group_concat(n) ns from jobs where status='done' and txid is not null "
                           "and done_at between ? and ? group by txid", (now - 48 * 3600, now - 1800)).fetchall():
            ids = [int(x) for x in r["ns"].split(",")]
            said = self.chain_says(r["txid"])
            if said in ("valid", "pending") or r["txid"] in self.state["dead"]:
                continue
            marks = ",".join("?" * len(ids))
            if said.startswith("invalid") and self.redo_of(ids) < 1:
                self.state["dead"][r["txid"]] = said
                for j in ids:
                    self.state["redo"][str(j)] = self.state["redo"].get(str(j), 0) + 1
                tmp = self.state_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.state))
                os.replace(tmp, self.state_path)
                c.execute(f"update jobs set status='queued', txid=null, err=?, at=? where n in ({marks})",
                          [f"the chain refused {r['txid']} ({said}): paying again", now] + ids)
                log("RECLAIM", ids, r["txid"], said)
            elif said == "lost" and any(t.get("txid") == r["txid"] for t in self.pub.transfers.by_key.values()):
                c.execute(f"update jobs set status='queued', err=?, at=? where n in ({marks})",
                          [f"the node lost {r['txid']}: the same bytes again", now] + ids)
                log("RECLAIM", ids, r["txid"], "lost: the same bytes again")
            else:
                c.execute(f"update jobs set status='held', err=? where n in ({marks})",
                          [f"{r['txid']}: {said}, and it cannot be paid again by itself: a person looks"] + ids)
                log("HELD", ids, r["txid"], said)
            c.commit()

    def sweep(self) -> None:
        try:
            out = self.pub.consolidate(int(self.cfg["sweep_below"] * fees.COIN),
                                       "bank consolidate " + time.strftime("%Y-%m-%dT%H", time.gmtime()), dry=self.dry)
            if out:
                log("SWEPT", out["inputs"], "small coins into one:", out["txid"])
        except Exception as exc:                       # noqa: BLE001 -- a sweep that fails waits for the next hour
            log("SWEEP FAILED", exc)

    # ------------------------------------------------------------ the two threads
    def deliver(self, stop: threading.Event) -> None:
        c, recent = self.db(), []
        t_audit = t_sweep = 0.0
        while not stop.is_set():
            if time.time() - t_audit > self.cfg["audit_every_s"]:
                t_audit = time.time()
                try:
                    self.reclaim(c)
                except Exception as exc:               # noqa: BLE001
                    log("RECLAIM FAILED", exc)
            if time.time() - t_sweep > self.cfg["sweep_every_s"]:
                t_sweep = time.time()
                self.sweep()
            recent = [t for t in recent if t > time.time() - 60]
            job, ids = self.take_batch(c) if len(recent) < self.cfg["per_minute"] else (None, [])
            if not job:
                stop.wait(3)
                continue
            marks = ",".join("?" * len(ids))
            if job["units"] > self.cfg["batch_cap"]:
                c.execute(f"update jobs set status='held', err='over the batch cap: a person looks' where n in ({marks})", ids)
                c.commit()
                continue
            try:
                txid = self.pay(job, ids)
                c.execute(f"update jobs set status='done', txid=?, done_at=? where n in ({marks})", [txid, time.time()] + ids)
                c.commit()
                recent.append(time.time())
                log("PAID", job["addr"], job["item"], job["units"], txid, "DRY" if self.dry else "")
                self.outbox.append({"t": "paid", "to": job["addr"], "item": job["item"], "units": job["units"], "txid": txid})
            except Exception as exc:                   # noqa: BLE001 -- every failure is recorded on the job
                said = str(exc)[:300]
                if said.startswith("HOLD") or isinstance(exc, publish.RpcTransportError):
                    c.execute(f"update jobs set status='held', err=? where n in ({marks})", [said] + ids)
                    log("HELD", ids, said)
                else:
                    tries = job["tries"] + 1
                    wait = 90 if "enough" in said else 60 * min(30, 2 ** tries)
                    c.execute(f"update jobs set status=?, tries=?, err=?, at=? where n in ({marks})",
                              ["failed" if tries >= 12 else "retry", tries, said, time.time() + wait] + ids)
                    log("RETRY" if tries < 12 else "FAILED", ids, said)
                c.commit()
            stop.wait(1)

    def listen(self, stop: threading.Event) -> None:
        c = self.db()
        room = CertRoom(int(Path(os.path.expanduser(self.cfg["key"])).read_text().strip(), 16), self.address,
                        self.cfg["game"], self.cfg["room"], node=self.cfg["node"])
        for event in room.events():
            if stop.is_set():
                return
            while self.outbox:
                room.send(self.outbox.pop(0))
            member = event.get("member") or {}
            if event.get("type") != "message" or member.get("guest") or not member.get("address") \
                    or member["address"] == self.address:
                continue
            try:
                msg = json.loads(event.get("data") or "null")
                answer = self.handle(c, member["address"], msg) if isinstance(msg, dict) else None
            except Exception:                          # noqa: BLE001
                log("HANDLE ERROR", traceback.format_exc()[-300:])
                continue
            if answer:
                room.send(answer)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", required=True)
    ap.add_argument("--dry", action="store_true", help="build and check every payout, send nothing")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--retry", nargs="+", type=int, metavar="JOB")
    args = ap.parse_args(argv)
    bank = Bank(json.loads(Path(args.config).read_text()), dry=args.dry)
    c = bank.db()
    if args.status:
        for r in c.execute("select status, count(*) n, sum(units) u from jobs group by status"):
            print(dict(r))
        for r in c.execute("select n, addr, item, units, err from jobs where status in ('held','failed') order by n desc limit 20"):
            print(dict(r))
        return 0
    if args.retry:
        for n in args.retry:
            done = c.execute("update jobs set status='queued', err=null, at=? where n=? and status in ('held','failed')",
                             (time.time(), n)).rowcount
            print(n, "queued" if done else "not held or failed")
        c.commit()
        return 0
    log("BANK", bank.address, "in", bank.cfg["game"], "/", bank.cfg["room"], "DRY" if args.dry else "")
    stop = threading.Event()
    threading.Thread(target=bank.deliver, args=(stop,), daemon=True).start()
    try:
        bank.listen(stop)
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""A launchpad paid in DigiDollar test: the Bytelings' (2026-11-07, SadFrog: "a launch pad that only takes digidollar
test ... the main collection of the Arcade on digibyte thanking people for testing out digidollar test").

The pieces are DigiByte ordinals on mainnet; the price is DigiDollars on DigiByte's test chain. No one transaction can
swap across two chains, so the pad holds the pieces itself and hands one over once it is paid:

1. **The pad's key** (`ddpad.key` in the arcade's home, 0600, made once, as node_trader.TradeKey): its DigiByte address
   holds the inscribed pieces and the DGB for fees; its DigiDollar test address (the same key's Taproot output) is
   where mints pay.
2. **Inscribing.** `python -m arcade.ddpad load <build>` files a HashLips build (examples/bytelings/generate.py) in the
   pad's book. With `inscribe` on, the arcade inscribes one piece at a time to the pad's own address, signing with the
   pad's key: no wallet clicks a hundred times.
3. **A mint.** A signed-in account asks; the pad holds a random piece for it (which one is said only once it has
   paid, so nobody can look and walk away), and offers a DigiDollar test send of the price to the pad, built by the
   Wallet's own DigiDollar send (arcade/digidollar.py). The account's browser signs it, as any send.
4. **Paid.** Once the payment is in a block and pays the pad's DigiDollar address at least the price, the pad sends the
   piece to the account's DigiByte address, the inscribed coin whole as output 0 (funding.build_doginal_send's rule).
   A payment that never lands lets the piece go after PAY_WAIT; one held but never paid, after HOLD_SECONDS.

One per account, per DigiByte address and per DigiDollar address (`per_wallet`). The engine here holds no chain: what
it asks of one goes through an `Env` (arcade/web/ddpadweb.py is the arcade's), so it is tested without one.

Evolving: every `evolve_days` after the pad opens, the next stage can be reached (Bit, then Byte, Kilo, Mega, Giga).
The pad only says when; how a baby grows (an owner signs to burn babies for a grown one) is a later Bytelings site.
"""

from __future__ import annotations

import contextlib
import json
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Protocol

from .node_trader import TradeKey

#: What a pad is called unless its config says otherwise, and where it keeps its book.
BOOK = "ddpad.sqlite"
KEY = "ddpad.key"
#: How long a held piece waits for its payment to be signed: an offer's own life (web/account.py OFFER_SECONDS), and
#: a minute more for the broadcast.
HOLD_SECONDS = 300 + 60
#: How long a broadcast payment may take to reach a block before the piece goes back.
PAY_WAIT = 3 * 3600
#: The stages a Byteling grows through (examples/bytelings/generate.py STAGE_NAMES).
STAGES = ("Bit", "Byte", "Kilo", "Mega", "Giga")
DAY = 86400

DEFAULTS = {"name": "Bytelings", "chain": "dgb", "pay_chain": "dgbtest", "price_cents": 100, "per_wallet": 1,
            "open": False, "inscribe": False, "opened_at": 0, "evolve_days": 30}

SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    key       TEXT PRIMARY KEY,
    value     TEXT NOT NULL
);
-- The collection: each piece's picture and traits, then where it was inscribed.
CREATE TABLE IF NOT EXISTS piece (
    number    INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    traits    TEXT NOT NULL,                -- JSON {trait: value}
    content   BLOB NOT NULL,
    ctype     TEXT NOT NULL,
    iid       TEXT,                         -- <reveal txid>i0 once inscribed
    outpoint  TEXT,                         -- txid:vout of the coin that carries it while the pad holds it
    value     INTEGER,
    commit_tx TEXT
);
-- Each mint, from the hold to the delivery.
CREATE TABLE IF NOT EXISTS mint (
    id        TEXT PRIMARY KEY,
    account   TEXT NOT NULL,                -- the account's key
    to_addr   TEXT NOT NULL,                -- its DigiByte address: where the piece goes
    payer     TEXT NOT NULL,                -- its DigiDollar test address: where the payment comes from
    number    INTEGER NOT NULL,
    state     TEXT NOT NULL,                -- held, paying, paid, sending, sent, let go
    pay_txid  TEXT UNIQUE,
    send_txid TEXT,
    made      REAL NOT NULL,
    changed   REAL NOT NULL,
    why       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS mint_state ON mint(state);
"""

#: A mint in one of these still holds its piece and counts against its wallet's limit.
LIVE = ("held", "paying", "paid", "sending", "sent")


class PadError(ValueError):
    """Said to the person asking, as it is."""


class PadKey(TradeKey):
    """The pad's own key: made once, kept 0600 beside the arcade's other keys."""

    def __init__(self, home: Path):
        super().__init__(home)
        self.path = Path(home) / KEY


class Env(Protocol):
    """What the pad asks of the chains. arcade/web/ddpadweb.py is the arcade's; tests give their own."""

    def payment(self, txid: str) -> tuple[str, int]:
        """("confirmed" | "waiting" | "missing", the cents it pays the pad's DigiDollar address)."""

    def ready(self) -> bool:
        """True when nothing the pad broadcast on the piece's chain is still waiting for the index."""

    def inscribe(self, content_type: str, content: bytes) -> dict:
        """Inscribe to the pad's own address: {iid, outpoint, value, commit}."""

    def deliver(self, iid: str, outpoint: str, value: int, to: str) -> str:
        """Send the coin that carries `iid` to `to`; the txid."""

    def settled(self, txid: str) -> bool:
        """True once `txid` is in a block on the piece's chain."""


class Pad:
    """One pad's book and what it does each tick."""

    def __init__(self, home: Path):
        self.home = Path(home)
        self.path = self.home / BOOK

    @contextlib.contextmanager
    def db(self):
        self.home.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # --- config ------------------------------------------------------------------------------------------------

    def config(self) -> dict:
        got = dict(DEFAULTS)
        if self.path.exists():
            with self.db() as conn:
                got.update({r["key"]: json.loads(r["value"]) for r in conn.execute("SELECT key, value FROM config")})
        return got

    def set(self, **values) -> dict:
        unknown = set(values) - set(DEFAULTS)
        if unknown:
            raise PadError(f"a pad has no {', '.join(sorted(unknown))}")
        with self.db() as conn:
            for k, v in values.items():
                conn.execute("INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = "
                             "excluded.value", (k, json.dumps(v)))
        return self.config()

    def open(self, now: float | None = None) -> dict:
        """Open for minting: only once every piece is inscribed and sitting in a block."""
        with self.db() as conn:
            total, done = conn.execute("SELECT COUNT(*), COUNT(outpoint) FROM piece").fetchone()
        if not total:
            raise PadError("no pieces are loaded: python -m arcade.ddpad load <build>")
        if done < total:
            raise PadError(f"{total - done} of {total} pieces are not inscribed yet")
        cfg = self.config()
        return self.set(open=True, opened_at=cfg["opened_at"] or int(now or time.time()))

    # --- the collection ----------------------------------------------------------------------------------------

    def load(self, build: Path) -> int:
        """File a HashLips build (build/images/<n>.png, build/json/<n>.json): each piece's picture and traits.
        Pieces already inscribed keep where they are."""
        build = Path(build)
        filed = 0
        with self.db() as conn:
            for meta in sorted((build / "json").glob("*.json"), key=lambda p: p.stem):
                if not meta.stem.isdigit():
                    continue
                said = json.loads(meta.read_text())
                n = int(meta.stem)
                image = build / "images" / f"{n}.png"
                traits = {a["trait_type"]: a["value"] for a in said.get("attributes") or []}
                content = image.read_bytes()
                row = conn.execute("SELECT iid, content FROM piece WHERE number = ?", (n,)).fetchone()
                if row is not None and row["iid"]:
                    if bytes(row["content"]) != content:
                        raise PadError(f"#{n} is inscribed already, and this build draws it differently")
                    continue
                conn.execute("INSERT INTO piece (number, name, traits, content, ctype) VALUES (?,?,?,?,?) "
                             "ON CONFLICT(number) DO UPDATE SET name = excluded.name, traits = excluded.traits, "
                             "content = excluded.content, ctype = excluded.ctype",
                             (n, str(said.get("name") or f"#{n}"), json.dumps(traits), content, "image/png"))
                filed += 1
        return filed

    def pieces(self) -> list[dict]:
        """Every piece, and whether it is taken: paid for, that is. One only held is not said, or whoever held it
        could see which it is and let it go for another."""
        with self.db() as conn:
            rows = conn.execute("SELECT number, name, traits, iid, outpoint FROM piece ORDER BY number").fetchall()
            taken = {r[0] for r in conn.execute(
                f"SELECT number FROM mint WHERE state IN ({','.join('?' * (len(LIVE) - 1))})", LIVE[1:])}
        return [{"number": r["number"], "name": r["name"], "traits": json.loads(r["traits"]), "iid": r["iid"] or "",
                 "inscribed": bool(r["outpoint"]), "taken": r["number"] in taken} for r in rows]

    def picture(self, number: int) -> tuple[str, bytes] | None:
        with self.db() as conn:
            row = conn.execute("SELECT ctype, content FROM piece WHERE number = ?", (int(number),)).fetchone()
        return (row["ctype"], bytes(row["content"])) if row else None

    # --- a mint ------------------------------------------------------------------------------------------------

    def hold(self, account: str, to_addr: str, payer: str, now: float | None = None) -> dict:
        """Hold a random piece for an account that is about to pay: refused when the pad is shut, sold out, or this
        account, address or DigiDollar address has had its share."""
        now = now or time.time()
        cfg = self.config()
        if not cfg["open"]:
            raise PadError(f"the {cfg['name']} pad is not open yet")
        account = account.lower()
        with self.db() as conn:
            self._let_go_stale(conn, now)
            limit = int(cfg["per_wallet"])
            for column, value, who in (("account", account, "this account"), ("to_addr", to_addr, "this DigiByte "
                                       "address"), ("payer", payer, "this DigiDollar address")):
                had = conn.execute(f"SELECT COUNT(*) FROM mint WHERE {column} = ? AND state IN "
                                   f"({','.join('?' * len(LIVE))})", (value, *LIVE)).fetchone()[0]
                if had >= limit:
                    raise PadError(f"{who} has minted one already: one each" if limit == 1
                                   else f"{who} has minted its {limit} already")
            free = [r[0] for r in conn.execute(
                f"SELECT number FROM piece WHERE outpoint IS NOT NULL AND number NOT IN (SELECT number FROM mint "
                f"WHERE state IN ({','.join('?' * len(LIVE))}))", LIVE)]
            if not free:
                raise PadError(f"every {cfg['name'][:-1] if cfg['name'].endswith('s') else cfg['name']} is taken")
            mint = {"id": secrets.token_urlsafe(12), "account": account, "to_addr": to_addr, "payer": payer,
                    "number": secrets.choice(free), "state": "held", "made": now, "changed": now}
            conn.execute("INSERT INTO mint (id, account, to_addr, payer, number, state, made, changed) "
                         "VALUES (:id, :account, :to_addr, :payer, :number, :state, :made, :changed)", mint)
        return mint

    def still_held(self, mint_id: str, now: float | None = None) -> None:
        """Asked just before the payment is broadcast: ValueError unless the piece is still this mint's."""
        with self.db() as conn:
            row = conn.execute("SELECT state, made FROM mint WHERE id = ?", (mint_id,)).fetchone()
        if row is None or row["state"] != "held" or (now or time.time()) - row["made"] > HOLD_SECONDS:
            raise ValueError("the piece held for you was let go: press Mint again")

    def paying(self, mint_id: str, txid: str, now: float | None = None) -> None:
        """The payment went out (the offer's `done`)."""
        now = now or time.time()
        with self.db() as conn:
            conn.execute("UPDATE mint SET state = 'paying', pay_txid = ?, changed = ? WHERE id = ? AND state = 'held'",
                         (txid, now, mint_id))

    def let_go(self, mint_id: str, why: str, now: float | None = None) -> None:
        with self.db() as conn:
            conn.execute("UPDATE mint SET state = 'let go', why = ?, changed = ? WHERE id = ? AND state IN "
                         "('held', 'paying')", (why, now or time.time(), mint_id))

    def mints_of(self, account: str) -> list[dict]:
        """An account's mints, newest first. A piece is named only once it is paid for."""
        with self.db() as conn:
            rows = conn.execute("SELECT m.*, p.name, p.iid FROM mint m JOIN piece p USING (number) WHERE m.account = ? "
                                "ORDER BY m.made DESC", (account.lower(),)).fetchall()
        out = []
        for r in rows:
            shown = r["state"] in ("paying", "paid", "sending", "sent")
            out.append({"id": r["id"], "state": r["state"], "why": r["why"], "made": r["made"], "to": r["to_addr"],
                        "pay_txid": r["pay_txid"] or "", "send_txid": r["send_txid"] or "",
                        "number": r["number"] if shown else None, "name": r["name"] if shown else "",
                        "iid": r["iid"] if shown else ""})
        return out

    def counts(self) -> dict:
        with self.db() as conn:
            total, inscribed = conn.execute("SELECT COUNT(*), COUNT(outpoint) FROM piece").fetchone()
            by = dict(conn.execute("SELECT state, COUNT(*) FROM mint GROUP BY state").fetchall())
        taken = sum(by.get(s, 0) for s in LIVE)
        return {"total": total, "inscribed": inscribed, "left": total - taken, "minted": by.get("sent", 0),
                "states": by}

    def evolve(self, now: float | None = None) -> dict:
        """When each stage after Bit can be reached: every `evolve_days` after the pad opened."""
        cfg = self.config()
        start, every = int(cfg["opened_at"] or 0), int(cfg["evolve_days"]) * DAY
        if not start:
            return {"every_days": cfg["evolve_days"], "stages": [], "next": None}
        now = now or time.time()
        stages = [{"stage": s, "from": start + k * every, "open": now >= start + k * every}
                  for k, s in enumerate(STAGES) if k]
        return {"every_days": cfg["evolve_days"], "stages": stages,
                "next": next((s for s in stages if not s["open"]), None)}

    # --- each tick ---------------------------------------------------------------------------------------------

    def _let_go_stale(self, conn, now: float) -> None:
        conn.execute("UPDATE mint SET state = 'let go', why = 'not paid in time', changed = ? WHERE state = 'held' "
                     "AND made < ?", (now, now - HOLD_SECONDS))

    def tick(self, env: Env, now: float | None = None) -> list[str]:
        """One round: let go of what was never paid, mark what was, then (one broadcast at a time, once the last is
        in the index) deliver a paid piece, or else inscribe the next. What happened, in words."""
        now = now or time.time()
        cfg = self.config()
        said = []
        with self.db() as conn:
            self._let_go_stale(conn, now)
            paying = conn.execute("SELECT id, pay_txid, changed FROM mint WHERE state = 'paying'").fetchall()
        for m in paying:
            got, cents = env.payment(m["pay_txid"])
            if got == "confirmed" and cents >= int(cfg["price_cents"]):
                self._move(m["id"], "paying", "paid", now)
                said.append(f"mint {m['id']} paid")
            elif got == "confirmed":
                self.let_go(m["id"], f"its payment paid the pad ${cents / 100:,.2f}, not "
                                     f"${int(cfg['price_cents']) / 100:,.2f}", now)
                said.append(f"mint {m['id']} underpaid")
            elif got == "missing" and now - m["changed"] > PAY_WAIT:
                self.let_go(m["id"], "its payment never reached a block", now)
                said.append(f"mint {m['id']} never paid")
        with self.db() as conn:
            sending = conn.execute("SELECT id, send_txid FROM mint WHERE state = 'sending'").fetchall()
        for m in sending:
            if env.settled(m["send_txid"]):
                self._move(m["id"], "sending", "sent", now)
                said.append(f"mint {m['id']} delivered")
        if not env.ready():
            return said
        with self.db() as conn:
            paid = conn.execute("SELECT m.id, m.to_addr, p.iid, p.outpoint, p.value FROM mint m JOIN piece p "
                                "USING (number) WHERE m.state = 'paid' ORDER BY m.changed LIMIT 1").fetchone()
        if paid is not None:
            txid = env.deliver(paid["iid"], paid["outpoint"], int(paid["value"]), paid["to_addr"])
            with self.db() as conn:
                conn.execute("UPDATE mint SET state = 'sending', send_txid = ?, changed = ? WHERE id = ? AND "
                             "state = 'paid'", (txid, now, paid["id"]))
            return said + [f"mint {paid['id']} sent in {txid}"]
        if cfg["inscribe"]:
            with self.db() as conn:
                nxt = conn.execute("SELECT number, ctype, content FROM piece WHERE iid IS NULL ORDER BY number "
                                   "LIMIT 1").fetchone()
            if nxt is not None:
                got = env.inscribe(nxt["ctype"], bytes(nxt["content"]))
                with self.db() as conn:
                    conn.execute("UPDATE piece SET iid = ?, outpoint = ?, value = ?, commit_tx = ? WHERE number = ?",
                                 (got["iid"], got["outpoint"], int(got["value"]), got.get("commit", ""),
                                  nxt["number"]))
                said.append(f"#{nxt['number']} inscribed as {got['iid']}")
        return said

    def _move(self, mint_id: str, was: str, now_state: str, now: float) -> None:
        with self.db() as conn:
            conn.execute("UPDATE mint SET state = ?, changed = ? WHERE id = ? AND state = ?",
                         (now_state, now, mint_id, was))

    def held_outpoints(self) -> set[tuple[str, int]]:
        """Every coin the pad holds a piece on: never spent for a fee."""
        with self.db() as conn:
            rows = conn.execute("SELECT p.outpoint FROM piece p WHERE p.outpoint IS NOT NULL AND p.number NOT IN "
                                "(SELECT number FROM mint WHERE state IN ('sending', 'sent'))").fetchall()
        out = set()
        for (op,) in rows:
            txid, _, vout = op.partition(":")
            out.add((txid, int(vout)))
        return out


def main(argv: list[str] | None = None) -> int:
    """`python -m arcade.ddpad <home> status | load <build> | set key=value... | open | close`."""
    import argparse
    ap = argparse.ArgumentParser(prog="python -m arcade.ddpad", description=main.__doc__)
    ap.add_argument("home", type=Path, help="the arcade's home folder (where trade.key lives)")
    sub = ap.add_subparsers(dest="what", required=True)
    sub.add_parser("status")
    one = sub.add_parser("load")
    one.add_argument("build", type=Path)
    many = sub.add_parser("set")
    many.add_argument("pairs", nargs="+", help="name=value, value as JSON (price_cents=100, inscribe=true)")
    sub.add_parser("open")
    sub.add_parser("close")
    args = ap.parse_args(argv)
    pad = Pad(args.home)
    if args.what == "load":
        print(f"{pad.load(args.build)} pieces filed")
    elif args.what == "set":
        values = {}
        for pair in args.pairs:
            k, _, v = pair.partition("=")
            try:
                values[k] = json.loads(v)
            except json.JSONDecodeError:
                values[k] = v
        pad.set(**values)
    elif args.what == "open":
        pad.open()
    elif args.what == "close":
        pad.set(open=False)
    key = PadKey(args.home)
    print(json.dumps({"config": pad.config(), "counts": pad.counts() if pad.path.exists() else {},
                      "pubkey": key.pubkey.hex() if key.path.exists() else "(made on first use)"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Inscriptions (Doginals on Dogecoin, and the same ord envelopes on Pepecoin) on the coins a wallet spends (the operator
2026-10-14: "identify which UTXOs are doginals to make sure they are not spent unless somebody is trying to send a doginal
to someone else").

Read from the chain itself, by this node (2026-10-15: remove the outside Doginals API; its index did not know Anti Doginal
Dog #1089, which sat on a 0.01 DOGE coin): each small output is traced back, koinu by koinu, to the transaction whose
input opened an ord envelope (`trace`), and an inscription's content is read out of that envelope (`content`). No index:
a few raw transactions per coin, through the chain's own node. Optionally an ord-style server answers as well
(ARCADE_DOGINALS=ord with ARCADE_ORD_URL, e.g. wonky-ord, asked per output).

A coin is kept OUT of an ordinary send when an inscription sits on it, or when it is small and could not be read. Every
answer is kept (an unspent coin's never changes): one reading per transaction, ever. Sending an inscription ON PURPOSE
spends its coin through doginal_send, not this filter.
"""

import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
from pathlib import Path

# 2026-10-14: "if at some point, we can spin up our own. Wonkyord we will do that." ARCADE_DOGINALS=ord with
# ARCADE_ORD_URL=<an ord-style server> adds its word to the chain's own reading. The default is the chain alone.
SOURCE = os.environ.get("ARCADE_DOGINALS", "chain")
ORD_URL = os.environ.get("ARCADE_ORD_URL", "")
POSTAGE_MAX = 1_000_000          # koinu (0.01): at or under this, a coin may be carrying an inscription
ASK_TIMEOUT = 90                 # an ord-style server, when one is set


def content_url(inscription: str) -> str:
    """Where a page shows an inscription's content from: this arcade's own /content/<id> (read from the chain by this
    node), or an ord-style server's when one is set (ARCADE_DOGINAL_CONTENT overrides, with {id} for the inscription)."""
    shape = os.environ.get("ARCADE_DOGINAL_CONTENT", "")
    if shape:
        return shape.replace("{id}", inscription)
    if SOURCE == "ord" and ORD_URL:
        return f"{ORD_URL.rstrip('/')}/content/{inscription}"
    return f"/content/{inscription}"


def frame_url(inscription: str) -> str:
    """What a card's frame shows: this arcade's /content/<id>/fit, which draws a picture scaled to the frame (2026-10-18,
    Doginal Dogs are 1000 px PNGs: framed as they are, a card showed only their top-left corner of background), and
    any other content as it is. An outside content server's URL is used unchanged."""
    url = content_url(inscription)
    return url + "/fit" if url.startswith("/content/") else url


class Unknown(Exception):
    """The chain (or the ord-style server) could not be read just now."""


class Doginals:
    """What sits on each output of a transaction, read from the chain (and an ord-style server, when set), remembered."""

    def __init__(self, path: Path, fetch=None, rpc_factory=None):
        self.path = Path(path)
        self.fetch = fetch or self._fetch
        # The Dogecoin node, when this arcade runs one: each small output is traced back to its inscription on the chain
        # itself (no index), 2026-10-15: Anti Doginal Dog #1089 on a 0.01 DOGE coin, which an outside index did not know.
        self.rpc_factory = rpc_factory
        self._lock = threading.Lock()
        with self._db() as db:
            # `tx2`: answers that include the chain's own reading. The old `tx` table held an outside index's word alone.
            db.execute("CREATE TABLE IF NOT EXISTS tx2 (txid TEXT PRIMARY KEY, held TEXT NOT NULL, at REAL NOT NULL)")
            # Where an unfinished walk back got to, per output: it goes on from here (trace's `resume`).
            db.execute("CREATE TABLE IF NOT EXISTS walk (txid TEXT NOT NULL, vout INTEGER NOT NULL, state TEXT NOT NULL, "
                       "at REAL NOT NULL, PRIMARY KEY (txid, vout))")

    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        return db

    @staticmethod
    def _get(url: str) -> dict:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=ASK_TIMEOUT) as r:
            return json.load(r)

    @classmethod
    def _fetch(cls, txid: str) -> dict:
        """An ord-style server's answer for a transaction, when one is set; else nothing -- the chain's own reading
        (`_add_traced`) is the answer."""
        if SOURCE == "ord" and ORD_URL:
            tx = cls._get(f"{ORD_URL.rstrip('/')}/tx/{txid}")
            outs = tx.get("transaction", tx).get("output") or tx.get("outputs") or []
            found = []
            for vout in range(len(outs)):
                got = cls._get(f"{ORD_URL.rstrip('/')}/output/{txid}:{vout}")
                for iid in got.get("inscriptions") or []:
                    found.append({"vout": vout, "inscription_id": iid, "sat_offset": None})
            return {"txid": txid, "inscriptions": found}
        return {"txid": txid, "inscriptions": []}

    def held(self, txid: str) -> dict[int, list[dict]]:
        """{vout: [{"id", "offset"}]} for one transaction; {} when nothing is inscribed on any of its outputs."""
        with self._lock, self._db() as db:
            row = db.execute("SELECT held, at FROM tx2 WHERE txid=?", (txid,)).fetchone()
        if row:
            kept = {int(k): v for k, v in json.loads(row[0]).items()}
            unsure = any(i.get("unknown") for on in kept.values() for i in on)
            if not unsure or time.time() - float(row[1]) < UNKNOWN_RETRY:
                return kept
        try:
            said = self.fetch(txid) or {}
        except Exception as exc:                            # noqa: BLE001
            raise Unknown(f"the inscription source did not answer for {txid}: {exc}") from exc
        if said.get("txid") not in (None, txid):
            raise Unknown(f"the inscription source answered about another transaction ({said.get('txid')})")
        held: dict[int, list[dict]] = {}
        for ins in said.get("inscriptions") or []:
            try:
                vout = int(ins["vout"])
            except (KeyError, TypeError, ValueError):
                continue
            held.setdefault(vout, []).append({"id": ins.get("inscription_id"), "offset": ins.get("sat_offset")})
        if self.rpc_factory is not None:
            self._add_traced(txid, held)
        elif self.fetch == self._fetch and not (SOURCE == "ord" and ORD_URL):
            raise Unknown("this node reads no chain for inscriptions, and no ord-style server is set")
        with self._lock, self._db() as db:
            db.execute("INSERT OR REPLACE INTO tx2 VALUES (?,?,?)", (txid, json.dumps(held), time.time()))
        return held

    def _add_traced(self, txid: str, held: dict[int, list[dict]]) -> None:
        """Add what the chain itself says sits on each small output of `txid` that nothing was named on yet: an
        inscription found by tracing the output's koinu back (`trace`), or -- when the trace could not finish -- a marker
        that keeps the coin out of every ordinary send without naming anything ({"id": None, "unknown": True})."""
        try:
            with self.rpc_factory() as rpc:
                tx = rpc.call("getrawtransaction", txid, 1)
                for out in tx.get("vout") or []:
                    n, value = int(out["n"]), round(float(out["value"]) * 1e8)
                    if held.get(n) or value > TRACE_MAX:
                        continue
                    with self._lock, self._db() as db:
                        row = db.execute("SELECT state FROM walk WHERE txid=? AND vout=?", (txid, n)).fetchone()
                    try:
                        found = trace(rpc, txid, n, tx=tx, resume=json.loads(row[0]) if row else None)
                    except TraceIncomplete as stop:
                        with self._lock, self._db() as db:
                            db.execute("INSERT OR REPLACE INTO walk VALUES (?,?,?,?)",
                                       (txid, n, json.dumps(stop.state), time.time()))
                        held[n] = [{"id": None, "offset": None, "unknown": True}]
                        continue
                    if row:
                        with self._lock, self._db() as db:
                            db.execute("DELETE FROM walk WHERE txid=? AND vout=?", (txid, n))
                    if found:
                        held[n] = found
        except Exception as exc:                            # noqa: BLE001 -- the node is the second opinion
            raise Unknown(f"the Dogecoin node could not trace {txid}: {exc}") from exc

    def content(self, inscription: str) -> tuple[str, bytes]:
        """An inscription's content from the chain (`content`), kept on disk for good beside the answers."""
        if self.rpc_factory is None:
            raise Unknown("this node reads no inscriptions from its chain")
        home = self.path.parent / "inscription-content"
        name = home / inscription
        if name.exists() and (home / (inscription + ".type")).exists():
            return (home / (inscription + ".type")).read_text(), name.read_bytes()
        # Two at a time (2026-10-17): a page of 60 uncached Doginals asked all at once, each walking blocks, filled
        # dogecoind's RPC work queue ("Work queue depth exceeded") and every Dogecoin call on the node failed with it.
        if not _READING.acquire(timeout=25):
            raise Unknown("the chain is busy reading other inscriptions; try again")
        try:
            with self.rpc_factory() as rpc:
                ctype, body = content(rpc, inscription)
        finally:
            _READING.release()
        home.mkdir(parents=True, exist_ok=True)
        (home / (inscription + ".type")).write_text(ctype)
        name.write_bytes(body)
        return ctype, body

    def kept(self, txid: str) -> dict[int, list[dict]] | None:
        """What is already known about a transaction, without asking anybody: None when it was never asked."""
        with self._lock, self._db() as db:
            row = db.execute("SELECT held FROM tx2 WHERE txid=?", (txid,)).fetchone()
        return {int(k): v for k, v in json.loads(row[0]).items()} if row else None

    def spendable_known(self, coins: list[dict]) -> list[dict]:
        """`spendable` from what is already known, reading nothing new:
        a coin known to carry a Doginal stays out; a small coin nobody vouched for stays out; anything else may go -- the
        rule `spendable` keeps while the chain cannot be read."""
        out = []
        for coin in coins:
            on = self.kept(coin["txid"])
            if on is None:
                if int(coin["value"]) > POSTAGE_MAX:
                    out.append(coin)
            elif not on.get(int(coin["vout"])):
                out.append(coin)
        return out

    def spendable(self, coins: list[dict]) -> list[dict]:
        """The coins an ordinary send may use: none carrying an inscription, and no small one that could not be read."""
        out = []
        for coin in coins:
            try:
                on = self.held(coin["txid"]).get(int(coin["vout"]))
            except Unknown:
                if int(coin["value"]) > POSTAGE_MAX:
                    out.append(coin)                        # a big coin while the chain cannot be read: not postage
                continue
            if not on:
                out.append(coin)
        return out


    def vouch_broadcast(self, raw_hex: str, txid: str) -> bool:
        """Remember a transaction this arcade just broadcast as carrying no Doginal, when that is
        certain without asking: every input spends a coin already known clean (its transaction is
        kept here and that output holds nothing), and no input script carries an inscription
        envelope ("ord"). Then nothing inscribed went in, so nothing inscribed came out -- above all
        the small dust outputs a token transaction makes, which would otherwise be read one by one
        like any small coin (2026-10-15: "If creating Omni assets
        creates a bunch of small dust UTXOs we don't want those to be saved like the rest of the
        small doginal UTXOs"). True if it was remembered."""
        try:
            inputs = _inputs_of(bytes.fromhex(raw_hex))
        except (ValueError, IndexError):
            return False
        if not inputs:
            return False
        with self._lock, self._db() as db:
            for prev, vout, script in inputs:
                if b"\x03ord" in script:
                    return False                        # a reveal: this transaction inscribes
                row = db.execute("SELECT held FROM tx2 WHERE txid=?", (prev,)).fetchone()
                if row is None or str(vout) in json.loads(row[0]):
                    return False                        # not known clean: leave it to the reading
            db.execute("INSERT OR IGNORE INTO tx2 VALUES (?,?,?)", (txid, "{}", time.time()))
        return True


def _inputs_of(raw: bytes) -> list[tuple[str, int, bytes]]:
    """(previous txid, vout, input script) for each input of a serialized (non-segwit) transaction."""
    def varint(at: int) -> tuple[int, int]:
        first = raw[at]
        if first < 0xFD:
            return first, at + 1
        size = {0xFD: 2, 0xFE: 4, 0xFF: 8}[first]
        return int.from_bytes(raw[at + 1:at + 1 + size], "little"), at + 1 + size
    count, at = varint(4)
    out = []
    for _ in range(count):
        prev = raw[at:at + 32][::-1].hex()
        vout = int.from_bytes(raw[at + 32:at + 36], "little")
        length, at = varint(at + 36)
        out.append((prev, vout, raw[at:at + length]))
        at += length + 4
    return out


# --- the chain's own reading ----------------------------------------------------------------------------------------

#: Outputs this small are traced (Doginals sit on postage: 0.001 DOGE usually, 0.01 often); the walk back also passes only
#: through outputs this small, since an inscription that went into a big coin went with that coin's owner's consent.
TRACE_MAX = 10_000_000          # koinu (0.1 DOGE)
#: How far back one output is followed before it is left as "cannot tell" (protected, never called clean).
# Steps a walk takes per ask. 2026-10-20: 40 was too few -- a Doginal Frog that had changed hands for years (#10000,
# sent to @dondogecon from a wallet off the arcade) took ~60 steps, its coin was filed "unknown" and the wallet never
# showed it -- and the operator: "Some doginals are going to be deeper". So there is no depth limit: a walk that runs out of
# steps keeps where it got to (`walk` table) and goes on from there the next time the coin is asked about.
TRACE_TXS = 600
#: A coin whose walk is not finished yet is asked about again after this long (each ask goes TRACE_TXS further).
UNKNOWN_RETRY = 60


class TraceIncomplete(Exception):
    """The walk back hit its limit before it could say. `state`: where it got to, for `trace(resume=...)`."""

    def __init__(self, message: str, state: dict | None = None):
        super().__init__(message)
        self.state = state or {}


def envelope(script_sig_hex: str) -> bool:
    """Does an input script open an ord envelope (a push of "ord" first)? The Doginals reveal: "ord", the part count,
    the content type, then (count, data) pairs down to 0, across as many transactions as it takes."""
    raw = bytes.fromhex(script_sig_hex or "")
    return raw[:4] == b"\x03ord"


def trace(rpc, txid: str, vout: int, tx: dict | None = None, limit: int = TRACE_TXS,
          resume: dict | None = None) -> list[dict]:
    """The inscriptions on output `vout` of `txid`, from the chain alone: each inscription is born on the first koinu of
    the input that carries its envelope, and koinu move first-in-first-out (ord's rule). The output's koinu are followed
    back, a range at a time, through outputs no bigger than TRACE_MAX; a transaction whose input opens an envelope is a
    birth. Returns [{"id": "<genesis txid>i0", "offset": n}] with `offset` the koinu inside this output; [] when the walk
    ends without one (a coinbase, or a coin too big to be postage). TraceIncomplete when it ran out of steps first."""
    txs: dict[str, dict] = {}

    def get(t):
        if t not in txs:
            txs[t] = tx if (tx is not None and t == txid) else rpc.call("getrawtransaction", t, 1)
        return txs[t]

    def value(o):
        return round(float(o["value"]) * 1e8)

    if resume and resume.get("queue"):
        found = [dict(f) for f in resume.get("found") or []]
        queue = [tuple(q) for q in resume["queue"]]
    else:
        first = get(txid)["vout"][vout]
        found, queue = [], [(txid, vout, 0, value(first), 0)]  # (tx, output, lo, hi, where that range starts in OUR output)
    seen = 0
    while queue:
        seen += 1
        if seen > limit:
            raise TraceIncomplete(f"more than {limit} steps back from {txid}:{vout} this time",
                                  {"queue": [list(q) for q in queue], "found": found})
        t, n, lo, hi, base = queue.pop()
        x = get(t)
        outs, ins = x["vout"], x["vin"]
        start = sum(value(o) for o in outs[:n])
        a, b = start + lo, start + hi                   # this range, as koinu of the transaction's inputs
        at = 0
        for i, v in enumerate(ins):
            if "coinbase" in v:
                at = None
                break
            prev = get(v["txid"])["vout"][v["vout"]]
            size = value(prev)
            if envelope(v.get("scriptSig", {}).get("hex", "")) and a <= at < b:
                found.append({"id": f"{t}i0", "offset": base + (at - a)})
            s, e = max(a, at), min(b, at + size)
            if s < e and size <= TRACE_MAX:
                queue.append((v["txid"], v["vout"], s - at, e - at, base + (s - a)))
            at += size
    return sorted(found, key=lambda f: f["offset"])


def _pushes(raw: bytes) -> list:
    """The pushes of an input script, in order: bytes for data (a small-number opcode as its one byte), None otherwise."""
    at, out = 0, []
    while at < len(raw):
        op = raw[at]
        at += 1
        if op == 0:
            out.append(b"")
            continue
        if 0x51 <= op <= 0x60:
            out.append(bytes([op - 0x50]))
            continue
        if 1 <= op <= 75:
            n = op
        elif op == 0x4C:
            n, at = raw[at], at + 1
        elif op == 0x4D:
            n, at = int.from_bytes(raw[at:at + 2], "little"), at + 2
        elif op == 0x4E:
            n, at = int.from_bytes(raw[at:at + 4], "little"), at + 4
        else:
            out.append(None)
            continue
        out.append(raw[at:at + n])
        at += n
    return out


#: Chain reads of inscription content at once, across every page (see Doginals.content).
_READING = threading.BoundedSemaphore(2)


def content(rpc, inscription: str, look: int = 30) -> tuple[str, bytes]:
    """(content type, bytes) of an ord inscription, read from the chain: the genesis transaction's envelope ("ord", the
    part count, the content type, then (count, data) pairs down to 0), continued -- when the parts run on -- in the
    transaction that spends output 0 next, found in the blocks after it (no index). A part count is a push of at most
    three bytes; the signature and redeem script that close each transaction's input end its parts."""
    if not INSCRIPTION_ID.match(inscription):
        raise ValueError("that is not an inscription id")
    txid = inscription.split("i")[0]
    tx = rpc.call("getrawtransaction", txid, 1)
    pushes = _pushes(bytes.fromhex(tx["vin"][0]["scriptSig"]["hex"]))
    if not pushes or pushes[0] != b"ord" or len(pushes) < 3 or pushes[2] is None:
        raise ValueError("no ord envelope in that transaction")
    ctype, body, data, here = pushes[2].decode("utf-8", "replace"), b"", pushes[3:], tx
    height = rpc.call("getblockheader", tx["blockhash"])["height"] if tx.get("blockhash") else None
    while True:
        i = 0
        while i + 1 < len(data) and data[i] is not None and len(data[i]) <= 3 and data[i + 1] is not None:
            count = int.from_bytes(data[i], "little")
            body += data[i + 1]
            i += 2
            if count == 0:
                return ctype, body
        if height is None:
            raise ValueError("the inscription runs on past a transaction that is not in a block yet")
        here, height = _spender(rpc, here["txid"], height, look)
        if here is None:
            raise ValueError("could not find the rest of that inscription")
        data = _pushes(bytes.fromhex(here["vin"][0]["scriptSig"]["hex"]))


def _spender(rpc, txid: str, height: int, look: int):
    """The transaction spending `txid`:0, searched block by block from `height`, and the height it was found at.

    Each block is fetched raw and searched for the outpoint's own 36 bytes; only the block that holds them is decoded
    (2026-10-17: decoding every transaction of every block walked -- one call each -- took 90 s for one Doginal Dog
    and held all of dogecoind's RPC workers, so a wallet took 17 s to unlock)."""
    outpoint = bytes.fromhex(txid)[::-1].hex() + "00000000"
    for h in range(height, height + look):
        block_hash = rpc.call("getblockhash", h)
        if outpoint not in str(rpc.call("getblock", block_hash, 0)):
            continue
        block = rpc.call("getblock", block_hash, 2)
        for t in block.get("tx") or []:
            if not isinstance(t, dict):                        # a node whose getblock takes a bool: txids only
                t = rpc.call("getrawtransaction", t, 1)
            if any(v.get("txid") == txid and v.get("vout") == 0 for v in t.get("vin") or []):
                return t, h
    return None, None


#: An inscription id as ord-dogecoin writes it: the reveal (genesis) transaction's id, "i", its index.
INSCRIPTION_ID = re.compile(r"^[0-9a-f]{64}i[0-9]{1,6}$")


def exactly(on: list[dict] | None, inscription: str) -> str:
    """"" when what sits on a coin (`held(txid)[vout]`) is exactly `inscription`, at the coin's first koinu;
    else why not. A coin with two inscriptions, or one sitting further in, would need an alignment output first, and moving it
    plainly could hand the wrong piece away or burn it to fees ([Claude 2026-10-15] shared by the plain send and the swap lock)."""
    on = on or []
    if [i.get("id") for i in on] != [inscription]:
        return "that coin does not hold exactly that Doginal (the chain says: %s)" % (
            ", ".join(str(i.get("id")) for i in on) or "nothing")
    if int(on[0].get("offset") or 0) != 0:
        return "that Doginal does not sit at the start of its coin: it needs an alignment output first"
    return ""

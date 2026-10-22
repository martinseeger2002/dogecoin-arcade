"""Each account's imported wallets, carried to every arcade that holds the account (2026-10-21: "an imported
wallet needs to be automatically broadcast to other arcades that you are signed in with. So all of your wallet
balances persist over all arcades that you sign into").

What travels is the account's sealed record (accounts.ImportedWallets): ciphertext no arcade can open, a version
number, and the account's login-key signature over both (wallet_imports.record_digest). A receiving arcade takes a
version only when it holds the account itself (its vault has the pubkey), the signature checks against THAT pubkey --
never one a frame names -- the blob's hash matches, and the version is newer than its own. So a linked arcade can
neither forge, alter nor roll back anybody's record; at worst it can say nothing.

On the wire (frames of kind "iw" on an ordinary link, never flooded as such; an arcade that takes a newer version
tells its other links in turn, and a version is taken once, so it spreads and stops):

    ls  {rows: [[h, v], ...]}         when a link comes up: every account with a signed record here, as
                                       h = wallet_imports.account_hash(pubkey) and its version v
    an  {h, v, d}                      a version was saved or taken here: account h, version v, sha256 of its blob d
    gt  {h, v}                         the asker holds account h at an older version: send the record
    rc  {h, v, d, sig, i, n} + slice   the answer, in n parts of the blob (a link frame is at most 64 KB)
    no  {h}                            not here (any more)

Accounts are named by a hash of the pubkey, so a list says nothing to an arcade that does not hold that account: it
can only match the accounts it has. Only accounts with a record are listed, at most LIST_MAX.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Any

log = logging.getLogger("arcade.mesh")

PART = 48 * 1024            # a slice of the blob in one frame: a link frame is at most 64 KB with its header
ROWS_PER_FRAME = 400        # an "ls" frame stays well under a frame
LIST_MAX = 20000            # rows one arcade lists, or reads from one "ls" round
ASK_SECONDS = 60.0          # a question unanswered this long is forgotten (asked again at the next announce/link)
ASKS_OPEN = 16              # questions to one arcade at once
HEARD_PER_MINUTE = LIST_MAX + 1000   # rows ("ls", "an") one linked arcade may have read in a minute: a whole list
LATER_MAX = 5000            # questions waiting for a free place (ASKS_OPEN) across all links
ANSWERS_PER_MINUTE = 60     # records one linked arcade may be sent in a minute
MAX_BLOB = 64 * 1024        # accounts.IMPORTED_MAX


class StateRecords:
    """The node's side, as the extension uses it (every call from a worker thread)."""

    def __init__(self, state: Any):
        self.state = state
        self._held: tuple[float, dict[str, str]] = (0.0, {})

    def _store(self):
        return self.state.imported_wallets()

    def held(self) -> dict[str, str]:
        """account_hash -> pubkey for every account this arcade's vault holds (kept a minute)."""
        from ..wallet_imports import account_hash
        at, kept = self._held
        if time.monotonic() - at < 60:
            return kept
        rows = self.state.vault().conn.execute("SELECT DISTINCT lower(pubkey) FROM vault").fetchall()
        kept = {account_hash(r[0]): r[0] for r in rows if r[0]}
        self._held = (time.monotonic(), kept)
        return kept

    def forget_held(self) -> None:
        self._held = (0.0, {})

    def listed(self) -> list[tuple[str, int]]:
        from ..wallet_imports import account_hash
        held = set(self.held().values())
        return [(account_hash(pk), v) for pk, v in self._store().signed() if pk in held][:LIST_MAX]

    def version(self, pubkey: str) -> int:
        have = self._store().get(pubkey)
        return int(have["version"]) if have else 0

    def record(self, pubkey: str) -> dict | None:
        have = self._store().get(pubkey)
        return have if have and have.get("signed") else None

    def adopt(self, pubkey: str, blob: str, version: int, sig: str) -> str:
        said = self._store().adopt(pubkey, blob, version, sig)
        if said == "taken":
            try:
                self.state.bump_generation()
            except Exception:                              # noqa: BLE001
                pass
        return said


class ImportShare:
    kinds = ("iw",)

    def __init__(self, records: Any):
        self.records = records
        self.node = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._asking: dict[tuple[str, str], dict] = {}     # (peer, h) -> {v, at, parts, n, d, sig}
        self._heard: dict[str, list[float]] = {}           # peer -> times rows were read (ls/an), for the limit
        self._sent: dict[str, list[float]] = {}            # peer -> times records were sent
        self._later: dict[tuple[str, str], int] = {}       # (peer, h) -> v: to ask once a place is free
        self.taken = 0                                     # versions taken from linked arcades (for Admin/tests)
        self.refused = 0                                   # versions refused (bad signature, hash, older)

    # ---------------------------------------------------------------- hooks
    async def link_up(self, link) -> None:
        self.loop = asyncio.get_running_loop()
        try:
            rows = await asyncio.to_thread(self.records.listed)
            for i in range(0, len(rows), ROWS_PER_FRAME):
                await link.send({"t": "iw", "op": "ls", "rows": [list(r) for r in rows[i:i + ROWS_PER_FRAME]]})
        except Exception:                                  # noqa: BLE001 -- the link went; listed again next time
            log.debug("iw link_up", exc_info=True)

    async def link_down(self, peer_id: str) -> None:
        for book in (self._asking, self._later):
            for key in [k for k in book if k[0] == peer_id]:
                book.pop(key, None)

    async def rebalance(self) -> None:
        self.loop = asyncio.get_running_loop()
        now = time.time()
        for key in [k for k, a in self._asking.items() if now - a["at"] > ASK_SECONDS]:
            self._asking.pop(key, None)
        for peer in {k[0] for k in self._later}:
            await self._ask_later(peer)

    async def on_frame(self, link, header: dict, blob: bytes) -> None:
        self.loop = self.loop or asyncio.get_running_loop()
        op, peer = header.get("op"), link.peer_id
        try:
            if op == "ls":
                rows = header.get("rows") if isinstance(header.get("rows"), list) else []
                await self._heard_rows(link, rows[:ROWS_PER_FRAME])
            elif op == "an":
                await self._heard_rows(link, [[header.get("h"), header.get("v")]])
            elif op == "gt":
                await self._answer(link, str(header.get("h") or ""))
            elif op == "rc":
                await self._part(link, header, blob)
            elif op == "no":
                self._asking.pop((peer, str(header.get("h") or "")), None)
        except Exception:                                  # noqa: BLE001
            log.debug("iw frame", exc_info=True)

    # ---------------------------------------------------------------- hearing what others hold
    def _allowed(self, book: dict, peer: str, limit: int, n: int = 1) -> bool:
        now = time.time()
        seen = [t for t in book.get(peer, []) if now - t < 60]
        if len(seen) + n > limit:
            book[peer] = seen
            return False
        book[peer] = seen + [now] * n
        return True

    async def _heard_rows(self, link, rows: list) -> None:
        peer = link.peer_id
        if not self._allowed(self._heard, peer, HEARD_PER_MINUTE, max(1, len(rows))):
            log.info("imported wallets: %s said too much this minute; not read", peer[:12])
            return
        held = await asyncio.to_thread(self.records.held)
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) != 2:
                continue
            h, v = row
            if not isinstance(h, str) or not isinstance(v, int) or isinstance(v, bool) or v < 1:
                continue
            pubkey = held.get(h)
            if pubkey is None:
                continue                                   # an account this arcade does not hold: nothing to do
            if (peer, h) in self._asking:
                continue
            mine = await asyncio.to_thread(self.records.version, pubkey)
            if v <= mine:
                continue
            if sum(1 for k in self._asking if k[0] == peer) >= ASKS_OPEN:
                if len(self._later) < LATER_MAX:
                    self._later[(peer, h)] = v
                continue
            await self._ask(link, h, v)

    async def _ask(self, link, h: str, v: int) -> None:
        self._asking[(link.peer_id, h)] = {"v": v, "at": time.time(), "parts": {}, "n": None, "d": None, "sig": None}
        await link.send({"t": "iw", "op": "gt", "h": h, "v": v})

    async def _ask_later(self, peer: str) -> None:
        link = (self.node.links if self.node else {}).get(peer)
        if link is None:
            return
        for (p, h), v in list(self._later.items()):
            if p != peer or (p, h) in self._asking:
                continue
            if sum(1 for k in self._asking if k[0] == peer) >= ASKS_OPEN:
                return
            self._later.pop((p, h), None)
            await self._ask(link, h, v)

    # ---------------------------------------------------------------- answering
    async def _answer(self, link, h: str) -> None:
        held = await asyncio.to_thread(self.records.held)
        pubkey = held.get(h)
        have = await asyncio.to_thread(self.records.record, pubkey) if pubkey else None
        if have is None:
            await link.send({"t": "iw", "op": "no", "h": h})
            return
        if not self._allowed(self._sent, link.peer_id, ANSWERS_PER_MINUTE):
            return                                          # asked again later
        raw = have["blob"].encode()
        digest = hashlib.sha256(raw).hexdigest()
        n = max(1, -(-len(raw) // PART))
        for i in range(n):
            await link.send({"t": "iw", "op": "rc", "h": h, "v": int(have["version"]), "d": digest,
                             "sig": have["sig"], "i": i, "n": n}, raw[i * PART:(i + 1) * PART])

    async def _part(self, link, header: dict, blob: bytes) -> None:
        peer, h = link.peer_id, str(header.get("h") or "")
        asked = self._asking.get((peer, h))
        if asked is None:
            return                                          # nobody asked: not read
        try:
            v, i, n = int(header["v"]), int(header["i"]), int(header["n"])
            d, sig = str(header["d"]), str(header["sig"])
        except (KeyError, TypeError, ValueError):
            self._asking.pop((peer, h), None)
            return
        if asked["n"] is None:
            asked.update(n=n, d=d, sig=sig, rv=v)
        if (not 1 <= n <= -(-MAX_BLOB // PART) or not 0 <= i < n or (n, d, sig, v) != (asked["n"], asked["d"],
                                                                                          asked["sig"], asked["rv"])):
            self._asking.pop((peer, h), None)
            self.refused += 1
            return
        asked["parts"][i] = bytes(blob)
        if sum(len(p) for p in asked["parts"].values()) > MAX_BLOB:
            self._asking.pop((peer, h), None)
            self.refused += 1
            return
        if len(asked["parts"]) < n:
            return
        self._asking.pop((peer, h), None)
        await self._ask_later(peer)
        raw = b"".join(asked["parts"][k] for k in range(n))
        if hashlib.sha256(raw).hexdigest() != d:
            log.info("imported wallets: %s sent a record that is not what it named; not taken", peer[:12])
            self.refused += 1
            return
        held = await asyncio.to_thread(self.records.held)
        pubkey = held.get(h)                                # this arcade's own vault's pubkey, never the frame's
        if pubkey is None:
            return
        try:
            said = await asyncio.to_thread(self.records.adopt, pubkey, raw.decode(), v, sig)
        except Exception as exc:                            # noqa: BLE001 -- not signed by the account, not sealed
            log.info("imported wallets: a record from %s was refused: %s", peer[:12], exc)
            self.refused += 1
            return
        if said == "taken":
            self.taken += 1
            log.info("imported wallets: account %s's version %d taken from arcade %s", pubkey[:12], v, peer[:12])
            await self._say_all({"op": "an", "h": h, "v": v, "d": d}, but=peer)
        elif said == "older":
            self.refused += 1

    # ---------------------------------------------------------------- telling
    def announce(self, pubkey: str, version: int, digest: str) -> None:
        """From a web thread: a version was saved here (or taken by the bring route): every linked arcade is told."""
        from ..wallet_imports import account_hash
        if self.loop is None or not self.node:
            return
        frame = {"op": "an", "h": account_hash(pubkey), "v": int(version), "d": str(digest)}
        try:
            asyncio.run_coroutine_threadsafe(self._say_all(frame), self.loop)
        except RuntimeError:                               # the loop is closing
            pass

    async def _say_all(self, frame: dict, but: str | None = None) -> None:
        for peer, link in list((self.node.links if self.node else {}).items()):
            if peer == but:
                continue
            try:
                await link.send({"t": "iw", **frame})
            except Exception:                              # noqa: BLE001
                pass

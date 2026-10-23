"""LLM service between arcades over the mesh (arcade/llm.py; 2026-10-23: apps and players on every arcade buy,
from "a shared 'LLM service' book").

On the wire (frames of kind "lm" on an ordinary link, never flooded):

    offer {o}                         this arcade's offer (llm.Seller.offer), or null: it sells nothing now
    q     {id, what, seq, end} + a slice of the request's JSON (zlib)
                                      a buyer's arcade asks, for one of its accounts: what = chat | topup | balance; the
                                      JSON carries the account's grant {address, expires, sig} and the body
    a     {id, seq, end[, err]} + one event's JSON
                                      the answer: for a chat, each OpenAI chunk as it comes and {"arcade": ...} last

A seller serves an address only with a grant that address's own key signed for THIS arcade (llm.grant_words, checked
from the bytes): the buyer's arcade carries it but cannot make one, and a grant made for another arcade is refused.
What a top-up is worth is read from the chain by the seller itself (llm.Seller.topup). An offer is the selling arcade's
own word; what it says of its operator is shown, never trusted.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import queue
import threading
import time
import zlib
from typing import Any, Callable, Iterator

from .. import llm as llmlib

log = logging.getLogger("arcade.mesh")

CHUNK = 48 * 1024
MAX_ASKED = 6 * 1024 * 1024
AT_ONCE_PER_PEER = 8
ANSWER_SECONDS = llmlib.REQUEST_SECONDS + 30
LOAD_EVERY = 15.0


def grant_ok(grant: Any, seller: str, now: float | None = None) -> str:
    """The address a grant proves, or LlmError."""
    if not isinstance(grant, dict):
        raise llmlib.LlmError("no grant")
    address, sig = str(grant.get("address") or ""), str(grant.get("sig") or "")
    try:
        expires = int(grant.get("expires") or 0)
    except (TypeError, ValueError):
        expires = 0
    now = now or time.time()
    if expires < now:
        raise llmlib.LlmError("that grant has run out; sign again")
    if expires > now + llmlib.GRANT_DAYS_MOST * 86400 + 600:
        raise llmlib.LlmError("a grant may last 90 days at most")
    if not llmlib.signed_by(sig, llmlib.grant_words(seller, address, expires), address):
        raise llmlib.LlmError("that grant's signature does not check")
    return address


class LlmShare:
    kinds = ("lm",)

    def __init__(self, seller: Callable[[], Any]):
        self.seller = seller                         # this arcade's llm.Seller, or None
        self.node = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.offers: dict[str, dict] = {}            # peer -> its offer, as it said it
        self.heard: dict[str, float] = {}            # peer -> when its offer was last said
        self._said: Any = None
        self._ids = itertools.count(1)
        self._waiting: dict[int, queue.Queue] = {}   # request id -> where its answer's events go
        self._asked: dict[tuple[str, int], list] = {}
        self._busy: dict[str, int] = {}
        self._pieces: dict[int, bytearray] = {}      # request id -> an event arriving in pieces

    # ---------------------------------------------------------------- hooks
    def _offer_now(self) -> dict | None:
        s = self.seller()
        try:
            return s.offer() if s is not None else None
        except Exception:                            # noqa: BLE001
            log.debug("llm offer", exc_info=True)
            return None

    async def link_up(self, link) -> None:
        self.loop = asyncio.get_running_loop()
        self._said = await asyncio.to_thread(self._offer_now)
        await self._send(link, {"op": "offer", "o": self._said})
        if getattr(self, "_ticker", None) is None and self.node is not None:
            self._ticker = self.node._spawn(self._tick())

    async def _tick(self) -> None:
        """The offer said again whenever it changed, at most every LOAD_EVERY seconds: its load (how busy the model
        is) moves far more often than the minute's rebalance."""
        while True:
            await asyncio.sleep(LOAD_EVERY)
            try:
                await self.rebalance()
            except Exception:                        # noqa: BLE001
                log.debug("llm tick", exc_info=True)

    async def link_down(self, peer_id: str) -> None:
        self.offers.pop(peer_id, None)
        self._busy.pop(peer_id, None)
        for key in [k for k in self._asked if k[0] == peer_id]:
            self._asked.pop(key, None)

    async def rebalance(self) -> None:
        """Every minute: say the offer again when it changed."""
        self.loop = asyncio.get_running_loop()
        now = await asyncio.to_thread(self._offer_now)
        if now != self._said:
            self._said = now
            for link in list((self.node.links if self.node else {}).values()):
                await self._send(link, {"op": "offer", "o": now})

    def say_again(self) -> None:
        """From a web thread, after the admin panel saved: say the offer now rather than within the minute."""
        if self.loop is not None:
            asyncio.run_coroutine_threadsafe(self.rebalance(), self.loop)

    async def on_frame(self, link, header: dict, blob: bytes) -> None:
        op, peer = header.get("op"), link.peer_id
        try:
            if op == "offer":
                o = header.get("o")
                if isinstance(o, dict) and isinstance(o.get("prices"), list) and isinstance(o.get("models"), list):
                    self.offers[peer] = _clean_offer(o)
                    self.heard[peer] = time.time()
                else:
                    self.offers.pop(peer, None)
            elif op == "q":
                await self._on_request(link, header, blob)
            elif op == "a":
                self._on_answer(header, blob)
        except Exception:                            # noqa: BLE001
            log.debug("llm frame", exc_info=True)

    # ---------------------------------------------------------------- the seller's side
    async def _on_request(self, link, header: dict, blob: bytes) -> None:
        rid, peer = header.get("id"), link.peer_id
        if not isinstance(rid, int):
            return
        key = (peer, rid)
        parts = self._asked.setdefault(key, [])
        parts.append(blob)
        if sum(map(len, parts)) > MAX_ASKED:
            self._asked.pop(key, None)
            await self._answer(link, rid, None, err="that request is too large")
            return
        if not header.get("end", True):
            return
        data = b"".join(self._asked.pop(key))
        if self._busy.get(peer, 0) >= AT_ONCE_PER_PEER:
            await self._answer(link, rid, None, err="busy")
            return
        try:
            asked = json.loads(zlib.decompress(data).decode())
        except Exception:                            # noqa: BLE001
            await self._answer(link, rid, None, err="not a request")
            return
        self._busy[peer] = self._busy.get(peer, 0) + 1
        self.node._spawn(self._serve(link, rid, str(header.get("what") or ""), asked))

    async def _serve(self, link, rid: int, what: str, asked: dict) -> None:
        peer = link.peer_id
        loop = asyncio.get_running_loop()
        events: asyncio.Queue = asyncio.Queue()

        def work():
            try:
                seller = self.seller()
                if seller is None:
                    raise llmlib.LlmError("this arcade sells no model")
                address = grant_ok(asked.get("grant"), self.node.node_id)
                body = asked.get("body") if isinstance(asked.get("body"), dict) else {}
                if what == "chat":
                    for ev in seller.chat(address, str(asked.get("leg") or ""), body, via=peer):
                        loop.call_soon_threadsafe(events.put_nowait, ("ev", ev))
                elif what == "topup":
                    loop.call_soon_threadsafe(events.put_nowait, ("ev", seller.topup(
                        address, str(body.get("leg") or ""), str(body.get("txid") or ""), str(body.get("sig") or ""))))
                elif what == "image":
                    if getattr(seller, "images", None) is None:
                        raise llmlib.LlmError("this arcade shares no pictures")
                    loop.call_soon_threadsafe(events.put_nowait, ("ev", seller.images.make(
                        address, str(asked.get("leg") or ""), body, via=peer)))
                elif what == "balance":
                    loop.call_soon_threadsafe(events.put_nowait, ("ev", seller.balance(address)))
                else:
                    raise llmlib.LlmError("not something an LLM seller answers")
            except llmlib.LlmError as exc:
                loop.call_soon_threadsafe(events.put_nowait, ("err", str(exc)))
            except Exception as exc:                 # noqa: BLE001
                log.debug("llm serve", exc_info=True)
                loop.call_soon_threadsafe(events.put_nowait, ("err", f"the seller failed: {exc}"[:300]))
            finally:
                loop.call_soon_threadsafe(events.put_nowait, ("end", None))

        threading.Thread(target=work, name="llm-serve", daemon=True).start()
        seq = 0
        try:
            while True:
                kind, ev = await events.get()
                if kind == "end":
                    break
                if kind == "err":
                    await self._answer(link, rid, None, err=ev, seq=seq)
                    return
                await self._answer(link, rid, ev, seq=seq, end=False)
                seq += 1
            await self._answer(link, rid, None, seq=seq)
        finally:
            self._busy[peer] = max(0, self._busy.get(peer, 1) - 1)

    async def _answer(self, link, rid: int, ev: Any, *, err: str | None = None, seq: int = 0,
                      end: bool = True) -> None:
        head = {"op": "a", "id": rid, "seq": seq, "end": end, **({"err": err[:300]} if err else {})}
        blob = json.dumps(ev, separators=(",", ":")).encode() if ev is not None else b""
        # A picture is far more than a frame holds: its event goes in pieces, each but the last marked `more`.
        while len(blob) > CHUNK:
            await self._send(link, {"op": "a", "id": rid, "seq": seq, "end": False, "more": True}, blob[:CHUNK])
            blob = blob[CHUNK:]
        await self._send(link, head, blob)

    # ---------------------------------------------------------------- the buyer's side
    def _on_answer(self, header: dict, blob: bytes) -> None:
        rid = header.get("id")
        q = self._waiting.get(rid)
        if q is None:
            return
        if header.get("more"):
            part = self._pieces.setdefault(rid, bytearray())
            part += blob
            if len(part) > MAX_ASKED * 4:
                self._pieces.pop(rid, None)
                q.put(("err", "that answer is too large"))
            return
        if rid in self._pieces:
            blob = bytes(self._pieces.pop(rid)) + blob
        if blob:
            try:
                q.put(("ev", json.loads(blob.decode())))
            except ValueError:
                pass
        if header.get("err"):
            q.put(("err", str(header["err"])))
        if header.get("end", True):
            q.put(("end", None))

    def ask(self, seller: str, what: str, asked: dict) -> Iterator[dict]:
        """From a web thread: ask the arcade `seller` (its mesh id); its events as they come, or LlmError."""
        if self.loop is None or self.node is None:
            raise llmlib.LlmError("the mesh is not running")
        link = self.node.links.get(seller)
        if link is None:
            raise llmlib.LlmError("that seller's arcade is not linked to this one right now")
        rid = next(self._ids)
        q: queue.Queue = queue.Queue()
        self._waiting[rid] = q
        data = zlib.compress(json.dumps(asked, separators=(",", ":")).encode(), 6)
        parts = [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)] or [b""]

        async def send():
            for n, part in enumerate(parts):
                await self._send(link, {"op": "q", "id": rid, "what": what, "seq": n, "end": n == len(parts) - 1},
                                 part)
        try:
            asyncio.run_coroutine_threadsafe(send(), self.loop).result(30)
            while True:
                try:
                    kind, ev = q.get(timeout=ANSWER_SECONDS)
                except queue.Empty:
                    raise llmlib.LlmError("the seller did not answer") from None
                if kind == "end":
                    return
                if kind == "err":
                    raise llmlib.LlmError(ev)
                yield ev
        finally:
            self._waiting.pop(rid, None)
            self._pieces.pop(rid, None)

    # ---------------------------------------------------------------- sending
    async def _send(self, link, frame: dict, blob: bytes = b"") -> None:
        try:
            await link.send({"t": "lm", **frame}, blob) if blob else await link.send({"t": "lm", **frame})
        except Exception:                            # noqa: BLE001
            pass


def _clean_offer(o: dict) -> dict:
    """Another arcade's offer, kept to the shape and sizes the book draws."""
    prices = []
    for p in o.get("prices")[:40]:
        if not isinstance(p, dict):
            continue
        try:
            llmlib.leg_parts(str(p.get("leg") or ""))
            prices.append({"leg": str(p["leg"]), "label": str(p.get("label") or p["leg"])[:80],
                           "in": format(llmlib.price(p.get("in", 0)), "f"),
                           "out": format(llmlib.price(p.get("out", 0)), "f"), "to": str(p.get("to") or "")[:100]})
        except (llmlib.LlmError, KeyError):
            continue
    models = [{"id": str(m.get("id"))[:200]} for m in o.get("models")[:20] if isinstance(m, dict) and m.get("id")]
    load = o.get("load") if isinstance(o.get("load"), dict) else {}
    keep = {}
    for k in ("active", "waiting", "at_once", "last_15m", "last_hour", "failed_hour", "first_ms"):
        try:
            keep[k] = max(0, int(load.get(k) or 0))
        except (TypeError, ValueError):
            pass
    try:
        keep["tok_s"] = max(0.0, float(load.get("tok_s") or 0))
    except (TypeError, ValueError):
        pass
    if load.get("word") in ("idle", "steady", "busy", "full"):
        keep["word"] = load["word"]
    pics = _clean_images(o.get("images")) if isinstance(o.get("images"), dict) else None
    return {**({"images": pics} if pics else {}),
            "name": str(o.get("name") or "")[:60], "models": models, "prices": prices, "free": o.get("free") is True,
            "load": keep,
            "max_out": int(o.get("max_out") or 0) if str(o.get("max_out") or "0").isdigit() else 0,
            "tag": str(o.get("tag") or "")[:24], "host": str(o.get("host") or "")[:100]}


def _clean_images(p: dict) -> dict | None:
    """The pictures part of another arcade's offer, kept to the shape the book draws."""
    prices = []
    for x in (p.get("prices") or [])[:40]:
        if not isinstance(x, dict):
            continue
        try:
            llmlib.leg_parts(str(x.get("leg") or ""))
            prices.append({"leg": str(x["leg"]), "label": str(x.get("label") or x["leg"])[:80],
                           "per": format(llmlib.price(x.get("per", 0)), "f"), "to": str(x.get("to") or "")[:100]})
        except (llmlib.LlmError, KeyError):
            continue
    models = [{"id": str(m.get("id"))[:200], "label": str(m.get("label") or m.get("id"))[:80]}
              for m in (p.get("models") or [])[:10] if isinstance(m, dict) and m.get("id")]
    if not models:
        return None
    load = p.get("load") if isinstance(p.get("load"), dict) else {}
    keep = {}
    for k in ("active", "waiting", "at_once", "last_15m", "last_hour", "failed_hour"):
        try:
            keep[k] = max(0, int(load.get(k) or 0))
        except (TypeError, ValueError):
            pass
    try:
        keep["avg_s"] = max(0.0, float(load.get("avg_s") or 0))
    except (TypeError, ValueError):
        pass
    if load.get("word") in ("idle", "steady", "busy", "full"):
        keep["word"] = load["word"]
    try:
        side = max(256, min(1536, int(p.get("max_side") or 1024)))
    except (TypeError, ValueError):
        side = 1024
    return {"models": models, "prices": prices, "free": p.get("free") is True, "max_side": side,
            "edits": p.get("edits") is True, "load": keep}

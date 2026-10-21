"""A new arcade's operator name, from the fruit stand over the mesh (arcade/fruit.py; 2026-10-20: "Whenever
someone spins up a new arcade, it should automatically choose a fruit name for its operator account").

On the wire (frames of kind "fr" on an ordinary link, never flooded):

    want  {}                       an arcade with no operator yet asks for a fruit
    offer {name}                   a holder of fruit names promises it one (the same one if it asks again)
    give  {name, to}               its operator signed up with that name: send it to this address
    given {name, txid}             sent
    no    {why}                    not sent, and why

A holder answers only for what it promised that same arcade (the link proves who is asking). The arcade keeps the
offer in its `fruit_offer` setting; the sign-up page reads it (web/app.py).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

log = logging.getLogger("arcade.mesh")

SETTING = "fruit_offer"


class FruitShare:
    kinds = ("fr",)

    def __init__(self, state: Any, stand: Callable[[], Any]):
        self.state = state
        self.stand = stand                       # this arcade's FruitStand, or None when it holds no fruit names
        self.node = None
        self.loop: asyncio.AbstractEventLoop | None = None

    # ---------------------------------------------------------------- what this arcade wants
    def _holder(self) -> bool:
        return bool(self.state.setting("fruit_stand"))

    def wants(self) -> bool:
        """No operator, no fruit offered yet, and no fruit names of its own."""
        return not self.state.operator and not self.state.setting(SETTING) and not self._holder()

    def offer(self) -> dict:
        return dict(self.state.setting(SETTING) or {})

    # ---------------------------------------------------------------- hooks
    async def link_up(self, link) -> None:
        self.loop = asyncio.get_running_loop()
        if await asyncio.to_thread(self.wants):
            await self._send(link, {"op": "want"})

    async def link_down(self, peer_id: str) -> None:
        pass

    async def rebalance(self) -> None:
        self.loop = asyncio.get_running_loop()
        if await asyncio.to_thread(self.wants):
            await self._send_all({"op": "want"})
            return
        offer = await asyncio.to_thread(self.offer)
        if offer.get("to") and not offer.get("txid"):             # asked to give, no answer yet: ask again
            await self._send_to(offer.get("from"), {"op": "give", "name": offer["name"], "to": offer["to"]})

    async def on_frame(self, link, header: dict, blob: bytes) -> None:
        op, peer = header.get("op"), link.peer_id
        try:
            if op == "want":
                stand = self.stand()
                name = await asyncio.to_thread(stand.promise, peer) if stand is not None else None
                if name:
                    await self._send(link, {"op": "offer", "name": name})
            elif op == "offer":
                name = str(header.get("name") or "")
                if name and await asyncio.to_thread(self.wants):
                    await asyncio.to_thread(self.state.set_setting, SETTING,
                                            {"name": name, "from": peer, "at": time.time()})
            elif op == "give":
                stand = self.stand()
                if stand is None:
                    return
                try:
                    txid = await asyncio.to_thread(stand.give_promised, peer, str(header.get("name") or ""),
                                                   str(header.get("to") or ""))
                    await self._send(link, {"op": "given", "name": header.get("name"), "txid": txid})
                except Exception as exc:                          # noqa: BLE001 -- said back
                    await self._send(link, {"op": "no", "why": str(exc)[:200]})
            elif op in ("given", "no"):
                offer = await asyncio.to_thread(self.offer)
                if offer.get("from") == peer:
                    offer.update({"txid": header.get("txid")} if op == "given" else {"error": header.get("why")})
                    await asyncio.to_thread(self.state.set_setting, SETTING, offer)
        except Exception:                                         # noqa: BLE001
            log.debug("fruit frame", exc_info=True)

    # ---------------------------------------------------------------- the sign-up page's request
    def ask_to_give(self, to: str) -> None:
        """From a web thread: this arcade's operator signed up with the offered fruit; ask its holder to send it."""
        offer = self.offer()
        offer.update(to=to, asked=time.time())
        offer.pop("error", None)
        self.state.set_setting(SETTING, offer)
        if self.loop is not None:
            asyncio.run_coroutine_threadsafe(
                self._send_to(offer.get("from"), {"op": "give", "name": offer["name"], "to": to}), self.loop)

    # ---------------------------------------------------------------- sending
    async def _send(self, link, frame: dict) -> None:
        try:
            await link.send({"t": "fr", **frame})
        except Exception:                                         # noqa: BLE001
            pass

    async def _send_all(self, frame: dict) -> None:
        for link in list((self.node.links if self.node else {}).values()):
            await self._send(link, frame)

    async def _send_to(self, peer: str | None, frame: dict) -> None:
        link = (self.node.links if self.node else {}).get(peer or "")
        if link is not None:
            await self._send(link, frame)

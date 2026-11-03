"""Every resource an arcade needs, from linked arcades that hold it (2026-11-02): "an auto adjusting mesh that
looks for the arcades containing the resources that we need ... We need to be able to at least have available every
core and every index from at least one node preferably three", and "the same sort of auto connect for game and
multimedia servers".

**What an arcade holds** rides in its SUB (node._send_own_sub `res`), which every node already floods across the mesh
every SUB_EVERY seconds and which its signature covers. So every arcade knows what every other holds, linked or not
(node._last_sub). A list of short keys:

    c:<net>   a Core of that chain it shares (coreshare)
    o:<net>   a whole ord index of that chain it keeps (mode "walk")
    t:<net>   that chain's token/name index, current
    m         a media relay anybody can reach (turn.py, its address proved by other arcades)

Game servers are the rooms in the same SUB: an arcade with players in a room holds `g:<room id>`.

**What an arcade needs** (`needs`): every chain it borrows (c:), every ord index it reads or helps with but does not
keep (o:), its token index while it is far behind (t:), a media relay (m), and every room it has players in (g:).

**The keeper** (`Coverage`), every EVERY seconds: for each need, the linked arcades holding it. Below GOAL (3), it
links to more holders: the ones covering the most needs that are short, needs with none linked counting double;
dialled where an address is known, else introduced (introduce.py), AT_ONCE at a time. It never asks the tuner to let a
holder go (node.keep). A node linking everyone (node.everyone) has room up to EVERYONE_MAX links; a small node (a phone
on battery) opens at most EXTRA_SMALL links beyond its target for this. Admin -> Coins & chains shows each need.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

log = logging.getLogger("arcade.mesh")

GOAL = 3
EVERY = 30.0
AT_ONCE = 3
EXTRA_SMALL = 3
#: A SUB heard within this long counts (they come every SUB_EVERY seconds).
FRESH = 120.0
MAX_KEYS = 64
#: Direct tries at a holder before a need nothing else covers is reached through relay hops (relay.py).
RELAY_AFTER = 2
OPEN_WAIT = 10.0


def clean(res: Any) -> set[str]:
    """What an arcade says it holds, bounded and shape-checked."""
    if not isinstance(res, list):
        return set()
    out = set()
    for key in res[:MAX_KEYS]:
        if isinstance(key, str) and 1 <= len(key) <= 24 and (key == "m" or key[:2] in ("c:", "o:", "t:")):
            out.add(key)
    return out


def label(key: str) -> str:
    kind, _, net = key.partition(":")
    return {"c": "Core", "o": "ord index", "t": "token index", "m": "media relay", "g": "game room"}.get(kind, kind) \
        + (f" {net}" if net and kind != "g" else "")


class Coverage:
    kinds = ("cov",)

    def __init__(self, holds: Callable[[], list[str]], needs: Callable[[], list[str]],
                 introducer: Callable[[], Any] | None = None):
        self.holds = holds
        self.needs = needs
        self.introducer = introducer or (lambda: None)
        self.node = None
        self.mine: list[str] = []                  # what this arcade holds, worked out off the loop, said in its SUB
        self.last: dict[str, dict] = {}            # need -> {"linked", "relayed", "known", "at"}
        self.tries: dict[str, int] = {}            # holder -> direct tries that did not link
        self._task: asyncio.Task | None = None

    # ---------------------------------------------------------------- hooks
    async def link_up(self, link) -> None:
        if self.node.resources is None:
            self.node.resources = lambda: self.mine
        if self._task is None or self._task.done():
            self._task = self.node._spawn(self._loop())

    async def link_down(self, peer_id: str) -> None:
        pass

    async def rebalance(self) -> None:
        pass

    async def on_frame(self, link, header: dict, blob: bytes) -> None:
        pass

    async def _loop(self) -> None:
        while self.node is not None and not self.node._stopping:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:                          # noqa: BLE001 -- the loop must not die
                log.exception("coverage")
            await asyncio.sleep(EVERY)

    # ---------------------------------------------------------------- the directory
    def holders(self, key: str) -> set[str]:
        """Arcades that said, within FRESH, that they hold `key` (rooms: that they have players in it)."""
        node, now = self.node, time.time()
        if node is None:
            return set()
        if key.startswith("g:"):
            rid, mono = key[2:], time.monotonic()
            return {o for o, info in node.routes.get(rid, {}).items() if info.get("until", 0) > mono}
        out = set()
        for origin, sub in list(node._last_sub.items()):
            if origin != node.node_id and now - node._heard.get(origin, 0) < FRESH and key in clean(sub.get("res")):
                out.add(origin)
        return out

    def _room(self) -> int:
        node = self.node
        if node.everyone():
            from .node import EVERYONE_MAX
            return max(0, EVERYONE_MAX - len(node.links))
        beyond = max(0, node._outbound() - node.outbound_target)
        return max(0, EXTRA_SMALL - beyond)

    async def step(self) -> list[str]:
        """One look: what is short, and links to the holders that cover most of it. The arcades it went for."""
        node = self.node
        if node is None:
            return []
        try:
            needs = list(dict.fromkeys(await asyncio.to_thread(self.needs)))
        except Exception:                              # noqa: BLE001
            needs = []
        try:
            mine = await asyncio.to_thread(self.holds)
        except Exception:                              # noqa: BLE001
            mine = self.mine
        if mine != self.mine:
            self.mine = list(mine)
            await node._send_own_sub()                 # said at once, not at the next SUB
        linked = set(node.links)
        relayed = set(getattr(getattr(node, "relays", None), "links", {}) or ())
        short: dict[str, int] = {}
        keep: set[str] = set()
        now = time.time()
        self.last = {}
        for peer in list(self.tries):
            if peer in linked:
                self.tries.pop(peer, None)
        stranded: list[tuple[str, set]] = []
        for key in needs:
            who = self.holders(key)
            if key.startswith("g:") and not who:
                continue                               # a room only this arcade's players are in: nothing to borrow
            have = who & linked
            self.last[key] = {"linked": len(have), "relayed": len(who & relayed), "known": len(who), "at": now}
            if not have and not (who & relayed) and who:
                stranded.append((key, who))
            keep |= set(sorted(have)[:GOAL])
            if len(have) < GOAL and who - linked:
                short[key] = 2 if not have else 1
        node.keep = keep
        # Relay hops (relay.py): a need no direct link covers, whose holders did not link after RELAY_AFTER tries,
        # gets one relayed link, to the holder tried most.
        if getattr(node, "relays", None) is not None:
            for key, who in stranded:
                tried = sorted((p for p in who if self.tries.get(p, 0) >= RELAY_AFTER), key=lambda p: -self.tries[p])
                if tried:
                    try:
                        await node.relays.open(tried[0], wait=OPEN_WAIT)
                    except Exception:                  # noqa: BLE001 -- tried again next step
                        log.debug("coverage: relay to %s", tried[0][:12], exc_info=True)
        if not short:
            return []
        score: dict[str, int] = {}
        for key, weight in short.items():
            for peer in self.holders(key) - linked:
                score[peer] = score.get(peer, 0) + weight
        room = min(AT_ONCE, self._room())
        chosen = sorted(score, key=lambda p: (-score[p], p))[:room]
        for peer in chosen:
            await self._reach(peer)
        return chosen

    async def _reach(self, peer: str) -> None:
        """Link to `peer`: dial an address it is known at, else ask for an introduction."""
        node = self.node
        if peer in node.links:
            return
        self.tries[peer] = self.tries.get(peer, 0) + 1
        addrs = [a for a, e in node.addrs.items() if e.get("id") == peer and node._may_dial(a)]
        if addrs:
            node._spawn(node._dial(addrs[0]))
            return
        intro = self.introducer()
        if intro is not None:
            try:
                await intro.meet(peer)
            except Exception:                          # noqa: BLE001 -- tried again next step
                log.debug("coverage: introduction to %s", peer[:12], exc_info=True)

    def status(self) -> list[dict]:
        """For Admin: each need, how many linked arcades hold it and how many are known to."""
        return [{"key": k, "what": label(k), "linked": v["linked"], "relayed": v.get("relayed", 0), "known": v["known"],
                 "goal": GOAL}
                for k, v in sorted(self.last.items())]


# --------------------------------------------------------------------- what this arcade holds and needs

def holds(state, service) -> list[str]:
    out: list[str] = []
    share = getattr(service, "coreshare", None)
    for net in sorted(getattr(share, "_offered", None) or ()):
        out.append(f"c:{net}")
    for net in _ord_nets(state):
        try:
            if state.ord_mode(net) == "walk":
                out.append(f"o:{net}")
        except Exception:                              # noqa: BLE001
            continue
    for chain in getattr(state, "token_chains", ()):
        try:
            tip = state.ledger_tips.get(chain.network)
            height = state.token_index(chain).indexed_height()
            if tip and height is not None and tip - height <= 20:
                out.append(f"t:{chain.network}")
        except Exception:                              # noqa: BLE001
            continue
    turn, stun = getattr(service, "turn", None), getattr(service, "stun", None)
    try:
        if turn is not None and turn.available() and stun is not None and stun.mine():
            out.append("m")
    except Exception:                                  # noqa: BLE001
        pass
    return out[:MAX_KEYS]


def needs(state, service) -> list[str]:
    out: list[str] = []
    share = getattr(service, "coreshare", None)
    for net in sorted(getattr(share, "borrowing", None) or ()):
        out.append(f"c:{net}")
    for net in _ord_nets(state):
        try:
            if state.ord_mode(net) in ("read", "help"):
                out.append(f"o:{net}")
        except Exception:                              # noqa: BLE001
            continue
    for chain in getattr(state, "token_chains", ()):
        try:
            tip = state.ledger_tips.get(chain.network)
            height = state.token_index(chain).indexed_height() or 0
            if tip and tip - height > 20:
                out.append(f"t:{chain.network}")
        except Exception:                              # noqa: BLE001
            continue
    if getattr(service, "turn", None) is not None or getattr(service, "turnshare", None) is not None:
        out.append("m")
    node = getattr(service, "node", None)
    if node is not None:
        from .node import room_id
        out += [f"g:{room_id(r)}" for r in sorted(node._own_rooms())]
    return out


def _ord_nets(state) -> list[str]:
    try:
        from ..doginal_index import STARTS
        return [c.network for c in getattr(state, "all_chains", ()) if c.network in STARTS]
    except Exception:                                  # noqa: BLE001
        return []

"""Every resource an arcade needs, from linked arcades that hold it (arcade/mesh/coverage.py, 2026-11-02:
"at least one node preferably three", and "the same sort of auto connect for game and multimedia servers")."""
import asyncio

from arcade.mesh import coverage as cov

from test_mesh import addr, fast, mk, run, started, until  # noqa: F401


def _cov(node, holds, needs):
    c = cov.Coverage(lambda: list(holds), lambda: list(needs))
    node.add_extension(c)
    return c


def test_a_small_arcade_links_to_more_holders_of_what_it_needs_and_to_nobody_else(fast, monkeypatch):
    monkeypatch.setattr(cov, "EVERY", 0.3)

    async def go():
        a = mk(target=1)                              # a phone: one link of its own
        b = mk()                                      # links everyone it can reach, and tells a where they are
        c, d, e = mk(target=0), mk(target=0), mk(target=0)
        ca = _cov(a, [], ["c:ltc", "m"])
        _cov(b, ["c:ltc"], [])
        _cov(c, ["c:ltc", "m"], [])
        _cov(d, ["c:ltc"], [])
        _cov(e, ["c:doge"], [])                       # holds nothing a needs
        await started(a, b, c, d, e)
        try:
            for n in (c, d, e):
                b.add_address(*addr(n))               # b reaches every one of them, and tells a where
            a.add_address(*addr(b))
            await until(lambda: b.node_id in a.links, what="a linked to b")
            await until(lambda: {c.node_id, d.node_id} <= set(a.links), timeout=10,
                        what="a linked to the other two Litecoin holders")
            assert e.node_id not in a.links, "nobody it does not need"
            await until(lambda: {r["key"]: r for r in ca.status()}.get("c:ltc", {}).get("linked") == 3, timeout=5,
                        what="the keeper's next look")
            rows = {r["key"]: r for r in ca.status()}
            assert rows["c:ltc"]["linked"] == 3 and rows["c:ltc"]["known"] == 3
            assert rows["m"]["linked"] == 1, "the one media relay known"
            assert {b.node_id, c.node_id, d.node_id} <= a.keep, "the tuner never lets them go"
        finally:
            for n in (a, b, c, d, e):
                await n.stop()
    run(go())


def test_what_an_arcade_holds_is_said_in_its_signed_sub_and_checked_on_receipt(fast):
    async def go():
        a, b = mk(), mk()
        _cov(a, [], [])
        _cov(b, ["c:ltc", "o:doge", "t:test", "m", "x:nonsense", "c:" + "z" * 40], [])
        await started(a, b)
        try:
            a.add_address(*addr(b))
            await until(lambda: "res" in (a._last_sub.get(b.node_id) or {}), timeout=5, what="b's SUB with res")
            assert cov.clean(a._last_sub[b.node_id]["res"]) == {"c:ltc", "o:doge", "t:test", "m"}
            assert a._verify(a._last_sub[b.node_id]), "signed with the rest of the SUB"
        finally:
            await a.stop(); await b.stop()
    run(go())


def test_a_need_no_direct_link_can_cover_is_reached_through_relay_hops(fast, monkeypatch):
    """Relay hops (relay.py): the only holder cannot be linked directly (no address, no introducer here); after
    RELAY_AFTER tries the keeper opens a relayed link to it, and Admin counts it."""
    monkeypatch.setattr(cov, "EVERY", 0.2)

    async def go():
        a, b, c = mk(target=1), mk(target=2), mk(target=1)
        ca = _cov(a, [], ["c:ltc"])
        _cov(b, [], [])
        _cov(c, ["c:ltc"], [])
        await started(a, b, c)
        try:
            a.add_address(*addr(b))
            c.add_address(*addr(b))                   # c reaches b; nobody can reach c
            await until(lambda: c.node_id in a.relays.links, timeout=10, what="a relayed link to c")
            assert c.node_id not in a.links
            await until(lambda: {r["key"]: r for r in ca.status()}.get("c:ltc", {}).get("relayed") == 1, timeout=5,
                        what="counted as relayed")
        finally:
            for n in (a, b, c):
                await n.stop()
    run(go())


def test_a_room_only_this_arcades_players_are_in_is_not_a_need(fast, monkeypatch):
    """Live on Strawberry 2026-11-02: a dozen rooms only its own pages were in were listed as needed, held by nobody."""
    monkeypatch.setattr(cov, "EVERY", 0.2)

    async def go():
        a = mk()
        ca = _cov(a, [], ["g:0123456789abcdef", "m"])
        await started(a)
        try:
            await ca.step()
            keys = {r["key"] for r in ca.status()}
            assert "g:0123456789abcdef" not in keys and "m" in keys
        finally:
            await a.stop()
    run(go())

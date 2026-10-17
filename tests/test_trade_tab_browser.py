"""A browser tab takes another arcade's swap and settles it through lock scripts, end to end.

2026-10-13: trades "only available if they leave their tab open so that transaction
building is complete on the client side solely ... 100% trustless with lock scripts", carried
by "the Pepe coin test net node to node communication protocol".

Node B is a facilitating node (NodeTrader) standing an order: 2 DOGE for 3 PEP. A person signs
up on node A in a real Chromium tab, gets some PEP, and presses Take (wallet.takeOrder). From
then on the test only mines blocks, runs each node's scanner and index as its watcher would,
and lets each side take its own steps -- the tab with its trade runner, node B with tick().
The tab ends with B's DOGE, B with the tab's PEP, and nobody held anybody's coins.
"""

import pathlib
import sys
import threading
import time

import pytest

pytest.importorskip("playwright.sync_api", reason="needs playwright")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_htlc import Key, doge                                       # noqa: E402,F401
from test_htlc_browser import _free_port                              # noqa: E402
from test_node_trader import _ctx, _scan                              # noqa: E402

COIN = 100_000_000
_AWAY = Key().address


def _mine(*rpcs):
    for rpc in rpcs:
        rpc.call("generatetoaddress", 1, _AWAY)


@pytest.fixture
def world(tmp_path, regtest, doge, monkeypatch):
    import uvicorn
    from arcade.messaging.keys import Identity
    from arcade.messaging.sender import funded_address
    from arcade.node_trader import NodeTrader
    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    for rpc in (regtest.rpc, doge.rpc):
        if float(rpc.call("getbalance")) < 50:
            rpc.call("generate", 101)
    # Dogecoin counts as synced here (the test chain is the whole chain).
    real = AppState.chain_sync
    monkeypatch.setattr(AppState, "chain_sync", lambda self, c: {"ready": True, "progress": 1.0, "online": True}
                        if c.is_doge else real(self, c))
    pep_a = _ctx(regtest, "regtest")
    pep_a.role = "messaging"
    a = AppState(home=tmp_path / "a", messaging=pep_a,
                 ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                                     datadir=pathlib.Path("/nonexistent")),
                 doge=_ctx(doge, "doge"))
    (tmp_path / "a").mkdir(parents=True, exist_ok=True)
    (tmp_path / "a" / "tokens-chain").write_text("regtest\n")
    a.identity = Identity.generate()
    # Node B: a facilitating node with its own home, identity and pinned message address.
    pep_b = _ctx(regtest, "regtest")
    b_state = AppState(home=tmp_path / "b", messaging=pep_b,
                       ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                                           datadir=pathlib.Path("/nonexistent")))
    b_state.identity = Identity.generate()
    home = regtest.rpc.call("getnewaddress")
    regtest.rpc.call("sendtoaddress", home, 25)
    with b_state.store() as store:
        store.set_meta(f"identity_address:{pep_b.network}", home)
    b = NodeTrader(b_state, {"regtest": pep_b, "doge": _ctx(doge, "doge")}, pep_b)
    _mine(regtest.rpc)

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(a), host="127.0.0.1", port=port,
                                           log_level="error", timeout_graceful_shutdown=1.0))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    yield f"http://127.0.0.1:{port}", a, b, regtest.rpc, doge.rpc
    server.should_exit = True
    thread.join(timeout=5)


def _catch_up(state):
    from test_account_feed import _catch_up as feed_catch_up
    feed_catch_up(state)
    for chain in state.token_chains:
        try:
            state.token_index(chain).sync()
        except Exception:
            pass


def _scan_a(state):
    """Node A as its watcher would: the mail (candidates for its accounts) and the index."""
    from arcade.messaging.scanner import Scanner
    with state.messaging.rpc() as node, state.store() as store:
        scanner = Scanner(node, state.messaging.params, store, identity=state.identity)
        scanner.scan()
        scanner.scan_mempool()
    _catch_up(state)


def _run(tab, net, trade, a, b, pep, dg, rounds=60, b_ledger=False):
    """The tab's side done by its own trade runner (wallet.startTradeRunner), as on every
    arcade page; this only mines, reads the chains as each node's watcher would, ticks node
    B, and nudges the runner (kickTrades) rather than waiting out its beat. Returns the
    tab's trade as it changed: [state, note, locked]."""
    tab.evaluate("async (net) => { const w = await import('/wallet.js'); w.startTradeRunner(net, 400); }", net)
    seen = []
    for _ in range(rounds):
        _mine(pep, dg)
        _scan_a(a)
        _scan(b)
        if b_ledger:
            b.state.token_index(b.state.messaging).sync()
        b.tick()
        _scan_a(a)
        tab.evaluate("async () => (await import('/wallet.js')).kickTrades()")
        tab.wait_for_timeout(600)
        got = tab.evaluate("""async ([net, id]) => { const w = await import('/wallet.js');
            const wal = await w.opened(net); const t = wal && w.myTrades(wal).find((t) => t.id === id);
            return t ? [t.state, t.note || '', !!t.myLock] : null; }""", [net, trade])
        if got and (not seen or seen[-1] != got):
            seen.append(got)
        mine = b.book.get(trade)
        if got and got[0] == "done" and mine is not None and mine.state == "done":
            break
    return seen


def test_a_tab_takes_a_swap_and_both_sides_settle(world):
    from playwright.sync_api import sync_playwright
    base, a, b, pep, dg = world
    b.stand("order-dogepep", {"network": "doge", "kind": "coin", "amount": 2 * COIN},
            {"network": "regtest", "kind": "coin", "amount": 3 * COIN})
    order = {"id": "order-dogepep",
             "give": {"network": "doge", "kind": "coin", "asset": "", "amount": 2 * COIN},
             "get": {"network": "regtest", "kind": "coin", "asset": "", "amount": 3 * COIN},
             "trade_key": b.key.pubkey.hex(), "msg_key": b.state.identity.public_bytes.hex(),
             "node": "ab" * 32, "expires": int(time.time()) + 3600}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        tab = browser.new_page()
        tab.goto(f"{base}/join")
        tab.evaluate("async () => { window.w = await import('/wallet.js'); }")
        made = tab.evaluate("""async (net) => {
            const r = await w.signUp('swapper' + Math.floor(Math.random() * 1e6), 'a long enough password',
                                     {network: net.network, version: net.version});
            return {regtest: r.wallet.on.regtest.address, doge: r.wallet.on.doge && r.wallet.on.doge.address}; }""",
            {"network": "regtest", "version": 111})
        assert made["doge"], "the tab has a Dogecoin key (D main open, test chain synced)"
        pep.call("sendtoaddress", made["regtest"], 10)
        _mine(pep)
        _scan_a(a)
        trade = tab.evaluate("""async (order) => {
            const net = {network: 'regtest', version: 111};
            const wal = await w.opened(net);
            const t = await w.takeOrder(wal, order); return t.id; }""", order)
        seen = _run(tab, {'network': 'regtest', 'version': 111}, trade, a, b, pep, dg)
        browser.close()
    assert seen and seen[-1][0] == "done", seen[-6:]
    assert seen[-1][1] == "I claimed their lock", seen[-6:]
    assert any(locked for _, _, locked in seen), "the tab locked its own coins"
    assert b.book.get(trade).state == "done", seen[-6:]


def test_one_tab_runs_the_trades_and_another_takes_over_when_it_closes(world):
    """2026-10-13: the trade loop runs "on every arcade page ... even if the tab is in
    the background". Every page starts the runner; a Web Lock lets one tab run it, and when
    that tab closes the next one does -- with no page reload."""
    from playwright.sync_api import sync_playwright
    base, a, b, pep, dg = world
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context()
        pages = [context.new_page() for _ in range(3)]
        for page in pages:
            page.goto(f"{base}/join")
            page.evaluate("async () => { const w = await import('/wallet.js'); w.startTradeRunner({network: 'regtest', version: 111}, 300); }")
        pages[0].wait_for_timeout(800)
        running = [page.evaluate("() => !!globalThis.__arcadeTradeRunner") for page in pages]
        assert running.count(True) == 1, running
        first = running.index(True)
        pages[first].close()
        rest = [page for n, page in enumerate(pages) if n != first]
        for _ in range(20):
            rest[0].wait_for_timeout(250)
            now = [page.evaluate("() => !!globalThis.__arcadeTradeRunner") for page in rest]
            if True in now:
                break
        assert now.count(True) == 1, now
        browser.close()


def test_every_signed_in_page_starts_the_runner():
    import re
    base = (pathlib.Path(__file__).resolve().parent.parent / "arcade/web/templates/base.html").read_text()
    assert re.search(r"startTradeRunner\(ARCADE_CHAIN\)", base), "base.html starts it on every page"
    swaps = (pathlib.Path(__file__).resolve().parent.parent / "arcade/web/templates/_tab_swaps.html").read_text()
    assert "setInterval" not in swaps and "tradeTick" not in swaps, "the trade pages only show trades"

import os
import sqlite3
import threading

# No test asks the real leaked-password list on the internet (wallet.js `leaked`, 2026-10-20): its own logic is tested
# under Node against a fake answer (test_leaked_password.py).
os.environ.setdefault("ARCADE_LEAKED_URL", "")

import pytest

from arcade.db import Database, StateDB, register_journalled_table
from arcade.regtest import RegtestNode, reap_leftovers, reap_stale_basetemps
from arcade.web import door as _door

# Starlette's TestClient connects from the made-up peer "testclient" to the made-up host "testserver". In the suite
# they stand for a browser on the node's own machine (door.from_outside); uvicorn never reports either.
_door.LOOPBACK_PEERS += ("testclient",)
_door.LOOPBACK_HOSTS += ("testserver",)

# A toy state table, used to prove the undo journal works without waiting for the
# real protocol tables that arrive in M2.
BALANCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS balance (
    address     TEXT    NOT NULL,
    property_id INTEGER NOT NULL,
    amount      INTEGER NOT NULL,
    PRIMARY KEY (address, property_id)
);
"""


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "arcade.sqlite")
    database.conn.executescript(BALANCE_SCHEMA)
    register_journalled_table("balance", ("address", "property_id"))
    yield database
    database.close()


@pytest.fixture
def state(db):
    return StateDB(db)


@pytest.fixture(scope="session", autouse=True)
def _previous_runs_do_not_inherit():
    """Do not start a run holding the last run's nodes.

    A run that is interrupted never reaches `RegtestNode.stop`, so its daemon and
    its datadir stay up -- RAM and disk that the next run pays for, which is how
    one commit comes back green once and red once. A node with a live process
    behind it is only stopped if its datadir predates a whole suite, so a run
    already going on this box is not disturbed, and neither is this one, whose
    own node has not started yet.
    """
    reap_leftovers()


@pytest.fixture(scope="session", autouse=True)
def _previous_runs_leave_no_temp_files(tmp_path_factory):
    """Do not start a run measuring itself against the last run's debris.

    The other half of what a killed run leaves behind. `RegtestNode.stop` removes
    a datadir and pytest removes a `tmp_path`, and neither teardown runs when a
    session is killed, so both piles just sit here: this box had fifteen numbered
    `pytest-N` directories, 645 MB, back to three days before. Anything younger
    than a suite is left alone, which is the same guard `_previous_runs_do_not_inherit`
    uses, because a gated commit and a census can be running on this box at the
    same time and neither is entitled to delete the other's files.
    """
    reap_stale_basetemps(mine=tmp_path_factory.getbasetemp())


@pytest.fixture(scope="session")
def regtest():
    """One regtest node shared across the session -- starting it is slow."""
    node = RegtestNode()
    try:
        # A busy machine (a mainnet node syncing beside it) can take longer than a minute.
        node.start(timeout=float(os.environ.get("ARCADE_REGTEST_TIMEOUT", "60")))
    except RuntimeError as exc:
        pytest.skip(f"regtest node unavailable: {exc}")
    yield node
    node.stop()


@pytest.fixture
def no_nodes(monkeypatch):
    """Guarantee that nothing under test can reach a real node.

    `datadir=Path("/nonexistent")` looks airtight and is not: credential loading
    falls back to a default location, so on any machine with a node running the
    "offline" web tests quietly talked to it. They then passed or failed
    according to what happened to be running, which is the opposite of a test.
    BOXA found this because the same test failed there and passed here.

    Both doors are shut: explicit credentials, and the discovery fallback.
    """
    def refuse(*args, **kwargs):
        raise RuntimeError("no node available (test)")

    import arcade.config
    import arcade.discovery
    import arcade.web.state

    monkeypatch.setattr(arcade.config, "load_rpc_credentials", refuse)
    monkeypatch.setattr(arcade.web.state, "load_rpc_credentials", refuse,
                        raising=False)
    monkeypatch.setattr(arcade.discovery, "best", lambda *a, **k: None)
    return None


#: How long a collection run is given to come back when the test that started
#: it ends. `Runner.pause` wakes a run out of its block wait instead of waiting
#: for the wait to end, so this is a few seconds and not a block's minute.
QUIESCE = 5.0


@pytest.fixture(autouse=True)
def wallet_assets_state_as_it_was():
    """create_app hands its AppState to DogeFeedX's wallet reads (wallet_assets.STATE, 2026-11-06); a test that built an
    app must not leave the next test reading that app's chains instead of what it patched."""
    yield
    import sys
    wa = sys.modules.get("arcade.web.dogefeedx.wallet_assets")
    if wa is not None:
        wa.STATE = None


@pytest.fixture(autouse=True)
def sys_path_as_it_was():
    """A test that adds to sys.path takes it out again: test_lora_keys and test_lora_txcheck once put an old checkout
    of arcade first and left it, and every worker process spawned later on that xdist worker imported that arcade
    (the ord index tests failed, 2026-10-29)."""
    import sys
    kept = list(sys.path)
    yield
    sys.path[:] = kept


@pytest.fixture(autouse=True)
def desk_default_look(monkeypatch, request):
    """Tests marked `desk_default` open pages in the shipped default look, the Ubuntu desktop (pytest_configure
    turns it off for the rest)."""
    if request.node.get_closest_marker("desk_default") is not None:
        from arcade.web import app as web_app
        monkeypatch.setattr(web_app, "DESK_DEFAULT", "ubuntu")
    if request.node.get_closest_marker("phone_default") is not None:
        from arcade.web import app as web_app
        monkeypatch.setattr(web_app, "PHONE_DEFAULT", "home")


@pytest.fixture(autouse=True)
def collection_runs(monkeypatch):
    """Finish the collection runs a test started before the next one begins.

    A run is a thread that keeps polling the node for as long as the job has
    pieces left, and nothing joined it -- not the route that started it, not
    the test that pressed the button. Tests hand the route a fake node and take
    it back at teardown; a thread from a finished test does not notice the swap,
    and spends the next test's coins or trips on the node the next test did not
    bring. That is why whole files passed alone and failed in the middle of a
    run (the 2026-09-22 census, and ARCADE_THREADWATCH below).

    The joining is this fixture's own business, not production shutdown's --
    a test ending is not an app closing, and what shutdown does about a run in
    flight has its own test.
    """
    import arcade.collections as C

    runners: list = []
    start = C.Runner.start

    def tracked_start(self, job_id):
        went = start(self, job_id)
        if went and self not in runners:
            runners.append(self)
        return went

    monkeypatch.setattr(C.Runner, "start", tracked_start)
    yield
    for runner in runners:
        if not runner.quiesce(QUIESCE):
            left = [t.name for t in threading.enumerate()
                    if t.name.startswith("arcade-collection-")]
            pytest.fail(f"a collection run outlived its test: {left}")


#: Set ARCADE_THREADWATCH to a path to record every thread that outlived the test
#: that started it, as "running<TAB>thread<TAB>born in". A suite where a file
#: passes alone and fails in the middle of the run is a suite with a thread still
#: talking to the next test's fakes: a patch on the CLASS (`setattr(type(chain),
#: "rpc", ...)`) reaches the ChainContext of every test that comes after, so a
#: run left running from an earlier test spends the next test's coins. The web
#: tests patch their own instances now; the watch is how the next one of these is
#: found rather than guessed at. The line names which test to fix. Off by default.
_WATCH = os.environ.get("ARCADE_THREADWATCH", "")
_orIGIN = ["(collected)"]

if _WATCH:
    _start = threading.Thread.start

    def _tracked_start(self, *args, **kwargs):
        self.arcade_origin = _orIGIN[0]
        return _start(self, *args, **kwargs)

    threading.Thread.start = _tracked_start


@pytest.fixture(autouse=True)
def threadwatch(request):
    """Note the threads that arrived from an earlier test. See above."""
    if not _WATCH:
        return
    here = threading.current_thread()
    with open(_WATCH, "a") as log:
        for other in threading.enumerate():
            if other is here or not other.is_alive():
                continue
            born = getattr(other, "arcade_origin", "(collected)")
            if born != request.node.name:
                print(f"threadwatch {request.node.name} <- {other.name} from {born}",
                      file=log)
    _orIGIN[0] = request.node.name


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "node_trades_default: run with node-facilitated trades at the "
        "shipped default (off) instead of on")
    # The browser tests drive the website. A computer's browser opens in the Ubuntu desktop until its reader picks a
    # look (app.DESK_DEFAULT, 2026-10-29), and a test's headless browser is one that never picked: every page would go
    # to /desktop and open in a window. Off for the whole run, module-scoped servers included; `desk_default` below.
    from arcade.web import app as web_app
    web_app.DESK_DEFAULT = ""
    web_app.PHONE_DEFAULT = ""                     # the same for a phone's home screen (2026-10-29)
    config.addinivalue_line(
        "markers", "desk_default: open pages in the shipped default look (the Ubuntu desktop) instead of the website")
    config.addinivalue_line(
        "markers", "phone_default: a phone's browser opens the home screen (the shipped default) instead of the website")


@pytest.fixture(autouse=True)
def node_trades_on_unless_asked(monkeypatch, request):
    """Node-facilitated trades are OFF by default since 2026-10-13 (the operator: trades
    only while both tabs are open, unless the operator turns them on). Most tests
    here are ABOUT those trades -- listings, held bids, away fills, the cross-chain
    book -- so they run with the switch on, as on a node whose operator chose it.
    A test of the default marks itself `node_trades_default`; a value a test saves
    itself always wins."""
    if request.node.get_closest_marker("node_trades_default"):
        return
    from arcade.web import state as statemod
    real = statemod.AppState.setting

    def setting(self, name, default=None):
        if name == "node_trades" and default is False:
            return real(self, name, True)
        return real(self, name, default)

    monkeypatch.setattr(statemod.AppState, "setting", setting)


@pytest.fixture(autouse=True)
def status_cache_starts_empty():
    """ChainContext.status keeps an answer for 10 s (2026-10-13). A test that changes a
    node and reads its status again must not see the answer an earlier test kept."""
    from arcade.web import state as statemod
    statemod._STATUS_KEPT.clear()
    yield
    statemod._STATUS_KEPT.clear()


@pytest.fixture(autouse=True)
def ledger_addresses_start_empty():
    """`_ledger_addresses` keeps a wallet's addresses for 15 s, keyed by the node's
    host and port -- and every fake node here has neither, so they all share one
    key. A test whose wallet holds nothing must not see the addresses an earlier
    test's wallet held."""
    from arcade.web import app as appmod
    appmod._LEDGER_ADDRESSES.clear()
    yield
    appmod._LEDGER_ADDRESSES.clear()


@pytest.fixture(autouse=True)
def pages_names_resolve(monkeypatch):
    """Tests name pages hosts that have no DNS (pages.example...); the arcade frames from here when its pages name
    does not resolve (2026-10-19), which test_pages_dns asks of on purpose."""
    from arcade.web.state import AppState
    monkeypatch.setattr(AppState, "_resolves", lambda self, name: True)



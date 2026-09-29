"""The block watcher: scan when a block lands, and never die doing it.

Messages only become visible once they are in a block, and the failure this
prevents is specific and bad -- a correspondent says "I sent it", the recipient
is looking straight at the conversation, and nothing is there because the
interface is not looking.
"""

import pathlib
import re
import time

import pytest

from arcade.config import NETWORKS
from arcade.web.watcher import BlockWatcher


class FakeChain:
    can_spend = False               # a watcher without a wallet keeps no shop

    def __init__(self, network="regtest", tip=100, fails=False):
        self.network = network
        self.params = NETWORKS[network]
        self.tip = tip
        self.fails = fails
        self.polls = 0

    def rpc(self):
        return self

    def __enter__(self):
        if self.fails:
            raise RuntimeError("connection refused")
        return self

    def __exit__(self, *exc):
        return False

    def get_block_count(self):
        self.polls += 1
        return self.tip


class FakeState:
    def __init__(self, messaging, ledger):
        self.messaging = messaging
        self.ledger = ledger
        self.tips = {}
        self.last_checked = {}
        self.generation = 0
        self.scans = []
        # No token indexes here; test_tokens_web.py drives _sync_ledgers
        # against a real node.
        self.token_chains = []
        self.ledger_tips = {}

    def bump_generation(self):
        self.generation += 1

    def set_update_status(self, status):
        self.update_status = status

    def setting(self, name, default=None):
        # The watcher asks before it updates itself or announces a release.
        # A double that answers nothing at all made eight watcher tests fail
        # with AttributeError -- the watcher was fine, its stand-in was not.
        return {"auto_update": False, "announce_releases": False}.get(name, default)

    def my_tag(self):
        return ""

    def live_progress(self):
        return None

    def ensure_identity(self):
        return None

    def store(self):
        raise AssertionError("no store expected in these tests")


def test_an_unchanged_tip_does_not_scan(monkeypatch):
    """A quiet chain must cost one RPC call and nothing else."""
    state = FakeState(FakeChain(tip=100), FakeChain("main", tip=5))
    watcher = BlockWatcher(state)

    scanned = []
    monkeypatch.setattr("arcade.web.watcher.Scanner",
                        lambda *a, **k: scanned.append(1))
    watcher._tick()
    first = len(scanned)
    watcher._tick()

    assert len(scanned) == first, "a tip that did not move must not rescan"


def test_a_node_that_is_down_does_not_raise(monkeypatch):
    """Nodes restart -- for a wallet switch, among other things."""
    state = FakeState(FakeChain(fails=True), FakeChain("main", fails=True))
    BlockWatcher(state)._tick()          # must simply return

    assert state.tips == {}


def test_a_failed_check_is_not_recorded_as_checked():
    """'Nothing new' and 'not looking' have to stay distinguishable."""
    state = FakeState(FakeChain(fails=True), FakeChain("main", fails=True))
    BlockWatcher(state)._tick()

    assert state.last_checked == {}


def test_a_successful_check_records_when(monkeypatch):
    state = FakeState(FakeChain(tip=100), FakeChain("main", tip=5))
    monkeypatch.setattr("arcade.web.watcher.Scanner", lambda *a, **k: None)
    before = time.time()
    BlockWatcher(state)._tick()

    assert state.last_checked["regtest"] >= before


def test_the_ledger_chain_is_scanned_public_only(monkeypatch):
    """Mainnet must never be trial-decrypted, whatever else changes here."""
    calls = []

    class Recorder:
        def __init__(self, rpc, params, store, identity=None, public_only=False):
            calls.append((params.name, public_only, identity))

        def scan(self, max_blocks=0):
            class R:
                opened = announcements = group_posts = 0
            return R()

    state = FakeState(FakeChain(tip=100), FakeChain("main", tip=5))
    state.store = lambda: _NullStore()
    monkeypatch.setattr("arcade.web.watcher.Scanner", Recorder)
    BlockWatcher(state)._tick()

    by_network = {name: public for name, public, _ in calls}
    assert by_network["main"] is True
    assert by_network["regtest"] is False


def test_an_empty_block_does_not_bump_the_generation(monkeypatch):
    """A block with nothing in it for us must not reload every open page."""
    class Empty:
        def __init__(self, *a, **k):
            pass

        def scan(self, max_blocks=0):
            class R:
                opened = announcements = group_posts = 0
            return R()

    state = FakeState(FakeChain(tip=100), FakeChain("main", tip=5))
    state.store = lambda: _NullStore()
    monkeypatch.setattr("arcade.web.watcher.Scanner", Empty)
    BlockWatcher(state)._tick()

    assert state.generation == 0


def test_finding_something_bumps_the_generation(monkeypatch):
    class Found:
        def __init__(self, *a, **k):
            pass

        def scan(self, max_blocks=0):
            class R:
                opened = 1
                announcements = 0
                group_posts = 0
            return R()

    state = FakeState(FakeChain(tip=100), FakeChain("main", tip=5))
    state.store = lambda: _NullStore()
    monkeypatch.setattr("arcade.web.watcher.Scanner", Found)
    BlockWatcher(state)._tick()

    assert state.generation > 0


#: The templates, read as text. What this asks is about a page's own
#: JavaScript, so it is asked where that is written rather than through a
#: running application.
TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "arcade" / "web" / "templates"


#: The flags a page raises while it is mid-way through something, named one by
#: one because the list is the claim. `working` is the wallet's own, set around
#: each signature; `trading` is a buy being relayed across a frame, which spends
#: most of its time between calls; `inscribing` is one file going up as several
#: transactions. A `...Ready` flag is not a hold and is not listed: it only says
#: a page's script is wired up, which is what the browser tests wait for.
HOLDS = {
    "working": ("wallet_js.js", "messaging_js.js"),
    "trading": ("inscription_view.html",),
    "inscribing": ("_account_inscribe.html",),
}


def test_a_page_holding_a_job_is_protected_from_the_reload():
    """A page in the middle of a job has to survive the block that job causes.

    base.html polls `/events` and reloads when the generation moves, unless the
    page says it is busy, and a page says so by raising a flag on the body. That
    works only if the guard knows the flag's name, and the name lives in two
    files, so it drifts -- and it cost a real inscription on the live instance
    on 2026-09-24. A big file's split confirmed, the generation moved, the tab
    reloaded out from under the loop that was offering the pieces, and the rest
    of the file was never broadcast. The split's fee was spent either way, so
    what was left was a file somebody paid for and did not get.

    So this reads the pages and the guard and asks that they agree: every hold
    is one this test names, is raised where it says it is raised, and is refused
    by `busy()`. Which is also the shape of the bug -- the panel simply stopped
    holding it, and nothing else anywhere noticed.
    """
    held = {}
    for page in sorted(TEMPLATES.glob("*.html")) + sorted(TEMPLATES.glob("*.js")):
        for name in re.findall(r"document\.body\.dataset\.(\w+)\s*=[^=]",
                               page.read_text()):
            if not name.endswith("Ready"):
                held.setdefault(name, set()).add(page.name)
    assert set(held) == set(HOLDS), \
        f"the pages raise {sorted(held)} and this names {sorted(HOLDS)}; a new " \
        "hold is a new thing a block can interrupt, so it belongs here and in " \
        "busy(), not only in the page that wants it"
    guard = (TEMPLATES / "base.html").read_text()
    for name, where in HOLDS.items():
        assert held.get(name, set()) & set(where), \
            f"{name} is no longer raised by {where}, so {name} no longer means " \
            "anything to busy()"
        assert f"if (document.body.dataset.{name}) return true;" in guard, \
            f"busy() in base.html never looks at {name}, so a block reloads " \
            "over the top of it"


def test_a_scan_that_raises_does_not_kill_the_watcher(monkeypatch):
    class Exploding:
        def __init__(self, *a, **k):
            raise RuntimeError("database is locked")

    state = FakeState(FakeChain(tip=100), FakeChain("main", tip=5))
    state.store = lambda: _NullStore()
    monkeypatch.setattr("arcade.web.watcher.Scanner", Exploding)

    BlockWatcher(state)._tick()          # must not raise
    assert state.generation == 0


class _NullStore:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# --- a post of ours reaches a block -------------------------------------------
# Our own copy is written when it is sent, and its height only arrived when the
# scanner had read the WHOLE post back off the chain. A picture is eighteen
# transactions across ten blocks, so a post whose first transaction confirmed
# within a minute sat marked pending for ten -- every byte already paid for and
# in a block. Reported from a phone.


class _Stored:
    """A FakeState that has a real store behind it, which the base one refuses."""

    def __init__(self, store, rpc):
        chain = FakeChain("regtest")
        chain.rpc = lambda: _Held(rpc)
        ledger = FakeChain("main")
        ledger.rpc = lambda: _Held(rpc)
        self.messaging = chain
        self.ledger = ledger
        self.tips = {}
        self.last_checked = {}
        self.generation = 0
        self.token_chains = []
        self.ledger_tips = {}
        self._store = store

    def bump_generation(self):
        self.generation += 1

    def store(self):
        return _Held(self._store)


class _Held:
    """A context manager that hands back something already made."""

    def __init__(self, thing):
        self.thing = thing

    def __enter__(self):
        return self.thing

    def __exit__(self, *exc):
        return False


def test_a_post_is_confirmed_when_its_transaction_is(tmp_path):
    from arcade.messaging.store import MessageStore
    from arcade.web.watcher import BlockWatcher

    store = MessageStore(tmp_path / "m.sqlite")
    store.add_group_post("test", "main", "tx-one", 0, 1000, "nMe", "Me",
                         "", mine=True, file_name="photo.jpg",
                         file_type="image/jpeg", file_data=b"\xff\xd8" * 60)
    assert [dict(r)["txid"] for r in store.unconfirmed_posts("test")] == ["tx-one"]

    class FakeRpc:
        def call(self, method, *args):
            if method == "getrawtransaction":
                return {"confirmations": 3, "blockhash": "bh", "blocktime": 1789}
            if method == "getblock":
                return {"height": 1486617}
            raise AssertionError(method)

    state = _Stored(store, FakeRpc())
    state.messaging.network = "test"

    BlockWatcher(state)._confirm_posts()

    row = store.conn.execute("SELECT height, block_time FROM group_post").fetchone()
    assert row["height"] == 1486617, "the block its first transaction reached"
    assert row["block_time"] == 1789
    assert store.unconfirmed_posts("test") == []
    assert state.generation >= 1, "the page has to be told to redraw"
    store.close()


def test_a_post_still_in_the_mempool_stays_pending(tmp_path):
    from arcade.messaging.store import MessageStore
    from arcade.web.watcher import BlockWatcher

    store = MessageStore(tmp_path / "m.sqlite")
    store.add_group_post("test", "main", "tx-one", 0, 1000, "nMe", "Me", "hi",
                         mine=True)

    class FakeRpc:
        def call(self, method, *args):
            return {"confirmations": 0}

    state = _Stored(store, FakeRpc())
    state.messaging.network = "test"
    BlockWatcher(state)._confirm_posts()

    assert store.conn.execute("SELECT height FROM group_post").fetchone()[0] == 0
    store.close()


def test_pieces_of_an_unfinished_post_keep_the_scanner_looking(tmp_path):
    """A chunk that landed in a block the scan had just passed waited for the
    NEXT block to be noticed -- and a post is only assembled once every one of
    its pieces has been. Seventeen of eighteen is not a post."""
    from arcade.messaging.store import MessageStore

    store = MessageStore(tmp_path / "m.sqlite")
    assert store.waiting_chunks("test") == 0
    store.add_group_chunk("test", b"\x01" * 8, 15, "tx", 1, 1000, "nMe", b"a piece")
    assert store.waiting_chunks("test") == 1
    assert store.waiting_chunks("main") == 0
    store.drop_group_chunks("test", b"\x01" * 8)
    assert store.waiting_chunks("test") == 0
    store.close()


def test_every_tick_looks_for_posts_to_confirm():
    """Wired in, not merely written: the method above is only worth anything
    if something calls it. `_confirm_sent` had exactly this shape and this is
    its twin for the public board."""
    import inspect

    from arcade.web.watcher import BlockWatcher

    source = inspect.getsource(BlockWatcher._tick)
    assert "self._confirm_posts" in source
    assert "self._confirm_sent" in source


def test_the_tip_is_only_marked_seen_once_a_scan_has_reached_it():
    """Marking it before scanning meant a pass that failed -- a busy node, a
    timeout -- skipped those blocks until the next one arrived, and nothing
    ever went back for them."""
    import inspect

    from arcade.web.watcher import BlockWatcher

    source = inspect.getsource(BlockWatcher._check)
    mark = source.index("self.state.tips[name] = tip")
    scan = source.index("scanner.scan(")
    assert scan < mark, "the tip is recorded before the scan that justifies it"


# --- how far along a long send is ----------------------------------------------
# A picture is dozens of transactions. A split wallet funds each from its own
# output, so they are independent and the miner takes them in whatever order it
# likes -- the FIRST one can be the last to land. Asking only about that one
# made a message with forty of its fifty transactions in blocks read
# "unconfirmed", with nothing to say how far along it was.


def _rpc_where(confirmed: set[str]):
    class FakeRpc:
        def call(self, method, *args):
            if method == "getrawtransaction":
                txid = args[0]
                if txid in confirmed:
                    return {"confirmations": 2, "blockhash": "bh", "blocktime": 99}
                return {"confirmations": 0}
            if method == "getblock":
                return {"height": 1486700}
            raise AssertionError(method)
    return FakeRpc()


def test_a_long_send_reports_how_much_of_it_is_in_blocks(tmp_path):
    from arcade.messaging.store import MessageStore
    from arcade.web.watcher import BlockWatcher

    store = MessageStore(tmp_path / "m.sqlite")
    txids = [f"tx{i}" for i in range(50)]
    store.add_sent(txids[0], b"\x01" * 32, "", "fp", b"a picture", txids=txids)

    # Forty landed; the first is not among them, which is the case that used to
    # read "unconfirmed" with nothing else to say.
    state = _Stored(store, _rpc_where(set(txids[10:])))
    BlockWatcher(state)._confirm_sent()

    row = store.conn.execute("SELECT confirmed, confirmed_count FROM sent").fetchone()
    assert row["confirmed"] == 0, "it is not finished until all of it is"
    assert row["confirmed_count"] == 40
    store.close()


def test_the_last_transaction_finishes_it(tmp_path):
    from arcade.messaging.store import MessageStore
    from arcade.web.watcher import BlockWatcher

    store = MessageStore(tmp_path / "m.sqlite")
    txids = ["a", "b", "c"]
    store.add_sent("a", b"\x01" * 32, "", "fp", b"x", txids=txids)

    state = _Stored(store, _rpc_where({"a", "b", "c"}))
    BlockWatcher(state)._confirm_sent()

    row = store.conn.execute("SELECT confirmed, height, confirmed_count "
                             "FROM sent").fetchone()
    assert row["confirmed"] == 1 and row["confirmed_count"] == 3
    assert row["height"] == 1486700, "the block the first transaction reached"
    store.close()


def test_a_send_from_before_this_existed_still_works(tmp_path):
    """Rows written by an older version know only their first transaction."""
    from arcade.messaging.store import MessageStore
    from arcade.web.watcher import BlockWatcher

    store = MessageStore(tmp_path / "m.sqlite")
    store.add_sent("only", b"\x01" * 32, "", "fp", b"x")
    store.conn.execute("UPDATE sent SET txids=''")          # as the old code left it

    row = store.unconfirmed_sent()[0]
    assert store.txid_list(row, row["txid"]) == ["only"]

    state = _Stored(store, _rpc_where({"only"}))
    BlockWatcher(state)._confirm_sent()
    assert store.conn.execute("SELECT confirmed FROM sent").fetchone()[0] == 1
    store.close()


def test_a_busy_node_leaves_the_count_alone(tmp_path):
    """Half an answer is worse than none: it would report going backwards."""
    from arcade.messaging.store import MessageStore
    from arcade.web.watcher import BlockWatcher

    store = MessageStore(tmp_path / "m.sqlite")
    store.add_sent("a", b"\x01" * 32, "", "fp", b"x", txids=["a", "b", "c"])
    store.conn.execute("UPDATE sent SET confirmed_count=2")

    class Refuses:
        def call(self, method, *args):
            raise RuntimeError("node busy")

    BlockWatcher(_Stored(store, Refuses()))._confirm_sent()
    row = store.conn.execute("SELECT confirmed, confirmed_count FROM sent").fetchone()
    assert row["confirmed_count"] == 2 and row["confirmed"] == 0
    store.close()


def test_the_update_check_is_not_hourly(monkeypatch):
    """Six hours was a guess against weekly releases. Nine went out in two
    hours on the day it shipped, and every machine sat on whichever one it
    happened to have -- including one holding a consensus rule that had since
    moved, while its owner was told to update by hand (D-078)."""
    from arcade.web.watcher import BlockWatcher

    assert BlockWatcher.UPDATE_EVERY <= 15 * 60, \
        "an automatic update nobody notices is not automatic, it is slow"


def test_a_node_that_publishes_nothing_checks_anyway(monkeypatch):
    """The first tick after a restart has to look: `_update_checked` starting
    at zero is what makes a machine that has been off for a week current
    before anybody uses it."""
    from arcade.web.watcher import BlockWatcher

    state = FakeState(FakeChain(tip=100), FakeChain("main", tip=5))
    watcher = BlockWatcher(state)
    assert watcher._update_checked == 0.0
    looked = []
    monkeypatch.setattr("arcade.web.watcher.update.check",
                        lambda: looked.append(1) or (None, None, False))
    monkeypatch.setattr(type(state), "setting",
                        lambda self, name, default=None: True, raising=False)
    watcher._auto_update()
    assert looked, "it did not even ask"


def test_one_broken_phase_does_not_end_the_pass(monkeypatch):
    """They ran in a bare sequence, so the first to raise took every phase
    after it, and the outer catch logs at debug -- a subsystem switching
    itself off in silence. That is how release announcements stopped
    (D-084, D-089)."""
    from arcade.web.watcher import BlockWatcher

    state = FakeState(FakeChain(tip=100), FakeChain("main", tip=5))
    watcher = BlockWatcher(state)
    ran = []
    monkeypatch.setattr(watcher, "_auto_update", lambda: ran.append("update"))
    monkeypatch.setattr(watcher, "_repair_once",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    later = []
    monkeypatch.setattr(watcher, "_announce_release", lambda: later.append("announce"))
    watcher._tick()                      # no raise: one phase cannot end the pass
    assert ran == ["update"], "the update went first"
    assert later == ["announce"], "and the phase after the broken one still ran"


def test_it_says_what_it_decided(monkeypatch):
    """A thing that runs on its own has to be able to say when it last ran.
    Robin asked twice why updates were not automatic while they were running
    exactly as written, and neither of us could answer it from outside."""
    from arcade.web.watcher import BlockWatcher

    state = FakeState(FakeChain(tip=100), FakeChain("main", tip=5))
    watcher = BlockWatcher(state)
    monkeypatch.setattr(type(state), "setting",
                        lambda self, name, default=None: True, raising=False)

    monkeypatch.setattr("arcade.web.watcher.update.check",
                        lambda: ("abc1234", "abc1234", False))
    watcher._auto_update()
    assert state.update_status["what"] == "up to date"
    assert state.update_status["installed"] == "abc1234"
    assert state.update_status["at"] > 0

    def unreachable():
        raise OSError("connection refused")

    watcher._update_checked = 0.0
    monkeypatch.setattr("arcade.web.watcher.update.check", unreachable)
    watcher._auto_update()
    assert state.update_status["what"] == "could not reach the site"
    assert "refused" in state.update_status["error"], "and why"


def test_only_the_newest_notice_is_kept_and_only_the_publisher_is_heard(
        monkeypatch, tmp_path):
    """A notice is a nudge, not a record.

    It used to be a board post, and a whole channel was re-read every pass:
    a node coming fresh to two notices recorded the OLDER one as seen, and a
    channel with several could make every pass find something "new" (D-087,
    D-093). One row, overwritten by the scanner, ends both -- and the sender
    is still checked, because anybody may broadcast (D-147).
    """
    import json as jsonlib

    from arcade.web.watcher import BlockWatcher

    state = FakeState(FakeChain(tip=100), FakeChain("main", tip=5))
    watcher = BlockWatcher(state)
    said = {"value": jsonlib.dumps(
        {"revision": "2222333", "from": "nPub", "height": 3})}

    class Store:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_meta(self, key, default=None):
            return said["value"] if key == "release_notice" else default

    monkeypatch.setattr(watcher, "_release_publisher", lambda: "nPub")
    monkeypatch.setattr(type(state), "store", lambda self: Store(), raising=False)
    watcher._update_checked = 999999.0
    watcher._check_release_notices()
    assert watcher._release_seen == "2222333"
    assert watcher._update_checked == 0.0, "and it asks the site now"

    # The same one again changes nothing: acted on once.
    watcher._update_checked = 999999.0
    watcher._check_release_notices()
    assert watcher._update_checked == 999999.0

    # And somebody who does not hold the release tag is not telling us
    # about a release, whatever they broadcast.
    said["value"] = jsonlib.dumps(
        {"revision": "9999999", "from": "nImpostor", "height": 4})
    watcher._check_release_notices()
    assert watcher._release_seen == "2222333"
    assert watcher._update_checked == 999999.0


def test_a_successful_update_is_not_read_from_the_exit_status(monkeypatch, tmp_path):
    """The updater restarts arcade-web, systemd stops this process in the same
    cgroup, and the subprocess is killed mid-flight -- so a SUCCESSFUL update
    returns non-zero and its benign progress output is logged as the error.
    The BOXA caught it on the one run that actually worked.

    What is on disk now is what happened (D-088).
    """
    from arcade.web.watcher import BlockWatcher

    state = FakeState(FakeChain(tip=100), FakeChain("main", tip=5))
    watcher = BlockWatcher(state)
    monkeypatch.setattr(type(state), "setting",
                        lambda self, name, default=None: True, raising=False)

    checks = iter([("old1234", "new5678", True),      # before
                   ("new5678", "new5678", False)])    # after, on disk
    monkeypatch.setattr("arcade.web.watcher.update.check", lambda: next(checks))

    class Killed:
        returncode = -15
        stdout = "Checking cloudflared\n  already installed"
        stderr = ""

    monkeypatch.setattr("arcade.web.watcher.subprocess.run", lambda *a, **k: Killed())
    watcher._auto_update()
    assert state.update_status["what"] == "installed", \
        "killed by the restart it caused is not a failure"
    assert state.update_status["installed"] == "new5678"


def test_an_update_that_really_failed_still_says_so(monkeypatch):
    from arcade.web.watcher import BlockWatcher

    state = FakeState(FakeChain(tip=100), FakeChain("main", tip=5))
    watcher = BlockWatcher(state)
    monkeypatch.setattr(type(state), "setting",
                        lambda self, name, default=None: True, raising=False)
    checks = iter([("old1234", "new5678", True), ("old1234", "new5678", True)])
    monkeypatch.setattr("arcade.web.watcher.update.check", lambda: next(checks))

    class Broke:
        returncode = 1
        stdout = ""
        stderr = "pip could not build a wheel"

    monkeypatch.setattr("arcade.web.watcher.subprocess.run", lambda *a, **k: Broke())
    watcher._auto_update()
    assert state.update_status["what"] == "the update did not take"
    assert "wheel" in state.update_status["error"], "and what it said"

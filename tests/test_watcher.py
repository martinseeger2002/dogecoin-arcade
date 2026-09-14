"""The block watcher: scan when a block lands, and never die doing it.

Messages only become visible once they are in a block, and the failure this
prevents is specific and bad -- a correspondent says "I sent it", the recipient
is looking straight at the conversation, and nothing is there because the
interface is not looking.
"""

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
    assert "self._confirm_posts()" in source
    assert "self._confirm_sent()" in source


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

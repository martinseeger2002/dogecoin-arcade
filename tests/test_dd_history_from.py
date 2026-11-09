"""How far back an account's DigiDollar history reaches (2026-11-09, security review): `/account/digidollar` says the
block it is complete from (`from_block`, 0 for all of it) and where what came before is read from (`earlier`), so an
arcade that borrows DigiByte says "history from block N" rather than a bare `whole: false` that read like missing
coins. `whole` stays for older pages."""
import pathlib
import re
import sys
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_account_claim import _sign_in                          # noqa: E402
import signup_proof as SP                                         # noqa: E402

from arcade import digidollar                                     # noqa: E402
from arcade.config import NETWORKS                                # noqa: E402
from arcade.ledger import LedgerIndex                             # noqa: E402
from arcade.script import b58check_encode, hash160                # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
TIP = 500


class _Core:
    """A DigiByte test chain Core with no DigiDollars in its coin set."""

    def __init__(self):
        self.asked = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def call(self, method, *params):
        self.asked.append(method)
        if method == "scantxoutset":
            return {"height": TIP, "unspents": []}
        raise AssertionError(f"{method} was not expected")


def _dgbtest(state, monkeypatch, *, borrowed):
    from arcade.web.state import ChainContext
    chain = ChainContext(network="dgbtest", role="ledger", label="DigiByte testnet",
                         conf=pathlib.Path("/nonexistent/digibyte.conf"), borrowed=borrowed)
    monkeypatch.setattr(state, "more", {"dgbtest": chain}, raising=False)
    said = {"ready": True, "progress": 1.0, "online": True, "blocks": TIP, "headers": TIP}
    monkeypatch.setattr(type(state), "chain_sync", lambda self, c: said)
    monkeypatch.setattr(LedgerIndex, "indexed_height", lambda self: TIP)
    core = _Core()
    object.__setattr__(chain, "rpc", lambda: core)
    return chain, core


def _ask(app, state, net="dgbtest"):
    pubkey = _sign_in(app)
    _priv, pub = SP.coin_key()
    state.set_setting(f"address:{net}:{pubkey}", b58check_encode(NETWORKS[net].pubkeyhash_version, hash160(pub)))
    return lambda: app.get(f"/account/digidollar?chain={net}&pubkey={pub.hex()}")


def test_a_history_is_complete_from_block_zero_once_its_earlier_coins_are_read():
    assert digidollar.from_block({"since": 500, "seeded": False}) == 501, "watched at 500: on_block reads from 501"
    assert digidollar.from_block({"since": 500, "seeded": True}) == 0


def test_a_borrowed_digibyte_says_the_block_its_history_starts_at(client, monkeypatch):
    """Here the operator said no to asking the sharer (`ask_coinscan` false): nothing reads what came before."""
    app, state = client
    state.set_setting("ask_coinscan", False)
    _dgbtest(state, monkeypatch, borrowed=True)
    got = _ask(app, state)().json()
    assert got["whole"] is False, "kept for older pages"
    assert got["from_block"] == TIP + 1 and got["earlier"] == "", got


def test_a_digibyte_run_here_reads_what_came_before_then_says_complete(client, monkeypatch):
    app, state = client
    _chain, core = _dgbtest(state, monkeypatch, borrowed=False)
    ask = _ask(app, state)
    real = threading.Thread
    monkeypatch.setattr(threading, "Thread", lambda target=None, **kw: type("T", (), {"start": lambda self: target()})()
                        if kw.get("name") == "digidollar seed" else real(target=target, **kw))
    first = ask().json()
    assert first["earlier"] == "core" and first["from_block"] == TIP + 1, first
    assert "scantxoutset" in core.asked, "its own Core's coin set read"
    again = ask().json()
    assert again["whole"] is True and again["from_block"] == 0 and again["earlier"] == "", again


def test_both_wallets_say_the_history_the_same_way():
    """The full Wallet and ArcadeCore's say "History complete." or "History from block N", never the old "Still
    reading" for a history that nobody reads (a borrowed DigiByte)."""
    said = []
    for page in ("arcade/web/templates/my_wallet.html", "arcade/lite/templates/_wallet.html"):
        text = (ROOT / page).read_text()
        assert "Still reading what came before" not in text, page
        fn = re.search(r"\nfunction ddHistory\(got\) \{\n(.*?)\n\}\n", text, re.S)
        assert fn, page
        said.append(fn.group(1))
        assert "got.from_block" in fn.group(1) and "got.earlier" in fn.group(1)
    assert said[0] == said[1], "one wording in both looks"
    lite = (ROOT / "arcade/lite/templates/_wallet.html").read_text()
    assert "from_block: j.from_block, earlier: j.earlier" in lite, "ArcadeCore's wallet passes them on"

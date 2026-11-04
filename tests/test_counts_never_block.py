"""A page is never held up by its red counts (2026-11-04: app.dogecoinarcade.com's /feed hung 48-74 s for a signed-in
account, its notification count waiting on the ledger's trade list after a block; the operator: "Why can't I reach
app.dogecoinarcade.com", "Yes please fix them").

The count gets COUNTS_WAIT of the page's time; past that the page is drawn with the last counts known for that
account, and the count finishes on its own for the next page and /account/badges."""
import pathlib
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_me_page import _seat                                   # noqa: E402

from arcade import notify                                        # noqa: E402


def test_a_slow_count_never_holds_a_page_and_lands_for_the_next(client, monkeypatch):
    app, state = client
    _seat(app)
    assert app.get("/account/badges").status_code == 200              # counted once, quickly: the last known
    real = notify.feed_events
    gate, entered = threading.Event(), []

    def slow(*a, **kw):
        entered.append(1)
        gate.wait(8)                                                    # a read behind a busy disk
        return real(*a, **kw)
    monkeypatch.setattr(notify, "feed_events", slow)
    t = time.monotonic()
    page = app.get("/feed")
    took = time.monotonic() - t
    assert entered, "the count read what it reads"
    assert page.status_code == 200 and took < 6, f"the page waited {took:.1f} s for its counts"
    t = time.monotonic()
    assert app.get("/account/badges").status_code == 200 and time.monotonic() - t < 6, "joined the one count, not stuck"
    gate.set()                                                          # the read comes back: the count lands
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if app.get("/account/badges").status_code == 200:
            break
    monkeypatch.setattr(notify, "feed_events", real)
    assert app.get("/feed").status_code == 200

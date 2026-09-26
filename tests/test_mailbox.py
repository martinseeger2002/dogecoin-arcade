"""An account's own message history, kept sealed on the node (accounts.Mailbox).

What the node must get right is small and all of it is here: only the account
itself reads or writes its copy, a stranger reaches neither route, a copy
written by another device in between is refused rather than overwritten, and
nothing too big is kept. What is IN the copy is the browser's business: it is
sealed with a key this node never sees (messaging.js syncMailbox).
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_me_page import EDGE, _seat, public                    # noqa: F401,E402

from arcade import accounts                                      # noqa: E402


def test_an_account_keeps_and_reads_back_its_own_copy(public):
    app, _ = public
    assert app.get("/account/mailbox", headers=EDGE).status_code == 403
    _seat(app)
    first = app.get("/account/mailbox", headers=EDGE).json()
    assert first == {"blob": "", "updated": 0}
    put = app.post("/account/mailbox", headers=EDGE,
                   json={"blob": "c2VhbGVk", "base": 0})
    assert put.status_code == 200, put.text
    back = app.get("/account/mailbox", headers=EDGE).json()
    assert back["blob"] == "c2VhbGVk" and back["updated"] == put.json()["updated"]


def test_a_copy_written_by_another_device_is_not_overwritten(public):
    app, _ = public
    _seat(app)
    one = app.post("/account/mailbox", headers=EDGE, json={"blob": "YQ==", "base": 0})
    assert one.status_code == 200
    stale = app.post("/account/mailbox", headers=EDGE, json={"blob": "Yg==", "base": 0})
    assert stale.status_code == 409, "merged from an older copy: merge again"
    assert stale.json()["updated"] == one.json()["updated"]
    fresh = app.post("/account/mailbox", headers=EDGE,
                     json={"blob": "Yg==", "base": one.json()["updated"]})
    assert fresh.status_code == 200
    assert fresh.json()["updated"] > one.json()["updated"]


def test_a_copy_too_big_to_keep_is_refused(public):
    app, _ = public
    _seat(app)
    huge = "A" * (accounts.MAILBOX_MAX + 1)
    said = app.post("/account/mailbox", headers=EDGE, json={"blob": huge, "base": 0})
    assert said.status_code == 400 and "at most" in said.json()["detail"]

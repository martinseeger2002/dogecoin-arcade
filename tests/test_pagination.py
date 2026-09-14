"""Only what is being looked at.

A busy channel and a chain full of inscriptions are both things that grow
without limit. Loading all of either to draw the last twenty is slow on this
machine and hopeless on a phone over a tunnel.
"""

import pathlib
import sys

import pytest

from arcade.messaging.store import MessageStore

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402


@pytest.fixture
def busy(tmp_path):
    store = MessageStore(tmp_path / "m.sqlite")
    for n in range(120):
        store.add_group_post("test", "main", f"tx{n}", 1000 + n, 5000 + n,
                             "nThem", "Them", f"post {n}")
    return store


def test_a_channel_hands_back_one_page(busy):
    page = busy.group_posts("test", "main", limit=40)
    assert len(page) == 40
    assert [p["text"] for p in page[-3:]] == ["post 117", "post 118", "post 119"], (
        "the newest are the ones on the first page, in reading order")


def test_older_pages_walk_backwards_without_a_seam(busy):
    """An id, not a block time: two posts can share a time, and a page that
    overlaps or skips at the seam is worse than no paging at all."""
    seen, cursor = [], None
    for _ in range(3):
        page = busy.group_posts("test", "main", limit=40, before_id=cursor)
        assert page, "there should be three full pages"
        seen.extend(p["text"] for p in page)
        cursor = page[0]["id"]
    assert len(seen) == 120
    assert len(set(seen)) == 120, "nothing repeated at a seam"
    assert busy.group_posts("test", "main", limit=40, before_id=cursor) == []


def test_a_channel_knows_whether_there_is_more(busy):
    page = busy.group_posts("test", "main", limit=40)
    assert busy.group_has_older("test", "main", page[0]["id"]) is True
    oldest = busy.group_posts("test", "main", limit=200)[0]
    assert busy.group_has_older("test", "main", oldest["id"]) is False
    assert busy.group_post_count("test", "main") == 120


def test_a_conversation_draws_its_end_not_its_whole_history(tmp_path):
    from arcade.messaging.keys import Identity

    store = MessageStore(tmp_path / "m.sqlite")
    me = Identity.generate()
    peer = b"\x33" * 32
    for n in range(200):
        store.add_sent(f"tx{n}", peer, "", me.fingerprint, f"message {n}".encode())

    everything = store.thread(me.fingerprint, peer)
    assert len(everything) == 200

    page = store.thread(me.fingerprint, peer, limit=60)
    assert len(page) == 60
    assert page[-1]["body"] == everything[-1]["body"], "the end, not the start"
    assert page == everything[-60:]
    store.close()


def test_inscriptions_are_paged_by_number(tmp_path):
    """Numbered, so a page number is a place somebody can mean to go."""
    from arcade.config import NETWORKS
    from arcade.db import Database
    from arcade.ledger import LedgerIndex
    from arcade.state import install_schema

    path = tmp_path / "l.sqlite"
    db = Database(path)
    install_schema(db)
    for n in range(50):
        db.conn.execute(
            "INSERT INTO inscription(txid,number,creator,owner,block_height,"
            "position,content_type,content_len,sha256,json,chunks) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (f"{n:064x}", n, "nMe", "nMe", 100 + n, 0, "text/plain", 10,
             "ab" * 32, "", 1))
    db.conn.commit()
    db.close()

    index = LedgerIndex(path, NETWORKS["regtest"], rpc_factory=lambda: None)
    assert index.inscription_count() == 50

    # Yours is asked of the whole index, not filtered out of the page on
    # show: the wallet's own pieces are usually the old ones (D-028).
    db = Database(path)
    db.conn.execute("UPDATE inscription SET owner='nMine' WHERE number IN (0, 1, 49)")
    db.conn.commit()
    db.close()
    index = LedgerIndex(path, NETWORKS["regtest"], rpc_factory=lambda: None)
    assert index.inscription_count(owners=["nMine", "nElse"]) == 3
    assert [r["number"] for r in index.inscriptions(owners=["nMine"], limit=24)] == [49, 1, 0]
    assert index.inscriptions(owners=[]) == [] and index.inscription_count(owners=[]) == 0

    first = index.inscriptions(limit=24, offset=0)
    second = index.inscriptions(limit=24, offset=24)
    last = index.inscriptions(limit=24, offset=48)
    assert [r["number"] for r in first[:2]] == [49, 48], "newest first"
    assert len(first) == 24 and len(second) == 24 and len(last) == 2
    assert not ({r["number"] for r in first} & {r["number"] for r in second})


def test_the_page_offers_numbers_and_a_way_to_jump(client):
    """A cursor makes page 40 mean walking 39 pages to find it."""
    app, _ = client
    # Nothing to page through on an empty index, and a pager drawn over one
    # page would be furniture. The page still renders.
    assert app.get("/inscriptions").status_code == 200
    assert app.get("/inscriptions?page=7").status_code == 200

    source = pathlib.Path("arcade/web/templates/inscriptions.html").read_text()
    assert "macro pager" in source
    assert 'name="page"' in source, "a box to jump with"
    assert "/inscriptions?page={{ pages }}" in source, "and a way to the last one"
    assert "{% if pages > 1 %}" in source, "and nothing at all when there is one"


def test_the_board_offers_a_way_back_through_older_posts(client):
    app, _ = client
    source = __import__("pathlib").Path(
        "arcade/web/templates/groups.html").read_text()
    assert "Older posts" in source and "before={{ posts[0].id }}" in source
    assert "Back to the newest" in source
    assert "Back to the newest" in app.get("/groups?channel=main&before=5").text

"""What the nav badges count, and what clears them.

Three different questions wear the same red circle. A private message is
addressed to you and has a read mark of its own. A public post is addressed to
nobody, so "unread" can only mean "since you last looked". An offer is neither:
it is waiting for a decision, and it stops waiting when you make one.
"""

from pathlib import Path

from arcade.messaging.store import MessageStore


def a_store(tmp_path: Path) -> MessageStore:
    return MessageStore(tmp_path / "m.sqlite")


def test_the_board_counts_what_arrived_since_you_looked(tmp_path):
    store = a_store(tmp_path)
    assert store.board_unread("test") == 0, "an empty board is not unread"

    for n in range(3):
        store.add_group_post("test", "main", f"{n:064x}", 100 + n, 1700 + n,
                             "nSomebody", "them", f"post {n}")
    assert store.board_unread("test") == 3

    store.mark_board_read("test")
    assert store.board_unread("test") == 0, "looking at it is reading it"

    store.add_group_post("test", "main", "f" * 64, 110, 1800, "nSomebody", "them", "later")
    assert store.board_unread("test") == 1, "only what came after the mark"


def test_your_own_posts_are_not_unread(tmp_path):
    """The count is for what somebody else said."""
    store = a_store(tmp_path)
    store.add_group_post("test", "main", "a" * 64, 100, 1700, "nMe", "me", "mine",
                         mine=True)
    assert store.board_unread("test") == 0
    store.add_group_post("test", "main", "b" * 64, 101, 1701, "nYou", "you", "yours")
    assert store.board_unread("test") == 1


def test_each_chain_is_counted_on_its_own(tmp_path):
    store = a_store(tmp_path)
    store.add_group_post("test", "main", "a" * 64, 100, 1700, "nYou", "you", "t")
    store.add_group_post("main", "main", "b" * 64, 100, 1700, "nYou", "you", "m")
    assert store.board_unread("test") == 1
    assert store.board_unread("main") == 1
    store.mark_board_read("test")
    assert store.board_unread("test") == 0
    assert store.board_unread("main") == 1, "reading one board is not reading the other"


def test_each_channel_says_what_is_new_in_it(tmp_path):
    """A list of channels that only says the board has something new makes
    somebody open all of them (D-108)."""
    store = a_store(tmp_path)
    for n, (channel, mine) in enumerate([("trading", False), ("trading", False),
                                         ("releases", False), ("chat", True)]):
        store.add_group_post("test", channel, f"{n:064x}", 100 + n, 1700 + n,
                             "nThem", "them", "hello", mine=mine)

    rows = {r["channel"]: r for r in store.group_channels("test")}
    assert rows["trading"]["unread"] == 2
    assert rows["releases"]["unread"] == 1
    assert rows["chat"]["unread"] == 0, "your own posts are not news to you"

    # Opening one silences that one and leaves the others alone.
    store.mark_channel_read("test", "trading")
    rows = {r["channel"]: r for r in store.group_channels("test")}
    assert rows["trading"]["unread"] == 0 and rows["releases"]["unread"] == 1

    # A channel nobody has opened falls back to the board's own mark, so an
    # update does not light up what was already read.
    store.mark_board_read("test")
    rows = {r["channel"]: r for r in store.group_channels("test")}
    assert all(r["unread"] == 0 for r in rows.values())

    # And something new after that is new again, in its own channel only.
    store.add_group_post("test", "releases", "e" * 64, 200, 1800, "nThem",
                         "them", "arcade-release abcdef0")
    rows = {r["channel"]: r for r in store.group_channels("test")}
    assert rows["releases"]["unread"] == 1 and rows["trading"]["unread"] == 0

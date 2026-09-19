"""What the page draws, decided without a page.

Rows in, display out: every rule about edits, deletes, threads, likes and
muting is arithmetic over two tables and is tested as arithmetic (D-138).
"""

from arcade import feedview
from arcade.messaging import feed

ME = "nMe"
THEM = "nThem"


def post(txid, author=THEM, text="hello", height=100, mine=False):
    return {"txid": txid, "sender": author, "text": text, "height": height,
            "block_time": height * 10, "mine": 1 if mine else 0}


def act(txid, kind, target, author=ME, text="", height=200):
    return {"txid": txid, "kind": kind, "target": target, "author": author,
            "text": text, "height": height, "block_time": height * 10, "mine": 0}


class Row(dict):
    """A stand-in for a sqlite3.Row: the view asks what keys it has."""

    def keys(self):
        return list(super().keys())


def rows(*items):
    return [Row(item) for item in items]


def test_a_post_with_nothing_done_to_it_is_itself():
    (shown,) = feedview.assemble(rows(post("a" * 64)), [])
    assert shown.text == "hello" and shown.likes == 0 and not shown.edited
    assert shown.replies == [] and not shown.deleted


def test_likes_are_counted_once_per_person_and_the_last_word_wins():
    """A like and an unlike from one person cancel; two people are two."""
    acts = rows(
        act("1" * 64, feed.LIKE, "a" * 64, author=ME, height=200),
        act("2" * 64, feed.LIKE, "a" * 64, author=ME, height=201),
        act("3" * 64, feed.LIKE, "a" * 64, author="nOther", height=202),
    )
    (shown,) = feedview.assemble(rows(post("a" * 64)), acts, me=ME)
    assert shown.likes == 2 and shown.liked_by_me

    acts.append(Row(act("4" * 64, feed.UNLIKE, "a" * 64, author=ME, height=203)))
    (shown,) = feedview.assemble(rows(post("a" * 64)), acts, me=ME)
    assert shown.likes == 1 and not shown.liked_by_me


def test_only_the_author_may_edit_or_delete():
    """An edit by somebody else is a stranger writing in your mouth."""
    acts = rows(
        act("1" * 64, feed.EDIT, "a" * 64, author=ME, text="I never said this"),
        act("2" * 64, feed.DELETE, "a" * 64, author=ME),
    )
    (shown,) = feedview.assemble(rows(post("a" * 64, author=THEM)), acts)
    assert shown.text == "hello", "their words, not the impostor's"
    assert shown.deleted is False


def test_the_authors_newest_edit_is_what_is_shown():
    acts = rows(
        act("1" * 64, feed.EDIT, "a" * 64, author=THEM, text="second", height=300),
        act("2" * 64, feed.EDIT, "a" * 64, author=THEM, text="third", height=400),
    )
    (shown,) = feedview.assemble(rows(post("a" * 64)), acts)
    assert shown.text == "third" and shown.edited


def test_a_deleted_post_is_not_drawn_at_all():
    acts = rows(act("1" * 64, feed.DELETE, "a" * 64, author=THEM))
    assert feedview.assemble(rows(post("a" * 64)), acts) == []


def test_replies_thread_by_what_they_point_at():
    """A reply's parent may be a post or another reply, and nothing in the
    format knows the difference."""
    acts = rows(
        act("b" * 64, feed.REPLY, "a" * 64, author=ME, text="first reply"),
        act("c" * 64, feed.REPLY, "b" * 64, author=THEM, text="reply to that"),
        act("d" * 64, feed.REPLY, "a" * 64, author=ME, text="another on the post"),
    )
    (shown,) = feedview.assemble(rows(post("a" * 64)), acts)
    assert [r.text for r in shown.replies] == ["first reply", "another on the post"]
    assert [r.text for r in shown.replies[0].replies] == ["reply to that"]
    assert shown.replies[0].is_reply


def test_a_reply_can_be_liked_and_deleted_like_anything_else():
    acts = rows(
        act("b" * 64, feed.REPLY, "a" * 64, author=THEM, text="a comment"),
        act("c" * 64, feed.LIKE, "b" * 64, author=ME),
        act("d" * 64, feed.REPLY, "a" * 64, author=ME, text="doomed"),
        act("e" * 64, feed.DELETE, "d" * 64, author=ME),
    )
    (shown,) = feedview.assemble(rows(post("a" * 64)), acts, me=ME)
    assert len(shown.replies) == 1, "the deleted one is not drawn"
    assert shown.replies[0].likes == 1 and shown.replies[0].liked_by_me


def test_muting_hides_the_words_and_keeps_the_count_honest():
    """A gap where a post was is how somebody learns they were muted, and a
    like is a number everybody else can see."""
    acts = rows(act("1" * 64, feed.LIKE, "a" * 64, author="nOther"))
    (shown,) = feedview.assemble(rows(post("a" * 64, author=THEM)), acts,
                                 muted={THEM})
    assert shown.muted and shown.text == ""
    assert shown.likes == 1, "counts are public arithmetic, not a local opinion"


def test_shares_and_tips_are_counted_on_the_post():
    acts = rows(
        act("1" * 64, feed.SHARE, "a" * 64, author=ME),
        act("2" * 64, feed.TIP, "a" * 64, author="nOther"),
        act("3" * 64, feed.TIP, "a" * 64, author=ME),
    )
    (shown,) = feedview.assemble(rows(post("a" * 64)), acts)
    assert shown.shares == 1 and shown.tips == 2


def test_a_share_carries_the_original_rather_than_copying_it():
    """So an edit or a delete by the author reaches every feed it reached."""
    original = Row(post("a" * 64, author=THEM, text="the original"))
    shared = feedview.shares_as_posts(
        rows(act("1" * 64, feed.SHARE, "a" * 64, author=ME, text="look at this")),
        {"a" * 64: original})
    assert len(shared) == 1
    assert shared[0]["post"] is original and shared[0]["by"] == ME
    assert shared[0]["note"] == "look at this"


def test_a_share_of_a_post_nobody_has_read_is_left_out():
    assert feedview.shares_as_posts(
        rows(act("1" * 64, feed.SHARE, "a" * 64)), {}) == []


def test_two_actions_in_one_block_resolve_the_same_way_everywhere():
    """Height ties break on txid, because "whichever the database returned
    first" is not an order (D-122)."""
    acts = rows(
        act("f" * 64, feed.EDIT, "a" * 64, author=THEM, text="f wins", height=300),
        act("0" * 64, feed.EDIT, "a" * 64, author=THEM, text="0 loses", height=300),
    )
    (shown,) = feedview.assemble(rows(post("a" * 64)), acts)
    assert shown.text == "f wins"
    (again,) = feedview.assemble(rows(post("a" * 64)), list(reversed(acts)))
    assert again.text == "f wins", "and the same however the rows arrive"

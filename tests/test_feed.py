"""What people do to each other's posts, on the chain.

One message type with a kind byte rather than six types with six parsers:
one shape, one table, and a kind a reader does not know is ignored rather
than fatal (D-138).
"""

import pytest

from arcade.messaging import feed
from arcade.messaging.envelope import EnvelopeError

POST = "ab" * 32


def test_a_like_is_small_enough_to_be_worth_sending():
    """Every like is a transaction, so its size is its price. A bare target
    and a kind is all it can be."""
    payload = feed.build(feed.LIKE, POST)
    assert len(payload) == 39, "six of header, one of kind, thirty-two of target"
    act = feed.parse(payload)
    assert act.kind == feed.LIKE and act.target_hex == POST and act.text == ""


def test_every_kind_survives_the_round_trip():
    for kind in feed.KINDS:
        text = "something" if kind in feed.WITH_TEXT else ""
        act = feed.parse(feed.build(kind, POST, text))
        assert act.kind == kind and act.target_hex == POST and act.text == text
        assert act.name == feed.NAMES[kind]


def test_a_target_is_any_transaction_which_is_what_makes_threads_work():
    """A comment's parent is a comment, and nothing in the format knows the
    difference: the target of a reply is a txid, whatever that txid is."""
    comment = "cd" * 32
    act = feed.parse(feed.build(feed.REPLY, comment, "replying to the reply"))
    assert act.target_hex == comment


def test_what_cannot_speak_is_refused_rather_than_carrying_words():
    """A like that could say something would be two things at once, and the
    reader would have to decide which."""
    for kind in (feed.LIKE, feed.UNLIKE, feed.DELETE, feed.TIP):
        with pytest.raises(feed.FeedError):
            feed.build(kind, POST, "but what about")


def test_a_comment_too_long_is_refused_not_trimmed():
    """Truncating here would be silent, permanent and paid for."""
    with pytest.raises(feed.FeedError) as complaint:
        feed.build(feed.REPLY, POST, "x" * (feed.MAX_TEXT + 1))
    assert str(feed.MAX_TEXT) in str(complaint.value)


def test_a_target_that_is_not_a_transaction_is_refused():
    for bad in ("", "ab", "zz" * 32, b"\x01" * 31):
        with pytest.raises(feed.FeedError):
            feed.build(feed.LIKE, bad)


def test_class_b_padding_does_not_become_part_of_a_comment():
    """Omni does not strip the NULs Class B pads with, so a comment would
    otherwise arrive with a tail of nothing attached to it."""
    payload = feed.build(feed.REPLY, POST, "nice one")
    assert feed.parse(payload + b"\x00" * 29).text == "nice one"


def test_a_kind_nobody_knows_is_refused_and_not_guessed_at():
    """A reader that invents a meaning for an unknown kind is a reader that
    can be told to do something nobody defined."""
    payload = bytearray(feed.build(feed.LIKE, POST))
    payload[6] = 99
    with pytest.raises(EnvelopeError):
        feed.parse(bytes(payload))


def test_something_that_is_not_a_feed_action_is_not_read_as_one():
    from arcade.messaging import group

    post = group.build(group.GroupPost(text="hello", channel="", nickname="me"))
    assert not feed.is_feed_act(post)
    with pytest.raises(EnvelopeError):
        feed.parse(post)


def test_the_cheap_check_agrees_with_the_parser():
    """The ledger index sees every arcade payload on a chain and has to say
    "not mine" without running a parser."""
    assert feed.is_feed_act(feed.build(feed.TIP, POST))
    for other in (b"", b"arcm", b"notarcm" + b"\x00" * 40,
                  feed.build(feed.LIKE, POST)[:20]):
        assert not feed.is_feed_act(other)

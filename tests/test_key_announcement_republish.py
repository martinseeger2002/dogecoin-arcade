"""A changed face, bio or link can be republished; an identical one cannot."""
import inspect

from arcade.web import app as webapp
from arcade.web.app import announcement_changed

PRIOR = {"pfp": "abc123i0", "bio": "much wow", "url": "https://x.com/b"}


def test_identical_profile_is_not_a_change():
    assert not announcement_changed(PRIOR, "abc123i0", "much wow", "https://x.com/b")


def test_none_and_empty_are_the_same():
    assert not announcement_changed({}, None, "", None)
    assert not announcement_changed(None, "", None, "")


def test_new_face_bio_or_url_is_a_change():
    assert announcement_changed(PRIOR, "def456i0", "much wow", "https://x.com/b")
    assert announcement_changed(PRIOR, "abc123i0", "such bio", "https://x.com/b")
    assert announcement_changed(PRIOR, "abc123i0", "much wow", "https://doge.x")
    assert announcement_changed({}, "abc123i0", "", "")


def test_refusal_is_gated_on_the_change_check():
    src = inspect.getsource(webapp.create_app)
    assert "if existing and not claimed and not _changed:" in src


def test_a_change_is_judged_against_the_chain_s_newest_announcement_not_the_store():
    """After a reset the store is empty: the same face, bio and link as the chain's newest announcement is still a
    repeat (refused, no second fee); one that differs from it is new. Only the newest counts."""
    from arcade.messaging.envelope import build_key_announcement, parse_announced_extras
    from arcade.web.app import announcement_is_new
    key, home = b"\x11" * 32, b"\x22" * 20
    face = "ab" * 32

    def ann(pfp="", bio="", url=""):
        return build_key_announcement(key, home, "", tag="billy", pfp=pfp, bio=bio, url=url)

    def on_chain(*payloads):
        return [("tx%d" % i, "nX", 100 + i, 0, True, "billy", parse_announced_extras(p)) for i, p in enumerate(payloads)]

    same = ann(face, "much wow", "https://x.com/b")
    assert not announcement_is_new(on_chain(same), same), "a repeat of what is on the chain"
    assert announcement_is_new(on_chain(same), ann("cd" * 32, "much wow", "https://x.com/b")), "a new face"
    assert announcement_is_new(on_chain(same), ann(face, "such bio", "https://x.com/b")), "a new bio"
    assert not announcement_is_new(on_chain(ann(), same), same), "the newest is what counts"
    assert announcement_is_new(on_chain(same, ann()), same), "a face taken off, then put back"
    assert not announcement_is_new([], same), "nothing on the chain: the first publish, not a change"

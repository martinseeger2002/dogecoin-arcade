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

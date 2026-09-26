"""A profile updated twice in one block shows the second update.

Found live, 2026-09-25: a picture, then a bio a minute later, both landed in
block 1,509,391. They tied on height and key_for returned the first, so the
profile showed the picture and lost the bio and the link.
"""

from arcade.messaging.store import MessageStore


def test_the_later_of_two_announcements_in_one_block_is_the_profile(tmp_path):
    store = MessageStore(tmp_path / "m.sqlite")
    key = b"\x11" * 32
    # txids chosen so the LATER one sorts first alphabetically, as it did live
    store.add_key_announcement("77" * 32, "nMe", key, "ff", 500, 5000, stated=True,
                               tag="me", pfp="ab" * 32)
    store.add_key_announcement("53" * 32, "nMe", key, "ff", 500, 5000, stated=True,
                               tag="me", pfp="ab" * 32, bio="hello", url="https://x.example")
    said = store.key_for("nMe")
    assert (said["bio"], said["url"]) == ("hello", "https://x.example")
    assert store.confirmed_key_for("nMe")["bio"] == "hello"
    assert [r["txid"][:2] for r in store.key_history("nMe")] == ["77", "53"]

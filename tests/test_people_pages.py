"""The address book and profile redesign (2026-10-08, the prototype the operator
approved: "I like it"; "things need to be clean and intuitive").

What the pages draw is checked in a browser elsewhere; this file is about the
facts under them: a profile's pieces counted by collection in one query, one
collection paged sixty at a time, when somebody joined, and how many posts
they have -- all of it the chain's, read from this node's indexes.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_collection_web import index_with_a_collection          # noqa: E402

from arcade.messaging.store import MessageStore                   # noqa: E402


def _index(tmp_path, count=5):
    from arcade.config import NETWORKS
    from arcade.ledger import LedgerIndex

    return LedgerIndex(index_with_a_collection(tmp_path, count), NETWORKS["main"],
                       rpc_factory=lambda: None)


def test_what_somebody_holds_is_counted_by_collection(tmp_path):
    index = _index(tmp_path)
    held = index.collections_held("nMe")
    assert [(c["collection"], c["count"]) for c in held] == [("Doge Punks", 5)]
    # A cover is pictures only, newest first, four at most.
    assert len(held[0]["covers"]) == 4
    assert held[0]["newest"] == 105


def test_pieces_in_no_collection_come_back_under_an_empty_name_and_last(tmp_path):
    index = _index(tmp_path)
    assert [(c["collection"], c["count"]) for c in index.collections_held("nYou")] \
        == [("", 1)]
    assert index.collections_held("nobody") == []


def test_one_collection_is_paged_by_itself(tmp_path):
    index = _index(tmp_path, count=7)
    first = index.inscriptions(owner="nMe", collection="Doge Punks", limit=3)
    rest = index.inscriptions(owner="nMe", collection="Doge Punks", limit=3, offset=3)
    assert len(first) == 3 and len(rest) == 3
    assert not {r["txid"] for r in first} & {r["txid"] for r in rest}
    assert all(r["collection"] == "Doge Punks" for r in first + rest)
    loose = index.inscriptions(owner="nYou", collection="")
    assert [r["txid"] for r in loose] == ["f" * 64]
    assert index.inscriptions(owner="nMe", collection="") == []


def test_joined_is_the_first_announcement_in_a_block(tmp_path):
    store = MessageStore(tmp_path / "m.sqlite")
    key = b"\x11" * 32
    # Seen in the pool first: no block time yet, so it does not count.
    store.add_key_announcement("11" * 32, "nMe", key, "ff", 0, 0, stated=True, tag="me")
    assert store.first_announced("nMe") == 0
    store.add_key_announcement("22" * 32, "nMe", key, "ff", 600, 7000, stated=True, tag="me")
    store.add_key_announcement("33" * 32, "nMe", key, "ff", 500, 5000, stated=True, tag="me")
    assert store.first_announced("nMe") == 5000
    assert store.first_announced("nSomebodyElse") == 0


def test_a_profile_counts_its_authors_posts_and_nobody_elses(tmp_path):
    store = MessageStore(tmp_path / "m.sqlite")
    for n, who in enumerate(["nMe", "nMe", "nYou", "nMe"]):
        store.add_group_post("test", "main", f"{n:064x}", 100 + n, 1000 + n, who, "", "hi")
    store.add_group_post("main", "main", "e" * 64, 100, 1000, "nMe", "", "other chain")
    assert store.feed_post_count("test", "nMe") == 3
    assert store.feed_post_count("test", "nYou") == 1
    assert store.feed_post_count("test", "nobody") == 0

"""A collection's maximum supply and its details, set by its #1 (2026-09-25).

0 or none is a set with no limit ("of ∞"); a number seals it. A second batch added
to a set already on the chain moves up to follow it."""

import json
import pathlib

from arcade import collections as C
from arcade import inscriptions as I


def build(n=3, start=1, name="Skull Squad"):
    items = [C.Item(edition=e, name=f"{name} #{e}",
                    json=C.compact({"name": f"{name} #{e}", "edition": e,
                                    "description": "skulls", "attributes": []}),
                    image=f"{e}.png", content_type="image/png", size=100)
             for e in range(start, start + n)]
    return C.Build(folder=pathlib.Path("."), items=items, collection=name)


def first_json(b):
    return min(b.items, key=lambda i: i.edition).json


def test_zero_is_no_limit():
    b = C.with_details(build(), {"supply": 0, "url": "https://skulls.example"})
    said = I.collection_details(first_json(b))
    assert "supply" not in said and said["url"] == "https://skulls.example"
    assert "supply" not in json.loads(first_json(b))["collection"]


def test_a_number_seals_the_set():
    b = C.with_details(build(), {"supply": 100})
    assert I.collection_details(first_json(b))["supply"] == 100


def test_saying_nothing_still_seals_at_the_build_size():
    """The local wizard's behaviour is unchanged: only an explicit 0 is unlimited."""
    b = C.with_details(build(), {})
    assert I.collection_details(first_json(b))["supply"] == 3


def test_a_second_batch_follows_the_set():
    later = C.renumbered(build(n=3), 13)
    assert [i.edition for i in later.items] == [13, 14, 15]
    assert [i.name for i in later.items] == ["Skull Squad #13", "Skull Squad #14", "Skull Squad #15"]
    data = json.loads(later.items[0].json)
    assert data["edition"] == 13 and data["name"] == "Skull Squad #13"
    assert I.collection_of(later.items[0].json)[0] == "Skull Squad", "still the same set"

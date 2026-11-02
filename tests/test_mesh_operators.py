"""Network -> Other operators (2026-10-20): each linked arcade says who runs it (arcade/mesh/operators.py)."""
import asyncio

from arcade.mesh.operators import OperatorsShare


def test_each_linked_arcade_says_its_operator_and_is_listed():
    sent = []

    class Link:
        peer_id = "ab" * 32

        async def send(self, header, blob=b""):
            sent.append(header)

    class Node:
        links = {}

    share = OperatorsShare(lambda: {"tag": "strawberry", "name": "DogecoinArcade", "version": "1.2", "hosts": []})
    link = Link()
    share.node = Node()
    Node.links = {link.peer_id: link}

    async def go():
        await share.link_up(link)
        await share.on_frame(link, {"t": "op", "op": "me", "tag": "bigchiefenergy", "name": "DogecoinArcade",
                                    "version": "1.3", "hosts": ["app.dogecoinarcade.com"]}, b"")
        await share.on_frame(link, {"t": "op", "op": "other", "tag": "nobody"}, b"")
    asyncio.run(go())
    assert sent == [{"t": "op", "op": "me", "tag": "strawberry", "name": "DogecoinArcade", "version": "1.2",
                     "hosts": [], "links": []}], "its only link is the hearer, which is never told about itself"
    rows = share.status()
    assert len(rows) == 1 and rows[0]["tag"] == "bigchiefenergy" and rows[0]["linked"]
    assert rows[0]["hosts"] == ["app.dogecoinarcade.com"] and rows[0]["node"] == "ab" * 6
    Node.links = {}
    assert share.status()[0]["linked"] is False, "an arcade heard from earlier stays listed, as not linked now"


def test_a_said_tag_is_kept_only_with_a_tags_shape_and_an_older_arcade_has_none():
    """2026-10-21 (Overview's Arcades panel): each arcade tells linked arcades its operator's @name. On receipt
    it is kept only when shaped like a tag (2-24 of a-z, 0-9 and _, not reserved), bounded before it is read, shown and
    never trusted; an arcade that says none (older, or its operator has no name) is shown by its mesh id."""
    from arcade.mesh.operators import said_tag

    class Link:
        def __init__(self, peer):
            self.peer_id = peer

        async def send(self, header, blob=b""):
            pass

    class Node:
        links = {}
    share = OperatorsShare(lambda: {"name": "DogecoinArcade", "version": "1", "hosts": []})
    share.node = Node()
    frames = {"a" * 64: {"tag": "@StrawBerry"}, "b" * 64: {"tag": "x" * 25}, "c" * 64: {"tag": "rm -rf"},
              "d" * 64: {"tag": "admin"}, "e" * 64: {"tag": ["list"]}, "f" * 64: {},
              "0" * 64: {"tag": "y" * 10_000_000, "hosts": ["app.dogecoinarcade.com", "<b>x</b>", 7, "a" * 200]}}

    async def go():
        for peer, said in frames.items():
            await share.on_frame(Link(peer), {"t": "op", "op": "me", "name": "DogecoinArcade", **said}, b"")
    asyncio.run(go())
    tags = {p: s["tag"] for p, s in share.peers.items()}
    assert tags["a" * 64] == "strawberry", "normalised like any tag"
    assert all(tags[p * 64] == "" for p in "bcdef0"), "too long, wrong letters, reserved, not text, or absent"
    assert share.peers["0" * 64]["hosts"] == ["app.dogecoinarcade.com"], "hosts only when shaped like host names"
    assert said_tag("ab") == "ab" and said_tag("a") == "" and said_tag(None) == ""


def test_an_arcade_whose_operator_has_no_name_leaves_the_tag_out():
    from arcade.mesh.service import operator_about

    class State:
        public_hosts = ("app.example.com",)

        def __init__(self, tag):
            self.tag = tag

        def operator_tag(self):
            return self.tag
    assert "tag" not in operator_about(State("")), "an operator with no name: the hearer shows the mesh id"
    said = operator_about(State("strawberry"))
    assert said["tag"] == "strawberry" and said["hosts"] == ["app.example.com"]


def test_each_arcade_says_what_it_is_linked_to_and_the_hearer_sees_them_through_it():
    """2026-11-02 (the phone "is not listing all the arcades seen by strawberry"): a small node keeps two or
    three links, so "me" carries `links`, the arcades the speaker is linked to now, with the tag and host each said.
    The hearer keeps them shape-checked and shows them as seen through the speaker while it is linked and fresh."""
    import time
    from arcade.mesh.operators import MAX_LINKS, VIA_FRESH, clean_links, seen_through
    sent = []

    class Link:
        def __init__(self, peer):
            self.peer_id = peer

        async def send(self, header, blob=b""):
            sent.append((self.peer_id, header))

    class Node:
        links = {}
    phone, straw, avocado, pep = "1" * 64, "2" * 64, "3" * 64, "4" * 64
    share = OperatorsShare(lambda: {"name": "DogecoinArcade", "version": "1", "hosts": []})
    share.node = Node()
    Node.links = {phone: Link(phone), avocado: Link(avocado), pep: Link(pep)}

    async def go():
        await share.on_frame(Link(avocado), {"t": "op", "op": "me", "tag": "avacado",
                                             "hosts": ["avo.example.com"]}, b"")
        await share.rebalance()
    asyncio.run(go())
    to_phone = [h for p, h in sent if p == phone][0]
    assert to_phone["links"] == [{"id": avocado, "tag": "avacado", "host": "avo.example.com"}, {"id": pep}], \
        "every link but the hearer's own, with what each said"

    # On the hearer's side: kept only in shape, bounded.
    said = [{"id": avocado, "tag": "avacado", "host": "avo.example.com"}, {"id": pep, "tag": "rm -rf", "host": "<b>"},
            {"id": "zz" * 32}, {"id": "ab"}, "x", {"id": avocado}] + [{"id": "%064x" % i} for i in range(200)]
    kept = clean_links(said)
    assert kept[:2] == [{"id": avocado, "tag": "avacado", "host": "avo.example.com"}, {"id": pep}]
    assert len(kept) <= MAX_LINKS and all(len(k["id"]) == 64 for k in kept), "at most MAX_LINKS, ids only"
    assert clean_links("nope") == [] and clean_links(None) == []

    me = phone
    now = time.time()
    peers = {straw: {"tag": "strawberry", "links": [{"id": avocado, "tag": "avacado"}, {"id": me}, {"id": pep}],
                     "at": now - 10},
             "5" * 64: {"tag": "old", "links": [{"id": "6" * 64}], "at": now - VIA_FRESH - 1}}
    seen = seen_through(peers, {straw, "5" * 64}, me, now)
    assert set(seen) == {avocado, pep}, "never this arcade, and not from a stale word"
    assert seen[avocado] == {"by": straw, "by_tag": "strawberry", "tag": "avacado", "host": ""}
    assert seen_through(peers, {"5" * 64}, me, now) == {}, "only from an arcade linked now"
    assert set(seen_through(peers, {straw, pep}, me, now)) == {avocado}, "one linked here is not seen through another"

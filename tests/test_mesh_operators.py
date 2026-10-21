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
    assert sent == [{"t": "op", "op": "me", "tag": "strawberry", "name": "DogecoinArcade", "version": "1.2", "hosts": []}]
    rows = share.status()
    assert len(rows) == 1 and rows[0]["tag"] == "bigchiefenergy" and rows[0]["linked"]
    assert rows[0]["hosts"] == ["app.dogecoinarcade.com"] and rows[0]["node"] == "ab" * 6
    Node.links = {}
    assert share.status()[0]["linked"] is False, "an arcade heard from earlier stays listed, as not linked now"

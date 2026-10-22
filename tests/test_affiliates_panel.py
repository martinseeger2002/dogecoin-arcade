"""Admin -> Affiliates (2026-10-21: "I want an admin affiliate panel. It should show the operators published URLs
with a check box next to each to share with affiliates. It should have a list of shared URLs from other node operators,
each with a checkmark to share on client side affiliate tab." Which URLs: "Arcade's names + ones I add". Then: "The
second list should be a list of operators and their published URLs that they shared with the affiliates.")
arcade/affiliates.py, the operators "me" frame's `sites` (arcade/mesh/operators.py)."""

import asyncio
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                                 # noqa: F401,E402
from test_admin import EDGE, LOCAL, WRITE, public                      # noqa: F401,E402

from arcade import affiliates as aff                                    # noqa: E402
from arcade.mesh.operators import OperatorsShare                        # noqa: E402

PEER_A = "a1" * 32
PEER_B = "b2" * 32


def test_only_https_links_to_named_sites_pass():
    good = ["https://antidoginaldogs.com", "https://Shop.Example.co.uk/path?q=1#top", "https://xn--bcher-kva.example/"]
    for url in good:
        assert aff.clean_url(url), url
    assert aff.clean_url("https://Shop.Example.co.uk/x") == "https://shop.example.co.uk/x"
    bad = ["http://example.com", "javascript:alert(1)", "data:text/html,<b>x</b>", "https://user:pw@example.com",
           "https://user@example.com", "https://10.0.0.1/", "https://[::1]/", "https://localhost/", "https://example.com:8443/",
           "https://exa mple.com", "https://example.com/\nx", "https://example.com/\"onmouseover=", "https://example.com/<x>",
           "https://example.com\\@evil.com", "//example.com", "https://", "https://" + "a" * 200 + ".com", "ftp://example.com",
           " https://example.com", "https://example.com\u0000", "https://-bad-.com", "https://example.123", None, 7, ["x"]]
    for url in bad:
        assert aff.clean_url(url) == "", url


def test_sites_are_checked_whole_and_bounded():
    ok = {"url": "https://example.com", "name": "Example", "about": "A  line."}
    assert aff.clean_site(ok) == {"url": "https://example.com", "name": "Example", "about": "A line."}
    assert aff.clean_site({**ok, "name": ""}) is None and aff.clean_site({**ok, "name": "x" * 61}) is None
    assert aff.clean_site({**ok, "about": "y" * 201}) is None and aff.clean_site({**ok, "about": "two\nlines"}) is None
    assert aff.clean_site({**ok, "name": "x" * 60, "about": "y" * 200})
    assert aff.clean_site({"url": "https://example.com", "name": "No about"})["about"] == ""
    assert aff.clean_sites([ok] * 21) is None, "more than 20: the whole field ignored"
    assert aff.clean_sites("https://example.com") is None and aff.clean_sites({"url": 1}) is None
    mixed = [ok, ok, {"url": "javascript:alert(1)", "name": "x"}, {"url": "https://b.example", "name": "B"}, "str"]
    assert [s["url"] for s in aff.clean_sites(mixed)] == ["https://example.com", "https://b.example"], \
        "bad ones dropped, each URL once"


class _Link:
    def __init__(self, peer):
        self.peer_id = peer
        self.sent = []

    async def send(self, header, blob=b""):
        self.sent.append(header)


class _Node:
    links = {}


def test_the_me_frame_carries_the_shared_sites_and_an_older_peer_lacks_them(app_state):
    state = app_state
    state.set_setting("public_hosts", ["app.example.com", "pages.example.com"])
    aff.save(state, {"mine": [{"url": "https://shop.example.org", "name": "Shop", "about": "Things.", "share": True},
                              {"url": "https://private.example.org", "name": "Mine only", "about": "", "share": False}],
                     "hosts_shared": ["https://app.example.com", "https://not-mine.example.com"]})
    from arcade.mesh.service import operator_about
    said = operator_about(state)
    assert said["sites"] == [{"url": "https://app.example.com", "name": said["name"] or "Arcade",
                              "about": aff.host_sites(state)[0]["about"]},
                             {"url": "https://shop.example.org", "name": "Shop", "about": "Things."}]
    heard = []
    share = OperatorsShare(lambda: said, lambda peer, tag, sites: heard.append((peer, tag, sites)))
    share.node = _Node()
    link = _Link(PEER_A)

    async def go():
        await share.link_up(link)
        await share.on_frame(_Link(PEER_A), {"t": "op", "op": "me", "tag": "strawberry", "name": "DogecoinArcade",
                                             "sites": [{"url": "https://frog.example", "name": "Frogs", "about": "x"},
                                                       {"url": "http://bad.example", "name": "Bad"}]}, b"")
        await share.on_frame(_Link(PEER_B), {"t": "op", "op": "me", "tag": "oldtimer", "name": "DogecoinArcade",
                                             "version": "0.9"}, b"")
        await share.on_frame(_Link(PEER_B), {"t": "op", "op": "me", "sites": [{"url": "https://x.example",
                                                                             "name": "x"}] * 21}, b"")
    asyncio.run(go())
    assert link.sent[0]["sites"] == said["sites"]
    assert heard == [(PEER_A, "strawberry", [{"url": "https://frog.example", "name": "Frogs", "about": "x"}])], \
        "an older arcade (no field) and an oversized list change nothing"
    assert share.peers[PEER_B]["tag"] == "", "the oversized frame said no tag; the peer is still listed"


def test_what_was_heard_survives_a_restart_and_an_empty_list_drops_it(app_state, tmp_path):
    state = app_state
    site = {"url": "https://frog.example", "name": "Frogs", "about": "Pepes."}
    assert aff.record(state, PEER_A, "strawberry", [site])
    assert not aff.record(state, PEER_A, "strawberry", [site]), "unchanged within the hour: not written again"
    assert not aff.record(state, PEER_B, "", None), "an older arcade: nothing"
    from arcade.web.state import AppState
    again = AppState(home=state.home, messaging=state.messaging, ledger=state.ledger)
    rows = aff.others(again)
    assert len(rows) == 1 and rows[0]["tag"] == "strawberry" and rows[0]["sites"][0]["url"] == site["url"]
    assert rows[0]["sites"][0]["shown"] is False, "nothing from another operator is shown until ticked"
    assert aff.record(again, PEER_A, "strawberry", [])
    assert aff.others(again) == []


def test_the_admin_routes_are_the_operators_only(public):
    app, state = public
    assert app.get("/admin/api/affiliates", headers=EDGE).status_code in (401, 403, 404)
    assert app.post("/admin/api/affiliates", headers={**EDGE, **WRITE}, json={"shown": []}).status_code in (401, 403, 404)
    assert app.post("/admin/api/affiliates", headers=LOCAL, json={"shown": []}).status_code == 403, "the admin header"
    got = app.get("/admin/api/affiliates", headers=LOCAL).json()
    assert [h["url"] for h in got["hosts"]] == ["https://node.dogecoinarcade.com"]
    assert [s["url"] for s in got["mine"]] == ["https://antidoginaldogs.com"], "the old fixed list, seeded"
    assert got["others"] == []
    for bad in ({"mine": [{"url": "javascript:alert(1)", "name": "x"}]},
                {"mine": [{"url": "https://ok.example", "name": ""}]},
                {"mine": [{"url": "https://ok.example", "name": "x"}] * 2},
                {"mine": [{"url": f"https://s{n}.example", "name": "x"} for n in range(21)]},
                {"mine": "nope"}):
        r = app.post("/admin/api/affiliates", headers={**LOCAL, **WRITE}, json=bad)
        assert r.status_code == 400, bad
    assert [s["url"] for s in aff.mine(state)] == ["https://antidoginaldogs.com"], "a refused save writes nothing"
    r = app.post("/admin/api/affiliates", headers={**LOCAL, **WRITE},
                 json={"mine": [*aff.mine(state), {"url": "https://shop.example", "name": "Shop", "share": True}],
                       "hosts_shared": ["https://node.dogecoinarcade.com"]})
    assert r.status_code == 200 and r.json()["sharing"] == 2
    got = app.get("/admin/api/affiliates", headers=LOCAL).json()
    assert got["hosts"][0]["share"] and [s["share"] for s in got["mine"]] == [False, True]
    assert "Affiliates" in app.get("/admin", headers=LOCAL).text and 'id="affgroups"' in app.get("/admin", headers=LOCAL).text


def test_the_client_tab_shows_own_sites_then_only_ticked_others_by_who_shares_them(public):
    app, state = public
    aff.record(state, PEER_A, "strawberry", [{"url": "https://frog.example", "name": "Frogs", "about": "Pepes."},
                                             {"url": "https://unticked.example", "name": "Unticked", "about": ""}])
    aff.record(state, PEER_B, "", [{"url": "https://evil.example", "name": "<script>alert(1)</script>",
                                    "about": "\"><img src=x onerror=alert(1)>"}])
    stale = "c3" * 32
    aff.record(state, stale, "oldfriend", [{"url": "https://stale.example", "name": "Stale", "about": ""}],
               now=time.time() - 40 * 86400)
    page = app.get("/affiliates").text
    assert "frog.example" not in page and "evil.example" not in page, "nothing shown until the operator ticks it"
    r = app.post("/admin/api/affiliates", headers={**LOCAL, **WRITE},
                 json={"shown": ["https://frog.example", "https://evil.example", "https://stale.example"]})
    assert r.status_code == 200
    got = app.get("/admin/api/affiliates", headers=LOCAL).json()
    groups = {g["tag"] or g["node"]: g for g in got["others"]}
    assert set(groups) == {"strawberry", PEER_B[:12], "oldfriend"}, "grouped by the operator who shares them"
    assert [s["shown"] for s in groups["strawberry"]["sites"]] == [True, False]
    page = app.get("/affiliates").text
    assert page.index("antidoginaldogs.com") < page.index("Shared by @strawberry") < page.index("frog.example")
    assert "unticked.example" not in page and "stale.example" not in page, "unticked, or not heard for 30 days"
    assert "node.dogecoinarcade.com" not in page.split("<h1>Affiliates</h1>", 1)[1], "this arcade's own names: not here"
    assert f"Shared by an arcade {PEER_B[:12]}" in page
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<img src=x" not in page
    assert page.count('rel="noopener noreferrer nofollow"') == 3
    # the sharer stops sharing it: gone from the tab though still ticked here
    aff.record(state, PEER_A, "strawberry", [])
    assert "frog.example" not in app.get("/affiliates").text


def test_seeding_keeps_antidoginaldogs_on_the_tab_until_the_operator_saves_otherwise(client):
    app, state = client
    assert state.setting("affiliates_mine") is None
    assert 'href="https://antidoginaldogs.com"' in app.get("/affiliates").text
    aff.save(state, {"mine": []})
    assert "antidoginaldogs.com" not in app.get("/affiliates").text, "an empty list saved stays empty"

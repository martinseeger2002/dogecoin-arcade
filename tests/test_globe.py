"""The nodes globe: the operator's list, the public feed, and the tiers.

A stranger may open the map. The feed they get has names, coarse places and
counts, and never a host, an IP or a mesh id.
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

from arcade import globe                                         # noqa: E402
from arcade.web import door                                      # noqa: E402

WRITE = {"X-Arcade-Admin": "1"}
SECRET_IP = "203.0.113.55"
SECRET_HOST = "node.cloudflare.com"
SECRET_ID = "deadbeefcafebabe"


def _rows():
    return [
        {"tag": "pepmonster", "linked": True, "self": True, "chains": ["doge", "pep"],
         "host": SECRET_HOST, "ip": SECRET_IP, "node": SECRET_ID},
        {"tag": "banana", "linked": True, "self": False, "chains": ["doge"],
         "hosts": [SECRET_IP], "id": SECRET_ID},
        {"tag": "banana", "linked": True, "self": False, "chains": ["ltc"],
         "host": SECRET_HOST},
        {"tag": "pumpkin", "linked": False, "self": False, "chains": ["doge"],
         "ip": SECRET_IP},
        {"tag": SECRET_HOST, "linked": True, "self": False, "chains": ["doge"]},
        {"tag": SECRET_ID, "linked": True, "self": False, "chains": ["doge"]},
        {"tag": "", "linked": True, "self": False, "chains": ["bells"],
         "node": SECRET_ID, "lat": 1.2345, "lon": 2.3456},
    ]


def test_the_door_lets_a_stranger_read_the_globe_and_not_the_choice():
    assert "/globe" in door.PUBLIC_PAGES and "/globe.json" in door.PUBLIC_PAGES
    assert "/admin/api/globe" not in door.PUBLIC_PAGES


def test_names_that_are_addresses_are_dropped():
    assert globe.clean_name("pepmonster") == "pepmonster"
    assert globe.clean_name("@Banana") == "banana"
    for bad in (SECRET_IP, SECRET_HOST, SECRET_ID, "10.1.2.3", "a.b.c.example",
                "not a name", "", "x"):
        assert globe.clean_name(bad) == "", bad
    assert globe.clean_place("203.0.113.9") == ""
    assert globe.clean_place("cdn.cloudflare.com") == ""
    assert globe.match_place("eastern france") == "Eastern France"
    assert globe.match_place("12 Secret Street") == ""


def test_bands_widen_as_more_chains_run_and_diamond_is_the_top():
    assert globe.tier_for(0, 4, 10) == ""
    assert globe.tier_for(4, 4, 20) == "copper"
    assert globe.tier_for(8, 4, 20) == "bronze"
    assert globe.tier_for(12, 4, 20) == "silver"
    assert globe.tier_for(13, 4, 20) == "gold"
    assert globe.tier_for(20, 4, 20) == "diamond"
    assert globe.tier_for(4, 8, 20) == "copper", "the same 4 nodes drop a band when chains double"
    assert globe.tier_for(4, 1, 4) == "diamond"
    assert globe.tier_for(4, 1, 4) == globe.tier_for(4, 1, 4)
    one = globe.bands(1)["copper"]
    eight = globe.bands(8)["copper"]
    assert eight[1] > one[1]


def test_a_tie_for_the_most_nodes_shares_diamond_and_secrets_stay_out():
    feed = globe.feed_from(_rows(), {"source": "mesh"})
    by = {row["tag"]: row for row in feed["members"]}
    assert set(by) == {"pepmonster", "banana", "pumpkin"}
    assert by["banana"]["nodes"] == 2 and by["banana"]["tier"] == "diamond"
    assert by["pepmonster"]["nodes"] == 1 and by["pepmonster"]["tier"] != "diamond"
    assert by["pepmonster"]["mine"] is True
    assert by["pumpkin"]["active"] is False and by["pumpkin"]["tier"] == ""
    assert by["pumpkin"]["place"] == "Rochester, New York"
    assert by["pumpkin"]["lat"] == 43.2167, "a known city stays on the map, dimmed"
    assert by["banana"]["place"] == "Eastern France"
    dumped = json.dumps(feed)
    for secret in (SECRET_IP, SECRET_HOST, SECRET_ID, "1.2345", "cloudflare"):
        assert secret not in dumped, secret
    assert "host" not in dumped and "node" not in feed["members"][0]


def test_choosing_a_list_and_a_place_changes_the_feed_without_a_new_address():
    mesh = globe.feed_from(_rows(), {"source": "mesh"})
    picked = globe.feed_from(_rows(), {
        "source": "picked",
        "picked": ["pumpkin", SECRET_IP, "banana"],
        "places": {"pumpkin": "St. Louis, Missouri", "banana": "12 Main St"},
    })
    assert [row["tag"] for row in mesh["members"]] != [row["tag"] for row in picked["members"]]
    by = {row["tag"]: row for row in picked["members"]}
    assert set(by) == {"pumpkin", "banana"}
    assert by["pumpkin"]["place"] == "St. Louis, Missouri"
    assert by["pumpkin"]["lat"] == 38.6364
    assert by["banana"]["place"] == "Eastern France", "a street is not a place, so the known one stands"
    linked = globe.feed_from(_rows(), {"source": "linked"})
    assert "pumpkin" not in {row["tag"] for row in linked["members"]}


def test_more_chains_widen_the_published_bands():
    narrow = globe.feed_from(
        [{"tag": "pepmonster", "linked": True, "self": True, "chains": ["doge"]}],
        {"source": "mesh"})
    wide = globe.feed_from(
        [{"tag": "pepmonster", "linked": True, "self": True,
          "chains": ["doge", "pep", "ltc", "bells", "ltctest", "dgb", "lky", "kot"]}],
        {"source": "mesh"})
    assert wide["chains"] > narrow["chains"]
    assert wide["bands"]["copper"][1] > narrow["bands"]["copper"][1]
    assert wide["updated"] != narrow["updated"]


def test_the_page_and_the_feed_are_public_and_the_choice_is_not(client):
    app, state = client
    page = app.get("/globe")
    assert page.status_code == 200
    assert "rotateSpeed = 0.35" in page.text and "autoRotateSpeed = 0.55" in page.text
    assert 'id="sub"' not in page.text
    assert "Inactive nodes" in page.text
    assert 'data-tier="copper"' in page.text and "Any tier can be shared" not in page.text
    assert page.text.find("const NODES = [") < page.text.find("assignTiers(NODES)")
    assert 'document.readyState === "complete"' in page.text
    assert "Math.max(h, 56)" in page.text and "* 1.10" in page.text
    assert "2026-04-20" in page.text and "5044" in page.text
    assert "unpkg.com" not in page.text
    for city in ("Secaucus", "Nuremberg", "Falkenstein", "Tbilisi", "Tainan", "Tokyo"):
        assert city not in page.text
    feed = app.get("/globe.json").json()
    assert feed["ok"] is True and "members" in feed and "bands" in feed
    state.public = True
    try:
        assert app.get("/globe.json", headers={"host": "127.0.0.1:8420"}).status_code == 200
        hidden = app.get("/admin/api/globe", headers={"host": "127.0.0.1:8420"})
        assert hidden.status_code == 404 and "Not here" in hidden.text
    finally:
        state.public = False


def test_the_operator_saves_a_list_and_a_linked_arcade_shows_up_without_its_address(client):
    app, state = client
    panel = app.get("/admin").text
    assert "Nodes globe" in panel and "/admin/api/globe" in panel

    class Node:
        links = {"peer-1": object()}
        addrs = {}

    class Operators:
        peers = {"peer-1": {"tag": "banana", "hosts": [SECRET_HOST], "node": SECRET_ID}}

    class Cores:
        offers = {"peer-1": {"doge": object(), "ltc": object()}}

    class Mesh:
        loop = object()
        node_id = SECRET_ID
        node = Node()
        operators = Operators()
        coreshare = Cores()

    state.mesh = Mesh()
    state.operator_tag = lambda: "pepmonster"
    saved = app.post("/admin/api/globe", headers=WRITE, json={
        "source": "picked",
        "picked": ["banana", SECRET_IP],
        "places": {"banana": "Eastern France"},
    })
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["choice"]["picked"] == ["banana"]
    assert SECRET_IP not in json.dumps(body) and SECRET_HOST not in json.dumps(body)
    assert SECRET_ID not in json.dumps(body)
    live = app.get("/globe.json").json()
    by = {row["tag"]: row for row in live["members"]}
    assert by["banana"]["nodes"] == 1 and by["banana"]["place"] == "Eastern France"
    assert by["banana"]["active"] is True
    again = app.post("/admin/api/globe", headers=WRITE, json={"source": "mesh", "picked": []})
    assert "pepmonster" in {row["tag"] for row in again.json()["members"]}
    assert "banana" in {row["tag"] for row in again.json()["members"]}

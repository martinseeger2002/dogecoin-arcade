"""The Node Map tab: every arcade this node has heard, and every chain peer, on a map."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                                 # noqa: F401,E402


def test_a_public_address_lands_in_its_country():
    from arcade.geoip import country, place, public_ip
    assert public_ip("10.1.2.3") == ""
    assert public_ip("127.0.0.1") == ""
    assert country("8.8.8.8") == "US"
    assert country("1.1.1.1") == "AU"
    assert country("2001:4860:4860::8888") == "US"
    where = place("8.8.8.8", salt="a")
    assert where["name"] == "United States"
    assert where["lat"] is not None and where["lon"] is not None
    again = place("8.8.8.8", salt="a")
    assert (where["lat"], where["lon"]) == (again["lat"], again["lon"])


def test_the_tab_is_in_both_menus_and_draws_without_a_node(client):
    app, _state = client
    from arcade.web.app import ACCOUNT_NAV, NAV
    from arcade.web.door import PUBLIC_PAGES
    for tabs in (NAV, ACCOUNT_NAV):
        paths = [t[0] for t in tabs]
        assert paths.index("/affiliates") < paths.index("/nodes") < paths.index("/docs")
    assert "/nodes" in PUBLIC_PAGES and "/nodes.json" in PUBLIC_PAGES
    page = app.get("/nodes").text
    assert "Node Map" in page and 'id="pins"' in page and "/nodes.json" in page
    assert "Zoom in" in page and 'id="nodeq"' in page
    said = app.get("/nodes.json").json()
    assert said["ok"] is True and said["nodes"] == [] and "counts" in said
    assert "8.8.8.8" not in page
    land = app.get("/world-land.svg")
    assert land.status_code == 200 and "<path" in land.text
    assert 'data-code="DE"' in land.text and 'data-code="US"' in land.text

"""SadFrog's DigiByteArcade face, and a face per web address (`brand_hosts`) -- 2026-11-03, SadFrog's side.

One node published as pepecoinarcade.com and digibyte-arcade.com shows each its own name, colours and icons; the
node's own face (the mesh, its own name) does not move."""
import base64
import io
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
from test_web import app_state  # noqa: F401,E402

from arcade import affiliates as aff  # noqa: E402
from arcade.web import brand as brandlib  # noqa: E402
from arcade.web.app import TEMPLATE_DIR, create_app  # noqa: E402

LOCAL = {"host": "127.0.0.1:8420"}
WRITE = {"x-arcade-admin": "1"}


def test_the_digibyte_face_is_named_coloured_and_iconed(app_state, monkeypatch):
    monkeypatch.setenv("ARCADE_BRAND", "digibyte")
    app = TestClient(create_app(app_state))
    page = app.get("/feed").text
    assert '<span class="brand">DigiByteArcade</span>' in page
    assert "--accent:#0066cc" in page and "--accent:#4d9fff" in page, "light and dark blue"
    manifest = app.get("/manifest.webmanifest").json()
    assert manifest["name"] == "DigiByteArcade" and manifest["background_color"] == "#f2f6fb"
    for size in (32, 180, 192, 512):
        own = (TEMPLATE_DIR / "brands" / "digibyte" / f"icon-{size}.png").read_bytes()
        assert app.get(f"/icon-{size}.png").content == own
        assert own != (TEMPLATE_DIR / f"icon-{size}.png").read_bytes()
        assert own != (TEMPLATE_DIR / "brands" / "litecoin" / f"icon-{size}.png").read_bytes()


def test_the_pixel_font_holds_every_letter_of_every_face():
    css = (TEMPLATE_DIR / "base_css.css").read_text()
    woff2 = base64.b64decode(re.search(r"font/woff2;base64,([A-Za-z0-9+/=]+)", css).group(1))
    assert woff2[:4] == b"wOF2"
    pytest.importorskip("brotli")          # fontTools and brotli are not the arcade's own dependencies
    TTFont = pytest.importorskip("fontTools.ttLib").TTFont
    has = {chr(c) for c in TTFont(io.BytesIO(woff2)).getBestCmap()}
    for face in brandlib.BRANDS.values():
        assert set(face.name) <= has, f"{face.name}: no {sorted(set(face.name) - has)}"


def test_each_web_address_wears_its_own_face(app_state, monkeypatch):
    monkeypatch.setenv("ARCADE_BRAND", "pepecoin")
    monkeypatch.setattr(brandlib, "FROM_ENV", True)
    app_state.set_setting("public_hosts", ["pepecoinarcade.com", "digibyte-arcade.com"])
    app_state.set_setting("brand_hosts", {"digibyte-arcade.com": "digibyte"})
    app = TestClient(create_app(app_state))
    dgb, pep = {"host": "digibyte-arcade.com"}, {"host": "pepecoinarcade.com"}
    page = app.get("/feed", headers=dgb).text
    assert '<span class="brand">DigiByteArcade</span>' in page and "--accent:#0066cc" in page
    assert "PepecoinArcade" not in page
    assert app.get("/manifest.webmanifest", headers=dgb).json()["name"] == "DigiByteArcade"
    assert app.get("/icon-192.png", headers=dgb).content == \
        (TEMPLATE_DIR / "brands" / "digibyte" / "icon-192.png").read_bytes()
    page = app.get("/feed", headers=pep).text
    assert '<span class="brand">PepecoinArcade</span>' in page and "DigiByteArcade" not in page
    assert app.get("/manifest.webmanifest", headers=pep).json()["name"] == "PepecoinArcade"
    assert '<span class="brand">PepecoinArcade</span>' in app.get("/feed", headers=LOCAL).text
    # Outside a request, and after one, the node is itself: the mesh and its own name never change.
    assert brandlib.current() is brandlib.PEPECOIN and brandlib.own() is brandlib.PEPECOIN
    from arcade.mesh.service import operator_about
    assert operator_about(app_state)["name"] == "PepecoinArcade"
    names = {s["url"]: s["name"] for s in aff.host_sites(app_state)}
    assert names == {"https://pepecoinarcade.com": "PepecoinArcade", "https://digibyte-arcade.com": "DigiByteArcade"}
    app_state.set_setting("public_hosts", [])


def test_a_face_per_address_is_saved_in_admin_only_for_a_known_face_and_a_public_name(app_state, monkeypatch):
    monkeypatch.setenv("ARCADE_BRAND", "pepecoin")
    monkeypatch.setattr(brandlib, "FROM_ENV", True)
    app_state.set_setting("public_hosts", ["pepecoinarcade.com", "digibyte-arcade.com"])
    app = TestClient(create_app(app_state))
    said = app.get("/admin/api/state", headers={**LOCAL, **WRITE}).json()["brand"]
    assert said["current"] == "pepecoin" and said["locked"] and said["hosts"] == {}
    assert said["public_hosts"] == ["pepecoinarcade.com", "digibyte-arcade.com"]
    assert ["digibyte", "DigiByteArcade"] in said["choices"]
    refused = app.post("/admin/api/settings", headers={**LOCAL, **WRITE},
                       json={"brand_hosts": {"digibyte-arcade.com": "bitcoin"}})
    assert refused.status_code == 400 and "no such face" in refused.text
    refused = app.post("/admin/api/settings", headers={**LOCAL, **WRITE},
                       json={"brand_hosts": {"elsewhere.example": "digibyte"}})
    assert refused.status_code == 400 and "public names" in refused.text
    assert app_state.setting("brand_hosts") is None, "a refused choice changes nothing"
    ok = app.post("/admin/api/settings", headers={**LOCAL, **WRITE},
                  json={"brand_hosts": {"Digibyte-Arcade.com": "DigiByte"}})
    assert ok.status_code == 200, ok.text
    assert app_state.setting("brand_hosts") == {"digibyte-arcade.com": "digibyte"}
    assert app.get("/admin/api/state", headers={**LOCAL, **WRITE}).json()["brand"]["hosts"] == \
        {"digibyte-arcade.com": "digibyte"}
    assert app.post("/admin/api/settings", headers={**LOCAL, **WRITE}, json={"brand_hosts": {}}).status_code == 200
    assert app_state.setting("brand_hosts") == {}
    app_state.set_setting("public_hosts", [])

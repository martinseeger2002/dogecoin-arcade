"""A node's face (arcade/web/brand.py): DogecoinArcade by default, PepecoinArcade with --brand pepecoin.

Only the presentation changes; the names the protocol signs or reads off the chain stay
DogecoinArcade on every face, so the two faces are one network (2026-10-17)."""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
from test_web import app_state  # noqa: F401,E402

from arcade.web import brand as brandlib  # noqa: E402
from arcade.web.app import TEMPLATE_DIR, create_app  # noqa: E402


def _app(app_state, monkeypatch, key):
    if key is None:
        monkeypatch.delenv("ARCADE_BRAND", raising=False)
    else:
        monkeypatch.setenv("ARCADE_BRAND", key)
    return TestClient(create_app(app_state))


def test_the_default_face_is_dogecoinarcade(app_state, monkeypatch):
    app = _app(app_state, monkeypatch, None)
    page = app.get("/feed").text
    assert '<span class="brand">DogecoinArcade</span>' in page
    assert "PepecoinArcade" not in page
    assert "this node's face" not in page, "the default face adds no styles"
    assert app.get("/manifest.webmanifest").json()["name"] == "DogecoinArcade"
    assert app.get("/icon-192.png").content == (TEMPLATE_DIR / "icon-192.png").read_bytes()


def test_the_pepecoin_face_is_named_coloured_and_iconed(app_state, monkeypatch):
    app = _app(app_state, monkeypatch, "pepecoin")
    page = app.get("/feed").text
    assert '<span class="brand">PepecoinArcade</span>' in page
    assert "<title>" in page and "DogecoinArcade</title>" not in page
    assert "--accent:#2f8f3a" in page and "--accent:#5fcb5a" in page, "light and dark green"
    assert 'content="#f4f8f1" media="(prefers-color-scheme: light)"' in page
    manifest = app.get("/manifest.webmanifest").json()
    assert manifest["name"] == "PepecoinArcade" and manifest["background_color"] == "#f4f8f1"
    for size in (32, 180, 192, 512):
        own = (TEMPLATE_DIR / "brands" / "pepecoin" / f"icon-{size}.png").read_bytes()
        assert app.get(f"/icon-{size}.png").content == own
        assert own != (TEMPLATE_DIR / f"icon-{size}.png").read_bytes()
    offline = app.get("/offline").text
    assert "Offline · PepecoinArcade" in offline and "--accent:#2f8f3a" in offline


def test_every_page_title_follows_the_face(app_state, monkeypatch):
    """No template keeps the name in its title by hand (62 did before)."""
    for f in TEMPLATE_DIR.glob("*.html"):
        assert "· DogecoinArcade{% endblock %}" not in f.read_text(), f.name
    app = _app(app_state, monkeypatch, "pepecoin")
    for path in ("/feed", "/tokens", "/exchange", "/docs"):
        assert "DogecoinArcade</title>" not in app.get(path).text, path


def test_an_unknown_face_falls_back_to_the_default(monkeypatch, capsys):
    monkeypatch.setenv("ARCADE_BRAND", "dogecoinn")
    assert brandlib.current() is brandlib.DOGECOIN
    assert "not one of" in capsys.readouterr().err


def test_the_protocol_keeps_its_name_on_every_face():
    """What is signed or read off the chain is not presentation: one network, two faces."""
    from arcade import accounts, config
    from arcade.messaging import derive
    assert config.MARKER_SEED == b"DogecoinArcade Class B marker v1"
    assert derive.DERIVATION_MESSAGE == "DogecoinArcade messaging identity v1"
    assert accounts.login_message("https://x", "n").startswith(b"DogecoinArcade login\n")
    assert "`DogecoinArcade login" in (TEMPLATE_DIR / "signin.js").read_text()


def test_the_brand_flag_sets_the_face(monkeypatch):
    import arcade.web.__main__ as launcher
    src = Path(launcher.__file__).read_text()
    assert '"--brand", choices=sorted(brandlib.BRANDS)' in src
    assert 'os.environ["ARCADE_BRAND"] = args.brand' in src

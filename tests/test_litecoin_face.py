"""SadFrog's LitecoinArcade face, choosing a face from the admin panel, and the Litecoin
node's switch (arcade/litecoin.py) -- 2026-10-17, SadFrog's side.

The face is presentation only (brand.py). The Litecoin switch reads and starts a node; it
never touches the arcade's networks, which do not include Litecoin yet."""
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
from test_web import app_state  # noqa: F401,E402

from arcade import litecoin as ltclib  # noqa: E402
from arcade.web import brand as brandlib  # noqa: E402
from arcade.web.app import TEMPLATE_DIR, create_app  # noqa: E402


def test_the_litecoin_face_is_named_coloured_and_iconed(app_state, monkeypatch):
    monkeypatch.setenv("ARCADE_BRAND", "litecoin")
    app = TestClient(create_app(app_state))
    page = app.get("/feed").text
    assert '<span class="brand">LitecoinArcade</span>' in page
    assert "--accent:#345d9d" in page and "--accent:#7fa3dc" in page, "light and dark blue"
    manifest = app.get("/manifest.webmanifest").json()
    assert manifest["name"] == "LitecoinArcade" and manifest["background_color"] == "#f3f5f9"
    for size in (32, 180, 192, 512):
        own = (TEMPLATE_DIR / "brands" / "litecoin" / f"icon-{size}.png").read_bytes()
        assert app.get(f"/icon-{size}.png").content == own
        assert own != (TEMPLATE_DIR / f"icon-{size}.png").read_bytes()


def test_use_switches_the_face_and_refuses_an_unknown_one(monkeypatch):
    monkeypatch.setenv("ARCADE_BRAND", "")   # so pytest restores what use() writes
    assert brandlib.use("Litecoin") is brandlib.LITECOIN
    assert brandlib.current() is brandlib.LITECOIN
    try:
        brandlib.use("bitcoin")
    except ValueError as exc:
        assert "no such face" in str(exc)
    else:
        raise AssertionError("an unknown face was accepted")
    assert brandlib.current() is brandlib.LITECOIN, "a refused choice changes nothing"


def test_a_saved_face_is_shown_when_the_service_sets_none(app_state, monkeypatch):
    monkeypatch.setenv("ARCADE_BRAND", "")   # so pytest restores what use() writes
    monkeypatch.setattr(brandlib, "FROM_ENV", False)
    app_state.set_setting("brand", "litecoin")
    page = TestClient(create_app(app_state)).get("/feed").text
    assert '<span class="brand">LitecoinArcade</span>' in page


def test_the_service_face_wins_over_a_saved_one(app_state, monkeypatch):
    monkeypatch.setenv("ARCADE_BRAND", "pepecoin")
    monkeypatch.setattr(brandlib, "FROM_ENV", True)
    app_state.set_setting("brand", "litecoin")
    page = TestClient(create_app(app_state)).get("/feed").text
    assert '<span class="brand">PepecoinArcade</span>' in page


def test_litecoin_status_never_raises_without_a_node(monkeypatch, tmp_path):
    monkeypatch.setattr(ltclib, "DATADIR", tmp_path)          # no .cookie here

    def no_systemd(*args):
        raise FileNotFoundError("systemctl")
    monkeypatch.setattr(ltclib, "_systemctl", no_systemd)
    said = ltclib.status()
    assert said["installed"] is False and said["service"] == "not installed"
    assert said["answering"] is False and "not running" in said["note"]
    try:
        ltclib.set_running(True)
    except RuntimeError as exc:
        assert "not installed" in str(exc)
    else:
        raise AssertionError("started a node that is not installed")


def test_starting_and_stopping_queue_the_service(monkeypatch, tmp_path):
    monkeypatch.setattr(ltclib, "DATADIR", tmp_path)
    calls = []

    def fake(*args):
        calls.append(args)
        out = "loaded" if args[:1] == ("show",) else ""
        return subprocess.CompletedProcess(args, 0, out, "")
    monkeypatch.setattr(ltclib, "_systemctl", fake)
    ltclib.set_running(True)
    ltclib.set_running(False)
    assert ("--no-block", "enable", "--now", ltclib.UNIT) in calls, "hosting survives a reboot"
    assert ("--no-block", "disable", "--now", ltclib.UNIT) in calls


def test_the_litecoin_page_says_what_to_know_about_taproot_and_cross_chain(app_state, monkeypatch):
    """The disclaimers ship as a Docs page, are linked from the admin panel, and are kept as
    constants for the wallet and Exchange screens that come next (2026-10-17)."""
    from arcade import litecoin as ltc, ltc_ord
    monkeypatch.setenv("ARCADE_BRAND", "pepecoin")
    app = TestClient(create_app(app_state))
    page = app.get("/docs/litecoin.md")
    assert page.status_code == 200
    for said in ("Taproot", "ltc1p", "Doginals format", "refunds its owner", "ltcmweb1",
                 "a network fee on each chain"):
        assert said in page.text, said
    assert "/docs/litecoin.md" in app.get("/docs").text, "offered on the Docs tab"
    assert "Taproot" in ltc_ord.TAPROOT_NOTE and "ltcmweb1" in ltc.CROSS_CHAIN_NOTE

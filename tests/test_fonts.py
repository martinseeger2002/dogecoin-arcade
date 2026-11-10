"""The arcade's own web fonts (arcade/web/fonts, 2026-11-10): what is served is what was fetched, with its licence,
from this node only."""
import fnmatch
import hashlib
import pathlib
import re
import tomllib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
FONTS = ROOT / "arcade" / "web" / "fonts"

#: The sums PROVENANCE.md names. A changed font is a changed application: change both, and say why.
PINNED = {
    "orbitron.woff2": "c25a9f9da5d9f3db1bf2a01474722dc9b377675b7bbab6d0dfda6902794fd1ed",
    "rajdhani-400.woff2": "759a9000e47b028799d7a4ca602634a7ac7adf415775df070a335d18d9b66f38",
    "rajdhani-500.woff2": "23afdb9b5b89b878fab04d80cc30bf41bb4f3f7e8be88e5f16a7cc7671cdb2dc",
    "rajdhani-600.woff2": "433a7007e4747a02a790167a6efa2625855f013970ba49b9b739a5d3db8b2601",
    "rajdhani-700.woff2": "5b7e4a6f97163c2636724d4de90304fc895653dcfe64c67a7a22f26331ca5c5f",
    "share-tech-mono.woff2": "41e6b9f297f7d9a2df2aaa274092f76d2f72711a15ca455f7f4f4f92caf16b72",
}


def test_every_font_is_the_one_that_was_fetched_and_written_down():
    assert {p.name for p in FONTS.glob("*.woff2")} == set(PINNED)
    provenance = (FONTS / "PROVENANCE.md").read_text()
    for name, sha in PINNED.items():
        assert hashlib.sha256((FONTS / name).read_bytes()).hexdigest() == sha, name
        assert sha in provenance, name


def test_each_font_has_its_licence_beside_it():
    for name in ("orbitron", "rajdhani", "sharetechmono"):
        assert "SIL Open Font License, Version 1.1" in (FONTS / f"OFL-{name}.txt").read_text(), name


def test_the_stylesheet_names_only_fonts_that_are_here():
    css = (FONTS / "fonts.css").read_text()
    named = set(re.findall(r"url\(/fonts/([^)]+)\)", css))
    assert named == set(PINNED)
    assert "http" not in css, "nothing from elsewhere"


def test_the_fonts_ship_in_the_package():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]["arcade.web"]
    for p in FONTS.iterdir():
        assert any(fnmatch.fnmatch(f"fonts/{p.name}", g) for g in data), p.name


from test_web import app_state, client                          # noqa: F401,E402


@pytest.mark.parametrize("name,kind", [("orbitron.woff2", "font/woff2"), ("fonts.css", "text/css")])
def test_the_arcade_serves_its_fonts_itself(client, name, kind):
    app, _ = client
    got = app.get(f"/fonts/{name}")
    assert got.status_code == 200 and got.headers["content-type"].startswith(kind)
    assert got.content == (FONTS / name).read_bytes()


@pytest.mark.parametrize("name", ["PROVENANCE.md", "OFL-orbitron.txt", "nope.woff2", "..%2Fapp.py", ".hidden.css"])
def test_nothing_else_comes_out_of_the_font_folder(client, name):
    assert client[0].get(f"/fonts/{name}").status_code == 404


def test_the_fonts_are_public_and_the_explorer_uses_them():
    from arcade.web import door
    assert door.public_path("/fonts/orbitron.woff2") and door.public_path("/fonts/fonts.css")
    page = (ROOT / "arcade" / "web" / "moonbrix_explorer.html").read_text()
    assert '<link rel="stylesheet" href="/fonts/fonts.css">' in page and "googleapis" not in page

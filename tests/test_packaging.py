"""What pip ships has to be what the interface serves.

A source checkout runs from its own tree, so a file the package-data list has
never heard of is still there. A pip-installed node -- every installer install
-- has only what setuptools copied. `templates/*.html` shipped and
`templates/*.js` did not, and /r/storage.js answered 500 on every installed
node for three releases while every test here passed. This is the test that
was missing: every file under arcade/web/templates has to be claimed by the
package-data globs, whatever its suffix.
"""

from __future__ import annotations

import fnmatch
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "arcade" / "web" / "templates"


def _shipped_globs(package: str = "arcade.web") -> list[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    return data["tool"]["setuptools"]["package-data"][package]


def test_every_template_file_is_shipped():
    globs = _shipped_globs()
    left_out = sorted(
        str(path.relative_to(TEMPLATES.parent))
        for path in TEMPLATES.iterdir()
        if path.is_file() and not path.name.startswith(".")
        and not any(fnmatch.fnmatch(str(path.relative_to(TEMPLATES.parent)), g)
                    for g in globs))
    assert left_out == [], f"not in package-data, so a pip install lacks: {left_out}"


def test_the_bridges_the_viewer_loads_are_shipped():
    """The two files the page sandbox fetches by name, checked by name as well:
    a rename to .mjs would pass the glob test and still 500 on a real node."""
    globs = _shipped_globs()
    for name in ("storage.js", "node.js"):
        assert (TEMPLATES / name).is_file()
        assert any(fnmatch.fnmatch(f"templates/{name}", g) for g in globs), name


def test_every_data_file_beside_the_code_is_shipped():
    """arcade/web/browser.html (the Browser, 2026-10-26) sat beside meshweb.py, not under templates/: /browser answered
    500 on Strawberry, an installed node, the first time it ran there. Any file in arcade/web that is not code has to be
    claimed too."""
    globs = _shipped_globs()
    web = ROOT / "arcade" / "web"
    left_out = sorted(p.name for p in web.iterdir()
                      if p.is_file() and p.suffix not in (".py", ".pyc") and not p.name.startswith(".")
                      and not any(fnmatch.fnmatch(p.name, g) for g in globs))
    assert left_out == [], f"not in package-data, so a pip install lacks: {left_out}"


def test_every_lite_template_file_is_shipped():
    """arcade/lite/templates shipped *.html only, and /lite/pages.css (ARCADE Lite's look for the full arcade's pages)
    answered 500 on the first installed Lite node while every test here passed."""
    globs, lite = _shipped_globs("arcade.lite"), ROOT / "arcade" / "lite" / "templates"
    left_out = sorted(p.name for p in lite.iterdir() if p.is_file() and not p.name.startswith(".")
                      and not any(fnmatch.fnmatch(f"templates/{p.name}", g) for g in globs))
    assert left_out == [], f"not in package-data, so a pip install lacks: {left_out}"

"""The arcade's face: the favicon, and the packaging that carries it.

The packaging half is here on purpose. This machine runs from a source
checkout and every other machine runs an installer build, so a data file
missing from the wheel works perfectly here and breaks there -- which is
the one shape of bug this project cannot catch by running its own tests.
"""

import hashlib
import pathlib
import sys
import tomllib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

TEMPLATES = pathlib.Path("arcade/web/templates")

#: The 180 as a test machine sent it, checked on arrival and pinned here so a
#: re-encode, a resize or a truncated copy is a failing test rather than a
#: favicon that quietly turns into something else.
LOGO_180_SHA256 = "16a559a7ce714bdedfc38b46dbe4f9996fdb6d56c020c29d883bf5584734252c"


def test_the_artwork_is_what_was_sent():
    data = (TEMPLATES / "icon-180.png").read_bytes()
    assert hashlib.sha256(data).hexdigest() == LOGO_180_SHA256
    assert len(data) == 7489


def test_both_sizes_are_real_pngs_of_the_right_size():
    Image = pytest.importorskip("PIL.Image", reason="pillow is in the dev extra")
    for name, size in (("icon-180.png", 180), ("icon-32.png", 32)):
        with Image.open(TEMPLATES / name) as picture:
            assert picture.size == (size, size), name


def test_the_icon_is_served_at_every_name_a_browser_asks_for(client):
    app, _ = client
    for path in ("/icon-32.png", "/icon-180.png", "/favicon.ico"):
        answer = app.get(path)
        assert answer.status_code == 200, path
        assert answer.headers["content-type"] == "image/png", path
        assert answer.content[:8] == b"\x89PNG\r\n\x1a\n", path
        assert "max-age" in answer.headers.get("cache-control", ""), path


def test_favicon_ico_is_the_small_one(client):
    """A 7 KB picture behind an address every browser requests once per
    origin is not worth it when a 2 KB one is already there."""
    app, _ = client
    assert app.get("/favicon.ico").content == app.get("/icon-32.png").content


def test_every_page_points_at_it(client):
    app, _ = client
    body = app.get("/").text
    assert 'rel="icon"' in body and "/icon-32.png" in body
    assert 'rel="apple-touch-icon"' in body and "/icon-180.png" in body


# --- what the wheel has to contain --------------------------------------------

def _package_data():
    with open("pyproject.toml", "rb") as handle:
        return tomllib.load(handle)["tool"]["setuptools"]["package-data"]


def test_the_icons_are_packaged():
    assert "templates/*.png" in _package_data()["arcade.web"]


def test_the_word_list_is_packaged():
    """`arcade/seed.py` reads it at IMPORT time, so leaving it out of the
    wheel does not degrade anything -- the application does not start."""
    assert "bip39-english.txt" in _package_data()["arcade"]


def test_every_non_python_file_under_arcade_is_packaged():
    """The general form of both, so the next data file is caught when it is
    added rather than when somebody installs it.

    Each file is matched against the patterns of the package it lives
    under, by the path relative to that package's own directory -- which is
    what setuptools does with these globs.
    """
    import fnmatch

    with open("pyproject.toml", "rb") as handle:
        config = tomllib.load(handle)["tool"]["setuptools"]
    data = config["package-data"]
    directories = {package: pathlib.Path(*package.split("."))
                   for package in config["packages"]}

    missing = []
    for path in sorted(pathlib.Path("arcade").rglob("*")):
        if path.is_dir() or path.suffix == ".py" or "__pycache__" in path.parts:
            continue
        # The package this file belongs to is the deepest one containing it.
        owner = max((name for name, where in directories.items()
                     if where in path.parents),
                    key=lambda name: len(directories[name].parts), default=None)
        if owner is None:
            missing.append(f"{path} (in no declared package)")
            continue
        relative = str(path.relative_to(directories[owner]))
        if not any(fnmatch.fnmatch(relative, pattern)
                   for pattern in data.get(owner, [])):
            missing.append(str(path))
    assert missing == [], f"not in any package-data pattern: {missing}"


def test_a_clean_build_carries_every_data_file(tmp_path):
    """Every non-.py file under `arcade/` is in a wheel built from a clean
    copy of the tree. Not a list of names: the whole class.

    It is written this way on a test machine's argument, and the argument is right.
    Naming the three files that were missing today protects those three;
    comparing the whole tree against the wheel means the next person to add
    a data file does not have to remember `pyproject.toml` at all, because
    this fails and tells them.

    `*.egg-info` is stripped from the copy, and that is the point of the
    whole test. A stale `SOURCES.txt` lists every file in the tree and
    setuptools reuses it, so a wheel built HERE contained the wordlist that
    a fresh clone's wheel did not -- the build worked, and it worked for a
    reason that had nothing to do with the configuration being right. That
    is what made this invisible on this machine and fatal on somebody
    else's.
    """
    import shutil
    import subprocess
    import zipfile

    pytest.importorskip("pip")
    source = tmp_path / "src"
    # The working tree as it is, so a data file added and not yet committed
    # still has to ship -- minus the artefacts that would let the build
    # answer from something other than the configuration.
    shutil.copytree(
        ".", source,
        ignore=shutil.ignore_patterns("*.egg-info", "__pycache__", ".venv",
                                      ".git", "build", "dist", "*.pyc"))

    built = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "-q",
         "-w", str(tmp_path / "out"), str(source)],
        capture_output=True, text=True)
    if built.returncode != 0:
        pytest.skip(f"no wheel build available here: {built.stderr[-200:]}")
    wheels = list((tmp_path / "out").glob("*.whl"))
    assert wheels, "pip built no wheel"
    inside = set(zipfile.ZipFile(wheels[0]).namelist())

    wanted = [
        str(path) for path in sorted(pathlib.Path("arcade").rglob("*"))
        if path.is_file() and path.suffix != ".py"
        and "__pycache__" not in path.parts
    ]
    assert wanted, "no data files found at all -- the sweep is broken"
    missing = [name for name in wanted if name not in inside]
    assert missing == [], (
        f"in the source tree but not in the wheel: {missing}. Add a pattern "
        f"for them under [tool.setuptools.package-data] -- a file that is "
        f"only in the checkout works here and is absent on every installed "
        f"copy.")

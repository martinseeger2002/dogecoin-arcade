"""Packages (arcade/packages.py), themes first: a folder `arcade-cli new theme` makes, read whole or refused, its id
the hash of every byte, and its values written out as CSS. A theme can hold no script, reach nothing outside itself,
and make nothing too hard to read a send in."""
import json
from pathlib import Path

import pytest

from arcade import packages
from arcade.lite import cli


def _edit(folder: Path, change) -> Path:
    manifest = json.loads((folder / "manifest.json").read_text())
    change(manifest)
    (folder / "manifest.json").write_text(json.dumps(manifest))
    return folder


def test_the_template_is_a_theme_and_its_id_follows_every_byte(tmp_path):
    folder = packages.template(tmp_path, "sunset")
    manifest, files = packages.read(folder)
    assert manifest["kind"] == "theme" and sorted(files) == ["README.md", "manifest.json"]
    first = packages.package_id(files)
    assert first == packages.package_id(packages.read(folder)[1]), "the same bytes, the same id"
    _edit(folder, lambda m: m["look"]["light"].update(accent="#2050b0"))
    assert packages.package_id(packages.read(folder)[1]) != first, "a changed theme is a new package"
    with pytest.raises(packages.PackageError, match="already there"):
        packages.template(tmp_path, "sunset")


@pytest.mark.parametrize("change, said", [
    (lambda m: m["look"]["light"].update(text="#f0f0f0"), "too hard to read"),
    (lambda m: m["look"]["dark"].update(bad="#2a1f1f"), "too hard to read"),
    (lambda m: m["look"]["light"].update(panel="red; background:url(https://x)"), "is a colour"),
    (lambda m: m["look"]["light"].pop("bar"), "sets every colour"),
    (lambda m: m["look"].update(font="https://fonts.example/x.woff2"), "look.font"),
    (lambda m: m["look"].update(radius=40), "radius"),
    (lambda m: m.update(kind="app"), "kind"),
    (lambda m: m.update(api=2), "api"),
])
def test_a_theme_that_could_hide_a_send_or_reach_outside_is_refused(tmp_path, change, said):
    folder = _edit(packages.template(tmp_path, "sunset"), change)
    with pytest.raises(packages.PackageError, match=said):
        packages.read(folder)


@pytest.mark.parametrize("name", ["files/run.js", "style.css", "files/logo.svg", "files/x.woff2.html"])
def test_a_theme_holds_values_and_fonts_only(tmp_path, name):
    folder = packages.template(tmp_path, "sunset")
    (folder / name).parent.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text("alert(1)")
    with pytest.raises(packages.PackageError, match="not something a theme may hold"):
        packages.read(folder)


def test_a_link_out_of_the_folder_is_refused(tmp_path):
    folder = packages.template(tmp_path, "sunset")
    (tmp_path / "secret").write_text("x")
    (folder / "files" / "a.woff2").symlink_to(tmp_path / "secret")
    with pytest.raises(packages.PackageError, match="is a link"):
        packages.read(folder)


def test_the_css_is_the_values_over_both_looks_and_a_font_of_its_own(tmp_path):
    folder = _edit(packages.template(tmp_path, "sunset"), lambda m: m["look"].update(font="files/sun.woff2"))
    (folder / "files" / "sun.woff2").write_bytes(b"wOF2")
    manifest, _ = packages.read(folder)
    css = packages.theme_css(manifest, "/lite/packages/abc")
    assert "--win:#efefef;--bg:#efefef" in css and "--sel-ink:#ffffff;--accent-fg:#ffffff" in css
    assert "prefers-color-scheme:dark" in css and "[data-theme=dark]" in css
    assert "url('/lite/packages/abc/files/sun.woff2')" in css and "http" not in css


def test_arcade_cli_makes_a_theme_folder_here_without_the_arcade(tmp_path, capsys):
    assert cli.main(["new", "theme", "sunset", str(tmp_path)]) == 0
    assert (tmp_path / "sunset" / "manifest.json").is_file() and "trytheme" in capsys.readouterr().out
    assert cli.main(["new", "theme", "Not A Name", str(tmp_path)]) == 1

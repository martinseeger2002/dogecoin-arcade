"""The search box (_pick.html) and the picture picker (base.html) under names of their own (2026-10-28).

Both were `arcadePick`. base.html's `function arcadePick(input, drawText)` is on every page, so _pick.html's
`window.arcadePick = window.arcadePick || ...` never installed the search box: Admin -> Ords -> Tokens, Admin -> LoRa,
the Exchange's Trade any pair and Explore's token box each called the picture picker with their settings and threw
"drawText is not a function"."""
import pathlib
import re

TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "arcade/web/templates"


def test_the_search_box_is_installed_under_a_name_nothing_else_uses():
    pick = (TEMPLATES / "_pick.html").read_text()
    assert "window.arcadeSearchBox = window.arcadeSearchBox || function (box, cfg) {" in pick
    for page in TEMPLATES.iterdir():
        if page.name == "_pick.html" or page.suffix not in (".html", ".js", ".css"):
            continue
        text = page.read_text()
        assert not re.search(r"(function\s+arcadeSearchBox\b|arcadeSearchBox\s*=)", text), \
            f"{page.name} defines arcadeSearchBox too"


def test_every_page_with_a_search_box_calls_it_by_its_own_name():
    users = [p for p in TEMPLATES.glob("*.html") if '{% include "_pick.html" %}' in p.read_text()]
    assert {p.name for p in users} >= {"admin.html", "exchange.html", "explore.html"}
    for page in users:
        text = page.read_text()
        assert "window.arcadeSearchBox(" in text, page.name
        assert "window.arcadePick(" not in text, f"{page.name} calls the picture picker with a search box's settings"


def test_the_picture_picker_keeps_its_name_for_the_composers():
    base = (TEMPLATES / "base.html").read_text()
    assert "function arcadePick(input, drawText){" in base
    assert "arcadePick(input, drawPicked);" in (TEMPLATES / "messages.html").read_text()
    assert 'arcadePick($("file"), quotePicked);' in (TEMPLATES / "_account_inscribe.html").read_text()

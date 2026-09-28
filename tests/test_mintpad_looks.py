"""Every mintpad look builds one self-contained page (2026-09-28: "add
the multiple styles of mint pads for both tokens and [NFTs]")."""
import json
import re

import pytest

from arcade import mintpad

ME = "nMeAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


@pytest.mark.parametrize("look", mintpad.LOOKS)
def test_every_nft_look_is_one_small_page(look):
    page = mintpad.account_page(ME, "Doge <Punks>", look=look, blurb="hi").decode()
    said = json.loads(re.search(r"id=pad>(.*?)</script>", page).group(1))
    assert said["look"] == look and "<h1>Doge &lt;Punks&gt;</h1>" in page
    assert len(page.encode()) < 16_000, "one transaction"
    assert page.count("<script") == 2


@pytest.mark.parametrize("look", mintpad.TOKEN_LOOKS)
def test_every_token_look_is_one_small_page(look):
    page = mintpad.account_token_page(ME, 14, "Ghost <b>", 100, 1000, look=look,
                                      icon="a" * 64).decode()
    said = json.loads(re.search(r"id=pad>(.*?)</script>", page).group(1))
    assert said["look"] == look and "Ghost <b>" not in page
    assert len(page.encode()) < 16_000


def test_the_wizard_offers_every_look():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "arcade/web"
    wizard = (root / "templates/mintpad_new.html").read_text()
    for look in mintpad.TOKEN_LOOKS:
        assert f'name="tlook" value="{look}"' in wizard, look
    app = (root / "app.py").read_text()
    for look in mintpad.LOOKS:
        assert f'"{look}": "' in app, look

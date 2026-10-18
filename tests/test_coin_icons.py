"""Every chain's coin on market icons (2026-10-18: "the LTC icon is showing the PEPTEST icon. We have the LTC
icon in the race condition game"): Litecoin's from the racer, its testnet coin purple like PEP TEST's, and a chain
added later gets its face from a coin-<network>.png alone."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_web import app_state, client  # noqa: F401,E402

from arcade.web import door  # noqa: E402


def test_each_chain_has_its_own_coin(client):
    app, _state = client
    from arcade.web import app as appmod
    tpl = appmod.TEMPLATES.env.from_string('{% import "_exui.html" as ex %}'
                                           '{% for n in ["main","test","doge","ltc","ltctest","nope"] %}'
                                           '{{ ex.coin_img(n, "c") }}|{% endfor %}')
    got = tpl.render().split("|")
    assert 'src="/coin-pep.png"' in got[0] and 'src="/coin-pep-test.png"' in got[1]
    assert 'src="/coin-doge.png"' in got[2] and 'src="/coin-ltc.png" alt="LTC"' in got[3]
    assert 'src="/coin-ltctest.png" alt="LTC TEST"' in got[4]
    assert "<img" not in got[5], "a chain with no picture gets its letter, never another chain's coin"
    for name in ("ltc", "ltctest", "pep", "pep-test", "doge"):
        r = app.get(f"/coin-{name}.png")
        assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
        assert door.public_path(f"/coin-{name}.png")
    assert app.get("/coin-..%2Fapp.png").status_code == 404
    assert app.get("/coin-nothing.png").status_code == 404

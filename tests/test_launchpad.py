"""The launchpad (2026-09-25): templates in, the existing forms out.

It signs nothing, so what matters is that it is reachable from the Exchange,
that a stranger may open it, and that its last step really does arrive at the
token form and the collection run with the choices filled in.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_me_page import EDGE, _seat, public                    # noqa: F401,E402


def test_the_exchange_leads_to_the_launchpad(client):
    app, _ = client
    assert 'href="/launch"' in app.get("/exchange").text
    page = app.get("/launch")
    assert page.status_code == 200
    for words in ("Launchpad", "Meme coin", "Community points",
                  "Profile-picture collection", "Open art series"):
        assert words in page.text, words


def test_a_stranger_may_look_at_it(public):
    app, _ = public
    page = app.get("/launch", headers=EDGE)
    assert page.status_code == 200 and "Launchpad" in page.text
    assert "'/me/nfts?'" in page.text, "an account's collection goes to its own run"


def test_the_token_form_arrives_filled_in(public):
    """Through an account, whose form is the one a public copy draws."""
    app, state = public
    pubkey = _seat(app)
    state.set_setting(f"address:{pubkey}", "nLaunchAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    for net in ("regtest", "test", "main"):
        state.set_setting(f"address:{net}:{pubkey}", "nLaunchAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    page = app.get("/tokens?launch=token&name=Moon%20Dust&supply=21000000"
                   "&units=indivisible&kind=fixed", headers=EDGE).text
    assert 'value="Moon Dust"' in page and 'value="21000000"' in page
    plain = app.get("/tokens?name=Moon%20Dust", headers=EDGE).text
    assert 'value="Moon Dust"' not in plain, "only the launchpad prefills"

"""Where to report a security problem (RFC 9116; 2026-11-08, security review: /.well-known/security.txt was a 404)."""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

OUTSIDE = {"host": "arcade.example.com"}


def test_security_txt_names_a_contact_and_an_expiry_open_to_anyone(client):
    app, state = client
    state.set_setting("public_hosts", ["arcade.example.com"])
    try:
        got = app.get("/.well-known/security.txt", headers=OUTSIDE)
        assert got.status_code == 200 and got.headers["content-type"].startswith("text/plain")
        lines = dict(line.split(": ", 1) for line in got.text.strip().splitlines())
        assert lines["Contact"] == "mailto:hello@arcade-nodes.com"
        expires = time.mktime(time.strptime(lines["Expires"], "%Y-%m-%dT%H:%M:%SZ"))
        assert time.time() < expires < time.time() + 366 * 86400, "a date within a year, as the RFC asks"
        state.set_setting("security_contact", "security@my-arcade.example")
        assert "Contact: mailto:security@my-arcade.example" in app.get("/.well-known/security.txt", headers=OUTSIDE).text
    finally:
        state.set_setting("public_hosts", [])
        state.set_setting("security_contact", None)

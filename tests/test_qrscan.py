"""Scan QR on the Wallet's Send (2026-10-17): the module, its reader, and the page using them."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from test_web import app_state, client                          # noqa: F401,E402

from arcade.web import door                                      # noqa: E402


def test_the_scanner_and_its_reader_are_served_and_public(client):
    app, _ = client
    js = app.get("/qrscan.js")
    assert js.status_code == 200 and "export function scanQR" in js.text and "/vendor/jsQR.js" in js.text
    assert app.get("/vendor/jsQR.js").status_code == 200
    assert door.public_path("/qrscan.js")


def test_the_send_form_offers_it():
    page = (pathlib.Path(__file__).resolve().parent.parent / "arcade/web/templates/my_wallet.html").read_text()
    assert 'import {scanQR, parseQR} from "/qrscan.js";' in page and "Scan QR" in page

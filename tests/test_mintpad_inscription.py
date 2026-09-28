"""A mintpad as an inscription (2026-09-27: "Launchpad should also be a
regular inscription so that it can be easily shared to the feed with a
/content/ command", and "the content should be available on anyone who is
running Dogecoin arcade")."""

import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

from arcade import mintpad                                        # noqa: E402

SELLER = "ndTq6goKXGb6JLoRRQPwks2kn6bWeGKX1G"


def test_every_look_is_one_transaction_and_names_what_it_sells():
    for look in mintpad.LOOKS:
        page = mintpad.account_page(SELLER, "Pixel Skull", look, "Spin for a skull")
        assert len(page) < 7000, f"{look} would need a second transaction"
        text = page.decode()
        data = json.loads(re.search(r'<script type=application/json id=pad>(.*?)</script>',
                                    text).group(1))
        assert data == {"creator": SELLER, "collection": "Pixel Skull", "look": look}
        assert "/r/mintpad/" in text and "/r/collection/" in text, "it reads, it does not bake"
        assert "arcade:'mint'" in text, "and it asks the page around it to buy"


def test_a_strange_name_is_a_name_and_not_markup():
    page = mintpad.account_page(SELLER, "</script><b>x</b>", "lottery",
                                "<img src=x onerror=alert(1)>").decode()
    assert "<b>x</b>" not in page and "<img src=x" not in page
    assert "<\\/script>" in page, "inside the JSON a closing tag cannot close the script"


def test_the_page_around_a_pad_answers_its_mint_request_with_the_buy_card(client):
    app, state = client
    body = app.get("/feed").text
    assert 'm.arcade !== "mint"' in body and "iframe.inscription-frame" in body
    assert "wallet.checked(offer, held)" in body, "the price comes off the bytes"


def test_only_a_seller_can_put_a_pad_on_the_chain(client):
    from test_me_page import _seat
    app, state = client
    _seat(app)
    answer = app.post("/account/mintpad/inscribe", json={"collection": "Not Mine"})
    assert answer.status_code in (400, 404), answer.text


def test_a_token_mintpad_is_one_transaction_in_every_look():
    """2026-09-27: token mintpads in the wizard. The page sells a lot at
    a time out of the seller's standing ask, reading /r/book live."""
    for look in mintpad.TOKEN_LOOKS:
        page = mintpad.account_token_page(SELLER, 14, "Ghost Credits", 100 * 10**8,
                                          10_000 * 10**8, look, "spend them", "ab" * 32)
        assert len(page) < 7000, look
        text = page.decode()
        data = json.loads(re.search(r'<script type=application/json id=pad>(.*?)</script>',
                                    text).group(1))
        assert data["property"] == 14 and data["lot"] == 100 * 10**8 and data["look"] == look
        assert "/r/book/" in text and "arcade:'take'" in text
    odd = mintpad.account_token_page(SELLER, 14, "<b>x</b>", 5, 0, "counter",
                                     "<img src=x onerror=1>").decode()
    assert "<b>x</b>" not in odd and "<img src=x" not in odd


def test_the_book_is_readable_by_a_page_and_a_bad_token_pad_is_refused(client):
    from test_me_page import _seat
    app, state = client
    assert app.get("/r/book/99999").status_code == 404
    _seat(app)
    answer = app.post("/account/tokenpad/inscribe", json={"property_id": 99999, "lot": "1"})
    assert answer.status_code == 400, answer.text

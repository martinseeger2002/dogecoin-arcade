"""The Dogecoin address goes out with the others (2026-10-15: "when we broadcast our test net and Pepe main net
addresses. We also broadcast our Doge address so that others can send us Doge via our @tag"; "the next time anyone logs
in that hasn't broadcasted their doge coin address ... it is automatically broadcast")."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                                 # noqa: F401,E402

from arcade.messaging import envelope as E                             # noqa: E402

KEY = bytes(range(32))
ME, OTHER, DOGE = b"\x11" * 20, b"\x22" * 20, b"\x33" * 20


def test_the_announcement_carries_the_dogecoin_address_last():
    body = E.build_key_announcement(KEY, hash160=ME, other_hash160=OTHER, tag="apple", bio="hi",
                                    url="https://x.example", doge_hash160=DOGE)
    said = E.parse_announced_extras(body)
    assert said["doge_hash160"] == DOGE and said["other_hash160"] == OTHER
    assert said["tag"] == "apple" and said["bio"] == "hi" and said["url"] == "https://x.example"
    assert body.endswith(bytes([E.ANNOUNCE_TAG_DOGE]) + DOGE), "last, so an older reader still reads the rest"
    plain = E.build_key_announcement(KEY, hash160=ME, other_hash160=OTHER, tag="apple")
    assert E.parse_announced_extras(plain)["doge_hash160"] == b""


def test_an_older_reader_still_gets_everything_before_it():
    body = E.build_key_announcement(KEY, hash160=ME, other_hash160=OTHER, tag="apple", doge_hash160=DOGE)
    # An older reader stops at the first section it does not know; everything it knew came first.
    cut = body[:body.index(bytes([E.ANNOUNCE_TAG_DOGE]) + DOGE)]
    assert E.parse_announced_extras(cut)["tag"] == "apple"


def test_the_store_keeps_it_and_a_newer_row_fills_it_in(client):
    app, state = client
    with state.store() as store:
        store.add_key_announcement("aa" * 32, "nAddr", KEY, "fp", 10, 1, stated=True, other_address="PMain")
        store.add_key_announcement("aa" * 32, "nAddr", KEY, "fp", 10, 1, stated=True, other_address="PMain",
                                   doge_address="DDoge")
        row = store.key_for("nAddr")
        assert row["doge_address"] == "DDoge" and row["other_address"] == "PMain"


def test_a_profile_wallet_lists_every_coin_name_left_balance_right(client):
    app, state = client
    import test_feed_web as F
    F.test_a_profile_wallet_shows_what_they_hold_not_what_you_hold(client)
    body = " ".join(app.get("/u/them/wallet").text.split())
    assert "<h2 style=\"margin:0\">Coins</h2>" in body
    coin = state.messaging.params.coin
    assert f'{coin}</span> <span class="pw-amt">85.10</span>' in body, body[body.find("Coins"):][:600]


def test_a_key_published_before_dogecoin_counts_as_not_published_so_the_page_publishes_again(client, monkeypatch):
    from test_me_page import _seat
    from test_dogecoin_network import _with_doge
    from test_funding import _pubkey
    from arcade.script import b58check_encode, hash160
    app, state = client
    _with_doge(state, monkeypatch, ready=True)
    _seat(app)
    net = state.messaging.network
    home = b58check_encode(state.messaging.params.pubkeyhash_version, hash160(_pubkey(int("41" * 32, 16))))
    doge = b58check_encode(30, hash160(_pubkey(int("42" * 32, 16))))
    assert app.post("/account/address", json={"address": home, "chain": net}).status_code == 200
    assert app.post("/account/address", json={"address": doge, "chain": "doge"}).status_code == 200
    with state.store() as store:
        store.add_key_announcement("b1" * 32, home, KEY, "fp", 100, 1, stated=True)      # mined, no Dogecoin address
    said = app.get("/account").json()
    assert said["announced"] is False and said["announcing"] is False, "out of date: the page's check publishes again"
    with state.store() as store:
        store.add_key_announcement("b2" * 32, home, KEY, "fp", 0, 0, stated=True, doge_address=doge)  # in the pool
    said = app.get("/account").json()
    assert said["announced"] is False and said["announcing"] is True, "on its way: not published a second time"
    with state.store() as store:
        store.add_key_announcement("b2" * 32, home, KEY, "fp", 101, 2, stated=True, doge_address=doge)  # mined
    assert app.get("/account").json()["announced"] is True

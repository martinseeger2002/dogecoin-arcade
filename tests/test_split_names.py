"""A name another arcade holds is not signed up fresh here (issue #15, 2026-10-25): players "Played on another arcade",
then made a new character here, with a new empty wallet, and lost their way to the real one. The name check and the
sign-up ask the arcades this one knows (faked here, never the network), the pages say "Played on another arcade? Sign
in", and Admin lists the names already split, for the operator to decide case by case."""
import json
import sys
import time
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_game_door import OUTSIDE, _ashvale                     # noqa: E402

from arcade import script                                         # noqa: E402
from arcade.web import app as appmod                              # noqa: E402
from arcade.web.app import create_app                             # noqa: E402
from arcade.web.state import AppState, ChainContext               # noqa: E402

BLOB = {"sealed": "c2VhbGVk", "salt": "00", "nonce": "00", "iterations": 600000}
LOCAL = {"host": "127.0.0.1:8420"}
THEIRS, NEW = "ab" * 32, "11" * 32
ADDR = script.b58check_encode(113, bytes(range(20)))
OTHER = script.b58check_encode(113, bytes(range(20, 40)))


def _arcade(tmp_path, name):
    (tmp_path / name).mkdir()
    st = AppState(home=tmp_path / name,
                  messaging=ChainContext(network="regtest", role="messaging", label="Testnet",
                                         datadir=Path("/nonexistent")),
                  ledger=ChainContext(network="main", role="ledger", label="Mainnet", datadir=Path("/nonexistent")))
    return st, TestClient(create_app(st))


class _Answer:
    def __init__(self, r):
        self.status_code, self._r = r.status_code, r

    def json(self):
        return self._r.json()


@pytest.fixture
def two(tmp_path, no_nodes, monkeypatch):
    """Two arcades: `home`, where @wanderer was made, and `here`, which knows home by its mesh announcement. Every
    ask of https://home.example.com is answered by home's own app."""
    class Index:                                      # the chain has no claim for any name yet
        def address_of(self, wanted):
            return None
    monkeypatch.setattr(AppState, "token_index", lambda self, chain: Index())
    home, home_client = _arcade(tmp_path, "home")
    here, here_client = _arcade(tmp_path, "here")
    home.vault().put("wanderer", THEIRS, ADDR, json.dumps(BLOB))
    with here.store() as store:
        store.add_mesh_announcement("t1", here.messaging.network, "nHome", "wss://home.example.com/mesh", 443,
                                    "cd" * 32, 0, 0)
    asked = []

    def get(url, timeout=0, headers=None, **kwargs):
        assert kwargs.get("allow_redirects") is False
        assert url.startswith("https://home.example.com/") and "here=1" in url, url
        asked.append(url)
        return _Answer(home_client.get(url.replace("https://home.example.com", "")))
    monkeypatch.setattr(appmod.requests, "get", get)
    monkeypatch.setattr(appmod, "_host_is_public", lambda host: True)
    return home, here, here_client, asked


def _sign_up(app, tag, pubkey, address=OTHER):
    return app.post("/signup", json={"tag": tag, "pubkey": pubkey, "address": address, "blob": BLOB})


def test_the_name_check_says_a_name_is_held_on_another_arcade(two):
    _, _, app, asked = two
    said = app.get("/signup/wanderer").json()
    assert said["free"] is False and said["elsewhere"] == "home.example.com"
    assert "already has an account on home.example.com: sign in instead" in said["detail"]
    free = app.get("/signup/newcomer").json()
    assert free["free"] is True and free["elsewhere"] == ""
    app.get("/signup/wanderer")
    assert len([u for u in asked if "/signin/wanderer" in u]) == 1, "kept a while, not asked again"


def test_sign_up_refuses_a_name_another_arcade_holds_under_another_key(two):
    _, here, app, _ = two
    refused = _sign_up(app, "wanderer", NEW)
    assert refused.status_code == 400
    assert "already has an account on home.example.com: sign in instead" in refused.json()["detail"]
    assert here.vault().get("wanderer") is None, "no new empty wallet under their name"
    assert "arcade_session" not in app.cookies


def test_the_same_key_elsewhere_is_the_same_person_bringing_their_account(two):
    _, here, app, _ = two
    brought = _sign_up(app, "wanderer", THEIRS, address=ADDR)
    assert brought.status_code == 200, brought.text
    assert here.vault().get("wanderer")["pubkey"] == THEIRS


def test_sign_up_still_works_when_no_other_arcade_answers(two, monkeypatch):
    _, here, app, _ = two

    def down(url, **kwargs):
        raise requests.ConnectionError("no route to host")
    monkeypatch.setattr(appmod.requests, "get", down)
    assert app.get("/signup/wanderer").json()["free"] is True, "nobody could say otherwise"
    assert _sign_up(app, "wanderer", NEW).status_code == 200
    assert here.vault().get("wanderer")["pubkey"] == NEW


def test_a_slow_arcade_never_holds_the_sign_up_up_and_its_late_answer_is_kept(two, monkeypatch):
    home, _, app, _ = two
    monkeypatch.setattr(appmod, "NAME_CHECK_WAIT", 0.1)
    passed = appmod.requests.get

    def slow(url, **kwargs):
        time.sleep(0.6)
        return passed(url, **kwargs)
    monkeypatch.setattr(appmod.requests, "get", slow)
    began = time.time()
    said = app.get("/signup/wanderer").json()
    assert time.time() - began < 0.5, "the name check did not wait for the slow arcade"
    assert said["free"] is True
    time.sleep(1.0)
    later = app.get("/signup/wanderer").json()
    assert later["free"] is False and later["elsewhere"] == "home.example.com", "its answer, kept when it came"


def test_no_other_arcade_known_signs_up_as_before(tmp_path, no_nodes, monkeypatch):
    _, app = _arcade(tmp_path, "alone")

    def never(url, **kwargs):
        raise AssertionError("nobody to ask")
    monkeypatch.setattr(appmod.requests, "get", never)
    assert app.get("/signup/wanderer").json()["free"] is True
    assert _sign_up(app, "wanderer", NEW).status_code == 200


def test_admin_lists_the_local_names_another_arcade_holds_under_another_key(two, monkeypatch):
    home, here, app, _ = two
    monkeypatch.setattr(appmod, "SPLIT_ASK_PACE", 0)
    here.vault().put("wanderer", NEW, OTHER, json.dumps(BLOB))        # split: a new empty wallet here
    home.vault().put("traveller", "cd" * 32, ADDR, json.dumps(BLOB))
    here.vault().put("traveller", "cd" * 32, ADDR, json.dumps(BLOB))  # brought here: the same key
    here.vault().put("homebody", "ef" * 32, OTHER, json.dumps(BLOB))  # only here
    outside = app.get("/admin/api/split-names", headers={"host": "node.example.com", "cf-ray": "x"})
    assert outside.status_code in (401, 403, 404), "not for anybody outside"
    said = app.get("/admin/api/split-names", headers=LOCAL).json()
    for _ in range(100):
        if not said["running"]:
            break
        time.sleep(0.05)
        said = app.get("/admin/api/split-names", headers=LOCAL).json()
    assert said["running"] is False and said["hosts"] == ["home.example.com"] and said["names"] == 3
    assert said["asked"] == 3
    assert [n["tag"] for n in said["split"]] == ["wanderer"]
    split = said["split"][0]
    assert split["pubkey"] == NEW and split["elsewhere"] == [{"host": "home.example.com", "pubkey": THEIRS,
                                                              "address": ADDR}]
    assert here.vault().get("wanderer") is not None, "read only: nothing removed"
    assert "split-names" in app.get("/admin", headers=LOCAL).text, "shown on the Admin page"


def test_the_join_page_and_the_game_door_offer_sign_in_for_another_arcades_player(client):
    app, state = client
    join = app.get("/join").text
    assert "Played on another arcade?" in join and 'id="to-signin"' in join
    _ashvale(state)
    door = app.get("/play/ashvale", headers=OUTSIDE).text
    assert "Played on another arcade? Sign in" in door and 'id="to-in-top"' in door

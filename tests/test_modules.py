"""Editions and modules (arcade/modules.py): Full is everything, as it was; ARCADE Lite is the crypto cores only, with
the Lite window as its home screen; every other module switches on and off, from Admin or the console, without
deleting anything. The edition is chosen once: by the installer's question, by arcade-web --edition, or by the
first-run question in Admin."""
import io
import json
import pathlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from nacl.signing import SigningKey

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "installer"))
from test_web import app_state  # noqa: F401,E402
from test_auth_web import _sign_in  # noqa: E402
import install  # noqa: E402

from arcade import modules as modulelib  # noqa: E402
from arcade.web.app import create_app  # noqa: E402
from arcade.web.watcher import PHASE_JOBS, BlockWatcher  # noqa: E402

ADMIN = {"x-arcade-admin": "1"}
OUTSIDE = {"host": "arcade.example.com", "cf-connecting-ip": "198.51.100.9"}


def served(app) -> set[str]:
    return {getattr(r, "path", "") for r in app.router.routes}


@pytest.fixture
def full(app_state):
    return create_app(app_state)


@pytest.fixture
def lite(app_state):
    app_state.set_setting("edition", "lite")
    return create_app(app_state)


# --- the registry ---------------------------------------------------------------------------------------------------


def test_every_route_belongs_to_a_module_by_a_claim_of_its_own(full):
    """A route added later without a claim would run in Lite unseen: every one is claimed, and none under /account,
    /admin or /me only by the catch-all of its first segment."""
    for path in served(full):
        claim = modulelib.claim_of(path)
        assert claim is not None, f"{path} belongs to no module (arcade/modules.py)"
        assert claim[0] not in ("/account", "/admin", "/me") or claim[0] == path, \
            f"{path} is claimed only by {claim[0]}: name it under its module"


def test_every_claim_names_a_route_and_every_module_is_well_formed(full):
    paths = served(full)
    for m in modulelib.MODULES.values():
        assert m.kind in modulelib.KINDS and m.title and m.summary
        assert all(n in modulelib.MODULES for n in m.needs)
        for prefix in m.routes:
            assert any(p == prefix or p.startswith(prefix + "/") for p in paths), f"{m.id} claims {prefix}: no route"


def test_a_node_that_never_chose_is_full_and_a_fresh_one_asks_once():
    assert modulelib.edition({}) == "full" and modulelib.enabled({}) == frozenset(modulelib.MODULES)
    assert modulelib.edition({"edition": "ask"}) == "full" and modulelib.asking({"edition": "ask"})
    assert modulelib.first_choice({}, fresh=True) == "ask"
    assert modulelib.first_choice({}, fresh=False) is None                     # an existing node is never asked
    assert modulelib.first_choice({}, fresh=False, given="lite") == "lite"    # unless told on the command line
    assert modulelib.first_choice({"edition": "ask"}, fresh=False, given="lite") == "lite"
    assert modulelib.first_choice({"edition": "full"}, fresh=True, given="lite") is None   # chosen is chosen


# --- Lite and Full ----------------------------------------------------------------------------------------------------


def test_a_lite_node_serves_only_the_crypto_cores(lite):
    for path in served(lite):
        assert modulelib.MODULES[modulelib.module_of(path)].core, f"{path} runs in Lite"
    client = TestClient(lite)
    # Messages is part of the core (2026-11-04): its pages run in Lite as the wallet's do
    for path in ("/wallet", "/wallet/tools", "/contacts", "/backup", "/admin", "/docs", "/lite", "/inbox"):
        assert client.get(path).status_code == 200, path
    for path in ("/feed", "/exchange", "/explore", "/apps", "/desktop", "/home", "/dogefeedx/feed",
                 "/v1/models", "/affiliates"):
        assert client.get(path).status_code == 404, path
    home = client.get("/")
    assert home.status_code == 200 and 'data-tab="overview"' in home.text and "Open the full arcade" not in home.text
    page = client.get("/wallet").text
    nav = page.split("<nav", 1)[1].split("</nav>", 1)[0]
    assert 'href="/feed"' not in nav and 'href="/exchange"' not in nav and 'href="/#send"' in nav, "the window's tabs"
    assert "every page stays the website" in page                          # no Desktop View in Lite


def test_a_full_node_is_unchanged(full, app_state):
    every = {getattr(r, "path", "") for r in full.router.routes}
    assert {"/feed", "/exchange", "/desktop", "/dogefeedx/feed", "/lite", "/admin/api/modules"} <= every
    assert served(full) == every and len(full.router.routes) == len(set(map(id, full.router.routes)))
    client = TestClient(full)
    assert client.get("/feed").status_code == 200
    home = client.get("/")
    assert home.status_code == 200 and 'data-tab="overview"' not in home.text
    nav = home.text.split("<nav", 1)[1].split("</nav>", 1)[0]
    assert "every page stays the website" not in home.text and 'href="/exchange"' in nav


def test_lite_serves_users_as_full_does(lite, app_state):
    app_state.set_setting("public_hosts", ["arcade.example.com"])
    client = TestClient(lite)
    visitor = client.get("/", headers=OUTSIDE)
    assert visitor.status_code == 200 and 'href="/join"' in visitor.text
    assert client.get("/join", headers=OUTSIDE).status_code == 200
    key = SigningKey.generate()
    assert _sign_in(client, key, headers=OUTSIDE).status_code == 200
    page = client.get("/", headers=OUTSIDE)
    assert page.status_code == 200 and 'id="w-form"' in page.text and "Edition and modules" not in page.text
    for asked in ("/wallet.js", "/vendor/qrcode.js", "/account", "/account/activity", "/me/wallet", "/messaging.js"):
        assert client.get(asked, headers=OUTSIDE).status_code == 200, f"Lite's tabs need {asked}"
    assert client.get("/node-mail.js", headers={"host": "127.0.0.1:8420"}).status_code == 200, "the operator's book"
    assert client.get("/me/contacts", headers=OUTSIDE).status_code == 200
    assert client.get("/feed", headers=OUTSIDE).status_code == 404
    app_state.set_setting("public_hosts", [])


class _Phases(BlockWatcher):
    """A watcher whose phases only say they ran."""
    def __init__(self, state):
        super().__init__(state)
        self.ran = []
        for name in [*PHASE_JOBS, "_auto_update", "_sync_ledgers", "_run_watch", "_run_trader", "_run_xchain",
                     "_keep_shop", "_walk_home", "_check"]:
            def phase(*_a, _name=name, **_k):
                self.ran.append(_name)
            phase.__name__ = name
            setattr(self, name, phase)


def test_lite_runs_only_the_cores_jobs_and_every_settlement_job(app_state):
    full = _Phases(app_state)
    full._tick()
    ticked = set(PHASE_JOBS) - {"_check_pending_offers", "_check_listers"}      # those two are the pool loop's
    assert ticked <= set(full.ran)
    app_state.set_setting("edition", "lite")
    lite = _Phases(app_state)
    lite._tick()
    # Messages' own job is the core's now; every other module's is off
    assert not (set(PHASE_JOBS) - {"_confirm_sent"}) & set(lite.ran) and "_confirm_sent" in lite.ran
    # what was agreed finishes whatever the switches say (arcade/web/docs/exchange-safety.md)
    assert {"_run_watch", "_run_trader", "_run_xchain", "_keep_shop", "_sync_ledgers", "_walk_home"} <= set(lite.ran)
    for job in ("ord-index", "sites-search", "lora", "known-collections", "book-relay", "card-watch"):
        assert not modulelib.job_on(app_state.settings(), job) and modulelib.job_on({}, job), job
    assert modulelib.job_on(app_state.settings(), "push-mail"), "Messages runs in every edition"
    assert modulelib.job_on(app_state.settings(), "mesh") and modulelib.job_on(app_state.settings(), "chain-scan")


# --- switching ---------------------------------------------------------------------------------------------------------


def test_admin_switches_a_module_on_and_off_at_once(lite, app_state):
    client = TestClient(lite)
    assert client.get("/feed").status_code == 404
    said = client.post("/admin/api/modules", headers=ADMIN, json={"module": "feed", "on": True}).json()
    assert next(m for m in said["modules"] if m["id"] == "feed")["on"]
    assert said["restart_for"] == ["Feed"]                                   # its jobs wait for a restart
    assert client.get("/feed").status_code == 200 and "/feed" in served(lite)
    client.post("/admin/api/modules", headers=ADMIN, json={"module": "feed", "on": False})
    assert client.get("/feed").status_code == 404
    # a module's needs come with it, and go with it
    client.post("/admin/api/modules", headers=ADMIN, json={"module": "dogefeedx", "on": True})
    assert client.get("/feed").status_code == 200 and client.get("/dogefeedx/feed").status_code == 200
    client.post("/admin/api/modules", headers=ADMIN, json={"module": "feed", "on": False})
    assert client.get("/dogefeedx/feed").status_code == 404
    assert app_state.setting("modules")["dogefeedx"] is True                # comes back with feed
    assert client.post("/admin/api/modules", headers=ADMIN, json={"module": "wallet", "on": False}).status_code == 400
    assert client.post("/admin/api/modules", json={"module": "feed", "on": True}).status_code == 403


def test_changing_edition_turns_things_on_or_off_and_deletes_nothing(full, app_state):
    client = TestClient(full)
    kept = app_state.home / "feed-and-trades.sqlite"
    kept.write_bytes(b"the operator's data")
    app_state.set_setting("bio", "mine")
    client.post("/admin/api/modules", headers=ADMIN, json={"module": "llm", "on": False})
    said = client.post("/admin/api/modules", headers=ADMIN, json={"edition": "lite"}).json()
    assert said["edition"] == "lite" and {m["id"] for m in said["modules"] if m["on"]} == {
        m.id for m in modulelib.MODULES.values() if m.core}
    assert client.get("/exchange").status_code == 404 and 'data-tab="overview"' in client.get("/").text
    assert kept.read_bytes() == b"the operator's data" and app_state.setting("bio") == "mine"
    said = client.post("/admin/api/modules", headers=ADMIN, json={"edition": "full"}).json()
    assert all(m["on"] for m in said["modules"])                           # llm too: Full is everything
    assert client.get("/exchange").status_code == 200 and client.get("/v1/models").status_code != 404
    assert client.post("/admin/api/modules", headers=ADMIN, json={"edition": "max"}).status_code == 400


def test_admin_has_the_panel_and_hides_the_tabs_of_modules_that_are_off(full, app_state):
    client = TestClient(full)

    def tabs():
        return client.get("/admin").text.split('class="admintabs"')[1].split("</nav>")[0]
    assert 'data-tab="trading"' in tabs() and 'data-tab="modules"' in tabs()
    assert 'id="modules"' in client.get("/admin").text
    app_state.set_setting("edition", "lite")
    assert 'data-tab="trading"' not in tabs() and 'data-tab="modules"' in tabs()


def test_a_fresh_install_asks_in_admin_until_the_operator_chooses(app_state):
    app_state.set_setting("edition", "ask")
    client = TestClient(create_app(app_state))
    assert 'id="editionask"' in client.get("/admin").text
    assert client.get("/feed").status_code == 200                           # Full until chosen
    client.post("/admin/api/modules", headers=ADMIN, json={"edition": "lite"})
    assert 'id="editionask"' not in client.get("/admin").text
    assert client.get("/feed").status_code == 404 and 'data-tab="overview"' in client.get("/").text


def test_a_node_that_never_chose_is_never_asked(full):
    assert 'id="editionask"' not in TestClient(full).get("/admin").text


# --- the installer --------------------------------------------------------------------------------------------------


def test_the_installer_asks_a_fresh_install_and_never_an_existing_one(tmp_path, capsys):
    home = tmp_path / "home"
    answers = iter(["maybe", "l"])
    assert install.choose_edition(home, None, ask=lambda _q: next(answers), interactive=True) == "lite"
    assert "ARCADE Lite" in capsys.readouterr().out
    assert install.choose_edition(home, None, ask=lambda _q: "", interactive=True) == "full"
    assert install.choose_edition(home, None, interactive=False) == "ask"      # nobody at the terminal: Admin asks
    assert install.choose_edition(home, "lite", interactive=False) == "lite"
    home.mkdir()
    (home / "test.sqlite").write_bytes(b"")                                     # an arcade already runs here
    assert install.choose_edition(home, None, ask=pytest.fail, interactive=True) is None
    assert install.choose_edition(home, "lite", interactive=False) == "lite"
    (home / "settings.json").write_text(json.dumps({"edition": "full", "auto_update": False}))
    assert install.choose_edition(home, "lite", interactive=True, ask=pytest.fail) is None


def test_the_installer_records_the_edition_and_keeps_every_other_setting(tmp_path, monkeypatch):
    monkeypatch.delenv("ARCADE_BRAND", raising=False)
    home = tmp_path / "home"
    install.write_edition(home, "lite", dry_run=False, fresh=True)
    assert json.loads((home / "settings.json").read_text()) == {"edition": "lite", "brand": "core"}, \
        "a new arcade starts with the neutral face"
    (home / "settings.json").write_text(json.dumps({"auto_update": False, "brand": "pepecoin"}))
    install.write_edition(home, "ask", dry_run=False)
    assert json.loads((home / "settings.json").read_text()) == {"auto_update": False, "brand": "pepecoin",
                                                                "edition": "ask"}
    (home / "settings.json").write_text("{half a file")
    install.write_edition(home, "lite", dry_run=False)
    assert (home / "settings.json").read_text() == "{half a file"
    install.write_edition(tmp_path / "dry", "lite", dry_run=True)
    assert not (tmp_path / "dry").exists()


def test_the_installer_offers_the_question_and_the_flag(monkeypatch, capsys):
    with pytest.raises(SystemExit):
        install.main(["--help"])
    out = capsys.readouterr().out
    assert "--edition {full,lite}" in out
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))                         # not a terminal: never blocks
    assert install.choose_edition(Path("/nonexistent/home"), None) == "ask"


def test_every_background_thread_the_app_starts_names_its_job():
    """A thread started while the app is built runs on every edition unless its module is asked first: found on the
    first real Lite install, where the card watch (Apps) ran. Each such start in app.py asks job_on."""
    import re
    source = (Path(__file__).resolve().parent.parent / "arcade" / "web" / "app.py").read_text()
    starts = [m.start() for m in re.finditer(r'"PYTEST_CURRENT_TEST" not in os.environ', source)]
    assert starts, "the build-time starts moved: look for them again"
    for at in starts:
        assert "modulelib.job_on(" in source[at:at + 300], source[at - 200:at + 300]


def test_lite_wears_no_coins_face_until_its_operator_picks_one(app_state, monkeypatch):
    """2026-11-03, SadFrog, on the first real Lite install: "It should not have any default theme ... it should be og
    core like if anything by default"."""
    from arcade.web import brand as brandlib
    monkeypatch.delenv("ARCADE_BRAND", raising=False)
    client = TestClient(create_app(app_state))
    assert "DogecoinArcade" in client.get("/wallet").text, "Full keeps its face"
    app_state.set_setting("edition", "lite")
    page = client.get("/wallet").text
    assert brandlib.own() is brandlib.CORE and "DogecoinArcade" not in page.split("<body", 1)[0]
    assert "<title>ARCADE Lite</title>" in client.get("/").text
    assert brandlib.CORE.icon("icon-192.png") is not None, "its own icons, not the cabinet"
    app_state.set_setting("brand", "pepecoin")                  # the operator's pick (Admin -> Face) wins in Lite too
    monkeypatch.setenv("ARCADE_BRAND", "pepecoin")               # what brand.use() sets for it, restored after
    assert brandlib.own() is brandlib.PEPECOIN and "PepecoinArcade" in client.get("/wallet").text


def test_a_new_arcade_starts_neutral_and_an_existing_one_keeps_its_face(tmp_path, monkeypatch):
    """2026-11-03, SadFrog: "Full should also be rebranded by default to a neutral theme labeled Arcade". New arcades
    only: one already running, DogecoinArcade by default, looks the same after an update."""
    from arcade.web import brand as brandlib
    monkeypatch.delenv("ARCADE_BRAND", raising=False)
    assert brandlib.first_face({}, fresh=True) == "core" and brandlib.BRANDS["core"].name == "ARCADE"
    assert brandlib.first_face({}, fresh=False) is None
    assert brandlib.first_face({"brand": "pepecoin"}, fresh=True) is None
    home = tmp_path / "old"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({"auto_update": False}))
    install.write_edition(home, "lite", dry_run=False, fresh=False)        # --edition on an arcade already here
    assert "brand" not in json.loads((home / "settings.json").read_text())

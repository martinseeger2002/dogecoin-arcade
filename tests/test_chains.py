"""Which chains a node hosts (arcade/chains.py): the two Pepecoin chains always, every other
chain an add-on its operator switches on or off in Admin -> "Face and chains" (2026-10-17,
SadFrog's side: "everything should work as a module but additive system")."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_web import app_state, client  # noqa: F401,E402
from test_admin import LOCAL, WRITE, public  # noqa: F401,E402

from arcade import chains as chainlib, litecoin as ltclib  # noqa: E402


def test_defaults_dogecoin_on_litecoin_off():
    assert chainlib.hosted({}, "doge") is True
    assert chainlib.hosted({}, "ltc") is False
    assert chainlib.hosted({"chains:doge": False, "chains:ltc": True}, "doge") is False
    assert chainlib.hosted({"chains:doge": False, "chains:ltc": True}, "ltc") is True


def test_a_restart_is_offered_only_when_it_would_change_something():
    rows = lambda settings, running, present: {r["key"]: r for r in chainlib.overview(
        settings, doge_running=running, ltc=None, doge_available=present)}
    assert rows({"chains:doge": False}, True, True)["doge"]["restart_needed"], "off, still running"
    assert rows({}, False, True)["doge"]["restart_needed"], "on, node there, not running yet"
    assert not rows({}, False, False)["doge"]["restart_needed"], "on, but no node to find"
    assert not rows({}, True, True)["doge"]["restart_needed"]
    req = rows({}, True, True)
    assert req["main"]["required"] and req["test"]["required"] and req["main"]["hosted"]


def test_dogecoin_switched_off_is_not_started(tmp_path, monkeypatch):
    from arcade.web import __main__ as launcher
    (tmp_path / "settings.json").write_text(json.dumps({"chains:doge": False}))
    args = argparse.Namespace(no_doge=False, doge_conf="/x/dogecoin.conf", doge_datadir=None,
                              home=str(tmp_path))
    assert launcher._doge_context(args) is None
    (tmp_path / "settings.json").write_text(json.dumps({"chains:doge": True}))
    assert launcher._doge_context(args) is not None


def test_the_panel_lists_chains_and_saves_litecoin(public, monkeypatch):
    app, state = public
    monkeypatch.setattr(ltclib, "installed", lambda: True)
    started = []
    monkeypatch.setattr(ltclib, "set_running", lambda on: started.append(on) or {})
    monkeypatch.setattr(ltclib, "status", lambda: {"installed": True, "service": "inactive"})
    chains = {c["key"]: c for c in app.get("/admin/api/state", headers=LOCAL).json()["chains"]}
    assert set(chains) == {"main", "test", "doge", "ltc"} and chains["ltc"]["hosted"] is False
    said = app.post("/admin/api/settings", headers={**LOCAL, **WRITE}, json={"chains": {"ltc": True}})
    assert said.status_code == 200 and started == [True]
    assert state.setting("chains:ltc") is True
    bad = app.post("/admin/api/settings", headers={**LOCAL, **WRITE}, json={"chains": {"btc": True}})
    assert bad.status_code >= 400
    page = app.get("/admin", headers=LOCAL).text
    assert "Chains this node hosts" in page and 'id="savechains"' in page


def test_litecoin_cannot_be_hosted_without_its_node(public, monkeypatch):
    app, state = public
    monkeypatch.setattr(ltclib, "installed", lambda: False)
    said = app.post("/admin/api/settings", headers={**LOCAL, **WRITE}, json={"chains": {"ltc": True}})
    assert said.status_code >= 400 and "not installed" in said.text
    assert not state.setting("chains:ltc")


def test_restart_is_refused_when_not_the_service(public, monkeypatch):
    app, _ = public
    monkeypatch.setattr(chainlib, "restart_self", lambda: (False, "not running as the service"))
    said = app.post("/admin/api/restart", headers={**LOCAL, **WRITE})
    assert said.status_code == 409 and said.json()["restarting"] is False

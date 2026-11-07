"""Admin -> Edition & modules -> Add a node module (2026-11-06): the node modules that come with the arcade's source
(examples/packages) are added with a button, by the operator at the machine or signed in from elsewhere, where before
only `installmodule <folder>` typed at the machine could. Only those: no folder is named from outside."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_admin import EDGE, LOCAL, PASSWORD, WRITE, _operator, public  # noqa: F401,E402

from arcade import modules as modulelib, packages as packagelib  # noqa: E402

ADMIN = {**LOCAL, **WRITE}


def _shipped(app, headers=LOCAL):
    return {p["name"]: p for p in app.get("/admin/api/modules", headers=headers).json()["shipped"]}


def test_the_arcades_own_modules_are_listed_and_added_off(client):
    app, state = client
    dd = _shipped(app)["digidollar"]
    assert dd["title"] == "DigiDollar" and dd["added"] is None and dd["same"] is False
    said = app.post("/admin/api/modules", headers=ADMIN, json={"add": "digidollar"})
    assert said.status_code == 200
    row = {m["id"]: m for m in said.json()["modules"]}["digidollar"]
    assert row["package"] and row["on"] is False, "kept, and off until the operator ticks it"
    assert said.json()["shipped"][0]["same"] is True
    have = [r for r in state.settings()[packagelib.SETTING].values() if r["name"] == "digidollar"]
    assert len(have) == 1 and have[0]["version"] == dd["version"]
    assert app.post("/admin/api/modules", headers=ADMIN, json={"module": "digidollar", "on": True}).status_code == 200
    assert "digidollar" in modulelib.enabled(state.settings())
    again = app.post("/admin/api/modules", headers=ADMIN, json={"add": "digidollar"})
    assert again.status_code == 200 and len(state.settings()[packagelib.SETTING]) == 1, "the same one, kept once"


def test_only_a_module_that_came_with_the_arcade_is_added(client):
    app, state = client
    for name in ("../../etc", "/tmp/anything", "nosuchmodule"):
        refused = app.post("/admin/api/modules", headers=ADMIN, json={"add": name})
        assert refused.status_code == 400 and "comes with this arcade" in refused.json()["detail"]
    assert not state.settings().get(packagelib.SETTING)


def test_the_operator_from_elsewhere_adds_one_a_stranger_cannot(public):
    """Node 3 is a server reached from outside: its operator could not add a module at all before."""
    app, state = public
    assert app.post("/admin/api/modules", headers={**EDGE, **WRITE}, json={"add": "digidollar"}).status_code in (
        401, 403, 404)
    _operator(app, state)
    assert app.post("/admin/login", headers=EDGE, json={"password": PASSWORD}).status_code == 200
    assert _shipped(app, EDGE)["digidollar"]["added"] is None
    assert app.post("/admin/api/modules", headers={**EDGE, **WRITE}, json={"add": "digidollar"}).status_code == 200
    assert _shipped(app, EDGE)["digidollar"]["same"] is True


def test_the_panel_draws_the_add_list():
    js = (pathlib.Path(__file__).resolve().parent.parent / "arcade/web/templates/modules_js.js").read_text()
    assert "Add a node module" in js and "call({add: p.name})" in js and "Tick it on under Apps" in js

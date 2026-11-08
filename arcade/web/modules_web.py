"""Admin -> Edition & modules, and the gate that keeps a switched-off module's routes out (arcade/modules.py).

Routes follow a switch at once: the app keeps every route it was built with and serves only those whose module is
on. Background jobs are started once, so they follow a switch at the next restart, and the panel says so.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import Body, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from .. import modules as modulelib
from .. import packages as packagelib

NO_STORE = {"Cache-Control": "no-store"}


def gate(app, state):
    """Serve only the routes of modules that are on; returns the function that applies a change of switches."""
    every = list(app.router.routes)

    def apply() -> None:
        settings = state.settings()
        app.router.routes = [r for r in every if modulelib.path_on(settings, getattr(r, "path", ""))]
        _mining(state, "mining" in modulelib.enabled(settings))

    apply()
    return apply


def _mining(state, on: bool) -> None:
    """Mining's background work follows its switch at once (2026-11-04): off stops a mining run and the stratum
    server; on starts the stratum server again if the operator had it on."""
    from .. import testmine
    service = getattr(state, "stratum", None)
    try:
        if not on:
            testmine.stop(state)
            if service is not None:
                service.stop()
        elif service is not None and service.server is None and service.conf().get("on"):
            service.apply()
    except Exception:                                      # noqa: BLE001 -- the switch holds; the work follows later
        pass


def audience_of(request: Request, admin_check, signed_in) -> str:
    """"operator", "user" or "anyone", as the Lite window decides it (arcade/lite/web.py caller_for)."""
    try:
        admin_check(request)
        return "operator"
    except HTTPException:
        pass
    return "user" if signed_in is not None and signed_in(request) is not None else "anyone"


def guard(app, state, admin_check, signed_in) -> None:
    """Who may use each module that is on (Admin -> Edition & modules, "Who may use it"): a caller below a module's
    access gets "not on this arcade" from its pages and actions, as if it were off. Its read APIs and inscriptions'
    content stay open (modules.OPEN_ALWAYS), so other arcades' pages and inscribed apps keep working."""

    @app.middleware("http")
    async def module_access(request: Request, call_next):
        path = request.url.path
        settings = state.settings()
        mid = modulelib.module_of(path)
        if modulelib.access(settings, mid) != "anyone" and not path.startswith(modulelib.OPEN_ALWAYS):
            if not modulelib.may_use(settings, mid, audience_of(request, admin_check, signed_in)):
                return PlainTextResponse("Not on this arcade.", status_code=404, headers=NO_STORE)
        return await call_next(request)


def needs(app, state) -> None:
    """An app that reaches for a switched-off module is told which one (2026-11-08, SadFrog: "warn what other modules
    are needed when starting an app"; ASHVALE on a Lite node with Rooms off started a new character, because the
    game never found the Bank that keeps the hero). The page API of a module that is off answers instead of a bare
    404: its script (/r/realtime.js, /r/llm.js ...) runs and tells the page's viewer, which warns over the app and
    names the module; its other /r/ calls say which module is off. Nothing of the module's API is defined, so a page
    that checks for it (window.arcade.realtime) still finds it missing."""

    @app.middleware("http")
    async def module_off(request: Request, call_next):
        path = request.url.path
        if path.startswith("/r/"):
            mid = modulelib.module_of(path)
            if mid is not None and not getattr(modulelib.MODULES.get(mid), "core", True):
                missing = modulelib.off(state.settings())
                if mid in missing:
                    return _off_answer(mid, missing[mid], path)
        return await call_next(request)


def _off_answer(mid: str, title: str, path: str) -> Response:
    said = f"{title} is switched off on this arcade"
    headers = {**NO_STORE, "Access-Control-Allow-Origin": "*"}
    if path.endswith(".js"):
        note = json.dumps({"arcade": "needs", "module": mid})
        script = (f"// {said}, so {path} is not here (Admin -> Edition & modules).\n"
                  f"(function () {{\n"
                  f"  try {{ console.warn({json.dumps(said + ': ' + path + ' is not here.')}); }} catch (e) {{}}\n"
                  f"  try {{ if (window.parent && window.parent !== window) window.parent.postMessage({note}, '*'); }}"
                  f" catch (e) {{}}\n"
                  f"}})();\n")
        return Response(script, media_type="application/javascript", headers=headers)
    return JSONResponse({"ok": False, "error": said, "module": mid, "title": title}, status_code=404,
                        headers=headers)


def register(app, state, admin_check, signed_in=None) -> None:
    """The panel's calls, then the gate over every route the app has (so this goes last in create_app)."""
    #: What ran when this arcade started: a module whose switch moved since then has jobs waiting for a restart.
    started = modulelib.enabled(state.settings())

    def shipped() -> list:
        """The node modules that come with this arcade's source: the tree it runs from, else the installer's checkout
        (an installed arcade runs from site-packages, which has no examples/)."""
        return packagelib.shipped(dict.fromkeys([state.checkout, Path.home() / ".dogecoinarcade" / "src"]))

    def answer() -> JSONResponse:
        settings = state.settings()
        out = modulelib.describe(settings)
        moved = started ^ modulelib.enabled(settings)
        out["restart_for"] = [m["title"] for m in out["modules"] if m["id"] in moved and getattr(modulelib.MODULES.get(m["id"]), "jobs", ())]
        have = {r.get("name"): (pid, r) for pid, r in (settings.get(packagelib.SETTING) or {}).items()
                if isinstance(r, dict) and r.get("kind") == "module"}
        out["shipped"] = [{"name": m["name"], "title": m.get("title") or m["name"], "version": m["version"],
                           "summary": m.get("summary", ""), "added": have.get(m["name"], ("", {}))[1].get("version"),
                           "same": have.get(m["name"], ("",))[0] == pid} for _, m, pid in shipped()]
        return JSONResponse(out, headers=NO_STORE)

    @app.get("/admin/modules.js")
    def admin_modules_js():
        """The one drawing of Edition & modules, for Admin's tab and the Lite window's Options (templates/modules_js.js)."""
        return Response((Path(__file__).parent / "templates" / "modules_js.js").read_text(),
                        media_type="application/javascript", headers={"Cache-Control": "no-cache"})

    @app.get("/admin/api/modules")
    def admin_modules(request: Request):
        admin_check(request)
        return answer()

    @app.post("/admin/api/modules")
    def admin_modules_set(request: Request, payload: Any = Body(None)):
        """{edition: "full" | "lite", look?}, {look: one of LOOKS}, {module: id, on: true | false}, {module: id,
        access: "operator" | "user" | "anyone"}, or {add: name}. A change of edition sets every module to that edition's
        default: Lite to Full turns everything on, Full to Lite switches off all but the core. Nothing is deleted either
        way, and who may use each module stays as the operator said. Lite opens in ArcadeCore unless a look is said with
        it; Full opens in the look said with it (the first-start question asks), else as it did. `add` keeps a node
        module that comes with this arcade (`shipped`), off until switched on: only those, so an operator signed in from
        elsewhere names no folder on this machine."""
        admin_check(request, write=True)
        said = payload if isinstance(payload, dict) else {}
        if "look" in said and said["look"] not in modulelib.LOOKS:
            raise HTTPException(400, "the look is one of " + ", ".join(modulelib.LOOKS))
        if "add" in said:
            from ..lite import commands as cmdlib
            found = [(folder, m) for folder, m, _ in shipped() if m["name"] == str(said["add"])]
            if not found:
                raise HTTPException(400, "no node module of that name comes with this arcade")
            try:
                cmdlib.keep_module(state, *found[0])
            except cmdlib.CommandError as exc:
                raise HTTPException(400, str(exc)) from None
        elif "edition" in said:
            if said["edition"] not in modulelib.EDITIONS:
                raise HTTPException(400, "the edition is full or lite")
            state.set_setting(modulelib.EDITION_SETTING, said["edition"])
            state.set_setting(modulelib.SWITCH_SETTING, None)
            state.set_setting(modulelib.LOOK_SETTING, said.get("look") or ("core" if said["edition"] == "lite" else None))
        elif "look" in said:
            state.set_setting(modulelib.LOOK_SETTING, said["look"])
        elif "module" in said and "access" in said:
            try:
                levels = modulelib.set_access(state.settings(), str(said["module"]), str(said["access"]))
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
            state.set_setting(modulelib.ACCESS_SETTING, levels)
        elif "module" in said:
            try:
                switches = modulelib.switch(state.settings(), str(said["module"]), bool(said.get("on")))
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
            state.set_setting(modulelib.SWITCH_SETTING, switches)
        else:
            raise HTTPException(400, "say an edition, a look, a module and on, or a module to add")
        apply()
        return answer()

    guard(app, state, admin_check, signed_in)
    needs(app, state)
    apply = gate(app, state)
    state.apply_modules = apply                            # the console's install and remove (arcade/lite/commands.py)

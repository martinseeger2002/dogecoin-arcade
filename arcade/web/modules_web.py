"""Admin -> Edition & modules, and the gate that keeps a switched-off module's routes out (arcade/modules.py).

Routes follow a switch at once: the app keeps every route it was built with and serves only those whose module is
on. Background jobs are started once, so they follow a switch at the next restart, and the panel says so.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Body, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from .. import modules as modulelib

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


def register(app, state, admin_check, signed_in=None) -> None:
    """The panel's calls, then the gate over every route the app has (so this goes last in create_app)."""
    #: What ran when this arcade started: a module whose switch moved since then has jobs waiting for a restart.
    started = modulelib.enabled(state.settings())

    def answer() -> JSONResponse:
        settings = state.settings()
        out = modulelib.describe(settings)
        moved = started ^ modulelib.enabled(settings)
        out["restart_for"] = [m["title"] for m in out["modules"] if m["id"] in moved and getattr(modulelib.MODULES.get(m["id"]), "jobs", ())]
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
        """{edition: "full" | "lite"}, {module: id, on: true | false}, or {module: id, access: "operator" | "user" |
        "anyone"}. A change of edition sets every module to that edition's default: Lite to Full turns everything on,
        Full to Lite switches off all but the core. Nothing is deleted either way, and who may use each module stays
        as the operator said."""
        admin_check(request, write=True)
        said = payload if isinstance(payload, dict) else {}
        if "edition" in said:
            if said["edition"] not in modulelib.EDITIONS:
                raise HTTPException(400, "the edition is full or lite")
            state.set_setting(modulelib.EDITION_SETTING, said["edition"])
            state.set_setting(modulelib.SWITCH_SETTING, None)
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
            raise HTTPException(400, "say an edition, or a module and on")
        apply()
        return answer()

    guard(app, state, admin_check, signed_in)
    apply = gate(app, state)
    state.apply_modules = apply                            # the console's install and remove (arcade/lite/commands.py)

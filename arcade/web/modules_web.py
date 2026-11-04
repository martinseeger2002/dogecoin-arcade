"""Admin -> Edition & modules, and the gate that keeps a switched-off module's routes out (arcade/modules.py).

Routes follow a switch at once: the app keeps every route it was built with and serves only those whose module is
on. Background jobs are started once, so they follow a switch at the next restart, and the panel says so.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Body, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .. import modules as modulelib

NO_STORE = {"Cache-Control": "no-store"}


def gate(app, state):
    """Serve only the routes of modules that are on; returns the function that applies a change of switches."""
    every = list(app.router.routes)

    def apply() -> None:
        settings = state.settings()
        app.router.routes = [r for r in every if modulelib.path_on(settings, getattr(r, "path", ""))]

    apply()
    return apply


def register(app, state, admin_check) -> None:
    """The panel's two calls, then the gate over every route the app has (so this goes last in create_app)."""
    #: What ran when this arcade started: a module whose switch moved since then has jobs waiting for a restart.
    started = modulelib.enabled(state.settings())

    def answer() -> JSONResponse:
        settings = state.settings()
        out = modulelib.describe(settings)
        moved = started ^ modulelib.enabled(settings)
        out["restart_for"] = [m["title"] for m in out["modules"] if m["id"] in moved and modulelib.MODULES[m["id"]].jobs]
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
        """{edition: "full" | "lite"}, or {module: id, on: true | false}. A change of edition sets every module to that
        edition's default: Lite to Full turns everything on, Full to Lite switches off all but the core. Nothing is
        deleted either way."""
        admin_check(request, write=True)
        said = payload if isinstance(payload, dict) else {}
        if "edition" in said:
            if said["edition"] not in modulelib.EDITIONS:
                raise HTTPException(400, "the edition is full or lite")
            state.set_setting(modulelib.EDITION_SETTING, said["edition"])
            state.set_setting(modulelib.SWITCH_SETTING, None)
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

    apply = gate(app, state)
    state.apply_modules = apply                            # the console's install and remove (arcade/lite/commands.py)

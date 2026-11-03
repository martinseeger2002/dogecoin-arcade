"""Lite's web layer: the Core-style window at /lite, and the commands behind it.

Everyone who reaches the arcade gets the window. A visitor sees the arcade itself; a signed-in user also sees their
own account; the operator also sees the node's workings. The page asks for nothing the console cannot: Overview is
the same commands, run as the same caller.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import Body, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from . import commands as cmdlib

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
NO_STORE = {"Cache-Control": "no-store"}
#: Sent by the Lite page with every console line; another site's page cannot add it without a preflight.
LITE_HEADER = "x-arcade-lite"


def register(app, state, admin_check, signed_in, account_address, name_of) -> None:
    """Add /lite to the node's app, with the web app's own checks: `admin_check(request, write=False)` raises unless
    the operator is asking, `signed_in(request)` is the account or None, `account_address(pubkey, chain)` and
    `name_of(address)` look an account up."""

    def caller_for(request: Request, write: bool = False) -> cmdlib.Caller:
        try:
            admin_check(request, write=write)
            audience = "operator"
        except HTTPException:
            audience = "anyone"
        account = signed_in(request)
        pubkey = getattr(account, "pubkey", None)
        if pubkey and audience == "anyone":
            audience = "user"
        return cmdlib.Caller(state, audience, pubkey,
                             address=(lambda chain: account_address(pubkey, chain)) if pubkey else (lambda chain: ""),
                             name_of=name_of)

    @app.get("/lite", response_class=HTMLResponse)
    def lite_page(request: Request):
        from ..web import brand as brandlib
        caller = caller_for(request)
        return TEMPLATES.TemplateResponse(request, "lite.html", {"name": brandlib.current().name,
                                                                 "audience": caller.audience}, headers=NO_STORE)

    @app.get("/lite/api/overview")
    def lite_overview(request: Request):
        caller = caller_for(request)
        out = {"audience": caller.audience}
        for key, name in (("node", "getnodeinfo"), ("chains", "listchains"), ("mesh", "getmeshinfo"),
                          ("arcades", "listarcades"), ("you", "whoami")):
            if not caller.may(cmdlib.COMMANDS[name].audience):
                continue
            try:
                out[key] = cmdlib.run(caller, name)
            except Exception as exc:                       # noqa: BLE001 -- one part failing still shows the rest
                out[key] = {"error": str(exc)}
        return JSONResponse(out, headers=NO_STORE)

    @app.get("/lite/api/commands")
    def lite_commands(request: Request):
        return JSONResponse([{"name": c.name, "category": c.category, "audience": c.audience, "usage": c.usage,
                              "summary": c.summary} for c in cmdlib.available(caller_for(request))],
                            headers=NO_STORE)

    @app.post("/lite/api/console")
    def lite_console(request: Request, payload: dict = Body(...)):
        from ..web import door as doorlib
        if request.headers.get(LITE_HEADER) != "1" or doorlib.cross_site(request.headers,
                                                                        request.headers.get("host", "")):
            raise HTTPException(403, "console lines come from the Lite page")
        caller = caller_for(request, write=True)
        line = str(payload.get("line") or "")
        try:
            return JSONResponse({"ok": True, "result": cmdlib.run_line(caller, line)}, headers=NO_STORE)
        except cmdlib.CommandError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, headers=NO_STORE)
        except Exception as exc:                           # noqa: BLE001 -- the console says what went wrong
            return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, headers=NO_STORE)

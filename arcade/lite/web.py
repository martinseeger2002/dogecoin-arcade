"""Lite's web layer: the Core-style window, and the commands behind it. It is ARCADE Lite's home screen at /, and
answers at /lite in either edition.

Everyone who reaches the arcade gets the window. A visitor sees the arcade itself; a signed-in user also sees their
own account; the operator also sees the node's workings. The page asks for nothing the console cannot: Overview is
the same commands, run as the same caller.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import Body, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from . import commands as cmdlib
from .. import modules as modulelib

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
NO_STORE = {"Cache-Control": "no-store"}
#: Sent by the Lite page with every console line; another site's page cannot add it without a preflight.
LITE_HEADER = "x-arcade-lite"


#: Console lines a non-operator may send each minute (the full arcade's terminal allows the same).
CONSOLE_PER_MINUTE = 30

def register(app, state, admin_check, signed_in, account_address, name_of, node_tools=None, public_tools=None,
             wallet_name=lambda request: "", account_view=lambda request: False, join_facts=None):
    """Add /lite to the node's app, with the web app's own checks: `admin_check(request, write=False)` raises unless
    the operator is asking, `signed_in(request)` is the account or None, `account_address(pubkey, chain)` and
    `name_of(address)` look an account up. `node_tools` is what the operator's commands ask of the node,
    `public_tools` what anyone's may, `wallet_name(request)` the name an account's wallet opens under, and
    `account_view(request)` whether the wallet shown is the account's rather than the node's, and `join_facts(request)`
    what Join says: seats, free, fruit, secure and why_not (app.py). Returns the window's page, which ARCADE Lite also serves at /."""

    def caller_for(request: Request, write: bool = False) -> cmdlib.Caller:
        local = False
        try:
            local = bool((admin_check(request, write=write) or {}).get("local"))
            audience = "operator"
        except HTTPException:
            audience = "anyone"
        account = signed_in(request)
        pubkey = getattr(account, "pubkey", None)
        if pubkey and audience == "anyone":
            audience = "user"
        return cmdlib.Caller(state, audience, pubkey,
                             address=(lambda chain: account_address(pubkey, chain)) if pubkey else (lambda chain: ""),
                             name_of=name_of, node=node_tools if audience == "operator" else None, local=local,
                             reads=public_tools)

    @app.get("/lite", response_class=HTMLResponse)
    def lite_page(request: Request):
        from ..web import brand as brandlib
        caller = caller_for(request)
        settings = state.settings()
        return TEMPLATES.TemplateResponse(request, "lite.html", {
            "name": brandlib.current().name, "audience": caller.audience,
            "edition": modulelib.edition(settings), "asking": modulelib.asking(settings),
            "apps": [m for m in modulelib.describe(settings)["modules"] if not m["core"]],
            # Whose wallet the Send, Receive and Transactions tabs are: the account's when one is signed in, even at
            # the node's machine, as the full arcade's Wallet decides it; else the node's, for the operator.
            "wallet": "account" if caller.account and account_view(request)
                      else "node" if caller.audience == "operator" else "",
            # What wallet.js unlocks an account's words with, as the full arcade's pages give it (base.html).
            "me": wallet_name(request) if caller.account else "", "account": bool(caller.account),
            "messages_on": "messages" in modulelib.enabled(settings), "csrf": state.csrf_token,
            "join": (join_facts or (lambda request: {"seats": 0, "free": 0, "secure": True}))(request),
            "tagchain": {"network": state.messaging.network, "version": state.messaging.params.pubkeyhash_version}},
            headers=NO_STORE)

    @app.get("/lite/pages.css")
    def lite_pages_css():
        """The Lite window's look for the full arcade's own pages, Admin and the rest (base.html, Lite edition)."""
        return Response((Path(__file__).parent / "templates" / "pages.css").read_text(), media_type="text/css",
                        headers={"Cache-Control": "no-cache"})

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

    #: Console lines a non-operator may send a minute, per address (2026-11-03, a test machine's review: /lite/api/console is
    #: public on every arcade; the full arcade's /api/terminal/exec allows 30 a minute).
    console_seen: dict[str, list[float]] = {}

    def console_limited(caller, request: Request) -> bool:
        if caller.audience == "operator":
            return False
        import time as _time
        now = _time.time()
        who = (caller.account or "") or (request.client.host if request.client else "")
        recent = [t for t in console_seen.get(who, ()) if now - t < 60]
        if len(recent) >= CONSOLE_PER_MINUTE:
            console_seen[who] = recent
            return True
        recent.append(now)
        console_seen[who] = recent
        if len(console_seen) > 10_000:                     # a flood of addresses: forget the oldest half
            for k in sorted(console_seen, key=lambda k: console_seen[k][-1])[:5_000]:
                console_seen.pop(k, None)
        return False

    @app.post("/lite/api/console")
    def lite_console(request: Request, payload: dict = Body(...)):
        from ..web import door as doorlib
        if request.headers.get(LITE_HEADER) != "1" or doorlib.cross_site(request.headers,
                                                                        request.headers.get("host", "")):
            raise HTTPException(403, "console lines come from the Lite page")
        caller = caller_for(request, write=True)
        if console_limited(caller, request):
            return JSONResponse({"ok": False, "error": f"at most {CONSOLE_PER_MINUTE} lines a minute; wait a little"},
                                status_code=429, headers=NO_STORE)
        line = str(payload.get("line") or "")
        try:
            return JSONResponse({"ok": True, "result": cmdlib.run_line(caller, line)}, headers=NO_STORE)
        except cmdlib.CommandError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, headers=NO_STORE)
        except Exception as exc:                           # noqa: BLE001 -- the console says what went wrong
            return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, headers=NO_STORE)

    return lite_page

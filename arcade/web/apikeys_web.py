"""API keys on the web (arcade/apikeys.py): the check every keyed call passes, and the calls that make and manage keys.

`register(app, ...)` adds the routes (before modules_web's gate, which keeps the routes it was built with);
`guard(app, state)` adds the middleware, last, so it runs first: a call that says a key is checked before the door
sees it, and an unknown, switched-off, ended or out-of-place key is refused there and then.

    GET  /api/v1/whoami            who this key is: the operator, or an account, and what it may do
    GET  /api/v1/routes            every route that runs here, by module (what a key opens, for Build -> API)
    POST /api/v1/run {line}        one console line, as the key's caller (arcade/lite/commands.py)
    GET/POST /admin/api/keys       the operator: every key, making one, and whether users may
    POST/DELETE /admin/api/keys/{id}
    GET/POST /account/keys         a signed-in account: its own keys, when the operator lets users have them
    POST/DELETE /account/keys/{id}
"""
from __future__ import annotations

import inspect
import time
from typing import Any

from fastapi import Body, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import apikeys as keylib
from .. import modules as modulelib

NO_STORE = {"Cache-Control": "no-store"}
SAFE = ("GET", "HEAD", "OPTIONS")


def _refuse(status: int, why: str) -> JSONResponse:
    return JSONResponse({"detail": why}, status_code=status, headers=NO_STORE)


def guard(app, state) -> None:
    """Check a call's key, if it says one, and carry it with the call (keylib.CURRENT)."""

    @app.middleware("http")
    async def api_key(request: Request, call_next):
        said = keylib.bearer(request.headers)
        if not said:
            if request.headers.get("authorization", "")[:10].lower() == "bearer ak_":
                return _refuse(401, "that is not an API key of this arcade")
            return await call_next(request)
        # The key's checks read (and now and then write) sqlite: on a thread, never the event loop that every request
        # waits on (2026-11-05 review: on a slow disk, every keyed call stalled the whole server).
        import anyio
        got = await anyio.to_thread.run_sync(lambda: _check(request, said))
        if isinstance(got, JSONResponse):
            return got
        mark = keylib.CURRENT.set(got)
        try:
            return await call_next(request)
        finally:
            keylib.CURRENT.reset(mark)

    def _check(request: Request, said: str):
        """The key, or the refusal to answer with."""
        store = state.api_keys()
        key = store.find(said)
        if key is None or key.off or (key.expires is not None and key.expires <= time.time()):
            return _refuse(401, "that API key is not one this arcade takes: unknown, switched off or ended")
        rules = keylib.settings_of(state.settings())
        if not key.operator and not rules["users"]:
            return _refuse(403, "this arcade does not take users' API keys now")
        client = getattr(request, "client", None)
        ip, edge = keylib.caller_ip(request.headers, client.host if client else None)
        if not keylib.where_ok(key.where, ip, edge):
            return _refuse(403, f"this API key works only from {key.where}")
        path, method = request.url.path, request.method.upper()
        if key.modules is not None and not path.startswith("/api/v1/") \
                and modulelib.module_of(path) not in key.modules:
            return _refuse(403, f"this API key does not open the {modulelib.module_of(path) or 'core'} module")
        # A key that reads only may still post a console line: /api/v1/run runs only the lines that read for it
        # (arcade/lite/commands.py, `writes`) and refuses the rest itself.
        if not key.write and method not in SAFE and not (path == "/api/v1/run" and method == "POST"):
            return _refuse(403, "this API key reads only")
        if store.too_many(key.id, 0 if key.operator else rules["per_minute"]):
            return _refuse(429, f"at most {rules['per_minute']} calls a minute with a user's key; wait a little")
        if key.used is None or time.time() - key.used > 60:
            store.touch(said)
        return key


def register(app, state, admin_check, signed_in, run_line) -> None:
    """The key routes. `run_line(request, line, password="", modules=None, reads_only=False)` runs a console line as
    the request's caller (arcade/lite/web.py). `password` is the admin password a node-wallet send asks of an operator
    key; `modules` and `reads_only` are the key's limits, which the line is held to."""
    store_box: dict[str, Any] = {}

    def store() -> keylib.KeyStore:
        if "s" not in store_box:
            store_box["s"] = keylib.KeyStore(state.home)
        return store_box["s"]
    state.api_keys = store

    def made(body: Any, owner: str) -> JSONResponse:
        said = body if isinstance(body, dict) else {}
        # Read only and 90 days unless asked otherwise (2026-11-08, security review: every key was full write, from
        # anywhere, for ever). Asked: "write": true, and "days" nothing or 0 for a key that does not end.
        days = said.get("days", keylib.DEFAULT_DAYS)
        expires = None
        if days not in (None, "", 0):
            try:
                expires = time.time() + float(days) * 86400
            except (TypeError, ValueError):
                raise HTTPException(400, "days is a number of days, or nothing for a key that does not end") from None
        modules = said.get("modules")
        if modules is not None:
            known = set(modulelib.MODULES) | {m.id for m in modulelib.packaged(state.settings())}
            if not isinstance(modules, list) or any(m not in known for m in modules):
                raise HTTPException(400, "modules is a list of this arcade's module ids, or nothing for every one")
        try:
            key, row = store().make(owner, said.get("name", ""), modules, bool(said.get("write", False)),
                                    str(said.get("where") or "anywhere"), expires)
        except keylib.KeyRefused as exc:
            raise HTTPException(400, str(exc)) from None
        return JSONResponse({"key": key, **row.public()}, headers=NO_STORE)

    # --- the operator: every key, and the say over users' --------------------------------------------------------
    @app.get("/admin/api/keys")
    def admin_keys(request: Request):
        admin_check(request)
        return JSONResponse({"keys": [k.public() | {"account": "" if k.operator else k.owner} for k in store().list()],
                             "rules": keylib.settings_of(state.settings())}, headers=NO_STORE)

    @app.post("/admin/api/keys")
    def admin_keys_make(request: Request, body: Any = Body(None)):
        """{name, modules?, write?, where?, days?}: a key that acts as the operator. Shown once."""
        admin_check(request, write=True)
        return made(body, "node")

    @app.post("/admin/api/keys/rules")
    def admin_keys_rules(request: Request, body: Any = Body(None)):
        """{users?, per_user?, per_minute?}: whether users may make keys, how many each, and calls a minute."""
        admin_check(request, write=True)
        said = body if isinstance(body, dict) else {}
        rules = keylib.settings_of(state.settings())
        rules.update({k: v for k, v in said.items() if k in keylib.DEFAULTS})
        state.set_setting(keylib.SETTING, rules)
        return JSONResponse({"rules": keylib.settings_of(state.settings())}, headers=NO_STORE)

    @app.post("/admin/api/keys/{kid}")
    def admin_key_switch(request: Request, kid: str, body: Any = Body(None)):
        """{off: true | false}, for any key: the operator's own and users'."""
        admin_check(request, write=True)
        if not store().set_off(None, kid, bool((body or {}).get("off"))):
            raise HTTPException(404, "no such key")
        return JSONResponse({"ok": True}, headers=NO_STORE)

    @app.delete("/admin/api/keys/{kid}")
    def admin_key_delete(request: Request, kid: str):
        admin_check(request, write=True)
        if not store().delete(None, kid):
            raise HTTPException(404, "no such key")
        return JSONResponse({"ok": True}, headers=NO_STORE)

    # --- an account's own keys -------------------------------------------------------------------------------------
    def account_of(request: Request) -> str:
        """The signed-in account making or managing its own keys. Never with a key: a key does not make keys."""
        if keylib.CURRENT.get() is not None and not keylib.CURRENT.get().operator:
            raise HTTPException(403, "a key does not make keys; sign in at the arcade for that")
        account = signed_in(request)
        if account is None:
            raise HTTPException(401, "sign in first")
        return account.pubkey.lower()

    @app.get("/account/keys")
    def account_keys(request: Request):
        owner = account_of(request)
        rules = keylib.settings_of(state.settings())
        return JSONResponse({"keys": [k.public() for k in store().list(owner)], "rules": rules}, headers=NO_STORE)

    @app.post("/account/keys")
    def account_keys_make(request: Request, body: Any = Body(None)):
        """{name, modules?, write?, where?, days?}: a key that acts as this account, as it would signed in."""
        owner = account_of(request)
        rules = keylib.settings_of(state.settings())
        if not rules["users"]:
            raise HTTPException(403, "this arcade's operator does not give users API keys now")
        if store().count(owner) >= rules["per_user"]:
            raise HTTPException(403, f"an account here holds at most {rules['per_user']} keys; delete one first")
        return made(body, owner)

    @app.post("/account/keys/{kid}")
    def account_key_switch(request: Request, kid: str, body: Any = Body(None)):
        # A body that is not an object reads as none, as null always did, not a 500 (2026-11-09, security review).
        said = body if isinstance(body, dict) else {}
        if not store().set_off(account_of(request), kid, bool(said.get("off"))):
            raise HTTPException(404, "no such key of yours")
        return JSONResponse({"ok": True}, headers=NO_STORE)

    @app.delete("/account/keys/{kid}")
    def account_key_delete(request: Request, kid: str):
        if not store().delete(account_of(request), kid):
            raise HTTPException(404, "no such key of yours")
        return JSONResponse({"ok": True}, headers=NO_STORE)

    # --- the API itself: who, what is there, and the console ------------------------------------------------------
    @app.get("/api/v1/whoami")
    def api_whoami(request: Request):
        key = keylib.CURRENT.get()
        account = signed_in(request)
        return JSONResponse({"key": key.public() if key else None,
                             "as": "operator" if key and key.operator else "account" if account else "visitor",
                             "account": account.pubkey if account is not None and key and not key.operator else None},
                            headers=NO_STORE)

    @app.get("/api/v1/routes")
    def api_routes(request: Request):
        """Every route that runs here, by module, with its methods and what its handler says it does. A key's own
        modules are marked, so Build -> API shows each key what it opens. A key's or a signed-in caller's only."""
        key = keylib.CURRENT.get()
        if key is None and signed_in(request) is None:
            try:
                admin_check(request)
            except HTTPException:
                raise HTTPException(401, "an API key, or sign in") from None
        groups: dict[str, dict] = {}
        for route in app.router.routes:
            path = getattr(route, "path", "")
            methods = sorted(m for m in (getattr(route, "methods", None) or ()) if m != "HEAD")
            if not path or not methods or path.startswith(("/docs", "/openapi", "/redoc")):
                continue
            mid = modulelib.module_of(path) or "node"
            module = modulelib.MODULES.get(mid)
            group = groups.setdefault(mid, {"id": mid, "title": module.title if module else mid,
                                            "opened": key is None or key.modules is None or mid in key.modules,
                                            "routes": []})
            doc = inspect.getdoc(getattr(route, "endpoint", None)) or ""
            group["routes"].append({"path": path, "methods": methods, "about": doc.split("\n\n", 1)[0][:300]})
        out = sorted(groups.values(), key=lambda g: (g["id"] != "node", g["title"].lower()))
        for g in out:
            g["routes"].sort(key=lambda r: r["path"])
        return JSONResponse({"modules": out}, headers=NO_STORE)

    @app.post("/api/v1/run")
    def api_run(request: Request, body: Any = Body(None)):
        """{line}: one console line (`help` lists them), run as this key's caller: the operator, or the account."""
        # A key's own (2026-11-05 review: with none it was the public console with none of its limits). The line is
        # held to the key's limits (2026-11-06): a key limited to some modules runs their commands and the core's
        # public reads, and a key that reads only runs no command that changes anything. Anything else is 403.
        key = keylib.CURRENT.get()
        if key is None:
            raise HTTPException(401, "an API key: Authorization: Bearer ak_...")
        line = str((body or {}).get("line") or "") if isinstance(body, dict) else ""
        if not line.strip():
            raise HTTPException(400, "say a line to run, such as help")
        password = str((body or {}).get("password") or "") if isinstance(body, dict) else ""
        return run_line(request, line, password, modules=key.modules, reads_only=not key.write)

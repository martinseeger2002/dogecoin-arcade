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
from .. import packages as packagelib

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
NO_STORE = {"Cache-Control": "no-store"}
FOREVER = {"Cache-Control": "public, max-age=31536000, immutable"}
#: Sent by the Lite page with every console line; another site's page cannot add it without a preflight.
LITE_HEADER = "x-arcade-lite"


#: How a node module's page is served: in a sandbox of its own origin, so its scripts never run as this arcade's.
MODULE_PAGE = {"Content-Security-Policy": "sandbox allow-scripts allow-forms allow-popups; default-src 'self' "
                                          "'unsafe-inline' data:; connect-src 'none'; form-action 'self'",
               "X-Content-Type-Options": "nosniff"}

def _icon(path: str) -> str:
    return (f'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">{path}'
            '</svg>')


#: Each module's icon on its tab in the window; one that has none of its own (a node module) gets "module".
ICONS = {"ords": _icon('<circle cx="12" cy="12" r="9"/><path d="M15 9l-2 4-4 2 2-4z"/>'),
         "feed": _icon('<path d="M4 6h16M4 12h16M4 18h10"/>'),
         "exchange": _icon('<path d="M4 8h13l-3-3M20 16H7l3 3"/>'),
         "tokens": _icon('<circle cx="9" cy="9" r="5"/><circle cx="15" cy="15" r="5"/>'),
         "collections": _icon('<rect x="4" y="4" width="16" height="12" rx="1"/><path d="M7 20h10"/>'),
         "apps": _icon('<rect x="4" y="4" width="7" height="7"/><rect x="13" y="4" width="7" height="7"/>'
                       '<rect x="4" y="13" width="7" height="7"/><rect x="13" y="13" width="7" height="7"/>'),
         "affiliates": _icon('<circle cx="8" cy="9" r="3"/><circle cx="16" cy="9" r="3"/><path d="M3 19c1-3 9-3 10 0'
                             'M11 19c1-3 9-3 10 0"/>'),
         "llm": _icon('<rect x="5" y="5" width="14" height="14" rx="2"/><path d="M9 9h6v6H9z"/>'),
         "module": _icon('<path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M12 12l8-4.5M12 12v9M12 12L4 7.5"/>')}

#: Console lines a non-operator may send each minute (the full arcade's terminal allows the same).
CONSOLE_PER_MINUTE = 30

def register(app, state, admin_check, signed_in, account_address, name_of, node_tools=None, public_tools=None,
             wallet_name=lambda request: "", account_view=lambda request: False, join_facts=None, offer_for=None,
             admin_password_ok=None):
    """Add /lite to the node's app, with the web app's own checks: `admin_check(request, write=False)` raises unless
    the operator is asking, `signed_in(request)` is the account or None, `account_address(pubkey, chain)` and
    `name_of(address)` look an account up. `node_tools` is what the operator's commands ask of the node,
    `public_tools` what anyone's may, `wallet_name(request)` the name an account's wallet opens under, and
    `account_view(request)` whether the wallet shown is the account's rather than the node's, and `join_facts(request)`
    what Join says: seats, free, fruit, secure and why_not (app.py). `offer_for(pubkey)` makes a node module's prepared
    transaction an offer that account's wallet signs: (network, unsigned) -> the offer, as /account/sign takes it.
    Returns the window's page, which ARCADE Lite also serves at /."""

    def caller_for(request: Request, write: bool = False, pubkeys: dict | None = None,
                   password: str = "") -> cmdlib.Caller:
        local = False
        who: dict = {}
        try:
            who = admin_check(request, write=write) or {}
            local = bool(who.get("local"))
            audience = "operator"
        except HTTPException:
            audience = "anyone"
        # An operator API key is not the machine (_admin_check). It may prepare a node-wallet send only with
        # the admin password. A person at the machine, and a remote admin session, keep today's prepare step;
        # confirm stays local-only either way.
        may_spend = True
        if who.get("key"):
            may_spend = bool(password) and admin_password_ok is not None and bool(
                admin_password_ok(str(password), request))
        account = signed_in(request)
        pubkey = getattr(account, "pubkey", None)
        if pubkey and audience == "anyone":
            audience = "user"
        return cmdlib.Caller(state, audience, pubkey,
                             address=(lambda chain: account_address(pubkey, chain)) if pubkey else (lambda chain: ""),
                             name_of=name_of, node=node_tools if audience == "operator" else None, local=local,
                             reads=public_tools, pubkeys=pubkeys if isinstance(pubkeys, dict) else {},
                             offer=offer_for(pubkey) if pubkey and offer_for else None, may_spend=may_spend)

    def tab_owner(caller, request: Request) -> str | None:
        """Whose choice of tabs this window shows: the account's when one is signed in and looking as itself, the
        node's for the operator, nobody's for a visitor (their browser keeps it)."""
        if caller.account and account_view(request):
            return caller.account.lower()
        return "node" if caller.audience == "operator" else None

    def module_tabs(caller, request: Request) -> list[dict]:
        settings = state.settings()
        return modulelib.tabs(settings, caller.audience, modulelib.hidden_tabs(settings, tab_owner(caller, request)))

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
            # The window's tabs after the core's: each module that is on, open to this caller and not hidden by
            # them, opened inside the window (2026-11-04, SadFrog: "when enabled should be a part of the ui").
            "tabs": [t for t in module_tabs(caller, request) if not t["hidden"]],
            "tab_owner": bool(tab_owner(caller, request)), "icons": ICONS,
            # DigiDollars in the wallet's own Send, Receive and balances, while the DigiDollar module is on and this
            # caller may use it (2026-11-06, SadFrog: "along side their wallet receives. and add them to send").
            "digidollar": any(t["id"] == "digidollar" for t in module_tabs(caller, request)),
            # Admin is a tab for the operator, at the machine or signed in from elsewhere. From elsewhere the admin
            # password's session is kept for /admin alone (its cookie's path), so this page never sees it: the
            # operator's account is enough to show the tab, and Admin itself asks for the password (2026-11-05).
            "admin_tab": caller.audience == "operator" or bool(
                caller.account and state.operator and caller.account.lower() == state.operator.lower()),
            # Window -> Build's guide (_build.html) names a theme's limits as packages.py holds them
            "fonts": list(packagelib.FONTS), "most_files": packagelib.MOST_FILES,
            "most_kb": packagelib.MOST_BYTES // 1000,
            "messages_on": "messages" in modulelib.enabled(settings), "apps_on": "apps" in modulelib.enabled(settings),
            "ords_on": "ords" in modulelib.enabled(settings), "csrf": state.csrf_token,
            "join": (join_facts or (lambda request: {"seats": 0, "free": 0, "secure": True}))(request),
            "tagchain": {"network": state.messaging.network, "version": state.messaging.params.pubkeyhash_version}},
            headers=NO_STORE)

    @app.get("/lite/pages.css")
    def lite_pages_css():
        """The Lite window's look for the full arcade's own pages, Admin and the rest (base.html, Lite edition)."""
        return Response((Path(__file__).parent / "templates" / "pages.css").read_text(), media_type="text/css",
                        headers={"Cache-Control": "no-cache"})

    @app.get("/lite/theme.css")
    def lite_theme_css(request: Request):
        """The theme this caller sees (arcade/packages.py), as CSS over the window's look; empty for Lite's own.

        The operator at the machine sees a folder they are trying (`trytheme`) instead, read afresh on every load,
        and what is wrong with it as a comment."""
        caller = caller_for(request)
        settings = state.settings()
        trying = settings.get(packagelib.TRY) if caller.audience == "operator" and caller.local else None
        try:
            if trying:
                manifest, _ = packagelib.read(Path(trying))
                css = packagelib.theme_css(manifest, "/lite/packages/try")
            else:
                owner = (caller.account.lower() if caller.account and account_view(request)
                         else "node" if caller.audience == "operator" else None)
                pid = packagelib.chosen(settings, owner)
                css = ""
                if pid:
                    manifest, files = packagelib.read(packagelib.home_of(state.home, pid))
                    if packagelib.package_id(files) != pid:
                        raise packagelib.PackageError(f"the theme kept as {pid[:12]} was changed on disk")
                    css = packagelib.theme_css(manifest, f"/lite/packages/{pid}")
        except (packagelib.PackageError, OSError) as exc:          # the arcade's own look, and why
            css = f"/* {'trytheme' if trying else 'theme'}: {str(exc).replace('*/', '* /')} */\n"
        return Response(css, media_type="text/css", headers=NO_STORE)

    @app.get("/lite/packages/{pid}/files/{name}")
    def lite_package_file(request: Request, pid: str, name: str):
        """A theme's own font: an installed theme's, or the operator's at the machine while it is being tried."""
        rel = f"files/{name}"
        if not packagelib.FILES.match(rel):
            raise HTTPException(404)
        if pid == "try":
            caller = caller_for(request)
            trying = state.setting(packagelib.TRY)
            if not (trying and caller.audience == "operator" and caller.local):
                raise HTTPException(404)
            folder, cache = Path(trying), NO_STORE
        else:
            if pid not in (state.setting(packagelib.SETTING) or {}):
                raise HTTPException(404)
            folder, cache = packagelib.home_of(state.home, pid), FOREVER    # an id is its bytes
        path = folder / rel
        if not path.is_file():
            raise HTTPException(404)
        return Response(path.read_bytes(), media_type="font/woff2", headers=cache)

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

    dd_keys: dict[str, bytes] = {}          # an @name's coin key, once read and checked against its address

    def dd_key_of_name(tag: str) -> bytes:
        """The coin key behind an @name on the name chain (arcade/ddaddress.py), or ValueError in plain words."""
        from .. import ddaddress as ddlib
        from ..script import b58check_decode
        name = tag.lstrip("@").strip().lower()
        if name in dd_keys:
            return dd_keys[name]
        claim = state.token_index(state.messaging).claim_of(name)
        if not claim:
            raise ValueError(f"nobody holds @{name}")
        address = claim["address"]
        key_hash = b58check_decode(address)[1]
        key = None
        # An account of this arcade told it its key when it signed up: the cheap answer, checked all the same.
        for k, v in state.settings().items():
            if k.startswith("address:") and v == address:
                said = state.setting("coinkey:" + k.split(":", 1)[1], "")
                try:
                    found = bytes.fromhex(str(said or ""))
                except ValueError:
                    found = b""
                if len(found) == 33 and ddlib.hash160(found) == key_hash:
                    key = found
        txids = [claim["claimed_txid"]]
        if key is None:
            with state.store() as store:
                row = store.key_for(address)
            if row is not None:
                txids.append(row["txid"])
        for txid in txids:
            if key is not None:
                break
            try:
                with state.messaging.rpc() as rpc:
                    key = ddlib.key_in(rpc.call("getrawtransaction", txid, 1), key_hash)
            except Exception:
                key = None
        if key is None:
            raise ValueError(f"@{name}'s key is not on the chain yet: ask them for their DigiDollar address (TD...)")
        dd_keys[name] = key
        return key

    @app.get("/lite/api/ddaddress")
    def lite_ddaddress(request: Request, to: str = "", chain: str = "dgbtest"):
        """{address, tag}: the DigiDollar address an @name is paid at on `chain` (dgbtest), a DD... / TD... address as
        typed, or the signed-in account's own (`to=me`). Wallet -> Receive and Send ask it (_wallet.html)."""
        from .. import ddaddress as ddlib
        to, chain = str(to or "").strip(), str(chain or "")
        if chain not in ddlib.VERSIONS:
            return JSONResponse({"detail": "DigiDollars are on dgb and dgbtest"}, status_code=400, headers=NO_STORE)
        try:
            if to == "me":
                account = signed_in(request)
                if account is None:
                    raise ValueError("sign in to have a DigiDollar address")
                said = state.setting(f"coinkey:{chain}:{account.pubkey}", "") or (
                    state.setting(f"coinkey:{account.pubkey}", "") if chain in ddlib.BY_NAME else "")
                if not said:
                    raise ValueError("open your wallet once in this arcade's window, so it knows your key")
                return JSONResponse({"address": ddlib.address(chain, bytes.fromhex(str(said))), "tag": ""},
                                    headers=NO_STORE)
            typed = ddlib.network_of(to)
            if typed is not None:
                if typed != chain:
                    raise ValueError(f"{to} is a DigiDollar address on the other chain")
                return JSONResponse({"address": to, "tag": ""}, headers=NO_STORE)
            if not to.startswith("@") and not to.replace("_", "").isalnum():
                raise ValueError(f"{to} is neither an @name nor a DigiDollar address (TD... or DD...)")
            if chain not in ddlib.BY_NAME:
                raise ValueError("on mainnet, pay a DigiDollar address (DD...): an @name's mainnet key is not on the "
                                 "name chain")
            return JSONResponse({"address": ddlib.address(chain, dd_key_of_name(to)), "tag": to.lstrip("@").lower()},
                                headers=NO_STORE)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400, headers=NO_STORE)

    @app.get("/lite/api/tabs")
    def lite_tabs(request: Request):
        """The module tabs this caller may have, each with whether they hid it (Settings -> Your tabs...)."""
        caller = caller_for(request)
        return JSONResponse({"tabs": module_tabs(caller, request), "kept": bool(tab_owner(caller, request))},
                            headers=NO_STORE)

    @app.post("/lite/api/tabs")
    def lite_tabs_set(request: Request, payload: dict = Body(...)):
        """{hidden: [module ids]}: the tabs this account (or the operator, for the node's own view) hides. A visitor's
        choice stays in their browser, so there is nothing to keep here for one."""
        from ..web import door as doorlib
        if request.headers.get(LITE_HEADER) != "1" or doorlib.cross_site(request.headers,
                                                                        request.headers.get("host", "")):
            raise HTTPException(403, "your tabs are chosen in the Lite window")
        caller = caller_for(request, write=True)
        owner = tab_owner(caller, request)
        if owner is None:
            raise HTTPException(403, "sign in to keep your tabs with your account")
        hidden = payload.get("hidden") if isinstance(payload, dict) else None
        if not isinstance(hidden, list) or len(hidden) > 200:
            raise HTTPException(400, "say the tabs to hide, as a list of module ids")
        state.set_setting(modulelib.TABS_SETTING, modulelib.set_hidden_tabs(state.settings(), owner, hidden))
        return JSONResponse({"tabs": module_tabs(caller, request), "kept": True}, headers=NO_STORE)

    @app.get("/lite/api/commands")
    def lite_commands(request: Request):
        return JSONResponse([{"name": c.name, "category": c.category, "audience": c.audience, "usage": c.usage,
                              "summary": c.summary, "package": c.module not in modulelib.MODULES,
                              **({"module": c.module, "order": c.order} if c.module not in modulelib.MODULES else {}),
                              **({"form": c.form, "args": [a.strip("[]") for a in c.usage.split()[1:]]}
                                 if c.form else {})}
                             for c in cmdlib.available(caller_for(request))],
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
        caller = caller_for(request, write=True, pubkeys=payload.get("pubkeys"),
                            password=str(payload.get("password") or ""))
        if console_limited(caller, request):
            return JSONResponse({"ok": False, "error": f"at most {CONSOLE_PER_MINUTE} lines a minute; wait a little"},
                                status_code=429, headers=NO_STORE)
        return answer_line(caller, str(payload.get("line") or ""))

    def run_for(request: Request, line: str, password: str = ""):
        """One console line for an API key's caller (/api/v1/run, web/apikeys_web.py): the key was checked on the
        way in, and its calls a minute are the key's own limit. `password` is the admin password, asked only when
        the line would sign the node wallet and the caller is an operator key."""
        return answer_line(caller_for(request, write=True, password=password), line)

    def answer_line(caller, line: str):
        try:
            return JSONResponse({"ok": True, "result": cmdlib.run_line(caller, line)}, headers=NO_STORE)
        except cmdlib.CommandError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, headers=NO_STORE)
        except Exception as exc:                           # noqa: BLE001 -- the console says what went wrong
            return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, headers=NO_STORE)

    @app.get("/m/{name}/files/{file}")
    def module_file(name: str, file: str):
        """A node module's own files for its pages (files/ in its package): served as they are, never run here."""
        host = getattr(state, "module_host", None)
        found = host.installed(state.settings()) if host is not None else {}
        rel = f"files/{file}"
        if name not in modulelib.enabled(state.settings()) or name not in found or not packagelib.MODULE_FILES.match(rel):
            raise HTTPException(404)
        path = packagelib.home_of(state.home, found[name][0]) / rel
        if not path.is_file():
            raise HTTPException(404)
        kind = {"html": "text/html", "css": "text/css", "js": "text/javascript", "json": "application/json",
                "png": "image/png", "svg": "image/svg+xml", "woff2": "font/woff2"}[rel.rsplit(".", 1)[1]]
        # Asked again each time (not kept for a year): an upgraded module keeps the same file names.
        return Response(path.read_bytes(), media_type=kind, headers={"Cache-Control": "no-cache", **MODULE_PAGE})

    # GET only (2026-11-05 review): a POST was refused on a public host and unguarded against other sites on a private
    # one, and no module posts; a module takes input by its console commands.
    @app.api_route("/m/{name}/{path:path}", methods=["GET"])
    async def module_page(request: Request, name: str, path: str = ""):
        """A node module's pages, answered by its own process. They are served sandboxed (MODULE_PAGE): a module's page
        runs no script with this arcade's origin, so it can never reach a wallet, a session or a key here."""
        from .. import modhost
        settings = state.settings()
        host = getattr(state, "module_host", None)
        if host is None or name not in modulelib.enabled(settings) or name not in host.installed(settings):
            raise HTTPException(404)
        manifest = host.installed(settings)[name][1]
        caller = caller_for(request)
        if not caller.may(manifest.get("pages", "anyone")):
            raise HTTPException(404)
        body = ""
        import anyio
        try:
            said = await anyio.to_thread.run_sync(lambda: host.ask(settings, name, {
                "call": "page", "method": request.method, "path": path, "query": dict(request.query_params),
                "body": body}, cmdlib.module_caller(caller, manifest.get("needs", {}).get("chains", {}))))
        except modhost.ModuleFailed as exc:
            return Response(f"{name}: {exc}", status_code=502, media_type="text/plain", headers=MODULE_PAGE)
        if isinstance(said, dict) and said.get("type") in ("text/html", "text/plain"):
            return Response(str(said.get("body") or ""), status_code=int(said.get("status") or 200),
                            media_type=said["type"], headers={**NO_STORE, **MODULE_PAGE})
        return JSONResponse(said, headers={**NO_STORE, **MODULE_PAGE})

    lite_page.run_line = run_for
    return lite_page

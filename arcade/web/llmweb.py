"""The LLM service's web side (arcade/llm.py): Admin -> LLM, the Exchange's LLM tab, and the OpenAI-compatible /v1 a
buyer's own programs use (2026-10-23: "A purchaser of model usage should be able to directly connect their local
harness to the Dogecoin arcade LLM api so they can use it on locally run applications as well").

Who buys is a signed-in account, as its messaging-chain address (the one its @tag points at). A seller is an arcade, by
its mesh id; this arcade's own offer is served here, any other's through the mesh (mesh/llmshare.py), carried with a
grant the account's own key signed for that arcade (llm.grant_words). An API key (`sk-arcade-...`) stands for one
account, one seller and one payment kind; only its hash is kept.
"""
from __future__ import annotations

import contextlib
import json
import logging
import time
from typing import Any, Callable, Iterator

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import llm as llmlib

log = logging.getLogger("arcade.web")

HERE = "here"


def register(app, state, *, signed_in: Callable, account_address: Callable, account_chain: Callable,
             admin_check: Callable, name_of: Callable, pay_choices: Callable, render: Callable | None = None) -> None:
    books = llmlib.Books(state.home)

    def mesh():
        return getattr(state, "mesh", None)

    def share():
        return getattr(mesh(), "llmshare", None)

    def me_id() -> str:
        return str(getattr(mesh(), "node_id", "") or "")

    # ---------------------------------------------------------------- this arcade as a seller
    def _chain(network: str):
        for chain in getattr(state, "all_chains", ()) or ():
            if chain.network == network:
                return chain
        try:
            return state.chain_named(network)
        except ValueError:
            raise llmlib.LlmError(f"this arcade has no {network} chain") from None

    def pay_to(leg: str) -> str:
        network, kind, _ = llmlib.leg_parts(leg)
        return state.payout_address(_chain(network))

    def scale(leg: str) -> int:
        if leg == "free":
            return llmlib.COIN
        network, kind, pid = llmlib.leg_parts(leg)
        if kind == "coin":
            return llmlib.COIN
        try:
            prop = state.token_index(_chain(network)).property(pid) or {}
        except Exception:                                           # noqa: BLE001
            prop = {}
        return llmlib.COIN if prop.get("divisible", True) else 1

    def verify(leg: str, txid: str, to: str, signer: bytes) -> int:
        network, kind, pid = llmlib.leg_parts(leg)
        chain = _chain(network)
        if kind == "coin":
            with chain.rpc() as rpc:
                return llmlib.check_coin_payment(rpc, txid, to, signer, bool(chain.params.real_coins))
        return llmlib.check_token_payment(state.token_index(chain), txid, to, pid, signer)

    def operator_address() -> str:
        return account_address(state.operator, account_chain()) if state.operator else ""

    def free(buyer: str) -> bool:
        own = operator_address()
        return bool(buyer) and (buyer == own or buyer == getattr(state, "derived_address", None))

    def about() -> dict:
        tag = state.operator_tag() if hasattr(state, "operator_tag") else ""
        hosts = list(getattr(state, "public_hosts", []) or [])
        return {"tag": tag or "", "host": hosts[0] if hosts else ""}

    seller = llmlib.Seller(lambda: llmlib.config(state), books, pay_to, scale, verify, free, about)
    seller.node_id = me_id
    from .. import imagegen
    pictures = imagegen.ImageSeller(lambda: imagegen.config(state), books, pay_to, scale, free)
    seller.images = pictures
    state.llm_seller = seller                       # mesh/llmshare.py serves through it

    # ---------------------------------------------------------------- reaching a seller
    def is_here(sid: str) -> bool:
        return sid in ("", HERE) or sid == me_id()

    def grant_for(account, sid: str) -> dict:
        g = books.grant(account.pubkey, sid)
        if g is None:
            raise llmlib.LlmError("sign once to use this seller (your wallet signs who you are)")
        return {"address": g["address"], "expires": g["expires"], "sig": g["sig"]}

    def ask(account, address: str, sid: str, what: str, leg: str, body: dict, via: str = "web") -> Iterator[dict]:
        if is_here(sid):
            if what == "chat":
                yield from seller.chat(address, leg, body, via=via)
            elif what == "image":
                yield pictures.make(address, leg, body, via=via)
            elif what == "topup":
                yield seller.topup(address, body.get("leg", ""), body.get("txid", ""), body.get("sig", ""))
            else:
                yield seller.balance(address)
            return
        s = share()
        if s is None:
            raise llmlib.LlmError("the mesh is not running, so other arcades cannot be reached")
        yield from s.ask(sid, what, {"grant": grant_for(account, sid), "leg": leg, "body": body})

    def offers() -> list[dict]:
        out = []
        mine = seller.offer()
        if mine:
            out.append({**mine, "seller": HERE, "here": True})
        s = share()
        for peer, o in (dict(s.offers) if s is not None else {}).items():
            if o:
                out.append({**o, "seller": peer, "here": False})
        return out

    def who(request: Request):
        account = signed_in(request)
        if account is None:
            return None, ""
        return account, account_address(account.pubkey, account_chain())

    def need_account(request: Request):
        account, address = who(request)
        if account is None:
            raise llmlib.LlmError("sign in first")
        if not address:
            raise llmlib.LlmError("this account has no address on the tag chain yet; open the Wallet once")
        return account, address

    def refuse(exc: Exception, status: int = 400) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=status)

    async def body_of(request: Request) -> dict:
        if "application/json" not in request.headers.get("content-type", ""):
            raise llmlib.LlmError("send JSON")
        got = await request.json()
        return got if isinstance(got, dict) else {}

    # ---------------------------------------------------------------- Admin -> LLM
    @app.get("/admin/api/llm")
    def admin_llm(request: Request):
        admin_check(request)
        cfg = llmlib.config(state)
        month = time.time() - 30 * 86400
        earned = []
        for row in books.earned(month):
            if row["leg"] == "free":
                continue
            sc = scale(row["leg"])
            earned.append({**row, "text": llmlib.fmt(int(row["units"] or 0), sc)})
        kinds = [c for c in pay_choices(request) if c.get("kind") in ("coin", "token")]
        return JSONResponse({"config": {**{k: v for k, v in cfg.items() if k != "key"}, "has_key": bool(cfg.get("key"))},
                             "earned": earned, "kinds": kinds, "selling": bool(seller.offer()), "load": load_now(),
                             "images": images_admin(request)},
                            headers={"Cache-Control": "no-store"})

    def images_admin(request: Request) -> dict:
        who = admin_check(request)
        icfg = imagegen.config(state)
        return {"config": {**icfg, "wake": icfg.get("wake") if who.get("local") else ("set" if icfg.get("wake") else "")},
                "local": bool(who.get("local")), "load": pictures.load(), "sharing": bool(pictures.part())}

    @app.post("/admin/api/llm/images")
    async def admin_images_save(request: Request):
        """Admin -> LLM -> Share your image model (2026-10-23: per image; make and edit)."""
        who = admin_check(request, write=True)
        try:
            said = await body_of(request)
            state.set_setting(imagegen.SETTING, imagegen.clean(said, imagegen.config(state), bool(who.get("local"))))
        except (llmlib.LlmError, ValueError, TypeError) as exc:
            return refuse(exc)
        s = share()
        if s is not None:
            s.say_again()
        return JSONResponse({"ok": True, "sharing": bool(pictures.part())})

    @app.post("/admin/api/llm/images/scan")
    async def admin_images_scan(request: Request):
        """The picture models ComfyUI holds, found by themselves (woken first when it sleeps and a wake is set)."""
        admin_check(request, write=True)
        import asyncio
        try:
            said = await body_of(request)
            url = str(said.get("url") or "").strip().rstrip("/") or imagegen.config(state).get("url") or ""
            if not url:
                raise llmlib.LlmError("name ComfyUI's address first")
            srv = imagegen.ComfyServer(url, imagegen.config(state).get("wake") or "")
            t = time.monotonic()
            up = await asyncio.to_thread(srv.up)
            if not up and said.get("wake_now") and srv.wake:
                await asyncio.to_thread(srv.ensure_up)
                up = True
            if not up:
                return JSONResponse({"ok": False, "asleep": True,
                                     "detail": "ComfyUI is not running" + ("; it is woken when a picture is asked for"
                                                                           if srv.wake else "")})
            models = await asyncio.to_thread(srv.models)
            return JSONResponse({"ok": True, "models": models, "ms": round((time.monotonic() - t) * 1000)})
        except llmlib.LlmError as exc:
            return refuse(exc)
        except Exception as exc:                                    # noqa: BLE001
            return refuse(f"ComfyUI did not answer: {exc}"[:300])

    def load_now() -> dict:
        got = seller.load(fresh=True)
        return {**got, "word": seller.word(got)}

    @app.get("/admin/api/llm/load")
    def admin_llm_load(request: Request):
        admin_check(request)
        return JSONResponse(load_now(), headers={"Cache-Control": "no-store"})

    @app.get("/admin/api/llm/usage")
    def admin_llm_usage(request: Request, days: float = 7):
        """Everything about the shared model's use (2026-10-23: "a link on their model that they're sharing and
        get all of the usage analytics"): a series, totals by model, buyer, payment kind and way in, the last replies."""
        admin_check(request)
        days = max(1 / 24, min(float(days), 365))
        bucket = 300 if days <= 1 else 3600 if days <= 14 else 86400
        got = books.analytics(time.time() - days * 86400, bucket)
        s = share()
        peers = {}
        for peer, o in (dict(s.offers) if s is not None else {}).items():
            peers[peer] = ("@" + o["tag"]) if o.get("tag") else o.get("host") or peer[:12]
        try:
            ops = dict(getattr(getattr(mesh(), "operators", None), "peers", {}) or {})
        except Exception:                                           # noqa: BLE001
            ops = {}

        def where(v: str) -> str:
            if v in ("web", "here"):
                return "This arcade (Exchange tab and apps)"
            if v == "api":
                return "API keys"
            if v in peers:
                return "Arcade " + peers[v]
            o = ops.get(v) if isinstance(ops, dict) else None
            tag = (o or {}).get("tag") or ""
            hosts = (o or {}).get("hosts") or []
            return "Arcade " + ("@" + tag if tag else hosts[0] if hosts else v[:12])

        tags: dict[str, str] = {}

        def who(address: str) -> str:
            if address not in tags:
                try:
                    tags[address] = name_of(address) or ""
                except Exception:                                   # noqa: BLE001
                    tags[address] = ""
            return tags[address]

        def coin(leg: str) -> dict:
            if leg == "free":
                return {"label": "Free", "scale": llmlib.COIN}
            p = next((x for x in llmlib.config(state).get("prices") or [] if x.get("leg") == leg), None)
            try:
                sc = scale(leg)
            except Exception:                                       # noqa: BLE001
                sc = llmlib.COIN
            return {"label": (p or {}).get("label") or leg, "scale": sc}
        for row in got["by"]["buyer"]:
            row["tag"] = who(row["key"])
        for row in got["by"]["via"]:
            row["label"] = where(row["key"])
        for row in got["by"]["leg"]:
            c = coin(row["key"])
            row["label"], row["text"] = c["label"], llmlib.fmt(int(row["units"] or 0), c["scale"])
        for row in got["last"]:
            c = coin(row["leg"])
            row["tag"], row["coin"] = who(row["buyer"]), c["label"].split(" \u00b7 ")[0]
            row["cost"] = llmlib.fmt(int(row["units"] or 0), c["scale"])
            row["where"] = where(row["via"])
        return JSONResponse({**got, "days": days, "bucket": bucket, "load": load_now(),
                             "models": llmlib.config(state).get("models") or []},
                            headers={"Cache-Control": "no-store"})

    if render is not None:
        @app.get("/admin/llm/usage")
        def admin_llm_usage_page(request: Request):
            from fastapi.responses import RedirectResponse
            try:
                admin_check(request)
            except Exception as exc:                                # noqa: BLE001
                if getattr(exc, "status_code", 0) == 401:
                    return RedirectResponse("/admin/login", status_code=303)
                raise
            return render(request, "llm_usage.html")

    @app.post("/admin/api/llm")
    async def admin_llm_save(request: Request):
        admin_check(request, write=True)
        try:
            said = await body_of(request)
            kept = llmlib.config(state)
            state.set_setting(llmlib.SETTING, llmlib.clean(said, kept))
        except (llmlib.LlmError, ValueError, TypeError) as exc:
            return refuse(exc)
        s = share()
        if s is not None:
            s.say_again()
        return JSONResponse({"ok": True, "selling": bool(seller.offer())})

    @app.post("/admin/api/llm/test")
    async def admin_llm_test(request: Request):
        admin_check(request, write=True)
        try:
            said = await body_of(request)
            cfg = llmlib.clean({"url": said.get("url", "")}, llmlib.config(state))
            url = cfg["url"]
            if not url:
                raise llmlib.LlmError("name the server first")
            key = str(said.get("key") or "") or llmlib.config(state).get("key") or ""
            t = time.monotonic()
            import asyncio
            models = await asyncio.to_thread(llmlib.Server(url, key).models)
            return JSONResponse({"ok": True, "models": models, "ms": round((time.monotonic() - t) * 1000)})
        except llmlib.LlmError as exc:
            return refuse(exc)
        except Exception as exc:                                    # noqa: BLE001
            return refuse(f"the server did not answer: {exc}"[:300])

    # ---------------------------------------------------------------- the Exchange's LLM tab
    @app.get("/account/llm/offers")
    def llm_offers(request: Request):
        account, address = who(request)
        out = []
        for o in offers():
            held = bool(account) and (o["here"] or books.grant(account.pubkey, o["seller"]) is not None)
            out.append({**o, "granted": held})
        base = str(request.base_url).rstrip("/")
        return JSONResponse({"offers": out, "me": address, "signed_in": account is not None,
                             "api_base": base + "/v1"}, headers={"Cache-Control": "no-store"})

    @app.get("/account/llm/balance")
    def llm_balance(request: Request, seller: str = HERE):
        try:
            account, address = need_account(request)
            got = next(ask(account, address, seller, "balance", "", {}), {})
        except llmlib.LlmError as exc:
            return refuse(exc)
        return JSONResponse(got, headers={"Cache-Control": "no-store"})

    @app.get("/account/llm/grant")
    def llm_grant_words(request: Request, seller: str = HERE):
        """What the account's key signs to use `seller` (nothing for this arcade, or a grant still good)."""
        try:
            account, address = need_account(request)
        except llmlib.LlmError as exc:
            return refuse(exc)
        if is_here(seller) or books.grant(account.pubkey, seller) is not None:
            return JSONResponse({"ok": True})
        expires = int(time.time()) + 30 * 86400
        chain = account_chain()
        return JSONResponse({"sign": {"message": llmlib.grant_words(seller, address, expires), "expires": expires,
                                      "magic": llmlib.MAGIC,
                                      "chain": {"network": chain.network,
                                                "version": chain.params.pubkeyhash_version}}})

    @app.post("/account/llm/grant")
    async def llm_grant_keep(request: Request):
        try:
            account, address = need_account(request)
            said = await body_of(request)
            sid, expires, sig = str(said.get("seller") or ""), int(said.get("expires") or 0), str(said.get("sig") or "")
            if is_here(sid):
                return JSONResponse({"ok": True})
            from ..mesh.llmshare import grant_ok
            grant_ok({"address": address, "expires": expires, "sig": sig}, sid)
            books.keep_grant(account.pubkey, sid, address, expires, sig)
        except (llmlib.LlmError, ValueError, TypeError) as exc:
            return refuse(exc)
        return JSONResponse({"ok": True})

    @app.get("/account/llm/topup")
    def llm_topup_words(request: Request, seller: str = HERE, leg: str = "", txid: str = ""):
        """The words the paying key signs, for a payment the browser just sent."""
        try:
            account, address = need_account(request)
            network = llmlib.leg_parts(leg)[0]
        except llmlib.LlmError as exc:
            return refuse(exc)
        sid = me_id() if is_here(seller) else seller
        return JSONResponse({"message": llmlib.topup_words(network, txid.strip().lower(), sid, address),
                             "magic": llmlib.MAGIC})

    @app.post("/account/llm/topup")
    async def llm_topup(request: Request):
        try:
            account, address = need_account(request)
            said = await body_of(request)
            sid = str(said.get("seller") or HERE)
            body = {"leg": str(said.get("leg") or ""), "txid": str(said.get("txid") or ""),
                    "sig": str(said.get("sig") or "")}
            import asyncio
            got = await asyncio.to_thread(lambda: next(ask(account, address, sid, "topup", body["leg"], body), {}))
        except llmlib.LlmError as exc:
            return refuse(exc)
        return JSONResponse(got)

    def stream(gen: Iterator[dict], openai: bool) -> StreamingResponse:
        """Server-sent events, as OpenAI streams them; an error mid-way is said as one more event."""
        def events():
            try:
                for ev in gen:
                    if openai and "arcade" in ev:
                        continue
                    yield f"data: {json.dumps(ev, separators=(',', ':'))}\n\n"
            except llmlib.LlmError as exc:
                yield f"data: {json.dumps({'error': {'message': str(exc), 'type': 'arcade'}})}\n\n"
            except Exception as exc:                                # noqa: BLE001
                log.debug("llm stream", exc_info=True)
                yield f"data: {json.dumps({'error': {'message': f'failed: {exc}'[:300], 'type': 'arcade'}})}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    def first(gen: Iterator[dict]) -> Iterator[dict]:
        """Start the reply before the response does, so a refusal (no credit, busy) is an HTTP error, not an event."""
        it = iter(gen)
        try:
            head = next(it)
        except StopIteration:
            return iter(())

        def rest():
            yield head
            yield from it
        return rest()

    @app.post("/account/llm/chat")
    async def llm_chat(request: Request):
        try:
            account, address = need_account(request)
            said = await body_of(request)
            sid, leg = str(said.pop("seller", HERE) or HERE), str(said.pop("leg", "") or "")
            import asyncio
            gen = await asyncio.to_thread(first, ask(account, address, sid, "chat", leg, said))
        except llmlib.LlmError as exc:
            return refuse(exc)
        return stream(gen, openai=False)

    @app.post("/account/llm/image")
    async def llm_image(request: Request):
        """One picture, made or edited by a seller's image model and paid per image (imagegen.py)."""
        try:
            account, address = need_account(request)
            said = await body_of(request)
            sid, leg = str(said.pop("seller", HERE) or HERE), str(said.pop("leg", "") or "")
            import asyncio
            got = await asyncio.to_thread(lambda: next(ask(account, address, sid, "image", leg, said), {}))
        except llmlib.LlmError as exc:
            return refuse(exc)
        return JSONResponse(got)

    @app.get("/account/llm/keys")
    def llm_keys(request: Request):
        try:
            account, _ = need_account(request)
        except llmlib.LlmError as exc:
            return refuse(exc)
        return JSONResponse({"keys": books.keys_of(account.pubkey)}, headers={"Cache-Control": "no-store"})

    @app.post("/account/llm/keys/new")
    async def llm_key_new(request: Request):
        try:
            account, address = need_account(request)
            said = await body_of(request)
            sid, leg = str(said.get("seller") or HERE), str(said.get("leg") or "")
            if leg != "free":
                llmlib.leg_parts(leg)
            if not is_here(sid):
                grant_for(account, sid)
            key = books.new_key(account.pubkey, address, HERE if is_here(sid) else sid, leg,
                                str(said.get("name") or ""))
        except llmlib.LlmError as exc:
            return refuse(exc)
        return JSONResponse({"ok": True, "key": key})

    @app.post("/account/llm/keys/drop")
    async def llm_key_drop(request: Request):
        try:
            account, _ = need_account(request)
            said = await body_of(request)
            gone = books.drop_key(account.pubkey, str(said.get("hash") or ""))
        except llmlib.LlmError as exc:
            return refuse(exc)
        return JSONResponse({"ok": gone})

    # ---------------------------------------------------------------- OpenAI-compatible, for a buyer's own programs
    def by_key(request: Request):
        auth = request.headers.get("authorization", "")
        key = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-api-key", "").strip()
        row = books.key(key) if key.startswith("sk-arcade-") else None
        if row is None:
            raise llmlib.LlmError("no such API key (make one on the Exchange's LLM tab)")
        return row

    def oai_error(msg: str, status: int) -> JSONResponse:
        return JSONResponse({"error": {"message": msg, "type": "invalid_request_error" if status < 500 else "server",
                                       "code": status}}, status_code=status)

    @app.get("/v1/models")
    def v1_models(request: Request):
        try:
            row = by_key(request)
        except llmlib.LlmError as exc:
            return oai_error(str(exc), 401)
        o = next((x for x in offers() if (x["seller"] == row["seller"]) or (x["here"] and is_here(row["seller"]))),
                 None)
        models = [m["id"] for m in (o or {}).get("models", [])]
        return JSONResponse({"object": "list", "data": [{"id": m, "object": "model", "owned_by": "arcade"}
                                                        for m in models]})

    async def v1_picture(request: Request, body: dict):
        try:
            row = by_key(request)
        except llmlib.LlmError as exc:
            return oai_error(str(exc), 401)

        class _Acct:
            pubkey = row["account"]
        import asyncio
        n = int(body.pop("n", 1) or 1)
        if n != 1:
            return oai_error("one picture per request (n=1)", 400)
        body.pop("response_format", None)
        try:
            got = await asyncio.to_thread(lambda: next(ask(_Acct, row["address"], row["seller"], "image", row["leg"],
                                                           body, via="api"), {}))
        except llmlib.LlmError as exc:
            msg = str(exc)
            return oai_error(msg, 402 if "credit" in msg else 429 if "busy" in msg else 400)
        return JSONResponse({"created": int(time.time()), "data": [{"b64_json": got.get("png", "")}],
                             "model": got.get("model", ""), "seed": got.get("seed")})

    @app.post("/v1/images/generations")
    async def v1_images(request: Request):
        """OpenAI's images API: {prompt, size, model}; the picture comes back as b64_json."""
        try:
            body = await request.json()
        except ValueError:
            return oai_error("send JSON", 400)
        if not isinstance(body, dict):
            return oai_error("send a JSON object", 400)
        body.pop("image", None)
        return await v1_picture(request, body)

    @app.post("/v1/images/edits")
    async def v1_image_edits(request: Request):
        """OpenAI's image edits: a multipart form with `image` (a file) and `prompt`."""
        import base64 as _b64
        try:
            form = await request.form()
        except Exception:                                           # noqa: BLE001
            return oai_error("send a multipart form with image and prompt", 400)
        f = form.get("image") or form.get("image[]")
        if f is None or not hasattr(f, "read"):
            return oai_error("bring the picture as the form field image", 400)
        try:
            raw = await f.read()
            body = {"prompt": str(form.get("prompt") or ""), "model": str(form.get("model") or ""),
                    "image": _b64.b64encode(raw).decode()}
        finally:
            await form.close()                                      # the upload's temporary file, at once
        return await v1_picture(request, body)

    @app.post("/v1/chat/completions")
    async def v1_chat(request: Request):
        try:
            row = by_key(request)
        except llmlib.LlmError as exc:
            return oai_error(str(exc), 401)
        try:
            said = await request.json()
        except ValueError:
            return oai_error("send JSON", 400)
        if not isinstance(said, dict):
            return oai_error("send a JSON object", 400)
        want_stream = bool(said.pop("stream", False))
        said.pop("stream_options", None)

        class _Acct:
            pubkey = row["account"]
        import asyncio
        try:
            gen = await asyncio.to_thread(first, ask(_Acct, row["address"], row["seller"], "chat", row["leg"], said,
                                                     via="api"))
        except llmlib.LlmError as exc:
            msg = str(exc)
            return oai_error(msg, 402 if "credit" in msg else 429 if "busy" in msg else 400)
        if want_stream:
            return stream(gen, openai=True)
        try:
            return JSONResponse(await asyncio.to_thread(whole, gen))
        except llmlib.LlmError as exc:
            return oai_error(str(exc), 502)


def whole(gen: Iterator[dict]) -> dict:
    """A streamed reply put back together as one chat.completion (for callers that did not ask to stream)."""
    content, reasoning, calls, finish, usage, head = [], [], {}, None, None, {}
    for ev in gen:
        if "arcade" in ev:
            continue
        head = head or {k: ev.get(k) for k in ("id", "created", "model")}
        if isinstance(ev.get("usage"), dict):
            usage = ev["usage"]
        for ch in ev.get("choices") or []:
            d = ch.get("delta") or {}
            if d.get("content"):
                content.append(d["content"])
            if d.get("reasoning_content"):
                reasoning.append(d["reasoning_content"])
            for tc in d.get("tool_calls") or []:
                slot = calls.setdefault(int(tc.get("index") or 0),
                                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                slot["function"]["name"] += fn.get("name") or ""
                slot["function"]["arguments"] += fn.get("arguments") or ""
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
    message: dict[str, Any] = {"role": "assistant", "content": "".join(content) if content or not calls else None}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if calls:
        message["tool_calls"] = [calls[i] for i in sorted(calls)]
    return {"id": head.get("id") or "chatcmpl-arcade", "object": "chat.completion",
            "created": head.get("created") or int(time.time()), "model": head.get("model") or "",
            "choices": [{"index": 0, "message": message, "finish_reason": finish or "stop"}],
            **({"usage": usage} if usage else {})}

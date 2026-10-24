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
import hashlib
import json
import re
import secrets
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
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
        """This arcade's own operator, and the accounts it chose to share with free (words and pictures alike)."""
        own = operator_address()
        if not buyer:
            return False
        if buyer == own or buyer == getattr(state, "derived_address", None):
            return True
        return any(r.get("address") == buyer for r in llmlib.config(state).get("free_for") or [])

    def tag_address(tag: str) -> str:
        """Who holds @tag on the tag chain, or LlmError."""
        name = str(tag or "").strip().lstrip("@").lower()
        if not re.fullmatch(r"[a-z0-9_]{2,24}", name):
            raise llmlib.LlmError(f"@{name} is not a name")
        try:
            address = state.token_index(account_chain()).address_of(name)
        except Exception:                                           # noqa: BLE001
            address = None
        if not address:
            raise llmlib.LlmError(f"nobody holds @{name}")
        return address

    def about() -> dict:
        tag = state.operator_tag() if hasattr(state, "operator_tag") else ""
        hosts = list(getattr(state, "public_hosts", []) or [])
        return {"tag": tag or "", "host": hosts[0] if hosts else ""}

    seller = llmlib.Seller(lambda: llmlib.config(state), books, pay_to, scale, verify, free, about)
    seller.node_id = me_id

    def arcade_name() -> str:
        try:
            from . import brand as brandlib
            return brandlib.current().name
        except Exception:                                           # noqa: BLE001
            return "DogecoinArcade"
    seller.arcade_name = arcade_name
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
        try:
            peers = dict(getattr(getattr(mesh(), "operators", None), "peers", {}) or {})
        except Exception:                                           # noqa: BLE001
            peers = {}
        operators = sorted({str(p.get("tag")) for p in peers.values() if isinstance(p, dict) and p.get("tag")})
        return JSONResponse({"config": {**{k: v for k, v in cfg.items() if k != "key"}, "has_key": bool(cfg.get("key"))},
                             "earned": earned, "kinds": kinds, "selling": bool(seller.offer()), "load": load_now(),
                             "operators": operators,
                             "helper": [{"seller": x["o"]["seller"], "here": x["o"]["here"], "tag": x["o"].get("tag", ""),
                                         "host": x["o"].get("host", ""), "model": x["m"], "ready": x["ready"],
                                         "approved": x["approved"], "key": f"{x['o']['seller']}|{x['m']}",
                                         "load": (x["o"].get("load") or {}).get("word", ""),
                                         "needs_grant": x.get("needs_grant", False) and helper_identity()[0] != "node"}
                                        for x in helper_sources()],
                             "helper_now": (lambda h: {"seller": h["seller"], "model": h["models"][0]["id"]} if h else None)(
                                 helper_pick()),
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
        who = admin_check(request, write=True)
        import asyncio
        try:
            said = await body_of(request)
            url = str(said.get("url") or "").strip().rstrip("/") or imagegen.config(state).get("url") or ""
            if not url:
                raise llmlib.LlmError("name ComfyUI's address first")
            # The wake command typed in the box, not yet saved, is tried too -- at the node machine only, as saving it
            # is (2026-10-23: "when i scan it says comfy is not running").
            wake = imagegen.config(state).get("wake") or ""
            if who.get("local") and str(said.get("wake") or "").strip():
                wake = str(said["wake"]).strip()[:300]
            srv = imagegen.ComfyServer(url, wake)
            t = time.monotonic()
            up = await asyncio.to_thread(srv.up)
            if not up and said.get("wake_now") and srv.wake:
                await asyncio.to_thread(srv.ensure_up)
                up = True
            if not up:
                return JSONResponse({"ok": False, "asleep": True,
                                     "detail": "ComfyUI is not running" + ("; press Scan again to wake it" if srv.wake
                                                                           else "; start it, or give a wake command")})
            try:
                models = await asyncio.to_thread(srv.models)
            except Exception:                                       # noqa: BLE001 -- stopped while asked: once more
                if not srv.wake:
                    raise
                await asyncio.to_thread(srv.ensure_up)
                models = await asyncio.to_thread(srv.models)
            return JSONResponse({"ok": True, "models": models, "ms": round((time.monotonic() - t) * 1000)})
        except llmlib.LlmError as exc:
            return refuse(exc)
        except Exception as exc:                                    # noqa: BLE001
            return refuse(f"ComfyUI did not answer: {exc}"[:300])

    @app.post("/admin/api/llm/discover")
    async def admin_llm_discover(request: Request):
        """Every model server on this machine, found by itself (2026-10-23: "Can the operator just have a button
        that they can click to scan for all models and then it will just give them a list of models that is on their
        computer and then they can just pick from the models?"). Each listening local port is asked, briefly, whether
        it is an OpenAI-compatible server (/v1/models) or ComfyUI (/system_stats); a ComfyUI that sleeps until asked
        (a user unit named for it) is offered with its wake command."""
        who = admin_check(request, write=True)
        import asyncio
        said = await body_of(request) if "json" in request.headers.get("content-type", "") else {}
        own = {int(request.url.port or 0)} if request.url.port else set()
        if said.get("wake") and who.get("local"):
            srv = imagegen.ComfyServer(str(said.get("url") or "http://127.0.0.1:8188"), str(said["wake"])[:300])
            try:
                await asyncio.to_thread(srv.ensure_up)
            except llmlib.LlmError as exc:
                return refuse(exc)
        found = await asyncio.to_thread(discover, own)
        if said.get("wake") and who.get("local") and not found["images"]:
            # woken, then stopped again before it was asked (the voice model took the slot): wake it once more
            try:
                await asyncio.to_thread(imagegen.ComfyServer(str(said.get("url") or "http://127.0.0.1:8188"),
                                                            str(said["wake"])[:300]).ensure_up)
                found = await asyncio.to_thread(discover, own)
            except llmlib.LlmError:
                pass
        found["local"] = bool(who.get("local"))
        return JSONResponse(found, headers={"Cache-Control": "no-store"})

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
        @app.get("/exchange/llm/model")
        def llm_model_page(request: Request, seller: str = HERE, kind: str = "text", model: str = ""):
            """One model's own page (2026-10-23: "whenever you click on one of the models, you should go to that
            specific models page to interact with it"): its card, prices, top-up, chat or pictures, and API keys."""
            focus = {"seller": seller[:100], "kind": "image" if kind == "image" else "text", "model": model[:200]}
            return render(request, "llm_model.html", focus=focus)

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
            if "free_for" in said:                                  # names, resolved here (never an address from the form)
                said["free_for"] = [{"tag": str(t).strip().lstrip("@").lower(), "address": tag_address(str(t))}
                                    for t in said.get("free_for") or [] if str(t).strip()]
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

    # Every picture an account gets is kept here 24 hours (2026-10-23: "if a person starts an image generation
    # and they accidentally shut down arcade. That image should be stored in a temporary cache for 24 hours where that
    # user can access it and download it"). The job runs in its own thread and keeps the picture whether or not the
    # page that asked is still there to receive it.
    PICS = Path(state.home) / "llm-pictures"
    PICS_KEEP = 24 * 3600
    jobs = ThreadPoolExecutor(max_workers=4, thread_name_prefix="llm-picture")

    def pics_dir(account) -> Path:
        d = PICS / hashlib.sha256(account.pubkey.encode()).hexdigest()[:32]
        d.mkdir(parents=True, exist_ok=True)
        return d

    def pics_tidy(d: Path) -> None:
        cut = time.time() - PICS_KEEP
        for f in d.glob("*"):
            try:
                if f.stat().st_mtime < cut:
                    f.unlink()
            except OSError:
                pass

    def keep_picture(account, got: dict, said: dict, sid: str) -> str:
        import base64 as _b64
        d = pics_dir(account)
        pics_tidy(d)
        pid = f"{int(time.time())}-{secrets.token_hex(4)}"
        (d / f"{pid}.png").write_bytes(_b64.b64decode(got.get("png") or ""))
        a = got.get("arcade") or {}
        (d / f"{pid}.json").write_text(json.dumps({
            "id": pid, "at": time.time(), "prompt": str(said.get("prompt") or "")[:500], "model": got.get("model", ""),
            "seller": sid, "edit": bool(got.get("edit")), "seed": got.get("seed"), "credits": got.get("credits"),
            "cost": a.get("cost_text", ""), "leg": a.get("leg", ""), "free": bool(a.get("free"))}))
        return pid

    @app.post("/account/llm/image")
    async def llm_image(request: Request):
        """One picture, made or edited by a seller's image model, charged in credits by its size (imagegen.py)."""
        import asyncio
        try:
            account, address = need_account(request)
            said = await body_of(request)
            sid, leg = str(said.pop("seller", HERE) or HERE), str(said.pop("leg", "") or "")

            def job():
                got = next(ask(account, address, sid, "image", leg, said), {})
                if got.get("png"):
                    got["kept"] = keep_picture(account, got, said, sid)
                return got
            got = await asyncio.wrap_future(jobs.submit(job))
        except llmlib.LlmError as exc:
            return refuse(exc)
        return JSONResponse(got)

    @app.get("/account/llm/pictures")
    def llm_pictures(request: Request):
        """The account's pictures of the last 24 hours, newest first."""
        try:
            account, _ = need_account(request)
        except llmlib.LlmError as exc:
            return refuse(exc)
        d = pics_dir(account)
        pics_tidy(d)
        out = []
        for f in sorted(d.glob("*.json"), reverse=True)[:60]:
            try:
                meta = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if (d / f"{meta['id']}.png").exists():
                out.append({**meta, "url": f"/account/llm/pictures/{meta['id']}.png",
                            "until": meta["at"] + PICS_KEEP})
        return JSONResponse({"pictures": out}, headers={"Cache-Control": "no-store"})

    @app.post("/account/llm/pictures/delete")
    async def llm_picture_delete(request: Request):
        """Take a picture out of the 24-hour cache (2026-10-23: "User should be able to delete generated images
        from the cache")."""
        try:
            account, _ = need_account(request)
            pid = str((await body_of(request)).get("id") or "")
            if not re.fullmatch(r"\d{6,12}-[0-9a-f]{8}", pid):
                raise llmlib.LlmError("no such picture")
            d = pics_dir(account)
            gone = False
            for f in (d / f"{pid}.png", d / f"{pid}.json"):
                if f.exists():
                    f.unlink()
                    gone = True
        except llmlib.LlmError as exc:
            return refuse(exc)
        return JSONResponse({"ok": gone})

    @app.get("/account/llm/pictures/{pid}.png")
    def llm_picture_file(request: Request, pid: str):
        from fastapi.responses import FileResponse
        try:
            account, _ = need_account(request)
        except llmlib.LlmError as exc:
            return refuse(exc, 403)
        if not re.fullmatch(r"\d{6,12}-[0-9a-f]{8}", pid):
            return refuse("no such picture", 404)
        f = pics_dir(account) / f"{pid}.png"
        if not f.exists():
            return refuse("that picture is gone (pictures are kept 24 hours)", 404)
        return FileResponse(f, media_type="image/png", filename=f"arcade-{pid}.png",
                            headers={"Cache-Control": "private, max-age=3600"})

    # ---------------------------------------------------------------- the arcade helper (the robot in every page's corner)
    # 2026-10-23: "a pixel art robot head ... in the lower right hand corner that persists across all tabs and
    # pages. When you click on it. It should open a chat dialog box with that same Dogecoin arcade chat harness as From
    # the LLM marketplace but using any LLM that is shared with the operator of that arcade." It speaks AS the operator:
    # this arcade's own model (the operator uses it free) or another arcade's that shares with this operator free
    # (its offer names the operator in `free_tags`; the operator's own key signed a grant for it in Admin -> LLM).
    # Never a paid source: the leg is "free", which a seller that charges refuses. Each person may ask HELPER_HOUR times
    # an hour (a guest fewer), so nobody spends the shared model on everyone's behalf.
    HELPER_HOUR, HELPER_GUEST_HOUR, HELPER_OUT = 40, 15, 1200
    asked_at: dict[str, list[float]] = {}

    def helper_identity() -> tuple[str, str]:
        """Who the helper speaks as: the operator's account (its browser signs grants in Admin -> LLM), or -- on an
        arcade whose operator IS its node, as on .25 -- the node's identity, whose key this arcade holds and signs with."""
        if state.operator:
            return state.operator, operator_address()
        return "node", str(getattr(state, "derived_address", "") or "")

    def node_grant(sid: str, address: str) -> None:
        """A grant for `sid`, signed by this node's own identity key (keywallet.node_key), kept as the helper's."""
        from .. import keywallet
        from ..service import sign_message
        key = keywallet.node_key(state)
        expires = int(time.time()) + 30 * 86400
        sig = sign_message(key._secret, llmlib.grant_words(sid, address, expires), llmlib.MAGIC)
        books.keep_grant("node", sid, address, expires, sig)

    def helper_sources() -> list[dict]:
        """Every model the helper could speak through, each `approved` or not: this arcade's own (approved unless the
        operator switched it off), and each model another arcade shares with this operator free -- used only once this
        arcade's operator approved it (Admin -> LLM -> Arcade helper)."""
        op_tag = (state.operator_tag() if hasattr(state, "operator_tag") else "") or ""
        ident, address = helper_identity()
        cfg = llmlib.config(state)
        ok = set(cfg.get("helper_ok") or [])
        out = []
        if not address:
            return out
        for o in offers():
            for m in o.get("models") or []:
                if o["here"]:
                    out.append({"o": o, "m": m["id"], "ready": True, "approved": not cfg.get("helper_off_own")})
                elif op_tag and op_tag in (o.get("free_tags") or []):
                    granted = books.grant(ident, o["seller"]) is not None
                    out.append({"o": o, "m": m["id"], "ready": ident == "node" or granted,
                                "needs_grant": not granted, "approved": f"{o['seller']}|{m['id']}" in ok})
        return out

    def helper_pick() -> dict | None:
        """The approved source the helper uses now: this arcade's own first, then the least busy; None when none."""
        ready = [x for x in helper_sources() if x["ready"] and x["approved"]]
        if not ready:
            return None
        rank = {"idle": 0, "steady": 1, "busy": 2, "full": 3}
        ready.sort(key=lambda x: (0 if x["o"]["here"] else 1, rank.get((x["o"].get("load") or {}).get("word"), 1)))
        return {**ready[0]["o"], "models": [{"id": ready[0]["m"]}]}

    @app.get("/account/llm/helper")
    def llm_helper_state(request: Request):
        o = helper_pick()
        return JSONResponse({"available": o is not None, "model": (o["models"][0]["id"] if o else ""),
                             "seller": (o and ("This arcade" if o["here"] else ("@" + o["tag"] if o.get("tag") else
                                                                                 o.get("host") or "another arcade"))) or ""},
                            headers={"Cache-Control": "no-store"})

    @app.post("/account/llm/helper")
    async def llm_helper(request: Request):
        import asyncio
        try:
            said = await body_of(request)
            account, address = who(request)
            client = account.pubkey if account else "ip:" + (request.headers.get("cf-connecting-ip") or
                                                            (request.client.host if request.client else "?"))
            now = time.time()
            recent = [t for t in asked_at.get(client, []) if now - t < 3600]
            if len(recent) >= (HELPER_HOUR if account else HELPER_GUEST_HOUR):
                raise llmlib.LlmError("that is a lot of questions for one hour; ask again a little later"
                                      + ("" if account else ", or sign in for more"))
            o = helper_pick()
            if o is None:
                raise llmlib.LlmError("no model is shared with this arcade right now")
            messages = [m for m in (said.get("messages") or []) if isinstance(m, dict)
                        and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)][-16:]
            if not messages or messages[-1]["role"] != "user":
                raise llmlib.LlmError("ask something")
            for m in messages:
                m["content"] = m["content"][:6000]
            page = str(said.get("page") or "")[:600]
            # Answer at once: a helper's question wants no long private thinking first (Qwen-style chat templates read
            # this; other servers ignore a field they do not know).
            body = {"model": o["models"][0]["id"], "messages": messages, "docs": True, "page": page,
                    "max_tokens": HELPER_OUT, "chat_template_kwargs": {"enable_thinking": False}}
            recent.append(now)
            asked_at[client] = recent
            ident, address = helper_identity()
            if not o["here"] and ident == "node" and books.grant("node", o["seller"]) is None:
                await asyncio.to_thread(node_grant, o["seller"], address)

            class _Op:
                pubkey = ident
            gen = await asyncio.to_thread(first, ask(_Op, address, o["seller"], "chat", "free", body, via="helper"))
        except llmlib.LlmError as exc:
            return refuse(exc)
        return stream(gen, openai=False)

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
            sid, leg = str(said.get("seller") or HERE), str(said.get("leg") or "auto")
            if leg not in ("free", "auto"):                         # "auto": whatever coin the credit is in
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


# ------------------------------------------------------------------------------------------------ finding model servers

COMMON_PORTS = (8000, 8080, 11434, 1234, 5000, 5001, 8188, 7860, 18300)
SKIP_PORTS = frozenset({22, 53, 631})


def listening_ports() -> set[int]:
    """The TCP ports this machine listens on (Linux /proc/net), plus the usual model-server ports."""
    ports = set(COMMON_PORTS)
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(name) as fh:
                next(fh, None)
                for line in fh:
                    f = line.split()
                    if len(f) > 3 and f[3] == "0A":                 # LISTEN
                        ports.add(int(f[1].rsplit(":", 1)[1], 16))
        except OSError:
            continue
    return {p for p in ports if p >= 1024 and p not in SKIP_PORTS}


def _server_name(port: int, models: list[dict]) -> str:
    owners = {str(m.get("owned_by") or "").lower() for m in models}
    if "vllm" in owners:
        return "vLLM"
    if "llamacpp" in owners or "llama.cpp" in owners:
        return "llama.cpp"
    if port == 11434 or "library" in owners or "ollama" in owners:
        return "Ollama"
    if port == 1234 or "organization_owner" in owners:
        return "LM Studio"
    return "OpenAI-compatible server"


def probe(port: int) -> dict | None:
    """What answers on 127.0.0.1:`port`: {"kind": "text"|"image", url, server, models} or None."""
    import requests as rq
    base = f"http://127.0.0.1:{port}"
    try:
        r = rq.get(f"{base}/v1/models", timeout=1.5)
        if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
            data = (r.json() or {}).get("data")
            if isinstance(data, list) and data and all(isinstance(m, dict) and m.get("id") for m in data):
                return {"kind": "text", "url": base, "server": _server_name(port, data), "port": port,
                        "models": [{"id": str(m["id"]),
                                    "ctx": int(m.get("max_model_len") or m.get("context_length") or 0)}
                                   for m in data[:50]]}
    except Exception:                                               # noqa: BLE001
        pass
    try:
        r = rq.get(f"{base}/system_stats", timeout=1.5)
        if r.status_code == 200 and isinstance(r.json(), dict) and "system" in r.json():
            from .. import imagegen
            models, why = [], ""
            for _ in range(2):                  # it can be stopped while asked (another model taking the GPU's slot)
                try:
                    models = imagegen.ComfyServer(base).models()
                    why = "" if models else "it holds no Qwen-Image model (the one family that can be shared now)"
                    break
                except Exception as exc:                            # noqa: BLE001
                    why = f"it stopped answering while asked ({type(exc).__name__}); look again"
                    import time as _t
                    _t.sleep(2)
            return {"kind": "image", "url": base, "server": "ComfyUI", "port": port, **({"why": why} if why else {}),
                    "models": [{"id": m["id"], "label": m["label"]} for m in models]}
    except Exception:                                               # noqa: BLE001
        pass
    return None


def sleeping_comfy() -> list[dict]:
    """A ComfyUI that starts on demand: a systemd user unit named for it, not running (its wake command offered)."""
    import shutil
    import subprocess
    if not shutil.which("systemctl"):
        return []
    try:
        out = subprocess.run(["systemctl", "--user", "list-unit-files", "--no-legend", "--no-pager", "*comfy*"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:                                               # noqa: BLE001
        return []
    found = []
    for line in out.splitlines():
        unit = line.split()[0] if line.split() else ""
        if not unit.endswith(".service") or "idle" in unit:
            continue
        active = subprocess.run(["systemctl", "--user", "is-active", "--quiet", unit]).returncode == 0
        if not active:
            found.append({"unit": unit, "wake": f"systemctl --user start {unit[:-8]}", "url": "http://127.0.0.1:8188"})
    return found


def discover(skip: set[int] | None = None) -> dict:
    from concurrent.futures import ThreadPoolExecutor
    ports = sorted(listening_ports() - (skip or set()))
    with ThreadPoolExecutor(max_workers=24) as pool:
        got = [g for g in pool.map(probe, ports) if g]
    text = [g for g in got if g["kind"] == "text"]
    seen: dict[tuple, str] = {}
    for g in text:                                  # a proxy in front of the same server lists the same models
        key = tuple(sorted(m["id"] for m in g["models"]))
        if key in seen:
            g["same_as"] = seen[key]
        else:
            seen[key] = g["url"]
    images = [g for g in got if g["kind"] == "image"]
    return {"text": text, "images": images, "asleep": [] if images else sleeping_comfy(), "ports": len(ports)}


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

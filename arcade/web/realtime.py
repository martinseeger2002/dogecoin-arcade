"""Realtime rooms for inscribed pages: the routes between a viewer and the mesh.

An inscribed page cannot reach the mesh, or anything else: its sandbox talks
only to the page showing it. So a game page asks its viewer (the piece page or
full screen, `_page_doors.js.html`) over postMessage, exactly like
`arcade.storage`, and the viewer talks to these routes. The page never learns
a token, a key or an address it was not meant to, and it can only ever be in
rooms of its own game -- the viewer names the game from the frame, not from
anything the page says, unless the page asks for a game family it declares.

    GET  /realtime/hello            is the mesh up, who would I be, what to sign
    POST /realtime/join             into a room, as an address (signed) or a guest
    POST /realtime/send             one message, opaque, at most 512 bytes
    POST /realtime/leave            out again
    GET  /realtime/stream?token=    what happens in the room, as server-sent events
    GET  /r/realtime.js             the page's half (templates/realtime.js)

**Who joins.** The operator's own viewer joins as this node's address, signed
by the node's own wallet. A signed-in account joins as its address with a
certificate its browser signed (the node checks it before using it, so a
browser cannot join as somebody else). Anybody else joins as a guest.

**Why the stream is async when every other route is not.** Every route in
create_app runs in the thread pool, and a test holds them to it. An open
stream is not a request that finishes: one per player per game, held for as
long as they play, would take a pool thread each and starve every other page
on the node. So the stream lives here, as an async generator that holds no
thread while it waits, and it does nothing but wait on the mesh's queue.
"""

from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ..mesh.node import MAX_PAYLOAD, MeshError
from ..mesh.service import MESSAGE_MAGIC, cert_message

TEMPLATE_DIR = Path(__file__).parent / "templates"
GAME = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
ROOM_MAX = 60
HEARTBEAT = 4.0


def room_key(game: Any, room: Any) -> str:
    game, room = str(game or ""), str(room or "")
    if not GAME.match(game):
        raise MeshError("a game is an inscription id or a short name: letters, digits, . _ : -")
    if not room or len(room) > ROOM_MAX or any(ord(c) < 32 for c in room):
        raise MeshError(f"a room is a name of 1 to {ROOM_MAX} characters")
    return f"{game}/{room}"


def register(app, state, *, account_view: Callable, signed_in: Callable,
             account_address: Callable, account_chain: Callable,
             check_csrf: Callable) -> None:

    tag_cache: dict[str, tuple[str, float]] = {}

    def tag_of(address: str | None) -> str:
        if not address:
            return ""
        hit = tag_cache.get(address)
        if hit and hit[1] > time.monotonic():
            return hit[0]
        try:
            tag = state.token_index(account_chain()).tag_of(address) or ""
        except Exception:                          # noqa: BLE001 -- a name is a nicety
            tag = ""
        tag_cache[address] = (tag, time.monotonic() + 60)
        return tag

    def who(member: str, node: str, cred: dict | None) -> dict:
        address = cred.get("a") if isinstance(cred, dict) else None
        return {"id": member, "node": node, "address": address,
                "tag": tag_of(address), "guest": address is None}

    def identity(request: Request) -> tuple[str, str]:
        """(kind, address): "operator", "account" or "guest"."""
        if not account_view(request):
            return "operator", state.derived_address or ""
        account = signed_in(request)
        if account is not None:
            address = account_address(account.pubkey, account_chain())
            if address:
                return "account", address
        return "guest", ""

    def mesh():
        if state.mesh is None or state.mesh.loop is None:
            raise MeshError("this node is not on the mesh")
        return state.mesh

    def refused(exc: Exception, status: int = 400) -> JSONResponse:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=status)

    @app.get("/realtime/hello")
    def realtime_hello(request: Request):
        try:
            svc = mesh()
        except MeshError as exc:
            return JSONResponse({"ok": True, "online": False, "why": str(exc)})
        kind, address = identity(request)
        # Online means this node runs the mesh: two people on this same node can
        # play together with no peer at all, and peers come and go mid-game.
        out = {"ok": True, **svc.status(), "online": True,
               "who": {"kind": kind, "address": address or None, "tag": tag_of(address)}}
        if kind == "account":
            expires = svc.expiry()
            chain = account_chain()
            out["sign"] = {"message": cert_message(svc.network, svc.node_id, address, expires),
                           "expires": expires, "magic": MESSAGE_MAGIC,
                           "chain": {"network": chain.network,
                                     "version": chain.params.pubkeyhash_version}}
        return JSONResponse(out)

    @app.post("/realtime/join")
    def realtime_join(request: Request, body: dict):
        check_csrf(str(body.get("csrf_token", "")))
        try:
            svc = mesh()
            room = room_key(body.get("game"), body.get("room"))
            kind, address = identity(request)
            cred, member = None, ""
            if kind == "operator" and address:
                expires = svc.expiry()
                try:
                    with state.messaging.rpc() as rpc:
                        sig = rpc.call("signmessage", address,
                                       cert_message(svc.network, svc.node_id, address, expires))
                    cred, member = {"a": address, "x": expires, "s": str(sig)}, address
                except Exception:                  # noqa: BLE001 -- a locked wallet plays as a guest
                    cred = None
            elif kind == "account" and body.get("sig"):
                said = {"a": address, "x": int(body.get("expires") or 0), "s": str(body["sig"])}
                if not svc.check_cert(address, said):
                    raise MeshError("that signature does not prove this account's address")
                cred, member = said, address
            if cred is None:
                member = svc.new_guest()
            session = svc.join(room, member, cred)
            members = [who(m["member"], m["node"], m["cred"]) for m in svc.members(room)]
        except MeshError as exc:
            return refused(exc)
        return JSONResponse({"ok": True, "token": session.token, "online": True,
                             "me": who(member, svc.node_id, cred), "members": members})

    @app.post("/realtime/send")
    def realtime_send(request: Request, body: dict):
        check_csrf(str(body.get("csrf_token", "")))
        try:
            if "b64" in body:
                data = base64.b64decode(str(body["b64"]), validate=True)
            else:
                data = str(body.get("data", "")).encode("utf-8")
            if len(data) > MAX_PAYLOAD:
                raise MeshError(f"a message is at most {MAX_PAYLOAD} bytes")
            mesh().send(str(body.get("token", "")), data)
        except (MeshError, ValueError) as exc:
            return refused(exc, 429 if "too fast" in str(exc) else 400)
        return JSONResponse({"ok": True})

    @app.post("/realtime/leave")
    def realtime_leave(request: Request, body: dict):
        check_csrf(str(body.get("csrf_token", "")))
        try:
            mesh().leave(str(body.get("token", "")))
        except MeshError:
            pass                                    # already gone is gone
        return JSONResponse({"ok": True})

    def event_json(ev) -> str:
        out = {"type": ev.type, "room": ev.room.split("/", 1)[1],
               "member": who(ev.member, ev.node, ev.cred)}
        if ev.type == "message":
            try:
                out["data"] = ev.data.decode("utf-8")
            except UnicodeDecodeError:
                out["b64"] = base64.b64encode(ev.data).decode()
        return json.dumps(out, separators=(",", ":"))

    @app.get("/realtime/stream")
    async def realtime_stream(request: Request, token: str = ""):
        try:
            svc = mesh()
            svc.session(token)
        except MeshError as exc:
            return refused(exc, 404)

        async def events():
            yield "retry: 2000\n\n"
            while not await request.is_disconnected():
                try:
                    ev = await svc.next_event(token, HEARTBEAT)
                except MeshError as exc:
                    yield "event: closed\ndata: " + json.dumps({"why": str(exc)}) + "\n\n"
                    return
                svc.touch(token)                    # still connected, so still here
                if ev is None:
                    yield ": still here\n\n"
                else:
                    yield "data: " + event_json(ev) + "\n\n"

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store",
                                          "X-Accel-Buffering": "no"})

    @app.get("/r/realtime.js")
    def r_realtime_js():
        """The page's half: `arcade.realtime`. Same for every page, like storage.js."""
        from . import content as contentlib
        body = (TEMPLATE_DIR / "realtime.js").read_text()
        return Response(body, media_type="application/javascript",
                        headers={**contentlib.CORS, "Cache-Control": "public, max-age=3600"})

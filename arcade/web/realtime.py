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

**Any node can be a player's way in** (2026-10-12, the operator: "I want the client to be able
to access any one of the connected nodes as their game server"). A room is the same room on
every node, so the viewer may carry a player through whichever node answers it fastest
(`/realtime/nodes` lists them; _page_doors picks). Those calls come from another site, so
the room routes say CORS yes -- they act only on a token or a certificate, never on a cookie,
so another site gains nothing it could not get by joining itself. A player signed in
elsewhere is still themselves here: their wallet signs THIS node's certificate (the words
come from `hello.cert`) and join takes `address` + `sig` with no session at all.

**Why the stream is async when every other route is not.** Every route in
create_app runs in the thread pool, and a test holds them to it. An open
stream is not a request that finishes: one per player per game, held for as
long as they play, would take a pool thread each and starve every other page
on the node. So the stream lives here, as an async generator that holds no
thread while it waits, and it does nothing but wait on the mesh's queue.
"""

from __future__ import annotations

import base64
import logging
import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ..mesh.node import MAX_PAYLOAD, MeshError

log = logging.getLogger("arcade.realtime")
from ..mesh.service import GUEST, MESSAGE_MAGIC, cert_message

TEMPLATE_DIR = Path(__file__).parent / "templates"
GAME = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
CORS_HEADERS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Max-Age": "600"}
ROOM_MAX = 60
HEARTBEAT = 4.0
PUBLIC_STUN = "stun:stun.cloudflare.com:3478"
# THE RELAY (2026-10-23: two homes "can't hear or see each other" -- Starlink's shared IPv4 lets no direct
# link through; chose "Cloudflare TURN"). The operator's TURN key (Admin, Network: setting `turn`); signed-in players
# get short-lived logins to it, so a stranger's browser cannot spend the arcade's relay.
TURN_API = "https://rtc.live.cloudflare.com/v1/turn/keys/{key}/credentials/generate-ice-servers"
TURN_TTL = 6 * 3600
# THE METER (2026-10-23: "some sort of usage meter on my node so that I don't go over the limit"). Cloudflare
# bills the relay by its egress (relay -> player), read from its GraphQL analytics with an Account Analytics token;
# at the operator's monthly limit the relay is no longer handed out (direct links only) until the month turns.
TURN_GRAPHQL = "https://api.cloudflare.com/client/v4/graphql"
TURN_LIMIT_GB = 1000            # Cloudflare's free relay allowance a month; the operator can set another
METER_EVERY = 600               # seconds between two asks of Cloudflare
TURN_USAGE_QUERY = """query($account: String!, $key: String!, $from: Date!, $to: Date!) {
  viewer { accounts(filter: {accountTag: $account}) {
    callsTurnUsageAdaptiveGroups(limit: 10000, filter: {keyId: $key, date_geq: $from, date_leq: $to}) {
      sum { egressBytes ingressBytes }
    }
  } }
}"""   # after the nodes' own, unless the operator says not (realtime_ice)
MAX_ICE_NODES = 4                               # more STUN servers only slow a browser's gathering


def turn_servers(key_id: str, token: str, ttl: int = TURN_TTL, post=None) -> list[dict]:
    """Fresh logins to the operator's Cloudflare TURN key: ICE servers with a username and credential.
    Raises on anything but a good answer."""
    import requests
    post = post or requests.post
    answer = post(TURN_API.format(key=key_id), timeout=10, json={"ttl": int(ttl)},
                  headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    if not 200 <= answer.status_code < 300:
        try:
            why = str((answer.json() or {}).get("error") or "")[:120]
        except Exception:                            # noqa: BLE001 -- not JSON
            why = ""
        # 404 "cannot find specified key": the id is not a TURN key's (2026-10-23: an Account or App id pasted).
        raise MeshError(f"Cloudflare answered {answer.status_code}" + (f": {why}" if why else "")
                        + (" -- use the TURN key's own id (Realtime -> TURN Server)" if answer.status_code == 404 else ""))
    said = answer.json().get("iceServers")
    servers = said if isinstance(said, list) else [said] if isinstance(said, dict) else []
    out = [{k: v for k, v in s.items() if k in ("urls", "username", "credential")}
           for s in servers if isinstance(s, dict) and s.get("username") and s.get("credential")]
    if not out:
        raise MeshError("Cloudflare gave no relay")
    return out


def turn_usage(account_id: str, token: str, key_id: str, today=None, post=None) -> dict:
    """This calendar month's relay traffic for one TURN key, in bytes: {"egress", "ingress", "from", "to"}."""
    import datetime

    import requests
    post = post or requests.post
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    start = today.replace(day=1)
    answer = post(TURN_GRAPHQL, timeout=20, headers={"Authorization": f"Bearer {token}",
                                                      "Content-Type": "application/json"},
                  json={"query": TURN_USAGE_QUERY, "variables": {"account": account_id, "key": key_id,
                                                                 "from": start.isoformat(), "to": today.isoformat()}})
    try:
        said = answer.json() or {}
    except Exception:                                # noqa: BLE001
        said = {}
    if not 200 <= answer.status_code < 300 or said.get("errors"):
        why = "; ".join(str(e.get("message", e)) for e in (said.get("errors") or [])[:2]) or str(answer.status_code)
        raise MeshError(f"Cloudflare analytics: {why}"[:300])
    accounts = ((said.get("data") or {}).get("viewer") or {}).get("accounts") or []
    if not accounts:
        raise MeshError("Cloudflare analytics: that account id is not this token's")
    groups = accounts[0].get("callsTurnUsageAdaptiveGroups") or []
    egress = sum(int((g.get("sum") or {}).get("egressBytes") or 0) for g in groups)
    ingress = sum(int((g.get("sum") or {}).get("ingressBytes") or 0) for g in groups)
    return {"egress": egress, "ingress": ingress, "from": start.isoformat(), "to": today.isoformat()}


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
    # A game's service on this machine (arcade/service.py): it shows the node's
    # service key instead of a form token, and is the operator. Never through
    # the door: a public request with the key is just a request without a token.
    from ..service import HEADER as SERVICE_HEADER, service_key
    import secrets as _secrets
    svc_key = service_key(state.home)

    def form_ok(request: Request, body: dict) -> None:
        shown = request.headers.get(SERVICE_HEADER, "")
        if shown and svc_key and not account_view(request) and _secrets.compare_digest(shown, svc_key):
            return
        # A public request with no cookie at all has no ambient authority to forge: it is a
        # guest, a certificate, or a token it was given (another node's viewer, cross-site).
        if account_view(request) and not request.cookies:
            return
        check_csrf(str(body.get("csrf_token", "")))

    @app.middleware("http")
    async def realtime_cors(request: Request, call_next):
        """The room routes, from any site (see the notes): never with credentials."""
        path = request.url.path
        if not path.startswith("/realtime/"):
            return await call_next(request)
        if request.method == "OPTIONS":
            return Response(status_code=204, headers=CORS_HEADERS)
        response = await call_next(request)
        for k, v in CORS_HEADERS.items():
            response.headers[k] = v
        return response

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
        # The words a player signed in on ANOTHER node signs to be themselves here (see the notes).
        out["cert"] = {"network": svc.network, "node": svc.node_id, "expires": svc.expiry(),
                       "magic": MESSAGE_MAGIC}
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
        form_ok(request, body)
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
            elif kind == "guest" and body.get("sig") and body.get("address"):
                # Signed in on another node, playing through this one: the certificate is the proof.
                claimed = str(body["address"])[:64]
                said = {"a": claimed, "x": int(body.get("expires") or 0), "s": str(body["sig"])}
                if not svc.check_cert(claimed, said):
                    raise MeshError("that signature does not prove this address on this node")
                cred, member = said, claimed
            if cred is None:
                # A guest coming back after the node restarted (or after its
                # stream dropped) keeps the name it had, so the other players
                # see the same player return. Only a name nobody here holds
                # now: `join` hands an existing seat to whoever names it.
                asked = str(body.get("guest") or "")
                member = (asked if GUEST.match(asked) and not svc.present(room, asked)
                          else svc.new_guest())
            session = svc.join(room, member, cred)
            members = [who(m["member"], m["node"], m["cred"]) for m in svc.members(room)]
        except MeshError as exc:
            return refused(exc)
        return JSONResponse({"ok": True, "token": session.token, "online": True,
                             "me": who(member, svc.node_id, cred), "members": members})

    @app.post("/realtime/send")
    def realtime_send(request: Request, body: dict):
        form_ok(request, body)
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
        form_ok(request, body)
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
                    # A named event, not a comment (2026-10-18, ASHVALE dropping): a browser never shows a comment to
                    # the page, so a stream that went quiet and one that died looked the same. A page that does not
                    # listen for "ping" ignores it.
                    yield "event: ping\ndata: {}\n\n"
                else:
                    yield "data: " + event_json(ev) + "\n\n"

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store",
                                          "X-Accel-Buffering": "no"})

    @app.get("/realtime/nodes")
    def realtime_nodes(request: Request):
        """Ways into the mesh a browser can use (https): this node's own public names, the
        tunnel addresses of nodes it knows, and arcade domains on the chain. The viewer
        measures them and picks the quickest (_page_doors)."""
        out, seen = [], set()

        def add(base: str, node: str | None) -> None:
            base = base.rstrip("/")
            if base and base not in seen and base.startswith("https://") and len(out) < 50:
                seen.add(base)
                out.append({"base": base, "node": node})
        try:
            svc = mesh()
        except MeshError:
            return JSONResponse({"ok": True, "nodes": []})
        if svc.tunnel_url:
            add("https://" + svc.tunnel_url[len("wss://"):].rsplit("/mesh", 1)[0], svc.node_id)
        now = time.time()
        for (host, _port), e in sorted(svc.node.addrs.items(), key=lambda kv: -(kv[1].get("ok") or 0)):
            if str(host).startswith("wss://") and e.get("id") and (e.get("id") in svc.node.links
                                                                  or now - (e.get("ok") or 0) < 3600):
                add("https://" + host[len("wss://"):].rsplit("/mesh", 1)[0], e["id"])
        try:
            if state.store_path.exists():
                with state.store() as store:
                    for row in store.instances(state.messaging.params.name)[:20]:
                        add("https://" + str(row["domain"]), None)
        except Exception:                          # noqa: BLE001 -- the directory is a nicety
            pass
        return JSONResponse({"ok": True, "self": svc.node_id, "nodes": out})

    relay_kept: dict[str, tuple[float, list]] = {}
    meter: dict[str, Any] = {}

    def relay_meter(fresh: bool = False) -> dict:
        """This month's relay use against the operator's limit, asked of Cloudflare at most every METER_EVERY s."""
        turn = state.settings().get("turn") or {}
        limit_gb = float(turn.get("limit_gb") or TURN_LIMIT_GB) if isinstance(turn, dict) else TURN_LIMIT_GB
        out = {"limit_gb": limit_gb, "metered": False}
        if not (isinstance(turn, dict) and turn.get("key_id") and turn.get("account_id") and turn.get("analytics")):
            return out
        kept = meter.get("said")
        if kept and not fresh and time.time() - meter.get("at", 0) < METER_EVERY:
            return {**kept, "limit_gb": limit_gb, "over": kept["used_gb"] >= limit_gb}
        try:
            got = turn_usage(str(turn["account_id"]), str(turn["analytics"]), str(turn["key_id"]))
            said = {"metered": True, "used_gb": round(got["egress"] / 1e9, 3), "in_gb": round(got["ingress"] / 1e9, 3),
                    "since": got["from"], "asked": int(time.time())}
        except Exception as exc:                     # noqa: BLE001 -- the last reading stands, and says so
            said = {**(kept or {"metered": False, "used_gb": 0.0}), "error": str(exc)[:200], "asked": int(time.time())}
        meter.update(said=said, at=time.time())
        return {**said, "limit_gb": limit_gb, "over": bool(said.get("metered")) and said["used_gb"] >= limit_gb}

    state.relay_meter = relay_meter                  # Admin's Call relay panel reads it

    def relay_for(request: Request, app_id: str = "") -> list[dict]:
        """The operator's TURN relay, for a signed-in player (or the operator) only, in the apps the operator chose
        (2026-10-23: "what apps on my arcade are allowed to use my relay?" -- "Apps I choose, any account";
        setting turn.apps, the page each app's Open opens; none chosen yet: every app). Kept half its life per player."""
        turn = state.settings().get("turn") or {}
        if not (isinstance(turn, dict) and turn.get("key_id") and turn.get("token")):
            return []
        chosen = turn.get("apps")
        if isinstance(chosen, list) and str(app_id or "").lower() not in chosen:
            return []
        kind, address = identity(request)
        if kind == "guest" or not address:
            return []
        if relay_meter().get("over"):
            return []                                # the month's limit is reached: direct links only
        kept = relay_kept.get(address)
        if kept and time.time() - kept[0] < TURN_TTL / 2:
            return kept[1]
        try:
            servers = turn_servers(str(turn["key_id"]), str(turn["token"]))
        except Exception as exc:                     # noqa: BLE001 -- no relay this time: direct only
            log.info("turn: %s", exc)
            return []
        relay_kept[address] = (time.time(), servers)
        return servers

    @app.get("/realtime/ice")
    def realtime_ice(request: Request):
        """The STUN servers a browser should use for the fast lane (2026-10-18, the operator: "Nodes, then public"):
        every arcade node's STUN that another node has proved reachable (mesh/stun.py), then a public one unless
        the operator turned that off (setting realtime_public_stun). And the operator's TURN relay, if set, for a
        signed-in player (relay_for)."""
        servers = []
        try:
            share = getattr(mesh(), "stun", None)
        except MeshError:
            share = None
        for host, port in (share.known() if share is not None else [])[:MAX_ICE_NODES]:
            servers.append({"urls": f"stun:[{host}]:{port}" if ":" in host else f"stun:{host}:{port}"})
        if state.settings().get("realtime_public_stun", True) is not False:
            servers.append({"urls": PUBLIC_STUN})
        relay = relay_for(request, request.query_params.get("app", ""))
        servers.extend(relay)
        return JSONResponse({"ok": True, "iceServers": servers, "relay": bool(relay)},
                            headers={"Cache-Control": "no-store"})

    @app.get("/r/realtime.js")
    def r_realtime_js():
        """The page's half: `arcade.realtime`. Same for every page, like storage.js."""
        from . import content as contentlib
        body = (TEMPLATE_DIR / "realtime.js").read_text()
        return Response(body, media_type="application/javascript",
                        headers={**contentlib.CORS, "Cache-Control": "public, max-age=3600"})

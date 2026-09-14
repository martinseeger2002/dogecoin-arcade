"""The DogecoinArcade web interface.

Messenger only, for now. The Exchange, NFT and Inscription sections are shown
but disabled, because the engines behind them (M3-M5) do not exist yet -- a menu
that pretends otherwise would be worse than one that says so.
"""

from __future__ import annotations

import contextlib
import base64
import datetime as dt
import hashlib
import threading
import time
import html
import secrets
import sys
import os
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    HTMLResponse, JSONResponse, RedirectResponse, Response,
)
from fastapi.templating import Jinja2Templates

from .. import backup, media, tokens as tokenlib, wallet as walletlib
from ..ledger import AmountError, format_amount, parse_amount
from ..config import NETWORKS, MainnetRefused, WrongChain
from .. import inscribe as inscribelib
from . import guide as guidelib
from .. import tags as taglib
from .. import remote as remotelib
from ..messaging import contact, content, group
from ..script import b58check_decode
from ..messaging.derive import DerivationError, derive_identity
from ..messaging.envelope import (
    MAX_ANNOUNCE_NAME, MAX_ANNOUNCE_NAME_CLASS_B,
    announcement_fits_one_output, build_key_announcement,
)
from ..messaging.keys import fingerprint_of
from ..messaging.miner import Miner, MiningError
from ..messaging.scanner import Scanner, find_own_announcements
from ..messaging.sender import (
    MessageSender, SendError, describe_duration, estimate_readable_seconds,
    send_cost,
    estimate_send_seconds,
    funded_address, plan_message, record_sent, recent_block_seconds,
)
from . import content as contentlib
from . import rpc as botrpc
from .state import AppState

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Sections that exist, and sections that do not. Shown honestly rather than
# hidden, so the shape of the finished product is visible.
#: (path, label, chain, built). `chain` is shown in the interface on every page,
#: because a user who cannot tell whether an action spends testnet or real coins
#: is one misclick from a bad day (D-012).
#: How much of a long thing to draw at once. A busy channel or a conversation
#: years old is not something a phone over a tunnel can be asked to render, and
#: the part anybody is looking at is the end of it.
PAGE_POSTS = 40
PAGE_MESSAGES = 60
PAGE_INSCRIPTIONS = 24

NAV = [
    ("/",             "Overview",     None,        True),
    ("/messages",     "Messages",     "testnet",   True),
    ("/contacts",     "Address book", None,        True),
    ("/groups",       "Public",       None,        True),
    ("/backup",       "Backup",       None,        True),
    ("/keys",         "Keys",         "testnet",   True),
    ("/wallet",       "Wallets",      None,        True),
    ("/tokens",       "Tokens",       "mainnet",   True),
    ("/nfts",         "NFTs",         "mainnet",   False),
    ("/exchange",     "Exchange",     "mainnet",   False),
    ("/inscriptions", "Inscriptions", "mainnet",   True),
    ("/remote",       "Remote",       None,        True),
    ("/guide",        "Guide",        None,        True),
]


LOCKED_PAGE = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DogecoinArcade</title>
<style>body{font:16px/1.5 system-ui,sans-serif;margin:0;display:grid;
place-items:center;min-height:100vh;background:#12131a;color:#e8e8ea}
div{max-width:26rem;padding:2rem;text-align:center}
h1{font-size:1.2rem}p{color:#a0a0ab}</style>
<div><h1>%s</h1><p>%s</p></div>
"""


def locked(title: str, detail: str, status: int = 403) -> HTMLResponse:
    """What a stranger sees. Deliberately plain: it names nothing about this
    wallet, because whoever is reading it has not shown they may see it."""
    return HTMLResponse(LOCKED_PAGE % (title, detail), status_code=status)


def remote_guard(state: AppState):
    """Refuse anything that arrived from outside without the key.

    Registered as middleware so it covers every route there is and every route
    anyone adds later. A door guarded route-by-route is a door that is open the
    first time somebody forgets.
    """
    async def guard(request: Request, call_next):
        tunnel = state.remote_tunnel()
        if not remotelib.is_remote(request.headers, request.headers.get("host", ""),
                                   tunnel.url if tunnel else None):
            return await call_next(request)

        if tunnel is None:
            return locked(
                "Not open",
                "This wallet is not accepting remote connections. It reached "
                "this answer because the request arrived through a proxy.")
        path = request.url.path
        if path.startswith("/rpc"):
            # The bot RPC has its own key, in a file on that machine, and it can
            # spend. It is for programs running beside the wallet, never for
            # anything that came in over the internet.
            return JSONResponse({"result": None, "id": None, "error": {
                "code": -32600,
                "message": "the bot RPC is not available through the tunnel"}},
                status_code=403)
        if path == "/remote/unlock":
            return await call_next(request)
        if not secrets.compare_digest(
                request.cookies.get(remotelib.COOKIE_NAME, ""), tunnel.token):
            return locked("Locked",
                          "Scan the QR code on the wallet's Remote page to open "
                          "this. The link on its own is not enough.")
        return await call_next(request)

    return guard


def create_app(state: AppState) -> FastAPI:
    @contextlib.asynccontextmanager
    async def _lifespan(_app: FastAPI):
        """Hold the shutdown while a send is in flight.

        Send threads are daemons, so nothing waits for them: the interpreter
        exits and the thread is killed wherever it happens to be. a test machine caught
        one writing to the store seventeen seconds AFTER a graceful `systemctl
        restart` -- the old process had released the port while its thread
        carried on, so two processes briefly shared the database. Nothing was
        harmed that time, and the pending-send record means a killed send can be
        finished rather than lost, but neither is a reason to let a restart land
        in the middle of a wallet operation.

        So: stop accepting new sends, and wait a short while for the running one
        to reach its own bookkeeping. If it does not, say so -- that line is the
        only warning anyone gets that a send was cut off, and the record of what
        went out is what to look at next.

        `begin_shutdown` blocks, deliberately, and this is the one place in the
        application where blocking the event loop is the point: nothing else
        needs it once shutdown has begun. Written without `await` so the guard
        that keeps the routes synchronous still reads cleanly.
        """
        yield
        if not state.begin_shutdown():
            print("arcade-web: a send was still running at shutdown. What is "
                  "already broadcast cannot be taken back; the interface will "
                  "offer to finish the rest when it starts again.",
                  file=sys.stderr, flush=True)

    app = FastAPI(title="DogecoinArcade", docs_url=None, redoc_url=None,
                  lifespan=_lifespan)
    app.middleware("http")(remote_guard(state))


    def render(request: Request, template: str, **context: Any) -> HTMLResponse:
        """Render a page, never from a cache.

        `no-store` because these pages carry live state -- a send in flight, a
        balance, an unread count -- and a browser re-serving an old one on a back
        navigation shows a message that has since gone or a send that has since
        finished. A page painted mid-send and restored from cache was part of how
        an interface got stuck looking busy against an idle server.
        """
        notice, notice_kind = state.take_notice()
        base = {
            "request": request,
            "nav": NAV,
            "path": request.url.path,
            "state": state,
            "csrf": state.csrf_token,
            "notice": notice,
            "notice_kind": notice_kind,
            "msg_net": state.messaging.label,
            "ledger_net": state.ledger.label,
        }
        base.update(context)
        # Request first: the older (name, context) signature is deprecated.
        response = TEMPLATES.TemplateResponse(request, template, base)
        # Never from a cache. These pages carry live state -- a send in flight,
        # a balance, an unread count -- and a browser re-serving an old one on a
        # back navigation shows a send that has since finished or a message that
        # has since gone. A page painted mid-send and restored from cache was
        # part of how an interface got stuck looking busy against an idle server.
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response

    @app.exception_handler(HTTPException)
    def refused(request: Request, exc: HTTPException):
        """Answer a refusal in the language of whoever asked.

        The status code stays what it was -- 400 for a rejected form -- because
        that is what made the protection legible to anything but a human. But a
        browser posting a form was being handed raw JSON, which is a worse
        experience than the 303 it replaced. So: the code for machines, a page
        for people, from the same refusal.
        """
        wants_html = "text/html" in (request.headers.get("accept") or "")
        if not wants_html:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return HTMLResponse(REFUSED_PAGE.format(detail=html.escape(str(exc.detail))),
                            status_code=exc.status_code)

    def check_csrf(token: str) -> None:
        """Reject a request whose form token does not match this process's.

        Raises `HTTPException(400)` rather than a plain error so the rejection
        has a status code that says what happened. It used to raise ValueError,
        which every handler caught into its own error path -- so a rejected
        request answered 200 or 303 and looked, to anything but a human reading
        the page, exactly like a successful one. a test machine audited this endpoint,
        saw `POST /publish-key -> 200` with no token, and had to check the chain
        and the wallet before concluding the protection worked. A check nobody
        can verify from the outside is a poor check even when it is sound.
        """
        import secrets as _s
        if not _s.compare_digest(token or "", state.csrf_token):
            raise HTTPException(
                status_code=400,
                detail="stale form -- reload the page and try again")

    def messaging_status() -> dict[str, Any]:
        """Testnet node health plus funding, which only the Messenger needs."""
        status = state.messaging.status()
        if status.get("online"):
            try:
                with state.messaging.rpc() as rpc:
                    status["funding"] = Miner(rpc, state.messaging.params).status()
            except HTTPException:
                raise          # a rejected form is a 400, not an error page
            except Exception as exc:
                status["funding"] = None
                status["funding_error"] = str(exc)
        return status

    def ledger_status() -> dict[str, Any]:
        """Mainnet node health. Read-only until a wallet is enabled there."""
        return state.ledger.status()

    # --- overview -------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request):
        stats = {}
        if state.store_path.exists():
            with state.store() as store:
                stats = store.stats()
        # Set the identity up silently on first visit. There is nothing to ask:
        # it comes from the wallet, so if the node is up it simply works.
        if not state.unlocked:
            try:
                state.ensure_identity()
            except HTTPException:
                raise          # a rejected form is a 400, not an error page
            except Exception:
                pass
        code, announced = None, True
        if state.unlocked:
            code = contact.encode(state.messaging.network, state.identity.public_bytes)
            # Until the key is on the chain, handing someone the address alone is
            # not enough -- they have nothing to encrypt to. Worth saying, since
            # the failure otherwise lands on the other person.
            if state.store_path.exists() and state.derived_address:
                with state.store() as store:
                    announced = store.key_for(state.derived_address) is not None
        return render(request, "overview.html", contact_code=code, announced=announced,
                      my_address=state.derived_address,
                      messaging=messaging_status(), ledger=ledger_status(), stats=stats)

    # --- identity -------------------------------------------------------------
    # No passphrase, no key file, nothing to write down. The identity is derived
    # from one wallet address, filed in the wallet under a fixed account, so
    # restoring wallet.dat restores the identity along with the coins.

    @app.post("/setup-identity")
    def setup_identity(request: Request, csrf_token: str = Form("")):
        try:
            check_csrf(csrf_token)
            state.ensure_identity()
            state.flash("You are ready to send and receive messages.", "ok")
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except (DerivationError, ValueError) as exc:
            state.flash(str(exc), "err")
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except Exception as exc:
            state.flash(f"Could not reach the testnet node: {exc}", "err")
        return RedirectResponse("/", status_code=303)

    # --- messenger ------------------------------------------------------------


    def _send_in_background(sender, address, plan, peer_key, payload_body,
                            own_copy, digest, attachment=None):
        """Send a long message on a thread, reporting progress as it goes.

        The send lock is already held by the caller; it is released here, at the
        end of the real work rather than the end of the request.
        """
        def work():
            try:
                with state.store() as store:
                    # Written down before anything is broadcast, so an interrupted
                    # send can be finished rather than stranded.
                    store.begin_pending_send(plan.msg_id, peer_key, address,
                                             payload_body, plan.chunk_payloads)

                def note(text, index, total):
                    state.update_progress(note=text)

                sender.ensure_outputs(address, plan.transactions, on_progress=note)

                def sent_one(index, total, txid):
                    state.update_progress(done=index,
                                          note=f"sent {index} of {total}")
                    with state.store() as store:
                        store.record_pending_progress(plan.msg_id, txid)

                txids = sender.send_all(address, plan.chunk_payloads,
                                        on_progress=note, on_broadcast=sent_one)
                with state.store() as store:
                    store.finish_pending_send(plan.msg_id)
                    record_sent(store, txids[0], peer_key,
                                state.identity.fingerprint, own_copy,
                                txids=txids, **(attachment or {}))
                state.note_send(digest)
                state.finish_progress()
            except Exception as exc:
                # Says what is already on the chain, because a part-sent message
                # cannot be finished later and that is the thing worth knowing.
                state.finish_progress(error=str(exc))
            finally:
                state.end_send()

        threading.Thread(target=work, name="arcade-send", daemon=True).start()

    def _record_post(store, txid, local, txids=None):
        """Keep our own copy of a post. One definition, three callers.

        The route, the background thread and the resume path all have to write
        the same row, and three copies of an eight-argument call is how they
        drift -- which is exactly how the CLI and the web came to record sends
        differently and each machine showed half a conversation.
        """
        store.add_group_post(
            local["network"], local["channel"], txid, 0, int(time.time()),
            local["address"], local["nickname"], local["text"], mine=True,
            file_name=local["file_name"], file_type=local["file_type"],
            file_data=local["file_data"], txids=txids or [txid])

    def _post_in_background(sender, address, plan, local):
        """Post a chunked public post on a thread, reporting as it goes.

        The send lock is already held by the caller and is released here, at the
        end of the real work rather than the end of the request.
        """
        def work():
            try:
                with state.store() as store:
                    # Written down before anything is broadcast. A post that
                    # stops half way has spent outputs that cannot be recovered,
                    # so the record of what went out is the one thing that must
                    # survive the interruption.
                    store.begin_pending_post(
                        plan.msg_id, local["network"], local["channel"],
                        local["nickname"], address, local["text"].encode(),
                        plan.payloads)

                def note(text, index, total):
                    state.update_progress(note=text)

                sender.ensure_outputs(address, plan.transactions,
                                      on_progress=note)

                def sent_one(index, total, txid):
                    state.update_progress(done=index,
                                          note=f"posted {index} of {total}")
                    with state.store() as store:
                        store.record_pending_progress(plan.msg_id, txid)

                txids = sender.send_all(address, plan.payloads,
                                        on_progress=note, on_broadcast=sent_one)
                with state.store() as store:
                    store.finish_pending_send(plan.msg_id)
                    _record_post(store, txids[0], local, txids)
                state.finish_progress()
            except Exception as exc:
                # Says what is already on the chain: a part-sent post cannot be
                # taken back and that is the thing worth knowing.
                state.finish_progress(error=str(exc))
            finally:
                state.end_send()

        threading.Thread(target=work, name="arcade-post", daemon=True).start()

    @app.get("/messages", response_class=HTMLResponse)
    def messages(request: Request):
        # Any visit to the messenger is the reload the progress bubble asked
        # for, so a finished send has nothing left to report.
        if state.live_progress().get("finished"):
            state.clear_progress()
        threads = []
        if state.store_path.exists() and state.unlocked:
            with state.store() as store:
                threads = store.conversations(state.identity.fingerprint)
        return render(request, "messages.html", threads=threads, thread=None,
                      peer=None, when=_when, fingerprint_of=fingerprint_of)

    @app.get("/messages/{peer_hex}", response_class=HTMLResponse)
    def conversation(request: Request, peer_hex: str):
        # A finished send has been reported; the message it produced is in the
        # thread below, so the progress bubble has nothing left to say. Cleared
        # on the reload it asked for, rather than lingering at 100% forever.
        finished = state.live_progress().get("finished")
        if finished and state.live_progress().get("peer") == peer_hex:
            state.clear_progress()

        # A send interrupted by a restart leaves a record on disk but no live
        # progress, so the bubble sat at 0% with nothing driving it. Surface it
        # as something that can be finished instead.
        unfinished = None
        if not state.live_progress() and state.store_path.exists():
            with state.store() as store:
                for record in store.pending_sends():
                    if record["recipient_key"].hex() == peer_hex:
                        unfinished = {
                            "sent": record["sent_count"],
                            "total": record["total"],
                            "msg_id": record["msg_id"].hex(),
                        }
                        break
        threads, items, peer = [], [], None
        if state.store_path.exists() and state.unlocked:
            try:
                peer_key = bytes.fromhex(peer_hex)
            except HTTPException:
                raise          # a rejected form is a 400, not an error page
            except ValueError:
                return RedirectResponse("/messages", status_code=303)
            with state.store() as store:
                threads = store.conversations(state.identity.fingerprint)
                items = _with_attachments(store,
                    store.thread(state.identity.fingerprint, peer_key,
                                 limit=PAGE_MESSAGES))
                store.mark_thread_read(state.identity.fingerprint, peer_key)
                peer = {
                    "pubkey": peer_key,
                    "hex": peer_hex,
                    "name": store.contact_name(peer_key),
                    "fingerprint": fingerprint_of(peer_key),
                    "code": contact.encode(state.messaging.network, peer_key),
                    "contact_id": (lambda r: r["id"] if r else None)(
                        store.contact_by_key(peer_key)),
                }
                for t in threads:
                    if t["pubkey"] == peer_key:
                        peer["address"] = t.get("address", "")
        return render(request, "messages.html", threads=threads, thread=items,
                      peer=peer, when=_when, fingerprint_of=fingerprint_of,
                      is_new_contact=bool(peer) and not items,
                      unfinished=unfinished,
                      profile_name=state.profile_name)

    @app.post("/messages/start")
    def start_conversation(request: Request, code: str = Form(""), name: str = Form(""),
                           csrf_token: str = Form("")):
        """Begin a conversation from an address or a contact code."""
        try:
            check_csrf(csrf_token)
            peer_key = _resolve_recipient(state, code)
            # When they gave an address, keep it: it is how the user will think
            # of this person, and it is what the address book wants.
            typed = code.strip()
            address = "" if typed.lower().startswith(f"{contact.PREFIX}:") else typed
            with state.store() as store:
                if name.strip() or address:
                    store.name_contact(peer_key, name.strip(), address)
            return RedirectResponse(f"/messages/{peer_key.hex()}", status_code=303)
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except Exception as exc:
            state.flash(str(exc), "err")
            return RedirectResponse("/messages", status_code=303)

    @app.post("/messages/{peer_hex}/name")
    def rename_contact(request: Request, peer_hex: str, name: str = Form(""),
                       csrf_token: str = Form("")):
        try:
            check_csrf(csrf_token)
            with state.store() as store:
                store.name_contact(bytes.fromhex(peer_hex), name.strip())
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except Exception as exc:
            state.flash(str(exc), "err")
        return RedirectResponse(f"/messages/{peer_hex}", status_code=303)

    @app.post("/messages/{peer_hex}/send", response_class=HTMLResponse)
    def send_in_thread(request: Request, peer_hex: str, body: str = Form(""),
                       confirmed: str = Form(""), csrf_token: str = Form(""),
                       share_profile: str = Form(""),
                       attachment: UploadFile | None = File(None),
                       attached_name: str = Form(""),
                       attached_type: str = Form(""),
                       attached_b64: str = Form("")):
        """Deliberately a sync route, not an async one.

        Everything in here blocks: RPC calls, waiting for a confirmation, and
        possibly splitting the wallet first. An `async def` runs on the event
        loop, so a send that takes minutes froze the whole interface -- no
        progress, no /events, a browser that appeared hung. That is what made a
        second click the obvious thing to do. FastAPI runs a sync route in its
        threadpool instead, so the rest of the application keeps answering while
        this works.
        """
        """Send within a conversation.

        Still two steps: the decoded transaction and its cost are shown before
        anything is broadcast. Messages cost real fees even on testnet, and the
        transaction is permanent either way.
        """
        prepared = None
        plan = None
        error = None
        file_bytes, file_name, file_type = b"", attached_name, attached_type
        try:
            check_csrf(csrf_token)
            if not state.unlocked:
                # Nothing for the user to do but wait for the node; there is no
                # passphrase to enter any more.
                state.ensure_identity()
            peer_key = bytes.fromhex(peer_hex)

            # The file survives the confirm step as base64 in a hidden field,
            # because the preview and the send are two separate requests and the
            # browser will not resend a file input on the second.
            file_bytes, file_name, file_type = b"", attached_name, attached_type
            if attachment is not None and attachment.filename:
                file_bytes = attachment.file.read()
                file_name = attachment.filename
                file_type = attachment.content_type or "application/octet-stream"
            elif attached_b64:
                file_bytes = base64.b64decode(attached_b64)

            if not body.strip() and not file_bytes:
                raise ValueError("write something, or choose a file to send")

            payload_body = content.build(
                text=body,
                attachment=content.Attachment(file_name, file_type, file_bytes)
                if file_bytes else None,
                profile=_profile_for_state(state, peer_key)
                if share_profile == "yes" else None,
            )
            plan = plan_message(state.identity, peer_key, payload_body)
            with state.messaging.rpc() as rpc:
                funding = Miner(rpc, state.messaging.params).status()
                if not funding.funded:
                    raise ValueError(f"cannot send: {funding.describe()}")
                sender = MessageSender(rpc, state.messaging.params)
                address = funded_address(rpc, prefer=state.derived_address)

                def _prepare_first(where: str):
                    """Build the first transaction, falling back if short.

                    The identity address is preferred so a message is attributed
                    to the address people were given. But preferring it is not
                    the same as being able to pay from it, and when it came up
                    short the send failed with advice a browser cannot act on:
                    "your coins are on a different address, use that one" -- with
                    no way to choose one. a test machine hit exactly that. So try the
                    preferred address, and if it cannot cover this message, use
                    the one that can.
                    """
                    try:
                        return where, sender.prepare(where, plan.chunk_payloads[0])
                    except SendError:
                        other = funded_address(rpc)
                        if other == where:
                            raise
                        return other, sender.prepare(other, plan.chunk_payloads[0])

                address, first = _prepare_first(address)
                # Only the first chunk is built for the preview. The rest cannot
                # be: each one spends the change of the one before it, so its
                # input does not exist until that one is broadcast. Building them
                # all up front -- which this did -- produced transactions that
                # spent the same output twice.
                prepared = [first]

                # A plain message on testnet goes straight out. There is nothing
                # to weigh up: the coins are free, it is one transaction, and it
                # is gone the instant it is broadcast either way -- a review step
                # only makes sending feel like filing paperwork.
                #
                # A file is different and keeps its confirmation: it costs dust
                # that can never be spent again, and a chunked send waits a block
                # between transactions, so it can take minutes. Both of those are
                # worth knowing BEFORE rather than discovering after.
                immediate = (not file_bytes
                             and plan.transactions == 1
                             and not state.messaging.is_mainnet)
                if immediate or confirmed == "yes":
                    # Only one send at a time. A long one can take minutes, the
                    # browser shows nothing while it waits, and a second click is
                    # then the natural thing to do -- but two sends select their
                    # outputs without seeing each other's claims, so they can
                    # collide and strand a half-written message on the chain.
                    digest = hashlib.sha256(
                        peer_key + body.encode() + file_bytes).hexdigest()
                    if state.is_repeat_send(digest):
                        raise ValueError(
                            "that exact message was just sent. If you meant to "
                            "send it twice, change something or wait a minute -- "
                            "a second click while a send is working is usually an "
                            "accident, and it costs the whole message again.")
                    if not state.begin_send():
                        raise ValueError(
                            "a message is already being sent. Wait for it to "
                            "finish: sending two at once can leave a half-written "
                            "message on the chain that nobody can read.")
                    own_copy = (body or f"[sent {file_name}]").encode()
                    if plan.transactions > 1:
                        # A long send runs on a thread and the browser goes back
                        # to the conversation to watch it. Holding the request
                        # open for minutes is what froze the interface, and a
                        # frozen interface is what made a second click look like
                        # the right thing to do.
                        # Estimated here, not borrowed from `timing` -- that is
                        # built further down for the rendered page and does not
                        # exist yet. Referencing it raised NameError inside the
                        # try, which surfaced as an unrelated error AND leaked
                        # the send lock, because only the background thread
                        # releases it.
                        try:
                            typical, slow = recent_block_seconds(rpc)
                            spare = sender.spendable_outputs(address)
                            quick, _ = estimate_send_seconds(
                                plan.transactions, typical, slow, spare)
                            estimate = describe_duration(quick)
                        except Exception:
                            estimate = "a few minutes"

                        state.start_progress(peer_hex, plan.transactions, estimate)
                        try:
                            _send_in_background(
                                sender, address, plan, peer_key, payload_body,
                                own_copy, digest,
                                attachment={"file_name": file_name,
                                            "file_type": file_type,
                                            "file_data": file_bytes or None}
                                if file_bytes else None)
                        except Exception:
                            # The thread never started, so nothing will release
                            # the claim on its behalf.
                            state.end_send()
                            state.clear_progress()
                            raise
                        return RedirectResponse(f"/messages/{peer_hex}",
                                                status_code=303)
                    try:
                        txids = sender.send_all(address, plan.chunk_payloads)
                        state.note_send(digest)
                    finally:
                        state.end_send()
                    # Keep our own plaintext: the sealed box is to the recipient,
                    # so we could never read this back off the chain ourselves.
                    with state.store() as store:
                        record_sent(store, txids[0], peer_key,
                                    state.identity.fingerprint, own_copy,
                                    file_name=file_name, file_type=file_type,
                                    file_data=file_bytes or None, txids=txids)
                    # No flash: the message itself appears in the conversation,
                    # marked unconfirmed until it is in a block. A green banner
                    # saying "Sent." on top of a bubble that says the same thing
                    # is one notification too many.
                    return RedirectResponse(f"/messages/{peer_hex}", status_code=303)
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except Exception as exc:
            error = str(exc)

        threads, items, peer = [], [], None
        if state.unlocked and state.store_path.exists():
            with state.store() as store:
                threads = store.conversations(state.identity.fingerprint)
                items = _with_attachments(store,
                    store.thread(state.identity.fingerprint, bytes.fromhex(peer_hex),
                                 limit=PAGE_MESSAGES))
                peer = {"pubkey": bytes.fromhex(peer_hex), "hex": peer_hex,
                        "name": store.contact_name(bytes.fromhex(peer_hex)),
                        "contact_id": (lambda r: r["id"] if r else None)(
                            store.contact_by_key(bytes.fromhex(peer_hex))),
                        "fingerprint": fingerprint_of(bytes.fromhex(peer_hex)),
                        "code": contact.encode(state.messaging.network, bytes.fromhex(peer_hex))}
        # What the confirmation needs to say: how long, and why.
        #
        # This was gated on `file_bytes`, so it was built for an attachment and
        # never for a text message -- however many transactions that message
        # needed. a test machine's two captures were both text-only three-transaction
        # sends and neither could show the timing note, which is the exact case
        # its block-packing measurement was about. A long text message chunks
        # for the same reason a file does and waits the same way.
        timing = None
        if plan is not None and plan.transactions > 1:
            independent = 0
            try:
                with state.messaging.rpc() as rpc:
                    typical, slow = recent_block_seconds(rpc)
                    # Confirmed outputs on the sending address: each one lets a
                    # chunk fund itself instead of waiting for the previous.
                    independent = MessageSender(
                        rpc, state.messaging.params).spendable_outputs(
                            state.derived_address or "")
            except Exception:
                typical, slow = 60.0, 180.0
            # If there are not enough outputs the send splits the wallet first:
            # one wait for the split to confirm, then everything at once. So the
            # honest estimate is one block, not one per transaction.
            will_split = plan.transactions > 1 and independent < plan.transactions
            effective = plan.transactions if will_split else independent
            quick, patient = estimate_send_seconds(plan.transactions, typical, slow,
                                                   effective)
            if will_split:
                quick, patient = int(typical), int(slow)
            timing = {
                "transactions": plan.transactions,
                "typical": describe_duration(quick),
                "slow": describe_duration(patient),
                "waits": quick > 0,
                "independent": independent,
                "will_split": will_split,
                # Broadcast and readable are different questions and the screen
                # only answered the first. A split send is broadcast in seconds
                # and still takes a block per chunk to confirm -- measured on
                # both chains; see estimate_readable_seconds for the
                # transactions it rests on.
                "readable": describe_duration(
                    estimate_readable_seconds(plan.transactions, typical)),
            }
        return render(request, "messages.html", threads=threads, thread=items, peer=peer,
                      when=_when, fingerprint_of=fingerprint_of, prepared=prepared,
                      plan=plan, draft=body, error=error, timing=timing,
                      cost=(send_cost(prepared[0], plan.transactions)
                            if prepared and plan else None),
                      is_new_contact=not items, profile_name=state.profile_name,
                      attached_b64=base64.b64encode(file_bytes).decode() if file_bytes else "",
                      attached_name=file_name, attached_type=file_type,
                      share_profile=share_profile)

    # --- inbox ----------------------------------------------------------------

    @app.get("/inbox", response_class=HTMLResponse)
    def inbox(request: Request):
        messages = []
        if state.store_path.exists() and state.unlocked:
            with state.store() as store:
                messages = store.inbox(recipient_fp=state.identity.fingerprint, limit=200)
        return render(request, "inbox.html", messages=messages, when=_when)

    @app.get("/message/{message_id}", response_class=HTMLResponse)
    def read_message(request: Request, message_id: int):
        message = None
        body_text = None
        if state.store_path.exists():
            with state.store() as store:
                message = store.get_message(message_id)
                if message:
                    store.mark_read(message_id)
        if message:
            try:
                body_text = message.body.decode("utf-8")
            except HTTPException:
                raise          # a rejected form is a 400, not an error page
            except UnicodeDecodeError:
                body_text = None
        return render(request, "message.html", message=message, body_text=body_text,
                      when=_when, fingerprint_of=fingerprint_of)

    # --- compose --------------------------------------------------------------

    @app.get("/compose", response_class=HTMLResponse)
    def compose_form(request: Request):
        keys = []
        if state.store_path.exists():
            with state.store() as store:
                keys = store.all_keys()
        return render(request, "compose.html", keys=keys, plan=None, prepared=None)

    @app.post("/compose", response_class=HTMLResponse)
    def compose_preview(request: Request, recipient: str = Form(""), body: str = Form(""),
                        csrf_token: str = Form("")):
        """Estimate cost. Deliberately does NOT build or broadcast anything."""
        keys = []
        plan = None
        error = None
        try:
            check_csrf(csrf_token)
            if not state.unlocked:
                # Nothing for the user to do but wait for the node; there is no
                # passphrase to enter any more.
                state.ensure_identity()
            if not body:
                raise ValueError("nothing to send")
            with state.store() as store:
                keys = store.all_keys()
            recipient_key = _resolve_recipient(state, recipient)
            plan = plan_message(state.identity, recipient_key, body.encode())
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except Exception as exc:
            error = str(exc)
        return render(request, "compose.html", keys=keys, plan=plan, error=error,
                      recipient=recipient, body=body, prepared=None)

    @app.post("/send", response_class=HTMLResponse)
    def send(request: Request, recipient: str = Form(""), body: str = Form(""),
             csrf_token: str = Form(""), confirmed: str = Form("")):
        """Build, fund and sign -- then show the decoded transaction.

        Broadcast happens only on a second, explicit submission. The rule that
        nothing goes out without the decoded transaction and fee being shown
        first is enforced here, not left to the caller.
        """
        error = None
        prepared = None
        broadcast_txids = None
        try:
            check_csrf(csrf_token)
            if not state.unlocked:
                # Nothing for the user to do but wait for the node; there is no
                # passphrase to enter any more.
                state.ensure_identity()
            recipient_key = _resolve_recipient(state, recipient)
            plan = plan_message(state.identity, recipient_key, body.encode())
            with state.messaging.rpc() as rpc:
                funding = Miner(rpc, state.messaging.params).status()
                if not funding.funded:
                    raise ValueError(f"cannot send: {funding.describe()}")
                sender = MessageSender(rpc, state.messaging.params)
                address = funded_address(rpc, prefer=state.derived_address)

                def _prepare_first(where: str):
                    """Build the first transaction, falling back if short.

                    The identity address is preferred so a message is attributed
                    to the address people were given. But preferring it is not
                    the same as being able to pay from it, and when it came up
                    short the send failed with advice a browser cannot act on:
                    "your coins are on a different address, use that one" -- with
                    no way to choose one. a test machine hit exactly that. So try the
                    preferred address, and if it cannot cover this message, use
                    the one that can.
                    """
                    try:
                        return where, sender.prepare(where, plan.chunk_payloads[0])
                    except SendError:
                        other = funded_address(rpc)
                        if other == where:
                            raise
                        return other, sender.prepare(other, plan.chunk_payloads[0])

                address, first = _prepare_first(address)
                prepared = [sender.prepare(address, p) for p in plan.chunk_payloads]
                if confirmed == "yes":
                    broadcast_txids = [sender.broadcast(p) for p in prepared]
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except Exception as exc:
            error = str(exc)

        keys = []
        if state.store_path.exists():
            with state.store() as store:
                keys = store.all_keys()
        return render(request, "compose.html", keys=keys, plan=None, error=error,
                      recipient=recipient, body=body, prepared=prepared,
                      broadcast=broadcast_txids)

    # --- keys -----------------------------------------------------------------

    # --- address book ---------------------------------------------------------
    # Purely local. Nothing here is published, and nothing here is derivable from
    # the chain by anyone else: it is the user's own note of who is who. It works
    # while the identity is locked, because it holds no secrets.

    @app.get("/contacts", response_class=HTMLResponse)
    def contacts_page(request: Request, edit: int | None = None):
        people, editing = [], None
        if state.store_path.exists():
            with state.store() as store:
                people = [_contact_view(row) for row in store.contacts()]
                if edit:
                    row = store.contact_by_id(edit)
                    editing = _contact_view(row) if row else None
        published = []
        if state.store_path.exists():
            with state.store() as store:
                published = []
                # Never offer to add yourself: your own announcement is yours,
                # and it appeared in the list of people to meet.
                rows = store.unknown_published_keys(
                    exclude=state.identity.public_bytes if state.unlocked else None)
                stated_keys = {bytes(r["pubkey"]) for r in rows if r["stated"]}
                for row in rows:
                    key = bytes(row["pubkey"])
                    if not row["stated"] and key in stated_keys:
                        continue      # an inferred address the key has replaced
                    published.append({
                        "address": row["address"],
                        "hex": key.hex(),
                        "height": row["height"],
                        "when": row["block_time"],
                        # The name as published, read from the announcement
                        # itself rather than from a contact row. Looking it up in
                        # the address book could only ever find names for people
                        # already in it, which is precisely who this list leaves
                        # out.
                        "name": row["name"] or "",
                    })
        return render(request, "contacts.html", people=people, editing=editing,
                      published=published, when=_when,
                      announce_limit=MAX_ANNOUNCE_NAME,
                      name_limit=MAX_ANNOUNCE_NAME_CLASS_B)

    @app.post("/contacts/save")
    def save_contact(request: Request, name: str = Form(""),
                     testnet_address: str = Form(""), mainnet_address: str = Form(""),
                     notes: str = Form(""), code: str = Form(""),
                     contact_id: str = Form(""), csrf_token: str = Form("")):
        try:
            check_csrf(csrf_token)
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except ValueError as exc:
            state.flash(str(exc), "err")
            return RedirectResponse("/contacts", status_code=303)
        name = name.strip()
        testnet_address = testnet_address.strip()
        mainnet_address = mainnet_address.strip()
        if not name:
            state.flash("Give the contact a name.", "err")
            return RedirectResponse("/contacts", status_code=303)

        # Reject an address that belongs to the wrong chain now, rather than
        # letting it sit in the book until someone pays it.
        for label, value, want_mainnet in (
            ("Testnet", testnet_address, False), ("Mainnet", mainnet_address, True)
        ):
            if value:
                problem = _check_address(value, mainnet=want_mainnet)
                if problem:
                    state.flash(f"{label} address: {problem}", "err")
                    return RedirectResponse("/contacts", status_code=303)

        pubkey = None
        if code.strip():
            try:
                network, pubkey = contact.decode(code.strip())
            except HTTPException:
                raise          # a rejected form is a 400, not an error page
            except Exception as exc:
                state.flash(f"Contact code: {exc}", "err")
                return RedirectResponse("/contacts", status_code=303)
            if network != state.messaging.network:
                state.flash(
                    f"That contact code is for {network}, but the Messenger runs "
                    f"on {state.messaging.network}.", "err")
                return RedirectResponse("/contacts", status_code=303)

        with state.store() as store:
            store.save_contact(
                contact_id=int(contact_id) if contact_id.strip().isdigit() else None,
                pubkey=pubkey, name=name, testnet_address=testnet_address,
                mainnet_address=mainnet_address, notes=notes.strip())
        state.flash(f"Saved {name}.", "ok")
        return RedirectResponse("/contacts", status_code=303)

    @app.post("/contacts/{contact_id}/delete")
    def delete_contact(request: Request, contact_id: int, csrf_token: str = Form("")):
        try:
            check_csrf(csrf_token)
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except ValueError as exc:
            state.flash(str(exc), "err")
            return RedirectResponse("/contacts", status_code=303)
        with state.store() as store:
            row = store.contact_by_id(contact_id)
            store.delete_contact(contact_id)
        state.flash(f"Removed {row['name'] if row else 'the contact'}.", "ok")
        return RedirectResponse("/contacts", status_code=303)

    # --- backup and restore ---------------------------------------------------
    # The whole security model in one page: the wallet is the only thing a user
    # has to keep, so backing it up, printing it, and putting it back have to be
    # things they can actually do. Both chains, because both hold something.

    #: Running key imports, per chain. Imports rescan the chain, which blocks the
    #: node's RPC for minutes, so they run on a thread and are reported here.
    imports: dict[str, Any] = {}

    def _chain_for(which: str):
        chain = state.ledger if which == "ledger" else state.messaging
        if which not in ("ledger", "messaging"):
            raise ValueError("unknown chain")
        return chain

    def _chain_cards() -> list[dict[str, Any]]:
        cards = []
        for which, chain in (("messaging", state.messaging), ("ledger", state.ledger)):
            card: dict[str, Any] = {
                "which": which, "label": chain.label, "network": chain.network,
                "is_mainnet": chain.is_mainnet, "online": False, "wallet": None,
                "wallet_file": None, "job": imports.get(which),
                "wallets": [], "can_switch": False,
            }
            try:
                with chain.rpc() as rpc:
                    card["wallet"] = backup.wallet_summary(rpc)
                    card["online"] = True
                if chain.datadir:
                    path = backup.wallet_path(chain.datadir, chain.network)
                    card["wallet_file"] = str(path)
                    card["wallet_readable"] = os.access(path, os.R_OK)
                    card["wallets"] = backup.list_wallets(chain.datadir, chain.network)
                    card["can_switch"] = os.access(path.parent, os.W_OK)
            except HTTPException:
                raise          # a rejected form is a 400, not an error page
            except Exception as exc:
                card["problem"] = str(exc)
            cards.append(card)
        return cards

    @app.get("/backup", response_class=HTMLResponse)
    def backup_page(request: Request):
        return render(request, "backup.html", chains=_chain_cards(),
                      default_dir=str(backup.default_backup_dir()))

    @app.post("/backup/{which}/save")
    def backup_save(request: Request, which: str, folder: str = Form(""),
                    csrf_token: str = Form("")):
        try:
            check_csrf(csrf_token)
            chain = _chain_for(which)
            target = Path(folder.strip()).expanduser() if folder.strip() \
                else backup.default_backup_dir()
            with chain.rpc() as rpc:
                written = backup.backup_wallet(rpc, target, datadir=chain.datadir,
                                               network=chain.network)
            state.flash(f"Saved a copy of the {chain.label.lower()} wallet to {written}. "
                        f"Keep it somewhere other than this computer.", "ok")
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except (backup.BackupError, ValueError) as exc:
            state.flash(str(exc), "err")
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except Exception as exc:
            state.flash(f"Could not back up: {exc}", "err")
        return RedirectResponse("/backup", status_code=303)

    @app.post("/backup/{which}/print", response_class=HTMLResponse)
    def backup_print(request: Request, which: str, understand: str = Form(""),
                     csrf_token: str = Form("")):
        """Render every private key once, for printing. Never written to disk."""
        try:
            check_csrf(csrf_token)
            if understand != "yes":
                raise ValueError("tick the box first -- these keys spend your coins")
            chain = _chain_for(which)
            with chain.rpc() as rpc:
                keys = backup.private_keys(rpc)
            return render(request, "printkeys.html", keys=keys, chain=chain,
                          when=_when)
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except (backup.BackupError, ValueError) as exc:
            state.flash(str(exc), "err")
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except Exception as exc:
            state.flash(f"Could not read the keys: {exc}", "err")
        return RedirectResponse("/backup", status_code=303)

    @app.post("/backup/{which}/import-key")
    def backup_import_key(request: Request, which: str, key: str = Form(""),
                          label: str = Form("imported"), csrf_token: str = Form("")):
        try:
            check_csrf(csrf_token)
            chain = _chain_for(which)
            running = imports.get(which)
            if running and not running.done:
                raise ValueError("an import is already running on this chain -- "
                                 "wait for it to finish")
            imports[which] = backup.import_private_key(
                chain.rpc, key, label.strip() or "imported")
            state.flash(
                "Importing. The node has to re-read the chain to find this key's "
                "coins, which takes a few minutes and makes it unresponsive in the "
                "meantime. This page will say when it is done.", "ok")
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except (backup.BackupError, ValueError) as exc:
            state.flash(str(exc), "err")
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except Exception as exc:
            state.flash(f"Could not import: {exc}", "err")
        return RedirectResponse("/backup", status_code=303)

    @app.post("/backup/{which}/restore")
    def backup_restore(request: Request, which: str, source: str = Form(""),
                       understand: str = Form(""), csrf_token: str = Form("")):
        try:
            check_csrf(csrf_token)
            if understand != "yes":
                raise ValueError(
                    "tick the box to confirm you want to replace the wallet that "
                    "is in use")
            chain = _chain_for(which)
            if not chain.datadir:
                raise ValueError(
                    "this application does not know where that node keeps its "
                    "files, so it cannot put a wallet there")
            result = backup.restore_wallet(chain.rpc, Path(source.strip()),
                                           chain.datadir, chain.network)
            came_back = backup.wait_for_node(chain.rpc, timeout=180)
            state.lock()      # the identity belongs to the old wallet
            previous = result["previous_saved_to"]
            state.flash(
                f"Restored. "
                + (f"The wallet that was there is saved as {previous}. " if previous else "")
                + ("The node has restarted and is using it now."
                   if came_back else
                   "The node is still starting -- give it a minute."), "ok")
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except (backup.BackupError, ValueError) as exc:
            state.flash(str(exc), "err")
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except Exception as exc:
            state.flash(f"Could not restore: {exc}", "err")
        return RedirectResponse("/backup", status_code=303)

    # --- several wallets ------------------------------------------------------
    # One wallet file is in place at a time; the rest wait in the library. Every
    # switch stops the node, so every one of these restarts it.

    @app.post("/backup/{which}/wallet/new")
    def wallet_new(request: Request, which: str, name: str = Form(""),
                   csrf_token: str = Form("")):
        try:
            check_csrf(csrf_token)
            chain = _chain_for(which)
            if not chain.datadir:
                raise ValueError("this application does not know where that node "
                                 "keeps its files")
            result = backup.create_wallet(chain.rpc, chain.datadir, chain.network,
                                          name)
            came_back = backup.wait_for_node(chain.rpc, timeout=180)
            state.lock()
            state.flash(
                f"Now using a new empty wallet called {result['created']}. Your "
                f"previous wallet is kept and can be switched back to at any time. "
                + ("The node has restarted." if came_back
                   else "The node is still starting -- give it a minute."), "ok")
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except (backup.BackupError, ValueError) as exc:
            state.flash(str(exc), "err")
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except Exception as exc:
            state.flash(f"Could not create a wallet: {exc}", "err")
        return RedirectResponse("/backup", status_code=303)

    @app.post("/backup/{which}/wallet/switch")
    def wallet_switch(request: Request, which: str, name: str = Form(""),
                      csrf_token: str = Form("")):
        try:
            check_csrf(csrf_token)
            chain = _chain_for(which)
            if not chain.datadir:
                raise ValueError("this application does not know where that node "
                                 "keeps its files")
            result = backup.switch_wallet(chain.rpc, chain.datadir, chain.network,
                                          name)
            came_back = backup.wait_for_node(chain.rpc, timeout=180)
            # A different wallet is a different identity. Holding on to the old
            # one would mean reading and writing as somebody this wallet is not.
            state.lock()
            state.flash(
                f"Now using {result['now_using']}. "
                + ("The node has restarted." if came_back
                   else "The node is still starting -- give it a minute."), "ok")
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except (backup.BackupError, ValueError) as exc:
            state.flash(str(exc), "err")
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except Exception as exc:
            state.flash(f"Could not switch wallets: {exc}", "err")
        return RedirectResponse("/backup", status_code=303)

    @app.post("/backup/{which}/wallet/remove")
    def wallet_remove(request: Request, which: str, name: str = Form(""),
                      csrf_token: str = Form("")):
        try:
            check_csrf(csrf_token)
            chain = _chain_for(which)
            if not chain.datadir:
                raise ValueError("this application does not know where that node "
                                 "keeps its files")
            result = backup.remove_wallet(chain.datadir, chain.network, name)
            state.flash(
                f"Removed {result['removed']} from the list. The file itself was "
                f"moved to {result['moved_to']}, not deleted -- a wallet can hold "
                f"coins nothing else records.", "ok")
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except (backup.BackupError, ValueError) as exc:
            state.flash(str(exc), "err")
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except Exception as exc:
            state.flash(f"Could not remove that wallet: {exc}", "err")
        return RedirectResponse("/backup", status_code=303)

    @app.get("/messages/attachment/{message_id}")
    def download_attachment(request: Request, message_id: int):
        """Hand back a file somebody sent. Never rendered inline.

        `Content-Disposition: attachment` with a fixed octet-stream type, so a
        file named by a stranger cannot be served back as HTML or script into
        this origin -- which is where the wallet lives.
        """
        with state.store() as store:
            row = store.attachment_for(message_id)
        if row is None:
            state.flash("That file is not here.", "err")
            return RedirectResponse("/messages", status_code=303)
        name = _safe_filename(row["name"])
        return Response(
            content=bytes(row["data"]),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{name}"',
                     "X-Content-Type-Options": "nosniff"},
        )

    @app.get("/messages/media/{message_id}")
    def render_attachment(request: Request, message_id: int):
        """Serve an attachment for inline display -- images, audio, video only.

        The type comes from the file's own bytes, never from what the sender
        claimed, and anything not on the allow-list is refused here and offered
        as a download instead. Refusing to render is not refusing to deliver.
        """
        with state.store() as store:
            row = store.attachment_for(message_id)
        if row is None:
            return Response(status_code=404)
        data = bytes(row["data"])
        kind = media.renderable(data)
        if kind is None:
            # Not recognised, or too large to inline. Never guess.
            return RedirectResponse(f"/messages/attachment/{message_id}",
                                    status_code=303)
        return Response(
            content=data, media_type=kind.mime,
            headers={
                "Content-Disposition": "inline",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": media.MEDIA_CSP,
                "Cache-Control": "private, max-age=300",
            },
        )

    @app.post("/messages/{peer_hex}/resume")
    def resume_send(request: Request, peer_hex: str, csrf_token: str = Form("")):
        """Finish a send that stopped part way.

        The sealed chunks were written down before the first broadcast, so the
        rest can go out unchanged under the same message id. Re-sealing would
        produce a different id and strand what is already on the chain.
        """
        try:
            check_csrf(csrf_token)
            record = None
            with state.store() as store:
                for candidate in store.pending_sends():
                    if candidate["recipient_key"].hex() == peer_hex:
                        record = candidate
                        break
            if record is None:
                raise ValueError("there is nothing left to finish.")
            if not state.begin_send():
                raise ValueError("a message is already being sent.")

            remaining = record["chunks"][record["sent_count"]:]
            plan = type("Resume", (), {
                "transactions": len(remaining),
                "chunk_payloads": remaining,
                "msg_id": record["msg_id"],
            })()
            state.start_progress(peer_hex, len(remaining), "a few minutes")
            # The record holds the encoded body; the copy we keep for ourselves
            # is what it decodes to, exactly as a send that never stopped keeps.
            own_copy, attachment = content.own_copy(record["body"])
            with state.messaging.rpc() as rpc:
                sender = MessageSender(rpc, state.messaging.params)
                try:
                    _send_in_background(sender, record["sender_address"], plan,
                                        record["recipient_key"], record["body"],
                                        own_copy, "", attachment=attachment or None)
                except Exception:
                    state.end_send()
                    state.clear_progress()
                    raise
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except Exception as exc:
            state.flash(str(exc), "err")
        return RedirectResponse(f"/messages/{peer_hex}", status_code=303)

    def _post_timing(chain, plan):
        """How long before a chunked post can be read, or None if not chunked.

        One definition called from both render paths. It was pasted into both,
        and the GET route has no `plan` -- so it raised NameError on every
        listing of the public board. Two copies of a thing that reads local
        state is the same mistake that let the CLI and the web disagree about
        recording a send.
        """
        if plan is None or plan.transactions <= 1:
            return None
        try:
            with chain.rpc() as rpc:
                typical, _slow = recent_block_seconds(rpc)
        except Exception:
            typical = 60.0
        return {"readable": describe_duration(
            estimate_readable_seconds(plan.transactions, typical))}

    @app.post("/groups/resume")
    def resume_post(request: Request, which: str = Form("messaging"),
                    channel: str = Form(""), csrf_token: str = Form("")):
        """Finish a public post that stopped part way.

        The public path had no record at all, so there was nothing to finish:
        an interrupted post spent the outputs it had already broadcast and left
        no trace. The payloads are written down before the first broadcast, so
        the rest go out unchanged under the same msg_id -- rebuilding them would
        produce a different id and strand what is already on the chain, which is
        the same reason the private path stores them rather than the text.
        """
        chain = state.ledger if which == "ledger" else state.messaging
        try:
            check_csrf(csrf_token)
            record = None
            with state.store() as store:
                for candidate in store.pending_posts(chain.network):
                    if not channel or candidate["channel"] == channel:
                        record = candidate
                        break
            if record is None:
                raise ValueError("there is nothing left to finish.")
            if not state.begin_send():
                raise ValueError("a message is already being sent.")

            remaining = record["chunks"][record["sent_count"]:]
            plan = type("ResumePost", (), {
                "transactions": len(remaining),
                "payloads": remaining,
                "msg_id": record["msg_id"],
            })()
            local = dict(network=record["network"], channel=record["channel"],
                         address=record["sender_address"],
                         nickname=record["nickname"],
                         text=record["body"].decode("utf-8", "replace"),
                         file_name="", file_type="", file_data=None)
            state.start_progress(f"#{record['channel']}", len(remaining),
                                 "a few minutes")
            with chain.rpc() as rpc:
                sender = MessageSender(rpc, chain.params, public_only=True)
                try:
                    _post_in_background(sender, record["sender_address"], plan,
                                        local)
                except Exception:
                    state.end_send()
                    state.clear_progress()
                    raise
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except Exception as exc:
            state.flash(str(exc), "err")
        return RedirectResponse(
            f"/groups?which={which}&channel={channel or 'main'}", status_code=303)

    @app.get("/messages/sent-media/{sent_id}")
    def sent_media(request: Request, sent_id: int, download: int = 0):
        """A file we sent. Same rules as one we received."""
        with state.store() as store:
            row = store.sent_file(sent_id)
        if row is None or row["file_data"] is None:
            return Response(status_code=404)
        data = bytes(row["file_data"])
        name = _safe_filename(row["file_name"])
        kind = None if download else media.renderable(data)
        if kind is None:
            return Response(
                content=data, media_type="application/octet-stream",
                headers={"Content-Disposition": f'attachment; filename="{name}"',
                         "X-Content-Type-Options": "nosniff"})
        return Response(
            content=data, media_type=kind.mime,
            headers={"Content-Disposition": "inline",
                     "X-Content-Type-Options": "nosniff",
                     "Content-Security-Policy": media.MEDIA_CSP,
                     "Cache-Control": "private, max-age=300"})

    @app.post("/profile")
    def set_profile(request: Request, name: str = Form(""), csrf_token: str = Form("")):
        try:
            check_csrf(csrf_token)
            state.set_profile_name(name)
            state.flash(
                f"New contacts will see you as {name.strip()}." if name.strip()
                else "Your name will no longer be sent to new contacts.", "ok")
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except ValueError as exc:
            state.flash(str(exc), "err")
        return RedirectResponse("/contacts", status_code=303)

    @app.get("/events")
    def events(request: Request):
        """What an open page polls to decide whether to refresh.

        Deliberately tiny and does no RPC of its own: the watcher thread already
        knows the answer, so a page left open overnight costs the node nothing.
        """
        return JSONResponse({
            "generation": state.generation,
            "tips": state.tips,
            "checked": state.last_checked,
            "sending": state.live_progress() or None,
        })

    # --- public group posts ---------------------------------------------------
    # The one part of this application that is NOT encrypted, and the interface
    # says so on every screen where a post can be written. Runs on both chains:
    # D-010 keeps the *Messenger* on testnet permanently, but a public post
    # carries no key material and reveals nothing that publishing it does not
    # already reveal, so mainnet is a cost decision rather than a safety one.

    @app.get("/groups", response_class=HTMLResponse)
    def groups(request: Request, which: str = "messaging", channel: str = "",
               before: int | None = None):
        chain = state.ledger if which == "ledger" else state.messaging
        channel = (channel or group.DEFAULT_CHANNEL).strip() or group.DEFAULT_CHANNEL
        posts, channels, balance = [], [], None
        older = False
        # An interrupted post leaves a record but no progress, so the bubble
        # would sit at 0% with nothing driving it. Offer it as something that
        # can be finished instead -- exactly as the messenger does.
        unfinished = None
        if not state.live_progress() and state.store_path.exists():
            with state.store() as store:
                for record in store.pending_posts(chain.network):
                    if record["channel"] == channel:
                        unfinished = {"sent": record["sent_count"],
                                      "total": record["total"],
                                      "channel": record["channel"]}
                        break
        if state.store_path.exists():
            with state.store() as store:
                posts = _with_media(store, store.group_posts(
                    chain.network, channel, limit=PAGE_POSTS, before_id=before))
                channels = store.group_channels(chain.network)
                older = (store.group_has_older(chain.network, channel, posts[0]["id"])
                         if posts else False)
        try:
            with chain.rpc() as rpc:
                balance = float(rpc.call("getbalance") or 0)
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except Exception:
            balance = None
        return render(request, "groups.html", which=which, chain=chain,
                      older=older, before=before,
                      channel=channel, posts=posts, channels=channels,
                      balance=balance, when=_when,
                      room=group.max_text_bytes(channel, state.profile_name),
                      nickname=state.profile_name, unfinished=unfinished,
                      timing=_post_timing(chain, None), cost=None)

    @app.get("/groups/media/{post_id}")
    def group_media(request: Request, post_id: int, download: int = 0):
        """A file attached to a public post. Same rules as a private one.

        The type is decided from the bytes, never from what the poster claimed,
        and anything unrecognised is sent as a download rather than rendered.
        """
        with state.store() as store:
            row = store.group_post_file(post_id)
        if row is None or row["file_data"] is None:
            return Response(status_code=404)
        data = bytes(row["file_data"])
        name = _safe_filename(row["file_name"])
        kind = None if download else media.renderable(data)
        if kind is None:
            return Response(
                content=data, media_type="application/octet-stream",
                headers={"Content-Disposition": f'attachment; filename="{name}"',
                         "X-Content-Type-Options": "nosniff"})
        return Response(
            content=data, media_type=kind.mime,
            headers={"Content-Disposition": "inline",
                     "X-Content-Type-Options": "nosniff",
                     "Content-Security-Policy": media.MEDIA_CSP,
                     "Cache-Control": "private, max-age=300"})

    @app.post("/groups/post", response_class=HTMLResponse)
    def group_post_send(request: Request, which: str = Form("messaging"),
                        channel: str = Form(""), text: str = Form(""),
                        confirmed: str = Form(""), csrf_token: str = Form(""),
                        attachment: UploadFile | None = File(None),
                        attached_name: str = Form(""),
                        attached_type: str = Form(""),
                        attached_b64: str = Form("")):
        """Sync for the same reason as `send_in_thread`: it blocks."""
        # After posting, the newest page is the one to show: the thing you just
        # posted is at the end of it.
        before = None
        chain = state.ledger if which == "ledger" else state.messaging
        channel = (channel or group.DEFAULT_CHANNEL).strip() or group.DEFAULT_CHANNEL
        prepared, error, plan = None, None, None
        file_bytes, file_name, file_type = b"", attached_name, attached_type
        try:
            check_csrf(csrf_token)
            if attachment is not None and attachment.filename:
                file_bytes = attachment.file.read()
                file_name = attachment.filename
                file_type = attachment.content_type or "application/octet-stream"
            elif attached_b64:
                file_bytes = base64.b64decode(attached_b64)

            plan = group.plan(group.GroupPost(
                channel=channel, nickname=state.profile_name, text=text,
                file_name=file_name, file_type=file_type, file_data=file_bytes))
            with chain.rpc() as rpc:
                # public_only=True is the narrow exception to D-010. Nothing
                # encrypted passes it; see MessageSender.__init__.
                sender = MessageSender(rpc, chain.params, public_only=True)
                address = funded_address(rpc, mainnet=chain.is_mainnet)
                # Only the first transaction is built for the preview: a chunked
                # post chains through change outputs, so the next one's input
                # does not exist until this one is broadcast.
                prepared = sender.prepare(address, plan.payloads[0],
                                          class_c=plan.class_c,
                                          change_address=address)
                if confirmed == "yes":
                    local = dict(
                        network=chain.network, channel=channel, address=address,
                        nickname=state.profile_name, text=text,
                        file_name=group._safe_name(file_name) if file_bytes else "",
                        file_type=file_type if file_bytes else "",
                        file_data=file_bytes or None)

                    if plan.transactions == 1:
                        txids = [sender.broadcast(prepared)]
                        with state.store() as store:
                            _record_post(store, txids[0], local, txids)
                        state.flash(
                            f"Posted to #{channel} in 1 transaction. "
                            f"It is public and permanent.", "ok")
                        return RedirectResponse(
                            f"/groups?which={which}&channel={channel}",
                            status_code=303)

                    # A chunked post used to run inside this request, with no
                    # record, no progress and nothing reported when it finished.
                    # a test machine measured two chunks at about 90 seconds and closed the
                    # browser part way through: the post completed anyway, which
                    # was luck rather than design, and nothing told anyone. A
                    # 30 KB post would hold the request for six or seven minutes.
                    # Same three fixes the private path already had.
                    if not state.begin_send():
                        raise ValueError(
                            "a message is already being sent. Wait for it to "
                            "finish: two sends choose their outputs without "
                            "seeing each other's claims and can collide.")
                    try:
                        typical, slow = recent_block_seconds(rpc)
                        spare = sender.spendable_outputs(address)
                        quick, _ = estimate_send_seconds(
                            plan.transactions, typical, slow, spare)
                        estimate = describe_duration(quick)
                    except Exception:
                        estimate = "a few minutes"
                    state.start_progress(f"#{channel}", plan.transactions,
                                         estimate)
                    try:
                        _post_in_background(sender, address, plan, local)
                    except Exception:
                        # The thread never started, so nothing will release the
                        # claim on its behalf.
                        state.end_send()
                        state.clear_progress()
                        raise
                    return RedirectResponse(
                        f"/groups?which={which}&channel={channel}",
                        status_code=303)
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except Exception as exc:
            error = str(exc)

        posts, channels, balance = [], [], None
        older = False
        # An interrupted post leaves a record but no progress, so the bubble
        # would sit at 0% with nothing driving it. Offer it as something that
        # can be finished instead -- exactly as the messenger does.
        unfinished = None
        if not state.live_progress() and state.store_path.exists():
            with state.store() as store:
                for record in store.pending_posts(chain.network):
                    if record["channel"] == channel:
                        unfinished = {"sent": record["sent_count"],
                                      "total": record["total"],
                                      "channel": record["channel"]}
                        break
        if state.store_path.exists():
            with state.store() as store:
                posts = _with_media(store, store.group_posts(
                    chain.network, channel, limit=PAGE_POSTS, before_id=before))
                channels = store.group_channels(chain.network)
                older = (store.group_has_older(chain.network, channel, posts[0]["id"])
                         if posts else False)
        try:
            with chain.rpc() as rpc:
                balance = float(rpc.call("getbalance") or 0)
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except Exception:
            balance = None
        return render(request, "groups.html", which=which, chain=chain,
                      older=older, before=before,
                      channel=channel, posts=posts, channels=channels,
                      balance=balance, when=_when, prepared=prepared, error=error,
                      plan=plan, draft=text, room=group.max_text_bytes(channel, state.profile_name),
                      nickname=state.profile_name,
                      attached_b64=base64.b64encode(file_bytes).decode() if file_bytes else "",
                      attached_name=file_name, attached_type=file_type,
                      unfinished=unfinished,
                      timing=_post_timing(chain, plan),
                      cost=(send_cost(prepared, plan.transactions)
                            if prepared and plan else None))

    @app.post("/contacts/scan")
    def contacts_scan(request: Request, csrf_token: str = Form("")):
        """Look for key announcements on the chain, from the address book.

        The same scan the Overview runs. Offered here because this is where
        somebody is actually trying to find a person, and sending them elsewhere
        to press a differently named button was a poor answer to "who is out
        there?".
        """
        try:
            check_csrf(csrf_token)
            with state.messaging.rpc() as rpc, state.store() as store:
                scanner = Scanner(rpc, state.messaging.params, store,
                                  identity=state.identity)
                result = scanner.scan(max_blocks=5000)
                found = len(store.unknown_published_keys())
            state.flash(
                f"Scanned {result}. "
                + (f"{found} published address{'' if found == 1 else 'es'} not in "
                   f"your address book." if found else
                   "Nothing new to add."), "ok")
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except Exception as exc:
            state.flash(f"Scan failed: {exc}", "err")
        return RedirectResponse("/contacts", status_code=303)

    @app.post("/contacts/add-published")
    def add_published(request: Request, pubkey: str = Form(""),
                      address: str = Form(""), name: str = Form(""),
                      csrf_token: str = Form("")):
        """Add somebody found on the chain to the address book."""
        try:
            check_csrf(csrf_token)
            raw = bytes.fromhex(pubkey.strip())
            if len(raw) != 32:
                raise ValueError("that is not a 32-byte key")
            with state.store() as store:
                store.save_contact(pubkey=raw, name=name.strip(),
                                   testnet_address=address.strip())
            state.flash(
                f"Added {name.strip() or address.strip()}. An announcement proves "
                f"control of that address, never who somebody is &mdash; confirm "
                f"it with them before trusting it.", "ok")
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except (ValueError, TypeError) as exc:
            state.flash(str(exc), "err")
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except Exception as exc:
            state.flash(f"Could not add that: {exc}", "err")
        return RedirectResponse("/contacts", status_code=303)

    @app.get("/keys", response_class=HTMLResponse)
    def keys_page(request: Request):
        keys = []
        if state.store_path.exists():
            with state.store() as store:
                keys = store.all_keys()
        return render(request, "keys.html", keys=keys)

    @app.post("/publish-key", response_class=HTMLResponse)
    def publish_key(request: Request, csrf_token: str = Form(""), confirmed: str = Form("")):
        error = None
        prepared = None
        txid = None
        try:
            check_csrf(csrf_token)
            if not state.unlocked:
                # Nothing for the user to do but wait for the node; there is no
                # passphrase to enter any more.
                state.ensure_identity()
            # Publish WHO the key belongs to, not just the key. Without this the
            # announcement is filed under whichever address funded it, which is
            # not the address anyone was told to use.
            home = state.derived_address or ""
            try:
                _, home_hash = b58check_decode(home) if home else (0, b"")
            except HTTPException:
                raise          # a rejected form is a 400, not an error page
            except Exception:
                home_hash = b""
            payload = build_key_announcement(
                state.identity.public_bytes, home_hash, state.profile_name)
            # A long name will not fit one OP_RETURN, so it goes as Class B --
            # a couple of dust outputs rather than none. Better than publishing
            # half a name, permanently, for the cheaper fee.
            single_output = announcement_fits_one_output(payload)
            with state.messaging.rpc() as rpc:
                # Ask the CHAIN, not the store, whether this key is already
                # published. The store can be wrong in the one direction that
                # costs money: a reset dropped the announcement rows, the page
                # said "None seen yet" and offered to publish again -- for
                # something already permanent, and sitting below the new
                # starting block where no rescan could ever find it.
                existing = find_own_announcements(
                    rpc, state.messaging.params, state.identity.public_bytes)
                if existing:
                    # Refuse, and do NOT write the rows back. Restoring them
                    # would undo a reset the user asked for -- these are the
                    # very transactions they cleared. Knowing the announcement
                    # exists is what saves the fee; storing it again is a
                    # separate thing they did not ask for.
                    latest = existing[-1]
                    where = (f"in block {latest[2]:,}" if latest[2]
                             else "and waiting for a block")
                    raise ValueError(
                        f"this key is already published {where}, as {latest[0]}. "
                        f"{len(existing)} announcement"
                        f"{'' if len(existing) == 1 else 's'} for this key "
                        f"{'is' if len(existing) == 1 else 'are'} already on the "
                        f"chain, so there is nothing to pay for. It is not listed "
                        f"below because this installation starts at a later block "
                        f"-- that is the starting point doing its job, not a key "
                        f"gone missing. Publishing again would cost a second fee "
                        f"for a record that is already permanent."
                    )
                if not Miner(rpc, state.messaging.params).status().funded:
                    raise ValueError("no spendable coins yet -- see Wallet")
                sender = MessageSender(rpc, state.messaging.params)
                prepared = sender.prepare(
                    funded_address(rpc, prefer=home), payload,
                    class_c=single_output, change_address=home)
                if confirmed == "yes":
                    txid = sender.broadcast(prepared)
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except Exception as exc:
            error = str(exc)
        keys = []
        if state.store_path.exists():
            with state.store() as store:
                keys = store.all_keys()
        return render(request, "keys.html", keys=keys, error=error,
                      prepared=prepared, txid=txid)

    # --- wallet ---------------------------------------------------------------

    def _context(which: str):
        if which not in ("messaging", "ledger"):
            raise ValueError("unknown wallet")
        return state.messaging if which == "messaging" else state.ledger

    @app.get("/wallet", response_class=HTMLResponse)
    def wallet(request: Request):
        return render(request, "wallet.html", messaging=messaging_status(),
                      ledger=ledger_status(), prepared=None, which=None)

    @app.post("/wallet/receive")
    def wallet_receive(request: Request, which: str = Form(""), csrf_token: str = Form("")):
        try:
            check_csrf(csrf_token)
            ctx = _context(which)
            with ctx.rpc() as rpc:
                address = walletlib.receive_address(rpc, "DogecoinArcade")
            state.flash(f"{ctx.label} receiving address:  {address}", "reveal")
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except Exception as exc:
            state.flash(str(exc), "err")
        return RedirectResponse("/wallet", status_code=303)

    @app.post("/wallet/send", response_class=HTMLResponse)
    def wallet_send(request: Request, which: str = Form(""), destination: str = Form(""),
                    amount: str = Form(""), confirmed: str = Form(""),
                    csrf_token: str = Form("")):
        """Prepare, then broadcast only on a second explicit submission.

        The two-step is not decoration. On mainnet these are real coins, and the
        decoded transaction with its fee is the last point at which a mistyped
        address or a misplaced decimal can be caught.
        """
        error = None
        prepared = None
        txid = None
        try:
            check_csrf(csrf_token)
            ctx = _context(which)
            sats = walletlib.parse_amount(amount)
            with ctx.rpc() as rpc:
                prepared = walletlib.prepare_send(rpc, destination.strip(), sats)
                if confirmed == "yes":
                    txid = walletlib.broadcast(rpc, prepared)
                    state.flash(f"Sent. Transaction {txid}", "ok")
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except Exception as exc:
            error = str(exc)

        if txid:
            return RedirectResponse("/wallet", status_code=303)
        return render(request, "wallet.html", messaging=messaging_status(),
                      ledger=ledger_status(), prepared=prepared, which=which,
                      error=error, destination=destination, amount=amount)

    @app.post("/scan")
    def scan(request: Request, csrf_token: str = Form("")):
        try:
            check_csrf(csrf_token)
            with state.messaging.rpc() as rpc, state.store() as store:
                scanner = Scanner(rpc, state.messaging.params, store, identity=state.identity)
                result = scanner.scan(max_blocks=5000)
                state.flash(f"Scanned {result}", "ok")
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except Exception as exc:
            state.flash(f"Scan failed: {exc}", "err")
        return RedirectResponse("/", status_code=303)

    @app.post("/wallet/split")
    def wallet_split(request: Request, csrf_token: str = Form(""),
                     pieces: str = Form("30"), each: str = Form("10")):
        """Cut the messaging wallet into many small outputs.

        Chunks of a long message chain through change outputs only because there
        is one output to spend. With many, each chunk funds itself independently
        and they all go at once -- minutes of waiting become seconds.
        """
        try:
            check_csrf(csrf_token)
            count = max(2, min(200, int(pieces or 30)))
            amount = walletlib.parse_amount(each or "10")
            with state.messaging.rpc() as rpc:
                sender = MessageSender(rpc, state.messaging.params)
                home = state.derived_address or funded_address(rpc)
                prepared = sender.split_outputs(home, count, amount)
                txid = sender.broadcast(prepared)
            state.flash(
                f"Split into {count} pieces of {amount / 100_000_000:.2f}. "
                f"This takes effect once the split confirms -- about a block. "
                f"After that a long message sends in one go instead of waiting "
                f"between every transaction. ({txid[:16]}…)", "ok")
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except (SendError, ValueError, walletlib.WalletError) as exc:
            state.flash(str(exc), "err")
        except Exception as exc:
            state.flash(f"Could not split: {exc}", "err")
        return RedirectResponse("/wallet", status_code=303)

    @app.post("/fund")
    def fund(request: Request, csrf_token: str = Form("")):
        try:
            check_csrf(csrf_token)
            with state.messaging.rpc() as rpc:
                miner = Miner(rpc, state.messaging.params)
                status = miner.status()
                if status.funded or status.pending:
                    state.flash(status.describe())
                else:
                    address = rpc.call("getnewaddress")
                    status = miner.bootstrap(address)
                    state.flash(status.describe())
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except (MiningError, Exception) as exc:
            state.flash(f"Mining failed: {exc}", "err")
        return RedirectResponse("/wallet", status_code=303)


    # --- tokens ---------------------------------------------------------------
    # Shown for one chain at a time: mainnet, the ledger (D-012), or testnet,
    # where anyone can try tokens for nothing (D-016). The chain tag on the
    # page switches. Every action here is prepare -> show -> confirm, exactly
    # as sending coins is, because on mainnet each one spends real coins and,
    # once a token exists, moves real value.

    def _funded_addresses(rpc) -> list[dict[str, Any]]:
        """Addresses with coins to pay a fee from, largest first."""
        sums: dict[str, int] = {}
        for utxo in rpc.call("listunspent", 0, 9_999_999):
            if utxo.get("address") and utxo.get("spendable", True):
                sums[utxo["address"]] = sums.get(utxo["address"], 0) + int(
                    round(float(utxo["amount"]) * 100_000_000))
        return [{"address": a, "coins": v / 100_000_000}
                for a, v in sorted(sums.items(), key=lambda kv: -kv[1])]

    def _token_chain() -> tuple[Any, Any]:
        """The chain the Tokens page is on, and its index."""
        chain = state.token_chain
        return chain, state.token_index(chain)

    def _token_page_data() -> dict[str, Any]:
        """Everything /tokens shows, with the node's absence explained, not hidden."""
        chain, index = _token_chain()
        data: dict[str, Any] = {
            "chain": chain, "node": chain.status(),
            "other_chains": [c for c in state.token_chains if c is not chain],
            "index": index.status(node_tip=state.ledger_tips.get(chain.network)),
            "tokens": [], "holdings": [], "funded": [], "owned": set(),
            "pending": [], "node_error": None,
        }
        try:
            data["tokens"] = index.properties()
        except Exception as exc:
            data["node_error"] = f"the token index could not be read: {exc}"
            return data
        try:
            with chain.rpc() as rpc:
                owned = _ledger_addresses(rpc)
                data["funded"] = _funded_addresses(rpc)
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except Exception as exc:
            data["node_error"] = str(exc)
            owned = []
        data["owned"] = set(owned)
        data["holdings"] = index.balances(owned)
        # A broadcast token transaction is invisible until its block is
        # indexed; say so rather than let the page look as if nothing happened.
        still = []
        for item in state.pending_tokens:
            if item["network"] != chain.network:
                still.append(item)                # the other chain's; keep
            elif index.transaction(item["txid"]) is None:
                still.append(item)
        state.pending_tokens = still
        data["pending"] = [i for i in still if i["network"] == chain.network]
        return data

    # --- inscriptions ---------------------------------------------------------

    def _inscription_page_data(error: str | None = None, plan: Any = None,
                               page: int = 1) -> dict[str, Any]:
        chain, index = _token_chain()
        data: dict[str, Any] = {
            "chain": chain, "node": chain.status(),
            "other_chains": [c for c in state.token_chains if c is not chain],
            "index": index.status(node_tip=state.ledger_tips.get(chain.network)),
            "inscriptions": [], "mine": [], "unfinished": [], "owned": set(),
            "funded": [], "error": error, "plan": plan, "node_error": None,
            "my_tag": None, "tags": {},
            "page": page, "pages": 1, "per_page": PAGE_INSCRIPTIONS, "total": 0,
        }
        try:
            data["total"] = index.inscription_count()
            data["pages"] = max(1, -(-data["total"] // PAGE_INSCRIPTIONS))
            page = max(1, min(page, data["pages"]))
            data["page"] = page
            data["inscriptions"] = index.inscriptions(
                limit=PAGE_INSCRIPTIONS, offset=(page - 1) * PAGE_INSCRIPTIONS)
        except Exception as exc:
            data["node_error"] = f"the index could not be read: {exc}"
            return data
        try:
            with chain.rpc() as rpc:
                owned = set(_ledger_addresses(rpc))
                data["funded"] = _funded_addresses(rpc)
        except HTTPException:
            raise
        except Exception as exc:
            data["node_error"] = str(exc)
            owned = set()
        data["owned"] = owned
        data["mine"] = [row for row in data["inscriptions"] if row["owner"] in owned]
        senders = {row["owner"] for row in data["inscriptions"]}
        senders |= {row["creator"] for row in data["inscriptions"]}
        try:
            data["tags"] = index.tags_for(sorted(senders))
            for address in owned:
                found = index.tag_of(address)
                if found:
                    data["my_tag"] = found
                    break
            data["unfinished"] = [u for u in index.unfinished_inscriptions()
                                  if u["sender"] in owned]
        except Exception:
            pass
        return data

    @app.get("/inscriptions", response_class=HTMLResponse)
    def inscriptions_page(request: Request, page: int = 1):
        return render(request, "inscriptions.html", **_inscription_page_data(page=page))

    @app.post("/inscriptions/create", response_class=HTMLResponse)
    def inscribe(request: Request, csrf_token: str = Form(""),
                 json_field: str = Form(""), confirmed: str = Form(""),
                 fromaddress: str = Form(""),
                 attachment: UploadFile | None = File(None),
                 attached_name: str = Form(""), attached_type: str = Form(""),
                 attached_b64: str = Form("")):
        """Two steps, like every other thing here that spends.

        An inscription is permanent and paid for in advance, so the first press
        prices it and the second pays. Sync, because building and broadcasting
        dozens of transactions blocks for as long as it blocks.
        """
        error, plan = None, None
        try:
            check_csrf(csrf_token)
            content, name, kind = b"", attached_name, attached_type
            if attachment is not None and attachment.filename:
                content = attachment.file.read()
                name = attachment.filename
                kind = attachment.content_type or "application/octet-stream"
            elif attached_b64:
                content = base64.b64decode(attached_b64)
            if not content:
                raise ValueError("choose a file to inscribe.")

            plan = inscribelib.plan(content, kind or "application/octet-stream",
                                    json_field)
            if confirmed == "yes":
                chain, _ = _token_chain()
                with chain.rpc() as rpc:
                    sender = (_check_own_address(rpc, fromaddress) if fromaddress
                              else funded_address(rpc, mainnet=chain.is_mainnet))
                    sent = _inscribe_in_background(chain, sender, plan, name)
                state.flash(f"Inscribing {name} in {plan.chunks} transactions.", "ok")
                return RedirectResponse("/inscriptions", status_code=303)
            return render(request, "inscriptions.html",
                          **_inscription_page_data(plan=plan),
                          attached_b64=base64.b64encode(content).decode(),
                          attached_name=name, attached_type=kind,
                          json_field=json_field)
        except HTTPException:
            raise
        except Exception as exc:
            error = str(exc)
        return render(request, "inscriptions.html",
                      **_inscription_page_data(error=error), json_field=json_field)

    def _inscribe_in_background(chain: Any, sender: str, plan: Any, name: str):
        """Broadcast the pieces on a thread, reporting as it goes.

        The same shape as a long message: the request returns the moment the
        work starts, because holding it open for a hundred transactions is what
        makes an interface look hung and a second click look sensible.
        """
        def inscribe_work():
            try:
                with chain.rpc() as rpc:
                    # public_only=True: an inscription is public, uncompressed,
                    # unencrypted data. Nothing sealed passes this flag (D-014).
                    sender_obj = MessageSender(rpc, chain.params, public_only=True)
                    sender_obj.ensure_outputs(
                        sender, plan.chunks,
                        on_progress=lambda text, done, total:
                            state.update_progress(note=text))
                    sender_obj.send_all(
                        sender, plan.payloads,
                        on_progress=lambda text, done, total:
                            state.update_progress(note=text),
                        on_broadcast=lambda index, total, txid:
                            state.update_progress(done=index,
                                                  note=f"sent {index} of {total}"))
                state.finish_progress()
            except Exception as exc:
                state.finish_progress(error=str(exc))
            finally:
                state.end_send()

        if not state.begin_send():
            raise ValueError("something is already being sent. Wait for it to finish.")
        state.start_progress("inscription", plan.chunks, "a few minutes")
        threading.Thread(target=inscribe_work, name="arcade-inscribe",
                         daemon=True).start()
        return True

    def _check_own_address(rpc: Any, address: str) -> str:
        if address not in _ledger_addresses(rpc):
            raise ValueError("that address is not in this node's wallet.")
        return address

    # --- what an inscribed page can ask ---------------------------------------
    # Read-only, every one of them, and the only URLs in this application that
    # answer a cross-origin request. See web/content.py for why that is safe
    # and what it deliberately does not do.

    def _content_index():
        _, index = _token_chain()
        return index

    @app.get("/content/{key}")
    def inscription_content(key: str, download: int = 0):
        return contentlib.content(_content_index(), key, download=bool(download))

    @app.get("/r/inscription/{key}")
    def r_inscription(key: str):
        row = _content_index().inscription(contentlib._key(key))
        if row is None:
            return contentlib._missing("no such inscription")
        return contentlib._json(contentlib.describe(row))

    @app.get("/r/metadata/{key}")
    def r_metadata(key: str):
        return contentlib.metadata(_content_index(), key)

    @app.get("/r/blockheight")
    def r_blockheight():
        chain, _ = _token_chain()
        return contentlib._json(state.ledger_tips.get(chain.network))

    @app.get("/r/blocktime")
    def r_blocktime():
        chain, _ = _token_chain()
        try:
            with chain.rpc() as rpc:
                tip = rpc.get_block_count()
                return contentlib._json(
                    int(rpc.call("getblock", rpc.get_block_hash(tip)).get("time", 0)))
        except Exception:
            return contentlib._json(None)

    @app.get("/r/inscriptions")
    def r_inscriptions(after: int = -1, limit: int = 100):
        rows = _content_index().inscriptions(limit=limit, after=after)
        return contentlib._json([contentlib.describe(row) for row in rows])

    @app.get("/r/inscriptions/{address}")
    def r_inscriptions_of(address: str):
        rows = _content_index().inscriptions(owner=address, limit=200)
        return contentlib._json([contentlib.describe(row) for row in rows])

    @app.get("/r/balances/{address}")
    def r_balances(address: str):
        index = _content_index()
        try:
            held = index.balances(address)
        except Exception:
            held = []
        return contentlib._json([
            {"propertyid": row["property_id"], "name": row.get("name", ""),
             "balance": row.get("display", str(row.get("balance", 0)))}
            for row in held])

    @app.get("/r/tag/{name}")
    def r_tag(name: str):
        address = _content_index().address_of(name)
        return contentlib._json({"tag": taglib.normalise(name), "address": address})

    @app.get("/r/address/{address}")
    def r_address(address: str):
        index = _content_index()
        return contentlib._json({"address": address,
                                 "tag": index.tag_of(address)})

    @app.get("/r/wallet")
    def r_wallet():
        """This wallet, as an inscribed page sees it.

        A balance is public -- anyone with an index can look one up -- but
        WHICH address is yours is not, and that is the one thing a page cannot
        learn from the chain. So it can be turned off, and what it says when it
        is off is that it is off, rather than that there is nothing there.
        """
        if not state.inscription_wallet_access:
            return contentlib._json(
                {"error": "this wallet does not tell inscriptions who is looking"},
                status=403)
        chain, index = _token_chain()
        try:
            with chain.rpc() as rpc:
                addresses = _ledger_addresses(rpc)
        except Exception:
            addresses = []
        return contentlib._json({
            "addresses": addresses,
            "tag": next((index.tag_of(a) for a in addresses if index.tag_of(a)), None),
            "network": chain.network,
        })

    @app.get("/guide", response_class=HTMLResponse)
    def guide(request: Request):
        """Everything this does, in the application rather than only on a site.

        A user who is offline, or behind the remote tunnel, or simply does not
        know there is a website, still has to be able to find out what the
        thing in front of them can do.
        """
        return render(request, "guide.html", sections=guidelib.SECTIONS)

    @app.get("/inscriptions/{key}/view", response_class=HTMLResponse)
    def inscription_view(request: Request, key: str):
        """Look at an inscription, including one that is a page of its own.

        The content is NOT rendered into this page. It goes in a frame with
        `sandbox` and no `allow-same-origin`, so whatever a stranger inscribed
        runs in an opaque origin: no cookies, no reach into this page, no
        navigating the window it sits in. The wallet around it is a wallet that
        can spend, and inscribed code is code somebody else wrote.
        """
        _, index = _token_chain()
        row = index.inscription(contentlib._key(key))
        if row is None:
            state.flash("no such inscription", "err")
            return RedirectResponse("/inscriptions", status_code=303)
        return render(request, "inscription_view.html", row=row,
                      tag=index.tag_of(row["owner"]),
                      renders=row["content_type"].startswith(contentlib.RENDERABLE))

    @app.get("/tokens", response_class=HTMLResponse)
    def tokens(request: Request):
        return render(request, "tokens.html", prepared=None, **_token_page_data())

    @app.post("/tokens/chain")
    def tokens_chain(request: Request, chain: str = Form(""), csrf_token: str = Form("")):
        """Switch the Tokens page between mainnet and testnet."""
        check_csrf(csrf_token)
        try:
            state.switch_token_chain(chain)
        except ValueError as exc:
            state.flash(str(exc), "err")
        return RedirectResponse("/tokens", status_code=303)

    def _token_action(request: Request, *, action: str, confirmed: str,
                      build, fields: dict[str, str], back: str,
                      template: str = "tokens.html", **extra):
        """Prepare a token transaction, show it, and broadcast on a second yes.

        `build(rpc)` returns (sender, payload, reference). Every action funnels
        through here so the confirmation step cannot be skipped by any of them.

        What is broadcast is the transaction that was shown, byte for byte: the
        prepared hex is kept under its txid and the confirm form hands the txid
        back. Rebuilding on the second submission would pick inputs afresh, and
        then the fee and txid on the screen would belong to a transaction that
        never went anywhere. If the shown one is gone (the server restarted),
        it is rebuilt and shown again, not sent.
        """
        error, prepared, txid = None, None, None
        chain, index = _token_chain()
        try:
            if not index.enabled:
                raise tokenlib.TokenError(
                    f"{chain.label} has no start block for tokens yet.")
            with chain.rpc() as rpc:
                sender = tokenlib.TokenSender(rpc, chain.params)
                prepared = state.prepared_tokens.get((chain.network, confirmed))
                if prepared is None:
                    sender_address, payload, reference = build(rpc)
                    prepared = sender.prepare(sender_address, payload, reference)
                    prepared.what = action
                    state.prepared_tokens[(chain.network, prepared.txid)] = prepared
                    while len(state.prepared_tokens) > 20:     # keep the newest
                        del state.prepared_tokens[next(iter(state.prepared_tokens))]
                else:
                    txid = sender.broadcast(prepared)
                    state.prepared_tokens.pop((chain.network, confirmed), None)
                    state.pending_tokens.append(
                        {"txid": txid, "what": action, "at": time.time(),
                         "network": chain.network})
                    state.flash(f"{action.capitalize()} broadcast as {txid}. It shows "
                                f"here once its block is indexed.", "ok")
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except (tokenlib.TokenError, AmountError, ValueError) as exc:
            error = str(exc)
        except Exception as exc:
            error = f"{exc.__class__.__name__}: {exc}"
        if txid:
            return RedirectResponse(back, status_code=303)
        context = dict(error=error, prepared=prepared,
                       confirm_action=request.url.path, confirm_fields=fields,
                       confirm_what=action, back=back)
        if template == "tokens.html":
            context.update(_token_page_data())
        context.update(extra)
        return render(request, template, **context)

    def _amount_for(index, property_id: int, text: str) -> tuple[dict[str, Any], int]:
        prop = index.property(property_id)
        if prop is None:
            raise tokenlib.TokenError(f"there is no token {property_id}.")
        return prop, parse_amount(text, prop["divisible"])

    def _recipient(address: str) -> str:
        address = (address or "").strip()
        if not address:
            raise tokenlib.TokenError("a recipient address is needed.")
        complaint = _check_address(address, mainnet=state.token_chain.is_mainnet)
        if complaint:
            raise tokenlib.TokenError(complaint)
        return address

    @app.post("/tokens/create", response_class=HTMLResponse)
    def tokens_create(request: Request, sender: str = Form(""), name: str = Form(""),
                      supply: str = Form(""), kind: str = Form("fixed"),
                      units: str = Form("divisible"),
                      category: str = Form(""), subcategory: str = Form(""),
                      url: str = Form(""), data: str = Form(""),
                      confirmed: str = Form(""), csrf_token: str = Form("")):
        check_csrf(csrf_token)
        fields = dict(sender=sender, name=name, supply=supply, kind=kind, units=units,
                      category=category, subcategory=subcategory,
                      url=url, data=data)

        def build(rpc):
            divisible = units != "indivisible"
            managed = kind == "managed"
            amount = None if managed else parse_amount(supply, divisible)
            payload = tokenlib.issuance_payload(
                name=name, divisible=divisible, managed=managed, amount=amount,
                category=category,
                subcategory=subcategory, url=url, data=data)
            if not sender.strip():
                raise tokenlib.TokenError("choose the address that will issue the token.")
            return sender.strip(), payload, None

        return _token_action(request, action="create", confirmed=confirmed, build=build,
                             fields=fields, back="/tokens", form_create=fields)

    @app.post("/tokens/send", response_class=HTMLResponse)
    def tokens_send(request: Request, sender: str = Form(""), property_id: str = Form(""),
                    amount: str = Form(""), recipient: str = Form(""),
                    confirmed: str = Form(""), csrf_token: str = Form("")):
        check_csrf(csrf_token)
        fields = dict(sender=sender, property_id=property_id, amount=amount,
                      recipient=recipient)

        def build(rpc):
            index = state.token_index(state.token_chain)
            prop, units = _amount_for(index, int(property_id or 0), amount)
            held = index.balance(sender.strip(), prop["property_id"])
            if units > held:
                raise tokenlib.TokenError(
                    f"{sender.strip()} holds {format_amount(held, prop['divisible'])} "
                    f"{prop['name']}, not {format_amount(units, prop['divisible'])}.")
            return (sender.strip(), tokenlib.send_payload(prop["property_id"], units),
                    _recipient(recipient))

        return _token_action(request, action="send", confirmed=confirmed, build=build,
                             fields=fields, back="/tokens", form_send=fields)

    @app.get("/tokens/{property_id}", response_class=HTMLResponse)
    def token(request: Request, property_id: int):
        return render(request, "token.html", prepared=None, **_token_detail(property_id))

    def _token_detail(property_id: int) -> dict[str, Any]:
        chain, index = _token_chain()
        prop = index.property(property_id)
        if prop is None:
            raise HTTPException(status_code=404, detail=f"no token {property_id}")
        owned: set[str] = set()
        node_error = None
        try:
            with chain.rpc() as rpc:
                owned = set(_ledger_addresses(rpc))
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except Exception as exc:
            node_error = str(exc)
        return {
            "chain": chain, "node": chain.status(),
            "prop": prop,
            "holders": index.holders(property_id),
            "history": index.history(property_id=property_id),
            "index": index.status(node_tip=state.ledger_tips.get(chain.network)),
            "owned": owned,
            "is_issuer": prop["issuer"] in owned,
            "node_error": node_error,
        }

    def _issuer_action(request: Request, property_id: int, action: str, confirmed: str,
                       fields: dict[str, str], build):
        def wrapped(rpc):
            index = state.token_index(state.token_chain)
            prop = index.property(property_id)
            if prop is None:
                raise tokenlib.TokenError(f"there is no token {property_id}.")
            return build(rpc, prop, index)
        return _token_action(request, action=action, confirmed=confirmed, build=wrapped,
                             fields=fields, back=f"/tokens/{property_id}",
                             template="token.html", **_token_detail(property_id))

    @app.post("/tokens/{property_id}/grant", response_class=HTMLResponse)
    def token_grant(request: Request, property_id: int, amount: str = Form(""),
                    recipient: str = Form(""), note: str = Form(""),
                    confirmed: str = Form(""), csrf_token: str = Form("")):
        check_csrf(csrf_token)

        def build(rpc, prop, index):
            units = parse_amount(amount, prop["divisible"])
            to = _recipient(recipient) if recipient.strip() else None
            if to == prop["issuer"]:
                to = None                      # to self: no recipient output needed
            return prop["issuer"], tokenlib.grant_payload(property_id, units, note), to

        return _issuer_action(request, property_id, "grant", confirmed,
                              dict(amount=amount, recipient=recipient, note=note), build)

    @app.post("/tokens/{property_id}/revoke", response_class=HTMLResponse)
    def token_revoke(request: Request, property_id: int, amount: str = Form(""),
                     note: str = Form(""), confirmed: str = Form(""),
                     csrf_token: str = Form("")):
        check_csrf(csrf_token)

        def build(rpc, prop, index):
            units = parse_amount(amount, prop["divisible"])
            held = index.balance(prop["issuer"], property_id)
            if units > held:
                raise tokenlib.TokenError(
                    f"the issuer address holds {format_amount(held, prop['divisible'])}, "
                    f"which is all that can be revoked from it.")
            return prop["issuer"], tokenlib.revoke_payload(property_id, units, note), None

        return _issuer_action(request, property_id, "revoke", confirmed,
                              dict(amount=amount, note=note), build)

    @app.post("/tokens/{property_id}/issuer", response_class=HTMLResponse)
    def token_issuer(request: Request, property_id: int, recipient: str = Form(""),
                     confirmed: str = Form(""), csrf_token: str = Form("")):
        check_csrf(csrf_token)

        def build(rpc, prop, index):
            to = _recipient(recipient)
            if to == prop["issuer"]:
                raise tokenlib.TokenError("that address is already the issuer.")
            return prop["issuer"], tokenlib.change_issuer_payload(property_id), to

        return _issuer_action(request, property_id, "change issuer", confirmed,
                              dict(recipient=recipient), build)

    # --- the bot RPC ----------------------------------------------------------
    #
    # Omni Core's method names over JSON-RPC, one URL per chain, cookie-
    # authenticated. The forms above are for people; this is for scripts, and
    # it follows the same prepare-then-broadcast rule (web/rpc.py).

    # --- remote access ------------------------------------------------------

    def _remote_context(request: Request, error: str | None = None) -> dict:
        tunnel = state.remote_tunnel()
        return {
            "tunnel": tunnel,
            "qr": remotelib.qr_svg(tunnel.link) if tunnel else None,
            "durations": remotelib.DURATIONS,
            "default_minutes": remotelib.DEFAULT_MINUTES,
            "cloudflared": remotelib.find_cloudflared(state.home),
            "unlocked_here": (tunnel is not None and request.cookies.get(
                remotelib.COOKIE_NAME) == tunnel.token),
            "error": error,
        }

    @app.get("/remote", response_class=HTMLResponse)
    def remote_page(request: Request):
        return render(request, "remote.html", **_remote_context(request))

    @app.post("/remote/start", response_class=HTMLResponse)
    def remote_start(request: Request, csrf_token: str = Form(""),
                     minutes: str = Form(str(remotelib.DEFAULT_MINUTES))):
        """Open the tunnel. Sync, because waiting for the edge takes seconds."""
        error = None
        claimed = False
        try:
            check_csrf(csrf_token)
            wanted = int(minutes)
            if wanted not in remotelib.DURATIONS:
                raise ValueError("choose one of the offered lengths")
            # Claimed before anything slow happens. Opening takes seconds, and
            # two presses in that window would leave a second cloudflared
            # running that nothing here holds a handle to -- an open door with
            # no button to close it.
            claimed = state.claim_tunnel()
            if not claimed:
                raise ValueError("a tunnel is already open, or one is opening. "
                                 "Close it first.")
            state.set_tunnel(remotelib.open_tunnel(state.port, wanted, state.home))
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except Exception as exc:
            error = str(exc)
        finally:
            if claimed:
                state.release_tunnel()
        return render(request, "remote.html", **_remote_context(request, error))

    @app.post("/remote/stop")
    def remote_stop(request: Request, csrf_token: str = Form("")):
        try:
            check_csrf(csrf_token)
            remotelib.close_tunnel(state.remote_tunnel())
            state.set_tunnel(None)
            state.flash("The tunnel is closed.", "ok")
        except HTTPException:
            raise
        except Exception as exc:
            state.flash(str(exc), "err")
        return RedirectResponse("/remote", status_code=303)

    @app.get("/remote/unlock")
    def remote_unlock(request: Request, k: str = ""):
        """Trade the key in the QR code for a cookie, then get out of the URL.

        Out of the URL because it would otherwise sit in the phone's history and
        in every Referer the browser sends afterwards. The cookie is the session
        from here on, and it dies with the tunnel.
        """
        tunnel = state.remote_tunnel()
        if tunnel is None:
            return locked("Not open",
                          "This wallet is not accepting remote connections.")
        if not secrets.compare_digest(k, tunnel.token):
            return locked("Wrong key", "That link does not open this wallet.")
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            remotelib.COOKIE_NAME, tunnel.token,
            max_age=tunnel.seconds_left, httponly=True, samesite="lax",
            # The tunnel is https end to end; a cookie that would travel in
            # clear has no business existing.
            secure=True)
        return response

    @app.post("/rpc/{which}")
    def bot_rpc(request: Request, which: str, payload: Any = Body(None)):
        """A sync route, like every route that touches the node (see send_in_thread)."""
        chain = state.rpc_chain(which)
        if chain is None:
            return JSONResponse(
                {"result": None, "id": None,
                 "error": {"code": -32600,
                           "message": "no such chain: use /rpc/main or /rpc/test"}},
                status_code=404)
        return botrpc.handle(state, request, payload, chain)

    @app.exception_handler(RequestValidationError)
    def unreadable(request: Request, exc: RequestValidationError):
        """A body the RPC cannot parse is answered in JSON-RPC's own terms."""
        if request.url.path.startswith("/rpc/"):
            return JSONResponse(botrpc.parse_error(), status_code=400)
        return JSONResponse({"detail": exc.errors()}, status_code=422)

    @app.post("/rpc")
    def bot_rpc_unnamed(request: Request):
        return JSONResponse(
            {"result": None, "id": None,
             "error": {"code": -32600,
                       "message": "name the chain: POST /rpc/main or /rpc/test"}},
            status_code=404)

    # --- not yet built --------------------------------------------------------

    UNBUILT = {
        "/nfts": ("NFTs", "M4",
                  "Non-fungible properties, range transfers, and the issuer and "
                  "holder data slots."),
        "/exchange": ("Exchange", "M3",
                      "Two-sided MetaDEx order book: bids, asks, on-chain matching, "
                      "partial fills and price charts."),
        "/inscriptions-unused": ("Inscriptions", "M5",
                          "Chunked file inscription over Class B, with a sandboxed "
                          "viewer that verifies content hashes before display."),
    }

    def _unbuilt(request: Request, path: str) -> HTMLResponse:
        section, milestone, detail = UNBUILT[path]
        return render(request, "unbuilt.html", section=section, milestone=milestone,
                      detail=detail, ledger=ledger_status())

    @app.get("/nfts", response_class=HTMLResponse)
    def nfts(request: Request):
        return _unbuilt(request, "/nfts")

    @app.get("/exchange", response_class=HTMLResponse)
    def exchange(request: Request):
        return _unbuilt(request, "/exchange")



    return app


def _resolve_recipient(state, recipient: str) -> bytes:
    """Turn whatever the user typed into a public key.

    Accepts a contact code as well as an address with an on-chain announcement.
    Without the first, nobody could send a first message: publishing a key needs
    coins, coins need mining, and mining needs four hours -- so a new network
    would have no way to get started at all.
    """
    recipient = (recipient or "").strip()
    if not recipient:
        raise ValueError("choose a recipient, or paste their contact code")

    if recipient.lower().startswith(f"{contact.PREFIX}:"):
        network, public_bytes = contact.decode(recipient)
        if network != state.messaging.network:
            raise ValueError(
                f"that contact code is for {network}, but the Messenger is on "
                f"{state.messaging.network}"
            )
        return public_bytes

    with state.store() as store:
        row = store.key_for(recipient)
    if row is None:
        raise ValueError(
            f"no announced key for {recipient}. Either scan the chain, or ask "
            "them for their contact code and paste that instead."
        )
    return bytes(row["pubkey"])


def _profile_for_state(state, peer_key: bytes) -> "content.Profile | None":
    """What to tell a new correspondent about ourselves, or None.

    Sent only to somebody we have not written to before, so it introduces rather
    than repeats. It is a real disclosure -- it ties this messaging identity to a
    mainnet address for whoever receives it -- so the interface shows exactly
    what will be sent and lets it be turned off.
    """
    with state.store() as store:
        if store.thread(state.identity.fingerprint, peer_key):
            return None            # already talking; they have it already
    name = state.profile_name or ""
    testnet = state.derived_address or ""
    mainnet = ""
    try:
        with state.ledger.rpc() as rpc:
            mainnet = rpc.call("getaccountaddress", "arcade-identity") or ""
    except HTTPException:
        raise          # a rejected form is a 400, not an error page
    except Exception:
        mainnet = ""
    profile = content.Profile(name=name, testnet_address=testnet,
                              mainnet_address=mainnet)
    return None if profile.is_empty() else profile


def _with_attachments(store, rows: list) -> list:
    """Attach file summaries to thread rows, without loading the file bytes."""
    out = []
    for row in rows:
        item = dict(row)
        message_id = item.get("id")
        if not message_id:
            out.append(item)
            continue

        # A file, whether it arrived or we sent it. The sender keeps their own
        # copy because the chain's is sealed to the recipient, and it is shown
        # the same way -- an image they sent should look like an image, not like
        # the text "[sent photo.png]".
        if item.get("mine"):
            if not item.get("file_size"):
                out.append(item)
                continue
            row = store.sent_file(message_id)
            data = bytes(row["file_data"]) if row and row["file_data"] else b""
            info = {"id": message_id, "name": item.get("file_name", ""),
                    "type": item.get("file_type", ""),
                    "size": item.get("file_size", 0),
                    "url": f"/messages/sent-media/{message_id}",
                    "kind": None, "label": ""}
        else:
            summary = store.attachment_summary(message_id)
            if summary is None:
                out.append(item)
                continue
            row = store.attachment_for(message_id)
            data = bytes(row["data"]) if row is not None else b""
            info = {"id": message_id, "name": summary["name"],
                    "type": summary["content_type"], "size": summary["size"],
                    "url": f"/messages/media/{message_id}",
                    "kind": None, "label": ""}

        # Decided from the bytes, here, once -- the template must never be in a
        # position to render something on a sender's say-so.
        found = media.renderable(data) if data else None
        if found is not None:
            info["kind"] = found.kind
            info["label"] = found.label
        item["file"] = info
        out.append(item)
    return out


def _with_media(store, rows: list) -> list:
    """Decide what each post's file is, from its bytes, once and server side.

    The template must never be in a position to render something on a poster's
    say-so, so the decision does not travel as a declared type.
    """
    out = []
    for row in rows:
        item = dict(row)
        if item.get("file_size"):
            found = None
            record = store.group_post_file(item["id"])
            if record is not None and record["file_data"] is not None:
                found = media.renderable(bytes(record["file_data"]))
            item["kind"] = found.kind if found else None
            item["label"] = found.label if found else ""
        out.append(item)
    return out


def _safe_filename(name: str) -> str:
    """A filename safe to put in a header a browser will act on."""
    cleaned = "".join(c for c in (name or "") if c.isprintable() and c not in '"\\/')
    return cleaned.strip() or "attachment"


def _contact_view(row: Any) -> dict[str, Any]:
    """Flatten an address book row for the template.

    The pubkey is bytes, and templates should not be doing hex conversion or
    fingerprinting; both are done once, here.
    """
    key = bytes(row["pubkey"]) if row["pubkey"] else None
    return {
        "id": row["id"],
        "name": row["name"],
        "testnet_address": row["testnet_address"],
        "mainnet_address": row["mainnet_address"],
        "notes": row["notes"],
        "hex": key.hex() if key else "",
        "fingerprint": fingerprint_of(key) if key else "",
    }


def _ledger_addresses(rpc) -> list[str]:
    """Every address this wallet owns, funded or not.

    `listunspent` alone misses an address that holds tokens but no coins --
    the usual state of a recipient -- so the address book is asked too. Module
    level because the bot RPC (rpc.py) asks the same question.
    """
    found: dict[str, None] = {}
    try:
        for row in rpc.call("listreceivedbyaddress", 0, True):
            found.setdefault(row["address"], None)
    except Exception:
        pass
    for utxo in rpc.call("listunspent", 0, 9_999_999):
        if utxo.get("address"):
            found.setdefault(utxo["address"], None)
    return list(found)


def _check_address(address: str, *, mainnet: bool) -> str | None:
    """Return a human-readable complaint about `address`, or None if it is fine.

    An address carries its chain in the version byte, so a mainnet address pasted
    into the testnet field is detectable -- and worth detecting, because the two
    look similar enough to confuse and the consequences differ enormously.
    """
    wanted = [p for p in NETWORKS.values() if p.name.endswith("main") == mainnet]
    try:
        version, payload = b58check_decode(address)
    except HTTPException:
        raise          # a rejected form is a 400, not an error page
    except Exception:
        return "that does not look like an address (the checksum does not match)."
    if len(payload) != 20:
        return "that is not a 20-byte address."
    if any(version in (p.pubkeyhash_version, p.scripthash_version) for p in wanted):
        return None
    other = [p.name for p in NETWORKS.values()
             if version in (p.pubkeyhash_version, p.scripthash_version)]
    if other:
        return (f"that is a {other[0]} address, not a "
                f"{'mainnet' if mainnet else 'testnet'} one.")
    return f"unrecognised address version {version}."


#: Deliberately self-contained rather than a template: it has to render when
#: something has already gone wrong, so it should not depend on page state.
REFUSED_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Not accepted \u00b7 DogecoinArcade</title>
<style>
body{{margin:0;background:#141310;color:#ece8e0;
  font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}}
main{{max-width:520px;margin:14vh auto;padding:0 24px}}
h1{{font-size:1.3rem;margin:0 0 10px}}
p{{color:#9b948a}}
a{{color:#d9a520}}
</style></head><body><main>
<h1>That was not accepted</h1>
<p>{detail}</p>
<p><a href="/">Back to DogecoinArcade</a></p>
</main></body></html>"""


def _when(ts: int | None = None) -> str:
    """A timestamp, or now when called with nothing."""
    moment = dt.datetime.now() if ts is None else dt.datetime.fromtimestamp(ts)
    return moment.strftime("%Y-%m-%d %H:%M")

"""The DogecoinArcade web interface.

Messenger only, for now. The Exchange, NFT and Inscription sections are shown
but disabled, because the engines behind them (M3-M5) do not exist yet -- a menu
that pretends otherwise would be worse than one that says so.
"""

from __future__ import annotations

import contextlib
import base64
import dataclasses
import datetime as dt
import hashlib
import json
import logging
import re
import threading
import time
import html
import secrets
import sys
import os
from pathlib import Path
from urllib.parse import quote
from typing import Any

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    HTMLResponse, JSONResponse, RedirectResponse, Response,
)
from fastapi.templating import Jinja2Templates

from .. import backup, media, tokens as tokenlib, wallet as walletlib
from ..ledger import COIN, AmountError, format_amount, parse_amount
from ..config import NETWORKS, MainnetRefused, WrongChain
from .. import inscribe as inscribelib
from .. import collections as collectionlib
from .. import approvals as approvalslib
from .. import payload as P
from .. import inscriptions as inscriptionlib
from . import guide as guidelib
from .. import charts as chartlib
from .. import fees
from .. import gather as gatherlib
from .. import mintpad as mintpadlib
from .. import tokenpad as tokenpadlib
from .. import swap as swaplib
from .. import tags as taglib
from .. import remote as remotelib
from ..messaging import contact, content, group
from ..script import b58check_decode, b58check_encode
from ..messaging.derive import DerivationError, derive_identity
from ..messaging.envelope import (
    MAX_ANNOUNCE_NAME, MAX_ANNOUNCE_NAME_CLASS_B,
    announcement_fits_one_output, build_key_announcement,
)
from .. import pageapi
from .. import release as releaselib
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
from . import watcher as watcherlib
from . import rpc as botrpc
from .state import AppState

TEMPLATE_DIR = Path(__file__).parent / "templates"
TEMPLATES = Jinja2Templates(directory=str(TEMPLATE_DIR))


def _fromjson(text: str):
    """A template filter: the parsed JSON, or None for anything that is not."""
    import json as _json
    try:
        return _json.loads(text) if text else None
    except ValueError:
        return None


TEMPLATES.env.filters["fromjson"] = _fromjson


def _describe_leg(data) -> str:
    """A template filter: one leg of a swap, in words (arcade/swap.py)."""
    from ..swap import describe_leg
    return describe_leg(data) if isinstance(data, dict) else "?"


TEMPLATES.env.filters["describe_leg"] = _describe_leg


def _ago(when: Any) -> str:
    """A template filter: how long ago, in the words people use out loud.

    A block time is a number nobody reads as a moment. "4 minutes ago" is
    what a sale feed is for -- whether the market is alive right now.
    """
    if not when:
        return ""             # no time at all, rather than 1970
    try:
        seconds = time.time() - float(when)
    except (TypeError, ValueError):
        return ""
    if seconds < 0:
        seconds = 0
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return "just now"


TEMPLATES.env.filters["ago"] = _ago


def _ask_price(amount: Any, kind: Any) -> str:
    """A template filter: an ask's price, from the columns the ledger keeps."""
    from ..ledger import COIN

    try:
        amount, kind = int(amount or 0), int(kind or 0)
    except (TypeError, ValueError):
        return "?"
    if kind == 3:                     # inscriptions.LEG_COINS
        return f"{amount / COIN:.8f}".rstrip("0").rstrip(".") + " coins"
    return f"{amount:,} units"


TEMPLATES.env.filters["ask_price"] = _ask_price

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
log = logging.getLogger(__name__)

PAGE_INSCRIPTIONS = 24

NAV = [
    ("/",             "Overview",     None,        True),
    ("/messages",     "Messages",     "testnet",   True),
    ("/contacts",     "Address book", None,        True),
    ("/groups",       "Public",       None,        True),
    ("/backup",       "Backup",       None,        True),
    ("/wallet",       "Wallet",       None,        True),
    ("/tokens",       "Tokens",       "mainnet",   True),
    ("/nfts",         "NFTs",         "mainnet",   True),
    ("/exchange",     "Exchange",     "mainnet",   True),
    ("/approvals",    "Approvals",    None,        True),
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


#: What the pages' hostname serves: what a page in the sandbox may reach.
PAGES_DOOR = ("/content/", "/r/")


def remote_guard(state: AppState):
    """Refuse anything that arrived from outside without the key.

    Registered as middleware so it covers every route there is and every route
    anyone adds later. A door guarded route-by-route is a door that is open the
    first time somebody forgets.
    """
    async def guard(request: Request, call_next):
        tunnel = state.remote_tunnel()
        host = request.headers.get("host", "")
        if tunnel is not None and remotelib.same_host(host, tunnel.pages_url):
            # The inscribed pages' door. A page in the sandbox has an opaque
            # origin and sends no cookie, so this hostname is its whole key:
            # given out only as the frame's address inside the viewer, and
            # good for nothing but the content and the page API.
            if request.url.path.startswith(PAGES_DOOR):
                return await call_next(request)
            return locked("Not here", "Nothing is served at this address but "
                          "inscribed pages.", status=404)
        if not remotelib.is_remote(request.headers, host,
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
            "approvals_waiting": _approvals_waiting(),
            "unread_messages": _unread_messages(),
            "unread_board": _unread_board(),
            "offers_waiting": _offers_waiting(),
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
        # Only this wallet may put one of its pages in a frame. The approval
        # pop-up under a running inscription is one, and that is the reason
        # for the rule: an inscribed page could otherwise frame the same
        # approval and dress an Approve button up as part of its game. Its
        # sandbox already stops that -- an opaque origin is not 'self', and
        # without allow-forms nothing inside it can submit -- but a rule that
        # holds on its own is better than one that holds because of another.
        response.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
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

    def _approvals_waiting() -> int:
        try:
            return state.approvals.waiting()
        except Exception:
            return 0

    def _unread_messages() -> int:
        """Private messages waiting, for the badge beside Messages."""
        if not state.unlocked:
            return 0
        try:
            with state.store() as store:
                return store.unread_for(fingerprint_of(state.ensure_identity().public_bytes))
        except Exception:
            return 0

    def _unread_board() -> int:
        """Public posts since this wallet last looked at the board."""
        try:
            with state.store() as store:
                return store.board_unread(state.messaging.network)
        except Exception:
            return 0

    #: What `_offers_waiting` last worked out, and when. Every page asks, and
    #: the answer needs the node: the mempool, and which addresses are ours.
    #: A few seconds of staleness on a badge is not worth an RPC round per
    #: page, and the Exchange page itself does the work properly.
    _offers_seen: dict = {"at": 0.0, "n": 0}

    def _offers_waiting() -> int:
        """Offers on this wallet's pieces that it has not answered."""
        now = time.time()
        if now - _offers_seen["at"] < 10:
            return _offers_seen["n"]
        _offers_seen["at"] = now
        try:
            chain, index = _token_chain()
            with chain.rpc() as rpc:
                own = set(_ledger_addresses(rpc))
            offers = _merge_offers([o for o in index.pending_offers()
                                    if o["owner"] in own],
                                   index.offers_on(sorted(own)))
            # An offer this wallet has already accepted is not waiting for
            # anything from this end; it is waiting for the buyer to sign.
            answered = {o["give"]["txid"] for o in
                        state.offers.open_offers(chain.network)
                        + state.offers.sold_offers(chain.network)
                        if o["give"].get("kind") == "inscription"}
            _offers_seen["n"] = sum(1 for o in offers
                                    if o["inscription"] not in answered)
        except Exception:
            pass                          # keep the last answer; a badge is not worth an error
        return _offers_seen["n"]

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
                      auto_update=bool(state.setting("auto_update", True)),
                      auto_sell=bool(state.setting("auto_sell", True)),
                      auto_fill=bool(state.setting("auto_fill", True)),
                      update_status=state.update_status, when=_when,
                      update_every=watcherlib.BlockWatcher.UPDATE_EVERY,
                      release_key=releaselib.PUBLIC_KEY,
                      messaging=messaging_status(), ledger=ledger_status(), stats=stats)

    @app.post("/settings/updates")
    def set_auto_update(request: Request, csrf_token: str = Form(""),
                        auto: str = Form("")):
        """Turn automatic updates off, or back on.

        Off is a real choice and is why the box is there: this machine fetches
        code from a website and runs it, and some people want to look first.
        On is the default because a node that is behind does not merely lack
        features -- a consensus rule starts at a height, and old code reads the
        same block differently from everybody else (D-062).
        """
        check_csrf(csrf_token)
        state.set_setting("auto_update", auto == "on")
        state.flash("Updates will install themselves." if auto == "on" else
                    "Automatic updates are off. Run dogecoinarcade-update yourself.",
                    "ok")
        return RedirectResponse("/", status_code=303)

    @app.post("/settings/selling")
    def set_auto_sell(request: Request, csrf_token: str = Form(""),
                      auto: str = Form("")):
        """Stop this wallet accepting offers that meet its own asking price.

        On by default, because an ask is a price said in public and a seller
        who then ignores a buyer meeting it is worse than a seller with no
        price. Off is a real choice: somebody may want to look at every sale
        first, and turning it off leaves the asks standing and the offers
        waiting for a person (D-101).
        """
        check_csrf(csrf_token)
        state.set_setting("auto_sell", auto == "on")
        state.flash(
            "Offers that meet your asking price are accepted for you."
            if auto == "on" else
            "Offers will wait for you, even when they meet your asking price.",
            "ok")
        return RedirectResponse("/", status_code=303)

    @app.post("/settings/filling")
    def set_auto_fill(request: Request, csrf_token: str = Form(""),
                      auto: str = Form("")):
        """Stop this wallet taking prices its own orders cross.

        On by default: an order is a public instruction to trade at a price,
        and a book where a bid sits above an ask and nothing happens is two
        people waiting for each other (D-102).
        """
        check_csrf(csrf_token)
        state.set_setting("auto_fill", auto == "on")
        state.flash(
            "Your bids will take any ask they cross."
            if auto == "on" else
            "Your orders will rest until you take a price yourself.", "ok")
        return RedirectResponse("/", status_code=303)

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
        # So a board open on another screen shows it without being asked. It
        # was only the watcher that bumped this, so a post made here appeared
        # on the phone a poll later at best, and after a refresh at worst.
        state.bump_generation()

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
                      peer=None, when=_when, fingerprint_of=fingerprint_of,
                      tags=_tags_for(t["address"] for t in threads))

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
                      tags=_tags_for([t["address"] for t in threads]
                                     + [(peer or {}).get("address", "")]),
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
                # On testnet everything goes straight out, files and long
                # sends included. The confirmation is there so nobody spends
                # real coins by accident; where the coins are free it is a
                # step between a person and the thing they just typed, and
                # what it cost and how long it will take are reported as it
                # happens anyway (D-052). Mainnet keeps it.
                immediate = not state.messaging.is_mainnet
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
                      tags=_tags_for([t["address"] for t in threads]
                                     + [(peer or {}).get("address", "")]),
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
                if confirmed == "yes" or not state.messaging.is_mainnet:
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
    def contacts_page(request: Request, edit: int | None = None, find: str = ""):
        return _contacts_view(request, edit, find=find)

    def _contacts_view(request: Request, edit: int | None = None, find: str = "",
                       **kwargs: Any):
        """The address book, and the one control the Keys page used to hold.

        Publishing your key belongs beside the name it publishes, which is
        here; a page of its own listing keys and fingerprints was plumbing
        (D-030). Not a route itself: FastAPI reads **kwargs off the query
        string, so the door and the view are separate functions.
        """
        people, editing = [], None
        if state.store_path.exists():
            with state.store() as store:
                people = [_contact_view(row) for row in store.contacts()]
                if edit:
                    row = store.contact_by_id(edit)
                    editing = _contact_view(row) if row else None
        known_tags, matches = [], []
        try:
            _, tag_index = _tag_chain()
            known_tags = tag_index.tags(limit=200)
            if find.strip():
                matches = tag_index.search_tags(find, limit=25)
                for entry in matches:
                    entry["known"] = any(
                        entry["address"] in (row["testnet_address"],
                                             row["mainnet_address"])
                        for row in (people or []))
        except Exception:
            known_tags, matches = [], []   # no node: the field still works
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
                        # What the announcement SAYS its tag is. Checked
                        # against the chain below, because a tag is the one
                        # thing in an announcement a reader can check.
                        "claimed_tag": (row["tag"] if "tag" in row.keys() else "") or "",
                        "other_address": (row["other_address"]
                                          if "other_address" in row.keys() else "") or "",
                        # The name as published, read from the announcement
                        # itself rather than from a contact row. Looking it up in
                        # the address book could only ever find names for people
                        # already in it, which is precisely who this list leaves
                        # out.
                        "name": row["name"] or "",
                    })
        addresses = [k["address"] for k in published]
        # Both addresses of every contact. A tag is looked up fresh on every
        # render rather than stored beside the name, which is what makes an
        # address book follow its people: publish a new tag and everyone who
        # saved you sees it the next time they look, without being told and
        # without anything in their book changing. What is stored is the
        # address, so what they PAY is unaffected either way (D-072).
        for row in people:
            addresses += [row["testnet_address"], row["mainnet_address"]]
        found = _tags_for(addresses)
        for entry in published:
            # A tag the chain agrees with, or nothing. An announcement that
            # names a tag held by somebody else is the announcement being
            # wrong, and showing it would be repeating a false claim.
            entry["tag"] = (found.get(entry["address"]) or "") \
                if not entry["claimed_tag"] or \
                found.get(entry["address"]) == entry["claimed_tag"] else ""
            entry["tag_disputed"] = bool(
                entry["claimed_tag"] and found.get(entry["address"]) != entry["claimed_tag"])
        # Whether this wallet's own key is on the chain, for the one card that
        # now shows both halves of who you are.
        announced = False
        if state.unlocked and state.store_path.exists() and state.derived_address:
            with state.store() as store:
                announced = store.key_for(state.derived_address) is not None
        return render(request, "contacts.html", people=people, editing=editing,
                      published=published, when=_when, mine=_my_tag(), tags=found,
                      known_tags=known_tags, announced=announced,
                      matches=matches, find=find,
                      other_address=_other_chain_address(),
                      announce_limit=MAX_ANNOUNCE_NAME,
                      name_limit=MAX_ANNOUNCE_NAME_CLASS_B, **kwargs)

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
        resolved = {}
        for label, value, want_mainnet in (
            ("Testnet", testnet_address, False), ("Mainnet", mainnet_address, True)
        ):
            if value:
                # A tag is a name for an address, so a field that wants an
                # address takes one (D-070). Stored as the address it names:
                # a book that kept the name would silently follow the tag if
                # it moved, and paying whoever holds a name today is not what
                # somebody meant when they wrote it down last year.
                try:
                    value = _tag_address(state, value, mainnet=want_mainnet)
                except ValueError as exc:
                    state.flash(f"{label} address: {exc}", "err")
                    return RedirectResponse("/contacts", status_code=303)
                resolved[want_mainnet] = value
                problem = _check_address(value, mainnet=want_mainnet)
                if problem:
                    state.flash(f"{label} address: {problem}", "err")
                    return RedirectResponse("/contacts", status_code=303)
        testnet_address = resolved.get(False, testnet_address)
        mainnet_address = resolved.get(True, mainnet_address)

        # The book holds people who have said who they are on the chain, and
        # nobody else. A name you can check beats a name you typed: an entry
        # whose address holds no @tag is a row of base58 with a label, which
        # is the thing the address book exists to stop you relying on (D-079).
        #
        # Only when an address is being set. Editing the notes on somebody
        # saved before this rule must not lock you out of your own book.
        if testnet_address or mainnet_address:
            named = ""
            try:
                _, tag_index = _tag_chain()
                named = tag_index.tag_of(testnet_address) or ""
            except Exception:
                named = ""
            if not named and state.store_path.exists():
                with state.store() as store:
                    named = (store.tag_announced_at(testnet_address)
                             or store.tag_announced_at(mainnet_address))
            if not named:
                state.flash(
                    f"{testnet_address or mainnet_address} has not claimed an "
                    "@tag on the chain, so there is no name here that anybody "
                    "could check. Ask them to publish one from their own "
                    "address book -- one button, and then they can be added "
                    "by searching for it.", "err")
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
                for post in posts:
                    post["cards"] = _cards_in(post.get("text") or "")
                channels = store.group_channels(chain.network)
                # Looking at the board is what reading it means: there is no
                # per-post read mark because a post is not addressed to
                # anybody. Marked before the page is rendered, so the count
                # beside Public is gone by the time it is drawn -- and the
                # channel being looked at is marked on its own, so opening
                # one does not silence the others (D-108).
                store.mark_board_read(chain.network)
                if channel:
                    store.mark_channel_read(chain.network, channel)
                    for row in channels:
                        if row["channel"] == channel:
                            channels = [dict(r) for r in channels]
                            for entry in channels:
                                if entry["channel"] == channel:
                                    entry["unread"] = 0
                            break
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
                # Testnet posts go straight out. The confirmation exists so
                # nobody spends real coins by accident; on a chain where the
                # coins are free it is a step between a person and the thing
                # they just typed (D-052). Mainnet keeps it.
                if confirmed == "yes" or not chain.is_mainnet:
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
                for post in posts:
                    post["cards"] = _cards_in(post.get("text") or "")
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

    @app.post("/publish", response_class=HTMLResponse)
    def publish_identity(request: Request, tag: str = Form(""),
                         csrf_token: str = Form("")):
        """One button: claim the @tag if it changed, and say so on the chain.

        They were two cards with two buttons, and the split was the bug: a
        name is only half-claimed until the key announcement says so too, and
        nothing told anybody that. Somebody who changed their tag and stopped
        there was findable under a name they no longer held, by every wallet
        reading announcements rather than the tag table (D-076).

        No preview. Both transactions are testnet, always (D-010, D-075), and
        what is worth saying about a claim -- it is permanent, first claim
        wins -- the card says before the button is pressed rather than after.
        """
        check_csrf(csrf_token)
        chain, index = _tag_chain()
        wanted, claimed_txid = "", ""
        try:
            if not state.unlocked:
                state.ensure_identity()
            home = state.derived_address
            if not home:
                raise taglib.TagError("this wallet has no identity address yet.")
            asked = (tag or "").strip().lstrip("@")
            current = index.tag_of(home) or ""
            if asked and asked.lower() != current.lower():
                wanted = taglib.validate(asked)
                holder = index.address_of(wanted)
                if holder and holder != home:
                    raise taglib.TagError(f"@{wanted} is taken.")
                if holder != home:
                    payload = P.AnyData(data=taglib.encode(wanted)).encode()
                    with chain.rpc() as rpc:
                        sender = tokenlib.TokenSender(rpc, chain.params)
                        prepared = sender.prepare(home, payload)
                        prepared.what = f"claim @{wanted}"
                        claimed_txid = sender.broadcast(prepared)
                    state.pending_tokens.append(
                        {"txid": claimed_txid, "what": f"claim @{wanted}",
                         "at": time.time(), "network": chain.network})
            else:
                wanted = current
        except HTTPException:
            raise
        except (taglib.TagError, tokenlib.TokenError, ValueError) as exc:
            return _contacts_view(request, tag_error=str(exc), tag_wanted=tag)
        except Exception as exc:
            return _contacts_view(request, tag_error=f"{exc.__class__.__name__}: {exc}",
                                  tag_wanted=tag)
        # Then the key, carrying the name, whether or not the name is new: a
        # wallet that has never announced still needs to, and one that just
        # changed its tag needs to say the new one.
        return publish_key(request, csrf_token=csrf_token, confirmed="yes",
                           say_tag=wanted or None, claimed=claimed_txid)

    @app.post("/publish-key", response_class=HTMLResponse)
    def publish_key(request: Request, csrf_token: str = Form(""), confirmed: str = Form(""),
                    say_tag: str | None = None, claimed: str = ""):
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
            # Key, this address, the same wallet's address on the other chain,
            # and the @tag that address holds. The tag replaces the name a
            # person typed: a reader can check a tag against the chain, and
            # could only ever take a name on trust (D-032).
            other_hash, tag = _announced_extras()
            # The tag just claimed, when one was: the claim may not be in a
            # block yet, so reading it back off the chain would announce the
            # old name (or none) and the two statements would disagree until
            # somebody published again. The announcement is this wallet's own
            # statement; the claim transaction is what makes it true (D-076).
            tag = say_tag if say_tag is not None else tag
            payload = build_key_announcement(
                state.identity.public_bytes, home_hash, "",
                other_hash160=other_hash, tag=tag)
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
                if existing and not claimed:
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
        return _contacts_view(request, publish_error=error, prepared=prepared,
                              published_txid=txid)

    # --- wallet ---------------------------------------------------------------

    def _context(which: str):
        if which not in ("messaging", "ledger"):
            raise ValueError("unknown wallet")
        return state.messaging if which == "messaging" else state.ledger

    @app.get("/wallet", response_class=HTMLResponse)
    def wallet(request: Request):
        return render(request, "wallet.html", messaging=messaging_status(),
                      ledger=ledger_status(), prepared=None, which=None, now=time.time(),
                      mining=_mining_json(), tab="coins")

    @app.get("/wallet/tokens", response_class=HTMLResponse)
    def wallet_tokens(request: Request):
        """What this wallet holds in tokens, and the form that sends it.

        One wallet, three tabs: coins, tokens, NFTs. What you HOLD is a
        question about your wallet; /tokens and /nfts answer the other
        question, which is what exists on the chain (D-030).
        """
        return render(request, "wallet_tokens.html", tab="tokens",
                      form_send=None, **_token_page_data())

    @app.get("/wallet/nfts", response_class=HTMLResponse)
    def wallet_nfts(request: Request):
        """The inscriptions this wallet holds, and the way to send one."""
        return render(request, "wallet_nfts.html", tab="nfts",
                      **_inscription_page_data())

    @app.post("/wallet/receive")
    def wallet_receive(request: Request, which: str = Form(""), csrf_token: str = Form("")):
        try:
            check_csrf(csrf_token)
            ctx = _context(which)
            # THE address, not a fresh one: one address a chain holds the
            # coins, the tokens and the NFTs, so anything sent to it can be
            # spent, moved and swapped without first funding the address it
            # happened to land on (D-046).
            address = state.home_address(ctx)
            state.flash(f"{ctx.label} address:  {address}", "reveal")
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except Exception as exc:
            state.flash(str(exc), "err")
        return RedirectResponse("/wallet", status_code=303)

    def _gather_senders(chain, rpc):
        """The three ways a thing travels home, for gather.walk_home."""
        def send_coins(sender_address: str, to: str, sats: int,
                       outpoint: dict | None = None) -> str:
            """Coins home, or a fee out to an address that has none.

            A sweep names the output it is sweeping. `prepare_send` chooses
            its own inputs from the whole wallet, which for a sweep is the
            one thing it must not do -- it would move coins from the home
            address to itself and leave the stray output exactly where it
            was (D-046).
            """
            if outpoint is None:
                prepared = walletlib.prepare_send(rpc, to, sats)
                return walletlib.broadcast(rpc, prepared)
            raw = rpc.call("createrawtransaction",
                           [{"txid": outpoint["txid"], "vout": outpoint["vout"]}],
                           {to: round(sats / 100_000_000, 8)})
            funded = fees.fund(rpc, raw, {"changeAddress": to,
                                          "subtractFeeFromOutputs": [0]})
            signed = rpc.call("signrawtransaction", funded["hex"])
            if not signed.get("complete"):
                raise ValueError(f"could not sign the sweep: {signed.get('errors')}")
            return str(rpc.call("sendrawtransaction", signed["hex"]))

        def send_token(sender_address: str, to: str, pid: int, units: int) -> str:
            sender = tokenlib.TokenSender(rpc, chain.params)
            prepared = sender.prepare(sender_address,
                                      tokenlib.send_payload(pid, units), to)
            return sender.broadcast(prepared)

        def send_piece(sender_address: str, to: str, txid: str) -> str:
            payload = P.AnyData(data=inscriptionlib.Transfer(
                txid=bytes.fromhex(txid)).encode()).encode()
            sender = tokenlib.TokenSender(rpc, chain.params)
            prepared = sender.prepare(sender_address, payload, to)
            return sender.broadcast(prepared)

        return send_coins, send_token, send_piece

    def gather_once(chain, limit: int = gatherlib.PER_PASS) -> list[str]:
        """Walk one pass of this wallet's strays home. Returns txids.

        Under the application's one-send-at-a-time lock, like everything
        else that spends. Housekeeping must never build a transaction from
        the same outputs a collection run or a message is spending at that
        moment -- and if something else is sending, this simply waits for
        the next pass rather than queueing (D-046).
        """
        if not state.begin_send():
            return []
        try:
            index = state.token_index(chain)
            with chain.rpc() as rpc:
                own = _ledger_addresses(rpc)
                home = state.home_address(chain)
                coins, token, piece = _gather_senders(chain, rpc)
                return gatherlib.walk_home(rpc, index, home, own, send_coins=coins,
                                           send_token=token, send_piece=piece,
                                           limit=limit)
        finally:
            state.end_send()

    state.gather_once = gather_once

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
            destination = _tag_address(state, destination,
                                       mainnet=ctx.is_mainnet)
            with ctx.rpc() as rpc:
                prepared = walletlib.prepare_send(rpc, destination, sats)
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
        """Mine one testnet block, funded or not.

        It began as the way an empty wallet got its first coins, and refused
        once it had any. But on a test chain a block is also the only way
        anything confirms, and when nobody else is mining, a swap or a
        message can sit in the mempool for an hour -- so the button stays,
        and mines one block whenever it is pressed. The reward goes to the
        messaging address, so a wallet funded this way pays messages from
        the address it hands out (funded_address).

        Mined on a thread: at testnet's difficulty a block is twenty minutes
        or so of hashing on a small machine (miner.py), and the page comes
        straight back saying so, with the button gone until the block is
        found and a count of what has been tried meanwhile. Pressed again, it
        says it is already at it.
        """
        try:
            check_csrf(csrf_token)
            if state.mining is not None:
                state.flash("Already mining a block; it will say when one is found.")
                return RedirectResponse("/wallet", status_code=303)
            with state.messaging.rpc() as rpc:
                miner = Miner(rpc, state.messaging.params)      # refuses mainnet
                address = state.derived_address or rpc.call("getnewaddress")
                expected = miner.expected_hashes()
            state.mining = {"started": time.time(), "address": address, "tries": 0,
                            "rate": 0.0, "expected": expected, "stop": False}
            threading.Thread(target=_mine_one, args=(address,), name="arcade-mine",
                             daemon=True).start()
            state.flash(f"Mining one block to {address}. This takes a minute or "
                        "more; the page will say when it is found.")
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except (MiningError, Exception) as exc:
            state.flash(f"Mining failed: {exc}", "err")
        return RedirectResponse("/wallet", status_code=303)

    def _mine_one(address: str) -> None:
        """The thread behind /fund: one block, counting as it goes, then a notice."""
        def progress(tries: int, rate: float) -> None:
            if state.mining:
                state.mining.update(tries=tries, rate=rate)

        def stopped() -> bool:
            return bool(state.mining and state.mining.get("stop"))

        try:
            with state.messaging.rpc() as rpc:
                miner = Miner(rpc, state.messaging.params)
                block = miner.mine_one(address, on_progress=progress, stop=stopped)
                tries = (state.mining or {}).get("tries", 0)
                if block:
                    state.flash(f"Mined block {block[:16]}… to {address}. "
                                f"{miner.status().describe()}.", "ok")
                elif stopped():
                    state.flash(f"Stopped mining after {tries:,} hashes; no block.")
                else:
                    state.flash(f"No block in {tries:,} hashes; press it again.", "err")
        except Exception as exc:
            state.flash(f"Mining failed: {exc}", "err")
        finally:
            state.mining = None
            state.bump_generation()

    @app.post("/fund/stop")
    def fund_stop(csrf_token: str = Form(...)):
        """Give up on the block after the batch the node is hashing now."""
        check_csrf(csrf_token)
        if state.mining is None:
            state.flash("Not mining.")
        else:
            state.mining["stop"] = True
            state.flash("Stopping: the node finishes the batch it is on (under a minute).")
        return RedirectResponse("/wallet", status_code=303)

    @app.get("/wallet/mining")
    def wallet_mining():
        """What the mining thread is up to, for the wallet page to watch."""
        return JSONResponse(_mining_json())

    def _mining_json() -> dict[str, Any]:
        mining = state.mining
        if mining is None:
            return {"mining": False}
        rate = float(mining.get("rate") or 0)
        return {"mining": True, "seconds": int(time.time() - mining["started"]),
                "tries": int(mining.get("tries", 0)), "rate": int(rate),
                "expected": int(mining.get("expected", 0)),
                # At the rate seen so far; 0 until the first batch is in.
                "expected_seconds": int(mining["expected"] / rate) if rate else 0,
                "stopping": bool(mining.get("stop"))}

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

    def _purses(holdings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """One row per token, not one per address.

        A wallet holding a token on four addresses holds one balance of it,
        not four; the addresses are how the node keeps it, which is plumbing
        (D-031). The pieces are kept on the row so a send can say which
        address it would come from, and so a person can see them if they
        want to.
        """
        by_token: dict[int, dict[str, Any]] = {}
        for row in holdings:
            purse = by_token.setdefault(row["property_id"], {
                "property_id": row["property_id"], "name": row["name"],
                "divisible": row["divisible"],
                "test_ecosystem": row["test_ecosystem"],
                "balance": 0, "pieces": []})
            purse["balance"] += int(row["balance"])
            purse["pieces"].append({"address": row["address"],
                                    "balance": int(row["balance"]),
                                    "display": row["display"]})
        for purse in by_token.values():
            purse["display"] = format_amount(purse["balance"], purse["divisible"])
            purse["pieces"].sort(key=lambda p: -p["balance"])
            # What one send can move: a token send comes from one address.
            purse["largest"] = purse["pieces"][0]
        return [by_token[k] for k in sorted(by_token)]

    def _token_page_data() -> dict[str, Any]:
        """Everything /tokens shows, with the node's absence explained, not hidden."""
        chain, index = _token_chain()
        data: dict[str, Any] = {
            "chain": chain, "node": chain.status(),
            "other_chains": [c for c in state.token_chains if c is not chain],
            "index": index.status(node_tip=state.ledger_tips.get(chain.network)),
            "tokens": [], "holdings": [], "funded": [], "owned": set(),
            "pending": [], "node_error": None, "faces": {}, "my_pictures": [],
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
        # Who issued a token, said the way people say it. An address is how
        # the chain names an issuer; a tag is how a person does (D-070).
        data["tags"] = _tags_for([t["issuer"] for t in data["tokens"]])
        data["holdings"] = index.balances(owned)
        data["purses"] = _purses(data["holdings"])
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
        data["faces"] = _faces_for(index, data["tokens"])
        # Pictures this wallet could give a token as its icon, offered rather
        # than asked for: an inscription id is 64 characters nobody types.
        try:
            data["my_pictures"] = [
                {"txid": row["txid"],
                 "label": (_fromjson(row["json"]) or {}).get("name")
                          or f"#{row['number']:,}"}
                for row in index.inscriptions(owners=sorted(owned), limit=60)
                if row["held"] and str(row["content_type"] or "").startswith("image/")]
        except Exception:
            data["my_pictures"] = []
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
            "my_tag": None, "tags": {}, "my_total": 0, "listed": {},
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
        # What this wallet holds is asked of the whole index, not filtered out
        # of the page on show: once there were more than a page of
        # inscriptions, everything of yours was older than page one and Yours
        # said "nothing yet" (D-028).
        try:
            addresses = sorted(owned)
            data["my_total"] = index.inscription_count(owners=addresses)
            data["mine"] = index.inscriptions(owners=addresses,
                                              limit=PAGE_INSCRIPTIONS)
        except Exception as exc:
            data["node_error"] = data["node_error"] or f"the index could not be read: {exc}"
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
        # Which of these are already for sale, so a card offers to list a
        # piece once and says where the price is the second time.
        try:
            data["listed"] = _prices_for(index, chain)
        except Exception:
            data["listed"] = {}
        return data

    @app.get("/nfts", response_class=HTMLResponse)
    def nfts_page(request: Request, page: int = 1):
        """Everything inscribed on this chain. Called NFTs because that is
        what people call them; the payload is still an inscription (D-030)."""
        return render(request, "inscriptions.html", **_inscription_page_data(page=page))

    @app.get("/inscriptions", response_class=HTMLResponse)
    def inscriptions_page(request: Request, page: int = 1):
        """The old name for /nfts. Kept, because links to it are on the chain
        and in other people's notes; a renamed page that 404s is a broken
        promise, not a rename."""
        where = "/nfts" + (f"?page={page}" if page != 1 else "")
        return RedirectResponse(where, status_code=303)

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
                    inscribelib.prepare_wallet(
                        sender_obj, sender, plan,
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

    # --- collections ----------------------------------------------------------

    @app.get("/collections", response_class=HTMLResponse)
    def collections_page(request: Request, page: int = 1):
        chain, index = _token_chain()
        data: dict[str, Any] = {"chain": chain, "node": chain.status(),
                                "collections": [], "node_error": None, "tags": {},
                                "page": page, "pages": 1, "total": 0}
        try:
            data["total"] = index.collection_count()
            data["pages"] = max(1, -(-data["total"] // PAGE_INSCRIPTIONS))
            page = max(1, min(page, data["pages"]))
            data["page"] = page
            data["collections"] = index.collections(
                limit=PAGE_INSCRIPTIONS, offset=(page - 1) * PAGE_INSCRIPTIONS)
            data["tags"] = index.tags_for(sorted({c["creator"] for c in data["collections"]}))
        except Exception as exc:
            data["node_error"] = f"the index could not be read: {exc}"
        return render(request, "collections.html", **data)

    @app.get("/collections/{creator}/{name}", response_class=HTMLResponse)
    def collection_page(request: Request, creator: str, name: str, page: int = 1):
        chain, index = _token_chain()
        summary = index.collection(creator, name)
        if summary is None:
            state.flash("no such collection on this chain", "err")
            return RedirectResponse("/collections", status_code=303)
        pages = max(1, -(-summary["count"] // PAGE_INSCRIPTIONS))
        page = max(1, min(page, pages))
        rows = index.collection_items(creator, name, limit=PAGE_INSCRIPTIONS,
                                      offset=(page - 1) * PAGE_INSCRIPTIONS)
        try:
            with chain.rpc() as rpc:
                owned = set(_ledger_addresses(rpc))
        except Exception:
            owned = set()
        senders = {r["owner"] for r in rows} | {creator}
        return render(request, "collection.html", chain=chain, node=chain.status(),
                      summary=summary, inscriptions=rows, owned=owned,
                      tags=index.tags_for(sorted(senders)),
                      traits=index.collection_traits(creator, name),
                      page=page, pages=pages, per_page=PAGE_INSCRIPTIONS)

    # --- inscribing a whole collection ----------------------------------------
    #
    # A wizard in three pages: where the build is, what it will cost, and the
    # run itself. The run is a job on disk (arcade/collections.py), so it can
    # be paused, resumed, and picked up after a crash.

    def _collection_upload_dir() -> Path:
        where = state.home / "collections"
        where.mkdir(parents=True, exist_ok=True, mode=0o700)
        return where

    def _collection_page_data(**extra: Any) -> dict[str, Any]:
        chain, _ = _token_chain()
        jobs, runner = state.collections
        data: dict[str, Any] = {"chain": chain, "node": chain.status(),
                                "funded": [], "node_error": None,
                                "jobs": jobs.list(chain.network),
                                # The mintpad offered at step 2: on unless the
                                # last attempt turned it off (D-036).
                                "pad_on": True, "pad_amount": "",
                                "pad_kind": "coins", "pad_token": "",
                                "clash": "", "partly": "",
                                "tokens": []}
        try:
            data["tokens"] = state.token_index(chain).properties()
        except Exception:
            data["tokens"] = []
        try:
            with chain.rpc() as rpc:
                data["funded"] = _funded_addresses(rpc)
        except Exception as exc:
            data["node_error"] = str(exc)
        data.update(extra)
        return data

    def _what_is_already_there(sender: str, build, label: str = "") -> dict[str, Any]:
        """What of this build is on the chain already, and what that means.

        Asked twice: at review, so the creator reads it before the fee is
        quoted, and at the press, where it refuses. A collection is
        (creator, name), so this only ever speaks about the creator's OWN
        sets -- somebody else's set of the same name is not in the way and
        cannot be (D-120).

        Three answers. Nothing there: carry on. All of it there: refuse,
        because a second copy of every piece is a second bill for pieces no
        node will file into the set. Some of it there: send the rest. That
        last one is the half-finished run, and paying again for the half that
        went up is exactly the mistake this is here to stop.
        """
        out: dict[str, Any] = {"blocked": "", "note": "", "skip": set()}
        collection = build.collection
        if not sender or not collection:
            return out
        try:
            chain, index = _token_chain()
            done = index.collection_editions(sender, collection)
        except Exception:
            return out                    # the index is the wizard's business
        jobs, _ = state.collections
        named = {collection, label.strip()} - {""}
        for job in jobs.list(chain.network):
            if (job.get("sender") == sender
                    and str(job.get("name") or "").strip() in named
                    and job.get("status") != "failed"):
                out["blocked"] = (
                    f"{collection} is already being inscribed from this "
                    f"address by run {job['id']}. Open that run and resume "
                    f"it rather than starting a second one.")
                return out
        out["skip"] = {item.edition for item in build.items if item.edition in done}
        if not out["skip"]:
            return out
        if len(out["skip"]) == len(build.items):
            out["blocked"] = (
                f"{collection} is already on this chain from this address, "
                f"all {len(done):,} pieces of it. Inscribing it again would "
                f"pay for a second copy of every item, and no node would file "
                f"the copies into the set.")
            return out
        left = len(build.items) - len(out["skip"])
        out["note"] = (
            f"{len(out['skip']):,} of these are already on this chain from "
            f"this address. This run sends the other {left:,}; the rest are "
            f"left alone, because a second copy joins nothing.")
        return out

    def _without(build, editions: set[int]):
        """The build minus the pieces that are already up."""
        if not editions:
            return build
        return dataclasses.replace(
            build, items=[i for i in build.items if i.edition not in editions])

    @app.get("/inscriptions/collection", response_class=HTMLResponse)
    def collection_wizard(request: Request):
        return render(request, "collection_wizard.html", **_collection_page_data())

    @app.post("/inscriptions/collection/review", response_class=HTMLResponse)
    def collection_review(request: Request, csrf_token: str = Form(""),
                          folder: str = Form(""), fromaddress: str = Form(""),
                          files: list[UploadFile] = File([])):
        """Read the build and price it. Nothing is written down yet."""
        check_csrf(csrf_token)
        try:
            uploaded = [f for f in files if f.filename]
            if uploaded:
                folder = str(_save_upload(uploaded))
            if not folder.strip():
                raise ValueError("point at the build folder, or choose its files.")
            build = collectionlib.read_build(Path(folder.strip()))
            if not build.items:
                raise ValueError("no items with both metadata and an image.")
            chain, _ = _token_chain()
            with chain.rpc() as rpc:
                sender = (_check_own_address(rpc, fromaddress) if fromaddress
                          else funded_address(rpc, mainnet=chain.is_mainnet))
            # Priced on what this run would actually send, so the number on
            # the page is the number that is charged (D-120).
            already = _what_is_already_there(sender, build)
            build = _without(build, already["skip"])
            cost = collectionlib.estimate_build(build)
            return render(request, "collection_wizard.html",
                          **_collection_page_data(
                              build=build, cost=cost, sender=sender,
                              clash=already["blocked"], partly=already["note"],
                              preview=build.items[:12]))
        except HTTPException:
            raise
        except Exception as exc:
            return render(request, "collection_wizard.html",
                          **_collection_page_data(error=str(exc), folder=folder))

    def _save_upload(files: list[UploadFile]) -> Path:
        """Lay uploaded files out the way HashLips does, by name.

        A browser sends a chosen folder as a flat list of files, so the
        layout is rebuilt from the names: `_metadata.json` and `<n>.json` go
        under json/, pictures under images/. Anything else is not part of a
        build and is left out.
        """
        root = _collection_upload_dir() / secrets.token_hex(4)
        (root / "json").mkdir(parents=True)
        (root / "images").mkdir()
        kept = 0
        for upload in files:
            name = Path(upload.filename or "").name
            if not name or name.startswith("."):
                continue
            if name == "_metadata.json" or (name.endswith(".json") and name[:-5].isdigit()):
                target = root / "json" / name
            elif name.lower().endswith(collectionlib.IMAGE_SUFFIXES):
                target = root / "images" / name
            else:
                continue
            with target.open("wb") as out:
                while True:
                    block = upload.file.read(1 << 20)
                    if not block:
                        break
                    out.write(block)
            kept += 1
        if not kept:
            raise ValueError("none of the chosen files were metadata or images.")
        return root

    # --- seeing the mintpad before paying for it ------------------------------
    #
    # The page that will be inscribed, served from the build on disk instead
    # of from the chain: same bytes, with two addresses rewritten so the wall
    # is the creator's own images and the listing is the one they are about to
    # make. Nothing here is inscribed and nothing is spent (D-104).

    def _preview_build(folder: str):
        """The build a preview is of, or a complaint. Never an arbitrary path:
        only files the build itself lists are ever served."""
        return collectionlib.read_build(
            collectionlib.find_build(Path((folder or "").strip())))

    @app.get("/inscriptions/collection/preview")
    def preview_mintpad(request: Request, folder: str = "", name: str = ""):
        """The mintpad page as it will be inscribed, pointed at the build."""
        build = _preview_build(folder)
        collection = (name or build.collection).strip() or "Collection"
        page = mintpadlib.page("preview", collection).decode("utf-8")
        where = f"/inscriptions/collection/preview/set?folder={quote(folder)}"
        page = page.replace(
            "'/r/collection/' + CREATOR + '/' + COLLECTION + '?limit=100&offset='",
            f"'{where}&offset='")
        page = page.replace(
            "'/content/' + id",
            f"'/inscriptions/collection/preview/piece?folder={quote(folder)}&n=' + id")
        # Said in the page rather than around it, because the frame is
        # sandboxed and this is the only way to reach the button inside it.
        page += ("<script>addEventListener('DOMContentLoaded',function(){"
                 "var b=document.getElementById('buy');"
                 "if(b){b.disabled=true;b.textContent='Preview \u2014 nothing is on the chain yet';}"
                 "var f=document.getElementById('foot');"
                 "if(f){f.textContent='This is the page that will be inscribed. "
                 "The pictures are the ones in your build folder.';}});</script>")
        return HTMLResponse(page, headers={"Cache-Control": "no-store"})

    @app.get("/inscriptions/collection/preview/set")
    def preview_set(folder: str = "", limit: int = 100, offset: int = 0):
        """The build, in the shape /r/collection answers in."""
        build = _preview_build(folder)
        items = build.items[max(0, offset):max(0, offset) + max(1, min(limit, 500))]
        return contentlib._json({
            "creator": "preview", "name": build.collection,
            "count": len(build.items),
            "items": [{"id": item.edition, "number": item.edition,
                       "creator": "preview", "owner": "preview",
                       "contenttype": item.content_type,
                       "collection": build.collection, "edition": item.edition,
                       "json": _fromjson(item.json)} for item in items]})

    @app.get("/inscriptions/collection/preview/piece")
    def preview_piece(folder: str = "", n: int = 0):
        """One picture out of the build. Only what the build lists, by edition
        -- never a path somebody handed this route."""
        build = _preview_build(folder)
        item = next((i for i in build.items if i.edition == int(n)), None)
        if item is None:
            raise HTTPException(404, "no such piece in this build")
        path = (build.folder / item.image).resolve()
        if not path.is_file() or build.folder.resolve() not in path.parents:
            raise HTTPException(404, "that picture is not in the build")
        return Response(path.read_bytes(), media_type=item.content_type,
                        headers={"Cache-Control": "no-store"})

    @app.post("/inscriptions/collection/start")
    def collection_start(request: Request, csrf_token: str = Form(""),
                         folder: str = Form(""), fromaddress: str = Form(""),
                         name: str = Form(""), launchpad: str = Form(""),
                         pad_amount: str = Form(""), pad_kind: str = Form("coins"),
                         pad_token: str = Form(""), thumb: str = Form(""),
                         about: str = Form(""), site: str = Form("")):
        """The second press: write the job down and start it.

        The mintpad is decided here and inscribed by the runner when the last
        item is on its way, so the price is settled before anything is paid
        for rather than after (D-036).
        """
        check_csrf(csrf_token)
        chain, _ = _token_chain()
        jobs, runner = state.collections
        try:
            build = collectionlib.read_build(Path(folder.strip()))
            # What the set says about itself goes on its #1, which is the
            # piece a collection is known by (D-097). A thumbnail is an
            # inscription on this chain or nothing: the face of a set is not
            # on somebody's website.
            if thumb.strip() and not inscriptionlib.inscription_in(thumb):
                raise ValueError(
                    "a thumbnail is an inscription on this chain: give its id, "
                    "or choose a picture and inscribe it first.")
            # Before the node is asked anything, because this refusal is
            # about what is already on the chain and it should cost nothing
            # to hear (D-120).
            already = _what_is_already_there(fromaddress.strip(), build, name)
            if already["blocked"]:
                raise ValueError(already["blocked"])
            if already["skip"]:
                # A run that finishes a set does not rewrite what the set
                # says about itself: that is on its #1, which is up already.
                build = _without(build, already["skip"])
            else:
                build = collectionlib.with_details(build, {
                    "icon": inscriptionlib.inscription_in(thumb),
                    "description": about.strip(), "url": site.strip()})
            with chain.rpc() as rpc:
                sender = _check_own_address(rpc, fromaddress)
            pad_json = ""
            if launchpad == "yes":
                take = mintpadlib.take_of(pad_kind, pad_amount,
                                          int(pad_token) if pad_token else None)
                identity = state.ensure_identity()
                pad_json = mintpadlib.shop_json(
                    contact.encode(state.messaging.network, identity.public_bytes),
                    name.strip() or build.collection, take)
            job_id = jobs.create(chain.network, sender, build, name=name.strip(),
                                 pad_json=pad_json)
            runner.start(job_id)
        except Exception as exc:
            state.flash(str(exc), "err")
            return RedirectResponse("/inscriptions/collection", status_code=303)
        state.flash(f"Inscribing {len(build.items):,} items. This page follows "
                    f"the run; it carries on if you leave.", "ok")
        return RedirectResponse(f"/inscriptions/collection/{job_id}", status_code=303)

    @app.get("/inscriptions/collection/{job_id}", response_class=HTMLResponse)
    def collection_job(request: Request, job_id: str, page: int = 1):
        jobs, runner = state.collections
        job = jobs.get(job_id)
        if job is None:
            state.flash("no such collection run", "err")
            return RedirectResponse("/inscriptions/collection", status_code=303)
        per_page = 100
        pages = max(1, -(-job["items"] // per_page))
        page = max(1, min(page, pages))
        items = jobs.items(job_id, limit=per_page, offset=(page - 1) * per_page)
        numbers: dict[str, int] = {}
        index = None
        try:
            index = state.token_index(state.chain_named(job["network"]))
            for item in items:
                if item["txid"]:
                    row = index.inscription(item["txid"])
                    if row:
                        numbers[item["txid"]] = row["number"]
        except Exception:
            pass
        chain = state.chain_named(job["network"])
        # A broadcast is not a page. The mintpad's transaction going out means
        # it is on its way; the link only works once the chain has it and this
        # node has read it -- and "The mintpad is up. Open it" leading to "no
        # such inscription" is the wallet lying about its own work (D-115).
        pad_live = False
        if job["pad_txid"]:
            try:
                pad_live = index is not None and \
                index.inscription(job["pad_txid"]) is not None
            except Exception:
                pad_live = False
        return render(request, "collection_job.html", job=job, items=items,
                      numbers=numbers, running=runner.running(job_id),
                      pad_live=pad_live,
                      chain=chain, page=page, pages=pages)

    @app.get("/inscriptions/collection/{job_id}/status")
    def collection_status(job_id: str):
        jobs, runner = state.collections
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "no such collection run")
        return JSONResponse({**job, "running": runner.running(job_id)})

    @app.post("/inscriptions/collection/{job_id}/pause")
    def collection_pause(job_id: str, csrf_token: str = Form("")):
        check_csrf(csrf_token)
        _, runner = state.collections
        runner.pause(job_id)
        return RedirectResponse(f"/inscriptions/collection/{job_id}", status_code=303)

    @app.post("/inscriptions/collection/{job_id}/resume")
    def collection_resume(job_id: str, csrf_token: str = Form("")):
        check_csrf(csrf_token)
        jobs, runner = state.collections
        if jobs.get(job_id) is None:
            raise HTTPException(404, "no such collection run")
        runner.start(job_id)
        return RedirectResponse(f"/inscriptions/collection/{job_id}", status_code=303)

    @app.post("/inscriptions/collection/{job_id}/delete")
    def collection_delete(job_id: str, csrf_token: str = Form("")):
        """Forget a run. Only one that is not running: what is on the chain
        stays there either way, but a run mid-flight is the record of it."""
        check_csrf(csrf_token)
        jobs, runner = state.collections
        if runner.running(job_id):
            state.flash("pause the run before removing it.", "err")
            return RedirectResponse(f"/inscriptions/collection/{job_id}", status_code=303)
        jobs.delete(job_id)
        return RedirectResponse("/inscriptions/collection", status_code=303)

    # --- what an inscribed page can ask ---------------------------------------
    # Read-only, every one of them, and the only URLs in this application that
    # answer a cross-origin request. See web/content.py for why that is safe
    # and what it deliberately does not do.

    # --- @tags ----------------------------------------------------------------

    def _tag_chain() -> tuple[Any, Any]:
        """The chain a tag of your own is claimed on, and its index.

        A tag is how people know who wrote to them, so it belongs on the
        chain the messages are on. Tags are per-chain -- the same name on two
        chains is two names -- and the interface claims only on this one
        (D-032).
        """
        for chain in state.token_chains:
            if chain.network == state.messaging.network:
                return chain, state.token_index(chain)
        return _token_chain()

    def _other_chain_address() -> str:
        """This wallet's address on the chain it does not message on."""
        other_hash, _ = _announced_extras()
        if not other_hash:
            return ""
        for chain in state.token_chains:
            if chain.network != state.messaging.network:
                return b58check_encode(chain.params.pubkeyhash_version, other_hash)
        return ""

    def _announced_extras() -> tuple[bytes, str]:
        """The other chain's address (as 20 bytes) and the tag, for publishing.

        Both are best-effort: an announcement that says less is worth more
        than one that is not made, so a node with no ledger wallet or no tag
        still publishes its key and its messaging address.
        """
        other_hash = b""
        for chain in state.token_chains:
            if chain.network == state.messaging.network:
                continue
            try:
                with chain.rpc() as rpc:
                    found = _funded_addresses(rpc) or [
                        {"address": a} for a in _ledger_addresses(rpc)]
                if found:
                    _, other_hash = b58check_decode(found[0]["address"])
            except HTTPException:
                raise
            except Exception:
                other_hash = b""
            break
        return other_hash, (_my_tag()["tag"] or "")

    def _tags_for(addresses) -> dict[str, str]:
        """Which of these addresses hold a tag. Empty when nothing is indexed.

        A stale or missing index shows the address, never a name it is not
        sure about: a wrong name is worse than base58 (tags.display).
        """
        wanted = sorted({a for a in addresses if a})
        if not wanted:
            return {}
        try:
            _, index = _tag_chain()
            return index.tags_for(wanted)
        except Exception:
            return {}

    def _my_tag() -> dict[str, Any]:
        """Your tag as the chain has it, and the address that would hold one."""
        chain, index = _tag_chain()
        home = state.derived_address or ""
        found = None
        try:
            found = index.tag_of(home) if home else None
        except Exception:
            found = None
        return {"tag": found, "address": home, "chain": chain,
                "min": taglib.MIN_LENGTH, "max": taglib.MAX_LENGTH}

    #: An inscription named in a post: a link to its content or its page, or
    #: the bare txid on a line of its own. Anchored on the 64 hex characters,
    #: so a sentence that merely contains the word "content" is not a card.
    INSCRIPTION_IN_TEXT = re.compile(
        r"(?:/content/|/inscriptions/|/nfts/)?\b([0-9a-f]{64})\b")

    def _cards_in(text: str) -> list[dict[str, Any]]:
        """The inscriptions a post names, as cards this wallet vouches for.

        Never the content itself: a post is written by a stranger, and an
        inscription can be a page of scripts. What is shown is what this
        node's own index says about it -- number, name, type, size -- and a
        link to the viewer, which is the one place with a sandbox and a
        wallet bridge (D-035).
        """
        if not text:
            return []
        try:
            _, index = _token_chain()
        except Exception:
            return []
        cards, seen = [], set()
        for txid in INSCRIPTION_IN_TEXT.findall(text):
            if txid in seen or len(cards) >= 4:
                continue
            seen.add(txid)
            try:
                row = index.inscription(txid)
            except Exception:
                row = None
            if row is None:
                continue          # not on this chain, or not indexed yet
            name = ""
            try:
                name = str(json.loads(row["json"] or "{}").get("name") or "")
            except Exception:
                name = ""
            cards.append({
                "txid": txid, "number": row["number"], "name": name[:80],
                "content_type": row["content_type"],
                "content_len": row["content_len"],
                "collection": row.get("collection"),
                "edition": row.get("edition"),
                "image": bool(row["held"]) and str(
                    row["content_type"] or "").startswith("image/"),
            })
        return cards

    def _content_index():
        _, index = _token_chain()
        return index

    @app.get("/content/{key}")
    def inscription_content(key: str, download: int = 0):
        return contentlib.content(_content_index(), key, download=bool(download))

    @app.get("/r/inscription/{key}")
    def r_inscription(key: str):
        index = _content_index()
        row = index.inscription(contentlib._key(key))
        if row is None:
            return contentlib._missing("no such inscription")
        return contentlib._json(contentlib.describe(
            row, _tags_for([row["owner"], row["creator"]])))

    @app.get("/r/inscription/{key}/history")
    def r_inscription_history(key: str, limit: int = 100):
        """Every hand this piece has changed in, oldest first.

        Provenance, which the indexed row cannot answer on its own: it holds
        the current owner and is overwritten on every transfer, so a page
        asking "when did this leave the wallet that made it" had nothing to
        read (D-071). `left_creator` is that question answered directly,
        because it is the one people actually ask.
        """
        index = _content_index()
        row = index.inscription(contentlib._key(key))
        if row is None:
            return contentlib._missing("no such inscription")
        moves = index.moves(row["txid"], limit=limit)
        first = next((m for m in moves if m["from_address"] == row["creator"]), None)
        return contentlib._json({
            "id": row["txid"],
            "creator": row["creator"],
            "owner": row["owner"],
            "inscribed": row["block_height"],
            "left_creator": first["block_height"] if first else None,
            "moves": [{"from": m["from_address"], "to": m["to_address"],
                       "block": m["block_height"], "txid": m["txid"],
                       "how": m["how"]} for m in moves],
        })

    @app.get("/r/inscription/{key}/routes")
    def r_inscription_routes(key: str):
        """What this piece will answer, and where the answer comes from.

        Public because the declaration is inscribed: a caller can read what a
        route resolves to before asking, and a holder cannot quietly widen it
        (D-091).
        """
        row = _content_index().inscription(contentlib._key(key))
        if row is None:
            return contentlib._missing("no such inscription")
        return contentlib._json({"id": row["txid"], "routes": pageapi.declared(row)})

    @app.post("/r/ask")
    def r_ask(body: dict = Body(default={})):
        """One page asking another a question.

        Held here, answered here: no message, no fee, no wait. Held
        elsewhere, the question goes to that node as a sealed node-to-node
        message and the answer comes back through arcade.node's replies --
        the page gets a txid and listens, exactly as it does for a shop.

        A route is answered by this node from what the inscription declares,
        so it works whether or not the holder is looking at the screen. That
        is the whole point: two pieces can interact while one of their owners
        is asleep (D-091).
        """
        if not isinstance(body, dict):
            return contentlib._json({"error": "send a JSON object"}, status=400)
        index = _content_index()
        row = index.inscription(contentlib._key(str(body.get("inscription") or "")))
        if row is None:
            return contentlib._missing("no such inscription")
        route = str(body.get("route") or "")
        try:
            with _token_chain()[0].rpc() as rpc:
                own = _ledger_addresses(rpc)
        except Exception:
            own = []
        if row["owner"] in own:
            try:
                items = state.pagestore.items(row["txid"])
                return contentlib._json({"answer": pageapi.answer(row, route, items),
                                         "from": "here", "inscription": row["txid"]})
            except pageapi.ApiError as exc:
                return contentlib._json({"error": str(exc)}, status=400)
        # Somebody else's. Ask their node, and hand the page the receipt so it
        # can listen for the answer.
        try:
            to = _key_at(row["owner"])
            sent = _page_send(str(body.get("from") or row["txid"]),
                              state.messaging, to,
                              json.dumps({"ask": row["txid"], "route": route,
                                          "args": body.get("args") or {}}).encode())
        except Exception as exc:
            return contentlib._json({"error": str(exc)}, status=400)
        return contentlib._json({"asked": sent["txid"], "from": "their node",
                                 "inscription": row["txid"], "to": to.hex()},
                                status=202)

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
    def r_inscriptions(limit: int = 100, offset: int = 0, after: int = -1,
                       creator: str = "", owner: str = ""):
        """A page of inscriptions, newest first, each with its JSON.

        `offset` walks back through pages; `after` filters to numbers above one
        you have already seen, which is what a page polling for new work wants.
        """
        rows = _content_index().inscriptions(
            limit=limit, offset=offset, after=after,
            creator=creator or None, owner=owner or None)
        named = _tags_for([r["owner"] for r in rows] + [r["creator"] for r in rows])
        return contentlib._json([contentlib.describe(row, named) for row in rows])

    @app.get("/r/inscriptions/count")
    def r_inscription_count(owner: str = "", creator: str = ""):
        """How many there are, so a page can size its own paging."""
        return contentlib._json({
            "count": _content_index().inscription_count(
                owner=owner or None, creator=creator or None)})

    @app.get("/r/inscriptions/{address}")
    def r_inscriptions_of(address: str, limit: int = 200, offset: int = 0):
        rows = _content_index().inscriptions(owner=address, limit=limit,
                                             offset=offset)
        named = _tags_for([r["owner"] for r in rows] + [r["creator"] for r in rows])
        return contentlib._json([contentlib.describe(row, named) for row in rows])

    @app.get("/r/collections")
    def r_collections(limit: int = 100, offset: int = 0, creator: str = ""):
        """Every collection, newest first. A collection is what the JSON says
        it is (`inscriptions.collection_of`), so a HashLips set is one the
        moment its items are indexed."""
        rows = _content_index().collections(limit=limit, offset=offset,
                                            creator=creator or None)
        return contentlib._json([contentlib.describe_collection(r) for r in rows])

    @app.get("/r/collections/count")
    def r_collections_count(creator: str = ""):
        return contentlib._json({
            "count": _content_index().collection_count(creator=creator or None)})

    @app.get("/r/collection/{creator}/{name}")
    def r_collection(creator: str, name: str, limit: int = 100, offset: int = 0,
                     traits: int = 0):
        """One collection and a page of its items in edition order.

        `traits=1` adds how often every trait value occurs -- what rarity
        is -- read from the items' own JSON.
        """
        index = _content_index()
        summary = index.collection(creator, name)
        if summary is None:
            return contentlib._missing("no such collection")
        out = contentlib.describe_collection(summary)
        items = index.collection_items(creator, name, limit=limit, offset=offset)
        named = _tags_for([r["owner"] for r in items] + [r["creator"] for r in items])
        out["items"] = [contentlib.describe(r, named) for r in items]
        if traits:
            out["traits"] = index.collection_traits(creator, name)
        return contentlib._json(out)

    @app.get("/r/balances/{address}")
    def r_balances(address: str):
        """What one address holds. A list of addresses, because that is what
        `balances` takes -- handing it a string made one SQL placeholder per
        CHARACTER and matched nothing, silently, for every address there is."""
        try:
            held = _content_index().balances([address])
        except Exception:
            held = []
        return contentlib._json([contentlib.holding(row) for row in held])

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
        addresses, spendable = [], None
        try:
            with chain.rpc() as rpc:
                addresses = _ledger_addresses(rpc)
                spendable = float(rpc.call("getbalance") or 0)
        except Exception:
            pass

        # One query for every address this wallet has, then summed per token:
        # a wallet with coins on fifteen addresses holds one balance of each
        # token, not fifteen, and showing the pieces would be showing the
        # plumbing.
        tokens: list[dict[str, Any]] = []
        owned = 0
        try:
            held: dict[int, dict[str, Any]] = {}
            for row in index.balances(addresses):
                entry = held.setdefault(row["property_id"], {
                    "propertyid": row["property_id"], "name": row["name"],
                    "divisible": row["divisible"], "units": 0})
                entry["units"] += int(row["balance"])
            tokens = [contentlib.holding(entry) for entry in
                      sorted(held.values(), key=lambda e: e["propertyid"])]
        except Exception:
            tokens = []
        for address in addresses:
            try:
                owned += index.inscription_count(owner=address)
            except Exception:
                continue
        return contentlib._json({
            # One chain, said out loud. An inscription lives on exactly one, and
            # a page that asked for balances and silently got the other chain's
            # would be showing somebody a number about a wallet they do not have
            # on the chain they are looking at.
            "network": chain.network,
            "mainnet": chain.is_mainnet,
            "addresses": addresses,
            "tag": next((index.tag_of(a) for a in addresses if index.tag_of(a)), None),
            "coin": {"spendable": spendable, "ticker": chain.params.ticker
                     if hasattr(chain.params, "ticker") else ""},
            "tokens": tokens,
            "inscriptions": owned,
        })


    # --- approvals: sends that a page or a bot asked for ----------------------
    #
    # Neither may spend. Each files a request (arcade/approvals.py); the person
    # who owns the wallet sees the transaction it would be, built and decoded,
    # and says yes or no here. These pages work over the remote tunnel, so the
    # answer can be given from a phone.

    def _own_addresses(chain) -> list[str]:
        try:
            with chain.rpc() as rpc:
                return _ledger_addresses(rpc)
        except Exception:
            return []

    def _given(value) -> str:
        # Inscription 0 is an inscription; `value or ""` would lose it.
        return "" if value is None else str(value)

    def _json_field(value: Any) -> str:
        """A page may hand metadata as an object or as text; the chain takes
        text. Passing an object through unchanged would inscribe the word
        "dict"."""
        if value is None or value == "":
            return ""
        if isinstance(value, str):
            return value
        return json.dumps(value, separators=(",", ":"))

    def _file_request(origin: str, body: dict, label: str = "") -> dict:
        """Validate and queue one request; the caller's words go in quotes."""
        chain, index = _token_chain()
        kind = str(body.get("kind") or "").strip().lower()
        fields = approvalslib.validate(
            kind, index, _own_addresses(chain), mainnet=chain.is_mainnet,
            to=str(body.get("to") or ""), amount=_given(body.get("amount")),
            propertyid=body.get("propertyid"),
            inscription=_given(body.get("inscription")),
            fromaddress=str(body.get("from") or ""),
            data=body.get("data"), contenttype=str(body.get("contenttype") or ""),
            contentjson=_json_field(body.get("json")))
        request_id = state.approvals.file(
            chain.network, kind, origin, fields.pop("toaddress"),
            label=label or str(body.get("label") or ""),
            note=str(body.get("note") or ""), **fields)
        return approvalslib.describe(state.approvals.get(request_id))

    @app.get("/approvals", response_class=HTMLResponse)
    def approvals(request: Request):
        queue = state.approvals
        pending = [dict(r, summary=approvalslib.summary(r)) for r in queue.pending()]
        recent = [dict(r, summary=approvalslib.summary(r)) for r in queue.recent()]
        return render(request, "approvals.html", pending=pending, recent=recent,
                      now=time.time())

    @app.get("/approvals/waiting")
    def approvals_waiting():
        """For a page that wants to notice a new request without reloading."""
        pending = state.approvals.pending()
        now = time.time()
        return JSONResponse({"waiting": len(pending),
                             "requests": [{"id": r["id"], "kind": r["kind"],
                                           "summary": approvalslib.summary(r),
                                           "origin": r["origin"], "label": r["label"],
                                           # Seconds, by this clock, so a page
                                           # can tell fresh from stale without
                                           # trusting the browser's.
                                           "age": round(now - r["created"], 1)}
                                          for r in pending]})

    @app.api_route("/approvals/{request_id}", methods=["GET", "POST"],
                   response_class=HTMLResponse)
    def approval(request: Request, request_id: str, csrf_token: str = Form(""),
                 confirmed: str = Form(""), decision: str = Form(""),
                 embed: str = ""):
        """Look at one request as the transaction it would be, and decide.

        The transaction is built when the page is drawn and broadcast only if
        the yes names the txid that was shown -- the same rule as every other
        send here (D-016). Deciding no costs nothing and builds nothing.

        `embed=1` draws it without the wallet's chrome, for the pop-up that
        opens under a running inscription the moment it asks. The decision is
        the same form against the same route; only the frame differs, and
        after deciding it comes back here instead of to the list so the
        pop-up can show the answer and close.
        """
        embedded = embed == "1"
        back = f"/approvals/{request_id}?embed=1" if embedded else "/approvals"
        queue = state.approvals
        row = queue.get(request_id)
        if row is None:
            state.flash("no such request", "err")
            return RedirectResponse("/approvals", status_code=303)
        chain = state.chain_named(row["network"])
        index = state.token_index(chain)
        if row["kind"] == "mint" and decision == "approve" and row["status"] == "pending":
            # An inscription is many transactions chained through change
            # outputs, so there is no single signed thing to show and then
            # broadcast: approving one starts the inscriber, and the progress
            # bubble reports it exactly as the Wallet's own does (D-092).
            try:
                check_csrf(csrf_token)
                plan = inscribelib.plan(bytes(row["content"] or b""),
                                        row["contenttype"] or "application/octet-stream",
                                        row["meta"] or "")
                with chain.rpc() as rpc:
                    sender = funded_address(rpc, mainnet=chain.is_mainnet)
                    _inscribe_in_background(chain, sender, plan,
                                            f"request {row['id'][:8]}")
                queue.decide(request_id, "sent", txid="")
                state.flash(f"Inscribing {int(row['units'] or 0):,} bytes in "
                            f"{plan.chunks} transactions.", "ok")
            except HTTPException:
                raise
            except Exception as exc:
                queue.decide(request_id, "failed", error=str(exc))
                state.flash(str(exc), "err")
            return RedirectResponse(back, status_code=303)
        error, prepared, sent = None, None, None
        if request.method == "POST":
            check_csrf(csrf_token)
            if decision == "deny":
                queue.decide(request_id, "denied")
                state.flash(f"Refused: {approvalslib.summary(row)}.", "ok")
                return RedirectResponse(back, status_code=303)
            held = state.prepared_tokens.get((chain.network, confirmed))
            if row["status"] != "pending":
                error = f"this request is already {row['status']}."
            elif held is None:
                error = ("what was shown is no longer held (the server restarted, "
                         "or it was shown too long ago); look at it again.")
            elif row["kind"] == "swap":
                # The buyer's half, signed here; the shop's node signs the
                # other and broadcasts. What goes out from here is a message.
                try:
                    sent = _hand_to_shop(row, held)
                except Exception as exc:
                    error = str(exc)
                    queue.decide(request_id, "failed", error=error)
                state.prepared_tokens.pop((chain.network, confirmed), None)
                if sent:
                    queue.decide(request_id, "sent", txid=sent)
                    state.flash(f"Approved: {approvalslib.summary(row)}, signed and "
                                f"handed to the shop's node in message {sent}.", "ok")
                    return RedirectResponse(back, status_code=303)
            else:
                try:
                    with chain.rpc() as rpc:
                        sent = approvalslib.broadcast(rpc, held)
                except Exception as exc:
                    error = str(exc)
                    queue.decide(request_id, "failed", error=error)
                state.prepared_tokens.pop((chain.network, confirmed), None)
                if sent:
                    queue.decide(request_id, "sent", txid=sent)
                    state.pending_tokens.append(
                        {"txid": sent, "what": held.what, "at": time.time(),
                         "network": chain.network})
                    state.flash(f"Approved and sent: {approvalslib.summary(row)} "
                                f"as {sent}.", "ok")
                    return RedirectResponse(back, status_code=303)
        if row["status"] == "pending" and prepared is None and error is None:
            try:
                with chain.rpc() as rpc:
                    prepared = approvalslib.prepare(row, rpc, chain.params, index,
                                                    _ledger_addresses(rpc))
                state.prepared_tokens[(chain.network, prepared.txid)] = prepared
                while len(state.prepared_tokens) > 20:
                    del state.prepared_tokens[next(iter(state.prepared_tokens))]
            except Exception as exc:
                error = str(exc)
        return render(request, "approval.html", row=row, chain=chain,
                      summary=approvalslib.summary(row), prepared=prepared,
                      error=error, now=time.time(), embed=embedded)

    def _hand_to_shop(row: dict, held) -> str:
        """The approved half of a swap goes back to the shop's node, sealed
        to the key the shop's JSON names -- never one the page supplied."""
        from .. import swap as swaplib
        offer = json.loads(row["offer"])
        text = json.dumps({"swap": "sign", "swapv": swaplib.PROTOCOL,
                           "offer": offer["id"], "hex": held.hex}).encode()
        return _page_send(row["page"], state.messaging, bytes.fromhex(row["peer"]),
                          text)["txid"]

    @app.post("/r/send")
    def r_send(request: Request, body: dict = Body(default={})):
        """An inscribed page asks this wallet to send something.

        It gets a request id and a promise that somebody will be asked -- not
        a transaction. Any origin may file (the frame it runs in has none), so
        what it files is a question, never an action, and there is a cap on
        how many questions can wait. It polls /r/send/<id> for the answer.
        """
        if not isinstance(body, dict):
            return contentlib._json({"error": "send a JSON object"}, status=400)
        try:
            filed = _file_request("page", body)
        except approvalslib.RequestError as exc:
            return contentlib._json({"error": str(exc)}, status=400)
        except Exception as exc:
            return contentlib._json({"error": f"could not file the request: {exc}"},
                                    status=503)
        return contentlib._json(filed, status=202)

    @app.options("/r/send")
    def r_send_preflight():
        return Response(status_code=204, headers={
            **contentlib.CORS,
            "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Max-Age": "600"})

    def _tx_status(chain, txid: str) -> dict | None:
        """How far along a transaction is, asked of the node.

        A page that was told "sent" needs to know when sent became final:
        a shop hands over the hat at one confirmation, or six, and that is
        its call to make. The wallet's own transactions answer through
        gettransaction; anything else through getrawtransaction, which the
        installer's own configuration answers fully -- it writes `txindex=1`
        on both chains. On a node somebody else set up without it, that call
        sees only the mempool and this wallet's own transactions, so anything
        relying on the weaker behaviour is relying on a configuration we do
        not write. None when the node cannot be asked or has never heard of
        it.
        """
        from ..rpc import RpcError
        try:
            with chain.rpc() as rpc:
                try:
                    tx = rpc.call("gettransaction", txid)
                except RpcError:
                    tx = rpc.call("getrawtransaction", txid, 1)
                confirmations = int(tx.get("confirmations", 0) or 0)
                height = None
                if tx.get("blockhash"):
                    height = rpc.call("getblockheader", tx["blockhash"]).get("height")
        except Exception:
            return None
        return {"txid": txid, "confirmed": confirmations > 0,
                "confirmations": max(confirmations, 0),
                # -1 from gettransaction: a conflicting transaction was
                # confirmed instead, and this one never will be.
                "conflicted": confirmations < 0,
                "block": height, "blockhash": tx.get("blockhash"),
                "time": tx.get("blocktime") or tx.get("time")}

    def _described(row: dict) -> dict:
        """A request as a caller sees it, with confirmations once it is sent."""
        told = approvalslib.describe(row)
        if row["status"] == "sent" and row["txid"] and row["kind"] != "swap":
            told["confirmations"] = None
            status = _tx_status(state.chain_named(row["network"]), row["txid"])
            if status is not None:
                told["confirmations"] = status["confirmations"]
                told["confirmed"] = status["confirmed"]
        return told

    @app.get("/r/send/{request_id}")
    def r_send_status(request_id: str):
        row = state.approvals.get(request_id)
        if row is None:
            return contentlib._missing("no such request")
        return contentlib._json(_described(row))

    @app.get("/r/storage.js")
    def r_storage_js():
        """The storage shim an inscribed page loads (see pagestore.py).

        Served as a script, with CORS, cached for an hour: it is the same for
        every page and changes only with the wallet.
        """
        body = (TEMPLATE_DIR / "storage.js").read_text()
        return Response(body, media_type="application/javascript",
                        headers={**contentlib.CORS, "Cache-Control": "public, max-age=3600"})

    def _stored_page(txid: str) -> str:
        """The inscription a storage call is about, or a 404."""
        key = contentlib._key(txid)
        if not isinstance(key, str) or _content_index().inscription(key) is None:
            raise HTTPException(404, "no such inscription")
        return key

    @app.get("/storage/{txid}")
    def storage_load(txid: str):
        """Everything remembered for one page. Same origin only -- this is
        the viewer asking, on the page's behalf, never the page itself."""
        return JSONResponse({"ok": True, "items": state.pagestore.items(_stored_page(txid))})

    @app.post("/storage/{txid}")
    def storage_write(txid: str, body: dict = Body(default={})):
        """One change: set, remove or clear. The viewer sends it with the
        wallet's CSRF token, which the page in the sandbox never sees."""
        from ..pagestore import StoreError
        check_csrf(str(body.get("csrf_token", "")))
        page = _stored_page(txid)
        op = body.get("op")
        try:
            if op == "set":
                state.pagestore.set(page, body.get("key", ""), body.get("value", ""))
            elif op == "remove":
                state.pagestore.remove(page, body.get("key", ""))
            elif op == "clear":
                state.pagestore.clear(page)
            else:
                raise StoreError("op must be set, remove or clear")
        except StoreError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True})

    @app.get("/r/node.js")
    def r_node_js():
        """The node-to-node shim an inscribed page loads (see nodetalk.py)."""
        body = (TEMPLATE_DIR / "node.js").read_text()
        return Response(body, media_type="application/javascript",
                        headers={**contentlib.CORS, "Cache-Control": "public, max-age=3600"})

    def _page_send(page: str, chain, to: bytes, text: bytes) -> dict:
        """One node-to-node message from an inscribed page, as this wallet.

        Counted against the page's hour (nodetalk.MAX_PER_HOUR) and recorded
        under the page, which is what lets it read the reply. The swap door
        sends through here too, so a shop's traffic and a page's own share
        one cap and one record.
        """
        from .. import nodetalk
        if state.talk.sent_lately(page, chain.network) >= nodetalk.MAX_PER_HOUR:
            raise nodetalk.TalkError(
                f"this page has sent {nodetalk.MAX_PER_HOUR} messages in the "
                "last hour; that is the most it may")
        identity = state.ensure_identity()
        try:
            payload = nodetalk.apilib.seal(identity, to, text)
        except nodetalk.apilib.ApiMessageError as exc:
            raise nodetalk.TalkError(str(exc)) from None
        with chain.rpc() as rpc:
            sender = MessageSender(rpc, chain.params)
            address = funded_address(rpc, prefer=state.derived_address)
            try:
                prepared = sender.prepare(address, payload)
            except SendError:
                other = funded_address(rpc)
                if other == address:
                    raise
                address, prepared = other, sender.prepare(other, payload)
            sent = sender.broadcast(prepared)
        with state.store() as store:
            newest = store.conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM api_message").fetchone()[0]
        state.talk.record(page, chain.network, to.hex(), sent, newest)
        return {"ok": True, "txid": sent, "to": to.hex(),
                "fromaddress": address, "size": prepared.size,
                "fee": f"{prepared.fee_sats / COIN:.8f}",
                "total": f"{prepared.total_sats / COIN:.8f}"}

    def _page_replies(page: str, chain, body: dict) -> list[dict]:
        """What the nodes this page wrote to have said back (nodetalk.replies)."""
        from .. import nodetalk
        identity = state.ensure_identity()
        with state.store() as store:
            return nodetalk.replies(
                store, state.talk, page, chain.network,
                fingerprint_of(identity.public_bytes),
                after=int(body.get("after") or 0),
                limit=int(body.get("limit") or nodetalk.MAX_REPLIES))

    @app.post("/node/{txid}")
    def node_talk(txid: str, body: dict = Body(default={})):
        """A page sends a message to another node, or reads what came back.

        Same origin only, like /storage: the viewer asks on the page's
        behalf, with the wallet's CSRF token the page never sees, and names
        the inscription it framed -- so a page can only ever read the
        replies to what it sent itself. Sending goes out at once, on the
        messaging chain, which is testnet only (D-010); see nodetalk.py for
        why nobody is asked.
        """
        from .. import nodetalk
        check_csrf(str(body.get("csrf_token", "")))
        page = _stored_page(txid)
        chain = state.messaging
        op = body.get("op")
        try:
            if chain.network == "main":
                raise nodetalk.TalkError("node-to-node messages are testnet only")
            if op == "identity":
                identity = state.ensure_identity()
                return JSONResponse({"ok": True, "pubkey": identity.public_bytes.hex(),
                                     "contactcode": contact.encode(chain.network,
                                                                   identity.public_bytes),
                                     "network": chain.network,
                                     "maxbytes": nodetalk.apilib.MAX_API_PAYLOAD})
            if op == "sent":
                return JSONResponse({"ok": True,
                                     "sent": state.talk.letters(page, chain.network)})
            if op == "replies":
                return JSONResponse({"ok": True, "replies": _page_replies(page, chain, body)})
            if op != "send":
                raise nodetalk.TalkError("op must be identity, send, replies or sent")
            to = nodetalk.parse_pubkey(body.get("to"))
            text = nodetalk.body_bytes(body.get("body"))
            return JSONResponse(_page_send(page, chain, to, text))
        except (nodetalk.TalkError, SendError, ValueError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": f"the node could not do it: {exc}"},
                                status_code=503)

    # --- shops: an inscription that sells, and the page that buys from it ----
    #
    # The terms are in the shop inscription's JSON (arcade/swap.py); the
    # buyer's wallet reads them from its own ledger, asks the shop's node for
    # an offer, shows the buyer the transaction, and hands the signed half
    # back. The seller's half is the shopkeeper (arcade/shopkeeper.py), which
    # needs nobody: it sells only what the owner wrote down.

    @app.get("/r/swap.js")
    def r_swap_js():
        body = (TEMPLATE_DIR / "swap.js").read_text()
        return Response(body, media_type="application/javascript",
                        headers={**contentlib.CORS, "Cache-Control": "public, max-age=3600"})

    def _shop_page(txid: str, chain) -> dict:
        """The shop inscription a swap call is about, on the messaging chain."""
        key = contentlib._key(txid)
        row = state.token_index(chain).inscription(key) if isinstance(key, str) else None
        if row is None:
            raise HTTPException(404, "no such inscription on the chain shops live on")
        return row

    @app.post("/swap/{txid}")
    def swap_door(txid: str, body: dict = Body(default={})):
        """A page buys from the shop it is: same origin, viewer-sent, CSRF.

        `shop` reads the listings; `offer` asks the shop's node for one;
        `accept` puts the offer in the approvals queue, where the buyer sees
        the transaction and says yes; `status` follows the request; `replies`
        is the node door's, so the page can hear the shop's answers. The
        page's word is never the price: the offer is checked against the
        shop's JSON on this node before anybody is asked to approve it.
        """
        from .. import nodetalk
        from .. import swap as swaplib
        check_csrf(str(body.get("csrf_token", "")))
        chain = state.messaging
        op = body.get("op")
        try:
            if chain.network == "main" or chain.params.swaps_from is None:
                raise swaplib.SwapError("shops are testnet only")
            index = state.token_index(chain)
            row = _shop_page(txid, chain)
            page = row["txid"]
            shop = swaplib.shop_of(row)
            node = nodetalk.parse_pubkey(shop["node"])
            if op == "shop":
                with chain.rpc() as rpc:
                    own = _ledger_addresses(rpc)
                    # A page can say whether this wallet is able to buy,
                    # before somebody presses a button that cannot work.
                    ready_to_buy = False
                    if row["owner"] not in own:
                        try:
                            ready_to_buy = not _too_few_outputs(
                                rpc, funded_address(rpc, prefer=state.derived_address))
                        except Exception:
                            ready_to_buy = True     # cannot tell; do not nag
                height = index.indexed_height()
                return JSONResponse({
                    "ok": True, "shop": page, "node": node.hex(),
                    "can_buy": ready_to_buy,
                    "seller": row["owner"], "network": chain.network,
                    "mine": row["creator"] == row["owner"] and row["owner"] in own,
                    "open": row["creator"] == row["owner"],
                    "listings": swaplib.listings_json(row, index),
                    "height": height, "from": chain.params.swaps_from,
                    "ready": height is not None and height >= chain.params.swaps_from})
            if op == "replies":
                return JSONResponse({"ok": True, "replies": _page_replies(page, chain, body)})
            if op == "offer":
                listing_no = int(body.get("listing", -1))
                if not 0 <= listing_no < len(shop["listings"]):
                    raise swaplib.SwapError(f"no listing {listing_no}")
                take = swaplib.leg_of(shop["listings"][listing_no]["take"], index)
                with chain.rpc() as rpc:
                    own = _ledger_addresses(rpc)
                    if row["owner"] in own:
                        raise swaplib.SwapError("this is your own shop")
                    buyer = _buyer_for(rpc, index, own, take)
                    # Refused here, before an order is paid for: a wallet whose
                    # coins are in one output cannot both sign the swap and pay
                    # for the message carrying it, and would find that out
                    # three transactions later (D-051).
                    short = _too_few_outputs(rpc, buyer)
                    if short:
                        raise swaplib.SwapError(short)
                text = json.dumps({"swap": "offer", "swapv": swaplib.PROTOCOL, "shop": page,
                                   "listing": listing_no, "buyer": buyer}).encode()
                sent = _page_send(page, chain, node, text)
                return JSONResponse({"ok": True, "txid": sent["txid"], "buyer": buyer,
                                     "to": node.hex(), "listing": listing_no})
            if op == "accept":
                with chain.rpc() as rpc:
                    own = _ledger_addresses(rpc)
                offer = swaplib.check_offer(body.get("offer"), shop=page, own=own,
                                            height=index.indexed_height(),
                                            params=chain.params)
                if offer["seller"] != row["owner"]:
                    raise swaplib.SwapError("the offer is not from the wallet that holds "
                                            "this shop")
                request_id = state.approvals.file(
                    chain.network, "swap", "page", offer["seller"],
                    fromaddress=offer["buyer"], label=str(body.get("label") or ""),
                    note=str(body.get("note") or ""), offer=offer, page=page,
                    peer=node.hex())
                return JSONResponse({"ok": True, "request": request_id,
                                     "offer": offer["id"]})
            if op == "status":
                found = state.approvals.get(str(body.get("request") or ""))
                if found is None or found["page"] != page:
                    raise swaplib.SwapError("no such request from this page")
                told = approvalslib.describe(found)
                told["message"] = told.pop("txid")   # for a swap, the message it went in
                return JSONResponse({"ok": True, "request": told})
            raise swaplib.SwapError("op must be shop, offer, accept, status or replies")
        except (swaplib.SwapError, nodetalk.TalkError, approvalslib.RequestError,
                SendError, ValueError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except HTTPException:
            raise
        except Exception as exc:
            return JSONResponse({"ok": False, "error": f"the node could not do it: {exc}"},
                                status_code=503)

    def _buyer_for(rpc, index, own: list[str], take) -> str:
        """The address in this wallet that pays: it must hold what the shop
        takes, and it receives what the shop gives."""
        from .. import inscriptions as I
        from .. import swap as swaplib
        if take.kind == I.LEG_TOKEN:
            for address in own:
                if index.balance(address, take.property_id) >= take.amount:
                    return address
            raise swaplib.SwapError(f"no address in this wallet holds "
                                    f"{swaplib.describe_leg(swaplib.leg_json(take, index))}")
        if take.kind == I.LEG_INSCRIPTION:
            found = index.inscription(take.txid.hex())
            if found is None or found["owner"] not in own:
                raise swaplib.SwapError("this wallet does not hold the inscription the "
                                        "shop takes")
            return found["owner"]
        return funded_address(rpc, prefer=state.derived_address,
                              need=take.amount + swaplib.FEE_PER_KB * 2)

    @app.get("/r/owner.js")
    def r_owner_js():
        body = (TEMPLATE_DIR / "owner.js").read_text()
        return Response(body, media_type="application/javascript",
                        headers={**contentlib.CORS, "Cache-Control": "public, max-age=3600"})

    @app.post("/owner/{txid}")
    def owner_door(txid: str, body: dict = Body(default={})):
        """A page this wallet made and holds sends without being asked.

        The owner wrote the page and still holds it, so what it sends is what
        they told it to send: a shop paying out, a game handing over a prize.
        Filed and answered in one step -- the request is in the queue as
        sent, with its origin, so the record is the same as for a page that
        had to ask. Anybody else's page, or one this wallet sold on, goes
        through the queue and waits for a yes. Testnet only: nothing sends
        from mainnet without a person looking at it.
        """
        check_csrf(str(body.get("csrf_token", "")))
        chain, index = _token_chain()
        try:
            key = contentlib._key(txid)
            row = index.inscription(key) if isinstance(key, str) else None
            if row is None:
                raise HTTPException(404, "no such inscription")
            page = row["txid"]
            with chain.rpc() as rpc:
                own = _ledger_addresses(rpc)
            op = body.get("op")
            if op == "identity":
                return JSONResponse({"ok": True,
                                     "owner": row["creator"] == row["owner"] and row["owner"] in own,
                                     "creator": row["creator"], "holder": row["owner"],
                                     "network": chain.network})
            if op != "send":
                raise approvalslib.RequestError("op must be identity or send")
            if chain.is_mainnet:
                raise approvalslib.RequestError("a page sends without approval on testnet "
                                                "only; on mainnet it asks")
            if row["creator"] != row["owner"] or row["owner"] not in own:
                raise approvalslib.RequestError(
                    "only a page this wallet created and still holds sends without "
                    "asking; this one has to ask (arcade.send)")
            told = _file_request("own page", body)
            queue = state.approvals
            request = queue.get(told["id"])
            try:
                with chain.rpc() as rpc:
                    prepared = approvalslib.prepare(request, rpc, chain.params, index,
                                                    _ledger_addresses(rpc))
                    sent = approvalslib.broadcast(rpc, prepared)
            except Exception as exc:
                queue.decide(told["id"], "failed", error=str(exc))
                raise approvalslib.RequestError(f"could not send: {exc}") from None
            queue.decide(told["id"], "sent", txid=sent)
            state.pending_tokens.append({"txid": sent, "what": prepared.what,
                                         "at": time.time(), "network": chain.network})
            state.bump_generation()
            return JSONResponse({"ok": True, "txid": sent, "request": told["id"],
                                 "what": prepared.what,
                                 "fee": f"{prepared.fee_coins:.8f}"})
        except approvalslib.RequestError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except HTTPException:
            raise
        except Exception as exc:
            return JSONResponse({"ok": False, "error": f"the node could not do it: {exc}"},
                                status_code=503)

    @app.get("/r/tx/{txid}")
    def r_tx(txid: str):
        """Whether a transaction is confirmed, and how deep.

        The other half of asking: a page that was told its request was sent
        as some txid watches that txid here until it is buried as deep as it
        cares about. Any transaction the node knows, not only approved ones.
        """
        txid = txid.strip().lower()
        if len(txid) != contentlib.TXID_LENGTH or any(c not in "0123456789abcdef" for c in txid):
            return contentlib._json({"error": "a txid is 64 hex characters"}, status=400)
        chain, _ = _token_chain()
        status = _tx_status(chain, txid)
        if status is None:
            return contentlib._missing("this node does not know that transaction")
        return contentlib._json(status)

    @app.get("/guide", response_class=HTMLResponse)
    def guide(request: Request):
        """Everything this does, in the application rather than only on a site.

        A user who is offline, or behind the remote tunnel, or simply does not
        know there is a website, still has to be able to find out what the
        thing in front of them can do.
        """
        return render(request, "guide.html", sections=guidelib.sections(),
                      bugs_url=guidelib.BUGS_URL)

    @app.api_route("/inscriptions/{key}/send", methods=["GET", "POST"],
                   response_class=HTMLResponse)
    def inscription_send(request: Request, key: str, to: str = Form(""),
                         csrf_token: str = Form(""), confirmed: str = Form("")):
        """Hand an inscription to somebody else.

        Two steps like every other thing that spends, and for a stronger reason
        than usual: this one cannot be undone by sending it back unless the
        person on the other end agrees to. The decoded transaction is shown
        before anything is broadcast.
        """
        chain, index = _token_chain()
        row = index.inscription(contentlib._key(key))
        if row is None:
            state.flash("no such inscription", "err")
            return RedirectResponse("/inscriptions", status_code=303)

        error, prepared, sent = None, None, None
        if request.method == "POST":
            try:
                check_csrf(csrf_token)
                with chain.rpc() as rpc:
                    if row["owner"] not in _ledger_addresses(rpc):
                        raise ValueError("this inscription is not yours to send.")
                    # A @tag is a name, not an address: resolve it and SHOW what
                    # it resolved to, because a tag can have moved since the
                    # last block this node read.
                    destination = to.strip()
                    resolved = None
                    if taglib.looks_like_a_tag(destination):
                        wanted = taglib.normalise(destination)
                        resolved = index.address_of(wanted)
                        if not resolved:
                            raise ValueError(f"nobody holds @{wanted}.")
                        destination = resolved
                    problem = _check_address(destination,
                                             mainnet=chain.is_mainnet)
                    if problem:
                        raise ValueError(problem)

                    sender = tokenlib.TokenSender(rpc, chain.params)
                    payload = P.AnyData(data=inscriptionlib.Transfer(
                        txid=bytes.fromhex(row["txid"])).encode()).encode()
                    held = state.prepared_tokens.get((chain.network, confirmed))
                    if held is None:
                        prepared = sender.prepare(row["owner"], payload, destination)
                        prepared.what = f"inscription #{row['number']}"
                        state.prepared_tokens[(chain.network, prepared.txid)] = prepared
                        while len(state.prepared_tokens) > 20:
                            del state.prepared_tokens[next(iter(state.prepared_tokens))]
                    else:
                        sent = sender.broadcast(held)
                        state.prepared_tokens.pop((chain.network, confirmed), None)
                        state.pending_tokens.append(
                            {"txid": sent, "what": f"inscription #{row['number']}",
                             "at": time.time(), "network": chain.network})
                        state.flash(f"Inscription #{row['number']} sent as {sent}. "
                                    f"It moves here once its block is indexed.", "ok")
            except HTTPException:
                raise
            except Exception as exc:
                error = str(exc)
        if sent:
            return RedirectResponse("/inscriptions", status_code=303)
        return render(request, "inscription_send.html", row=row, chain=chain,
                      prepared=prepared, error=error, to=to,
                      tag=index.tag_of(row["owner"]))

    @app.get("/inscriptions/{key}/view", response_class=HTMLResponse)
    def inscription_view(request: Request, key: str):
        """Look at an inscription, including one that is a page of its own.

        The content is NOT rendered into this page. It goes in a frame with
        `sandbox` and no `allow-same-origin`, so whatever a stranger inscribed
        runs in an opaque origin: no cookies, no reach into this page, no
        navigating the window it sits in. The wallet around it is a wallet that
        can spend, and inscribed code is code somebody else wrote.
        """
        chain, index = _token_chain()
        row = index.inscription(contentlib._key(key))
        if row is None:
            state.flash("no such inscription", "err")
            return RedirectResponse("/inscriptions", status_code=303)
        # Over the tunnel the frame is addressed to the pages' own hostname:
        # from inside the sandbox nothing carries the cookie, and that door
        # needs none. On this machine there is no door, and no second name.
        tunnel = state.remote_tunnel()
        pages = (tunnel.pages_url or "") if tunnel is not None and remotelib.is_remote(
            request.headers, request.headers.get("host", ""), tunnel.url) else ""
        mine, held, coins = False, [], 0.0
        try:
            with chain.rpc() as rpc:
                own = _ledger_addresses(rpc)
                mine = row["owner"] in own
                # Only what this wallet HOLDS can be offered: an offer for a
                # token you do not have is a fee spent to be refused, and the
                # refusal would come from the other side (D-040).
                held = _purses(index.balances(own))
                coins = float(rpc.call("getbalance") or 0)
        except HTTPException:
            raise
        except Exception:
            mine, held, coins = mine, [], 0.0
        # Said by the wallet, around the frame, because an inscribed page
        # cannot be changed to say it (D-051).
        advice = ""
        try:
            with chain.rpc() as rpc:
                if not mine and (row["json"] or "").find('"shop"') >= 0:
                    advice = _too_few_outputs(
                        rpc, funded_address(rpc, prefer=state.derived_address))
        except HTTPException:
            raise
        except Exception:
            advice = ""
        # Both names in one lookup, which also covers the common case of a
        # piece still held by the wallet that made it -- most of a collection,
        # most of the time (D-074).
        named = _tags_for([row["owner"], row["creator"]])
        # Whether this piece is already for sale, so the wallet that holds it
        # is offered the listing it has rather than a second one.
        try:
            sale = _prices_for(index, chain).get(row["txid"])
        except Exception:
            sale = None
        return render(request, "inscription_view.html", row=row, chain=chain,
                      tag=named.get(row["owner"]), sale=sale,
                      creator_tag=named.get(row["creator"]),
                      pages=pages, mine=mine,
                      tokens=held, coins=coins, advice=advice,
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
        if template in ("tokens.html", "wallet_tokens.html"):
            context.update(_token_page_data())
            context["tab"] = "tokens"
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
        try:
            address = _tag_address(state, address,
                                   mainnet=state.token_chain.is_mainnet)
        except ValueError as exc:
            raise tokenlib.TokenError(str(exc)) from None
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
                      icon: str = Form(""),
                      confirmed: str = Form(""), csrf_token: str = Form("")):
        check_csrf(csrf_token)
        fields = dict(sender=sender, name=name, supply=supply, kind=kind, units=units,
                      category=category, subcategory=subcategory,
                      url=url, data=data, icon=icon)

        def build(rpc):
            divisible = units != "indivisible"
            managed = kind == "managed"
            amount = None if managed else parse_amount(supply, divisible)
            if icon.strip() and not tokenlib.icon_in(icon):
                raise tokenlib.TokenError(
                    "an icon is an inscription on this chain: paste its "
                    "/content/ link or its id, not a picture from elsewhere.")
            payload = tokenlib.issuance_payload(
                name=name, divisible=divisible, managed=managed, amount=amount,
                category=category, subcategory=subcategory, url=url,
                # The icon rides in `data` beside the description, because an
                # issuance has five strings and no sixth (tokens.details).
                data=tokenlib.data_with_icon(data, icon))
            if not sender.strip():
                raise tokenlib.TokenError("choose the address that will issue the token.")
            return sender.strip(), payload, None

        return _token_action(request, action="create", confirmed=confirmed, build=build,
                             fields=fields, back="/tokens", form_create=fields)

    def _send_parts(rpc, index, prop: dict, units: int,
                    avoid: str = "") -> list[tuple[str, int]]:
        """Which addresses a token send comes out of, and how much from each.

        A token send comes out of exactly one address, so a wallet holding a
        token in several piles cannot always send in one transaction. The
        wallet works out the pieces instead of refusing:

        * the SMALLEST pile that can cover the whole amount, when one can --
          it spends a small pile up rather than breaking a large one, which
          is what leaves a wallet with fewer, bigger pieces over time;
        * otherwise the largest piles first until what is left can be
          covered, and the remainder from the smallest pile that covers it,
          so the send is as few transactions as it can be and the last one
          does not shatter another big pile (D-043).

        Between piles that can pay, one that also has coins for its own fee
        wins: a pile with no coins cannot send at all.
        """
        pid = prop["property_id"]
        piles = [(index.balance(a, pid), a) for a in _ledger_addresses(rpc)
                 if a != avoid]          # a send to itself moves nothing
        piles = sorted(((held, a) for held, a in piles if held > 0), reverse=True)
        if not piles:
            raise tokenlib.TokenError(
                f"no address in this wallet holds any {prop['name']}"
                + (" other than the one you are sending to." if avoid else "."))
        total = sum(held for held, _ in piles)
        if total < units:
            raise tokenlib.TokenError(
                f"this wallet holds {format_amount(total, prop['divisible'])} "
                f"{prop['name']}, not {format_amount(units, prop['divisible'])}.")
        funded = {row["address"] for row in _funded_addresses(rpc)}

        def smallest_covering(need: int) -> str | None:
            fitting = sorted((p for p in piles if p[0] >= need))
            for held, where in fitting:              # one that can pay its fee
                if where in funded:
                    return where
            return fitting[0][1] if fitting else None

        one = smallest_covering(units)
        if one is not None:
            return [(one, units)]

        # A pile with no coins cannot pay its own fee, so the ones that can
        # go first; if they are not enough the rest are used anyway, and
        # `prepare` says precisely which address needs coins and how many.
        ordered = ([p for p in piles if p[1] in funded]
                   + [p for p in piles if p[1] not in funded])
        parts: list[tuple[str, int]] = []
        left = units
        for held, where in ordered:                  # those that can pay, biggest first
            if left <= 0:
                break
            last = smallest_covering(left)
            if last is not None and not any(where == a for a, _ in parts):
                parts.append((last, left))
                left = 0
                break
            take = min(held, left)
            parts.append((where, take))
            left -= take
        if left > 0:
            raise tokenlib.TokenError(
                f"this wallet holds {format_amount(total, prop['divisible'])} "
                f"{prop['name']} but cannot reach "
                f"{format_amount(units, prop['divisible'])} of it from these "
                f"addresses.")
        return parts

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
            to = _recipient(recipient)
            from_address = sender.strip() or _send_parts(
                rpc, index, prop, units, avoid=to)[0][0]
            held = index.balance(from_address, prop["property_id"])
            if units > held:
                raise tokenlib.TokenError(
                    f"{from_address} holds {format_amount(held, prop['divisible'])} "
                    f"{prop['name']}, not {format_amount(units, prop['divisible'])}.")
            return (from_address, tokenlib.send_payload(prop["property_id"], units), to)

        # More than one pile means more than one transaction, and they are
        # shown and broadcast together rather than refused (D-043).
        parts: list[tuple[str, int]] = []
        if not sender.strip():
            try:
                chain, index = _token_chain()
                prop, units = _amount_for(index, int(property_id or 0), amount)
                with chain.rpc() as chain_rpc:
                    parts = _send_parts(chain_rpc, index, prop, units,
                                        avoid=_recipient(recipient))
            except HTTPException:
                raise
            except Exception:
                parts = []          # let the single-send path say why
        if len(parts) > 1:
            return _token_send_many(request, parts, property_id, recipient,
                                    fields, confirmed)

        # Sending what you hold is a wallet question, so it is shown and
        # confirmed on the wallet's Tokens tab (D-030).
        return _token_action(request, action="send", confirmed=confirmed, build=build,
                             fields=fields, back="/wallet/tokens",
                             template="wallet_tokens.html", form_send=fields)

    def _token_send_many(request: Request, parts: list[tuple[str, int]],
                         property_id: str, recipient: str, fields: dict,
                         confirmed: str):
        """A send that has to come out of several addresses, in one decision.

        Every transaction is built and shown before any is broadcast, and
        what goes out is what was shown -- the same rule as a single send
        (D-016), with one yes for the set. They are independent of each
        other, so one failing does not invalidate the rest; what did go is
        named, because a partly-sent amount the person is not told about is
        the worst outcome there is.
        """
        chain, index = _token_chain()
        error, prepared, sent = None, [], []
        try:
            prop = index.property(int(property_id or 0))
            if prop is None:
                raise tokenlib.TokenError(f"there is no token {property_id}.")
            to = _recipient(recipient)
            held = state.prepared_tokens.get((chain.network, confirmed)) if confirmed \
                else None
            with chain.rpc() as rpc:
                sender_obj = tokenlib.TokenSender(rpc, chain.params)
                if isinstance(held, list):
                    for one in held:
                        sent.append(sender_obj.broadcast(one))
                        state.pending_tokens.append(
                            {"txid": sent[-1], "what": "send", "at": time.time(),
                             "network": chain.network})
                    state.prepared_tokens.pop((chain.network, confirmed), None)
                    state.flash(
                        f"Sent as {len(sent)} transactions, out of "
                        f"{len(sent)} addresses: {', '.join(t[:12] + '…' for t in sent)}.",
                        "ok")
                    return RedirectResponse("/wallet/tokens", status_code=303)
                for address, units in parts:
                    prepared.append(sender_obj.prepare(
                        address, tokenlib.send_payload(prop["property_id"], units), to))
                    prepared[-1].what = "send"
            plan = prepared[0].txid
            state.prepared_tokens[(chain.network, plan)] = prepared
            while len(state.prepared_tokens) > 20:
                del state.prepared_tokens[next(iter(state.prepared_tokens))]
        except HTTPException:
            raise
        except (tokenlib.TokenError, AmountError, ValueError, SendError) as exc:
            error, prepared = str(exc), []
        except Exception as exc:
            error, prepared = f"{exc.__class__.__name__}: {exc}", []
        context = dict(error=error, prepared=None, parts=prepared,
                       part_amounts=[units for _, units in parts],
                       confirm_action=request.url.path, confirm_fields=fields,
                       confirm_what="send", back="/wallet/tokens",
                       form_send=fields, tab="tokens")
        context.update(_token_page_data())
        context["tab"] = "tokens"
        return render(request, "wallet_tokens.html", **context)

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
            "face": _faces_for(index, [prop])[property_id],
            "pad": _pad_on(index, chain, property_id),
            "node_error": node_error,
        }

    def _pad_on(index, chain, property_id: int) -> dict[str, Any] | None:
        """The launchpad selling this token, if one is open (D-107)."""
        try:
            shops = _shop_listings(index, chain)
        except Exception:
            return None
        for shop in shops:
            for listing in shop["listings"]:
                give = listing["give"]
                if give.get("kind") == "token" and \
                        int(give.get("propertyid") or 0) == int(property_id):
                    return {"txid": shop["txid"], "seller": shop["seller"],
                            "text": listing["text"],
                            "available": listing["available"]}
        return None

    @app.get("/tokens/launchpad/preview")
    def preview_launchpad(name: str = "", lot: str = "", price: str = "",
                          icon: str = "", about: str = ""):
        """The launchpad page as it will be inscribed, before anything is.

        Built from what the form says rather than from the chain, which is the
        whole point: a page that does not exist yet can still be looked at.
        Buying is off, because there is nothing to buy from.
        """
        page = tokenpadlib.page(name or "A token", lot or "?", price or "?",
                                icon=icon, about=about).decode("utf-8")
        page += ("<script>addEventListener('DOMContentLoaded',function(){"
                 "var b=document.getElementById('buy');"
                 "if(b){b.disabled=true;b.textContent='Preview \u2014 not on the chain yet';}"
                 "var l=document.getElementById('left');"
                 "if(l){l.textContent='This is the page that will be inscribed.';}});"
                 "</script>")
        return HTMLResponse(page, headers={"Cache-Control": "no-store"})

    @app.post("/tokens/{property_id}/launchpad", response_class=HTMLResponse)
    def make_launchpad(request: Request, property_id: int, lot: str = Form(""),
                       amount: str = Form(""), kind: str = Form("coins"),
                       take_token: str = Form(""), confirmed: str = Form(""),
                       csrf_token: str = Form("")):
        """Inscribe the page that sells this token, from the issuer's address.

        Two presses, like everything else that spends: the first prices the
        page, the second inscribes it. It can only be made after the token
        exists, because a shop names the token by the id the engine gave it
        -- which is why this is on the token's own page rather than on the
        form that creates one (D-107).
        """
        check_csrf(csrf_token)
        chain, index = _token_chain()
        prop = index.property(property_id)
        if prop is None:
            raise HTTPException(status_code=404, detail=f"no token {property_id}")
        extra: dict[str, Any] = {"pad_lot": lot, "pad_amount": amount,
                                 "pad_kind": kind, "pad_take_token": take_token}
        try:
            from_address = state.home_address(chain)
            units = parse_amount(lot, bool(prop["divisible"]))
            if units <= 0:
                raise tokenlib.TokenError("say how many go in one sale.")
            held = index.balance(from_address, property_id)
            if held < units:
                raise tokenlib.TokenError(
                    f"{from_address} holds "
                    f"{format_amount(held, bool(prop['divisible']))} {prop['name']}, "
                    f"so it cannot sell "
                    f"{format_amount(units, bool(prop['divisible']))} at a time.")
            take = mintpadlib.take_of(kind, amount,
                                      int(take_token) if take_token else None)
            price = swaplib.describe_leg(
                swaplib.leg_json(swaplib.leg_of(take, index), index))
            face = _faces_for(index, [prop])[property_id]
            identity = state.ensure_identity()
            plan = inscribelib.plan(
                tokenpadlib.page(prop["name"], lot.strip(), price,
                                 icon=face["icon"], about=face["about"]),
                "text/html",
                tokenpadlib.shop_json(
                    contact.encode(state.messaging.network, identity.public_bytes),
                    property_id, prop["name"], lot.strip(), take))
            if confirmed == "yes":
                # An inscription, not a token transaction: written from the
                # address that holds the tokens, because a shop's seller is
                # the shop inscription's own owner and every buyer's node
                # checks it still holds what it sells.
                _inscribe_in_background(chain, from_address, plan,
                                        f"{prop['name']} Launchpad")
                state.flash(
                    f"Inscribing the {prop['name']} launchpad: {lot.strip()} for "
                    f"{price} a sale. It is open from the block it lands in, and "
                    f"appears in the Exchange by itself.", "ok")
                return RedirectResponse(f"/tokens/{property_id}", status_code=303)
            extra.update(pad_plan=plan, pad_price=price)
        except HTTPException:
            raise
        except Exception as exc:
            state.flash(str(exc), "err")
        return render(request, "token.html", prepared=None,
                      **_token_detail(property_id), **extra)

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
        "/exchange": ("Exchange", "M3",
                      "Two-sided MetaDEx order book: bids, asks, on-chain matching, "
                      "partial fills and price charts."),
    }

    def _unbuilt(request: Request, path: str) -> HTMLResponse:
        section, milestone, detail = UNBUILT[path]
        return render(request, "unbuilt.html", section=section, milestone=milestone,
                      detail=detail, ledger=ledger_status())

    # --- the exchange ---------------------------------------------------------

    EXCHANGE_TABS = ("offers", "mintpads", "tokens", "market")

    def _pairs(index, trades) -> list[dict[str, Any]]:
        """Tokens against the coin, most traded first."""
        wanted = {p["property_id"]: p for p in _token_props(index)}
        faces = _faces_for(index, wanted.values())
        out = []
        for pid, prop in wanted.items():
            points = chartlib.token_prices(trades, pid)
            book = index.book(pid, limit=1)
            if not points and not (book["asks"] or book["bids"]):
                continue
            stats = chartlib.day(points)
            out.append({
                "property_id": pid, "name": prop["name"],
                "divisible": prop["divisible"],
                "icon": faces[pid]["icon"], "about": faces[pid]["about"],
                "last": stats["last"], "change": stats["change"],
                "high": stats["high"], "low": stats["low"],
                "trades": stats["trades"], "volume": stats["volume"],
                "coins": stats["coins"],
                "ask": book["asks"][0]["price"] if book["asks"] else None,
                "bid": book["bids"][0]["price"] if book["bids"] else None,
            })
        # The market people are actually trading, first -- a table of pairs
        # is read from the top, and the top should be where the trading is.
        out.sort(key=lambda p: (-p["coins"], -p["trades"], p["name"].lower()))
        return out

    def _token_props(index) -> list[dict[str, Any]]:
        try:
            return index.properties()
        except Exception:
            return []

    #: What a wallet needs to take part in a swap: one output to spend in the
    #: swap itself, and another to pay for the message that carries it.
    OUTPUTS_FOR_A_SWAP = 2

    def _too_few_outputs(rpc, address: str) -> str:
        """Why this address cannot buy yet, or "" if it can.

        Every swap is two transactions from the buyer's side -- the half they
        sign, and the message carrying it -- and each needs an output of its
        own, confirmed. A wallet with one output discovers that at the last
        step, after paying for an order and waiting for an offer, which is
        the worst moment to find out (D-051).
        """
        try:
            outputs = [u for u in (rpc.call("listunspent", 1, 9_999_999, [address]) or [])
                       if u.get("spendable", True)]
        except Exception:
            return ""              # cannot tell; let the usual path decide
        if len(outputs) >= OUTPUTS_FOR_A_SWAP:
            return ""
        return (f"{address} has its coins in "
                f"{len(outputs)} confirmed output{'' if len(outputs) == 1 else 's'}, "
                f"and a swap needs two: one to put into the trade and one to "
                f"pay for the message that carries it. Split it first -- "
                f"Wallet, Fast sending -- and this will go through without "
                f"waiting for a block between each step")

    def _shop_listings(index, chain) -> list[dict[str, Any]]:
        """Every shop on this chain, with its listings as the pages see them.

        Read from the chain each time rather than from a list somebody
        keeps: a shop closes by its inscription being sent away, and a
        directory that had to be told would go on advertising it (D-037).
        """
        from .. import swap as swaplib
        out = []
        for row in index.shops():
            try:
                swaplib.shop_of(row)
                listings = swaplib.listings_json(row, index)
            except Exception:
                continue          # JSON that names a shop and is not one
            if not listings:
                continue
            name = ""
            try:
                name = str(json.loads(row["json"] or "{}").get("name") or "")
            except Exception:
                name = ""
            # A face for the card: a random piece of the collection it sells,
            # or the piece itself when it sells exactly one (D-041).
            thumb = None
            for listing in listings:
                give = listing["give"]
                if give.get("kind") == "random":
                    found = index.collection_thumb(row["creator"],
                                                   give.get("collection") or "")
                    thumb = (found or {}).get("txid")
                elif give.get("kind") == "inscription":
                    piece = index.inscription(give.get("txid") or "")
                    if piece and piece["held"] and str(
                            piece["content_type"] or "").startswith("image/"):
                        thumb = piece["txid"]
                if thumb:
                    break
            out.append({"txid": row["txid"], "number": row["number"],
                        "name": name, "seller": row["owner"], "thumb": thumb,
                        "mine": False, "listings": listings})
        return out

    #: How many collections the NFT marketplace lists. A market people can
    #: read, not a directory: the rest are a page away on /collections.
    MARKET_COLLECTIONS = 60

    def _nft_listings(index, chain) -> dict[str, dict[str, Any]]:
        """Every single NFT a shop is selling right now, by the piece's txid.

        Read from the shops on the chain each time, like everything else on
        the Exchange (D-037): a piece whose seller has sent it away is not
        for sale any more, and nothing had to be told.
        """
        out: dict[str, dict[str, Any]] = {}
        for shop in _shop_listings(index, chain):
            for listing in shop["listings"]:
                give = listing["give"]
                if give.get("kind") != "inscription" or listing["available"]:
                    continue
                # First shop wins: two shops may name the same piece, and only
                # one of them can be given -- the buyer's node checks which
                # when it reads the terms.
                take = listing["take"]
                out.setdefault(give["txid"], {
                    "shop": shop["txid"], "seller": shop["seller"],
                    "price": swaplib.describe_leg(take),
                    # A floor is a number, and only a coin price is one that
                    # can be compared: a piece priced in a token is priced in
                    # another market (D-040).
                    "sats": take.get("sats") if take.get("kind") == "coins" else None,
                    "number": give.get("number"),
                    "collection": give.get("collection"),
                    "edition": give.get("edition")})
        return out

    def _prices_for(index, chain) -> dict[str, dict[str, Any]]:
        """Every NFT with a price on it right now, by the piece's txid.

        Two ways a price gets said, and both are read from the chain. An ASK
        is the holder's own word, one OP_RETURN, and the ordinary way
        (D-099). A SHOP is an inscription whose JSON sells a piece -- how it
        was done before asks existed, and still how a mintpad sells a whole
        collection -- so those are read too rather than made to disappear.
        The ask wins where a piece has both: it is the newer statement, and
        it is the one its holder made.
        """
        out: dict[str, dict[str, Any]] = {}
        try:
            standing = index.asks(limit=500)
        except Exception:
            standing = []
        # The mempool first, then the blocks. A price is a statement, and a
        # marketplace that shows nothing for ten minutes after somebody makes
        # one looks as though the listing failed -- which is the same
        # complaint offers answered in D-058, met from the seller's side
        # (D-117). A pending ask is marked as pending and says so.
        try:
            fresh = index.pending_asks()
        except Exception:
            fresh = []
        in_pool = {row["inscription"] for row in fresh}
        standing = fresh + [a for a in standing if a["inscription"] not in in_pool]
        for ask in standing:
            take = _take_json(ask, index)
            out[ask["inscription"]] = {
                "kind": "ask", "shop": None, "seller": ask["seller"],
                "price": swaplib.describe_leg(take), "take": take,
                "sats": take.get("sats") if take.get("kind") == "coins" else None,
                "number": ask["number"], "collection": ask["collection"],
                "edition": ask["edition"], "when": ask.get("when_"),
                "pending": bool(ask.get("pending"))}
        for txid, entry in _nft_listings(index, chain).items():
            entry = dict(entry, kind="shop", take=None, when=None, pending=False)
            out.setdefault(txid, entry)
        return out

    def _nft_points(index, trades) -> dict[tuple[str, str] | None,
                                           list[dict[str, Any]]]:
        """What NFTs have sold for in coins, bucketed by collection.

        Keyed by (creator, name), because that is what a collection IS: two
        people may inscribe a set called Doge Punks, and keying on the name
        alone would give them one price history, one chart and one floor
        between them. Found by a test machine reading the function against its own
        comment three lines down, where `for_sale` is keyed correctly --
        invisible on a chain with one collection, and wrong on the day there
        are two.

        One pass over the trades. Asking chartlib for each collection's
        prices asks the index about every trade again -- a hundred
        collections and five hundred trades is fifty thousand questions to
        draw one table.
        """
        points: dict[tuple[str, str] | None, list[dict[str, Any]]] = {}
        for trade in trades:
            legs = (trade["give"], trade["take"])
            piece = next((l for l in legs
                          if l.kind == inscriptionlib.LEG_INSCRIPTION), None)
            paid = next((l for l in legs
                         if l.kind == inscriptionlib.LEG_COINS), None)
            if piece is None or paid is None:
                continue          # a piece paid for in tokens is its own market
            try:
                row = index.inscription(piece.txid.hex())
            except Exception:
                row = None
            where = None
            if row and row.get("collection"):
                where = (row["creator"], row["collection"])
            points.setdefault(where, []).append({
                "when": trade["when"], "height": trade["height"],
                "price": (paid.amount or 0) / COIN, "size": 1,
                "txid": trade["txid"]})
        return points

    def _just_listed(index, limit: int = 12) -> list[dict[str, Any]]:
        """The prices most recently put on a piece, newest first.

        The book of asks, as a marketplace shows one: what somebody can buy
        right now, whether or not it belongs to a collection (D-099). The
        mempool first, so a price that has just been made is here rather than
        in ten minutes' time (D-117).
        """
        out = []
        try:
            fresh = index.pending_asks()
        except Exception:
            fresh = []
        in_pool = {row["inscription"] for row in fresh}
        standing = fresh + [a for a in index.asks(limit=limit * 3)
                            if a["inscription"] not in in_pool]
        for ask in standing:
            row = index.inscription(ask["inscription"])
            if row is None:
                continue
            take = _take_json(ask, index)
            out.append({
                "txid": ask["inscription"], "number": ask["number"],
                "name": _piece_name(row), "collection": ask["collection"],
                "edition": ask["edition"], "creator": ask["creator"],
                "seller": ask["seller"], "held": ask["held"],
                "content_type": ask["content_type"],
                "price": swaplib.describe_leg(take), "take": take,
                "when": ask.get("when_"), "height": ask["block_height"],
                "pending": bool(ask.get("pending"))})
            if len(out) >= limit:
                break
        return out

    def _recent_sales(index, trades, limit: int = 12) -> list[dict[str, Any]]:
        """The last NFTs to change hands, newest first.

        Read from the swaps the chain holds, like everything else here: a
        sale is a transaction both sides signed, so there is no list to keep
        and nothing to believe (D-039). The buyer is not in the swap row --
        what is known is who sold it and who holds it now, which for a sale
        this recent is the same answer.
        """
        out: list[dict[str, Any]] = []
        for trade in sorted(trades, key=lambda t: (-(t["height"] or 0),
                                                   -(t["when"] or 0))):
            legs = (trade["give"], trade["take"])
            piece = next((l for l in legs
                          if l.kind == inscriptionlib.LEG_INSCRIPTION), None)
            paid = next((l for l in legs
                         if l.kind != inscriptionlib.LEG_INSCRIPTION), None)
            if piece is None or paid is None:
                continue
            txid = piece.txid.hex()
            try:
                row = index.inscription(txid)
                price = swaplib.describe_leg(swaplib.leg_json(paid, index))
            except Exception:
                continue
            if row is None:
                continue
            data = _fromjson(row["json"]) or {}
            out.append({
                "txid": txid, "number": row["number"],
                "name": (data.get("name") if isinstance(data, dict) else None)
                        or f"Inscription #{row['number']}",
                "collection": row["collection"], "edition": row["edition"],
                "creator": row["creator"], "owner": row["owner"],
                "held": row["held"], "content_type": row["content_type"],
                "price": price, "when": trade["when"], "height": trade["height"],
                "seller": trade["seller"], "swap": trade["txid"]})
            if len(out) >= limit:
                break
        return out

    def _drawable(index, txid: str) -> str:
        """That inscription's id, if this node holds bytes a browser will draw."""
        if not txid:
            return ""
        try:
            row = index.inscription(txid)
        except Exception:
            return ""
        if row and row["held"] and str(row["content_type"] or "").startswith("image/"):
            return row["txid"]
        return ""

    def _face_of(index, creator: str, name: str) -> tuple[str, dict[str, Any]]:
        """A collection's picture and what it says about itself.

        #1 by default, because that is the piece a set is known by -- but a
        creator who wants a different face says so on that same #1, and this
        prefers what they said (D-103). Either way it is an inscription this
        node can actually draw, or nothing.
        """
        cover = index.collection_cover(creator, name) or {}
        about = inscriptionlib.collection_details(cover.get("json") or "")
        chosen = _drawable(index, about.get("icon", ""))
        if not chosen and str(cover.get("content_type") or "").startswith("image/"):
            chosen = cover.get("txid") or ""
        return chosen, dict(about, edition=cover.get("edition"))

    def _market_collections(index, chain, trades) -> list[dict[str, Any]]:
        """Every collection on this chain as a market of its own.

        The same table the Tokens tab draws for pairs, because a collection
        IS the pair here: what a Goofball goes for says nothing about what a
        Doge Punk goes for (D-040). Its face is #1 rather than a random
        member -- a market row is a name people are meant to recognise
        (D-096).
        """
        listed = _prices_for(index, chain)
        # Which set each listed piece belongs to. The listing carries the
        # collection's name but not whose it is, and a collection is (creator,
        # name) -- two people may inscribe a set called Doge Punks.
        for_sale: dict[tuple[str, str], int] = {}
        floors: dict[tuple[str, str], int] = {}
        for txid, entry in listed.items():
            try:
                row = index.inscription(txid)
            except Exception:
                row = None
            if row and row.get("collection"):
                key = (row["creator"], row["collection"])
                for_sale[key] = for_sale.get(key, 0) + 1
                if entry["sats"] and entry["sats"] < floors.get(key, 1 << 62):
                    floors[key] = entry["sats"]
        try:
            offers = index.collection_offers()
        except Exception:
            offers = {}
        points = _nft_points(index, trades)
        out = []
        for row in index.collections(limit=MARKET_COLLECTIONS):
            key = (row["creator"], row["collection"])
            face, about = _face_of(index, *key)
            mine = points.get(key, [])
            stats = chartlib.day(mine)
            out.append({
                "creator": row["creator"], "name": row["collection"],
                "count": row["count"],
                "cover": face or None,
                "cover_edition": about.get("edition"),
                "about": about.get("description", ""),
                "first_number": row["first_number"],
                "last_number": row["last_number"],
                "for_sale": for_sale.get(key, 0),
                "floor": floors.get(key),
                "offers": offers.get(key, 0),
                "last": stats["last"], "change": stats["change"],
                "day_coins": stats["coins"], "day_trades": stats["trades"],
                "volume": sum(p["price"] for p in mine),
                "trades": len(mine)})
        # What can be acted on, first: a market lists the collections
        # somebody is selling from before the ones nobody is.
        out.sort(key=lambda c: (-c["for_sale"], -c["offers"], -c["trades"],
                                c["name"].lower()))
        return out

    def _names_for(index, addresses) -> dict[str, str]:
        """Which of these addresses hold a tag, asked of the chain they are on.

        A tag is claimed per chain and the same name on two chains is two
        names (D-032). A piece and whoever holds it are on the chain being
        shown, so that index answers first; the messaging chain is asked for
        what is left, which is where this wallet claims its own name when the
        two chains are different.
        """
        wanted = sorted({a for a in addresses if a})
        if not wanted:
            return {}
        try:
            found = dict(index.tags_for(wanted))
        except Exception:
            found = {}
        missing = [a for a in wanted if a not in found]
        if missing:
            found.update(_tags_for(missing))
        return found

    def _faces_for(index, props) -> dict[int, dict[str, str]]:
        """What each token looks like: its icon, its description, its link.

        The icon is an inscription (tokens.details), so it is only shown when
        THIS node holds its bytes and a browser will draw them -- an <img>
        pointing at content nobody has is a broken picture in a table, which
        is worse than a token with no face at all (D-098).
        """
        out: dict[int, dict[str, str]] = {}
        for prop in props:
            face = tokenlib.details(prop)
            if face["icon"]:
                try:
                    row = index.inscription(face["icon"])
                except Exception:
                    row = None
                drawable = (row and row["held"]
                            and str(row["content_type"] or "").startswith("image/"))
                if not drawable:
                    face["icon"] = ""
            out[prop["property_id"]] = face
        return out

    def _offerable(chain, index):
        """What this wallet could offer with: its tokens, and its coins."""
        held: list[dict[str, Any]] = []
        coins = 0.0
        owned: set[str] = set()
        try:
            with chain.rpc() as rpc:
                owned = set(_ledger_addresses(rpc))
                held = _purses(index.balances(sorted(owned)))
                coins = float(rpc.call("getbalance") or 0)
        except HTTPException:
            raise
        except Exception:
            pass
        return owned, held, coins

    @app.get("/exchange/pair/{property_id}", response_class=HTMLResponse)
    def exchange_pair(request: Request, property_id: int):
        """One pair: its chart, its book, and the form that adds to the book."""
        chain, index = _token_chain()
        prop = index.property(property_id)
        if prop is None:
            state.flash(f"there is no token {property_id}", "err")
            return RedirectResponse("/exchange?tab=tokens", status_code=303)
        try:
            trades = index.trades()
        except Exception:
            trades = []
        points = chartlib.token_prices(trades, property_id)
        book = index.book(property_id)
        owned, held, coins = set(), 0, 0.0
        try:
            with chain.rpc() as rpc:
                owned = set(_ledger_addresses(rpc))
                coins = float(rpc.call("getbalance") or 0)
            held = sum(index.balance(a, property_id) for a in owned)
        except HTTPException:
            raise
        except Exception:
            pass
        for side in ("asks", "bids"):
            for order in book[side]:
                order["mine"] = order["address"] in owned
                order["price_shown"] = f"{float(order['price']):.8f}".rstrip("0").rstrip(".")
                order["tokens_shown"] = format_amount(order["tokens"], prop["divisible"])
                order["coins_shown"] = format_amount(order["coins"], True)
        # Depth behind each row of the book, as a share of the largest resting
        # order on that side: a book is read at a glance, and the glance is
        # where the size is.
        for side in ("asks", "bids"):
            biggest = max((o["tokens"] for o in book[side]), default=0)
            for order in book[side]:
                order["depth"] = round(100 * order["tokens"] / biggest, 1) if biggest else 0
        spread = None
        if book["asks"] and book["bids"]:
            spread = float(book["asks"][0]["price"]) - float(book["bids"][0]["price"])
        face = _faces_for(index, [prop])[property_id]
        return render(request, "pair.html", chain=chain, prop=prop, book=book,
                      face=face, spread=spread, day=chartlib.day(points),
                      stats=chartlib.last_and_change(points),
                      slots=chartlib.candles(points),
                      recent=sorted(points, key=lambda p: -p["when"])[:12],
                      held=format_amount(held, prop["divisible"]), held_units=held,
                      coins=coins, mine=index.orders_of(sorted(owned)),
                      fills=state.offers.fills(chain.network, limit=8),
                      fills_from=chain.params.fills_from,
                      fills_ready=(chain.params.fills_from is not None
                                   and (index.indexed_height() or 0)
                                   >= chain.params.fills_from),
                      height=index.indexed_height(),
                      tags=_tags_for([o["address"] for o in book["asks"] + book["bids"]]))

    @app.post("/exchange/order")
    def place_order(request: Request, property_id: str = Form(""),
                    side: str = Form("ask"), amount: str = Form(""),
                    price: str = Form(""), csrf_token: str = Form("")):
        """Put an order on the book: this much, at this price, until cancelled.

        Two integers, never a float: the amount of token and the amount of
        coin are what go on the chain, and the price is the ratio between
        them. What is sold is held back by the engine if it is a token; a bid
        holds nothing, because coins cannot be reserved (D-048).
        """
        check_csrf(csrf_token)
        chain, index = _token_chain()
        try:
            prop = index.property(int(property_id or 0))
            if prop is None:
                raise tokenlib.TokenError(f"there is no token {property_id}.")
            units = parse_amount(amount, prop["divisible"])
            each = parse_amount(price, True)
            if units <= 0 or each <= 0:
                raise tokenlib.TokenError("an amount and a price, both above zero.")
            coins = units * each // COIN
            if coins <= 0:
                raise tokenlib.TokenError(
                    "that comes to less than a satoshi in coins; raise the price "
                    "or the amount.")
            with chain.rpc() as rpc:
                own = _ledger_addresses(rpc)
                home = state.home_address(chain)
                if side == "ask":
                    held = index.balance(home, prop["property_id"])
                    if held < units:
                        raise tokenlib.TokenError(
                            f"{home} holds "
                            f"{format_amount(held, prop['divisible'])} "
                            f"{prop['name']}, not {format_amount(units, prop['divisible'])}.")
                    message = P.MetaDExTrade(
                        property_id_for_sale=prop["property_id"], amount_for_sale=units,
                        property_id_desired=0, amount_desired=coins)
                else:
                    message = P.MetaDExTrade(
                        property_id_for_sale=0, amount_for_sale=coins,
                        property_id_desired=prop["property_id"], amount_desired=units)
                sender = tokenlib.TokenSender(rpc, chain.params)
                prepared = sender.prepare(home, message.encode())
                txid = sender.broadcast(prepared)
            state.flash(
                f"Order on the book in {txid}: "
                f"{'sell' if side == 'ask' else 'buy'} "
                f"{format_amount(units, prop['divisible'])} {prop['name']} for "
                f"{format_amount(coins, True)} coins. It stands until you cancel "
                f"it.", "ok")
        except HTTPException:
            raise
        except Exception as exc:
            state.flash(str(exc), "err")
        return RedirectResponse(f"/exchange/pair/{property_id}", status_code=303)

    @app.post("/exchange/order/cancel")
    def cancel_orders(request: Request, property_id: str = Form(""),
                      side: str = Form(""), csrf_token: str = Form("")):
        """Take this wallet's orders off one side of one pair."""
        check_csrf(csrf_token)
        chain, index = _token_chain()
        try:
            pid = int(property_id or 0)
            if index.property(pid) is None:
                raise tokenlib.TokenError(f"there is no token {property_id}.")
            sale, want = (pid, 0) if side == "ask" else (0, pid)
            message = P.MetaDExCancelPair(property_id_for_sale=sale,
                                          property_id_desired=want)
            with chain.rpc() as rpc:
                sender = tokenlib.TokenSender(rpc, chain.params)
                prepared = sender.prepare(state.home_address(chain), message.encode())
                txid = sender.broadcast(prepared)
            state.flash(f"Cancelling in {txid}. What it was holding comes back "
                        f"when the block lands.", "ok")
        except HTTPException:
            raise
        except Exception as exc:
            state.flash(str(exc), "err")
        return RedirectResponse(f"/exchange/pair/{property_id}", status_code=303)

    def _merge_offers(pending: list[dict], confirmed: list[dict]) -> list[dict]:
        """Mempool offers in front of confirmed ones, each transaction once."""
        seen = {o["txid"] for o in pending}
        return pending + [o for o in confirmed if o["txid"] not in seen]

    @app.post("/exchange/offer", response_class=HTMLResponse)
    def make_offer_on(request: Request, inscription: str = Form(""),
                      amount: str = Form(""), kind: str = Form("coins"),
                      property_id: str = Form(""), csrf_token: str = Form(""),
                      back: str = Form("")):
        """Offer for an NFT, whoever holds it and whatever they have listed.

        The offer is a message to the wallet that holds it. Accepting is
        theirs to do, and what they accept is exactly these terms: the
        wallet checks the shop's answer against them before it signs, so a
        yes here cannot be turned into a different trade (D-038).
        """
        check_csrf(csrf_token)
        chain, index = _token_chain()
        try:
            row = index.inscription(contentlib._key(inscription))
            if row is None:
                raise swaplib.SwapError("no such inscription on this node")
            with chain.rpc() as rpc:
                own = _ledger_addresses(rpc)
                if row["owner"] in own:
                    raise swaplib.SwapError("that one is already yours")
                take = swaplib.leg_of(
                    mintpadlib.take_of(kind, amount,
                                       int(property_id) if property_id else None),
                    index)
                buyer = _buyer_for(rpc, index, own, take)
            # The holder answers by message, so this wallet has to be
            # reachable before it spends a fee asking (D-042).
            with state.store() as store:
                if store.key_for(buyer) is None and not _own_announcement(buyer):
                    raise swaplib.SwapError(
                        "publish your key first, from the address book, or "
                        "whoever holds this cannot answer you. It costs a "
                        "small fee and is done once.")
            # Said on the chain, not sent as a message: whoever holds an NFT
            # never asked to be reachable, and most have published no key at
            # all. Their own node finds this by watching their own things
            # (D-042). It fits one OP_RETURN, so it is a flat fee.
            payload = P.AnyData(data=inscriptionlib.Offer(
                txid=bytes.fromhex(row["txid"]), take=take).encode()).encode()
            with chain.rpc() as rpc:
                sender = tokenlib.TokenSender(rpc, chain.params)
                prepared = sender.prepare(buyer, payload)
                txid = sender.broadcast(prepared)
            # Written down here as well as on the chain. The chain is what an
            # answer is checked against (D-049) -- this row is so the wallet
            # can show what it has asked for before the block lands, and so
            # a reply that arrives first still finds its terms.
            now = time.time()
            state.offers.add_bid({
                "id": txid, "network": chain.network, "direction": "out",
                "inscription": row["txid"], "number": row["number"],
                "owner": row["owner"], "buyer": buyer, "peer_pubkey": "",
                "take": swaplib.leg_json(take, index),
                "created": now, "expires": now + swaplib.OFFER_TTL * 24})
            state.flash(f"Offer made in {txid}. It stands from the block it is "
                        f"in; whoever holds it sees it in their own Exchange.", "ok")
            return RedirectResponse("/exchange?tab=offers", status_code=303)
        except HTTPException:
            raise
        except Exception as exc:
            state.flash(str(exc), "err")
            # Back to the page the offer was made from -- a card in a
            # collection, usually, and being dropped onto the piece's own
            # page loses the place. Only this application's own paths: a
            # form field is whatever was posted.
            where = back if back.startswith("/") and not back.startswith("//") \
                else f"/inscriptions/{inscription}/view"
            return RedirectResponse(where, status_code=303)

    def _own_announcement(address: str) -> bool:
        """Whether this wallet has published a key at this address.

        Its own announcement is not in the address book -- that is other
        people -- so the store is asked about the key itself.
        """
        if not state.unlocked:
            return False
        with state.store() as store:
            rows = store.all_keys()
        mine = state.identity.public_bytes
        return any(bytes(row["pubkey"]) == mine and row["address"] == address
                   for row in rows)

    def _take_json(entry: dict[str, Any], index) -> dict[str, Any]:
        """An offer's price, as the pages and the wallet both say it."""
        leg = inscriptionlib.Leg(int(entry["take_kind"]),
                                 property_id=int(entry["take_property"] or 0),
                                 amount=int(entry["take_amount"] or 0))
        try:
            return swaplib.leg_json(leg, index)
        except Exception:
            return {"kind": "unknown", "amount": entry["take_amount"]}

    def _key_at(address: str) -> bytes:
        """The messaging key the wallet at this address has announced.

        Without one there is nobody to send an offer to: an address is a
        place to pay, not somewhere a message can be read.
        """
        with state.store() as store:
            row = store.key_for(address)
        if row is None:
            raise swaplib.SwapError(
                f"{address} has not published a messaging key, so there is "
                "nobody to send an offer to. Ask them to publish one from "
                "their address book.")
        return bytes(row["pubkey"])

    @app.post("/exchange/offers/{offer_txid}")
    def decide_offer(request: Request, offer_txid: str, decision: str = Form(""),
                     csrf_token: str = Form("")):
        """Accept an offer on something of yours, or refuse it.

        Accepting builds the same half of the same swap a shop would: the
        item, the price, one of this wallet's outputs locked to carry it.
        The answer goes back to the buyer, whose wallet signs its half
        against the terms it offered and hands it back to be broadcast.
        """
        check_csrf(csrf_token)
        chain, index = _token_chain()
        try:
            with chain.rpc() as rpc:
                own = _ledger_addresses(rpc)
                standing = [o for o in _merge_offers(
                                [p for p in index.pending_offers()
                                 if p["owner"] in set(own)],
                                index.offers_on(sorted(own)))
                            if o["txid"] == offer_txid]
                if not standing:
                    raise swaplib.SwapError(
                        "no such offer on anything of yours -- it may have been "
                        "made for a piece that has since moved")
                found = standing[0]
                # The buyer has to be reachable for this to finish: the
                # seller's half goes back to them as a message. The wallet
                # refuses to MAKE an offer without a published key for the
                # same reason, so this is the rare case of an offer made by
                # something else (D-042).
                to = _key_at(found["buyer"])
                bid = {"inscription": found["inscription"],
                       "take": _take_json(found, index),
                       "buyer": found["buyer"], "peer_pubkey": to.hex()}
                offer = swaplib.offer_for_bid(rpc, index, state.offers,
                                              chain.network, bid, own=own)
            _page_send(found["inscription"], chain, to,
                       json.dumps({"swap": "bid", "swapv": swaplib.PROTOCOL,
                                   "id": offer_txid, "ok": True,
                                   "offer": offer}).encode())
            state.flash("Accepted. Their wallet signs its half and it goes as one "
                        "transaction; nothing moves unless both halves do.", "ok")
        except HTTPException:
            raise
        except Exception as exc:
            state.flash(str(exc), "err")
        return RedirectResponse("/exchange?tab=offers", status_code=303)

    def _earliest_at_or_better(index, clicked: dict, tokens: int) -> dict | None:
        """The order a fill should actually take, given the one pressed.

        The book is already sorted price-then-time, so this is the first row
        that is not worse on price, has enough left, is not this wallet's, and
        belongs to somebody this node can send a message to. An unreachable
        maker is skipped rather than refused: their order stands, but it
        cannot be negotiated with, and stopping the queue on it would let one
        unreachable wallet block a price for everybody (D-042).
        """
        from fractions import Fraction

        try:
            book = index.book(int(clicked["sale_property"]))["asks"]
        except Exception:
            return None
        want = Fraction(clicked["want_amount"], clicked["sale_amount"])
        for row in book:
            if row.get("pending") or row["txid"] == clicked["txid"]:
                continue
            if Fraction(row["want_amount"], row["sale_amount"]) > want:
                break                     # sorted, so nothing after is better
            if row["sale_amount"] < tokens:
                continue
            try:
                _key_at(row["address"])
            except Exception:
                continue                  # nobody to ask; leave their order be
            return row
        return None

    @app.post("/exchange/fill")
    def fill_order(request: Request, order: str = Form(""), amount: str = Form(""),
                   property_id: str = Form(""), csrf_token: str = Form("")):
        """Take part of a price off the book.

        The taker asks the maker's node for the one thing it cannot work out
        alone -- which of the maker's outputs will carry the swap -- and gets
        back an offer at the maker's own price (D-063). The answer is checked
        against this note and against the order as this node reads it off the
        chain, and then this wallet signs. Pressing this button is the
        agreement; there is no second question.
        """
        check_csrf(csrf_token)
        chain, index = _token_chain()
        try:
            row = index.order(str(order))
            if row is None:
                raise swaplib.SwapError(
                    "that order is not on this node's book -- it may have been "
                    "cancelled or filled, or its block may not have arrived here")
            if row["want_property"] != 0:
                raise swaplib.SwapError(
                    "this fills an order that sells a token for coins. To fill a "
                    "bid, the wallet holding the tokens has to offer them")
            prop = index.property(row["sale_property"])
            tokens = parse_amount(str(amount), bool(prop["divisible"]))
            if not 0 < tokens <= row["sale_amount"]:
                raise swaplib.SwapError(
                    f"that order has {format_amount(row['sale_amount'], bool(prop['divisible']))} left")
            # The most this wallet will pay, worked out here from the chain so
            # the maker's answer is checked against our own arithmetic rather
            # than believed. Rounded up, which is what the engine's price guard
            # requires of a fill (D-062).
            # Price, then time. Somebody pressing Take on the third row at a
            # price is asking to buy at that price, not to choose which of
            # three identical offers gets the trade -- and the maker who
            # queued first is entitled to be filled first (D-083). So the
            # order actually taken is the earliest one at that price or
            # better with enough left, skipping any this wallet cannot reach.
            row = _earliest_at_or_better(index, row, tokens) or row
            prop = index.property(row["sale_property"])
            coins = -(-row["want_amount"] * tokens // row["sale_amount"])
            with chain.rpc() as rpc:
                own = _ledger_addresses(rpc)
                if row["address"] in own:
                    raise swaplib.SwapError("that order is your own")
                buyer = _buyer_for(rpc, index, own,
                                   swaplib.I.Leg(swaplib.I.LEG_COINS, amount=coins))
                short = _too_few_outputs(rpc, buyer)
                if short:
                    raise swaplib.SwapError(short)
            to = _key_at(row["address"])
            sent = _page_send(str(order), chain, to, json.dumps({
                "swap": "fill", "swapv": swaplib.PROTOCOL, "order": str(order),
                "tokens": tokens, "buyer": buyer}).encode())
            now = time.time()
            state.offers.add_fill({
                "id": sent["txid"], "network": chain.network, "order": str(order),
                "maker": row["address"], "buyer": buyer, "tokens": tokens,
                "coins": coins, "created": now, "expires": now + swaplib.OFFER_TTL})
            state.flash(
                f"Asked for {format_amount(tokens, bool(prop['divisible']))} "
                f"at {coins / COIN:.8f} "
                f"coins. Their node answers with its half; this wallet signs and "
                f"broadcasts. Nothing moves unless both halves do.", "ok")
        except HTTPException:
            raise
        except Exception as exc:
            state.flash(str(exc), "err")
        return RedirectResponse(f"/exchange/pair/{property_id or ''}", status_code=303)

    def _answer_bid(chain, bid: dict, body: dict) -> None:
        """Tell the buyer what became of their offer."""
        answer = {"swap": "bid", "swapv": swaplib.PROTOCOL, "id": bid["id"]}
        answer.update(body)
        try:
            _page_send(bid["inscription"], chain,
                       bytes.fromhex(bid["peer_pubkey"]),
                       json.dumps(answer).encode())
        except Exception as exc:
            log.warning("could not answer offer %s: %s", bid["id"], exc)

    @app.get("/exchange/collection/{creator}/{name}", response_class=HTMLResponse)
    def exchange_collection(request: Request, creator: str, name: str,
                            page: int = 1):
        """One collection, whole: what can be bought, then what was inscribed.

        Every card carries the two names that matter about a piece -- who
        made it and who holds it now -- and the way to offer for it. An offer
        can be made on any of them, listed or not (D-042), which is what
        makes this a market rather than a shop window.
        """
        chain, index = _token_chain()
        summary = index.collection(creator, name)
        if summary is None:
            state.flash("no such collection on this chain", "err")
            return RedirectResponse("/exchange?tab=market", status_code=303)
        data: dict[str, Any] = {"chain": chain, "node": chain.status(),
                                "summary": summary, "node_error": None,
                                "rows": [], "tags": {}, "tokens": [], "coins": 0.0,
                                "for_sale": 0, "offered": 0, "prices": None, "yours": [],
                                "owners": 0, "floor": None, "volume": 0.0, "traded": 0,
                                "cover": None, "about": {},
                                "page": page, "pages": 1,
                                "per_page": PAGE_INSCRIPTIONS}
        # What the set says about ITSELF, read from its #1 -- the piece a
        # collection is known by is where a description, a site and the rest
        # of it belong, rather than on all five hundred (inscriptions.py).
        face, about = _face_of(index, creator, name)
        data["cover"] = face or None
        data["about"] = about
        # What this collection has actually traded for, on its own page
        # rather than in the list of collections: a chart belongs beside the
        # pieces it prices.
        try:
            points = _nft_points(index, index.trades()).get((creator, name), [])
        except Exception:
            points = []
        if points:
            data["prices"] = {"unit": f"{chain.label} coins each",
                              "stats": chartlib.summary(points),
                              "slots": chartlib.candles(points)}
        listed: dict[str, dict[str, Any]] = {}
        try:
            listed = _prices_for(index, chain)
        except Exception as exc:
            data["node_error"] = f"the prices could not be read: {exc}"
        owned, data["tokens"], data["coins"] = _offerable(chain, index)
        try:
            data["pages"] = max(1, -(-summary["count"] // PAGE_INSCRIPTIONS))
            data["page"] = page = max(1, min(page, data["pages"]))
            # Cheapest first among what is for sale, so the first card of a
            # collection is its floor -- which is the number people come to
            # a marketplace for.
            selling = sorted(listed, key=lambda t: (listed[t]["sats"] is None,
                                                    listed[t]["sats"] or 0))
            rows = index.collection_market(
                creator, name, for_sale=selling,
                limit=PAGE_INSCRIPTIONS, offset=(page - 1) * PAGE_INSCRIPTIONS)
            for row in rows:
                row["listing"] = listed.get(row["txid"])
                row["mine"] = row["owner"] in owned
            data["rows"] = rows
            # Said about the whole collection, not about this page of it: the
            # header answers "is anything here for sale", and page four
            # saying no would be an answer about page four.
            mine_to_sell = [
                entry for txid, entry in listed.items()
                if entry.get("collection") == name
                and (index.inscription(txid) or {}).get("creator") == creator]
            data["for_sale"] = len(mine_to_sell)
            priced = [e["sats"] for e in mine_to_sell if e["sats"]]
            data["floor"] = min(priced) if priced else None
            data["offered"] = index.collection_offers().get((creator, name), 0)
            data["owners"] = index.collection_owners(creator, name)
            data["volume"] = sum(p["price"] for p in points)
            data["traded"] = len(points)
            # What this wallet holds of the set, asked of the whole set: it
            # is the answer to "what can I sell", and page four is not where
            # that is decided.
            data["yours"] = index.collection_held_by(creator, name, sorted(owned))
            for row in data["yours"]:
                row["listing"] = listed.get(row["txid"])
            data["tags"] = _names_for(
                index, [r["owner"] for r in rows] + [r["creator"] for r in rows]
                + [creator])
        except Exception as exc:
            data["node_error"] = data["node_error"] or f"the index could not be read: {exc}"
        return render(request, "market_collection.html", **data)

    # --- putting a price on one NFT ------------------------------------------
    #
    # An ask: one OP_RETURN saying "this piece, for this much", read by every
    # node and shown by every marketplace (inscriptions.Ask, D-099). It needs
    # no page of its own, nothing is locked by it, and it is written from the
    # address that holds the piece -- the engine refuses one from anybody
    # else, and `ledger.asks` drops it the moment the piece moves on.

    def _sell_page_data(row: dict[str, Any], chain, index, **extra) -> dict[str, Any]:
        data: dict[str, Any] = {
            "chain": chain, "node": chain.status(), "row": row,
            "name": _piece_name(row), "ask": None, "shop": None,
            # `error` and `prepared` are _token_action's to fill in, and are
            # deliberately absent here: a default of None passed as `extra`
            # overwrote the very error it was meant to leave room for, and
            # the page said nothing at all when the wallet refused.
            "tokens": [], "amount": "", "kind": "coins", "property_id": "",
            # An ask is read from a height, like everything else that makes
            # valid what used to be invalid. Said before the fee, not after.
            "asks_ready": (chain.params.asks_from is not None
                           and (index.indexed_height() or 0) >= chain.params.asks_from),
            "asks_from": chain.params.asks_from,
            "height": index.indexed_height(),
        }
        try:
            data["ask"] = index.ask_on(row["txid"])
        except Exception:
            data["ask"] = None
        try:
            data["shop"] = _nft_listings(index, chain).get(row["txid"])
        except Exception:
            data["shop"] = None
        try:
            data["tokens"] = index.properties()
        except Exception:
            data["tokens"] = []
        data.update(extra)
        return data

    def _piece_name(row: dict[str, Any]) -> str:
        """What to call a piece: what its own JSON calls it, else its number."""
        data = _fromjson(row.get("json")) or {}
        name = data.get("name") if isinstance(data, dict) else None
        return str(name) if name else f"Inscription #{row.get('number')}"

    @app.get("/exchange/sell/{key}", response_class=HTMLResponse)
    def sell_form(request: Request, key: str):
        """The form that puts a price on one piece."""
        chain, index = _token_chain()
        row = index.inscription(contentlib._key(key))
        if row is None:
            state.flash("no such inscription on this node", "err")
            return RedirectResponse("/wallet/nfts", status_code=303)
        return render(request, "sell.html", **_sell_page_data(row, chain, index))

    @app.post("/exchange/sell", response_class=HTMLResponse)
    def sell(request: Request, inscription: str = Form(""), amount: str = Form(""),
             kind: str = Form("coins"), property_id: str = Form(""),
             confirmed: str = Form(""), csrf_token: str = Form("")):
        """Two presses, like everything else here that spends: the first shows
        the transaction, the second broadcasts the one that was shown."""
        check_csrf(csrf_token)
        chain, index = _token_chain()
        row = index.inscription(contentlib._key(inscription))
        if row is None:
            state.flash("no such inscription on this node", "err")
            return RedirectResponse("/wallet/nfts", status_code=303)
        fields = dict(inscription=row["txid"], amount=amount, kind=kind,
                      property_id=property_id)

        def build(rpc):
            if row["owner"] not in _ledger_addresses(rpc):
                raise tokenlib.TokenError(
                    "only the wallet holding a piece can price it, and this "
                    f"one is held by {row['owner']}")
            take = swaplib.leg_of(
                mintpadlib.take_of(kind, amount,
                                   int(property_id) if property_id else None),
                index)
            payload = P.AnyData(data=inscriptionlib.Ask(
                txid=bytes.fromhex(row["txid"]), take=take).encode()).encode()
            # From the address that holds it: a shop's seller is its own
            # owner, and the engine refuses an ask from anybody else.
            return row["owner"], payload, None

        return _token_action(request, action="price", confirmed=confirmed,
                             build=build, fields=fields,
                             back="/exchange?tab=market", template="sell.html",
                             **_sell_page_data(row, chain, index, amount=amount,
                                               kind=kind, property_id=property_id))

    @app.post("/exchange/unlist", response_class=HTMLResponse)
    def unlist(request: Request, inscription: str = Form(""),
               confirmed: str = Form(""), csrf_token: str = Form("")):
        """Take the price off. A withdrawal is an ask with no price in it."""
        check_csrf(csrf_token)
        chain, index = _token_chain()
        row = index.inscription(contentlib._key(inscription))
        if row is None:
            state.flash("no such inscription on this node", "err")
            return RedirectResponse("/wallet/nfts", status_code=303)
        fields = dict(inscription=row["txid"])

        def build(rpc):
            if row["owner"] not in _ledger_addresses(rpc):
                raise tokenlib.TokenError("that piece is not this wallet's to unlist")
            payload = P.AnyData(data=inscriptionlib.Ask(
                txid=bytes.fromhex(row["txid"]),
                take=inscriptionlib.Leg(inscriptionlib.LEG_NONE)).encode()).encode()
            return row["owner"], payload, None

        return _token_action(request, action="unlist", confirmed=confirmed,
                             build=build, fields=fields,
                             back="/exchange?tab=market", template="sell.html",
                             **_sell_page_data(row, chain, index))

    @app.get("/exchange", response_class=HTMLResponse)
    def exchange(request: Request, tab: str = "offers"):
        """Everything for sale on this chain, and what has been offered to you.

        Four questions, four tabs: what somebody has offered for something of
        yours, which mintpads still have pieces, what tokens are being sold
        for, and which single NFTs are for sale.
        """
        tab = tab if tab in EXCHANGE_TABS else "offers"
        chain, index = _token_chain()
        data: dict[str, Any] = {"tab": tab, "chain": chain, "node": chain.status(),
                                "shops": [], "node_error": None, "owned": set(),
                                "offers_in": [], "offers_out": [], "tags": {}}
        try:
            data["shops"] = _shop_listings(index, chain)
        except Exception as exc:
            data["node_error"] = f"the index could not be read: {exc}"
        try:
            with chain.rpc() as rpc:
                data["owned"] = set(_ledger_addresses(rpc))
        except HTTPException:
            raise
        except Exception as exc:
            data["node_error"] = data["node_error"] or str(exc)
        for shop in data["shops"]:
            shop["mine"] = shop["seller"] in data["owned"]
        data["tags"] = _tags_for([s["seller"] for s in data["shops"]])
        # Only what can still be bought. A listing whose item has been sold
        # says so in `available` -- the listings are read from the chain
        # every time, so a mintpad that has minted out and an NFT that has
        # moved drop off by themselves, with nobody to tell (D-037).
        def selling(shop, kind):
            return any(l["give"].get("kind") == kind and not l["available"]
                       for l in shop["listings"])
        data["mintpads"] = [s for s in data["shops"] if selling(s, "random")]
        data["market"] = [s for s in data["shops"] if selling(s, "inscription")]
        data["tokens"] = [s for s in data["shops"] if selling(s, "token")]
        data["pairs"] = data.get("pairs", [])
        data["collections"] = []
        data["popular"] = []
        data["sales"] = []
        data["listings"] = []
        # What has actually traded, and what it went for. Read from the swaps
        # the chain holds, not from a book -- there is no book (D-039).
        try:
            trades = index.trades()
        except Exception:
            trades = []
        if tab == "tokens":
            # Every token paired against the coin: the ones with a book and
            # the ones that have traded, last price and the day's move
            # (D-048). Clicking one opens its own page.
            data["pairs"] = _pairs(index, trades)
        elif tab == "market":
            # A collection is a market of its own, and the marketplace is the
            # list of them -- the same table the Tokens tab draws for pairs.
            # Its chart belongs to the collection's own page, where the
            # pieces it prices are (D-096).
            try:
                data["collections"] = _market_collections(index, chain, trades)
                # Popular means traded, and traded recently: what a market
                # is for is not the biggest set, it is the busy one. Falls
                # back on what is for sale where nothing has traded at all,
                # because a chain with no history still has a marketplace.
                data["popular"] = sorted(
                    data["collections"],
                    key=lambda c: (-c["day_coins"], -c["day_trades"],
                                   -c["volume"], -c["for_sale"],
                                   c["name"].lower()))[:6]
                data["sales"] = _recent_sales(index, trades)
                data["listings"] = _just_listed(index)
                data["tags"].update(_names_for(
                    index, [c["creator"] for c in data["collections"]]
                    + [s["seller"] for s in data["sales"]]
                    + [s["owner"] for s in data["sales"]]
                    + [l["seller"] for l in data["listings"]]))
            except Exception as exc:
                data["node_error"] = data["node_error"] or \
                    f"the collections could not be read: {exc}"
        try:
            # The mempool first, then the blocks, so an offer made a minute
            # ago is here rather than in ten minutes' time. A transaction is
            # in exactly one of the two, so there is nothing to dedupe -- but
            # a block landing between the two reads could show one twice, and
            # the txid settles it (D-058).
            pending = index.pending_offers()
            own = set(data["owned"])
            data["offers_in"] = _merge_offers(
                [o for o in pending if o["owner"] in own],
                index.offers_on(sorted(data["owned"])))
            data["offers_out"] = _merge_offers(
                [o for o in pending if o["buyer"] in own],
                index.offers_by(sorted(data["owned"])))
            for entry in data["offers_in"] + data["offers_out"]:
                entry["price"] = swaplib.describe_leg(_take_json(entry, index))
            # An offer already accepted is waiting for the buyer's wallet to
            # sign, not waiting for a second Accept. Drawing the button again
            # was how somebody pressed it twice and got told their own offer
            # belonged to somebody else (D-049).
            standing = {}
            for offer in state.offers.open_offers(chain.network):
                if offer["give"].get("kind") == "inscription":
                    standing[offer["give"]["txid"]] = offer
            for offer in state.offers.sold_offers(chain.network):
                if offer["give"].get("kind") == "inscription":
                    standing.setdefault(offer["give"]["txid"], offer)
            for entry in data["offers_in"]:
                held = standing.get(entry["inscription"])
                entry["accepted"] = bool(held and held["buyer"] == entry["buyer"])
                entry["held_for"] = held["buyer"] if held else ""
                entry["until"] = (time.strftime("%H:%M", time.localtime(held["expires"]))
                                  if held else "")
                entry["settled"] = bool(held and held.get("status") == "sent")
                entry["swap_txid"] = (held or {}).get("txid", "")
        except Exception as exc:
            data["node_error"] = data["node_error"] or str(exc)
        return render(request, "exchange.html", **data)



    return app


def _resolve_recipient(state, recipient: str) -> bytes:
    """Turn whatever the user typed into a public key.

    Accepts an @tag, a contact code, or an address with an on-chain
    announcement.
    Without the first, nobody could send a first message: publishing a key needs
    coins, coins need mining, and mining needs four hours -- so a new network
    would have no way to get started at all.
    """
    recipient = (recipient or "").strip()
    if not recipient:
        raise ValueError("choose a recipient, or paste their contact code")

    if taglib.looks_like_a_tag(recipient):
        # A tag is a name for an ADDRESS, and messaging needs a key, so this
        # is two lookups: the chain says which address holds the name, and
        # that address's announcement says which key lives there (D-032).
        # Typed with or without the @, because the shape is what tells a name
        # from an address and the punctuation tells nothing (D-112).
        wanted = taglib.normalise(recipient)
        address = None
        for chain in state.token_chains:
            if chain.network == state.messaging.network:
                try:
                    address = state.token_index(chain).address_of(wanted)
                except Exception:
                    address = None
                break
        if not address:
            raise ValueError(
                f"nobody holds @{wanted} on {state.messaging.network}, or this "
                "node has not indexed the claim yet.")
        recipient = address

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


#: The node's accounts (labels, on a newer Core) that belong to this
#: application. A node's wallet is often somebody's own wallet as well, with
#: coins and addresses that have nothing to do with the arcade; those are not
#: ours to show, to spend or to gather (D-046). Everything the arcade makes
#: is filed under one of these.
ARCADE_ACCOUNTS = ("arcade-identity", "arcade-messaging", "DogecoinArcade")


def _is_arcade_account(name: str | None) -> bool:
    name = (name or "").strip()
    return name in ARCADE_ACCOUNTS or name.startswith("arcade")


#: Version bytes that mean a real chain. An address says which chain it is
#: for, so the rule below needs no extra call and no caller has to remember
#: to pass a flag -- which is the kind of thing that gets forgotten exactly
#: once, on mainnet.
_MAINNET_VERSIONS = frozenset(
    p.pubkeyhash_version for p in NETWORKS.values() if p.name in ("main", "doge-main"))


def _on_a_real_chain(address: str) -> bool:
    try:
        version, _ = b58check_decode(address)
    except Exception:
        return False
    return version in _MAINNET_VERSIONS


def _ledger_addresses(rpc) -> list[str]:
    """The addresses this wallet owns on a chain, funded or not.

    On a test chain, every address in the node's wallet was made by this
    application: the node exists to run it. So all of them are its, and
    anything that landed anywhere can be walked home (D-046).

    On a real chain a node is usually somebody's own wallet as well, with
    coins that have nothing to do with the arcade. Those are not ours to
    show, to spend or to gather, so only what the arcade filed under its own
    accounts counts.

    `listunspent` alone would miss an address holding tokens and no coins --
    the usual state of a recipient -- so the address book is asked too, and
    the accounts come from there. Module level because the bot RPC (rpc.py)
    asks the same question.
    """
    rows = []
    try:
        rows = list(rpc.call("listreceivedbyaddress", 0, True) or [])
    except Exception:
        rows = []
    spendable = []
    try:
        spendable = [u for u in (rpc.call("listunspent", 0, 9_999_999) or [])
                     if u.get("address")]
    except Exception:
        spendable = []
    every = [row["address"] for row in rows] + [u["address"] for u in spendable]
    real = any(_on_a_real_chain(a) for a in every[:5])

    found: dict[str, None] = {}
    for row in rows:
        if not real or _is_arcade_account(row.get("account", row.get("label"))):
            found.setdefault(row["address"], None)
    for utxo in spendable:
        if not real or _is_arcade_account(utxo.get("account", utxo.get("label"))):
            found.setdefault(utxo["address"], None)
    return list(found)


def _tag_address(state, destination: str, *, mainnet: bool) -> str:
    """An @tag turned into the address that holds it, or the text unchanged.

    Every send takes one of these now -- coins, tokens, grants, revokes, an
    inscription, an offer. A tag is a name for an address and the chain says
    which, so there is no reason one place should take a name and the next one
    only take 34 characters of base58 (D-070).

    Resolved at the moment of sending, never remembered: a tag can move, and a
    wallet that pays yesterday's answer pays the wrong person. What is shown
    afterwards is the address it resolved to, because that is what was paid.
    """
    destination = (destination or "").strip()
    # With or without the @: nobody types the punctuation consistently, and
    # nothing can be read both ways -- a tag is at most 24 characters of
    # a-z0-9_ and an address is 34 of mixed-case base58 (D-112).
    if not taglib.looks_like_a_tag(destination):
        return destination
    wanted = taglib.normalise(destination)
    for chain in state.token_chains:
        if bool(chain.is_mainnet) != bool(mainnet):
            continue
        try:
            found = state.token_index(chain).address_of(wanted)
        except Exception:
            found = None
        if found:
            return found
    raise ValueError(
        f"nobody holds @{wanted} on "
        f"{'mainnet' if mainnet else 'testnet'}, or this node has not indexed "
        f"the claim yet. A tag is claimed from the Address book.")


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

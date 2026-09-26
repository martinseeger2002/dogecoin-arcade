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
import math
import re
import threading
import time
import html
import secrets
import urllib.parse
import shutil
import sys
import os
from pathlib import Path
from urllib.parse import quote
from typing import Any

from markupsafe import Markup

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
from .. import feedview
from ..messaging import feed as feedlib
from ..messaging import mempool as mempoollib
from ..messaging import envelope as envelopelib
from .. import inscriptions as inscriptionlib
from . import guide as guidelib
from .. import charts as chartlib
from .. import fees
from .. import gather as gatherlib
from .. import mintpad as mintpadlib
from .. import tokenpad as tokenpadlib
from .. import swap as swaplib
from .. import tags as taglib
from .. import txbuild
from ..messaging import sender as sendermod
from .. import state as statelib
from ..messaging import contact, content, group
from ..script import b58check_decode, b58check_encode, hash160
from ..messaging.derive import DerivationError, derive_identity
from ..messaging.envelope import (
    MAX_ANNOUNCE_NAME, MAX_ANNOUNCE_NAME_CLASS_B,
    announcement_fits_one_output, build_key_announcement,
)
from .. import pageapi
from .. import release as releaselib
from .. import seed as seedlib
from .. import accounts as accountslib
from .. import faucet as faucetlib
from .. import admin as adminlib
from .. import funding as fundinglib
from .. import listings as listingslib
from .. import utxos as utxoslib
from .. import update as updatelib
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
from . import account as accountlib
from .. import accountruns as accountrunslib
from .. import accountparts as accountpartslib
from . import door as doorlib
from .. import bootstrap as bootstraplib
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


def _coins(sats: Any) -> str:
    """A template filter: satoshis as coins, for the totals a feed card keeps."""
    try:
        return format_amount(int(sats or 0), True)
    except (TypeError, ValueError):
        return "?"


TEMPLATES.env.filters["coins"] = _coins


def _cursor(before: Any) -> tuple[int | None, float | None]:
    """The (id, anchor) halves of a feed cursor; `_cursor3` also gives the moment."""
    ident, anchor, _asof = _cursor3(before)
    return ident, anchor


def _cursor3(before: Any) -> tuple[int | None, float | None, int | None]:
    """A feed cursor out of the one token a link carries: `id@anchor@asof`.

    `412@0.35@1790350000` means "post 412, the rank it had, and the moment it
    was ranked at". A popularity order is cut along a number that moves, so a
    cursor remembers where the cut WAS -- and since 2026-09-25 the rank also
    moves with the clock (feed.hot), so every page of one scroll is ranked at
    the moment its first page was made. One query parameter rather than three,
    because a link whose parts must agree breaks when somebody copies one. An
    older `id@anchor`, or a bare id (a bookmark, a profile page), still parses:
    its page is ranked now, and store.py measures the anchor itself if absent.
    """
    if before is None:
        return None, None, None
    ident, _, rest = str(before).partition("@")
    score, _, asof = rest.partition("@")
    try:
        number = int(ident)
    except ValueError:
        return None, None, None
    try:
        when = int(asof) if asof else None
    except ValueError:
        when = None
    try:
        return number, (float(score) if score else None), when
    except ValueError:
        return number, None, when


def _next_cursor(rows: list[Any], sort: str, asof: int | None = None) -> Any:
    """What the next page's link carries, which depends on the order.

    Newest-first needs the id and nothing else: an id does not move. The
    endorsed order needs the pair, or the page after this one is cut along a
    line that has already shifted.

    The score is written SHORT, and short in one direction: truncated, not
    rounded. Scores only rise, so a cursor rounded down can only miss a post
    that moved past the reader, while one rounded up can hand back a post they
    have already read -- which is the whole failure this cursor exists to
    avoid, coming back through six decimal places of a number nobody reads.
    """
    last = rows[-1]
    if sort != "popular":
        return last["id"]
    # The RANK now (feed.hot), truncated as the score was, and the moment it was
    # ranked at, so the next page is ranked at that same moment.
    value = last["rank"] if "rank" in last.keys() else last["score"]
    token = f"{last['id']}@{int(float(value) * 1_000_000_000) / 1_000_000_000}"
    return f"{token}@{int(asof)}" if asof else token


# A global rather than a filter: it takes what the page already looked up,
# and a filter taking a second argument reads worse in the template.
def _screen_text(text: str):
    """A post's verdict for the feed card, from whichever state serves the page."""
    screen = getattr(TEMPLATES, "_screen_of", None)
    if screen is None:
        return "ok"
    s = screen()
    return s.check_text(text) if s.enabled else "ok"


TEMPLATES.env.globals["screen_text"] = _screen_text
TEMPLATES.env.globals["render_post"] = lambda text, drawable=None: Markup(
    post_html(text, drawable))


#: An inscription named in a post: a bare id is not enough, because a post is
#: prose and sixty-four hex characters can be anything. `/content/<id>` is the
#: same spelling a token icon and a collection thumbnail use (D-103).
_CONTENT_IN_TEXT = re.compile(r"/content/([0-9a-f]{64})")


def post_html(text: str, drawable: dict[str, str] | None = None) -> str:
    """One post's words as HTML: escaped first, then its inscriptions drawn.

    Everything is escaped before anything is added, so a post that contains
    angle brackets is a post that contains angle brackets. What is added back
    is decided HERE, from what the index says the inscription is -- never
    from anything the post says about itself (D-138).

    * a picture is shown;
    * a page goes in the sandboxed frame the viewer already uses, which is
      the one whose guarantees D-121 put back under test;
    * anything else stays a link, because a thing this node cannot identify
      is a thing it should not be drawing.
    """
    drawable = drawable or {}
    out = []
    last = 0
    for found in _CONTENT_IN_TEXT.finditer(text or ""):
        out.append(html.escape((text or "")[last:found.start()]))
        piece = found.group(1)
        kind = drawable.get(piece, "")
        if kind.startswith("image/"):
            out.append(f'<img class="postmedia" src="/content/{piece}" alt="" '
                       f'loading="lazy">')
        elif kind == "text/html":
            # Same sandbox as the inscription viewer: no same-origin, so it
            # cannot reach this page, this wallet or anybody's storage.
            out.append(f'<iframe class="inscription-frame postmedia" '
                       f'src="/content/{piece}" loading="lazy" '
                       f'sandbox="allow-scripts allow-pointer-lock"></iframe>')
        else:
            out.append(f'<a href="/inscriptions/{piece}/view">'
                       f'/content/{piece[:12]}…</a>')
        last = found.end()
    out.append(html.escape((text or "")[last:]))
    return "".join(out)


def inscriptions_in(text: str) -> list[str]:
    """Every inscription a post names, so a page can ask about them at once."""
    return _CONTENT_IN_TEXT.findall(text or "")

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
    ("/feed",         "Feed",         "testnet",   True),
    ("/contacts",     "Address book", None,        True),
    ("/backup",       "Backup",       None,        True),
    ("/wallet",       "Wallet",       None,        True),
    ("/tokens",       "Tokens",       "mainnet",   True),
    ("/nfts",         "NFTs",         "mainnet",   True),
    ("/exchange",     "Exchange",     "mainnet",   True),
    ("/approvals",    "Approvals",    None,        True),
    ("/docs",         "Docs",         None,        True),
]

#: What an account sees instead of the operator's tabs. Their own pages
#: drive their own keys; the operator's drive the node's wallet, and almost
#: nothing on those would be true for somebody else.
#: In the same order the operator's own NAV is: identity, messaging,
#: address book, the feed, backup, then the wallet and the shared
#: marketplace pages. Two things on the operator's are deliberately not
#: here: Approvals has no account equivalent (Overview explains why --
#: an account never delegates a signature ahead of time, so there is
#: nothing to review afterward), and /clone is reachable from every
#: page's own footer already.
ACCOUNT_NAV = [
    ("/me",              "Your arcade",  None,        True),
    ("/me/messages",     "Messages",     "testnet",   True),
    # 2026-09-25: Messages, Notifications, Feed, then the address book.
    ("/me/notifications", "Notifications", None,      True),
    ("/feed",            "Feed",         "testnet",   True),
    ("/me/contacts",     "Address book", None,        True),
    ("/me/backup",       "Backup",       None,        True),
    ("/me/wallet",       "Wallet",       None,        True),
    ("/tokens",          "Tokens",       "mainnet",   True),
    ("/nfts",            "NFTs",         "mainnet",   True),
    ("/exchange",        "Exchange",     "mainnet",   True),
    ("/docs",            "Docs",         None,        True),
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


#: The session cookie's name. At module level because the door reads it
#: before any route exists, and the routes read it after.
SESSION = "arcade_session"
from ..admin import ADMIN_COOKIE, ADMIN_HEADER  # noqa: E402

#: Hostnames a browser treats as a secure context over plain http. Everywhere
#: else -- a LAN address, a name on the local network -- `crypto.subtle` is
#: simply undefined, so a page that needs it must say why rather than fail
#: half way through somebody's signup with "undefined is not an object".
LOOPBACK = ("localhost", "127.0.0.1", "::1", "[::1]")

WHY_NOT_SECURE = (
    "This page was not served over https, so the browser will not let it use "
    "its own cryptography \u2014 and a wallet that cannot generate a key "
    "cannot sign anything. Open the wallet at localhost on the machine it "
    "runs on, or through the https address on its Remote page. This is a "
    "browser rule, not ours, and it is the right one: a key made over plain "
    "http is a key anybody on the network watched being made.")


def _count(text: Any, label: str, ceiling: int) -> int:
    """One allowance read off a form: a number, not negative, not pretend.

    Refused rather than guessed, because a settings file that quietly holds
    `null` is an allowance nobody set. Zero is a real answer -- it closes the
    action, and the account is told that is what happened.
    """
    said = str(text or "").strip().replace(",", "").replace(" ", "")
    try:
        value = int(said)
    except ValueError:
        raise ValueError(f"{label} has to be a whole number, not {said or 'nothing'}.")
    if value < 0:
        raise ValueError(f"{label} cannot be below zero. Zero closes it.")
    if value > ceiling:
        raise ValueError(f"{label} cannot be above {ceiling:,} -- past that "
                         f"it is not an allowance, it is no limit.")
    return value


def _percent(text: Any, label: str) -> float:
    """A percentage read off a form: a number, not negative, not over a hundred.

    The same rule as `_count` -- refused rather than guessed, because a
    settings file that quietly holds `null` is a rate nobody set. A hundred is
    the ceiling because past that it is not a cut in the trade, it is the
    whole trade and more.
    """
    said = str(text or "").strip().replace(",", "").replace(" ", "").rstrip("%")
    try:
        value = float(said)
    except ValueError:
        raise ValueError(f"{label} has to be a percentage, not {said or 'nothing'}.")
    if not math.isfinite(value):
        raise ValueError(f"{label} has to be a number, not {said}.")
    if value < 0:
        raise ValueError(f"{label} cannot be below zero. Zero charges nothing.")
    if value > 100:
        raise ValueError(f"{label} cannot be above 100 -- past that it is not a "
                         f"cut, it is the whole trade and more.")
    return value


def secure_context(request: Request) -> bool:
    """Whether `crypto.subtle` will exist on the page we are about to send."""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    if proto == "https":
        return True
    name = (request.headers.get("host", "").rsplit(":", 1)[0]).strip().lower()
    return name in LOOPBACK or name.startswith("127.")


def locked(title: str, detail: str, status: int = 403) -> HTMLResponse:
    """What a stranger sees. Deliberately plain: it names nothing about this
    wallet, because whoever is reading it has not shown they may see it."""
    return HTMLResponse(LOCKED_PAGE % (title, detail), status_code=status)


def the_door(state: AppState):
    """The one guard, registered as middleware so it covers every route.

    A door guarded route-by-route is a door that is open the first time
    somebody forgets, and the route they forget is the one that spends.

    Two shapes, chosen by how the instance is run (arcade/web/door.py):

    **A wallet** -- the default, and what this has always been. One
    person's machine, reachable from that machine. Everything is served,
    because everything belongs to whoever is sitting there.

    **A public instance** -- what `dogecoinarcade.com` runs. The address is
    public and there is no token to hold, so only the public surface is
    served and everything else is refused: an ALLOWLIST, including every
    route added after it was written. A session says who somebody is; it
    does not make the operator's wallet theirs, and nothing here consults
    one.

    The inscribed pages' hostname is separate in both shapes. A page in the
    sandbox has an opaque origin and sends no cookie, so that hostname is
    its whole key: good for the content and the page API and nothing else.
    """
    async def guard(request: Request, call_next):
        host = (request.headers.get("host", "").rsplit(":", 1)[0]).strip().lower()
        path = request.url.path

        pages_host = state.pages_hostname
        if pages_host and host == pages_host:
            if doorlib.pages_path(path):
                return await call_next(request)
            return locked("Not here", "Nothing is served at this address but "
                          "inscribed pages.", status=404)

        outside = doorlib.from_outside(
            request.headers, request.headers.get("host", ""),
            state.public_hosts)

        # Claiming a node is the one thing that is about WHERE you are
        # rather than what the instance is: a public node on somebody's
        # server still has an operator, and they claim it from a loopback
        # connection to it. So this is decided by origin alone, even under
        # `--public`.
        if path == "/auth/operator" and not outside:
            return await call_next(request)

        # Per request, not per process. One machine is both things at once:
        # The operator's wallet on their desk, and the arcade on a public
        # name. `--public` makes the whole instance public (a node that is
        # only that); otherwise a request is public if it arrived from
        # outside -- by one of the published names, or carrying a header
        # only an edge adds.
        if not (state.public or outside):
            return await call_next(request)

        if doorlib.public_path(path, request.method):
            return await call_next(request)

        # The one exception, and it is one ACCOUNT rather than one secret.
        # Whoever runs a node reaches their own wallet from outside it by
        # signing in as the key that node names as its operator -- the same
        # twenty-four words, the same challenge, the same signature. A
        # stranger holding a perfectly good session is still refused here,
        # because the question is not "are you signed in" but "are you the
        # person this node belongs to".
        #
        # This is the door's one route-independent rule, and it is written
        # here rather than in the allowlist so that it is impossible to read
        # `door.py` and think a session opens anything.
        # Since 2026-09-25 the operator, from outside, is an account like any
        # other plus one thing: /admin. The panel asks for the admin password
        # itself (arcade/admin.py), and everything the node's own wallet does
        # from outside happens in it -- so a stolen account cookie opens that
        # account, never the node.
        operator = state.operator
        if operator:
            account = state.account_for(request.cookies.get(SESSION, ""))
            if (account is not None and account.pubkey.lower() == operator
                    and (path == "/admin" or path.startswith("/admin/"))):
                # The panel itself asks for the admin password (_admin_check);
                # everything the node's wallet does from outside is in it.
                return await call_next(request)
        if path.startswith("/rpc"):
            # The bot RPC has its own key, in a file beside the node, and it
            # can spend. It is for programs running on the same machine,
            # never for anything that arrived from the internet.
            return JSONResponse({"result": None, "id": None, "error": {
                "code": -32600,
                "message": "the bot RPC is not served publicly"}},
                status_code=403)
        return locked(
            "Not here",
            "This is a public arcade: the feed, the collections and the "
            "chain are open to anyone, and the wallet behind it belongs to "
            "whoever runs the node. Nothing of yours is here \u2014 your "
            "coins and your name are on the chain, and a node you run "
            "yourself serves them all.",
            status=404)

    return guard


def _above_dust(sats: int, what: str) -> None:
    """Refuse an amount the network will not carry. An output under the dust limit
    must pay 0.01 more in fee or no peer relays the transaction, so it would sit
    "waiting for its block" for good (fees.DUST_LIMIT)."""
    if sats < fees.DUST_LIMIT:
        raise ValueError(f"the smallest {what} the network will carry is "
                         f"{fees.DUST_LIMIT / 100_000_000:g} -- anything less is dust, "
                         f"and nobody would relay it")


def create_app(state: AppState) -> FastAPI:
    TEMPLATES._screen_of = state.screen          # the feed card's screen_text()
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
        try:                                  # the faucet's daily top-off
            # Not on an app pointed at no node (the tests' datadir does not exist).
            where = state.messaging.datadir
            if where is None or Path(where).exists():
                _start_top_offs()
        except Exception as exc:              # noqa: BLE001 -- never block startup
            log.info("top-off not started: %s", exc)
        try:                                  # mail for phones already subscribed
            push = state.push()
            if push is not None:              # notes every account's mail
                _watch_for_mail()
        except Exception as exc:              # noqa: BLE001 -- never block startup
            log.info("push not started: %s", exc)
        yield
        if not state.begin_shutdown():
            print("arcade-web: a send was still running at shutdown. What is "
                  "already broadcast cannot be taken back; the interface will "
                  "offer to finish the rest when it starts again.",
                  file=sys.stderr, flush=True)

    app = FastAPI(title="DogecoinArcade", docs_url=None, redoc_url=None,
                  lifespan=_lifespan)
    app.middleware("http")(the_door(state))


    def _from_outside(request: Request) -> bool:
        return doorlib.from_outside(request.headers,
                                    request.headers.get("host", ""),
                                    state.public_hosts)

    def _public_request(request: Request) -> bool:
        """Whether THIS request is being served publicly (arcade/web/door.py).

        The operator, signed in, is never served publicly -- otherwise they
        would be let through the door and then shown a splash and a
        navigation with their own wallet missing from it.
        """
        if not (state.public or _from_outside(request)):
            return False
        return True

    def _is_operator(request: Request) -> bool:
        account = signed_in(request)
        return bool(account is not None and state.operator
                    and account.pubkey.lower() == state.operator)

    def _admin_remote(request: Request) -> bool:
        """The operator, from outside, with the admin password's session open."""
        return _is_operator(request) and state.admin_sessions().valid(
            request.cookies.get(ADMIN_COOKIE, ""), state.operator)

    def _nav_for(request: Request) -> list:
        """The tabs: an account's own, the operator's machine's, and Admin for the
        operator either way (the panel itself asks for what it needs)."""
        admin = ("/admin", "Admin", None, True)
        if _public_request(request):
            if signed_in(request) is not None:
                return ACCOUNT_NAV + ([admin] if _is_operator(request) else [])
            return [entry for entry in NAV if doorlib.public_path(entry[0])]
        return NAV + [admin]

    def _again(where: str, **context: Any) -> RedirectResponse:
        """Post, redirect, get -- with what the POST decided carried over.

        Every one of these routes used to answer a POST by rendering the
        page itself. That is a correct answer and a bad page: the browser
        remembers that the URL was reached by POST, so reloading it, or
        going back to it, asks "Firefox must send information that will
        repeat any action performed earlier" -- and the action it offers to
        repeat is a send. The operator met that dialog often enough to report it.

        A redirect ends that: the confirmation, or the error, is held for
        the GET that follows, and the page the browser lands on is an
        ordinary page it may reload as often as it likes. Held by token
        rather than put in the URL because some of it is a whole file, and
        none of it is anybody else's business.
        """
        token = _hold(**context)
        return RedirectResponse(f"{where}{'&' if '?' in where else '?'}"
                                f"held={token}", status_code=303)

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
            # The navigation is what the door allows, not a fixed list. A
            # public instance showing Wallet, Messages and Backup is a site
            # whose own menu 404s -- and the menu is the first thing a
            # visitor uses. Filtered here rather than in each template, so a
            # page added later cannot forget.
            # An account gets its own tabs, not the operator's with the
            # unreachable ones removed: a menu of four things that work
            # beats a menu of eleven with seven missing.
            "nav": _nav_for(request),
            "public": _public_request(request),
            "path": request.url.path,
            "state": state,
            "csrf": state.csrf_token,
            "notice": notice,
            "notice_kind": notice_kind,
            "msg_net": state.messaging.label,
            "ledger_net": state.ledger.label,
            "approvals_waiting": _approvals_waiting(),
            "unread_messages": _unread_messages(),
            **_account_counts(request),
            # Whether this page is drawn for a signed-in account, for the
            # key-publishing check every page runs (base.html).
            "account_here": bool(_public_request(request)
                                 and signed_in(request) is not None),
            "unread_board": _unread_board(),
            "offers_waiting": _offers_waiting(),
        }
        base.update(context)
        # What a POST decided, picked up by the GET it redirected to. Last,
        # so it wins over whatever the page worked out for itself: the page
        # is being drawn precisely to show this.
        base.update(_picked_up(request.query_params.get("held", "")))
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
        # On a public instance the front page is the SPLASH, not the
        # operator's wallet. `/` is in the door's allowlist because a public
        # arcade has to have a front page at all -- so the route itself has
        # to be the one that is safe to serve, or the allowlist would be
        # handing out the Overview's balances, unread counts and identity.
        if _public_request(request):
            # Somebody already signed in goes to their own arcade. Without
            # this, signing up ends on the page that asks you to sign up.
            if signed_in(request) is not None:
                return RedirectResponse("/me", status_code=303)
            return join_page(request)
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
                    announced = (store.confirmed_key_for(state.derived_address)
                                 is not None)
        # A wallet with no name yet is a wallet somebody has just installed.
        # Asked here, once, at the top of the first page they see: a @tag is
        # how anybody addresses them -- in the address book, on a shop, on
        # every listing they make -- and until they have one they are a
        # 34-character address to everybody, including themselves (D-131).
        my_tag = state.my_tag()
        claiming = ""
        if not my_tag:
            for item in reversed(state.pending_tokens):
                what = str(item.get("what") or "")
                if (item.get("network") == state.messaging.network
                        and what.startswith("claim @")):
                    claiming = what[len("claim @"):]
                    break
        register = state.accounts()
        return render(request, "overview.html", contact_code=code, announced=announced,
                      seats_free=register.free(), seats_total=register.seats,
                      my_tag=my_tag, claiming=claiming,
                      my_address=state.derived_address,
                      auto_update=bool(state.setting("auto_update", True)),
                      auto_sell=bool(state.setting("auto_sell", True)),
                      auto_fill=bool(state.setting("auto_fill", True)),
                      trade_cut=swaplib.cut_bps(state.setting("trade_cut",
                                                              TRADE_CUT)) / 100,
                      update_status=state.update_status, when=_when,
                      update_every=watcherlib.BlockWatcher.UPDATE_EVERY,
                      release_key=releaselib.PUBLIC_KEY,
                      quota=accountslib.limits(state.settings()),
                      quota_labels=accountslib.LABELS,
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

    @app.post("/settings/cut")
    def set_trade_cut(request: Request, csrf_token: str = Form(""),
                      percent: str = Form("")):
        """What this node takes of a trade it made the offer for (§1d).

        One field for one rule. It is a percentage OF THE PRICE, paid by the
        buyer on top of it, so an ask of 100 still means the seller receives
        100: the alternatives are that an advertised price stops meaning what
        the seller gets, or that the engine's leg checks get rewritten to know
        about a fee, and both are worse than one extra output.

        Zero is the default and is the ordinary node, asking nothing of
        anybody. A node that sets a number should say so somewhere people read
        before they buy -- and every one of its offers says it anyway, on the
        page a person looks at before signing, because a fee somebody did not
        see is a fee they did not agree to.
        """
        check_csrf(csrf_token)
        try:
            value = _percent(percent, "The cut on trades")
        except ValueError as exc:
            state.flash(str(exc), "err")
            return RedirectResponse("/", status_code=303)
        state.set_setting("trade_cut", value)
        state.flash(
            f"Offers this node makes now ask {value:g}% of the buyer, on top of "
            f"the price and named in the offer." if value else
            "Offers this node makes carry nothing on top of the price again.",
            "ok")
        return RedirectResponse("/", status_code=303)

    @app.post("/settings/quotas")
    def set_quotas(request: Request, csrf_token: str = Form(""),
                   post: str = Form(""), react: str = Form(""),
                   message: str = Form(""), send: str = Form(""),
                   listings: str = Form(""), trade: str = Form(""),
                   claims: str = Form(""), inscribe: str = Form(""),
                   issue: str = Form(""), payload: str = Form("")):
        """How much of this node one account may take.

        The defaults (arcade/accounts.py) are sized for a node that seats
        fifty people, and a node is not always that. An operator running one
        for four friends should not have to inherit a number meant for
        strangers, and an operator running a public arcade wants to be able
        to close a door without editing code while the traffic is arriving.

        Zero is allowed and means zero: an operator who sets it is closing
        that action, and the account is told so plainly rather than being
        given a number it can never reach. This route saves what it is given
        and enforces nothing -- the enforcement is in the build routes, which
        read the settings each time so a change lands on the NEXT action and
        not on the next restart.
        """
        check_csrf(csrf_token)
        wanted = {"post": (post, "posts an hour", accountslib.CEILING),
                  "react": (react, "reactions an hour", accountslib.CEILING),
                  "message": (message, "messages an hour",
                              accountslib.CEILING),
                  "send": (send, "sends an hour", accountslib.CEILING),
                  "list": (listings, "listings an hour", accountslib.CEILING),
                  "trade": (trade, "trades an hour", accountslib.CEILING),
                  "name": (claims, "name and key claims an hour",
                           accountslib.CEILING),
                  "inscribe": (inscribe, "inscriptions an hour",
                               accountslib.CEILING),
                  "issue": (issue, "token issuances an hour",
                            accountslib.CEILING),
                  "bytes": (payload, "bytes a day", accountslib.BYTE_CEILING)}
        # Read every one of them before writing any. A form with one bad
        # number in it saving the seven it liked would leave the operator
        # looking at a page that claims the set they typed is in force, when
        # it is not.
        numbers: dict[str, int] = {}
        for kind, (said, label, ceiling) in wanted.items():
            try:
                numbers[kind] = _count(said, label, ceiling)
            except ValueError as exc:
                state.flash(str(exc), "err")
                return RedirectResponse("/", status_code=303)
        for kind, value in numbers.items():
            state.set_setting(f"quota:{kind}", value)
        state.flash("Those are the allowances accounts have here now. Each "
                    "one is measured again from the next thing they ask for.",
                    "ok")
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
        # `/content/<id>` in a message is drawn as the feed draws it (the operator,
        # 2026-09-25): what each id is comes from the index, never the message.
        from types import SimpleNamespace
        drawable = _drawable_in([SimpleNamespace(
            text=(m["body"] or b"").decode("utf-8", "replace") if isinstance(m["body"], bytes)
            else str(m["body"] or ""), replies=[]) for m in (items or [])])
        return render(request, "messages.html", threads=threads, thread=items,
                      drawable=drawable,
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
        # Only what this POST decided. The conversation itself -- the
        # threads, the messages, who the peer is -- is the GET's job, and
        # doing it twice is how two versions of one page drift apart.
        return _again(
            f"/messages/{peer_hex}",
            prepared=prepared, plan=plan, draft=body, error=error,
            timing=timing,
            cost=(send_cost(prepared[0], plan.transactions)
                  if prepared and plan else None),
            attached_b64=(base64.b64encode(file_bytes).decode()
                          if file_bytes else ""),
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
    def compose_form(request: Request, to: str = ""):
        keys = []
        if state.store_path.exists():
            with state.store() as store:
                keys = store.all_keys()
        # `?to=@tag` from a profile's Message button (2026-09-26).
        return render(request, "compose.html", keys=keys, plan=None, prepared=None,
                      recipient=to.strip()[:80])

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
        return _again("/compose", plan=plan, error=error,
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
        return _again("/compose", plan=None, error=error,
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
                # What their own announcement already says, filled into any
                # gaps: a book that held somebody whose mainnet address was
                # one table away is what this is for (D-139).
                store.complete_contacts()
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
                    if not (row["tag"] or ""):
                        # No name on the chain, nothing anybody could check:
                        # offering it would be offering the row of base58 the
                        # address book exists to avoid (D-137).
                        continue
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
                # On the chain, not merely seen here: the card says whoever has
                # your tag can write to you, and that is true only once other
                # people's nodes can read the key off a block.
                announced = (store.confirmed_key_for(state.derived_address)
                             is not None)
        return render(request, "contacts.html", people=people, editing=editing,
                      published=published, when=_when, mine=_my_tag(), tags=found,
                      known_tags=known_tags, announced=announced,
                      matches=matches, find=find,
                      other_address=_mainnet_identity(),
                      my_face=_my_picture(),
                      my_bio=state.setting("bio", ""), my_url=state.setting("url", ""),
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

        # What the tag itself says: a published announcement carries the
        # key and the OTHER chain's address beside the name, so adding
        # somebody by @tag should not leave two of the three blank and a
        # contact nobody can message. Filled in only where the form left a
        # gap, so an explicit value is never overwritten (D-137).
        pubkey = None
        if testnet_address and state.store_path.exists():
            with state.store() as store:
                said = store.key_for(testnet_address)
            if said is not None:
                pubkey = pubkey or bytes(said["pubkey"])
                mainnet_address = mainnet_address or (said["other_address"] or "")

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

    # --- the feed -------------------------------------------------------------
    #
    # One public feed of everybody's posts, and a page per person (D-138).
    # Channels are gone: a post belongs to whoever wrote it, and that is the
    # only place it lives.

    #: How many posts a feed page holds before somebody has to ask for more.
    FEED_PAGE = 10

    #: Read from the mempool, rather than told. `_profile_of` uses this as the
    #: default for its `waiting` argument, where `None` is already taken to mean
    #: "do not look at the pool at all" and an empty `Pending()` would be a
    #: silent lie about what is waiting to be confirmed.
    _POOL = object()

    def _pending_feed(network: str):
        """What the mempool holds for the feed right now.

        A block is a minute or ten, and a like that takes ten minutes to
        appear reads as a like that did not work. Read fresh and never
        written down, exactly as the marketplace reads offers and asks
        (D-117, D-141).
        """
        try:
            with state.messaging.rpc() as rpc:
                return mempoollib.read(rpc, state.messaging.params, network,
                                       mine=state.derived_address or "")
        except HTTPException:
            raise
        except Exception:
            return mempoollib.Pending()

    def _feed_page(network: str, author: str = "", before: Any = None,
                   limit: int = FEED_PAGE, sort: str = "new") -> tuple[list[Any], Any, Any]:
        """A page of posts, the cursor for the next, and what the mempool is
        holding.

        `sort` is the order, and it is the database's to answer: "new" is the
        newest first, which is what a person's own page keeps, and "popular"
        is the feed's -- the endorsed first, weighted, with muted authors
        already gone (2026-09-23; store.feed_posts_popular).

        A popularity order cannot be paged by an id alone, because the thing
        the pages are cut along moves. So the cursor for that order carries
        the ANCHOR PAIR -- the last post shown and the score it had when this
        page was made -- in one token, and store.py compares against it.
        Nothing else in the contract changes: one token in, one page out.

        Pending posts go on top of the FIRST page only: they have no id to
        page by, and a post that has not confirmed cannot be older -- or
        less endorsed -- than one that has.
        """
        ident, anchor, asof = _cursor3(before)
        if sort == "popular" and asof is None:
            asof = int(time.time())
        with state.store() as store:
            if sort == "popular":
                rows = store.feed_posts_popular(network, cursor=ident,
                                                anchor=anchor, limit=limit + 1,
                                                author=author, asof=asof)
            else:
                rows = store.feed_posts(network, author=author, before=ident,
                                        limit=limit + 1)
        more = rows[limit:]
        rows = rows[:limit]
        cursor = _next_cursor(rows, sort, asof) if rows and more else None
        waiting = _pending_feed(network)
        if before is None:
            known = {row["txid"] for row in rows}
            fresh = [mempoollib.Row(post) for post in waiting.posts
                     if post["txid"] not in known
                     and (not author or post["sender"] == author)]
            rows = fresh + list(rows)
        return rows, cursor, waiting

    def _shown(rows: list[Any], network: str, waiting: Any = None,
               me: str | None = None) -> list[Any]:
        """Posts with everything done to them applied, ready to draw.

        `waiting` is the mempool's contribution: reactions that have not
        confirmed are counted and drawn like any other, because the person
        who pressed the button has already paid for them (D-141).

        `me` is whose page this is being drawn for. It decides which posts are
        "mine" and which ones I have already liked, so on a public instance it
        has to be the reader's address and not the node's -- left as the node's,
        an account sees its own posts with a Delete it did not earn and its own
        likes as ♡, and pressing one a second time is a second transaction.
        Nobody but the operator gets the node's address here.
        """
        if not rows:
            return []
        targets = [row["txid"] for row in rows]
        with state.store() as store:
            acts = list(store.feed_acts_on(network, targets))
            # Replies can be replied to, so their own actions are wanted too:
            # one more query rather than one per reply (D-138's leanness).
            replies = [a["txid"] for a in acts if a["kind"] == feedlib.REPLY]
            if replies:
                acts += list(store.feed_acts_on(network, replies))
            # A tip is a payment, and payments may be made on either chain,
            # so a post's running total gathers them from every chain and
            # keeps them apart by chain (feed.py, 2026-09-23). The
            # rows the partitioned query already found are not counted twice.
            seen = {a["txid"] for a in acts}
            acts += [t for t in store.feed_tips_on(targets + replies)
                     if t["txid"] not in seen]
            muted = store.muted()
        if waiting is not None and waiting.acts:
            known = {a["txid"] for a in acts}
            acts += [mempoollib.Row(act) for act in waiting.acts
                     if act["txid"] not in known]
        return feedview.assemble(rows, acts,
                                 me=state.derived_address if me is None
                                 else me, muted=muted)

    def _drawable_in(shown: list[Any]) -> dict[str, str]:
        """The content type of every inscription these posts name.

        Asked once for the page rather than once per post, and asked of the
        INDEX rather than of the post: what a file is, is decided from the
        chain (D-138).
        """
        wanted: set[str] = set()

        def walk(items):
            for item in items:
                wanted.update(inscriptions_in(item.text))
                walk(item.replies)

        walk(shown)
        if not wanted:
            return {}
        out: dict[str, str] = {}
        try:
            _, index = _token_chain()
            for piece in wanted:
                row = index.inscription(piece)
                if row is not None:
                    out[piece] = row["content_type"] or ""
        except Exception:
            return {}
        return out

    def _bylines(shown: list[Any], waiting: Any = None) -> dict[str, dict[str, Any]]:
        """Who wrote these, as @tags and pictures, read from the chain.

        Never stored beside a post: a tag can move, and a copy would be a name
        that used to be right (D-137).
        """
        addresses = set()

        def walk(items):
            for item in items:
                addresses.add(item.author)
                walk(item.replies)

        walk(shown)
        out: dict[str, dict[str, Any]] = {}
        tags = _tags_for(sorted(addresses))
        for address in addresses:
            # The pool first, for both: a name claimed a minute ago and a face
            # changed a minute ago are things this node can already see, and
            # waiting for a block to draw them is the wait the pool read was
            # meant to remove (D-144).
            face = (waiting.face_for(address) if waiting is not None else "")
            tag = (waiting.tag_for(address) if waiting is not None else "")
            out[address] = {"tag": tag or tags.get(address, ""),
                            "face": _face_still_held(face) or _face_for(address)}
        return out

    def _face_still_held(piece: str) -> str:
        """A piece announced in the pool, if the chain says they hold it.

        The same check a confirmed one gets: an announcement is somebody
        saying something, and holding it is what makes it true (D-138).
        """
        if not piece:
            return ""
        try:
            _, index = _token_chain()
            row = index.inscription(piece)
        except Exception:
            return ""
        return piece if row is not None else ""

    def _face_for(address: str) -> str:
        """The inscription somebody uses as a profile picture, if they still
        hold it. Checked on every draw, because a picture of a piece somebody
        has sold is a picture of somebody else's property (D-138)."""
        if not address:
            return ""
        try:
            with state.store() as store:
                said = store.key_for(address)
            piece = (said["pfp"] or "") if said is not None else ""
            if not piece:
                return ""
            chain, index = _token_chain()
            row = index.inscription(piece)
            if row is None or row["owner"] != address:
                return ""
            return piece
        except Exception:
            return ""

    def _operator_friends(request: Request) -> list[str]:
        """The operator's own address book, as addresses, for the Friends feed.

        The operator's book is on this node already (the contacts table), so it
        is handed to the page; an account's is in its browser and the page
        reads it there. Nothing for a public request.
        """
        if _public_request(request) or not state.store_path.exists():
            return []
        try:
            with state.store() as store:
                return sorted({a for row in store.contacts()
                               for a in (row["testnet_address"],
                                         row["mainnet_address"]) if a})
        except Exception:
            return []

    @app.get("/feed", response_class=HTMLResponse)
    def feed_page(request: Request, before: str | None = None, sort: str = "popular",
                  post: str | None = None):
        """Everybody's posts, the endorsed first: likes, shares, and tips
        weighted by what they gave (2026-09-23; feed.py says the
        arithmetic and store.py runs it as the query's own ORDER BY).

        `before` is a string because the endorsed order's cursor is a pair
        that has to survive the round trip -- an int is what the profile
        page's own order needs, and this takes either.
        """
        chain = state.messaging
        mine = _tag_of_whoever_is_asking(request)
        # Popular, the default, or New: newest first (2026-09-25). And
        # Friends: New, narrowed in the browser to the people in the reader's
        # address book. An account's book lives only in its browser, and asking
        # the node for "these people's posts" would hand it the book, so the
        # node sends the newest posts and the page hides the rest.
        sort = sort if sort in ("new", "friends") else "popular"
        rows, cursor, waiting = _feed_page(
            chain.network, before=before,
            sort="new" if sort == "friends" else sort)
        # One post and its thread: where a notification points (2026-09-25).
        wanted = (post or "").strip().lower()
        if len(wanted) == 64 and all(c in "0123456789abcdef" for c in wanted):
            with state.store() as store:
                rows = list(store.conn.execute(
                    "SELECT * FROM group_post WHERE network = ? AND txid = ?",
                    (chain.network, wanted)))
            cursor = None
        shown = _shown(rows, chain.network, waiting, me=mine["address"])
        # Looking at it is reading it. Marked BEFORE the page is rendered, so
        # the count beside Feed is gone by the time it is drawn rather than
        # one refresh later (D-108, and the badge the operator watched stay).
        try:
            with state.store() as store:
                store.mark_board_read(chain.network)
        except Exception:
            pass                      # a badge is not worth failing a page for
        return render(request, "feed.html", chain=chain, posts=shown,
                      bylines=_bylines(shown, waiting),
                      drawable=_drawable_in(shown), cursor=cursor, whose=None,
                      here="/feed" if sort == "popular" else f"/feed?sort={sort}",
                      sort=sort, mine=mine, kinds=feedlib.BY_NAME,
                      friends=_operator_friends(request) if sort == "friends" else [],
                      when=_when, node=chain.status())

    @app.get("/u/{tag}", response_class=HTMLResponse)
    def profile_page(request: Request, tag: str, before: int | None = None):
        """One person's feed. Every @tag on every page links here."""
        chain = state.messaging
        mine = _tag_of_whoever_is_asking(request)
        wanted = (tag or "").strip().lstrip("@").lower()
        address, claiming = _address_of_tag(wanted)
        rows, cursor, waiting = ([], None, None) if not address else _feed_page(
            chain.network, author=address, before=before)
        shown = _shown(rows, chain.network, waiting, me=mine["address"])
        return render(request, "feed.html", chain=chain, posts=shown,
                      bylines=_bylines(shown, waiting),
                      drawable=_drawable_in(shown), cursor=cursor,
                      whose={"tag": wanted, "address": address,
                             "claiming": claiming,
                             "face": _face_for(address),
                             **{k: v for k, v in _profile_of(address, waiting).items()
                                if k in ("bio", "url")}},
                      here=f"/u/{wanted}",
                      mine=mine, kinds=feedlib.BY_NAME, when=_when,
                      node=chain.status())

    #: How many transactions a picture posted to the feed may take. A post
    #: should feel like a post: this is inscribed while the request waits,
    #: because the post has to carry the id and the id is the first piece's
    #: txid. Anything larger belongs on the NFTs page, where inscribing has a
    #: progress bar and can be resumed (D-138).
    POST_PIECE_LIMIT = 6

    def _inscribe_for_post(data: bytes, name: str, content_type: str) -> str:
        """Inscribe a file posted to the feed, and return its inscription id.

        The same inscription as any other: owned by the poster, sellable,
        rendered by the same viewer. What is different is only that the
        wallet does it on the way to a post rather than being asked.
        """
        chain, _ = _token_chain()
        # From the bytes, not from what the browser said they were: the
        # content type is what every reader will draw it as, and a sender's
        # say-so is not evidence (media.sniff, and the rule the viewer
        # already follows).
        found = media.sniff(data)
        kind = found.content_type if found else (
            (content_type or "").strip() or "application/octet-stream")
        plan = inscribelib.plan(data, kind, "")
        if plan.chunks > POST_PIECE_LIMIT:
            raise ValueError(
                f"that file needs {plan.chunks} transactions and a post takes "
                f"at most {POST_PIECE_LIMIT}. Inscribe it from the NFTs page, "
                f"where it can be watched and resumed, then paste its link.")
        with chain.rpc() as rpc:
            sender_obj = MessageSender(rpc, chain.params, public_only=True)
            address = funded_address(rpc, mainnet=chain.is_mainnet)
            inscribelib.prepare_wallet(sender_obj, address, plan)
            txids = sender_obj.send_all(address, plan.payloads)
        if not txids:
            raise ValueError("the node took none of it")
        # The inscription is named by its FIRST transaction, which is the one
        # carrying the manifest.
        return txids[0]

    def _my_picture() -> str:
        """The piece this wallet publishes as its face, if it still holds it.

        Checked here as well as when drawing, because announcing a picture of
        something you have sold puts a claim on the chain that is wrong the
        moment it lands (D-138).
        """
        chosen = str(state.setting("pfp", "") or "")
        if not chosen:
            return ""
        for chain in state.token_chains:
            try:
                row = state.token_index(chain).inscription(chosen)
                if row is None:
                    continue
                with chain.rpc() as rpc:
                    if row["owner"] in set(_ledger_addresses(rpc)):
                        return chosen
            except HTTPException:
                raise
            except Exception:
                continue
        return ""

    @app.post("/profile/picture", response_class=HTMLResponse)
    def set_profile_picture(request: Request, piece: str = Form(""),
                            csrf_token: str = Form("")):
        """Choose your face and put it on the chain, in one press.

        A picture that is saved and not published does nothing for anybody:
        it is the announcement that carries it, and the announcement is what
        every other wallet reads (D-138). This publishes the SAME tag with
        the new picture -- changing a face is not changing a name, and
        nothing here claims anything (the operator).
        """
        try:
            check_csrf(csrf_token)
            chosen = inscriptionlib.inscription_in(piece) if piece.strip() else ""
            if piece.strip() and not chosen:
                raise ValueError("a profile picture is an inscription on one of "
                                 "these chains: give its id.")
            state.set_setting("pfp", chosen)
        except HTTPException:
            raise
        except Exception as exc:
            state.flash(str(exc), "err")
            return RedirectResponse("/contacts", status_code=303)
        # The same announcement the Publish button makes, with the tag left
        # exactly as it is: `publish_key` reads the picture itself.
        return publish_key(request, csrf_token=csrf_token, confirmed="yes",
                           say_tag=(_my_tag()["tag"] or None))

    def _needs_a_name() -> str:
        """Why this wallet cannot post yet, or "".

        Posting asks for a name first, so everything in the feed has a person
        behind it and every byline goes somewhere (D-138). One button away,
        on the page this says it on.
        """
        if not state.unlocked:
            return "waiting for the testnet node"
        if not (_my_tag()["tag"] or ""):
            return ("claim a @tag first -- it is the name your posts appear "
                    "under, and it is one button on the address book")
        return ""

    @app.post("/feed/post")
    def feed_post(request: Request, text: str = Form(""),
                  csrf_token: str = Form(""),
                  attachment: UploadFile | None = File(None)):
        """Say something. A file becomes an inscription and the post links it."""
        try:
            check_csrf(csrf_token)
            complaint = _needs_a_name()
            if complaint:
                raise ValueError(complaint)
            said = (text or "").strip()
            data = attachment.file.read() if attachment and attachment.filename else b""
            if data:
                # A file posted to a feed is an inscription like any other:
                # owned, sellable, and rendered by the same viewer (D-138).
                # The post carries its id rather than its bytes, which is what
                # keeps a post one cheap transaction.
                piece = _inscribe_for_post(data, attachment.filename or "",
                                           attachment.content_type or "")
                said = (said + f"\n/content/{piece}").strip()
            if not said:
                raise ValueError("say something, or attach something")
            state.post(said)
        except HTTPException:
            raise
        except Exception as exc:
            state.flash(str(exc), "err")
        return RedirectResponse("/feed", status_code=303)

    def _feed_thing(network: str, txid: str) -> dict[str, Any] | None:
        """Whatever that transaction is on the feed: a post, or a comment.

        A comment is a `feed_act` row and a post is a `group_post` row, and
        anything you can like you can tip -- so the tip page has to accept
        either. Looking in one table is why tipping a comment said "no such
        post on this chain" (D-143). The mempool is asked last, because
        something broadcast a moment ago is in neither table yet.
        """
        with state.store() as store:
            post = store.feed_post_by_txid(network, txid)
            if post is not None:
                return {"txid": txid, "author": post["sender"],
                        "text": post["text"], "block_time": post["block_time"],
                        "what": "post"}
            act = store.feed_act_by_txid(network, txid)
        if act is not None and act["kind"] in (feedlib.REPLY, feedlib.SHARE):
            return {"txid": txid, "author": act["author"], "text": act["text"],
                    "block_time": act["block_time"],
                    "what": feedlib.NAMES.get(act["kind"], "comment")}
        waiting = _pending_feed(network)
        for row in waiting.posts:
            if row["txid"] == txid:
                return {"txid": txid, "author": row["sender"],
                        "text": row["text"], "block_time": 0, "what": "post"}
        for row in waiting.acts:
            if row["txid"] == txid and row["kind"] in (feedlib.REPLY, feedlib.SHARE):
                return {"txid": txid, "author": row["author"],
                        "text": row["text"], "block_time": 0,
                        "what": feedlib.NAMES.get(row["kind"], "comment")}
        return None

    def _profile_of(address: str, waiting: Any = _POOL) -> dict[str, Any]:
        """What somebody has published about themselves.

        The pool first and the store second, so a bio written a minute ago
        is on the page now (D-144). Everything here is what THEY said: a
        wallet repeating somebody's own words is not vouching for them.

        Say nothing about `waiting` and the mempool is read for you. That
        default exists because the callers that omit it are the ones drawing a
        page for somebody, and the case that matters is a profile broadcast
        minutes ago -- which is in the pool and nowhere else yet. A caller that
        genuinely means "ignore the pool" passes `None`; passing an empty pool
        by accident is a page showing the profile from before the one this
        account just paid to publish.
        """
        out = {"bio": "", "url": "", "pfp": "", "tag": "", "mainnet": ""}
        if not address:
            return out
        if waiting is _POOL:
            waiting = _pending_feed(state.messaging.network)
        if waiting is not None:
            for row in reversed(waiting.said):
                if row["address"] == address:
                    out.update({"bio": row.get("bio", ""),
                                "url": row.get("url", ""),
                                "pfp": row.get("pfp", ""),
                                "tag": row.get("tag", "")})
                    break
        if not any((out["bio"], out["url"], out["pfp"])):
            try:
                with state.store() as store:
                    said = store.key_for(address)
            except Exception:
                said = None
            if said is not None:
                out.update({"bio": said["bio"] or "", "url": said["url"] or "",
                            "pfp": said["pfp"] or "",
                            "tag": said["tag"] or "",
                            "mainnet": said["other_address"] or ""})
        return out

    @app.get("/u/{tag}/wallet", response_class=HTMLResponse)
    def profile_wallet(request: Request, tag: str):
        """What somebody holds, read from the chain for the addresses their
        tag names. Not this wallet's own page: nothing here can be spent,
        and nothing here is private -- it is a chain, and anybody can look.
        """
        wanted = (tag or "").strip().lstrip("@").lower()
        address, _claiming = _address_of_tag(wanted)
        profile = _profile_of(address)
        holdings: list[dict[str, Any]] = []
        for context in state.token_chains:
            where = address if context.network == state.messaging.network \
                else profile.get("mainnet", "")
            if not where:
                continue
            entry: dict[str, Any] = {"chain": context, "address": where,
                                     "coins": None, "tokens": [], "pieces": []}
            try:
                index = state.token_index(context)
                entry["tokens"] = [row for row in index.balances([where])
                                   if row["balance"]]
                entry["pieces"] = index.inscriptions(owners=[where], limit=24)
            except Exception:
                pass
            try:
                with context.rpc() as rpc:
                    entry["coins"] = sum(
                        float(row.get("amount", 0))
                        for row in (rpc.call("listunspent", 1, 9_999_999,
                                             [where]) or []))
            except HTTPException:
                raise
            except Exception:
                entry["coins"] = None     # their balance is the node's to know
            holdings.append(entry)
        return render(request, "profile_wallet.html", tag=wanted,
                      address=address, profile=profile, holdings=holdings)

    @app.post("/profile/about")
    def set_profile_about(request: Request, bio: str = Form(""),
                          url: str = Form(""), csrf_token: str = Form("")):
        """A line about yourself and a link, published with your tag."""
        try:
            check_csrf(csrf_token)
            said = " ".join((bio or "").split())
            link = (url or "").strip()
            if len(said.encode()) > envelopelib.MAX_ANNOUNCE_BIO:
                raise ValueError(
                    f"a bio is at most {envelopelib.MAX_ANNOUNCE_BIO} "
                    f"characters; this one is {len(said.encode())}")
            if link and not link.startswith(("https://", "http://")):
                raise ValueError("a link starts with https:// -- it is shown "
                                 "on your page as words, never as somewhere to "
                                 "click")
            if len(link.encode()) > envelopelib.MAX_ANNOUNCE_URL:
                raise ValueError(
                    f"a link is at most {envelopelib.MAX_ANNOUNCE_URL} characters")
            state.set_setting("bio", said)
            state.set_setting("url", link)
        except HTTPException:
            raise
        except Exception as exc:
            state.flash(str(exc), "err")
            return RedirectResponse("/contacts", status_code=303)
        return publish_key(request, csrf_token=csrf_token, confirmed="yes",
                           say_tag=(_my_tag()["tag"] or None))

    @app.get("/feed/{txid}/tip", response_class=HTMLResponse)
    def tip_page(request: Request, txid: str):
        """Who wrote this, where they take payment, and the form to send one."""
        chain = state.messaging
        row = _feed_thing(chain.network, txid)
        if row is None:
            state.flash("nothing on this chain with that transaction id", "err")
            return RedirectResponse("/feed", status_code=303)
        author = row["author"]
        tag = _tags_for([author]).get(author, "")
        # Where they said to pay them, on each chain, from their own
        # announcement (D-137). Never guessed at: an address nobody published
        # is an address nobody asked to be paid at.
        wheres = []
        with state.store() as store:
            said = store.key_for(author)
        for context in state.token_chains:
            if context.network == chain.network:
                where = author
            else:
                where = (said["other_address"] or "") if said is not None else ""
            if where:
                wheres.append({"network": context.network, "label": context.label,
                               "address": where})
        return render(request, "tip.html", chain=chain, post=row, tag=tag,
                      author=author, wheres=wheres, when=_when)

    @app.post("/feed/{txid}/tip")
    def tip_send(request: Request, txid: str, network: str = Form(""),
                 amount: str = Form(""), csrf_token: str = Form("")):
        try:
            check_csrf(csrf_token)
            row = _feed_thing(state.messaging.network, txid)
            if row is None:
                raise ValueError("nothing on this chain with that transaction id")
            with state.store() as store:
                said = store.key_for(row["author"])
            context = next((c for c in state.token_chains
                            if c.network == network), None)
            if context is None:
                raise ValueError("no such chain here")
            where = row["author"] if network == state.messaging.network else (
                (said["other_address"] or "") if said is not None else "")
            if not where:
                raise ValueError(
                    "they have not published an address on that chain, so "
                    "there is nowhere to send it")
            sats = parse_amount(amount, True)
            txid_out = state.send_tip(network, txid, where, sats)
            state.flash(f"Tipped. {txid_out} is on its way, and the post shows "
                        f"it once its block lands.", "ok")
        except HTTPException:
            raise
        except Exception as exc:
            state.flash(str(exc), "err")
            return RedirectResponse(f"/feed/{txid}/tip", status_code=303)
        return RedirectResponse("/feed", status_code=303)

    @app.post("/feed/{txid}/{doing}")
    def feed_act(request: Request, txid: str, doing: str,
                 text: str = Form(""), csrf_token: str = Form(""),
                 back: str = Form("/feed")):
        """Like, unlike, reply, share, edit or delete one post.

        One route for all of them: they are one message type with a kind
        byte, and a route per kind would be six copies of this (D-138).
        """
        kinds = {"like": feedlib.LIKE, "unlike": feedlib.UNLIKE,
                 "dislike": feedlib.DISLIKE, "undislike": feedlib.UNDISLIKE,
                 "reply": feedlib.REPLY, "share": feedlib.SHARE,
                 "edit": feedlib.EDIT, "delete": feedlib.DELETE}
        try:
            check_csrf(csrf_token)
            if doing == "mute":
                # Local, free, and tells nobody (D-138).
                with state.store() as store:
                    store.mute(text.strip() or txid, on=True)
                state.flash("Muted. Their posts are hidden here and nowhere "
                            "else -- they are not told, and the counts on "
                            "their posts do not change.", "ok")
                return RedirectResponse(back or "/feed", status_code=303)
            if doing == "unmute":
                with state.store() as store:
                    store.mute(text.strip() or txid, on=False)
                return RedirectResponse(back or "/feed", status_code=303)
            if doing not in kinds:
                raise HTTPException(404, "no such thing to do to a post")
            complaint = _needs_a_name()
            if complaint:
                raise ValueError(complaint)
            state.send_feed_act(kinds[doing], txid, text)
        except HTTPException:
            raise
        except Exception as exc:
            state.flash(str(exc), "err")
        return RedirectResponse(back or "/feed", status_code=303)

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
                said = store.key_for(address.strip())
                store.save_contact(
                    pubkey=raw, name=name.strip(),
                    testnet_address=address.strip(),
                    # Everything that announcement carries, not just the
                    # address the button happened to know (D-139).
                    mainnet_address=(said["other_address"] or "") if said else "")
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
            # The picture goes out with the name and the addresses: one
            # search then finds everything somebody needs to know about
            # whoever holds that tag (D-138). Only if this wallet still holds
            # the piece -- announcing one you have sold would be announcing
            # somebody else's property.
            face = _my_picture()
            payload = build_key_announcement(
                state.identity.public_bytes, home_hash, "",
                other_hash160=other_hash, tag=tag, pfp=face,
                bio=str(state.setting("bio", "") or ""),
                url=str(state.setting("url", "") or ""))
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
        chain, _ = _token_chain()
        return render(request, "wallet.html", messaging=messaging_status(),
                      ledger=ledger_status(), prepared=None, which=None, now=time.time(),
                      mining=_mining_json(), tab="coins", chain=chain,
                      other_chains=[c for c in state.token_chains if c is not chain])

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
        return _again("/wallet", prepared=prepared, which=which,
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

    def _token_page_data(addresses: list[str] | None = None) -> dict[str, Any]:
        """Everything /tokens shows, with the node's absence explained, not hidden.

        `addresses` says whose wallet the page is about. Left alone it is the
        node's own, which is what an operator needs and the only thing an
        operator should be shown by default: on a public instance the caller
        passes the looking account's own address and this function never reads
        the node's wallet at all. Which addresses of the node's hold coins is
        not something an operator hands out by forgetting a branch
        (docs/multi-user.md §7).
        """
        chain, index = _token_chain()
        data: dict[str, Any] = {
            "chain": chain, "node": chain.status(),
            "other_chains": [c for c in state.token_chains if c is not chain],
            "index": index.status(node_tip=state.ledger_tips.get(chain.network)),
            "tokens": [], "pending_tokens": [], "holdings": [],
            "funded": [], "owned": set(),
            "pending": [], "node_error": None, "faces": {}, "my_pictures": [],
        }
        try:
            data["tokens"] = index.properties()
        except Exception as exc:
            data["node_error"] = f"the token index could not be read: {exc}"
            return data
        # And the ones created a minute ago. This wallet already showed its
        # OWN from what it remembered broadcasting; anybody else's simply did
        # not exist for a block (D-146). Marked pending, never stored, and
        # dropped the moment the pool does.
        try:
            settled = {str(row["name"]).strip().lower() for row in data["tokens"]}
            data["pending_tokens"] = [
                row for row in index.pending_properties()
                if str(row["name"]).strip().lower() not in settled]
        except Exception:
            data["pending_tokens"] = []
        if addresses is None:
            try:
                with chain.rpc() as rpc:
                    owned = _ledger_addresses(rpc)
                    data["funded"] = _funded_addresses(rpc)
            except HTTPException:
                raise          # a rejected form is a 400, not an error page
            except Exception as exc:
                data["node_error"] = str(exc)
                owned = []
        else:
            # Somebody else's page about somebody else's coins. The node's
            # wallet is not read, so there is nothing here that could name it.
            owned = [a for a in addresses if a]
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
        # Pruned above either way; shown only to the wallet that sent them. The
        # sentence on the page says "your", and on somebody else's page these
        # transactions belong to this node's own wallet -- so what they see
        # instead is `pending_tokens` above, which is the mempool and is
        # everybody's.
        data["pending"] = ([] if addresses is not None else
                           [i for i in still if i["network"] == chain.network])
        data["faces"] = _faces_for(index, data["tokens"])
        # Pictures this wallet could give a token as its icon, offered rather
        # than asked for: an inscription id is 64 characters nobody types.
        try:
            data["my_pictures"] = [
                {"txid": row["txid"],
                 "label": (_fromjson(row["json"]) or {}).get("name")
                          or f"#{row['number']:,}"}
                for row in index.inscriptions(owners=sorted(owned), limit=200)
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
        what people call them; the payload is still an inscription (D-030).

        Drawn for the reader, which on a public instance is not this wallet.
        Two things follow. The inscribe controls are the account's -- this node
        has no key here and `/inscriptions/create` is shut, so the node's forms
        were buttons that answered "Not here" to the one person who could have
        made the thing (§ "Item 1 reopened"). And `owned` means whose the
        pieces on this page are, which for a stranger is nobody and for an
        account is the address it told this node. Left as this wallet's
        addresses, the page tells a visitor that the node's pieces are theirs
        and offers the node's wallet page to send them with.
        """
        data = _inscription_page_data(page=page)
        if _public_request(request):
            account = signed_in(request)
            address = (_account_address(account.pubkey, data["chain"])
                       if account else "")
            data["owned"] = {address} if address else set()
            data["mine"] = []
            data["unfinished"] = []
            data["funded"] = []
            data["account_address"] = address
            data["account_signed"] = account is not None
        return render(request, "inscriptions.html", **data)

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
            return _again("/inscriptions", plan=plan,
                          attached_b64=base64.b64encode(content).decode(),
                          attached_name=name, attached_type=kind,
                          json_field=json_field)
        except HTTPException:
            raise
        except Exception as exc:
            error = str(exc)
        return _again("/inscriptions", error=error, json_field=json_field)

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
                                "other_chains": [c for c in state.token_chains
                                                 if c is not chain],
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
                                # The mintpad offered at step 2: OFF unless
                                # the person asks for it. A collection and a
                                # shop are two decisions, and inscribing a
                                # shop front nobody asked for spends coins
                                # on a page they did not want (D-036, D-149).
                                "pad_on": False, "pad_amount": "",
                                "pad_kind": "coins", "pad_token": "",
                                "pad_html": "",
                                "clash": "", "partly": "", "says": {},
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
        # The mintpad's own page, for the editor at step 2. Generated with
        # the REAL creator address rather than a placeholder, because what
        # the box shows is what gets inscribed -- and the two substitutions
        # that point it at the build folder are handed over as data so the
        # live preview cannot drift from the page it is previewing.
        build = data.get("build")
        folder = str(getattr(build, "folder", "") or "")
        if build is not None and not data.get("pad_html"):
            try:
                data["pad_html"] = _pad_page(
                    data.get("sender") or "",
                    str(data.get("name") or build.collection))
            except Exception:
                data["pad_html"] = ""
        # `</` is escaped because both of these are JSON embedded in an
        # inline <script>, and PREVIEW_NOTE contains a literal `</script>`.
        # An HTML parser ends the script block at that sequence wherever it
        # appears -- inside a string literal included -- so the page's whole
        # script died silently and every button on the mintpad panel did
        # nothing. `<\/` is the same character to JavaScript and invisible
        # to the HTML parser. Found in a browser; no server-side check
        # could have seen it, because the bytes are correct.
        data["pad_swaps"] = json.dumps(_preview_swaps(folder)).replace("</", "<\\/")
        data["pad_note"] = json.dumps(PREVIEW_NOTE).replace("</", "<\\/")
        return data

    def _what_is_already_there(sender: str, build, label: str = "") -> dict[str, Any]:
        """What of this build is on the chain already, and what that means.

        Asked twice: at review, so the creator reads it before the fee is
        quoted, and at the press, where it refuses. A collection is
        (creator, name), so this only ever speaks about the creator's OWN
        sets -- somebody else's set of the same name is not in the way and
        cannot be (D-120).

        Three answers about the chain. Nothing there: carry on. All of it
        there: refuse, because a second copy of every piece is a second bill
        for pieces no node will file into the set. Some of it there: send the
        rest -- the half-finished run, where paying again for the half that
        went up is the mistake this exists to stop.

        And two about this node's own runs, both scoped to the CURRENT floor,
        because a run belongs to a chain era and the run list survives a reset
        that the index does not (D-125):

        * one still going -- resume it, do not start a second;
        * one finished whose pieces the index has not caught up with yet,
          which is the minutes between the last broadcast and its block.

        A finished run whose set IS on the chain says nothing this function
        cannot see for itself, and a finished run on a chain that no longer
        reads its pieces says nothing at all.
        """
        out: dict[str, Any] = {"blocked": "", "note": "", "skip": set()}
        collection = build.collection
        if not sender or not collection:
            return out
        # What this node wrote down about its own runs, which asks nothing of a
        # node: `token_chain` reads a file and `jobs.list` reads the job book. The
        # guard that refuses a run already under way is about those records alone,
        # so it no longer sits behind the index read below. It used to, which
        # meant an index that would not answer -- a node restarting, a chain set
        # to one this node is not indexing -- switched the refusal off, and the
        # press then started a second run of the same set and paid for every piece
        # twice. (D-120 is about this refusal costing nothing to hear; this is
        # about it not being switchable off by a fault somewhere else.)
        chain = state.token_chain
        jobs, _ = state.collections
        named = {collection, label.strip()} - {""}
        floor = chain.params.activation_height
        mine = [job for job in jobs.list(chain.network)
                if job.get("sender") == sender
                and str(job.get("name") or "").strip() in named]
        for job in mine:
            if (job.get("floor") == floor
                    and job.get("status") in collectionlib.UNFINISHED):
                out["blocked"] = (
                    f"{collection} is already being inscribed from this "
                    f"address by run {job['id']}. Open that run and resume "
                    f"it rather than starting a second one.")
                return out

        try:
            done = state.token_index(chain).collection_editions(sender, collection)
        except Exception:
            # The index is the wizard's business, and "nothing of it is indexed"
            # is the honest reading of an index that will not answer. The
            # finished-at-this-floor scan below is what speaks on that reading, so
            # an out-of-the-way index makes this say less, not nothing.
            done = set()
        out["skip"] = {item.edition for item in build.items if item.edition in done}
        if out["skip"] and len(out["skip"]) == len(build.items):
            out["blocked"] = (
                f"{collection} is already on this chain from this address, "
                f"all {len(done):,} pieces of it. Inscribing it again would "
                f"pay for a second copy of every item, and no node would file "
                f"the copies into the set.")
            return out

        if not out["skip"]:
            # Nothing of it is indexed. A run of this name that finished on
            # THIS chain minutes ago is the gap: its pieces are broadcast and
            # not yet in a block, so the index is honestly empty and a second
            # run would pay for the set twice over.
            finished = [job for job in mine
                        if job.get("floor") == floor and job.get("status") == "done"]
            if finished:
                job = finished[-1]
                when = dt.datetime.fromtimestamp(
                    float(job.get("created") or 0)).strftime("%d %b %H:%M")
                out["blocked"] = (
                    f"this wallet already inscribed {collection} on this chain "
                    f"-- run {job['id']}, started {when}, {job.get('items', 0):,} "
                    f"pieces. None of it is indexed yet, which is the minutes "
                    f"between the last piece being sent and its block. Wait for "
                    f"it rather than pay for the set twice.")
                return out
            stale = [job for job in mine if job.get("floor") != floor]
            if stale:
                out["note"] = (
                    f"a run of {collection} from before this chain started at "
                    f"{floor:,} is being ignored: the pieces it sent are below "
                    f"the floor, so no node reads them.")
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
        # The held context goes in BEFORE the page data is built, not only after
        # it, because the mintpad's HTML box is generated from the build, and the
        # build only ever arrives with the redirect now that the POSTs answer
        # with one. Asked to build the page without it, this looked for a build
        # that was not there yet and handed the editor an empty box -- which the
        # page's own JavaScript then treats as "the standard page to put back".
        # (2026-09-22, found from a test of what the box holds.)
        return render(request, "collection_wizard.html",
                      **_collection_page_data(
                          **_picked_up(request.query_params.get("held", ""))))

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
            chain = state.token_chain
            with chain.rpc() as rpc:
                sender = (_check_own_address(rpc, fromaddress) if fromaddress
                          else funded_address(rpc, mainnet=chain.is_mainnet))
            # Priced on what this run would actually send, so the number on
            # the page is the number that is charged (D-120).
            already = _what_is_already_there(sender, build)
            if not already["blocked"]:
                # Only when there is a run left to price. Stripping the
                # skipped pieces out of a build that is ENTIRELY on the chain
                # leaves no items at all, and the page then died on
                # `build.items[0]` -- "list object has no element 0" -- while
                # the refusal it was about to print sat in `clash`, unread.
                # A crash in front of a message is worse than no message: it
                # says nothing and looks like a fault in the wizard (D-128).
                build = _without(build, already["skip"])
            cost = collectionlib.estimate_build(build)
            # What the #1 already says about the SET, shown before the press.
            # That object is what a marketplace reads for the whole
            # collection (D-097), it is on the chain for ever, and it was the
            # one thing on this screen nobody could see: a build wrote
            # "artist": a real name into it and the only reason it was not
            # inscribed was an unrelated defect stopping the run (a test machine).
            first = min(build.items, key=lambda i: i.edition) if build.items else None
            about = (inscriptionlib.collection_details(first.json)
                     if first is not None else {})
            return _again("/inscriptions/collection",
                          build=build, cost=cost, sender=sender,
                          clash=already["blocked"], partly=already["note"],
                          says=about, preview=build.items[:12])
        except HTTPException:
            raise
        except Exception as exc:
            return _again("/inscriptions/collection",
                          error=str(exc), folder=folder)

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

    #: Said IN the page rather than around it: the frame is sandboxed with no
    #: same-origin, so this is the only way to reach the button inside it.
    PREVIEW_NOTE = (
        "<script>addEventListener('DOMContentLoaded',function(){"
        "var b=document.getElementById('buy');"
        "if(b){b.disabled=true;b.textContent='Preview \u2014 nothing is on the chain yet';}"
        "var f=document.getElementById('foot');"
        "if(f){f.textContent='This is the page that will be inscribed. "
        "The pictures are the ones in your build folder.';}});</script>")

    def _preview_swaps(folder: str) -> list[list[str]]:
        """What turns the real page into one that draws from a build folder.

        Defined once and handed to the browser as data, because the wizard's
        live editor has to make exactly the same two substitutions on
        whatever the person has typed. Two copies of this list -- one here,
        one in JavaScript -- is a preview that quietly stops matching what
        gets inscribed the first time either is touched.
        """
        where = f"/inscriptions/collection/preview/set?folder={quote(folder)}"
        return [
            ["'/r/collection/' + CREATOR + '/' + COLLECTION + '?limit=100&offset='",
             f"'{where}&offset='"],
            ["'/content/' + id",
             f"'/inscriptions/collection/preview/piece?folder={quote(folder)}&n=' + id"],
        ]

    def _as_preview(page: str, folder: str) -> str:
        for old, new in _preview_swaps(folder):
            page = page.replace(old, new)
        return page + PREVIEW_NOTE

    def _edited_pad(said: str, sender: str, collection: str) -> str:
        """The page as typed, if it is not simply the standard one.

        Compared after normalising line endings, because a textarea posts
        CRLF whatever it was given -- so a page nobody touched came back
        "different" on every byte of every line, and every run would have
        carried a frozen copy of that day's template.
        """
        said = (said or "").replace("\r\n", "\n").strip()
        if not said:
            return ""
        try:
            standard = _pad_page(sender, collection)
        except Exception:
            return said
        return "" if said == standard.replace("\r\n", "\n").strip() else said

    def _pad_page(sender: str, collection: str) -> str:
        """The standard mintpad page for this collection, as it would be
        inscribed -- the real creator address in it, not a placeholder, so
        that what the editor shows is what goes on the chain."""
        return mintpadlib.page(sender or "preview",
                               collection or "Collection").decode("utf-8")

    @app.get("/inscriptions/collection/preview")
    def preview_mintpad(request: Request, folder: str = "", name: str = "",
                        creator: str = "preview"):
        """The mintpad page as it will be inscribed, pointed at the build."""
        build = _preview_build(folder)
        collection = (name or build.collection).strip() or "Collection"
        return HTMLResponse(_as_preview(_pad_page(creator, collection), folder),
                            headers={"Cache-Control": "no-store"})

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
                         about: str = Form(""), site: str = Form(""),
                         pad_html: str = Form("")):
        """The second press: write the job down and start it.

        The mintpad is decided here and inscribed by the runner when the last
        item is on its way, so the price is settled before anything is paid
        for rather than after (D-036).
        """
        check_csrf(csrf_token)
        chain = state.token_chain          # the rpc and the floor, not the index
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
            edited = ""
            if launchpad == "yes":
                take = mintpadlib.take_of(pad_kind, pad_amount,
                                          int(pad_token) if pad_token else None)
                identity = state.ensure_identity()
                collection = name.strip() or build.collection
                pad_json = mintpadlib.shop_json(
                    contact.encode(state.messaging.network, identity.public_bytes),
                    collection, take)
                # Only kept when it was actually changed. Storing a copy of
                # the standard page on every run would freeze each one to
                # the template of the day it was made, so a fix to the
                # mintpad would never reach a job written down yesterday.
                edited = _edited_pad(pad_html, sender, collection)
            job_id = jobs.create(chain.network, sender, build, name=name.strip(),
                                 pad_json=pad_json, pad_html=edited,
                                 floor=chain.params.activation_height)
            runner.start(job_id)
        except Exception as exc:
            # Back to the REVIEW, not to step one. A refusal here is usually
            # one field -- a price left out of a mintpad that was ticked --
            # and bouncing to the top threw away the build, the costing and
            # everything else typed, to say one word about one box (D-129).
            try:
                again = collectionlib.read_build(Path(folder.strip()))
                first = (min(again.items, key=lambda i: i.edition)
                         if again.items else None)
                return _again("/inscriptions/collection",
                              build=again, sender=fromaddress.strip(),
                              cost=collectionlib.estimate_build(again),
                              says=(inscriptionlib.collection_details(first.json)
                                    if first is not None else {}),
                              preview=again.items[:12], error=str(exc),
                              pad_on=launchpad == "yes", pad_amount=pad_amount,
                              pad_kind=pad_kind, pad_token=pad_token,
                              pad_html=pad_html)
            except Exception:
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
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "no such collection run")
        # A run from before the floor cannot be finished, only continued into
        # a different chain: the pieces it already sent are below the floor
        # and invisible, so resuming would inscribe the rest and make a set
        # with holes in it -- and no #1, which is where a set says how big it
        # is (D-125). Shown rather than hidden, because somebody paid for the
        # half that went out.
        chain, _ = _token_chain()
        floor = chain.params.activation_height
        if job.get("floor") is not None and job.get("floor") != floor:
            state.flash(
                f"that run was made when this chain started at "
                f"{int(job['floor']):,}; it now starts at {floor:,}, so the "
                f"pieces it sent are below the floor and no node reads them. "
                f"Start the build again rather than resuming into a set with "
                f"holes in it.", "err")
            return RedirectResponse(f"/inscriptions/collection/{job_id}",
                                    status_code=303)
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

    def _mainnet_identity() -> str:
        """The one mainnet address this wallet publishes as its own.

        Chosen once and remembered, because an announcement is a statement
        about WHERE somebody is and it has to keep saying the same thing.
        It used to be whichever address happened to be funded at the moment
        of publishing, which follows coin selection: republish after spending
        and the same @tag points somewhere else, with no way for anybody
        holding the old one to know (D-137, and the same mistake D-062 fixed
        for the messaging address).

        Still best-effort: a node with no ledger wallet publishes without it
        rather than not at all.
        """
        remembered = ""
        path = state.home / "mainnet-address"
        try:
            remembered = path.read_text().strip()
        except OSError:
            remembered = ""
        chain = next((c for c in state.token_chains
                      if c.network != state.messaging.network), None)
        if chain is None:
            return ""
        try:
            with chain.rpc() as rpc:
                ours = _ledger_addresses(rpc)
        except HTTPException:
            raise
        except Exception:
            return remembered         # no node: say what was said before
        if remembered in ours:
            return remembered
        if not ours:
            return ""
        # A funded one if there is one, else the first in a fixed order --
        # never "whichever the node listed first", which is not an order.
        try:
            with chain.rpc() as rpc:
                funded = [row["address"] for row in _funded_addresses(rpc)]
        except Exception:
            funded = []
        chosen = sorted(funded)[0] if funded else sorted(ours)[0]
        try:
            path.write_text(chosen + "\n")
        except OSError:
            pass                      # remembered for this run at least
        return chosen

    def _announced_extras() -> tuple[bytes, str]:
        """The other chain's address (as 20 bytes) and the tag, for publishing.

        Both are best-effort: an announcement that says less is worth more
        than one that is not made, so a node with no ledger wallet or no tag
        still publishes its key and its messaging address.
        """
        other_hash = b""
        try:
            mine = _mainnet_identity()
            if mine:
                _, other_hash = b58check_decode(mine)
        except HTTPException:
            raise
        except Exception:
            other_hash = b""
        return other_hash, (_my_tag()["tag"] or "")

    def _pending_tags() -> dict[str, str]:
        """Names claimed in the pool: address -> tag."""
        try:
            _, index = _tag_chain()
            return {row["address"]: row["tag"] for row in index.pending_tags()}
        except Exception:
            return {}

    def _address_of_tag(wanted: str) -> tuple[str, bool]:
        """Who holds that name, and whether it is still waiting for a block.

        The chain first: a confirmed claim beats a pending one, because that
        is what first-claim-wins means. The pool only answers for a name
        nothing has settled yet (D-146).
        """
        try:
            _, index = _tag_chain()
            settled = index.address_of(wanted) or ""
        except Exception:
            settled = ""
        if settled:
            return settled, False
        for address, tag in _pending_tags().items():
            if tag == wanted:
                return address, True
        return "", False

    def _about_of(data: Any) -> str:
        """A token's description as a person wrote it. An issuance that carries an
        icon stores `{"about": ..., "icon": ...}`; plain text is itself."""
        text = str(data or "")
        try:
            said = json.loads(text)
        except ValueError:
            return text
        if isinstance(said, dict):
            return str(said.get("about") or "")
        return text

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

    def _tag_of_whoever_is_asking(request: Request) -> dict[str, Any]:
        """Whose name to show on a page: the reader's, not the node's.

        `_my_tag()` is the NODE's tag, which is right on the operator's own
        pages and wrong everywhere else. Served publicly it showed every
        visitor the operator's name -- "Say something as @bigchiefenergy"
        to somebody signed in as @gx1 -- which is both a lie about who they
        are and a disclosure about who runs the node (D-158).
        """
        if not _public_request(request):
            return _my_tag()
        chain, index = _tag_chain()
        account = signed_in(request)
        tag, home = None, ""
        if account is not None:
            home = str(state.setting(f"address:{account.pubkey}", "") or "")
            try:
                tag = index.tag_of(home) if home else None
            except Exception:
                tag = None
            if not tag:
                # Signed up here and not yet on the chain: their own name
                # is still the one to call them by.
                tag = (state.vault().by_pubkey(account.pubkey) or {}
                       ).get("tag") or None
        return {"tag": tag, "address": home, "chain": chain,
                "min": taglib.MIN_LENGTH, "max": taglib.MAX_LENGTH}

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
    def inscription_content(key: str, download: int = 0, reveal: int = 0):
        """An inscription's bytes -- screened first when this arcade screens
        (arcade/moderation.py): a sensitive picture comes back blurred unless the
        viewer asked to see it (`reveal=1`), an illegal one as a notice, and one not
        yet judged is judged now (about a second) or, if the model is away, shown
        as "checking". Everything else about the response is unchanged."""
        screen = state.screen()
        if screen.enabled:
            index = _content_index()
            found = index.inscription_content(contentlib._key(key))
            if found is not None:
                answer = _screened(screen, *found, reveal=bool(reveal))
                if answer is not None:
                    return answer
        return contentlib.content(_content_index(), key, download=bool(download))

    def _screened(screen, content_type: str, body: bytes, reveal: bool):
        """The response a screened piece gets instead of its bytes, or None to serve
        them as usual."""
        from .. import moderation as mod
        if content_type.startswith("image/"):
            verdict = screen.check_image(content_type, body)
        elif content_type.startswith("text/"):
            verdict = screen.check_text(body.decode("utf-8", "replace")[:20000], now=True)
        else:
            return None                                # sound, video: not screened yet
        fresh = {**contentlib.CONTENT_HEADERS, "Cache-Control": "no-store"}
        if verdict == mod.OK:
            return None
        if verdict == mod.ILLEGAL:
            return Response(mod.notice_image("Removed by this arcade"), status_code=451,
                            media_type="image/png", headers=fresh)
        if verdict is None:
            return Response(mod.notice_image("Checking this picture\u2026"), status_code=503,
                            media_type="image/png", headers={**fresh, "Retry-After": "5"})
        if reveal:                                     # sensitive, and asked for
            return Response(body, media_type=content_type, headers=fresh)
        if content_type.startswith("image/"):
            cover = mod.blurred(body) or mod.notice_image("Sensitive picture")
            return Response(cover, media_type="image/png", headers=fresh)
        return Response("Sensitive content. Open it with ?reveal=1 if you want to see it.",
                        media_type="text/plain", headers=fresh)

    @app.get("/moderation/verdicts")
    def moderation_verdicts(ids: str = ""):
        """What this arcade decided about some pieces, for the page to label its
        covers: ok, sensitive, illegal, or null while checking. Known verdicts only:
        asking is the /content route's job."""
        screen = state.screen()
        if not screen.enabled:
            return JSONResponse({"enabled": False, "verdicts": {}})
        from .. import moderation as mod
        index = _content_index()
        out: dict[str, Any] = {}
        for key in [k for k in ids.split(",") if k][:200]:
            found = index.inscription_content(contentlib._key(key))
            out[key] = screen.known(mod.digest_of(found[1])) if found else None
        return JSONResponse({"enabled": True, "verdicts": out},
                            headers={"Cache-Control": "no-store"})

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

    @app.get("/r/profile/{tag}")
    def r_profile(tag: str):
        """Who holds a name, and what they have published under it.

        Public in every sense: it repeats what somebody put on the chain
        themselves, so an inscribed page can greet a visitor by name, show a
        shop owner's face, or link a piece to the person who made it without
        asking this wallet anything about itself (D-145).

        Read from the chain and the pool both, so a name claimed a minute
        ago answers rather than 404s -- `pending` says which (D-146).
        """
        wanted = (tag or "").strip().lstrip("@").lower()
        address, claiming = _address_of_tag(wanted)
        if not address:
            raise HTTPException(404, "nobody holds that tag on this chain")
        said = _profile_of(address, _pending_feed(state.messaging.network))
        face = _face_for(address)
        return contentlib._json({
            "tag": wanted, "address": address, "pending": claiming,
            "mainnet": said.get("mainnet", ""),
            "bio": said.get("bio", ""), "url": said.get("url", ""),
            # The picture only while the chain says they hold it: a page
            # showing a piece somebody has sold is showing somebody else's
            # property (D-138).
            "picture": face, "content": f"/content/{face}" if face else "",
        })

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
        return RedirectResponse("/docs/features.md", status_code=307)

    @app.get("/docs", response_class=HTMLResponse)
    def docs_index(request: Request):
        """Every document that ships, which is all of them (D-152)."""
        return render(request, "docs.html", pages=guidelib.pages(),
                      bugs_url=guidelib.BUGS_URL)

    @app.get("/docs/{name:path}", response_class=HTMLResponse)
    def docs_page(request: Request, name: str):
        text = guidelib.page(name)
        if text is None:
            state.flash("there is no such document", "err")
            return RedirectResponse("/docs", status_code=303)
        return render(request, "doc.html", name=name,
                      title=guidelib.title_of(name),
                      sections=guidelib.document(text))

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
        # The frame is addressed to the pages' own hostname when the operator
        # has configured one: from inside the sandbox nothing carries a
        # cookie, and that door needs none. With no second name -- one
        # person's machine -- the frame is served from here, which is the
        # same isolation minus the extra origin.
        pages = state.pages_origin
        # Who answers what the page in the frame asks for. The frame is the
        # same in all three cases; the door behind it is not: the operator's
        # copy has this node's wallet behind it, a public copy has the reading
        # account's own key, and a stranger on a public copy has neither. The
        # route says it because the page cannot tell -- one template, one set
        # of buttons, either way.
        public = _public_request(request)
        viewer = "wallet"
        if public:
            viewer = "account" if signed_in(request) is not None else "nobody"
        mine, held, coins = False, [], 0.0
        if viewer == "account":
            # One read on the public path, and it is of the looking account's
            # own holdings, not of this node's wallet: an offer can only be
            # made in something the offerer holds (D-040), so a form that
            # cannot say what those are is a form that spends somebody's fee
            # to be refused by the other side. Both numbers come off the
            # index -- the ledger's book for a token, this node's own watch of
            # the address for the coins -- and nothing here asks the wallet
            # what it holds, because on this path it holds nobody's key.
            looking = signed_in(request)
            try:
                here = _account_address(looking.pubkey, chain)
                if here:
                    held = _purses(index.balances([here]))
                    with contextlib.closing(index.open()) as db:
                        coins = utxoslib.balance(db, here) / 100_000_000
            except Exception:
                held, coins = [], 0.0
        # Said by the wallet, around the frame, because an inscribed page
        # cannot be changed to say it (D-051).
        advice = ""
        if not public:
            # Nothing below is read on the public path, and it must stay that
            # way: these are the node's own addresses, its own balances, and an
            # address it would like this visitor to send coins to. On a public
            # instance the wallet that buys is the one in the browser, and this
            # page has no reason to know anything about the machine serving it.
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
                      pages=pages, mine=mine, viewer=viewer,
                      tokens=held, coins=coins, advice=advice,
                      renders=row["content_type"].startswith(contentlib.RENDERABLE))

    @app.get("/tokens", response_class=HTMLResponse)
    def tokens(request: Request):
        """Every token on the chain, and the way to make one.

        Whose way that is belongs to the door, not to the page. `/tokens` is a
        public page, and on a public instance it used to render the operator's
        issuance form anyway -- which asked a stranger to choose which of
        this node's addresses should issue their token, and printed their
        balances doing it. So a public request gets the same list computed
        from the looking account's own address, the account's own form, and
        not one read of the node's wallet.

        An account that is not signed in gets the list and the reason there is
        no form, rather than a form that would refuse on the next button: the
        first thing it would say is "sign in first", and a page can say that
        itself. An account that IS signed in but has no address on the chain
        this page is showing gets that said instead, because the chain here is
        the node's switch and the two can disagree -- and a page that answers
        that with "sign in first" is a page that lies to somebody who did.
        """
        if _public_request(request):
            account = signed_in(request)
            address = (_account_address(account.pubkey, _token_chain()[0])
                       if account else "")
            return render(request, "tokens.html", prepared=None,
                          account_address=address,
                          account_signed=account is not None,
                          **_token_page_data([address] if address else []))
        return render(request, "tokens.html", prepared=None, **_token_page_data())

    @app.post("/tokens/chain")
    def tokens_chain(request: Request, chain: str = Form(""), csrf_token: str = Form(""),
                     back: str = Form("")):
        """Switch between mainnet and testnet, from wherever it was pressed."""
        check_csrf(csrf_token)
        try:
            state.switch_token_chain(chain)
        except ValueError as exc:
            state.flash(str(exc), "err")
        # A path of ours and nothing else: one leading slash, no scheme, no
        # second slash to make it protocol-relative.
        where = (back or "").strip()
        if not (where.startswith("/") and not where.startswith("//")):
            where = "/tokens"
        return RedirectResponse(where, status_code=303)

    def _token_action(request: Request, *, action: str, confirmed: str,
                      build, fields: dict[str, str], back: str,
                      where: str = "", **extra):
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
                         "network": chain.network,
                         # Kept so a name this wallet has just claimed is not
                         # offered again while its block is still coming
                         # (_name_is_taken, D-122).
                         "name": str(fields.get("name") or "")})
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
        # Back to the page this came from, carrying the confirmation with
        # it. The page's own data is the GET's job; only what this POST
        # decided is held.
        context = dict(error=error, prepared=prepared,
                       confirm_action=request.url.path, confirm_fields=fields,
                       confirm_what=action, back=back)
        context.update(extra)
        # `where` is the page that SHOWS a confirmation, which is not
        # always where a success goes: pricing a piece is confirmed on that
        # piece's own page and lands on the market afterwards.
        landing = (where or back or "").strip()
        if not (landing.startswith("/") and not landing.startswith("//")):
            landing = "/tokens"
        return _again(landing, **context)

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

    def _name_is_taken(name: str, chain=None) -> str:
        """Why this token name cannot be used, or "" if it can.

        The chain refuses a second token of the same name now (D-122), and a
        refused issuance still costs its fee -- so the wallet asks first. The
        mempool counts too: a name claimed by a transaction that has not been
        indexed yet is claimed, and a wallet that forgets what it broadcast
        two minutes ago will happily pay to lose the race with itself.

        `chain` is which chain to ask, and it is a parameter because there
        are two answers to "which chain" on this page. An operator's is a
        switch on the node (`_token_chain`); an account's is named by the
        request (`_chain_asked`) and can be the one the switch is NOT on.
        Checking the free name on one chain and paying for it on the other is
        precisely the mistake that makes somebody pay a fee for a name that
        was taken, so the caller that knows the chain says so.
        """
        complaint = statelib.name_complaint(name)
        if complaint:
            return complaint
        wanted = statelib.name_key(name)
        if chain is None:
            chain, index = _token_chain()
        else:
            index = state.token_index(chain)
        try:
            taken = [p for p in index.properties()
                     if statelib.name_key(p["name"]) == wanted]
        except Exception:
            return ""                     # the index is the wallet's business
        if taken:
            first = taken[0]
            return (f"{first['name']} is already token #{first['property_id']} "
                    f"on this chain. One name is one token: pick another.")
        for item in state.pending_tokens:
            if (item.get("network") == chain.network
                    and item.get("what") == "create"
                    and statelib.name_key(item.get("name", "")) == wanted):
                return (f"this wallet broadcast a token called {name.strip()} "
                        f"a moment ago ({item['txid'][:12]}...). Wait for its "
                        f"block rather than pay for the same name twice.")
        # The node's mempool, not just this wallet's memory of what it sent.
        # A name claimed on ANOTHER machine seconds ago is invisible in the
        # index and in `pending_tokens`, and that is exactly how the duplicate
        # pair happened: two wallets, neither able to see the other, both
        # paying for a name only one of them could keep (D-124).
        try:
            claimed = index.pending_names()
        except Exception:
            claimed = []
        for row in claimed:
            if statelib.name_key(row["name"]) == wanted:
                return (f"{row['name'].strip()} was claimed a moment ago by a "
                        f"transaction waiting for its block "
                        f"({row['txid'][:12]}...). The chain will keep the "
                        f"first one, so this would pay a fee for nothing.")
        return ""

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
            complaint = _name_is_taken(name)
            if complaint:
                raise tokenlib.TokenError(complaint)
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
                             form_send=fields)

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
        return _again("/wallet/tokens",
                      error=error, prepared=None, parts=prepared,
                      part_amounts=[units for _, units in parts],
                      confirm_action=request.url.path, confirm_fields=fields,
                      confirm_what="send", back="/wallet/tokens",
                      form_send=fields)

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
        holders = index.holders(property_id)
        return {
            "chain": chain, "node": chain.status(),
            "prop": prop,
            "holders": holders,
            "history": index.history(property_id=property_id),
            "index": index.status(node_tip=state.ledger_tips.get(chain.network)),
            "owned": owned,
            "is_issuer": prop["issuer"] in owned,
            "face": _faces_for(index, [prop])[property_id],
            # Who, by @name where they have one, as every list shows them; and the
            # description as its words, not the issuance JSON that carries the icon
            # beside them (filming the token tutorial, 2026-09-25).
            "tags": _tags_for([prop["issuer"]] + [h["address"] for h in holders]),
            "about": _about_of(prop.get("data")),
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
        return _again(f"/tokens/{property_id}", prepared=None, **extra)

    def _issuer_action(request: Request, property_id: int, action: str, confirmed: str,
                       fields: dict[str, str], build):
        def wrapped(rpc):
            index = state.token_index(state.token_chain)
            prop = index.property(property_id)
            if prop is None:
                raise tokenlib.TokenError(f"there is no token {property_id}.")
            return build(rpc, prop, index)
        return _token_action(request, action=action, confirmed=confirmed, build=wrapped,
                             fields=fields, back=f"/tokens/{property_id}")

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

    # --- the arcade's own face ------------------------------------------------
    #
    # One picture, vendored at two sizes rather than resized at runtime: the
    # application depends on two packages on purpose, and adding an imaging
    # library to draw a favicon would be the worst trade in the project. The
    # 180 is the artwork as it was drawn; the 32 is a LANCZOS downscale of it
    # made once and committed beside it.

    ICONS = {"/icon-32.png": "icon-32.png",
             "/icon-180.png": "icon-180.png",
             # The installable app's (manifest.webmanifest): the same artwork
             # as the homepage's, at the two sizes every platform asks for.
             "/icon-192.png": "icon-192.png",
             "/icon-512.png": "icon-512.png",
             # Browsers ask for this by name when a page carries no link tag
             # -- an error page, or a route that answers without a template.
             # It is a PNG under an .ico name, which every browser in use
             # reads by its content type rather than its extension.
             "/favicon.ico": "icon-32.png"}

    @app.get("/icon-32.png")
    @app.get("/icon-180.png")
    @app.get("/icon-192.png")
    @app.get("/icon-512.png")
    @app.get("/favicon.ico")
    def icon(request: Request):
        name = ICONS.get(request.url.path)
        if name is None:
            raise HTTPException(404, "no such icon")
        return Response(
            (TEMPLATE_DIR / name).read_bytes(), media_type="image/png",
            # A year. The picture is the application's identity; when it
            # changes, its name changes with it.
            headers={"Cache-Control": "public, max-age=31536000, immutable"})

    # --- the installable app, and push for the Messenger (2026-09-25) ---
    #
    # A PWA so a phone can put the arcade on its home screen, and -- because an
    # installed app is what iOS needs before it allows web push -- notifications
    # when a message arrives (arcade/push.py). The service worker caches ONE
    # thing, the offline page: pages, balances, keys and messages always come
    # live, so an update can never be hidden behind a stale copy.

    @app.get("/manifest.webmanifest")
    def web_manifest():
        return JSONResponse({
            "name": "DogecoinArcade", "short_name": "Arcade",
            "description": "Messages, a feed, tokens and art on Pepecoin.",
            "id": "/", "start_url": "/", "scope": "/", "display": "standalone",
            "background_color": "#faf8f4", "theme_color": "#1b1a17",
            "icons": [
                {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png",
                 "purpose": "any"},
                {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png",
                 "purpose": "any"},
            ],
        }, media_type="application/manifest+json",
            headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/sw.js")
    def service_worker():
        body = TEMPLATES.get_template("sw.js").render(
            revision=state.running_version or "dev")
        return Response(body, media_type="text/javascript", headers={
            # Never cached: a new worker has to be seen the moment the site changes.
            "Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    @app.get("/offline", response_class=HTMLResponse)
    def offline_page():
        return HTMLResponse(TEMPLATES.get_template("offline.html").render(),
                            headers={"Cache-Control": "no-cache"})

    @app.get("/push/key")
    def push_key():
        push = state.push()
        if push is None:
            return JSONResponse({"enabled": False})
        return JSONResponse({"enabled": True, "key": push.public_key()})

    @app.post("/account/push/subscribe")
    def push_subscribe(request: Request, payload: Any = Body(None)):
        account = _signed_in_account(request)
        push = state.push()
        if push is None:
            return JSONResponse({"detail": "this arcade does not send notifications"},
                                status_code=400)
        said = payload if isinstance(payload, dict) else {}
        keys = said.get("keys") if isinstance(said.get("keys"), dict) else {}
        try:
            push.subscribe(account.pubkey, str(said.get("endpoint", "")),
                           str(keys.get("p256dh", "")), str(keys.get("auth", "")))
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        _watch_for_mail()
        return JSONResponse({"ok": True})

    @app.post("/account/push/unsubscribe")
    def push_unsubscribe(request: Request, payload: Any = Body(None)):
        account = _signed_in_account(request)
        push = state.push()
        said = payload if isinstance(payload, dict) else {}
        if push is not None:
            push.unsubscribe(account.pubkey, str(said.get("endpoint", "")))
        return JSONResponse({"ok": True})

    @app.get("/account/push/news")
    def push_news(request: Request):
        """What woke this account's phone: who wrote, and when. Never what."""
        account = _signed_in_account(request)
        push = state.push()
        rows = push.news(account.pubkey) if push is not None else []
        out = []
        for row in rows:
            name = ""
            try:
                name = state.token_index(_account_chain()).tag_of(row["sender"]) or ""
            except Exception:                           # noqa: BLE001
                pass
            out.append({"from": f"@{name}" if name else row["sender"][:10] + "\u2026",
                        "txid": row["txid"], "at": row["at"]})
        return JSONResponse({"news": out}, headers={"Cache-Control": "no-store"})

    def _owners() -> dict[str, str]:
        """Every account address on this node -> the account. Read per pass, so a
        new account is covered without a restart."""
        owners: dict[str, str] = {}
        for key, value in state.settings().items():
            if key.startswith("address:") and isinstance(value, str) and value:
                owners[value] = key.rsplit(":", 1)[1]
        return owners

    def _paid(txid: str) -> list[str]:
        with state.messaging.rpc() as rpc:
            tx = rpc.call("getrawtransaction", txid, 1)
        out = []
        for vout in tx.get("vout", []):
            spk = vout.get("scriptPubKey", {})
            out.extend(spk.get("addresses") or ([spk["address"]] if spk.get("address") else []))
        return out

    _watcher: dict[str, Any] = {}

    def _watch_for_mail() -> None:
        """Start the one thread that notices mail for subscribed accounts."""
        push = state.push()
        if push is None or _watcher.get("thread") is not None:
            return

        def loop():
            while not getattr(state, "shutting_down", False):
                try:
                    owners = _owners()
                    with state.store() as store:
                        push.watch(lambda after: store.candidates_for_others(after, 200),
                                   store.newest_candidate, _paid, owners.get)
                except Exception as exc:                # noqa: BLE001 -- keep watching
                    log.info("push watch: %s", exc)
                time.sleep(4)

        _watcher["thread"] = threading.Thread(target=loop, name="arcade-push",
                                              daemon=True)
        _watcher["thread"].start()

    # --- notifications (2026-09-25) ------------------------------------
    #
    # Everything that happened to an account, or to posts it follows, read from
    # what the node already keeps (arcade/notify.py). Seen is a marker per source,
    # kept per account in settings; the red counts on the Notifications and
    # Messages tabs are what is past it.

    def _notif_seen(pubkey: str) -> dict:
        got = state.setting(f"notif_seen:{pubkey}")
        return dict(got) if isinstance(got, dict) else {}

    def _notif_events(account) -> list:
        from .. import notify
        chain = _account_chain()
        me = _account_address(account.pubkey, chain)
        events: list = []
        try:
            with state.store() as store:
                events += notify.feed_events(store.conn, chain.network, me)
        except Exception as exc:                          # noqa: BLE001
            log.info("notifications: feed: %s", exc)
        push = state.push() if hasattr(state, "push") else None
        if push is not None:
            events += notify.message_events(push.arrivals(account.pubkey))
        owners = {a for a in (_account_address(account.pubkey, c)
                              for c in _account_chains()) if a}
        try:
            with state.listings._open() as conn:
                events += notify.sale_events(conn, owners)
        except Exception as exc:                          # noqa: BLE001
            log.info("notifications: sales: %s", exc)
        return notify.merge(events, _notif_seen(account.pubkey))

    def _account_counts(request: Request) -> dict:
        """The red counts for an account's tabs; nothing for anybody else."""
        if not _public_request(request):
            return {}
        account = signed_in(request)
        if account is None:
            return {}
        try:
            events = _notif_events(account)
        except Exception:                                 # noqa: BLE001 -- never break a page
            return {}
        seen = _notif_seen(account.pubkey)
        mail = sum(1 for e in events if e.source == "message"
                   and e.seq > int(seen.get("message_tab", 0)))
        return {"notif_count": sum(1 for e in events if e.unread and e.source != "message")
                + mail, "mail_count": mail}

    @app.get("/me/notifications", response_class=HTMLResponse)
    def my_notifications(request: Request):
        account = signed_in(request)
        if account is None:
            return RedirectResponse("/join", status_code=303)
        from .. import notify
        events = _notif_events(account)
        chain = _account_chain()
        index = state.token_index(chain)
        names: dict[str, str] = {}
        for ev in events:
            if ev.actor and ev.actor not in names:
                try:
                    names[ev.actor] = index.tag_of(ev.actor) or ""
                except Exception:                         # noqa: BLE001
                    names[ev.actor] = ""
        # Looked at: everything on the page is read now, the Messages count
        # included -- written before the page is drawn, so its own tab shows no
        # count, while the rows still say which of them were new.
        seen = notify.seen_now(events, _notif_seen(account.pubkey))
        seen["message_tab"] = max(int(seen.get("message_tab", 0)), int(seen.get("message", 0)))
        state.set_setting(f"notif_seen:{account.pubkey}", seen)
        rows = notify.grouped(events)
        faces: dict[str, str] = {}
        for row in rows:
            for who in row.actors[:1]:
                if who and who not in faces:
                    try:
                        faces[who] = str(_profile_of(who).get("pfp") or "")
                    except Exception:                     # noqa: BLE001
                        faces[who] = ""
        # A colour per person for those without a picture: every address starts
        # with the same letter, so an initial alone made everybody the same "N".
        hues = {who: int(hashlib.sha256(who.encode()).hexdigest()[:4], 16) % 360
                for row in rows for who in row.actors[:1] if who}
        return render(request, "notifications.html", rows=rows, names=names,
                      faces=faces, hues=hues, ago=notify.ago, now=int(time.time()),
                      chain=chain)

    # --- seats, and signing in ------------------------------------------------
    #
    # The node is a builder, an index and a window -- never a custodian
    # (docs/multi-user.md). So there is no password here and no credential
    # database: an account is an Ed25519 public key made in somebody's
    # browser, and proving it is a signature over a nonce this node just
    # issued. The worst thing a copy of `accounts.sqlite` gives an attacker
    # is a list of public keys.
    #
    # None of this gates anything yet. The wallet's own pages are still the
    # wallet's own, exactly as they were; what exists now is the door, the
    # seat count and the register behind them. The page says so rather than
    # implying an account does more than it does.

    SESSION_COOKIE = SESSION

    def _origin(request: Request) -> str:
        """What the browser thinks it is talking to, which is what gets signed.

        `x-forwarded-proto` first: behind a proxy that terminates TLS,
        uvicorn sees http on a loopback socket while the browser sees https,
        and a signature over the wrong one of those would never verify.
        """
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme
        return f"{proto}://{request.headers.get('host', '')}"

    def _over_https(request: Request) -> bool:
        return (request.headers.get("x-forwarded-proto")
                or request.url.scheme) == "https"

    def signed_in(request: Request):
        """The account this request belongs to, or None. Touches the seat."""
        try:
            return state.accounts().session(
                request.cookies.get(SESSION_COOKIE, ""))
        except Exception:
            return None

    @app.get("/bip39-english.txt")
    def bip39_wordlist():
        """The official 2048 words, served from the node that vendors them.

        A wallet whose restore depends on somebody else's website is a wallet
        that stops restoring. Cached hard: it is the same file for ever --
        changing it would change every phrase ever made from it.
        """
        return Response(seedlib.WORDLIST_PATH.read_text(encoding="utf-8"),
                        media_type="text/plain; charset=utf-8",
                        headers={"Cache-Control": "public, max-age=604800"})

    @app.get("/vendor/{name:path}")
    def vendored(name: str):
        """The two libraries WebCrypto cannot replace.

        Served from here rather than from a CDN: a wallet whose
        cryptography arrives from somebody else's server at page load is
        one compromised mirror away from key theft. What is in the
        directory, where it came from and how it was checked is in
        `web/vendor/PROVENANCE.md`; `tests/test_vendor.py` pins the sums.
        """
        root = Path(__file__).parent / "vendor"
        if any(part in ("..", "") for part in Path(name).parts):
            raise HTTPException(404, "no such file")
        target = (root / name).resolve()
        if not str(target).startswith(str(root.resolve())) \
                or not target.is_file() or target.suffix != ".js":
            raise HTTPException(404, "no such file")
        return Response(target.read_text(), media_type="application/javascript",
                        # Immutable: the file is pinned by hash in a test,
                        # so a changed one is a changed application.
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})

    @app.get("/wallet.js")
    def wallet_js():
        """Signing up and signing in, as a person does it (templates/wallet_js.js)."""
        return Response((TEMPLATE_DIR / "wallet_js.js").read_text(),
                        media_type="application/javascript",
                        headers={"Cache-Control": "no-store"})

    @app.get("/messaging.js")
    def messaging_js():
        """Opening and sealing messages in the browser (templates/messaging_js.js)."""
        return Response((TEMPLATE_DIR / "messaging_js.js").read_text(),
                        media_type="application/javascript",
                        headers={"Cache-Control": "no-store"})

    @app.get("/coins.js")
    def coins_js():
        """The browser's coin half (templates/coins.js)."""
        return Response((TEMPLATE_DIR / "coins.js").read_text(),
                        media_type="application/javascript",
                        headers={"Cache-Control": "no-store"})

    @app.get("/signin.js")
    def signin_js():
        """The browser's half of the login (templates/signin.js).

        Not cached. It is small, and a stale copy of the code that derives
        somebody's keys is the one kind of staleness with no acceptable
        failure mode.
        """
        body = (TEMPLATE_DIR / "signin.js").read_text()
        return Response(body, media_type="application/javascript",
                        headers={"Cache-Control": "no-store"})

    @app.get("/join", response_class=HTMLResponse)
    def join_page(request: Request):
        """What a person meets: a name, a password, and a wallet.

        `/join/keys` is the same door for somebody who would rather hold
        their own words -- it is the older page and it is still the whole
        of what this one does underneath.
        """
        register = state.accounts()
        return render(request, "signup.html", chain=_account_chain(),
                      seats=register.seats, free=register.free(),
                      secure_enough=secure_context(request),
                      why_not=WHY_NOT_SECURE)

    @app.get("/join/keys", response_class=HTMLResponse)
    def join_with_keys(request: Request):
        register = state.accounts()
        return render(request, "join.html",
                      seats=register.seats,
                      free=register.free(),
                      idle_days=register.idle_days,
                      repo_url=updatelib.REPO_URL,
                      secure_enough=secure_context(request),
                      why_not=WHY_NOT_SECURE)

    @app.get("/auth/challenge")
    def auth_challenge(request: Request):
        """A nonce to sign, good for two minutes and for this node only."""
        return JSONResponse(state.accounts().challenge(_origin(request)))

    @app.post("/auth/login")
    def auth_login(request: Request, payload: Any = Body(None)):
        """Check a signed challenge, and set the session cookie.

        No CSRF token on this one, deliberately: the page that posts it is
        served to somebody who has not signed in, and handing a stranger the
        wallet's form token to get them through the door would be a worse
        trade than the one it protects against. What guards it instead is
        that it is JSON -- a cross-site form cannot send
        `application/json` without a preflight this server never answers --
        and that the body has to contain a signature over a nonce issued
        seconds earlier to this origin.
        """
        said = payload if isinstance(payload, dict) else {}
        register = state.accounts()
        try:
            token = register.login(
                str(said.get("pubkey", "")),
                str(said.get("nonce", "")),
                str(said.get("signature", "")),
                origin=_origin(request),
                ip=(request.client.host if request.client else ""),
                join=bool(said.get("join")))
        except accountslib.SeatsFull as full:
            return JSONResponse({"detail": str(full),
                                 "seats": register.seats, "free": 0},
                                status_code=409)
        except accountslib.AccountError as refused:
            return JSONResponse({"detail": str(refused)}, status_code=403)
        account = register.account(str(said.get("pubkey", "")))
        # No name comes back, because none is kept: a @tag is chain state
        # and a copy here would be a name that is wrong rather than one
        # that is missing (D-147). The page says "no name claimed yet"
        # until the claim exists, which is honest and is also true.
        answer = JSONResponse({"pubkey": account.pubkey, "tag": "",
                               "created": account.created,
                               "free": register.free()})
        answer.set_cookie(
            SESSION_COOKIE, token,
            max_age=accountslib.SESSION_DAYS * 86400,
            httponly=True, samesite="strict",
            # Secure only where the browser would keep it: marking a cookie
            # Secure on plain http means the browser drops it, and the
            # symptom is a login that appears to work and then does not.
            secure=_over_https(request))
        return answer

    @app.post("/auth/logout")
    def auth_logout(request: Request):
        state.accounts().logout(request.cookies.get(SESSION_COOKIE, ""))
        answer = JSONResponse({"pubkey": None})
        answer.delete_cookie(SESSION_COOKIE)
        return answer

    # --- what an account does on the chain ------------------------------------
    #
    # The node builds and explains; the browser shows, asks and signs; the
    # node checks what came back is what it offered and broadcasts. One
    # shape for everything an account ever does (docs/multi-user.md §5).

    _offers = accountlib.Offers()
    #: Collection runs an account owns. Its own book, not `collections.Jobs`,
    #: for the reason in `accountruns`: the operator's Runner resumes every row
    #: it finds with the node's own wallet as the signer, and an account's run
    #: must never be signed by this node at all.
    _runs = accountrunslib.Runs(state.home / "accountruns.sqlite")
    #: Inscriptions too big for one transaction, and which of their transactions
    #: are still owed. Its own book again, for the same reason as the runs: the
    #: node's Runner resumes what the node's own wallet should finish, and this
    #: node must never be the one that signs an account's piece.
    _parts = accountpartslib.Parts(state.home / "accountparts.sqlite")
    #: What an account has broadcast and the index has not read
    #: yet, so a second transaction does not pick the same coin.
    _pool_seen: dict[str, tuple] = {}          # txid -> the outpoints it spends
    _pool_cache: dict[str, tuple[float, frozenset]] = {}

    def _pool_spent(network: str) -> frozenset:
        """Every outpoint a transaction in that chain's mempool spends. Cached
        for a few seconds, and each transaction is read once: they do not change."""
        now = time.time()
        held = _pool_cache.get(network)
        if held and now - held[0] < 10:
            return held[1]
        ctx = next((c for c in (state.messaging, state.ledger)
                    if c.network == network), None)
        if ctx is None:
            return frozenset()
        out: set = set()
        with ctx.rpc() as rpc:
            pool = list(rpc.call("getrawmempool"))
            for txid in pool:
                if txid not in _pool_seen:
                    tx = rpc.call("getrawtransaction", txid, 1)
                    _pool_seen[txid] = tuple((v["txid"], v["vout"])
                                             for v in tx.get("vin", []) if "txid" in v)
                out.update(_pool_seen[txid])
        for gone in set(_pool_seen) - set(pool):
            _pool_seen.pop(gone, None)
        spent = frozenset(out)
        _pool_cache[network] = (now, spent)
        return spent

    _flights = accountlib.Flights(pool=_pool_spent)

    def _account_chain():
        """The chain a tag lives on. Testnet, as tags always have been."""
        return state.messaging

    def _account_chains() -> tuple:
        """Every chain an account can hold coins on, the tag chain first.

        One set of words, a key on each chain, and the node told which
        address belongs to which. On a node whose ledger IS the messaging
        chain there is only one -- a test node, usually -- and saying so
        once here stops every caller having to.
        """
        out = [_account_chain()]
        for chain in (state.ledger,):
            if chain is not None and chain.network != _account_chain().network:
                out.append(chain)
        return tuple(out)

    def _chain_asked(said: dict, field: str = "chain"):
        """Which chain a request is about. The tag chain unless it says.

        By NETWORK, not by a word like "mainnet": an account that names a
        chain this node does not run should be told so, rather than
        quietly served the other one -- which on these routes would mean
        building a payment on the wrong chain.
        """
        wanted = str((said or {}).get(field, "") or "").strip().lower()
        if not wanted:
            return _account_chain()
        for chain in _account_chains():
            if wanted in (chain.network, chain.label.lower(),
                          "mainnet" if chain.is_mainnet else "testnet"):
                return chain
        raise ValueError(f"this node does not run {wanted}")

    def _chain_on(network: str):
        """The chain a stored network name means, asked from the account side.

        `state.chain_named` answers from the ledger's end of the node and
        `_account_chains` from the account's. Where the two roles sit on two
        different chains the question has one answer either way. Where they sit
        on ONE chain -- any node that runs both roles on the same network --
        they are still two contexts with their own `params`, and the difference
        is not cosmetic: a Class B payload carries the marker address of the
        chain it was built for, and `tx.detect_class` calls a transaction a
        message only when that output is the marker the INDEX knows. Build an
        inscription on the wrong copy of a chain and it is paid for, goes into a
        block, is walked straight over, and is invisible everywhere without
        saying so once. The run book stores a network rather than a role, so the
        run routes ask this instead of `chain_named`.
        """
        for chain in _account_chains():
            if chain.network == network:
                return chain
        return state.chain_named(network)

    #: Where an account's coins live, by chain. The tag chain keeps the
    #: unqualified key it has always had, so an account made before there
    #: was a second chain is not asked to register its address again.
    def _address_key(pubkey: str, chain) -> str:
        if chain.network == _account_chain().network:
            return f"address:{pubkey}"
        return f"address:{chain.network}:{pubkey}"

    def _account_address(pubkey: str, chain) -> str:
        return str(state.setting(_address_key(pubkey, chain), "") or "")

    def _real_coins_open(account) -> bool:
        """Whether this account has asked to spend coins that are money.

        §1b's opt-in. It is a setting and not a check, and the reason is
        worth having in the file rather than in a comment somewhere else:
        the node CANNOT verify that somebody wrote their twelve words down
        without knowing the words, and a server that knows an account's
        words is the one thing every other decision here exists to make
        impossible. So the browser verifies them the only way that keeps
        that true -- by deriving the account's own mainnet key out of the
        words as typed and comparing it with the one already in use -- and
        sends only the answer.

        Which means this stops the person who never wrote the words down
        from putting real money somewhere nothing can bring it back from,
        and it stops nobody who is determined to be stopped. That is what
        it is for. The page says so rather than showing a padlock.
        """
        return str(state.setting(f"mainnet:{account.pubkey}", "") or "") == "yes"

    def _real_coins_gate(account, chain) -> None:
        """Refuse to spend real coins from an account that never opted in."""
        if chain.is_mainnet and not _real_coins_open(account):
            raise ValueError(
                "those are real coins, and this account has not asked for "
                "them. Type your twelve words back on the Backup page to "
                "switch them on -- what this account can spend is only yours "
                "for as long as you hold those words, and nothing here can "
                "bring them back.")

    def _coinkey_key(pubkey: str, chain) -> str:
        if chain.network == _account_chain().network:
            return f"coinkey:{pubkey}"
        return f"coinkey:{chain.network}:{pubkey}"

    def _quota(account, kind: str, nbytes: int = 0, count: bool = True) -> None:
        """What this node lets one account do, checked where the work is done.

        Two things, in the order they cost the machine something:

        * **The pile of unsigned offers.** Building a transaction is free to
          ask for and costs this node a read of the index and a thing held in
          memory naming specific coins, so an account cannot be allowed an
          unlimited number of asks outstanding. This is §6's "one active run
          at a time", in the only shape an account has today.
        * **The allowance for the action itself** (arcade/accounts.py), which
          is counted AFTER this node built the transaction and before it is
          offered. Before the build and the refusal would be about a thing
          that costs the account nothing to attempt; after the offer and a
          build that failed for an unrelated reason -- no coins, a name not
          claimed -- would have spent an allowance on something that never
          went near the chain.

        The numbers are the operator's to move and the page's to show; what
        is not movable is that there are numbers (§6: enforced and said, not
        enforced silently).

        `count=False` checks the pile and charges nothing. It exists for the
        pieces of one inscription, which are the tail of a gesture that was
        charged for when its split was offered: `PER_HOUR["inscribe"]` is a
        count of gestures, and counting the signatures would be counting the
        same photograph twenty-seven times. The pile is still checked, because
        twenty-seven outstanding offers is still twenty-seven transactions
        naming coins.

        The same word covers the echo of a gesture: a file this node has an
        unsigned offer out for, asked again because the confirmation was
        dismissed, was charged when that offer was built and is not a second
        gesture either. Gating it again would be worse than the thing it fixes
        -- it would strand an inscription whose allowance has already been
        spent -- and it buys no protection, because an offer only exists in the
        pile at all if some earlier ask paid for it.
        """
        if len(_offers.waiting(account.pubkey)) >= accountslib.OFFERS_WAITING:
            raise ValueError(
                "this node is still waiting to hear about the other offers it "
                "made you. Sign one or let a few minutes go by and ask again "
                "-- each one names specific coins, and two of them spending "
                "the same coin is a transaction the network refuses.")
        try:
            if count:
                state.accounts().charge(account.pubkey, kind, nbytes,
                                        caps=accountslib.limits(state.settings()))
        except accountslib.AccountError as exc:
            raise ValueError(str(exc))

    def _note_payment(txid: str, pubkey: str, address: str,
                      network: str = "") -> None:
        """Record an output this node just paid to an account.

        Read back from the node rather than assumed: `sendtoaddress`
        chooses its own output order, and guessing which one is the payment
        would offer a coin that is somebody's change.
        """
        try:
            with _account_chain().rpc() as rpc:
                decoded = rpc.call("decoderawtransaction",
                                   rpc.call("getrawtransaction", txid))
            for out in decoded.get("vout", []):
                where = (out.get("scriptPubKey") or {}).get("addresses") or []
                if address in where:
                    _flights.note_incoming(
                        pubkey, txid, int(out.get("n", 0)), address,
                        int(round(float(out.get("value", 0)) * 100_000_000)),
                        network=network or _account_chain().network)
                    return
        except Exception:
            pass                 # it will be spendable when its block lands

    def _on_its_way(pubkey: str, network: str, db, address: str) -> int:
        """Satoshis this node paid an account that no block has filed yet.

        The flight book remembers what came back from a broadcast so the coin
        can be spent before its block lands. Nothing retires an entry but its
        thirty-minute age -- `Flights.forget` has no caller -- so for that long
        a mined change output stands in both books: the row the scanner wrote,
        which is what `balance` adds up, and this node's note of it, which is
        what a page reads here. Printed under the balance as "on its way", one
        output looks like two wallets -- which is how a wallet of a hundred
        coins read as close to two hundred on 2026-09-25.

        `funding.choose` has the rule the other way round: an `extra` coin the
        index also lists is the same coin twice, and where the index agrees the
        coin exists, the index is what gets used. This is that rule on the way
        out to a page, where it is only a number. Nobody is refused anything;
        somebody is just shown what they hold once.
        """
        listed = {(coin["txid"], coin["vout"])
                  for coin in utxoslib.unspent(db, address)}
        return sum(coin["value"] for coin
                   in _flights.change_for(pubkey, network)
                   if (coin["txid"], coin["vout"]) not in listed)

    def _leaving(pubkey: str, network: str, db, address: str) -> int:
        """Satoshis the index still lists for this address that a transaction in
        the pool has already spent (this node's flights, and the node's own
        mempool -- `Flights(pool=...)`). Neither gone nor spendable: leaving."""
        spent = _flights.spent_by(pubkey, network)
        return sum(coin["value"] for coin in utxoslib.unspent(db, address)
                   if (coin["txid"], coin["vout"]) in spent)

    def _spendable(row: dict) -> None:
        """The big number (Order item 4, 2026-09-25): what can be spent now.

        `balance - leaving + incoming`: a coin the pool has taken is neither
        gone nor counted, a change output on its way is spendable before its
        block (this node offers it), and a coin a block has filed is counted
        once (D-175). The page prints this, and says what is arriving and
        what is leaving under it."""
        row["spendable"] = max(0, int(row.get("balance") or 0)
                               - int(row.get("leaving") or 0)
                               + int(row.get("incoming") or 0))

    _topoff_thread: dict[str, Any] = {}

    def _top_off_all(now: int | None = None) -> int:
        """One pass of the daily top-off (2026-09-25): every seated account
        brought back up to the faucet's gift on the tag chain, if it is short."""
        from .. import faucet as faucetmod
        chain = _account_chain()
        if chain.is_mainnet or not bool(state.setting("faucet_topoff", True)):
            return 0
        faucet = state.faucet()
        sent = 0
        index = state.token_index(chain)
        for account in state.accounts().seated():
            address = _account_address(account.pubkey, chain)
            if not address:
                continue
            row = {}
            with contextlib.closing(index.open()) as db:
                row["balance"] = utxoslib.balance(db, address)
                row["incoming"] = _on_its_way(account.pubkey, chain.network, db, address)
                row["leaving"] = _leaving(account.pubkey, chain.network, db, address)
            _spendable(row)
            try:
                sent += faucetmod.top_off(chain, faucet, account.pubkey, address,
                                          row["spendable"], now=now)
            except Exception as exc:                      # noqa: BLE001 -- next account
                log.info("top-off for %s: %s", account.pubkey[:12], exc)
        return sent

    def _auto_mine_pass() -> bool:
        """Mine a block to the node's own testnet wallet when the faucet runs low
        (2026-09-25). Spendable and still-maturing coins both count, so it
        stops asking for more once enough is on its way. One block per pass,
        never while a mining run is already going, testnet only (Miner refuses
        mainnet), and an operator can switch it off (`faucet_mine`)."""
        if not bool(state.setting("faucet_mine", True)) or state.mining is not None:
            return False
        chain = state.messaging
        if chain.is_mainnet:
            return False
        gift = int(state.setting("faucet", faucetlib.GIFT) or 0)
        low = int(state.setting("faucet_low", 20 * gift) or 0)
        if gift <= 0 or low <= 0:
            return False
        with chain.rpc() as rpc:
            held = walletlib.balance(rpc)
            have = int((held["spendable"] + held["immature"]) * 100_000_000)
            if have >= low:
                return False
            miner = Miner(rpc, chain.params)                   # refuses mainnet
            address = state.derived_address or rpc.call("getnewaddress")
            expected = miner.expected_hashes()
        log.info("faucet low (%s of %s): mining a block", have, low)
        state.mining = {"started": time.time(), "address": address, "tries": 0,
                        "rate": 0.0, "expected": expected, "stop": False, "auto": True}
        threading.Thread(target=_mine_one, args=(address,), name="arcade-mine",
                         daemon=True).start()
        return True

    def _start_top_offs() -> None:
        if _topoff_thread.get("t") is not None:
            return

        def loop():
            while not getattr(state, "shutting_down", False):
                try:
                    _top_off_all()
                except Exception as exc:                  # noqa: BLE001 -- keep going
                    log.info("top-off pass: %s", exc)
                try:
                    _auto_mine_pass()
                except Exception as exc:                  # noqa: BLE001 -- keep going
                    log.info("auto-mine pass: %s", exc)
                time.sleep(1800)

        _topoff_thread["t"] = threading.Thread(target=loop, name="arcade-topoff", daemon=True)
        _topoff_thread["t"].start()

    def _watch(address: str, why: str, chain=None) -> None:
        """Start following an address's coins, so it can be funded at all."""
        index = state.token_index(chain or _account_chain())
        with contextlib.closing(index.open()) as db:
            utxoslib.watch(db, address, index.indexed_height() or 0, why)

    def _signed_in_account(request: Request):
        account = signed_in(request)
        if account is None:
            raise HTTPException(403, "sign in first")
        return account

    # --- signing up, which is a name and a password ---------------------------
    #
    # The browser makes the words, derives the keys, encrypts the seed and
    # hands over a blob. The password never arrives here, so there is
    # nothing to check and nothing to steal: only somebody who can decrypt
    # the blob can produce the key that signs a challenge.

    @app.get("/signup/{tag}")
    def signup_free(tag: str):
        """Is this name free -- here, and as far as the chain has been read.

        Two answers rather than one, because they are different questions
        with different authorities. The chain decides, first claim wins,
        and neither answer is a promise about the next minute.
        """
        try:
            wanted = taglib.validate((tag or "").strip().lstrip("@"))
        except taglib.TagError as bad:
            return JSONResponse({"free": False, "detail": str(bad)})
        here = state.vault().taken(wanted)
        on_chain = ""
        try:
            on_chain = state.token_index(_account_chain()).address_of(wanted) or ""
        except Exception:
            pass                          # a node still catching up says nothing
        return JSONResponse({
            "tag": wanted, "free": not here and not on_chain,
            "here": here, "on_chain": bool(on_chain),
            "detail": (f"@{wanted} is already signed up on this node"
                       if here else
                       f"@{wanted} is claimed on the chain" if on_chain else ""),
        })

    @app.post("/signup")
    def signup(request: Request, payload: Any = Body(None)):
        """Make an account: a name, a public key, an address, and a blob.

        No password reaches this route, by design. What arrives is
        ciphertext the node cannot read and the parameters it was made
        with, so a stolen copy of this file is a pile of encrypted wallets
        and no way to open one.
        """
        said = payload if isinstance(payload, dict) else {}
        chain = _account_chain()
        try:
            wanted = taglib.validate(str(said.get("tag", "")).strip().lstrip("@"))
            pubkey = str(said.get("pubkey", "")).strip().lower()
            address = str(said.get("address", "")).strip()
            blob = said.get("blob")
            if not isinstance(blob, dict) or not blob.get("sealed"):
                raise ValueError("that is not an encrypted wallet")
            complaint = _check_address(address, mainnet=chain.is_mainnet)
            if complaint:
                raise ValueError(complaint)
            register = state.accounts()
            register.join(pubkey)         # a seat, and it may say there is none
            state.vault().put(wanted, pubkey, address, json.dumps(blob))
        except accountslib.SeatsFull as full:
            return JSONResponse({"detail": str(full)}, status_code=409)
        except (taglib.TagError, accountslib.AccountError, ValueError) as bad:
            return JSONResponse({"detail": str(bad)}, status_code=400)
        state.set_setting(f"address:{pubkey}", address)
        coin = str(said.get("coin_pubkey", "")).strip().lower()
        if coin:
            state.set_setting(f"coinkey:{pubkey}", coin)
        # The same words also hold coins on the other chain, and the
        # browser derived that address at the same moment. Registered here
        # rather than on a later visit, so an account is payable on both
        # from the minute it exists -- and refused rather than half-stored
        # if it does not check out, which would leave somebody with an
        # address on their screen that the node is not watching.
        for other in _account_chains()[1:]:
            elsewhere = str(said.get(f"address_{other.network}", "")).strip()
            if not elsewhere:
                continue
            if _check_address(elsewhere, mainnet=other.is_mainnet):
                continue
            state.set_setting(_address_key(pubkey, other), elsewhere)
            their_key = str(said.get(f"coin_pubkey_{other.network}",
                                     "")).strip().lower()
            if their_key:
                state.set_setting(_coinkey_key(pubkey, other), their_key)
            try:
                _watch(elsewhere, why=f"@{wanted}", chain=other)
            except Exception:
                pass                      # watched again when it is used
        # Where the chain was when this account came into existence.
        # Nobody could have written to a key that did not exist, so there
        # is nothing before this point to look at -- and saying so costs
        # no privacy, because the node knows when an account signed up
        # whatever it does with that fact (D-155).
        try:
            with state.store() as store:
                state.set_setting(f"mail_from:{pubkey}",
                                  store.newest_candidate())
        except Exception:
            pass
        try:
            _watch(address, why=f"@{wanted}")
        except Exception:
            pass                          # it will be watched at the claim

        # The coins, straight away. An account with none cannot claim its
        # own name, post, or inscribe anything -- every one of those is a
        # transaction and every transaction needs an input. A refusal here
        # is never fatal: the account exists either way and the page says
        # what happened (arcade/faucet.py).
        given, why_not = 0, ""
        try:
            gift = faucetlib.pour(
                _account_chain(), state.faucet(), pubkey, address,
                ip=(request.client.host if request.client else ""))
            given = gift.amount
            # Spendable now, not in ten minutes. The node broadcast this
            # itself, so it knows the output exists -- making somebody wait
            # a block to claim their own name, for a transaction this
            # machine is holding in its own pool, is a wait for nothing.
            _note_payment(gift.txid, pubkey, address,
                          network=_account_chain().network)
        except faucetlib.FaucetError as refused:
            why_not = str(refused)
        except Exception as exc:
            why_not = f"the faucet could not pay just now: {exc}"

        token = _open_session(pubkey)
        answer = JSONResponse({"tag": wanted, "pubkey": pubkey,
                               "address": address,
                               "given": given, "no_coins": why_not})
        _set_session(answer, token, request)
        return answer

    @app.get("/signin/{tag}")
    def signin_blob(request: Request, tag: str):
        """The encrypted wallet for a name, for the browser to open.

        Handed to whoever asks, which is the acknowledged cost of being
        able to sign in on a device that has never seen your words
        (docs/multi-user.md §2). Rate-limited, because a list of blobs is
        worth collecting even when each one is useless without a password.
        """
        register = state.accounts()
        who = (request.client.host if request.client else "")
        now = int(time.time())
        if register._rate_limited(f"vault:{who}", now):
            return JSONResponse(
                {"detail": "too many in a row. Wait a few minutes."},
                status_code=429)
        register._attempt(f"vault:{who}", now)
        found = state.vault().get(tag)
        if found is None:
            return JSONResponse({"detail": "no wallet here by that name"},
                                status_code=404)
        return JSONResponse({"tag": found["tag"], "pubkey": found["pubkey"],
                             "address": found["address"],
                             "blob": json.loads(found["blob"])})

    def _open_session(pubkey: str) -> str:
        """A session for an account that has just proved itself another way."""
        register = state.accounts()
        now = int(time.time())
        token = secrets.token_urlsafe(32)
        register.conn.execute(
            "INSERT INTO session (token_hash, pubkey, made, expires) "
            "VALUES (?,?,?,?)",
            (accountslib._hash(token), pubkey.lower(), now,
             now + accountslib.SESSION_DAYS * 86400))
        return token

    def _set_session(answer, token: str, request: Request) -> None:
        answer.set_cookie(
            SESSION_COOKIE, token,
            max_age=accountslib.SESSION_DAYS * 86400,
            httponly=True, samesite="strict", secure=_over_https(request))

    @app.get("/account")
    def account_state(request: Request):
        """Everything the page needs to draw itself, and nothing else.

        Only ever about the account making the request. An endpoint that
        can be asked about somebody else is an endpoint somebody will ask
        about somebody else.
        """
        account = signed_in(request)
        chains = [{"network": one.network, "label": one.label,
                   "version": one.params.pubkeyhash_version,
                   "mainnet": bool(one.is_mainnet),
                   "tags": one is _account_chain()}
                  for one in _account_chains()]
        if account is None:
            # Which chains this node runs is not a secret and is needed
            # BEFORE anybody signs up: the browser derives a key on each
            # from the same words and hands over both addresses at once.
            return JSONResponse({"pubkey": None, "chains": chains})
        chain = _account_chain()
        said: dict[str, Any] = {
            "pubkey": account.pubkey, "network": chain.network,
            "version": chain.params.pubkeyhash_version,
            "address": "", "balance": 0, "watching": None, "tag": "",
            "announced": False, "announcing": False,
            # What is already published, so the page can show a person their
            # own words rather than an empty box they have to retype. An
            # announcement replaces every field, so a form that starts blank
            # would take a bio down every time somebody changed a face.
            "profile": {"pfp": "", "bio": "", "url": ""},
        }
        mine = state.vault().by_pubkey(account.pubkey) or {}
        said["name"] = mine.get("tag", "")
        # The earliest candidate worth trying a key against. A browser with
        # no cursor of its own starts here rather than at the beginning of
        # the chain.
        said["mail_from"] = int(
            state.setting(f"mail_from:{account.pubkey}", 0) or 0)
        said["claiming"] = mine.get("claimed", "")
        address = _account_address(account.pubkey, chain)
        said["address"] = address
        if address:
            try:
                index = state.token_index(chain)
                with contextlib.closing(index.open()) as db:
                    said["balance"] = utxoslib.balance(db, address)
                    said["watching"] = utxoslib.since(db, address)
                    # What this node has broadcast to them and no block has
                    # filed yet. Spendable, and shown separately so the page
                    # can say "on its way" rather than "nothing" -- and read
                    # from the same open index, so what it has already written
                    # down is not also on its way.
                    said["incoming"] = _on_its_way(account.pubkey,
                                                   chain.network, db, address)
                    said["leaving"] = _leaving(account.pubkey, chain.network,
                                               db, address)
                    _spendable(said)
                said["tag"] = index.tag_of(address) or ""
                with state.store() as store:
                    # Two questions, and the page has to be able to answer them
                    # differently. A key that is out in the pool is not a key a
                    # stranger's node can read yet, and a signup page that says
                    # "confirmed" a minute early has taught a person that this
                    # site's word about the chain is worth nothing -- which is
                    # exactly what the row for a new account did on 2026-09-25,
                    # at height 0.
                    said["announced"] = (
                        store.confirmed_key_for(address) is not None)
                    # Not the same question and not the same answer: the key is
                    # out, the block has not come. Nothing spends on it and no
                    # page calls it published; it is here so a page that wants
                    # to say "on its way" can say that instead of "not yet".
                    said["announcing"] = store.key_for(address) is not None
                profile = _profile_of(address)
                said["profile"] = {field: profile[field]
                                   for field in ("pfp", "bio", "url")}
            except Exception:
                pass                       # a node still catching up says 0

        # Every chain this account can hold coins on, the tag chain first
        # and named the same way whichever it is. One set of words, a key
        # on each: what differs is the version byte, the index that is
        # read, and whether the coins are worth anything.
        said["chains"] = []
        for one in _account_chains():
            here = _account_address(account.pubkey, one)
            row = {"network": one.network, "label": one.label,
                   "version": one.params.pubkeyhash_version,
                   "mainnet": bool(one.is_mainnet),
                   "address": here, "balance": 0, "incoming": 0,
                   "watching": None, "tags": one.network == chain.network,
                   # Not a secret and not a judgement: the page needs it to
                   # put the twelve-words box in front of a send rather than
                   # after it, where it would arrive too late to be read.
                   "locked": bool(one.is_mainnet)
                             and not _real_coins_open(account)}
            if here:
                try:
                    index = state.token_index(one)
                    with contextlib.closing(index.open()) as db:
                        row["balance"] = utxoslib.balance(db, here)
                        row["watching"] = utxoslib.since(db, here)
                        row["incoming"] = _on_its_way(account.pubkey,
                                                      one.network, db, here)
                        row["leaving"] = _leaving(account.pubkey, one.network,
                                                  db, here)
                except Exception:
                    pass                   # a node still catching up says 0
            _spendable(row)
            said["chains"].append(row)

        # What this node lets this account do, and how much of it is left.
        # The other half of §6: a number that only appears when somebody runs
        # into it is a number enforced silently, and the page they were
        # reading looks broken rather than limited.
        said["quota"] = state.accounts().room(
            account.pubkey, caps=accountslib.limits(state.settings()))
        return JSONResponse(said)

    @app.get("/me", response_class=HTMLResponse)
    def my_arcade(request: Request):
        """An account's own Overview: its name, its address, its chains.

        The operator's Overview is a control panel for the NODE -- update
        settings, auto-sell, a scan button -- because the node is a single
        machine with one operator. An account has none of that to control:
        it holds no automation running on its behalf (accepting an offer
        automatically would mean a key on this server that could spend
        without asking, which is exactly what an account does not have),
        so this page is the identity half of Overview and nothing else --
        who you are, how to be reached, and where the rest of the
        application lives. Coins are the Wallet page's; messages are the
        Messages page's.
        """
        account = signed_in(request)
        if account is None:
            return RedirectResponse("/join", status_code=303)
        chain = _account_chain()
        register = state.accounts()
        return render(request, "me.html", chain=chain,
                      node=chain.status(), when=_when,
                      messaging=messaging_status(), ledger=ledger_status(),
                      seats_free=register.free(), seats_total=register.seats)

    @app.get("/me/messages", response_class=HTMLResponse)
    def my_messages(request: Request):
        """An account's own messages, opened in its own browser."""
        if signed_in(request) is None:
            return RedirectResponse("/join", status_code=303)
        chain = _account_chain()
        page = render(request, "my_messages.html", chain=chain, when=_when)
        try:                     # opened: the red count on the Messages tab is read
            account = signed_in(request)
            push = state.push() if hasattr(state, "push") else None
            if account is not None and push is not None:
                latest = push.arrivals(account.pubkey, limit=1)
                if latest:
                    seen = _notif_seen(account.pubkey)
                    seen["message_tab"] = max(int(seen.get("message_tab", 0)),
                                              int(latest[0]["rowid"]))
                    state.set_setting(f"notif_seen:{account.pubkey}", seen)
        except Exception as exc:                          # noqa: BLE001
            log.info("messages seen: %s", exc)
        return page

    @app.get("/me/contacts", response_class=HTMLResponse)
    def my_contacts(request: Request):
        """An account's own address book, kept in its own browser."""
        if signed_in(request) is None:
            return RedirectResponse("/join", status_code=303)
        return render(request, "my_contacts.html", chain=_account_chain())

    @app.get("/account/find")
    def account_find(request: Request, q: str = ""):
        """Names claimed on this chain that look like what was typed.

        The same question the wallet's own address book asks, answered the
        same way: from the index, so a name in somebody's book is a name
        that exists on the chain rather than one they typed.
        """
        _signed_in_account(request)
        wanted = (q or "").strip().lstrip("@").lower()
        if not wanted:
            return JSONResponse({"matches": []})
        chain = _account_chain()
        out = []
        try:
            index = state.token_index(chain)
            for row in index.search_tags(wanted, limit=20):
                out.append({"tag": row["tag"], "address": row["address"]})
        except Exception:
            pass
        return JSONResponse({"matches": out})

    @app.get("/account/nfts")
    def account_nfts(request: Request):
        """What this account holds, on every chain it has an address on.

        The same index the wallet's own NFT tab reads, asked about a
        different address. Ownership is the chain's answer and not this
        node's opinion of it: an inscription moves when its transfer is in
        a block, and until then it is still where it was.
        """
        account = _signed_in_account(request)
        out: list[dict[str, Any]] = []
        for chain in _account_chains():
            address = _account_address(account.pubkey, chain)
            if not address:
                continue
            try:
                index = state.token_index(chain)
                held = index.inscriptions(owner=address, limit=200)
                listed = _nft_listings(index, chain)
            except Exception:
                continue           # a chain this node has no index for
            out.append({
                "network": chain.network, "label": chain.label,
                "mainnet": bool(chain.is_mainnet), "address": address,
                "pieces": [{
                    "txid": row["txid"], "number": row["number"],
                    "content_type": row["content_type"],
                    "content_len": row["content_len"],
                    "block_height": row["block_height"],
                    "collection": row.get("collection") or "",
                    "edition": row.get("edition"),
                    "creator": row["creator"],
                    "sale": listed.get(row["txid"]),
                } for row in held],
            })
        return JSONResponse({"chains": out})

    @app.post("/account/inscribe")
    def account_inscribe(request: Request, payload: Any = Body(None)):
        """Offer the next transaction of an inscription, as this account.

        The same inscription the node's own wallet writes at
        `/inscriptions/create`, built the way an account has to have it
        built: as an unsigned offer naming this account's own coins, which
        its browser signs and this node has no way of signing itself. What
        comes back is checked against what was offered, and what is
        broadcast is the offer, not whatever arrives.

        Two shapes, because one inscription can be several transactions.

        * `{content: ...}` asks for the **start**. A file that fits in one
          transaction is answered exactly as it always was -- one OP_RETURN
          where the content is short, Class B packet outputs when it is not.
          A bigger one starts with a SPLIT: one output per piece, paid out of
          this account's own coins, and then one transaction per piece, each
          spending its own output. That costs exactly ONE block -- the
          split's -- and then every piece goes at once, which is what the
          node's own wallet has always done: `sender.ensure_outputs` splits,
          waits one confirmation, and "then all the chunks go at once. Six
          transactions measured at about four minutes chained, against one
          block plus two seconds this way." The account cannot make that
          wallet call, so its tab makes the requests one signature at a time
          and `accountparts` keeps the score. It is the pieces that must not
          be broadcast while the split is still only in the mempool: the
          chain counts the whole package sitting under an unconfirmed parent
          and refuses at about a hundred kilobytes, which is two of these
          transactions (`_inscribe_piece`). One wait for the file, never one
          wait between pieces.
        * `{part, chunk, content}` asks for **piece n**, funded by the split
          output that was made for it. Its bytes are checked against the
          digest written down at the start, because an inscription cannot be
          corrected and pieces of two different files would assemble into a
          third file that nobody chose and everybody paid for.

        Asking again with the whole file continues the inscription that is
        waiting for it rather than starting a second copy of it
        (`accountparts.find`), and the allowance is charged ONCE, at the
        split. The pieces are one gesture with a great many signatures, not a
        great many gestures: `PER_HOUR["inscribe"]` counting them would turn a
        photograph into a four-hour job, which is the stall this exists to
        remove. What bounds the size is the day's bytes (§6), which is the
        operator's number and is shown before anything is spent.
        """
        account = _signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = _chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = _account_address(account.pubkey, chain)
        if not address:
            return JSONResponse(
                {"detail": f"this account has no {chain.label.lower()} "
                           f"address yet"}, status_code=400)
        try:
            try:
                content = base64.b64decode(str(said.get("content", "")),
                                           validate=True)
            except ValueError:
                raise ValueError("the content has to arrive encoded in "
                                 "base64, the way a file read in the browser "
                                 "does") from None
            if not content:
                raise ValueError("there is nothing to inscribe")
            if str(said.get("part") or ""):
                return _inscribe_piece(account, chain, address, said, content)
            kind = str(said.get("content_type") or "application/octet-stream")
            return _inscribe_start(account, chain, address, said, content, kind)
        except (fundinglib.FundingError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)

    def _spend_now(account, chain, address: str, outputs: list,
                   what: str) -> Any:
        """A transaction out of the coins this account holds right now.

        The two lines of bookkeeping are the whole reason this is a function
        rather than a call to `fundinglib.build`: between a broadcast and its
        block the index still shows the coin that was just spent and does not
        show the change that came back, and an inscription that needs several
        transactions in a row is exactly where that bites.
        """
        index = state.token_index(chain)
        with contextlib.closing(index.open()) as db:
            return fundinglib.build(
                db, chain.params, address, outputs,
                rate=fees.MIN_FEE_PER_KB, what=what,
                exclude=_flights.spent_by(account.pubkey, chain.network),
                extra=_flights.change_for(account.pubkey, chain.network))

    def _how_it_is_split(plan) -> tuple:
        """(the manifest's bytes, its length, the chunk's length, the content of
        every chunk).

        Read back out of the payloads rather than recomputed, because the
        framing belongs to `inscriptions.py` and a second arithmetic of it here
        is how the two drift apart. Chunk 0's body is the manifest and then
        content; every other body is content alone, and every body is the same
        length but the last -- that is what "split evenly" means, and it is what
        lets a browser slice a file from two numbers.
        """
        bodies = [p[inscriptionlib.CHUNK_HEADER_LEN:] for p in plan.payloads]
        _manifest, after = inscriptionlib.Manifest.decode(bodies[0])
        at = len(bodies[0]) - len(after)
        return (bodies[0][:at], at, len(bodies[0]),
                [b[at:] if n == 0 else b for n, b in enumerate(bodies)])

    def _inscribe_start(account, chain, address: str, said: dict,
                        content: bytes, kind: str):
        """The first transaction of an inscription: the piece, or the split.
        """
        label = (str(said.get("name") or "").strip()[:60]
                 or kind.split(";")[0])
        digest = hashlib.sha256(content).hexdigest()
        waiting = _parts.find(account.pubkey, chain.network, digest)
        if waiting is not None:
            return _inscribe_again(account, chain, address, waiting)

        tag = secrets.token_bytes(8)
        plan = inscribelib.plan(content, kind, str(said.get("json", "")),
                                inscription_id=tag)
        if plan.chunks == 1:
            unsigned = _spend_now(
                account, chain, address,
                _class_c_or_b(chain, address, plan.payloads[0],
                              _coin_pubkey(account.pubkey, chain)),
                f"inscribe {label}")
            # A file this node already has an unsigned offer for is one
            # gesture asked twice -- the confirmation dismissed, the tab
            # reloaded -- and it would cost an allowance the chain never heard
            # about. `_inscribe_again` has said so of a split since splits were
            # built; this is that rule where there is no job book to remember
            # it in, so the offer pile remembers it.
            again = any(o.digest == digest
                        for o in _offers.waiting(account.pubkey)
                        if o.network == chain.network)
            _quota(account, "inscribe", len(content), count=not again)
            offer = _offers.add(account.pubkey, chain.network, unsigned,
                                unsigned.what, digest=digest)
            return JSONResponse({"offer": offer.id, "bytes": plan.content_len,
                                 "chunks": 1, "chain": chain.network,
                                 **unsigned.as_json()})

        manifest, at, chunk_len, contents = _how_it_is_split(plan)
        piece = inscribelib.piece_size(plan)
        unsigned = _spend_now(
            account, chain, address,
            [(piece, txbuild.p2pkh_script(address))] * plan.chunks,
            f"split into {plan.chunks:,} outputs, to inscribe {label} "
            f"in {plan.chunks:,} transactions")
        _quota(account, "inscribe", len(content))
        job = _parts.create(
            account=account.pubkey, address=address, network=chain.network,
            name=label, content_type=plan.content_type, json_text=plan.json,
            size=len(content), sha256=digest, inscription_id=tag.hex(),
            contents=contents, piece=piece, chunk_len=chunk_len,
            manifest=manifest, fee=plan.estimate.fee,
            dust=plan.estimate.dust, floor=chain.params.activation_height)
        offer = _offers.add(account.pubkey, chain.network, unsigned,
                            unsigned.what,
                            done=lambda txid, job=job: _parts.note_split(job,
                                                                         txid))
        return JSONResponse({"offer": offer.id, "part": job,
                             "split": "offered", "bytes": plan.content_len,
                             "chunks": plan.chunks,
                             "chunk_len": chunk_len,
                             "manifest_len": at, "sent": 0, "next": 0,
                             "chain": chain.network, **unsigned.as_json()})

    def _inscribe_again(account, chain, address: str, row: dict):
        """The same file, asked again while its inscription is unfinished.

        Nothing is charged and nothing is inscribed twice. If the split never
        went out it is rebuilt -- same outputs, from whatever coins are here
        now -- because an offer is not a promise the chain has heard. If it did
        go out there is nothing at all to offer from this shape of the request:
        the answer is where it stopped, and the tab goes back to asking for
        pieces. The framing comes from the row, never from this request, so a
        resume cannot quietly re-describe a file that is already half on the
        chain.
        """
        said = {"part": row["id"], "chunks": int(row["chunks"]),
                "chunk_len": int(row["chunk_len"]),
                "manifest_len": len(bytes.fromhex(row["manifest"])),
                "bytes": int(row["size"]), "name": row["name"],
                "sent": row["sent"], "next": row["next"],
                "chain": row["network"], "resumed": True}
        if row["split_txid"]:
            return JSONResponse(said)
        unsigned = _spend_now(
            account, chain, address,
            [(int(row["piece"]), txbuild.p2pkh_script(address))]
            * int(row["chunks"]),
            f"split into {int(row['chunks']):,} outputs, to inscribe "
            f"{row['name']} in {int(row['chunks']):,} transactions")
        offer = _offers.add(
            account.pubkey, chain.network, unsigned, unsigned.what,
            done=lambda txid, job=row["id"]: _parts.note_split(job, txid))
        return JSONResponse({**said, "offer": offer.id, "split": "offered",
                             **unsigned.as_json()})

    def _inscribe_piece(account, chain, address: str, said: dict,
                        content: bytes):
        """One piece of an inscription already started, funded by its own
        output of the split.

        Not built by searching for coins: `fundinglib.build_one` is told which
        outpoint to spend, which is the only way a piece is still buildable a
        week from now, and the only way the output the split made for it is the
        thing that pays for it.
        """
        job = str(said.get("part"))
        row = _parts.get(job)
        if (not row or row["account"] != account.pubkey
                or row["network"] != chain.network):
            raise ValueError("that inscription is not one this node is "
                             "finishing for this account. Start it again from "
                             "the file.")
        if row["status"] in ("done", "stopped"):
            raise ValueError(f"that inscription is {row['status']}, so there "
                             "is no piece of it left to offer.")
        try:
            n = int(said.get("chunk"))
        except (TypeError, ValueError):
            raise ValueError("which piece was not said") from None
        if not 0 <= n < int(row["chunks"]):
            raise ValueError(f"that inscription has {int(row['chunks']):,} "
                             f"pieces, numbered 0 to "
                             f"{int(row['chunks']) - 1}.")
        mine = [c for c in _parts.chunks(job) if int(c["n"]) == n]
        if not mine:
            raise ValueError("that piece was never written down")
        chunk = mine[0]
        if chunk["status"] == "sent":
            raise ValueError(f"piece {n + 1} is already on the chain "
                             f"({chunk['txid']}). Nothing has been paid for "
                             "twice.")
        if hashlib.sha256(content).digest() != bytes(chunk["digest"]):
            raise ValueError(
                f"those are not the bytes piece {n + 1} of that inscription "
                f"was promised to. A piece is permanent and paid for, and "
                f"pieces of two different files make a third file nobody "
                f"chose -- so the file has to be the one that started this. "
                f"Nothing has been paid for.")
        if not row["split_txid"]:
            raise ValueError("the split has not gone out yet, so there is "
                             "nothing here to pay for this piece. Ask for the "
                             "file again and sign the split first.")
        # The piece spends an output of the split, and the chain will not take
        # a third generation: what it counts is the whole package sitting under
        # an unconfirmed parent, and that ceiling is about a hundred kilobytes.
        # Two of these transactions are already 90 KB of it -- measured, the
        # third came back `too-long-mempool-chain`. So this waits for the
        # split's BLOCK and then they all go, which is what the node's own
        # wallet has always done (`sender.ensure_outputs` splits, waits one
        # confirmation, sends every chunk at once: "one block plus two seconds"
        # against four minutes chained). One wait for the whole file, never one
        # wait between pieces.
        how = _tx_status(chain, row["split_txid"])
        if how is None:
            raise ValueError("this node cannot tell whether that split is in a "
                             "block, so it will not offer a piece that spends "
                             "an output it cannot see. Look at the Overview -- "
                             "a node without txindex cannot carry a split.")
        if how["conflicted"]:
            _parts.set_status(job, "stopped",
                              note="the split was replaced by another "
                                   "transaction")
            raise ValueError("the transaction that was to fund this "
                             "inscription is not coming -- something spending "
                             "the same coins reached a block first. Nothing "
                             "further has been paid for. Ask again with the "
                             "file and it starts again.")
        if not how["confirmed"]:
            now = _parts.get(job) or {}
            return JSONResponse({"part": job, "chunk": n,
                                 "chunks": int(row["chunks"]),
                                 "sent": now.get("sent", 0),
                                 "next": now.get("next"),
                                 "chunk_len": int(row["chunk_len"]),
                                 "manifest_len": len(bytes.fromhex(
                                     row["manifest"])),
                                 "waiting": "the split is in the mempool, and "
                                            "these pieces spend its outputs, "
                                            "so they go out together once it "
                                            "is in a block -- about a minute, "
                                            "and no minute in between them."})
        body = (bytes.fromhex(row["manifest"]) + content if n == 0
                else content)
        payload = inscriptionlib.Chunk(
            inscription_id=bytes.fromhex(row["inscription_id"]),
            countdown=int(row["chunks"]) - 1 - n, body=body).encode()
        unsigned = fundinglib.build_one(
            chain.params, address,
            {"txid": row["split_txid"], "vout": n,
             "value": int(row["piece"]), "address": address},
            _class_c_or_b(chain, address, payload,
                          _coin_pubkey(account.pubkey, chain)),
            rate=fees.MIN_FEE_PER_KB,
            what=f"piece {n + 1:,} of {int(row['chunks']):,} of "
                 f"{row['name']}, on the chain forever")
        # The pile is checked, the allowance is not: this is the same gesture
        # that was charged for when the split was offered.
        _quota(account, "inscribe", count=False)
        _parts.offer_chunk(job, n)
        offer = _offers.add(
            account.pubkey, chain.network, unsigned, unsigned.what,
            done=lambda txid, job=job, n=n: _parts.record_chunk(job, n, txid))
        now = _parts.get(job) or {}
        return JSONResponse({"offer": offer.id, "part": job, "chunk": n,
                             "sent": now.get("sent", 0),
                             "next": now.get("next"),
                             "chunks": int(row["chunks"]),
                             "chain": chain.network, **unsigned.as_json()})

    def _what_the_account_has(account, chain, address: str, build,
                              label: str = "") -> dict[str, Any]:
        """What of this build this account has already put on the chain.

        `_what_is_already_there` asked the way an account has to be asked:
        about an address rather than about a wallet this node holds, and about
        `accountruns` rather than the job book the node's Runner resumes. The
        answers are the same three because the money at stake is the same --
        an inscription pays once, cannot be recalled, and a second copy of an
        edition joins nothing (D-120).

        The reading of the run book is narrower than the operator's, and
        deliberately so. `running` means something different here: for the
        node it is a thread going now, and for an account it is a browser that
        stopped asking, so a run of this name that is `running` says nothing
        about whether its pieces reached the chain -- `collection_editions`
        below is what says that. What the book does know on its own is a run
        that finished at this floor, whose pieces are broadcast and not yet in
        a block. That is the minutes between the last piece and its block, the
        index is honestly empty, and a second run would pay for the set twice.
        """
        out: dict[str, Any] = {"blocked": "", "note": "", "skip": set()}
        collection = build.collection
        if not address or not collection:
            return out
        floor = chain.params.activation_height
        named = {collection, label.strip()[:60]} - {""}
        mine = [run for run in _runs.list(account=account.pubkey,
                                          network=chain.network)
                if run["name"] in named]
        try:
            done = state.token_index(chain).collection_editions(
                address, collection)
        except Exception:
            # Same reading as the operator's: an index that will not answer
            # makes this say less, not nothing, and the finished-at-this-floor
            # scan below is what speaks on the empty answer.
            done = set()
        out["skip"] = {item.edition for item in build.items if item.edition in done}
        if out["skip"] and len(out["skip"]) == len(build.items):
            out["blocked"] = (
                f"{collection} is already on this chain from this address, "
                f"all {len(out['skip']):,} pieces of it. Inscribing it again "
                "would pay for a second copy of every item, and no node would "
                "file the copies into the set. Nothing has been paid for.")
            return out

        if not out["skip"]:
            finished = [run for run in mine
                        if run.get("floor") == floor and run["status"] == "done"]
            if finished:
                run = finished[-1]
                when = dt.datetime.fromtimestamp(
                    float(run.get("created") or 0)).strftime("%d %b %H:%M")
                out["blocked"] = (
                    f"this account already inscribed {collection} on this "
                    f"chain -- run {run['id']}, started {when}, "
                    f"{run['items']:,} pieces. None of it is indexed yet, "
                    "which is the minutes between the last piece being sent "
                    "and its block. Wait for it rather than pay for the set "
                    "twice. Nothing has been paid for.")
                return out
            stale = [run for run in mine if run.get("floor") != floor]
            if stale:
                out["note"] = (
                    f"a run of {collection} from before this chain started at "
                    f"{floor:,} is being ignored: the pieces it sent are below "
                    "the floor, so no node reads them.")
            return out

        left = len(build.items) - len(out["skip"])
        out["note"] = (
            f"{len(out['skip']):,} of these are already on this chain from "
            f"this address, so this run is written down as the other "
            f"{left:,}. A second copy joins nothing and costs again.")
        return out

    @app.post("/account/run/start")
    def account_run_start(request: Request, files: list[UploadFile] = File([]),
                          name: str = Form(""), run_chain: str = Form("")):
        """Write a collection down as this account's run. Nothing is inscribed.

        One upload, one run, and no transaction in it. The pieces are asked
        for one at a time afterwards, which is the only shape an account can
        use: the node cannot sign for it, and a browser that closes its tab
        cannot finish anything. The run outlives both, which is the entire
        point of writing it here rather than doing it inside the request.

        What is already on the chain is asked here, before the run is written,
        which is where the operator's wizard asks it. It used to not be asked
        at all, and the hole was the expensive kind: an account that uploaded
        the same folder twice -- because a run is not obviously finished, and
        a finished run leaves no mark the upload form shows -- was handed a
        second run and paid a second time for pieces no node files into the
        set. The wizard's review step is not the point of the operator's
        version; the refusal is, and the refusal costs an index read.

        The uploaded build stays on this node, and nothing ages it out or
        deletes it yet -- stated rather than left implicit, because it means
        an account's pictures sit on somebody else's disk indefinitely, and an
        operator should know that before offering the page.
        """
        account = _signed_in_account(request)
        try:
            chain = _chain_asked({"chain": run_chain})
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = _account_address(account.pubkey, chain)
        if not address:
            return JSONResponse(
                {"detail": f"this account has no {chain.label.lower()} "
                           f"address yet"}, status_code=400)
        # One run going at a time, which is a rule rather than a courtesy. Two
        # runs from one address would be offered interleaved, and the account
        # signing piece nine could not know which collection it was paying for.
        # Checked before the upload is read: this is the one question that can
        # be answered without touching the disk.
        ahead = _runs.due(account.pubkey, chain.network)
        if ahead is not None:
            return JSONResponse(
                {"detail": f"{ahead['name']} is still going, at {ahead['sent']} "
                           f"of {ahead['items']} pieces. Finish it or stop it "
                           "before starting another. Nothing has been paid "
                           "for."}, status_code=400)
        try:
            build = collectionlib.read_build(
                collectionlib.find_build(_save_upload(files)))
        except (collectionlib.CollectionError, ValueError, OSError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        # Asked between the two writes and not inside either: the refusal is
        # about the chain and the run book, and `create` is about the build.
        already = _what_the_account_has(account, chain, address, build, name)
        if already["blocked"]:
            return JSONResponse({"detail": already["blocked"]}, status_code=400)
        if already["skip"]:
            # Only the pieces still owed are written down, so the fee on the
            # page is the fee the run will charge (D-120) and the pieces that
            # are up are not in the table to be re-offered.
            build = _without(build, already["skip"])
        try:
            run_id = _runs.create(account.pubkey, address, build,
                                  chain.network,
                                  name=name.strip()[:60] or build.collection,
                                  floor=chain.params.activation_height)
        except (collectionlib.CollectionError, accountrunslib.TooBig,
                ValueError, OSError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        run = _runs.get(run_id)
        if already["note"]:
            # On the run, not just in this answer: the sentence outlives the
            # tab that heard it, and the page that follows the run is the one
            # that has to still be able to say why it is short of the folder.
            _runs.note(run_id, already["note"])
            run = _runs.get(run_id)
        return JSONResponse({"run": run_id, "name": run["name"],
                             "items": run["items"], "fee": run["fee"],
                             "dust": run["dust"], "chain": chain.network,
                             "note": run["note"], "next": run["next"]})

    @app.post("/account/run")
    def account_runs(request: Request):
        """This account's runs, unfinished first.

        A read, and its own route rather than a side effect of asking for a
        piece, because asking for a piece BUILDS an offer: a page that checked
        whether a run was finished by asking for its next piece would reserve
        coins against an offer nobody meant to sign. This is what lets the
        page's promise -- that a run is still there when the tab comes back --
        be true rather than aspirational.
        """
        account = _signed_in_account(request)
        mine = [run for run in _runs.list(account=account.pubkey)]
        mine.sort(key=lambda run: (run["status"] == "done", -run["created"]))
        return JSONResponse({"runs": [{"run": run["id"], "name": run["name"],
                                       "items": run["items"],
                                       "sent": run["sent"],
                                       "status": run["status"],
                                       "sending": run["sending"],
                                       "refused": run["failed_pieces"],
                                       "note": run["note"],
                                       "next": run["next"],
                                       "chain": run["network"]}
                                      for run in mine]})

    @app.post("/account/run/stop")
    def account_run_stop(request: Request, payload: Any = Body(None)):
        """File a run as stopped, so it is no longer the run this account is on.

        Nothing is cancelled by it. There is no thread to stop -- the pieces
        that are already on the chain stay there whatever this row says, and
        the ones still owed were never going out by themselves. What it changes
        is the answer to "which run is this account working on", which would
        otherwise be answered by a run its owner walked away from, for as long
        as this node exists: an account is only allowed one run going at a time,
        and a run that was abandoned rather than finished would hold that place
        forever.
        """
        account = _signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        run = _runs.get(str(said.get("run", "")))
        if run is None:
            return JSONResponse({"detail": "there is no run of that id"},
                                status_code=404)
        if run["account"] != account.pubkey.lower():
            return JSONResponse({"detail": "that run belongs to somebody else"},
                                status_code=403)
        # `only_from` because a finished run has nothing to stop, and saying
        # `stopped` over `done` would make a collection that is entirely on the
        # chain read as one that was abandoned. `failed` is in the list because
        # it holds this account's one place in the same way `running` does, and
        # a run nobody can finish and nobody told how to stop would block every
        # collection after it.
        _runs.set_status(run["id"], "stopped",
                         only_from=("open", "running", "failed"))
        run = _runs.get(run["id"])
        return JSONResponse({"run": run["id"], "name": run["name"],
                             "sent": run["sent"], "items": run["items"],
                             "status": run["status"]})

    @app.post("/account/run/retry")
    def account_run_retry(request: Request, payload: Any = Body(None)):
        """Put the refused pieces of a run back in line.

        The operator's page calls this "Retry the rest", and the account's
        version means the same thing with one difference that has to be said
        out loud: the pieces keep the inscription ids written down when the run
        was, so a retry puts the same piece up rather than a second copy beside
        it -- which is the only reason pressing it is safe at all.

        Whether it can do any good depends on the reason on the piece, which is
        why the reasons come back with the count. A piece refused for something
        true of this node at this minute is worth pressing; one refused because
        its picture is not in the folder this node kept is not, and the honest
        move there is a new upload, which now costs only the pieces that are
        still missing.
        """
        account = _signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        run = _runs.get(str(said.get("run", "")))
        if run is None:
            return JSONResponse({"detail": "there is no run of that id"},
                                status_code=404)
        if run["account"] != account.pubkey.lower():
            return JSONResponse({"detail": "that run belongs to somebody else"},
                                status_code=403)
        if not run["failed_pieces"]:
            return JSONResponse({"run": run["id"], "retried": 0,
                                 "status": run["status"], "refused": []})
        reasons = [f"{piece['name']}: {piece['error']}"
                   for piece in _runs.pieces(run["id"], status="failed")]
        left = _runs.retry_failed(run["id"])
        run = _runs.get(run["id"])
        return JSONResponse({"run": run["id"], "retried": left,
                             "sent": run["sent"], "items": run["items"],
                             "status": run["status"], "refused": reasons})

    @app.post("/account/run/delete")
    def account_run_delete(request: Request, payload: Any = Body(None)):
        """Forget a run, and take its pictures off this node with it.

        What is on the chain stays there whatever this says -- there is no
        un-inscribing -- so what is being deleted is the node's memory of a set
        and the folder it kept to build the rest of it from.

        Refused while a piece is out for signature, which is the account's
        reading of the operator's "pause the run before removing it". There is
        no thread to stop here; what there is is a signature that could still
        arrive, and the broadcast it completes would come looking for rows that
        are gone. So the offer has to be spent, expired, or given up on -- and
        `stopped` is what "given up on" is called here, which is why a stopped
        run is deletable with a piece still marked `sending` while a running one
        is not. What that costs, stated: a signature that arrives after such a
        deletion still pays and still lands -- the chain does not know about
        this book -- it simply arrives as a piece of a set this node no longer
        keeps. Nothing is lost but the row.

        The folder goes too, and only a folder that is inside this node's
        upload directory: `Runs.delete` takes rows only, on the reason in that
        module that a directory with no rule written for it is not a directory
        to remove from a book. This is the rule, so the account's pictures stop
        sitting on somebody else's disk after the account has walked away from
        them -- which is the thing the retention note has been saying an
        operator should know about since the day it was written.
        """
        account = _signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        run = _runs.get(str(said.get("run", "")))
        if run is None:
            return JSONResponse({"detail": "there is no run of that id"},
                                status_code=404)
        if run["account"] != account.pubkey.lower():
            return JSONResponse({"detail": "that run belongs to somebody else"},
                                status_code=403)
        # `stopped` is in this condition as the account's own "I am not doing
        # this run any more". A piece left `sending` by an offer nobody signed
        # is the ordinary state of a run whose tab closed, and a rule that
        # refused deletion forever on the strength of it would leave every
        # abandoned run's pictures on this disk past the point where anybody
        # wanted them -- which is the exact thing the retention note tells an
        # operator about.
        if run["sending"] and run["status"] != "stopped":
            return JSONResponse(
                {"detail": "one of its pieces is out for a signature. Sign it, "
                           "let it expire, or stop the run first -- a run "
                           "deleted under a signature that later arrives "
                           "leaves that inscription unrecorded. Nothing has "
                           "been paid for."}, status_code=400)
        folder = Path(run["folder"])
        _runs.delete(run["id"])
        uploads = _collection_upload_dir().resolve()
        if run["folder"] and folder.exists():
            try:
                inside = folder.resolve().is_relative_to(uploads)
            except OSError:
                inside = False
            if inside:
                shutil.rmtree(folder, ignore_errors=True)
            else:
                # A build this node did not save is somebody else's directory.
                state.flash(f"{run['name']} is forgotten. Its pictures are not "
                            "in this node's upload folder, so they were left "
                            "where they are.", "err")
        return JSONResponse({"run": run["id"], "name": run["name"],
                             "forgotten": True})

    @app.post("/account/run/piece")
    def account_run_piece(request: Request, payload: Any = Body(None)):
        """Offer the next piece of a run, or say that the run is finished.

        The piece is built the way a single inscription is -- `_class_c_or_b`
        over the account's own coins -- carrying the id written down when the
        run was, so an offer that expired and was rebuilt inscribes the same
        piece rather than a new one beside it. `next_piece` answers the piece
        an offer has already gone out for before it answers one never
        offered, which is what makes a closed tab and an expired offer the
        same recoverable event rather than a lost piece.

        The hour's `inscribe` dial paces a run rather than being waived for
        it. A hundred pieces at ten an hour takes ten hours, and that is the
        operator's number doing its job on the one action this node carries
        forever -- not an obstacle for a run to route around.
        """
        account = _signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        run = _runs.get(str(said.get("run", "")))
        if run is None:
            return JSONResponse({"detail": "there is no run of that id"},
                                status_code=404)
        if run["account"] != account.pubkey.lower():
            return JSONResponse({"detail": "that run belongs to somebody else"},
                                status_code=403)
        chain = _chain_on(run["network"])
        piece = _runs.next_piece(run["id"])
        if piece is None:
            if run["failed_pieces"]:
                # Not finished. It is the run that stopped, not the work, and
                # `finished: True` here would be the page saying all forty are
                # up while one of them was never even offered.
                return JSONResponse(
                    {"detail": f"{run['failed_pieces']:,} of {run['name']} "
                               "refused and will not go out as this run is "
                               "written. Nothing has been paid for. Retry them "
                               "if the reason was on this node's side; if the "
                               "build itself is short, upload the set again "
                               "and it will be written down as only what is "
                               "still missing."}, status_code=400)
            return JSONResponse({"run": run["id"], "sent": run["sent"],
                                 "items": run["items"], "finished": True})
        try:
            content = (Path(run["folder"]) / piece["image"]).read_bytes()
            plan = inscribelib.plan(content, piece["content_type"],
                                    piece["json"],
                                    inscription_id=bytes.fromhex(
                                        piece["inscription_id"]))
        except (OSError, ValueError) as exc:
            # This node cannot make this piece at all, so saying no again on
            # the next press would be the same refusal forever and the run
            # would sit `running` over a hole nobody is told about. Filed as
            # refused, with the reason on it, the rest of the set still goes.
            _runs.piece_failed(run["id"], piece["edition"], str(exc))
            return JSONResponse(
                {"detail": f"{piece['name']} cannot be built from the build "
                           f"this node kept: {exc}. Nothing has been paid for, "
                           "and the pieces after it are still here to ask "
                           "for."}, status_code=400)
        try:
            outputs = _class_c_or_b(chain, run["address"], plan.payloads[0],
                                    _coin_pubkey(account.pubkey, chain))
            index = state.token_index(chain)
            with contextlib.closing(index.open()) as db:
                unsigned = fundinglib.build(
                    db, chain.params, run["address"], outputs,
                    rate=fees.MIN_FEE_PER_KB,
                    what=f"inscribe {piece['name']}",
                    exclude=_flights.spent_by(account.pubkey, chain.network),
                    extra=_flights.change_for(account.pubkey, chain.network))
            _quota(account, "inscribe", len(content))
        except (fundinglib.FundingError, ValueError) as exc:
            # Not a failure of the piece: no coins and a closed dial are both
            # true of this minute and not of the piece, so it stays pending
            # and the next press builds it. A piece filed `failed` here would
            # need a button press to come back for a reason that fixes itself.
            return JSONResponse({"detail": str(exc)}, status_code=400)
        if run["status"] == "stopped":
            # Asking for the next piece is how an account takes back its own
            # "stop" without a second button whose only job is to undo the
            # first. The pieces were always still owed; what `stopped` gave up
            # was the run's place as the one this account is working on.
            _runs.set_status(run["id"], "running")
        # Before the offer goes out, not after: a browser that never comes
        # back has to leave the piece `sending`, which is the state
        # `next_piece` puts first again, rather than looking never-asked-for
        # to everything except the coins the offer has already reserved.
        _runs.offer_piece(run["id"], piece["edition"])
        offer = _offers.add(
            account.pubkey, chain.network, unsigned, unsigned.what,
            done=lambda txid: _runs.record_piece(run["id"], piece["edition"],
                                                 txid))
        return JSONResponse({"offer": offer.id, "run": run["id"],
                             "piece": piece["edition"], "items": run["items"],
                             "sent": run["sent"], "name": piece["name"],
                             "bytes": plan.content_len,
                             "chain": chain.network, **unsigned.as_json()})

    @app.post("/account/nft/send")
    def account_nft_send(request: Request, payload: Any = Body(None)):
        """Offer to hand one inscription to somebody. Nothing is broadcast.

        A transfer is an arcade payload plus a REFERENCE output: a dust
        payment to the recipient, which is how the engine reads who the
        piece went to (`tx.determine_reference`). The change goes back to
        the sender, as it must -- a Class B payload's obfuscation is seeded
        from the sender, so change that wandered elsewhere would make the
        payload unreadable by everybody.

        Two steps, and for a stronger reason than a coin send: this one
        cannot be undone by sending it back unless the person on the other
        end agrees to.
        """
        account = _signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = _chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = _account_address(account.pubkey, chain)
        if not address:
            return JSONResponse(
                {"detail": f"this account has no {chain.label.lower()} "
                           f"address yet"}, status_code=400)
        try:
            index = state.token_index(chain)
            row = index.inscription(contentlib._key(str(said.get("piece", ""))))
            if row is None:
                raise ValueError("no such inscription on this node")
            if row["owner"] != address:
                raise ValueError("that piece is not this account's to send")
            to = str(said.get("to", "")).strip()
            if taglib.looks_like_a_tag(to):
                to = _where_to_pay(taglib.normalise(to), chain)
            complaint = _check_address(to, mainnet=chain.is_mainnet)
            if complaint:
                raise ValueError(complaint)
            if to == address:
                raise ValueError("that is this account's own address")
            body = inscriptionlib.Transfer(
                txid=bytes.fromhex(row["txid"])).encode()
            outputs = _class_c_or_b(chain, address, body,
                                    _coin_pubkey(account.pubkey, chain))
            # The recipient's dust, last: the reference rule skips the
            # first output back to the sender as change and takes the last
            # of the rest, so this is the one it lands on either way.
            outputs.append((sendermod.OUTPUT_VALUE, txbuild.p2pkh_script(to)))
            with contextlib.closing(index.open()) as db:
                unsigned = fundinglib.build(
                    db, chain.params, address, outputs,
                    rate=fees.MIN_FEE_PER_KB,
                    what=f"send inscription #{row['number']} to {to}",
                    exclude=_flights.spent_by(account.pubkey, chain.network),
                    extra=_flights.change_for(account.pubkey, chain.network))
            _quota(account, "send")
        except (taglib.TagError, fundinglib.FundingError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except Exception as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        offer = _offers.add(account.pubkey, chain.network, unsigned,
                            unsigned.what)
        return JSONResponse({"offer": offer.id, "to": to,
                             "number": row["number"], "chain": chain.network,
                             **unsigned.as_json()})

    @app.post("/account/nft/sell")
    def account_nft_sell(request: Request, payload: Any = Body(None)):
        """Offer to put a price on a piece, or to take the price off.

        An ask is a standing, public instruction and not an escrow: the
        piece stays where it is and the ask is honoured only while its
        owner still holds it, which is why the engine refuses one from
        anybody but the owner (D-037). So there is no reference output and
        nothing moves -- what goes on the chain is the price.

        Taking it off is the same payload with nothing in the take leg.
        """
        account = _signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = _chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = _account_address(account.pubkey, chain)
        if not address:
            return JSONResponse(
                {"detail": f"this account has no {chain.label.lower()} "
                           f"address yet"}, status_code=400)
        try:
            index = state.token_index(chain)
            row = index.inscription(contentlib._key(str(said.get("piece", ""))))
            if row is None:
                raise ValueError("no such inscription on this node")
            if row["owner"] != address:
                raise ValueError(
                    "only whoever holds a piece can price it, and this one "
                    f"is held by {row['owner']}")
            if said.get("unlist"):
                take = inscriptionlib.Leg(inscriptionlib.LEG_NONE)
                what = f"take the price off inscription #{row['number']}"
            else:
                take = swaplib.leg_of(
                    mintpadlib.take_of(
                        str(said.get("kind", "coins")),
                        str(said.get("amount", "")),
                        int(said["property_id"]) if said.get("property_id")
                        else None),
                    index)
                what = f"price inscription #{row['number']}"
            body = inscriptionlib.Ask(txid=bytes.fromhex(row["txid"]),
                                      take=take).encode()
            outputs = _class_c_or_b(chain, address, body,
                                    _coin_pubkey(account.pubkey, chain))
            with contextlib.closing(index.open()) as db:
                unsigned = fundinglib.build(
                    db, chain.params, address, outputs,
                    rate=fees.MIN_FEE_PER_KB, what=what,
                    exclude=_flights.spent_by(account.pubkey, chain.network),
                    extra=_flights.change_for(account.pubkey, chain.network))
            _quota(account, "list")
        except (tokenlib.TokenError, fundinglib.FundingError, AmountError,
                ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except Exception as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        offer = _offers.add(account.pubkey, chain.network, unsigned, what)
        return JSONResponse({"offer": offer.id, "what": what,
                             "number": row["number"], "chain": chain.network,
                             **unsigned.as_json()})

    @app.post("/account/offer")
    def account_offer(request: Request, payload: Any = Body(None)):
        """Say on the chain what this account would pay for somebody's piece.

        The same bytes the operator's `/exchange/offer` writes -- an `Offer`
        in an `AnyData` in one OP_RETURN -- funded from the account's address
        and signed in its own tab, so this node pays nothing and holds
        nothing of it. An offer is a message and not an escrow: the piece
        stays where it is, none of this account's coins are reserved, and what
        it spends is a fee. Whoever holds it finds it from their own node, and
        nothing here waits for them (D-038).

        One thing the operator's route does that this one cannot: it puts the
        piece out of its own wallet's reach while the offer stands, with
        `lockunspent`. A node holding nobody's key cannot lock anything, and
        the account's equivalent is not in this request -- it is the signed leg
        `/account/list` files, which is the seller committing the piece and
        belongs on the seller's own screens, not on a page somebody uses to ask.

        What is refused here is what the other side would refuse later, moved
        forward to before the fee (D-040): a price in a token this account
        does not hold, a price in more coins than its address holds, an offer
        on a piece that is already this account's, and -- the one that costs
        the whole offer -- an account with no published key, which nobody can
        answer.
        """
        account = _signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = _chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = _account_address(account.pubkey, chain)
        if not address:
            return JSONResponse(
                {"detail": f"this account has no {chain.label.lower()} "
                           f"address yet"}, status_code=400)
        try:
            index = state.token_index(chain)
            row = index.inscription(contentlib._key(str(said.get("piece", ""))))
            if row is None:
                raise ValueError("no such inscription on this node")
            if row["owner"] == address:
                raise ValueError("that one is already yours")
            take = swaplib.leg_of(
                mintpadlib.take_of(
                    str(said.get("kind", "coins")),
                    str(said.get("amount", "")),
                    int(said["property_id"]) if said.get("property_id")
                    else None),
                index)
            price = swaplib.describe_leg(swaplib.leg_json(take, index))
            with state.store() as store:
                if store.key_for(address) is None:
                    raise ValueError(
                        "publish your key first, from your own page, or "
                        "whoever holds this cannot answer you. It costs a "
                        "small fee and is done once.")
            if take.kind == inscriptionlib.LEG_TOKEN and \
                    index.balance(address, take.property_id) < take.amount:
                raise ValueError(f"this account does not hold {price}. An "
                                 f"offer is not a promise a node can keep -- "
                                 f"it is the terms of a trade, and offering "
                                 f"in a token you do not have spends your fee "
                                 f"to be told no by whoever holds the piece.")
            with contextlib.closing(index.open()) as db:
                if take.kind == inscriptionlib.LEG_COINS:
                    enough = utxoslib.balance(db, address)
                    if enough < take.amount:
                        raise ValueError(
                            f"this account holds {format_amount(enough, True)} "
                            f"on {chain.label.lower()}, which is less than the "
                            f"{price} offered. Anything on its way back to it "
                            f"is counted once its block lands.")
                body = inscriptionlib.Offer(txid=bytes.fromhex(row["txid"]),
                                            take=take).encode()
                outputs = _class_c_or_b(chain, address, body,
                                        _coin_pubkey(account.pubkey, chain))
                what = f"offer {price} for inscription #{row['number']}"
                unsigned = fundinglib.build(
                    db, chain.params, address, outputs,
                    rate=fees.MIN_FEE_PER_KB, what=what,
                    exclude=_flights.spent_by(account.pubkey, chain.network),
                    extra=_flights.change_for(account.pubkey, chain.network))
            terms = swaplib.leg_json(take, index)
            _quota(account, "trade")
        except (tokenlib.TokenError, fundinglib.FundingError, AmountError,
                swaplib.SwapError, mintpadlib.MintpadError,
                inscriptionlib.InscriptionError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except Exception as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)

        def note(txid: str) -> None:
            """This node's own memory of what it asked for, keyed by the offer.

            The chain is what an answer is checked against, and every page
            that shows an offer reads the index (D-049). This row is only so
            that a reply landing before its own block still finds its terms --
            the same note the operator's route leaves, in the same book.
            """
            now = time.time()
            state.offers.add_bid({
                "id": txid, "network": chain.network, "direction": "out",
                "inscription": row["txid"], "number": row["number"],
                "owner": row["owner"], "buyer": address, "peer_pubkey": "",
                "take": terms, "created": now,
                "expires": now + swaplib.OFFER_TTL * 24})

        offer = _offers.add(account.pubkey, chain.network, unsigned, what,
                            done=note)
        return JSONResponse({"offer": offer.id, "what": what,
                             "number": row["number"], "chain": chain.network,
                             **unsigned.as_json()})

    def _ask_payload(row: dict, price: int) -> bytes:
        """What a listing writes at output 0: the trade the finished swap IS.

        A swap payload rather than an ask payload, and that choice is what makes
        a pre-signed listing possible at all: by the time a block carries these
        bytes the buyer's input is in the same transaction, and
        `state.Engine._swap` reads both parties from the INPUTS because
        `inscriptions.Swap` has no room to name a buyer. So the bytes can be
        written, signed and advertised while the other party is still a
        stranger -- which is exactly the promise a leg is.

        Wrapped in `AnyData` for the reason `_class_c_or_b` gives, and Class C
        only: a leg's two outputs are the bytes it sells and the payment it
        accepts, with one signature standing over each, so there is nowhere to
        put the dust outputs a longer payload would need.
        """
        from ..encoding import EncodingError, encode_class_c

        body = inscriptionlib.Swap(
            give=inscriptionlib.Leg(inscriptionlib.LEG_INSCRIPTION,
                                    txid=bytes.fromhex(row["txid"])),
            take=inscriptionlib.Leg(inscriptionlib.LEG_COINS,
                                    amount=price)).encode()
        try:
            return encode_class_c(P.AnyData(data=body).encode())
        except EncodingError as exc:
            raise ValueError(
                f"{exc} A listing is written in one OP_RETURN because a leg has "
                f"two outputs and both are signed: what it sells, and what it "
                f"costs.") from None

    def _listing_swap(naming: bytes) -> inscriptionlib.Swap | None:
        """The trade a listing's bytes promise, or nothing if they promise none.

        One reading, because two surfaces have to say the same sentence about
        the same row: the page that lists it and the request that shows a buyer
        the transaction which fills it. What they read is the payload and not
        the row's `input`, because a listing's input is a COIN -- the seller's
        own, whose index signs the bytes -- while the piece that changes hands
        is the inscription the payload names. For a piece that arrived by
        transfer those are two different transactions, and a reader that asked
        the coin would be describing the seller's change.

        The reading itself is `listings.named_swap`, which the node's own
        completion of a leg reads the same way: which piece a leg sells is one
        fact, and the page and the wallet cannot be allowed to answer it
        separately.
        """
        return listingslib.named_swap(naming)

    def _listing_words(naming: bytes, chain) -> str:
        """What a filed listing's own bytes say it sells, in words.

        The other direction from `_ask_payload`, and it is only honest to read
        the row's payload rather than repeat what the browser said, because
        the payload is the one part of a listing that a signature actually
        stands over. `Listings.register` compares those bytes against the leg
        byte for byte, so a sentence worked out from them cannot promise a
        trade the signatures do not back -- which is more than any wording
        handed in with the request could claim.

        Returns nothing rather than guessing: a payload this repository cannot
        read is a listing that sold anyway, and the price beside it is the
        truth a page has to carry.
        """
        swap = _listing_swap(naming)
        if swap is None:
            return ""
        index = state.token_index(chain)
        try:
            give = swaplib.describe_leg(swaplib.leg_json(swap.give, index))
            take = swaplib.describe_leg(swaplib.leg_json(swap.take, index))
        except swaplib.SwapError:
            return ""
        return f"gives {give} and takes {take}"

    @app.post("/account/list")
    def account_list(request: Request, payload: Any = Body(None)):
        """Show an account the leg it is about to sign, and stop there.

        Two requests, with nothing kept between them, because the signature
        outlives the tab that made it: this one reads the index and builds a
        leg, the next takes the signatures back and files the row. What binds
        them is the arithmetic in `Listings.register` and not a memory of this
        one, so a node that restarts in the middle files the identical listing
        instead of stranding a signature the browser has already made.

        Nothing is spent here, so nothing is charged: an account could compute
        this leg itself, since it holds the key and this node never sees it. The
        allowance is spent at `/account/list/sign`, which is the request that
        puts a row on a public page.

        What comes back is the leg's own bytes, the TWO digests it asks to be
        signed, and the words -- the words first, because they are what is being
        decided on. One signature is over the bytes naming the piece and the
        other over the price, and a leg that got that pairing wrong would be
        sold by a signature that promises something else.
        """
        account = _signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = _chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = _account_address(account.pubkey, chain)
        if not address:
            return JSONResponse(
                {"detail": f"this account has no {chain.label.lower()} "
                           f"address yet"}, status_code=400)
        try:
            price = parse_amount(str(said.get("amount", "")), True)
            if price <= 0:
                raise ValueError("a listing names a price, and that is nothing")
            _above_dust(price, "price")
            index = state.token_index(chain)
            row = index.inscription(contentlib._key(str(said.get("piece", ""))))
            if row is None:
                raise ValueError("no such inscription on this node")
            if row["owner"] != address:
                raise ValueError(
                    "only whoever holds a piece can price it, and this one "
                    f"is held by {row['owner']}")
            naming = _ask_payload(row, price)
            piece = swaplib.describe_leg(swaplib.leg_json(
                swaplib.leg_of({"inscription": row["txid"]}, index), index))
            cost = swaplib.describe_leg(swaplib.leg_json(
                inscriptionlib.Leg(inscriptionlib.LEG_COINS, amount=price),
                index))
            what = f"list {piece} for {cost}"
            with contextlib.closing(index.open()) as db:
                held = [c for c in utxoslib.unspent(db, address)
                        if (c["txid"], c["vout"])
                        not in _flights.spent_by(account.pubkey, chain.network)]
                if len(held) < 2:
                    raise ValueError(
                        f"this address has {len(held)} coin to spend and a "
                        f"listing that says what it sells needs two of them: "
                        f"one input signs the bytes naming the piece, the other "
                        f"signs the price. Send yourself a little change and "
                        f"list it again.")
                leg = fundinglib.build_leg(
                    chain.params, address, held[0], coins=price,
                    rate=fees.MIN_FEE_PER_KB, what=what,
                    payload=naming, coin=held[1])
        except (fundinglib.FundingError, inscriptionlib.InscriptionError,
                swaplib.SwapError, AmountError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse({"chain": chain.network,
                             "price": price, "number": row["number"],
                             **leg.as_json()})

    @app.post("/account/list/sign")
    def account_list_sign(request: Request, payload: Any = Body(None)):
        """File the leg this browser signed, from its bytes and the chain alone.

        `Listings.register` is the whole of it: the piece's value comes from
        `gettxout`, the payload and the price and the outpoints come from the
        leg's own bytes, and the row is refused unless the numbers close. So
        this route needs nothing left over from `/account/list`, and an account
        that built the leg somewhere else files the same row it would have
        filed anyway -- there is no privileged path.

        The allowance is spent here, before the filing, and deliberately not
        after it: a leg can be built offline by anyone holding the key, so this
        is the request that can be looped, and what the loop would buy is rows
        on a public page. A refusal therefore costs an allowance it did not use,
        which is the direction an allowance is allowed to go wrong in.

        The owner is not taken on faith either: `check_leg` refuses a leg whose
        public key does not hash to the address it is listed from, so an account
        cannot credit its sale to somebody else.

        No sentence is stored with the row, and none is taken from this request
        either. A listing's words belong to its payload -- which is signed, and
        which `register` compares byte for byte -- so the sentence is read back
        out of the row below rather than written beside it. A seller's note
        about their own piece is a post, and there is a route for that.
        """
        account = _signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = _chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = _account_address(account.pubkey, chain)
        if not address:
            return JSONResponse(
                {"detail": f"this account has no {chain.label.lower()} "
                           f"address yet"}, status_code=400)
        raw = str(said.get("raw") or "")
        try:
            _quota(account, "list", nbytes=len(bytes.fromhex(raw)))
            price = parse_amount(str(said.get("amount", "")), True)
            with chain.rpc() as rpc:
                listing = state.listings.register(
                    rpc, raw=raw,
                    signatures=[str(s) for s in (said.get("signatures") or [])],
                    pubkey=bytes.fromhex(str(said.get("pubkey") or "")),
                    network=chain.network, owner=address, price=price,
                    seconds=listingslib.LISTED_FOR)
        except (listingslib.ListingError, fundinglib.FundingError,
                swaplib.SwapError, AmountError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        # Both of the coins this leg spends are gone as far as this account is
        # concerned, from this moment and not from whenever a buyer turns up.
        # Nothing this account broadcasts retires them -- the transaction that
        # spends them is somebody else's, and may never be broadcast at all --
        # so without this note the leg's own fee coin stays in the index looking
        # spendable, and the next thing this account buys spends the coin that
        # was already promised to pay for the sale.
        committed = [(listing["input"]["txid"], listing["input"]["vout"])]
        if listing.get("coin"):
            committed.append((listing["coin"]["txid"], listing["coin"]["vout"]))
        _flights.note_committed(account.pubkey, tuple(committed),
                               network=chain.network)
        return JSONResponse({"listed": listing["id"],
                             "what": _listing_words(
                                 bytes.fromhex(listing["payload"]), chain),
                             "chain": chain.network,
                             "expires": listing["expires"],
                             "piece": f"{listing['input']['txid'][:16]}…"
                                      f":{listing['input']['vout']}"})

    def _listing_to_fill(account, chain, address: str, listing_id: str) -> tuple:
        """A listing this node filed, and the transaction that completes it.

        The row out of the book, and then the whole of `_fill_terms`, which is
        where the trade is decided -- and which an answered leg that was never
        filed reaches the same way.
        """
        listing = state.listings.get(str(listing_id))
        if listing is None or listing["network"] != chain.network:
            raise ValueError(
                "no such listing on this chain -- it may have expired, been "
                "filled, or been made over on the other one")
        return _fill_terms(account, chain, address, listing)

    def _fill_terms(account, chain, address: str, listing: dict) -> tuple:
        """A leg's trade, and the transaction this account's signature completes.

        Both halves of a buy come through here, which is why it is written
        once: the request that shows a buyer the trade and the request that
        finishes it decide it identically, from the row and the chain and not
        from anything remembered in between. A signature posted straight at
        the second request therefore meets the same answers as one brought the
        long way round.

        The first of those answers is the rule this route exists to state. A
        listing is a promise made to a stranger, and filling your own is two
        transactions that move nothing while printing a price and a volume on
        a public page as though a stranger had paid it. `swap.countersign` has
        refused it for the operator's wallet since before there were accounts
        (`swap.py:877`), and it would fail by itself anyway: `paste_leg` will
        not take a buyer input that pays to the seller, and in a self-fill
        every buyer input does.

        The row comes from one of two places, which is why this is a function of
        a row and not of an id. Most listings are advertised: filed in the book,
        shown on a page, taken by whoever reads it. A leg can also be answered to
        one buyer and handed over in a message, and then `Listings.register`
        checks every term of it and writes nothing down, because filing it would
        put one buyer's arrangement on a public page for every stranger to take.
        What the trade is does not depend on which of those happened, so neither
        does any of the arithmetic below.
        """
        if listing["owner"] == address:
            raise swaplib.SwapError("a wallet cannot fill its own order")

        index = state.token_index(chain)
        naming = bytes.fromhex(str(listing["payload"] or ""))
        # What the seller's two signatures stand over -- its bytes at output 0,
        # its payment at the next, its own coins in front, and the fee it
        # reserved paid back -- is `listings.leg_terms`, and it is that precisely
        # because a node finishing a leg out of its own wallet has to arrive at
        # the identical transaction. Two readings of one trade, one per buyer, is
        # how two buyers end up with two trades.
        foreign, outputs = listingslib.leg_terms(listing)
        # What changes hands is named by those bytes, never by input 0: a
        # listing's input is the coin the seller spent to sign them, and for a
        # piece that arrived by transfer that coin comes from a different
        # transaction than the inscription does. A leg that names nothing sells
        # its own input, which is why that is what gets said then.
        named = _listing_swap(naming)
        piece = (f"{listing['input']['txid'][:16]}…:{listing['input']['vout']}"
                 if named is None else
                 swaplib.describe_leg(swaplib.leg_json(named.give, index)))
        cost = swaplib.describe_leg(swaplib.leg_json(
            inscriptionlib.Leg(inscriptionlib.LEG_COINS,
                               amount=int(listing["price"])), index))
        what = f"buy {piece} for {cost}"
        with contextlib.closing(index.open()) as db:
            unsigned = fundinglib.build_partial(
                db, chain.params, address, foreign, outputs,
                rate=fees.MIN_FEE_PER_KB, what=what,
                exclude=_flights.spent_by(account.pubkey, chain.network),
                extra=_flights.change_for(account.pubkey, chain.network))
        return listing, unsigned, what

    @app.post("/account/buy")
    def account_buy(request: Request, payload: Any = Body(None)):
        """Show an account the transaction that fills somebody else's listing.

        Nothing is spent here, exactly as `/account/list` spends nothing: the
        buyer holds the key and this node never sees it, so it could work the
        transaction out for itself. What comes back is this node's own reading
        of a row it filed -- the piece, the price and the payment output come
        off the leg the seller signed, and the coins behind them come from the
        chain -- plus the buyer's coins, chosen from what the index holds for
        it right now.

        `signed_from` is the part a buyer's browser most needs. It says which
        inputs are the buyer's to sign and which are the seller's and already
        signed, so a tab is never asked for a signature over a coin it does not
        hold -- and cannot be talked into giving one, which is the same
        protection `build_partial` was written for.
        """
        account = _signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = _chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = _account_address(account.pubkey, chain)
        if not address:
            return JSONResponse(
                {"detail": f"this account has no {chain.label.lower()} "
                           f"address yet"}, status_code=400)
        try:
            listing, unsigned, _ = _listing_to_fill(
                account, chain, address, str(said.get("listing", "")))
        except (fundinglib.FundingError, listingslib.ListingError,
                swaplib.SwapError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse({"chain": chain.network,
                             "listing": listing["id"],
                             "seller": listing["owner"],
                             "price": int(listing["price"]),
                             **unsigned.as_json()})

    @app.post("/account/buy/sign")
    def account_buy_sign(request: Request, payload: Any = Body(None)):
        """Paste the buyer's signatures onto the seller's leg, and broadcast it.

        `listings.paste_leg`, not `swap.countersign`, and deliberately not a
        branch of `/account/sign`. This node signs nothing here: it already
        holds both of the seller's signatures, from the request that filed the
        row, and all it adds is the buyer's, into the inputs `build_partial`
        left empty for exactly that. Whether a node signed a transaction or
        pasted in a signature it was handed is the difference the whole
        multi-user plan is built on, so it stays in a name and in a stack trace
        instead of becoming one more path through a route that does both.

        Nothing is remembered between this and `/account/buy`, so the trade is
        decided again by the same code, out of the row and the chain. That
        costs one thing worth naming: if this account's coins changed in the
        meantime, this node would now build a different transaction, and the
        signatures in this request are over the one it showed first. So the
        bytes are compared and a mismatch is refused with what actually
        happened, rather than going to the network with signatures that do not
        fit and coming back as a code nobody reading it can act on.
        """
        account = _signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = _chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = _account_address(account.pubkey, chain)
        if not address:
            return JSONResponse(
                {"detail": f"this account has no {chain.label.lower()} "
                           f"address yet"}, status_code=400)
        # This account's own lane, for the reason `/account/sign` gives: it
        # covers the broadcast and the note after it, not the build before it.
        lane = f"account {account.pubkey}"
        if not state.begin_send(lane):
            return JSONResponse(
                {"detail": "one of your transactions is still going. Wait for "
                           "it to be broadcast and try again -- signing two at "
                           "once from one wallet spends the same coin twice."},
                status_code=409)
        try:
            try:
                listing, unsigned, what = _listing_to_fill(
                    account, chain, address, str(said.get("listing", "")))
            except (fundinglib.FundingError, listingslib.ListingError,
                    swaplib.SwapError, ValueError) as exc:
                return JSONResponse({"detail": str(exc)}, status_code=400)
            try:
                _real_coins_gate(account, chain)
                if str(said.get("raw") or "") != unsigned.raw:
                    return JSONResponse(
                        {"detail": "this node would build you a different "
                                   "transaction now than the one you signed -- "
                                   "a block landed, or one of your other "
                                   "transactions went out. Ask for another one "
                                   "and sign that; a signature over the old "
                                   "bytes would buy something you never "
                                   "agreed to."}, status_code=409)
                signatures = [str(x) for x in (said.get("signatures") or [])]
                pubkey = bytes.fromhex(str(said.get("pubkey") or ""))
                if not pubkey:
                    raise ValueError("this request names no public key, and a "
                                     "transaction nobody can trace to a key is "
                                     "not a transaction this node will finish")
                if hash160(pubkey) != b58check_decode(address)[1]:
                    # The buyer's side of what `check_leg` insists on for the
                    # seller's: the key pasted into a scriptSig has to be the
                    # key behind the address the value leaves. Otherwise the
                    # signatures say one wallet paid and the chain says
                    # another one did.
                    raise ValueError(
                        "that public key is not the key behind this account's "
                        f"{chain.label.lower()} address, so pasting it would "
                        "write a sale one wallet paid and another is named on")
                raw = None
                with chain.rpc() as rpc:
                    raw = listingslib.paste_leg(rpc, listing, unsigned,
                                                signatures, pubkey)
                _quota(account, "send", nbytes=len(bytes.fromhex(raw)))
                with chain.rpc() as rpc:
                    txid = rpc.call("sendrawtransaction", raw)
            except (listingslib.ListingError, fundinglib.FundingError,
                    swaplib.SwapError, AmountError, ValueError) as exc:
                return JSONResponse({"detail": str(exc)}, status_code=400)
            except Exception as exc:
                return JSONResponse({"detail": f"the node refused it: {exc}"},
                                    status_code=409)
            # The row says filled, with the transaction that spent the piece
            # beside it. This is the only place a listing reaches that word:
            # nothing in this book could have known the sale happened, and a
            # row left `open` would be offered to the next buyer as though it
            # were still for sale.
            state.listings.close(listing["id"], "filled", spent_by=txid)
            _flights.add(account.pubkey, txid, unsigned, address,
                         network=chain.network)
            state.bump_generation()
            return JSONResponse({"txid": txid, "what": what,
                                 "chain": chain.network,
                                 "seller": listing["owner"],
                                 "price": int(listing["price"])})
        finally:
            state.end_send(lane)

    def _leg_answered(said: dict, chain) -> dict:
        """A leg that arrived by message: checked end to end, left unwritten.

        The seller's address, its public key and its price all come in through
        this request, and none of them is taken on faith. `Listings.register`
        refuses a leg whose key does not hash to the address it is listed from,
        refuses one that pays somewhere other than that address, and derives the
        price from the piece, the coin behind the second signature and the fee
        the leg's own numbers imply -- then refuses the row again if the two
        prices ever disagree.

        The one thing it cannot check is whether this was the price THIS buyer
        asked for. The tab that sent the offer is the only place that knows, so
        the price comes back on the response to be compared there, before
        anything is signed. The operator's version of this completion does have
        a note to compare against, and `shopkeeper._bid` says "that is not the
        price that was offered" when the two disagree.

        `seconds` is `ANSWERED_FOR`, whose own comment gives the reason: an
        answered leg is never advertised and never swept by `expire_due`, so the
        deadline that means anything is the piece being spent -- which
        `paste_leg` asks the chain about, every single time.

        The leg travels as one object for the same reason it travels at all: it
        is one thing somebody signed. It also keeps the request around it the
        same shape as `/account/buy/sign`'s, down to the field names -- and those
        two cannot share a `signatures` field anyway, because one list is the
        seller's, over the leg's outputs, and the other is this account's, over
        its own coins. Mixing them up would be a silent mixup, which is the one
        kind worth designing out.
        """
        leg = said.get("leg")
        leg = leg if isinstance(leg, dict) else {}
        with chain.rpc() as rpc:
            return state.listings.register(
                rpc, raw=str(leg.get("raw") or ""),
                signatures=[str(s) for s in (leg.get("signatures") or [])],
                pubkey=bytes.fromhex(str(leg.get("pubkey") or "")),
                network=chain.network, owner=str(leg.get("seller") or ""),
                price=parse_amount(str(leg.get("amount", "")), True),
                seconds=listingslib.ANSWERED_FOR, record=False)

    @app.post("/account/fill")
    def account_fill(request: Request, payload: Any = Body(None)):
        """Show an account the transaction that completes a leg sent to it.

        The other half of a listing is `/account/buy`, and this is that same
        trade arrived at from the other side: not a row this node is advertising,
        but a leg some particular seller answered to this particular buyer and
        handed over in a message, its signatures beside it rather than inside it.
        It is deliberately not filed -- a filed row puts the price of a private
        arrangement on a public page for any stranger to take -- so every check
        `Listings.register` has ever run still runs, and nothing is written.

        Nothing is spent here, and nothing is kept. The leg stays in the tab that
        holds the message it came in, which is the same rule `/account/buy`
        follows with a row it did write: the second request decides the trade
        again from the leg's bytes and the chain, so a tab that reloaded still
        finishes the same transaction, and a node that restarted in between has
        lost nothing that mattered.
        """
        account = _signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = _chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = _account_address(account.pubkey, chain)
        if not address:
            return JSONResponse(
                {"detail": f"this account has no {chain.label.lower()} "
                           f"address yet"}, status_code=400)
        try:
            listing, unsigned, _ = _fill_terms(
                account, chain, address, _leg_answered(said, chain))
        except (fundinglib.FundingError, listingslib.ListingError,
                swaplib.SwapError, AmountError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        # No `listing` in the answer, on purpose. This row has an id, and it
        # means nothing outside this one request: posted to `/account/buy` it
        # would come back as "no such listing on this chain", which is true and
        # useless. `answered` says what it is instead, and the seller's own
        # address is what a page has to hold onto.
        return JSONResponse({"chain": chain.network, "answered": True,
                             "seller": listing["owner"],
                             "price": int(listing["price"]),
                             **unsigned.as_json()})

    @app.post("/account/fill/sign")
    def account_fill_sign(request: Request, payload: Any = Body(None)):
        """Paste this account's signatures onto a leg it was handed, and send it.

        The guards are `/account/buy/sign`'s, word for word, and they have to
        stay that way: a buyer told one sentence about a listing and a different
        one about an answer is being told two things about one transaction. The
        byte comparison is the one that matters most here, because the leg came
        through a message rather than off a page this node drew -- the transaction
        this node would build now is compared against the bytes that were signed,
        and a mismatch says so instead of carrying signatures that do not fit to
        the network and taking back a rejection code nobody reading it can act on.

        What differs is the end. There is no row to mark `filled`, because there
        was no row: the transaction is the record, and the leg stops working the
        moment the piece is spent, which was the only cancellation either side
        ever had -- advertised or answered.
        """
        account = _signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = _chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = _account_address(account.pubkey, chain)
        if not address:
            return JSONResponse(
                {"detail": f"this account has no {chain.label.lower()} "
                           f"address yet"}, status_code=400)
        lane = f"account {account.pubkey}"
        if not state.begin_send(lane):
            return JSONResponse(
                {"detail": "one of your transactions is still going. Wait for "
                           "it to be broadcast and try again -- signing two at "
                           "once from one wallet spends the same coin twice."},
                status_code=409)
        try:
            try:
                listing, unsigned, what = _fill_terms(
                    account, chain, address, _leg_answered(said, chain))
            except (fundinglib.FundingError, listingslib.ListingError,
                    swaplib.SwapError, AmountError, ValueError) as exc:
                return JSONResponse({"detail": str(exc)}, status_code=400)
            try:
                _real_coins_gate(account, chain)
                if str(said.get("raw") or "") != unsigned.raw:
                    return JSONResponse(
                        {"detail": "this node would build you a different "
                                   "transaction now than the one you signed -- "
                                   "a block landed, or one of your other "
                                   "transactions went out. Ask for another one "
                                   "and sign that; a signature over the old "
                                   "bytes would buy something you never "
                                   "agreed to."}, status_code=409)
                signatures = [str(x) for x in (said.get("signatures") or [])]
                pubkey = bytes.fromhex(str(said.get("pubkey") or ""))
                if not pubkey:
                    raise ValueError("this request names no public key, and a "
                                     "transaction nobody can trace to a key is "
                                     "not a transaction this node will finish")
                if hash160(pubkey) != b58check_decode(address)[1]:
                    raise ValueError(
                        "that public key is not the key behind this account's "
                        f"{chain.label.lower()} address, so pasting it would "
                        "write a sale one wallet paid and another is named on")
                with chain.rpc() as rpc:
                    raw = listingslib.paste_leg(rpc, listing, unsigned,
                                                signatures, pubkey)
                _quota(account, "send", nbytes=len(bytes.fromhex(raw)))
                with chain.rpc() as rpc:
                    txid = rpc.call("sendrawtransaction", raw)
            except (listingslib.ListingError, fundinglib.FundingError,
                    swaplib.SwapError, AmountError, ValueError) as exc:
                return JSONResponse({"detail": str(exc)}, status_code=400)
            except Exception as exc:
                return JSONResponse({"detail": f"the node refused it: {exc}"},
                                    status_code=409)
            _flights.add(account.pubkey, txid, unsigned, address,
                         network=chain.network)
            state.bump_generation()
            return JSONResponse({"txid": txid, "what": what,
                                 "chain": chain.network,
                                 "seller": listing["owner"],
                                 "price": int(listing["price"])})
        finally:
            state.end_send(lane)

    # --- a shop, for an account: the same door, paid for by the buyer --------
    #
    # `/swap/{txid}` is where a page buys, and behind it every step that costs
    # money is this machine's wallet: it seals the order, it carries the
    # message, it asks its owner to approve the trade. An account has a key
    # instead of a wallet, so each of those steps is offered here and signed
    # there. What the node still does on its own is build transactions and
    # broadcast the ones it was handed signatures for.
    #
    # The far end does not change at all. `swap.make_offer` answers an order
    # from a public key that belongs to no wallet it knows, exactly as it
    # answers the node next door, and `swap.countersign` signs what it offered
    # and nothing else. A shop cannot tell an account from another node and,
    # by design, has no reason to want to.
    #
    # What that costs is three transactions where the operator's wallet spends
    # two: the order, the trade, and the message carrying the trade. The first
    # and the third exist because the key that opens the answer is the
    # account's and is not on this machine, so this machine cannot open the
    # channel on somebody's behalf -- and a node that could would be holding
    # the arrangement up by its own end.

    def _account_shop(said: dict):
        """The shop an account is asking about, and the node that answers for it.

        Read off the inscription every time, because that is the one copy of
        the terms both sides trust: what is for sale, what it costs, whose key
        answers. Nothing is remembered between one request and the next, so
        every half of a buy decides the same thing again from the chain.
        """
        from .. import nodetalk
        chain = _chain_asked(said)
        if chain.network == "main" or chain.params.swaps_from is None:
            raise swaplib.SwapError("shops are testnet only")
        row = _shop_page(str(said.get("shop") or ""), chain)
        shop = swaplib.shop_of(row)
        return chain, row, shop, nodetalk.parse_pubkey(shop["node"])

    def _not_your_own_shop(row: dict, address: str) -> None:
        """A shop may not sell to the account that runs it.

        `swap.countersign` has refused a wallet filling its own order since
        before there were accounts, and `check_offer` refuses an offer whose
        seller sits in the buyer's wallet. Neither of them reaches an account:
        the `own` list they check is the shop's wallet, and this address is not
        in it -- which is the whole point of an account, and the hole this
        closes. A self-sale is two transactions that move nothing while
        printing a price and a volume on a public page as though a stranger had
        paid it.
        """
        if row["owner"] == address:
            raise swaplib.SwapError("this is your own shop")

    def _shop_order(row: dict, address: str, listing_no: int) -> dict:
        """What an order at this shop says, word for word.

        Written here rather than in the browser so the two doors cannot drift
        apart on what a shop is being asked to do. The shop answers the address
        in it, not the key: the key rides in the envelope, which is what lets
        the answer come back sealed to somebody this node has never met.
        """
        return {"swap": "offer", "swapv": swaplib.PROTOCOL, "shop": row["txid"],
                "listing": int(listing_no), "buyer": address}

    def _shop_half(account, chain, address: str, offer: dict) -> tuple:
        """An offer, and the transaction this account's signature completes.

        The shape `_listing_to_fill` gives a listing, from an offer instead of
        a row: the shop's output first and unsigned, the trade named in one
        OP_RETURN, the seller made whole, the cut the offering node announced,
        and the change back here. That ordering is not cosmetics -- the shop's
        signature at countersign time stands over its own input in slot 0 and
        the payment it is owed, and `build_partial` puts the buyer's coins
        behind them and never moves either.

        Both sides are checked before a coin is chosen, the way `swap.build`
        checks them before it touches a wallet. A trade that cannot land is a
        message fee already spent finding that out.
        """
        from ..encoding import encode_class_c
        give = swaplib.leg_from_json(offer["give"])
        take = swaplib.leg_from_json(offer["take"])
        named = str(offer.get("order") or "")
        payload = P.AnyData(data=inscriptionlib.Swap(
            give=give, take=take,
            order=bytes.fromhex(named) if named else b"").encode()).encode()
        # What the shop nets: its own input back, plus the coins this way
        # round, less the coins it hands over. `countersign` measures the same
        # expression when it decides whether it was paid, so the two sides
        # cannot end up reading one price two ways.
        owes = swaplib.coins_in(take) - swaplib.coins_in(give)
        outpoint = offer["outpoint"]
        seller_out = int(outpoint["value"]) + owes
        if seller_out < swaplib.MIN_CHANGE:
            raise swaplib.SwapError("the seller's output would be dust; ask for "
                                    "another offer")
        cut = swaplib.cut_sats(offer.get("cut"), max(owes, 0))
        cut_to = str((offer.get("cut") or {}).get("to") or "") if cut else ""
        outputs = [(0, txbuild.op_return_script(encode_class_c(payload))),
                   (seller_out, txbuild.p2pkh_script(offer["seller"]))]
        if cut:
            outputs.append((cut, txbuild.p2pkh_script(cut_to)))
        index = state.token_index(chain)
        with chain.rpc() as rpc:
            for who, leg, name in ((offer["seller"], give, "the shop"),
                                   (address, take, "this account")):
                problem = swaplib.holds(index, rpc, who, leg)
                if problem:
                    raise swaplib.SwapError(f"{name} cannot give that: {problem}")
        what = (f"buy {swaplib.describe_leg(swaplib.leg_json(give, index))}"
                f" for {swaplib.describe_leg(swaplib.leg_json(take, index))}")
        with contextlib.closing(index.open()) as db:
            unsigned = fundinglib.build_partial(
                db, chain.params, address, [outpoint], outputs,
                rate=fees.MIN_FEE_PER_KB, what=what,
                exclude=_flights.spent_by(account.pubkey, chain.network),
                extra=_flights.change_for(account.pubkey, chain.network))
        return unsigned, what, cut, cut_to

    @app.post("/account/shop")
    def account_shop(request: Request, payload: Any = Body(None)):
        """A shop, asked by the person whose key this node will never see.

        `shop` reads the listings; `offer` says what an order at this shop
        reads as, and to which key it has to be sealed; `send` offers the API
        message that carries it; `accept` shows the trade. Each of the two
        messages is a transaction this account signs and this node broadcasts,
        and the trade itself is signed at `/account/shop/sign`, which
        broadcasts nothing.

        The sealing happens in the browser and the node never learns what is
        inside -- exactly as with a private message from an account, which is
        the only reason the shop's answer can come back sealed to somebody
        this node cannot read for. What is checked here is what can be checked
        without opening anything: that the listing exists, that the buyer is
        not the shop's own account, and that one transaction can carry the
        bytes.
        """
        from .. import nodetalk
        from ..messaging import api as apilib
        account = _signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        op = str(said.get("op") or "")
        try:
            chain, row, shop, node = _account_shop(said)
            address = _account_address(account.pubkey, chain)
            if not address:
                raise ValueError(f"this account has no {chain.label.lower()} "
                                 f"address yet")
            index = state.token_index(chain)
            listing_no = int(said.get("listing", -1))
            if op == "shop":
                height = index.indexed_height()
                with contextlib.closing(index.open()) as db:
                    coins = utxoslib.unspent(db, address)
                return JSONResponse({
                    "ok": True, "shop": row["txid"], "node": node.hex(),
                    "seller": row["owner"], "buyer": address,
                    "chain": chain.network, "network": chain.network,
                    "stamp": apilib.stamp().hex(),
                    "listings": swaplib.listings_json(row, index),
                    # D-051, asked of the index instead of of a wallet: a buy is
                    # two transactions from this side, and an address with one
                    # output left learns that at the last step, after paying for
                    # the order and waiting for an offer.
                    "can_buy": len(coins) >= OUTPUTS_FOR_A_SWAP,
                    # For the account, `mine` is the refusal rather than the
                    # owner's toolkit: there are no owner's controls here, and
                    # a shop's own account is not a customer of it.
                    "mine": row["owner"] == address,
                    "open": row["creator"] == row["owner"],
                    "height": height, "from": chain.params.swaps_from,
                    "ready": height is not None
                             and height >= chain.params.swaps_from})
            if op == "offer":
                if not 0 <= listing_no < len(shop["listings"]):
                    raise swaplib.SwapError(f"no listing {listing_no}")
                _not_your_own_shop(row, address)
                take = swaplib.leg_of(shop["listings"][listing_no]["take"], index)
                return JSONResponse({
                    "ok": True, "chain": chain.network,
                    "order": _shop_order(row, address, listing_no),
                    "seal_to": node.hex(), "to": row["owner"],
                    "stamp": apilib.stamp().hex(),
                    "buying": swaplib.describe_leg(swaplib.leg_json(take, index))})
            if op == "send":
                sealed = bytes.fromhex(str(said.get("sealed") or ""))
                if not sealed:
                    raise ValueError("there is nothing to send")
                _not_your_own_shop(row, address)
                # Type 6 rather than type 1: the same sealed envelope, read by
                # a program at the other end instead of landing in somebody's
                # chat, and answered from a book rather than from a person. The
                # header is written HERE and not in the browser, which is what
                # `/account/write` does with a private message -- and the length
                # of what fits in one transaction is said once, by the function
                # that builds it, rather than twice from two different
                # measurements of bytes this node cannot count.
                header = envelopelib.Header(type=envelopelib.TYPE_API,
                                            clen=len(sealed))
                outputs = _class_c_or_b(chain, address, header.encode() + sealed,
                                        _coin_pubkey(account.pubkey))
                outputs.append((sendermod.OUTPUT_VALUE,
                                txbuild.p2pkh_script(row["owner"])))
                with contextlib.closing(index.open()) as db:
                    unsigned = fundinglib.build(
                        db, chain.params, address, outputs,
                        rate=fees.MIN_FEE_PER_KB,
                        what=f"a message to the shop at {row['txid'][:12]}\u2026",
                        exclude=_flights.spent_by(account.pubkey, chain.network),
                        extra=_flights.change_for(account.pubkey, chain.network))
                _quota(account, "message", len(sealed))
                offered = _offers.add(account.pubkey, chain.network, unsigned,
                                      unsigned.what)
                return JSONResponse({"ok": True, "offer": offered.id,
                                     "bytes": len(sealed), "to": row["owner"],
                                     "node": node.hex(), "chain": chain.network,
                                     **unsigned.as_json()})
            if op == "accept":
                offer = swaplib.check_offer(
                    said.get("offer"), shop=row["txid"], own=[address],
                    height=index.indexed_height(), params=chain.params)
                if offer["seller"] != row["owner"]:
                    raise swaplib.SwapError("the offer is not from the wallet "
                                            "that holds this shop")
                _not_your_own_shop(row, address)
                unsigned, what, cut, cut_to = _shop_half(
                    account, chain, address, offer)
                return JSONResponse({"ok": True, "shop": row["txid"],
                                     "listing": offer["listing"],
                                     "seller": offer["seller"],
                                     "what": what, "chain": chain.network,
                                     "cut": ({"bps": int((offer.get("cut") or {})
                                                         .get("bps") or 0),
                                              "sats": cut, "to": cut_to}
                                             if cut else {}),
                                     **unsigned.as_json()})
            raise swaplib.SwapError("op must be shop, offer, send or accept")
        except (swaplib.SwapError, fundinglib.FundingError, nodetalk.TalkError,
                accountslib.AccountError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except HTTPException:
            raise
        except Exception as exc:
            return JSONResponse({"detail": f"the node could not do it: {exc}"},
                                status_code=409)

    @app.post("/account/shop/sign")
    def account_shop_sign(request: Request, payload: Any = Body(None)):
        """Complete the buyer's half, and hand it back rather than broadcast it.

        The one difference from `/account/buy/sign` that matters. A listing is
        filled by this node pasting two signatures it already holds onto a
        third; a shop buy is finished by the shop, which is the only machine
        that can spend the piece. So nothing goes out here -- there is nothing
        yet that the network would accept -- and what comes back is the bytes,
        for the browser to seal into the message that carries them.

        The half is worked out again from the offer, as `/account/buy/sign`
        works it out from the row, and the raw bytes are compared before
        anything else: if this account's coins moved since the trade was
        shown, these signatures are over a different transaction, and the shop
        would be right to refuse them. Better to say so here.

        The coins are then held back as committed with no broadcast behind
        them, which is what a listing's leg does for the same wait. Without
        it, the index still calls the trade's own inputs spendable -- including
        by the very message that carries them, which would pay for the swap by
        spending it.
        """
        from ..messaging import api as apilib
        account = _signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain, row, _shop, node = _account_shop(said)
            address = _account_address(account.pubkey, chain)
            if not address:
                raise ValueError(f"this account has no {chain.label.lower()} "
                                 f"address yet")
            offer = swaplib.check_offer(
                said.get("offer"), shop=row["txid"], own=[address],
                height=state.token_index(chain).indexed_height(),
                params=chain.params)
            if offer["seller"] != row["owner"]:
                raise swaplib.SwapError("the offer is not from the wallet that "
                                        "holds this shop")
            _not_your_own_shop(row, address)
            unsigned, what, cut, cut_to = _shop_half(account, chain, address, offer)
            if str(said.get("raw") or "") != unsigned.raw:
                return JSONResponse(
                    {"detail": "this node would build you a different "
                               "transaction now than the one you signed -- a "
                               "block landed, or one of your other transactions "
                               "went out. Ask for the trade again and sign that; "
                               "the shop would refuse these signatures, and a "
                               "message spent finding that out is a message fee "
                               "gone."}, status_code=409)
            signatures = [str(x) for x in (said.get("signatures") or [])]
            pubkey = bytes.fromhex(str(said.get("pubkey") or ""))
            if not pubkey:
                raise ValueError("this request names no public key, and a "
                                 "transaction nobody can trace to a key is not "
                                 "a transaction this node will finish")
            if hash160(pubkey) != b58check_decode(address)[1]:
                raise ValueError("that public key is not the key behind this "
                                 "account's "
                                 f"{chain.label.lower()} address, so finishing "
                                 "it would write a trade one wallet paid and "
                                 "another is named on")
            try:
                hex_ = fundinglib.assemble(unsigned, signatures, pubkey)
            except fundinglib.FundingError as exc:
                return JSONResponse({"detail": str(exc)}, status_code=400)
            _flights.note_committed(
                account.pubkey,
                spent=tuple((coin["txid"], coin["vout"])
                            for coin in unsigned.inputs[unsigned.signed_from:]),
                network=chain.network)
            return JSONResponse({
                "hex": hex_, "what": what, "chain": chain.network,
                "shop": row["txid"], "seller": offer["seller"],
                "cut": ({"bps": int((offer.get("cut") or {}).get("bps") or 0),
                         "sats": cut, "to": cut_to} if cut else {}),
                "order": {"swap": "sign", "swapv": swaplib.PROTOCOL,
                          "offer": offer["id"], "hex": hex_},
                "seal_to": node.hex(), "to": row["owner"],
                "stamp": apilib.stamp().hex()})
        except (swaplib.SwapError, fundinglib.FundingError,
                accountslib.AccountError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except HTTPException:
            raise
        except Exception as exc:
            return JSONResponse({"detail": f"the node could not do it: {exc}"},
                                status_code=409)

    @app.post("/account/talk")
    def account_talk(request: Request, payload: Any = Body(None)):
        """A page speaks to another node, as this account's own key.

        The account half of the `/node` door, and it exists for the reason
        the shop door exists: an inscribed page asked for a conversation and
        the wallet that would hold it has a key instead of a node. So the
        sealing happens in the browser -- messaging.js, `sealForProgram`, to
        a key the browser checked -- and what arrives here is ciphertext and
        a destination: this node builds the one transaction that carries it,
        never learns what is inside, and offers it for the account's
        signature. Replies are read back by the browser too, from
        `/account/messages`; this route keeps no conversation, because a
        node that remembers a page's chats remembers the wrong thing.

        `identity` says what this node's API speaks -- the stamp the sealing
        must carry and the one-message size -- and `ask` turns the page's
        `to` into the key to seal to. Testnet only, as every node-to-node
        message is (D-010), and metered on the account's message dial: the
        page's hourly count IS the account's, since a count kept in a tab
        resets with the tab and could only ever be theatre.
        """
        from .. import nodetalk
        from ..messaging import api as apilib
        account = _signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        op = str(said.get("op") or "")
        chain = _account_chain()
        try:
            if chain.is_mainnet:
                raise nodetalk.TalkError("node-to-node messages are testnet "
                                         "only, and this account lives on "
                                         "mainnet")
            if op == "identity":
                return JSONResponse({
                    "ok": True, "network": chain.network,
                    "stamp": apilib.stamp().hex(),
                    "maxbytes": apilib.MAX_API_PAYLOAD})
            to = nodetalk.parse_pubkey(said.get("to"))
            if op == "ask":
                return JSONResponse({"ok": True, "seal_to": to.hex(),
                                     "stamp": apilib.stamp().hex()})
            if op != "send":
                raise nodetalk.TalkError("op must be identity, ask or send")
            address = _account_address(account.pubkey, chain)
            if not address:
                raise ValueError("this account has no address on this chain yet")
            sealed = bytes.fromhex(str(said.get("sealed") or ""))
            if not sealed:
                raise ValueError("there is nothing to send")
            # Say it here rather than let the envelope header say it: a
            # ciphertext past a uint16 does not reach the class check, and
            # a page told "the node could not do it" learns nothing it can
            # act on. Nothing is built yet, so the refusal costs nothing.
            from ..encoding import MAX_CLASS_B_PAYLOAD
            if len(sealed) + 16 > MAX_CLASS_B_PAYLOAD:
                raise nodetalk.TalkError(
                    "a page's message is one transaction, and that is more "
                    f"than the {MAX_CLASS_B_PAYLOAD:,} bytes one can carry. "
                    "Send a reference to an inscription instead of the "
                    "thing itself.")
            # Type 6 rather than type 1, the header written HERE: the same
            # rule the shop's `send` and `/account/write` follow, so the
            # length of what fits is said once by the function that builds
            # it and a relayed page never prefixes its envelope twice.
            header = envelopelib.Header(type=envelopelib.TYPE_API,
                                        clen=len(sealed))
            outputs = _class_c_or_b(chain, address, header.encode() + sealed,
                                    _coin_pubkey(account.pubkey))
            with contextlib.closing(state.token_index(chain).open()) as db:
                unsigned = fundinglib.build(
                    db, chain.params, address, outputs,
                    rate=fees.MIN_FEE_PER_KB,
                    what="a page's message to another node",
                    exclude=_flights.spent_by(account.pubkey, chain.network),
                    extra=_flights.change_for(account.pubkey, chain.network))
            _quota(account, "message", len(sealed))
            offered = _offers.add(account.pubkey, chain.network, unsigned,
                                  unsigned.what)
            return JSONResponse({"ok": True, "offer": offered.id,
                                 "bytes": len(sealed), "to": to.hex(),
                                 "chain": chain.network,
                                 **unsigned.as_json()})
        except (nodetalk.TalkError, fundinglib.FundingError,
                accountslib.AccountError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except HTTPException:
            raise
        except Exception as exc:
            return JSONResponse({"detail": f"the node could not do it: {exc}"},
                                status_code=409)

    @app.get("/account/tokens")
    def account_tokens(request: Request):
        """What this account holds in tokens, on every chain it has an
        address on -- the same question `index.balances()` answers for
        the node's own wallet, asked about a different address."""
        account = _signed_in_account(request)
        out: list[dict[str, Any]] = []
        for chain in _account_chains():
            address = _account_address(account.pubkey, chain)
            if not address:
                continue
            try:
                index = state.token_index(chain)
                held = index.balances([address])
            except Exception:
                continue
            out.append({
                "network": chain.network, "label": chain.label,
                "mainnet": bool(chain.is_mainnet), "address": address,
                "tokens": [{
                    "property_id": row["property_id"], "name": row["name"],
                    "issuer": row["issuer"], "divisible": row["divisible"],
                    "balance": row["balance"], "display": row["display"],
                } for row in held],
            })
        return JSONResponse({"chains": out})

    @app.post("/account/token/send")
    def account_token_send(request: Request, payload: Any = Body(None)):
        """Offer to send a token. Nothing is broadcast here.

        A genuine Omni Simple Send (type 0), built the way `TokenSender`
        builds one -- NOT wrapped in AnyData, because it is not an arcade
        payload piggybacking on the chain, it IS the transaction the token
        engine reads a balance change out of. The recipient's dust output
        is what the engine's reference rule (`tx.determine_reference`)
        resolves the payment to; the change goes back to the sender, which
        a Class B payload's obfuscation requires either way.
        """
        account = _signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = _chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = _account_address(account.pubkey, chain)
        if not address:
            return JSONResponse(
                {"detail": f"this account has no {chain.label.lower()} "
                           f"address yet"}, status_code=400)
        try:
            index = state.token_index(chain)
            property_id = int(said.get("property_id", 0))
            prop = index.property(property_id)
            if prop is None:
                raise tokenlib.TokenError(f"there is no token {property_id}.")
            to = str(said.get("to", "")).strip()
            if taglib.looks_like_a_tag(to):
                to = _where_to_pay(taglib.normalise(to), chain)
            complaint = _check_address(to, mainnet=chain.is_mainnet)
            if complaint:
                raise ValueError(complaint)
            if to == address:
                raise ValueError("that is this account's own address")
            amount = parse_amount(str(said.get("amount", "")),
                                       prop["divisible"])
            held = index.balance(address, property_id)
            if amount > held:
                raise tokenlib.TokenError(
                    f"only {format_amount(held, prop['divisible'])} "
                    f"of {prop['name']} is here to send.")
            body = tokenlib.send_payload(property_id, amount)
            outputs = _class_c_or_b(chain, address, body,
                                    _coin_pubkey(account.pubkey, chain),
                                    wrap=False)
            # Last, so the reference rule finds it: it skips the first
            # output back to the sender as change and takes the last of
            # the rest, which is exactly the shape a Class B or Class C
            # payload plus this one output produces.
            outputs.append((sendermod.OUTPUT_VALUE, txbuild.p2pkh_script(to)))
            with contextlib.closing(index.open()) as db:
                unsigned = fundinglib.build(
                    db, chain.params, address, outputs,
                    rate=fees.MIN_FEE_PER_KB,
                    what=f"send {format_amount(amount, prop['divisible'])} "
                         f"{prop['name']} to {to}",
                    exclude=_flights.spent_by(account.pubkey, chain.network),
                    extra=_flights.change_for(account.pubkey, chain.network))
            _quota(account, "send")
        except (taglib.TagError, tokenlib.TokenError, fundinglib.FundingError,
                AmountError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except Exception as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        offer = _offers.add(account.pubkey, chain.network, unsigned,
                            unsigned.what)
        return JSONResponse({"offer": offer.id, "to": to,
                             "property_id": property_id, "name": prop["name"],
                             "chain": chain.network, **unsigned.as_json()})

    @app.post("/account/token/create")
    def account_token_create(request: Request, payload: Any = Body(None)):
        """Offer to create a token, as this account. Nothing is broadcast here.

        The same type 50 or 54 issuance the operator's own form builds, from
        the same function -- `issuance_payload` is byte-building and does not
        know, and must not know, who is asking. Two things differ, and both
        are who rather than what:

        * There is no sender field. The issuer is whoever owns the
          transaction's first input, and every input here is picked from this
          account's own address, so the token is the account's and this node
          never had the key that makes it so. A form that let somebody name a
          sender would be a form that lets them issue a token onto an address
          they did not open here.
        * It is not wrapped in `AnyData`, for the reason in `_class_c_or_b`:
          this IS the message the token engine reads a new property out of.
          Enveloped, it would be paid for, sit in a block, and credit nobody.

        A creation has no recipient output at all -- `TokenSender` passes
        `reference=None` for one and the engine ignores the field -- so what
        is offered is the payload outputs plus this account's change, and
        nothing else.

        Worth stating where the money is: an issuance with an icon goes Class
        B, because an inscription id is 64 characters and a Class C payload is
        76 bytes for the WHOLE issuance, name included. That is the marker
        output and a few sweepable dust outputs in the transaction the browser
        is about to be shown, not a fee, and the confirmation names them
        because they are outputs like any other.
        """
        account = _signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = _chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = _account_address(account.pubkey, chain)
        if not address:
            return JSONResponse(
                {"detail": f"this account has no {chain.label.lower()} "
                           f"address yet"}, status_code=400)
        name = str(said.get("name") or "")
        try:
            divisible = str(said.get("units") or "divisible") != "indivisible"
            managed = str(said.get("kind") or "fixed") == "managed"
            amount = None if managed else parse_amount(
                str(said.get("supply") or ""), divisible)
            complaint = _name_is_taken(name, chain)
            if complaint:
                raise tokenlib.TokenError(complaint)
            icon = str(said.get("icon") or "")
            if icon.strip() and not tokenlib.icon_in(icon):
                raise tokenlib.TokenError(
                    "an icon is an inscription on this chain: paste its "
                    "/content/ link or its id, not a picture from elsewhere.")
            body = tokenlib.issuance_payload(
                name=name, divisible=divisible, managed=managed, amount=amount,
                category=str(said.get("category") or ""),
                subcategory=str(said.get("subcategory") or ""),
                url=str(said.get("url") or ""),
                # The icon rides in `data` beside the description, because an
                # issuance has five strings and no sixth (tokens.details).
                data=tokenlib.data_with_icon(str(said.get("data") or ""), icon))
            outputs = _class_c_or_b(chain, address, body,
                                    _coin_pubkey(account.pubkey, chain),
                                    wrap=False)
            index = state.token_index(chain)
            with contextlib.closing(index.open()) as db:
                unsigned = fundinglib.build(
                    db, chain.params, address, outputs,
                    rate=fees.MIN_FEE_PER_KB,
                    what=f"create the token {name.strip()}",
                    exclude=_flights.spent_by(account.pubkey, chain.network),
                    extra=_flights.change_for(account.pubkey, chain.network))
            _quota(account, "issue", len(body))
        except (tokenlib.TokenError, fundinglib.FundingError, AmountError,
                ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        offer = _offers.add(account.pubkey, chain.network, unsigned,
                            unsigned.what)
        return JSONResponse({"offer": offer.id, "name": name.strip(),
                             "managed": managed, "chain": chain.network,
                             "class": ("B" if len(outputs) > 1 else "C"),
                             **unsigned.as_json()})

    @app.get("/me/backup", response_class=HTMLResponse)
    def my_backup(request: Request):
        """Backup, for an account: there is no wallet.dat, so there is no
        file to show. What there is instead is said plainly -- the twelve
        words are the whole account, and what lives only in this browser."""
        account = signed_in(request)
        if account is None:
            return RedirectResponse("/join", status_code=303)
        mine = state.vault().by_pubkey(account.pubkey) or {}
        return render(request, "my_backup.html", chain=_account_chain(),
                      my_name=mine.get("tag", ""),
                      # The chains where the coins are money, and the address
                      # this browser has to check its words against -- the
                      # words themselves never come here, and this page is the
                      # one place they are ever asked for (§1b).
                      real_chains=[{"network": one.network, "label": one.label,
                                    "version": one.params.pubkeyhash_version,
                                    "address": _account_address(
                                        account.pubkey, one)}
                                   for one in _account_chains()
                                   if one.is_mainnet],
                      real_on=_real_coins_open(account))

    @app.get("/me/wallet", response_class=HTMLResponse)
    def my_wallet(request: Request):
        """An account's Wallet: coins, one per chain, and the send form.

        The Coins tab of the three the operator's Wallet has. Tokens is
        `/me/wallet/tokens`; NFTs is `/me/nfts`, built first because
        the operator asked for it first -- the tab bar links all three.
        """
        if signed_in(request) is None:
            return RedirectResponse("/join", status_code=303)
        return render(request, "my_wallet.html", chain=_account_chain())

    @app.get("/me/wallet/tokens", response_class=HTMLResponse)
    def my_wallet_tokens(request: Request):
        """The Tokens tab: what this account's addresses hold, and a form
        to send some of it -- read the same way `/account/nfts` reads
        what an account holds in inscriptions."""
        if signed_in(request) is None:
            return RedirectResponse("/join", status_code=303)
        return render(request, "my_wallet_tokens.html", chain=_account_chain())

    @app.get("/me/nfts", response_class=HTMLResponse)
    def my_nfts(request: Request):
        """What an account holds, and the one button that sends one on."""
        if signed_in(request) is None:
            return RedirectResponse("/join", status_code=303)
        return render(request, "my_nfts.html", chain=_account_chain())

    @app.get("/me/runs", response_class=HTMLResponse)
    def my_runs(request: Request):
        """Every collection run this account has written down.

        `/inscriptions/collection` for one account instead of for the node.
        The ordering is the operator's -- unfinished first -- because the
        question this page gets asked is "what did I leave half-done", and a
        list that buries it under everything already paid for answers it
        slowly.
        """
        account = signed_in(request)
        if account is None:
            return RedirectResponse("/join", status_code=303)
        mine = [run for run in _runs.list(account=account.pubkey)]
        mine.sort(key=lambda run: (run["status"] == "done", -run["created"]))
        # The book keeps the network, which is what a run has to be to find its
        # chain again; what a person reads is the label. Keyed rather than
        # `_account_chain().label` because a run's chain is its own, and on a
        # node with two of them the one on this row is not necessarily the one
        # this page is about.
        labels = {one.network: one.label for one in _account_chains()}
        return render(request, "account_runs.html", chain=_account_chain(),
                      runs=[{"run": run["id"], "name": run["name"],
                             "items": run["items"], "sent": run["sent"],
                             "status": run["status"],
                             "refused": run["failed_pieces"],
                             "next": run["next"],
                             "chain": labels.get(run["network"], run["network"])}
                            for run in mine])

    @app.get("/me/run/{run_id}", response_class=HTMLResponse)
    def my_run(request: Request, run_id: str, page: int = 1):
        """One run, piece by piece -- the account's `/inscriptions/collection/{id}`.

        The same reading as `/account/run`, drawn instead of answered: same
        book, same one-piece-at-a-time buttons, nothing here that signs or
        broadcasts. What the wallet page's little panel cannot hold is here --
        every piece with its own status, the reason on the ones that were
        refused, what it costs, and which inscription each landed piece became.

        A run that is not this account's gets the same words as a run that does
        not exist. The POST routes can tell those two apart -- they have to, to
        be honest about what they refused -- but this page is on a public
        instance, where being told that an id names somebody's run is an answer
        about somebody else.
        """
        account = signed_in(request)
        if account is None:
            return RedirectResponse("/join", status_code=303)
        run = _runs.get(run_id)
        if run is None or run["account"] != account.pubkey.lower():
            state.flash("there is no run of that id", "err")
            return RedirectResponse("/me/runs", status_code=303)
        per_page = 100
        pages = max(1, -(-run["items"] // per_page))
        page = max(1, min(page, pages))
        pieces = _runs.pieces(run["id"], limit=per_page,
                              offset=(page - 1) * per_page)
        # The chain the run was written on, by name, as the operator's page
        # does: a run belongs to a chain era, and the pieces it paid for are on
        # that one whatever this instance runs now.
        chain = _chain_on(run["network"])
        numbers: dict[str, int] = {}
        try:
            index = state.token_index(chain)
            for piece in pieces:
                if piece["txid"]:
                    row = index.inscription(piece["txid"])
                    if row:
                        numbers[piece["txid"]] = row["number"]
        except Exception:
            numbers = {}             # the page still draws; a number is a link
        return render(request, "account_run.html",
                      run={**run, "refused": run["failed_pieces"]},
                      pieces=pieces, numbers=numbers, chain=chain,
                      page=page, pages=pages)

    @app.get("/account/feed")
    def account_feed(request: Request, before: str | None = None):
        """The feed, as an account sees it: the same posts, its own name.

        Read from the same store the wallet's own feed reads. Nothing here
        is private -- every post was public when it was mined -- so this
        differs from `/feed` only in what it says about who is looking.
        """
        account = signed_in(request)
        chain = _account_chain()
        rows, cursor, waiting = _feed_page(chain.network, before=before,
                                          sort="popular")
        shown = _shown(rows, chain.network, waiting)
        mine = ""
        if account is not None:
            mine = (state.vault().by_pubkey(account.pubkey) or {}).get("tag", "")
        return JSONResponse({
            "mine": mine,
            "cursor": cursor,
            "posts": [{
                "txid": item.txid,
                "text": item.text,
                "author": item.author,
                "when": item.block_time,
                "height": item.height,
                "likes": item.likes,
                "replies": len(item.replies),
                "tips": item.tips,
                # The running total, per chain, in sats -- held apart because
                # the units differ and adding them would be a lie.
                "tipped": item.tipped,
            } for item in shown],
        })

    @app.get("/account/messages")
    def account_messages(request: Request, after: int = 0, limit: int = 200):
        """Candidate payloads, for the browser to try its key against.

        The node cannot tell which of these belong to whom: an account's
        identity lives in its browser, and that is the whole arrangement.
        So it hands over what it has seen and the browser finds out by
        trying -- most will not open, and that costs a failed
        authentication rather than anything on the chain.

        Everything here is already public. These are the bytes as
        broadcast; the only thing a reader gains is the trouble of trying a
        key against them, which they could do by reading the chain
        themselves.
        """
        _signed_in_account(request)       # a seat, so this is not an open firehose
        limit = max(1, min(int(limit), 500))
        with state.store() as store:
            rows = store.candidates_for_others(after=int(after), limit=limit)
            newest = store.newest_candidate()
        return JSONResponse({
            "cursor": rows[-1]["cursor"] if rows else int(after),
            "newest": newest,
            "more": bool(rows) and rows[-1]["cursor"] < newest,
            "candidates": [{
                "cursor": row["cursor"], "txid": row["txid"],
                "height": row["height"], "when": row["block_time"],
                "from_address": row["sender_addr"],
                "type": row["msg_type"],
                "msg_id": (row["msg_id"] or b"").hex() or None,
                "countdown": row["countdown"],
                "payload": bytes(row["payload"]).hex(),
            } for row in rows],
        })

    @app.get("/account/who/{name}")
    def account_who(request: Request, name: str):
        """Where to write to somebody: their address and their published key.

        A @tag or an address, and the answer is what the CHAIN says --
        never what anybody typed into an address book. Somebody who has
        published no key cannot be written to, and this says so rather than
        letting a message be sealed to nothing.
        """
        _signed_in_account(request)
        chain = _account_chain()
        wanted = (name or "").strip().lstrip("@")
        address, tag = "", ""
        try:
            index = state.token_index(chain)
            if _looks_like_an_address(wanted):
                address = wanted
                tag = index.tag_of(address) or ""
            else:
                tag = taglib.validate(wanted)
                address = index.address_of(tag) or ""
        except (taglib.TagError, Exception) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        if not address:
            return JSONResponse(
                {"detail": f"nobody holds @{wanted} on this chain"},
                status_code=404)
        with state.store() as store:
            said = store.key_for(address)
        if said is None:
            return JSONResponse({
                "address": address, "tag": tag, "key": None,
                "detail": "they have not published a messaging key, so "
                          "there is nowhere to send it. Ask them to publish "
                          "their tag -- it is one button."}, status_code=404)
        return JSONResponse({"address": address, "tag": tag,
                             "key": bytes(said["pubkey"]).hex(),
                             "fingerprint": said["fingerprint"]})

    @app.post("/account/write")
    def account_write(request: Request, payload: Any = Body(None)):
        """Offer to send a message an account has already sealed.

        The sealing happened in the browser, to a key the browser looked
        up and can check. What arrives here is ciphertext and an address:
        the node builds the transaction that carries it and never learns
        what is inside or, beyond the address it is paying, who it is for.
        """
        account = _signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        chain = _account_chain()
        address = _account_address(account.pubkey, chain)
        if not address:
            return JSONResponse({"detail": "this account has no address yet"},
                                status_code=400)
        try:
            sealed = bytes.fromhex(str(said.get("sealed", "")))
            if not sealed:
                raise ValueError("there is nothing to send")
            to = str(said.get("to", "")).strip()
            complaint = _check_address(to, mainnet=chain.is_mainnet)
            if complaint:
                raise ValueError(complaint)
            # The envelope, as the scanner will read it back: a header
            # naming the type and the ciphertext length, then the bytes.
            header = envelopelib.Header(
                type=envelopelib.TYPE_SINGLE, clen=len(sealed))
            body = header.encode() + sealed
            outputs = _class_c_or_b(chain, address, body,
                                    _coin_pubkey(account.pubkey))
            # A message pays its recipient the dust that carries it, so the
            # transaction is also how they are told something arrived.
            outputs.append((sendermod.OUTPUT_VALUE, txbuild.p2pkh_script(to)))
            index = state.token_index(chain)
            with contextlib.closing(index.open()) as db:
                unsigned = fundinglib.build(
                    db, chain.params, address, outputs,
                    rate=fees.MIN_FEE_PER_KB, what=f"a message to {to}",
                    exclude=_flights.spent_by(account.pubkey, chain.network),
                    extra=_flights.change_for(account.pubkey, chain.network))
            _quota(account, "message", len(sealed))
        except (fundinglib.FundingError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        offer = _offers.add(account.pubkey, chain.network, unsigned,
                            unsigned.what)
        return JSONResponse({"offer": offer.id, "bytes": len(sealed),
                             "chain": chain.network,
                             **unsigned.as_json()})

    @app.post("/account/post")
    def account_post(request: Request, payload: Any = Body(None)):
        """Offer to say something on the feed, as this account.

        A post is not encrypted and never was: every row of the feed was
        readable by anybody with a node the moment it was mined. What
        changes for an account is only who pays for it and whose name is on
        it -- the byline is read from the chain, so nobody can post under a
        name they do not hold.
        """
        account = _signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        chain = _account_chain()
        address = _account_address(account.pubkey, chain)
        if not address:
            return JSONResponse({"detail": "this account has no address yet"},
                                status_code=400)
        try:
            text = str(said.get("text", "")).strip()
            if not text:
                raise ValueError("say something")
            # A name first, for the same reason the wallet's own feed asks
            # for one: everything in the feed has a person behind it, and a
            # byline that is an address is not a person (D-138).
            if not state.token_index(chain).tag_of(address):
                raise ValueError(
                    "claim your name first -- it is what your posts appear "
                    "under, and it is one button.")
            plan = group.plan(group.GroupPost(channel="", nickname="",
                                              text=text))
            if plan.transactions != 1:
                raise ValueError(
                    "that is too long for one transaction -- put a file in "
                    "it instead, or say less")
            index = state.token_index(chain)
            with contextlib.closing(index.open()) as db:
                unsigned = fundinglib.build(
                    db, chain.params, address,
                    _class_c_or_b(chain, address, plan.payloads[0],
                                  _coin_pubkey(account.pubkey)),
                    rate=fees.MIN_FEE_PER_KB, what="a post",
                    exclude=_flights.spent_by(account.pubkey, chain.network),
                    extra=_flights.change_for(account.pubkey, chain.network))
            _quota(account, "post", len(plan.payloads[0]))
        except (fundinglib.FundingError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        offer = _offers.add(account.pubkey, chain.network, unsigned, "a post")
        return JSONResponse({"offer": offer.id, "chain": chain.network,
                             **unsigned.as_json()})

    @app.post("/account/react")
    def account_react(request: Request, payload: Any = Body(None)):
        """Offer to like, reply to, share or tip a post, as this account.

        One route for all of them because they are one kind of thing on the
        chain: a note saying what was done and which post it was done to
        (D-138). A tip differs only by paying the author as well, which is
        what makes it one transaction rather than two.
        """
        account = _signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        chain = _account_chain()
        address = _account_address(account.pubkey, chain)
        if not address:
            return JSONResponse({"detail": "this account has no address yet"},
                                status_code=400)
        try:
            target = str(said.get("txid", "")).strip().lower()
            if len(target) != 64 or not all(c in "0123456789abcdef"
                                            for c in target):
                raise ValueError("that is not a transaction id")
            kind = int(said.get("kind", 0))
            if kind not in feedlib.KINDS:
                raise ValueError("that is not something that can be done")
            text = str(said.get("text", ""))
            note = feedlib.build(kind, target, text)

            outputs = _class_c_or_b(chain, address, note,
                                    _coin_pubkey(account.pubkey))
            paid = 0
            if kind == feedlib.TIP:
                # A tip pays the post's author in the same transaction that
                # says which post it was for, so nothing has to be
                # reconciled afterwards and nobody pays twice.
                amount = parse_amount(str(said.get("amount", "")), True)
                if amount <= 0:
                    raise ValueError("a tip of nothing is not a tip")
                _above_dust(amount, "tip")
                where = _feed_author_address(chain, target)
                if not where:
                    raise ValueError(
                        "there is nowhere to send it: whoever wrote that has "
                        "not published an address")
                outputs.append((amount, txbuild.p2pkh_script(where)))
                paid = amount

            index = state.token_index(chain)
            with contextlib.closing(index.open()) as db:
                unsigned = fundinglib.build(
                    db, chain.params, address, outputs,
                    rate=fees.MIN_FEE_PER_KB,
                    what=feedlib.NAMES.get(kind, "a reaction"),
                    exclude=_flights.spent_by(account.pubkey, chain.network),
                    extra=_flights.change_for(account.pubkey, chain.network))
            _quota(account, "react", len(note))
        except (fundinglib.FundingError, AmountError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        offer = _offers.add(account.pubkey, chain.network, unsigned,
                            unsigned.what)
        return JSONResponse({"offer": offer.id, "paid": paid,
                             "chain": chain.network,
                             **unsigned.as_json()})

    def _feed_author_address(chain, txid: str) -> str:
        """Who wrote the post a reaction is about, from this node's store."""
        row = _feed_thing(chain.network, txid)
        return (row or {}).get("author", "") if row is not None else ""

    def _announcement_offer(account, chain, address: str, said: dict, *,
                            tag: str, what: str, pfp: str = "",
                            bio: str = "", url: str = ""):
        """The offer that puts this account's own announcement on the chain.

        One build for the two routes that say something about a key -- the
        key itself, and what its holder says about themselves -- because
        they are one transaction: an announcement this node could never have
        signed, handed to a browser that signs it and hands it back (§5).
        What differs is only which fields the announcement carries and what
        the offer calls itself in the browser.

        The addresses are put in here rather than believed out there. The
        identity address goes in as its hash160, so a reader files the key
        under the address the account actually hands out rather than under
        whichever one funded the transaction; the other chain's goes in the
        same way, so anybody who finds this account by name can pay it on
        either chain without asking for anything (D-032). Its version byte
        is the OTHER chain's and is put back by whoever reads it -- what
        travels is twenty bytes with no chain in them.
        """
        try:
            key = bytes.fromhex(str(said.get("key", "")))
            if len(key) != 32:
                raise ValueError("a messaging key is 32 bytes")
            _, our_hash = b58check_decode(address)
            other = b""
            for one in _account_chains():
                if one.network == chain.network:
                    continue
                elsewhere = _account_address(account.pubkey, one)
                if elsewhere:
                    _, other = b58check_decode(elsewhere)
                    break
            body = envelopelib.build_key_announcement(
                key, hash160=our_hash, tag=tag, other_hash160=other,
                pfp=pfp, bio=bio, url=url)
            index = state.token_index(chain)
            with contextlib.closing(index.open()) as db:
                unsigned = fundinglib.build(
                    db, chain.params, address,
                    _class_c_or_b(chain, address, body,
                                  _coin_pubkey(account.pubkey)),
                    rate=fees.MIN_FEE_PER_KB, what=what,
                    exclude=_flights.spent_by(account.pubkey, chain.network),
                    extra=_flights.change_for(account.pubkey, chain.network))
            _quota(account, "name", len(body))
        except (fundinglib.FundingError, envelopelib.EnvelopeError,
                ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        offer = _offers.add(account.pubkey, chain.network, unsigned,
                            unsigned.what)
        return JSONResponse({"offer": offer.id, "chain": chain.network,
                             **unsigned.as_json()})

    @app.post("/account/announce")
    def account_announce(request: Request, payload: Any = Body(None)):
        """Offer to publish this account's messaging key on the chain.

        Nobody can write to somebody who has not published a key, so this
        is what makes an account reachable. The key is made in the browser
        and arrives here as bytes to put in a transaction -- the node never
        derives it and could not.
        """
        account = _signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        chain = _account_chain()
        address = _account_address(account.pubkey, chain)
        if not address:
            return JSONResponse({"detail": "this account has no address yet"},
                                status_code=400)
        return _announcement_offer(
            account, chain, address, said,
            tag=str(said.get("tag", "")).strip().lstrip("@"),
            what="publish your messaging key")

    @app.post("/account/profile")
    def account_profile(request: Request, payload: Any = Body(None)):
        """Offer to publish what this account says about itself.

        The operator's `/profile/picture` and `/profile/about` write into
        this installation's settings and announce from the node's own key.
        An account has neither, so its words travel with the request and go
        straight into the announcement -- which is what every other wallet
        reads, and what this node keeps as what THEY said rather than as a
        fact about anybody (D-145). Publishing a profile publishes the key
        with it, so an account that never announced pays one fee, not two.

        Two things the operator's routes are not asked to get right:

        * **The tag comes off the chain, never out of the request.** An
          announcement naming a name the account does not hold is a lie
          signed by the wrong person, and changing a face is not changing a
          name (D-076).
        * **A field left out is filled from what is already published.** An
          announcement replaces every field rather than merging into the
          last one, so without this a new picture would quietly take the
          bio down with it.
        """
        account = _signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        chain = _account_chain()
        address = _account_address(account.pubkey, chain)
        if not address:
            return JSONResponse({"detail": "this account has no address yet"},
                                status_code=400)
        try:
            index = state.token_index(chain)
            try:
                tag = index.tag_of(address) or ""
            except Exception:
                tag = ""                      # a node still catching up
            if not tag:
                # Claimed here and not in a block yet. Reading the chain now
                # would announce no name at all, and the two statements would
                # disagree until somebody published again (D-076).
                tag = (state.vault().by_pubkey(account.pubkey) or {}
                       ).get("tag") or ""
            now = _profile_of(address)
            if "pfp" in said:
                face = inscriptionlib.inscription_in(str(said["pfp"] or ""))
                if said.get("pfp"):
                    if not face:
                        raise ValueError(
                            "a profile picture is an inscription on one of "
                            "these chains: give its id.")
                    owned = False
                    for one in _account_chains():
                        where = _account_address(account.pubkey, one)
                        try:
                            row = state.token_index(one).inscription(face)
                        except Exception:
                            row = None
                        if where and row is not None and row["owner"] == where:
                            owned = True
                            break
                    if not owned:
                        raise ValueError(
                            "that piece is not yours to wear. A profile "
                            "picture is a piece you hold, and it is drawn "
                            "only while the chain says you hold it.")
            else:
                face = now["pfp"]
            text = " ".join(str(said.get("bio", now["bio"]) or "").split())
            if len(text.encode()) > envelopelib.MAX_ANNOUNCE_BIO:
                raise ValueError(
                    f"a bio is at most {envelopelib.MAX_ANNOUNCE_BIO} "
                    f"characters; this one is {len(text.encode())}")
            link = str(said.get("url", now["url"]) or "").strip()
            if link and not link.startswith(("https://", "http://")):
                raise ValueError("a link starts with https:// -- it is shown "
                                 "on your profile as words, never as somewhere "
                                 "to click")
            if len(link.encode()) > envelopelib.MAX_ANNOUNCE_URL:
                raise ValueError(
                    f"a link is at most {envelopelib.MAX_ANNOUNCE_URL} "
                    f"characters")
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return _announcement_offer(account, chain, address, said, tag=tag,
                                   what="publish your profile",
                                   pfp=face, bio=text, url=link)

    @app.post("/account/address")
    def account_address(request: Request, payload: Any = Body(None)):
        """Tell the node which address to watch for this account.

        The browser derives it; the node is told. It is checked rather than
        believed -- the version byte has to be this chain's, and the shape
        has to decode -- but it is not DERIVED here, because the node has
        no key and the only machine that can say is the one that made it.
        """
        account = _signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        address = str(said.get("address", "")).strip()
        try:
            chain = _chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        complaint = _check_address(address, mainnet=chain.is_mainnet)
        if complaint:
            return JSONResponse({"detail": complaint}, status_code=400)
        state.set_setting(_address_key(account.pubkey, chain), address)
        coin = str(said.get("coin_pubkey", "")).strip().lower()
        if coin:
            # The coin key is per chain too: a Class B payload puts the
            # SENDER's key in every output, and the sender on mainnet is a
            # different key derived from the same words.
            state.set_setting(_coinkey_key(account.pubkey, chain), coin)
        try:
            _watch(address, why=f"account {account.pubkey[:12]}", chain=chain)
        except Exception as exc:
            return JSONResponse({"detail": f"the index is not ready: {exc}"},
                                status_code=503)
        return JSONResponse({"address": address, "chain": chain.network})

    @app.post("/account/mainnet")
    def account_mainnet(request: Request, payload: Any = Body(None)):
        """Switch this account's real coins on, or report why it did not.

        The twelve words are NOT part of this request and never will be: a
        node that could check them is a node that knows them, which is the
        failure mode this whole side of the application is built to avoid
        (§2). So the browser derives this account's mainnet coin key out of
        the words as they were typed and compares it with the key already in
        use -- which is a check that can only pass if the words are right --
        and sends the answer and nothing else.

        What that does and does not buy is said here rather than implied by
        a padlock: it stops the person who never wrote the words down from
        moving money they could never recover, because the only way through
        is to type the words and see them work. It does not stop a browser
        that lies about the answer, and no design can, because the machine
        holding the keys is the machine being asked. The words stay the
        account's own backup; this is a speed bump with a sentence on it.
        """
        account = _signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        if not said.get("words_match"):
            return JSONResponse(
                {"detail": "those are not your words, so nothing is switched "
                           "on. Nothing was spent and nothing was changed -- "
                           "check the order and the spelling and try again."},
                status_code=400)
        state.set_setting(f"mainnet:{account.pubkey}", "yes")
        return JSONResponse({"mainnet": True,
                             "chains": [one.network for one
                                        in _account_chains()
                                        if one.is_mainnet]})

    @app.post("/account/claim")
    def account_claim(request: Request, payload: Any = Body(None)):
        """Offer to claim a @tag. Nothing is broadcast here.

        First claim wins, in chain order, so this can only say the name is
        free NOW -- and says exactly that rather than promising it.
        """
        account = _signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        chain = _account_chain()
        address = _account_address(account.pubkey, chain)
        if not address:
            return JSONResponse(
                {"detail": "this account has no address yet"}, status_code=400)
        try:
            # The name they signed up with, unless they asked for another.
            # Nobody should have to type it twice, and the one on file is
            # the one the rest of the page is already calling them.
            asked = str(said.get("tag", "")).strip().lstrip("@")
            if not asked:
                mine = state.vault().by_pubkey(account.pubkey)
                asked = (mine or {}).get("tag", "")
            wanted = taglib.validate(asked)
            index = state.token_index(chain)
            holder = index.address_of(wanted)
            if holder and holder != address:
                raise taglib.TagError(f"@{wanted} is taken.")
            outputs = _class_c_or_b(chain, address, taglib.encode(wanted),
                                    _coin_pubkey(account.pubkey))
            with contextlib.closing(index.open()) as db:
                unsigned = fundinglib.build(
                    db, chain.params, address, outputs,
                    rate=fees.MIN_FEE_PER_KB, what=f"claim @{wanted}",
                    exclude=_flights.spent_by(account.pubkey, chain.network),
                    extra=_flights.change_for(account.pubkey, chain.network))
            _quota(account, "name")
        except (taglib.TagError, fundinglib.FundingError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        offer = _offers.add(account.pubkey, chain.network, unsigned,
                            f"claim @{wanted}")
        return JSONResponse({"offer": offer.id, "chain": chain.network,
                             **unsigned.as_json()})

    @app.post("/account/send")
    def account_send(request: Request, payload: Any = Body(None)):
        """Offer to send coins. Nothing is broadcast here.

        The same handshake as a claim, carrying money instead of a name --
        which is the point of there being one handshake. What is different
        is only what the node has to say out loud before somebody signs:
        who is being paid, how much, and what it costs.
        """
        account = _signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        try:
            chain = _chain_asked(said)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        address = _account_address(account.pubkey, chain)
        if not address:
            return JSONResponse(
                {"detail": f"this account has no {chain.label.lower()} "
                           f"address yet"}, status_code=400)
        try:
            to = str(said.get("to", "")).strip()
            # A @tag is a name for an address, so it is resolved here and
            # the ANSWER is shown: somebody paying @robin should see the
            # address their coins are going to before they sign.
            if to.startswith("@") or (to and not _looks_like_an_address(to)):
                wanted = taglib.validate(to.lstrip("@"))
                to = _where_to_pay(wanted, chain)
            complaint = _check_address(to, mainnet=chain.is_mainnet)
            if complaint:
                raise ValueError(complaint)
            if to == address:
                raise ValueError("that is this account's own address")
            amount = parse_amount(str(said.get("amount", "")), True)
            if amount <= 0:
                raise ValueError("a payment of nothing is not a payment")
            _above_dust(amount, "payment")
            index = state.token_index(chain)
            with contextlib.closing(index.open()) as db:
                unsigned = fundinglib.build(
                    db, chain.params, address,
                    [(amount, txbuild.p2pkh_script(to))],
                    rate=fees.MIN_FEE_PER_KB,
                    what=(f"send {format_amount(amount, True)} "
                          f"{chain.label.lower()} to {to}"),
                    exclude=_flights.spent_by(account.pubkey, chain.network),
                    extra=_flights.change_for(account.pubkey, chain.network))
            _quota(account, "send")
        except (taglib.TagError, fundinglib.FundingError, AmountError,
                ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        offer = _offers.add(account.pubkey, chain.network, unsigned,
                            unsigned.what)
        return JSONResponse({"offer": offer.id, "to": to, "amount": amount,
                             "chain": chain.network, "label": chain.label,
                             **unsigned.as_json()})

    def _where_to_pay(tag: str, chain) -> str:
        """The address a @tag points at on the chain being paid.

        Tags are claimed on ONE chain -- the messaging chain, as they
        always have been -- so a mainnet payment to @robin cannot simply
        look the tag up: there is no claim there to find. What there is, is
        the other address the holder PUBLISHED with their key, which is the
        whole reason that field exists: "anybody who searches for @you can
        pay you on either chain" (D-032).

        So the tag is resolved where tags live, and the answer is then
        translated to the chain being paid. A holder who has not published
        one is not payable there, and this says so rather than paying an
        address on the wrong chain -- which on a real chain is coins gone.
        """
        home = _account_chain()
        found = state.token_index(home).address_of(tag)
        if not found:
            raise ValueError(f"nobody holds @{tag} on this chain")
        if chain.network == home.network:
            return found
        with state.store() as store:
            said = store.key_for(found)
        other = str((said["other_address"] if said is not None
                     and "other_address" in said.keys() else "") or "")
        if not other:
            raise ValueError(
                f"@{tag} has not published a {chain.label.lower()} address, "
                f"so there is nowhere on {chain.label.lower()} to pay them. "
                f"Ask them for an address.")
        return other

    def _looks_like_an_address(text: str) -> bool:
        """Address or name? Decided by shape, and only to choose which
        complaint to make -- both are checked properly afterwards."""
        try:
            b58check_decode(text)
            return True
        except Exception:
            return False

    @app.post("/account/sign")
    def account_sign(request: Request, payload: Any = Body(None)):
        """Take the signatures, check what they make, and broadcast it."""
        account = _signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        # This account's own lane (docs/multi-user.md §3). Claimed BEFORE the
        # offer is taken, not after: an offer is single-use, so a refusal that
        # consumed one would leave somebody building the whole transaction
        # again just to send it. It covers the broadcast and the note that
        # follows, not the build that came before -- a lane held across two
        # requests could be wedged shut by a tab somebody closed, and a wallet
        # that cannot spend is the worse of those two failures.
        lane = f"account {account.pubkey}"
        if not state.begin_send(lane):
            return JSONResponse(
                {"detail": "one of your transactions is still going. Wait for "
                           "it to be broadcast and try again -- signing two at "
                           "once from one wallet spends the same coin twice."},
                status_code=409)
        try:
            # Real coins are refused HERE rather than in the eleven routes that
            # build an offer, so that no route can forget to ask -- and looked
            # up without consuming, for the same reason the lane is claimed
            # before the offer is taken: a refusal that ate an offer sends the
            # browser off to build the whole transaction again, and this one is
            # followed by a trip to the Backup page and back.
            offered = str(said.get("offer", ""))
            for pending in _offers.waiting(account.pubkey):
                if pending.id != offered:
                    continue
                try:
                    _real_coins_gate(account, _chain_on(pending.network))
                except ValueError as exc:
                    return JSONResponse({"detail": str(exc)}, status_code=400)
                break
            try:
                offer = _offers.take(offered, account.pubkey)
                signatures = [str(x) for x in (said.get("signatures") or [])]
                pubkey = bytes.fromhex(str(said.get("pubkey", "")))
                signed = fundinglib.assemble(offer.unsigned, signatures, pubkey)
            except (accountlib.OfferError, fundinglib.FundingError,
                    ValueError) as exc:
                return JSONResponse({"detail": str(exc)}, status_code=400)

            chain = _chain_on(offer.network)
            try:
                with chain.rpc() as rpc:
                    # What the node offered is what the node checks. The
                    # decoded transaction has to spend the coins it chose and
                    # pay the outputs it built -- a browser cannot talk it into
                    # broadcasting anything else.
                    decoded = rpc.call("decoderawtransaction", signed)
                    _same_as_offered(decoded, offer.unsigned)
                    txid = rpc.call("sendrawtransaction", signed)
            except ValueError as exc:
                return JSONResponse({"detail": str(exc)}, status_code=400)
            except Exception as exc:
                return JSONResponse({"detail": f"the node refused it: {exc}"},
                                    status_code=409)
            # Remembered until the index reads it, so the next transaction
            # this account builds does not offer the coin this one just spent
            # or miss the change it just made.
            _flights.add(account.pubkey, txid, offer.unsigned,
                         _account_address(account.pubkey, chain),
                         network=chain.network)

            # Somewhere to write the txid down, if the route that built this
            # had somewhere. A run's next piece is chosen from what its book
            # believes is already on the chain, so a piece that went out and
            # was not recorded would be inscribed twice -- which money cannot
            # undo and a second attempt cannot fix. So this one is not
            # swallowed the way the note below is: the transaction is out
            # either way, and the only honest answer is to say so with the
            # txid in it and let the run be read before another piece is asked
            # for.
            if offer.done is not None:
                try:
                    offer.done(txid)
                except Exception as exc:
                    return JSONResponse(
                        {"detail": f"it went out ({txid}) but this node could "
                                   f"not write it down: {exc}. Read the run "
                                   "before asking for another piece."},
                        status_code=500)

            # A claim is worth remembering against the account: the page can
            # then say "on its way" rather than "no name" for the minutes
            # between the broadcast and the block.
            if offer.what.startswith("claim @"):
                try:
                    state.vault().note_claim(
                        offer.what[len("claim @"):], txid)
                except Exception:
                    pass                   # a note is not worth failing on
            state.bump_generation()
            return JSONResponse({"txid": txid, "what": offer.what})
        finally:
            state.end_send(lane)

    def _class_c_or_b(chain, address: str, raw: bytes,
                      coin_pubkey: bytes = b"", wrap: bool = True):
        """The outputs that carry a payload, in whichever class fits.

        **It wraps in `AnyData` by default**, and that is the point of it
        being one function. Raw, the `arcm` magic is read by the token
        engine as an Omni header -- version "ar", type "cm" = 25453 -- and
        an unknown message type does not get ignored, it STOPS the ledger
        index and says balances can no longer be trusted. Three callers
        wrapped and two did not; a test caught it, and the fix is that no
        caller can.

        `wrap=False` is for a payload that is ALREADY a genuine Omni
        message -- a token send, built the same way `TokenSender` builds
        one. Wrapping that in AnyData too would turn a real type-0 Simple
        Send into an opaque type-200 blob the token engine cannot credit
        to anybody; it would be paid for and invisible, the same failure
        `inscribe.py`'s own docstring records for inscriptions.

        Class C where it fits: one OP_RETURN, no dust. Class B otherwise,
        which is a marker output plus obfuscated bare-multisig outputs --
        the same choice `TokenSender` makes, made here because that one
        funds through the node's wallet and an account cannot.

        Class B needs the sender's own public key in every output, so the
        dust stays spendable by them rather than being burned. The node
        does not have it and cannot derive it: the browser sends it at
        signup and it is kept beside the address.
        """
        from ..encoding import (
            MAX_CLASS_B_PAYLOAD, encode_class_b, encode_class_c,
            max_class_c_payload,
        )
        from ..txbuild import multisig_script, op_return_script, p2pkh_script

        payload = P.AnyData(data=raw).encode() if wrap else raw
        if len(payload) <= max_class_c_payload():
            return [(0, op_return_script(encode_class_c(payload)))]
        if len(payload) > MAX_CLASS_B_PAYLOAD:
            raise fundinglib.FundingError(
                f"that is {len(payload)} bytes, and one transaction carries "
                f"at most {MAX_CLASS_B_PAYLOAD}. Longer messages go as a "
                f"chain of transactions, which accounts cannot do yet "
                f"(docs/multi-user.md §9).")
        if not coin_pubkey:
            raise fundinglib.FundingError(
                "this account has not told the node its public key, so a "
                "Class B payload cannot be built. Sign in again.")
        outputs = [(sendermod.OUTPUT_VALUE, p2pkh_script(chain.params.marker))]
        for group in encode_class_b(address, coin_pubkey, payload):
            outputs.append((sendermod.OUTPUT_VALUE,
                            multisig_script(list(group.keys), group.required)))
        return outputs

    def _coin_pubkey(pubkey: str, chain=None) -> bytes:
        """The account's own coin key on a chain, as the browser said."""
        said = str(state.setting(
            _coinkey_key(pubkey, chain or _account_chain()), "") or "")
        try:
            return bytes.fromhex(said)
        except ValueError:
            return b""

    def _same_as_offered(decoded: dict, unsigned) -> None:
        """Refuse anything that is not the transaction that was offered."""
        spent = {(vin.get("txid"), int(vin.get("vout", -1)))
                 for vin in decoded.get("vin", [])}
        wanted = {(coin["txid"], coin["vout"]) for coin in unsigned.inputs}
        if spent != wanted:
            raise ValueError("that transaction does not spend what was offered")
        outs = [(int(round(float(v.get("value", 0)) * 100_000_000),),
                 (v.get("scriptPubKey") or {}).get("hex", ""))
                for v in decoded.get("vout", [])]
        offered = [(value, script.hex()) for value, script in unsigned.outputs]
        if outs != offered:
            raise ValueError("that transaction does not pay what was offered")

    @app.post("/auth/password")
    def auth_password(request: Request, payload: Any = Body(None)):
        """Sign in with a username and a password.

        This is the operator's door, and it is the one place in this
        application where a password is checked. The rule it appears to
        break -- no password on the server -- is about other people's keys:
        a node holding a hash of the password that guards ITS OWN pages
        creates nothing worth stealing that the machine does not already
        hold, because the wallet is on it either way (D-154).
        """
        said = payload if isinstance(payload, dict) else {}
        try:
            token = state.credentials().open_session(
                str(said.get("username", "")), str(said.get("password", "")),
                ip=(request.client.host if request.client else ""))
        except accountslib.AccountError as refused:
            return JSONResponse({"detail": str(refused)}, status_code=403)
        account = state.account_for(token)
        answer = JSONResponse({"pubkey": account.pubkey,
                               "operator": account.pubkey.lower() == state.operator,
                               "tag": ""})
        answer.set_cookie(
            SESSION_COOKIE, token,
            max_age=accountslib.SESSION_DAYS * 86400,
            httponly=True, samesite="strict", secure=_over_https(request))
        # The operator's password IS the admin password (arcade/admin.py): having
        # just proved it, the admin panel opens too, without asking twice.
        if account.pubkey.lower() == state.operator:
            answer.set_cookie(ADMIN_COOKIE, state.admin_sessions().open(state.operator),
                              max_age=adminlib.ADMIN_HOURS * 3600, httponly=True,
                              samesite="strict", secure=_over_https(request), path="/admin")
        return answer

    @app.post("/auth/set-password")
    def auth_set_password(request: Request, payload: Any = Body(None)):
        """Choose the username and password this node answers to.

        From the machine itself only, for the same reason claiming it is:
        the whole security of it is that setting it requires already being
        where the node is. Changing it later is the same act, from the same
        place, or signed in as the operator.
        """
        signed_in_as = signed_in(request)
        outside = _from_outside(request)
        already = state.operator
        allowed = (not outside) or (
            already and signed_in_as is not None
            and signed_in_as.pubkey.lower() == already)
        if not allowed:
            return JSONResponse(
                {"detail": "this is set from the machine the node runs on, "
                           "or by whoever already holds it"}, status_code=403)
        said = payload if isinstance(payload, dict) else {}
        creds = state.credentials()
        register = state.accounts()
        # The account this name opens. An existing operator keeps theirs, so
        # changing a password does not change who the node belongs to; a
        # node with none gets an identifier made for it -- not a coin key
        # and not a key anybody signs with, just the id a session names.
        pubkey = already or secrets.token_bytes(32).hex()
        try:
            name = creds.set(str(said.get("username", "")),
                             str(said.get("password", "")), pubkey)
        except accountslib.AccountError as refused:
            return JSONResponse({"detail": str(refused)}, status_code=400)
        if not already:
            register.join(pubkey)
            state.claim_operator(pubkey)
        return JSONResponse({"username": name, "operator": pubkey})

    @app.get("/auth/door")
    def auth_door(request: Request):
        """What the sign-in page should offer, without saying who anybody is."""
        creds = state.credentials()
        return JSONResponse({
            "password": creds.anybody(),
            # Only from the machine itself, and only while there is nothing
            # set: a page that offers this to the internet is a page that
            # offers the node away.
            "settable": not _from_outside(request) and not creds.anybody(),
            "min_password": accountslib.MIN_PASSWORD,
        })

    # --- the admin panel (2026-09-25) --------------------------------
    #
    # One place for everything a node's operator does. At the node machine it
    # needs nothing; from outside it needs the operator account AND the admin
    # password (arcade/admin.py). Every write also needs the X-Arcade-Admin
    # header and, when the browser says where it came from, this very origin.

    def _admin_check(request: Request, write: bool = False) -> dict:
        """Who is using the panel, or an HTTPException saying what is missing."""
        local = not (state.public or _from_outside(request))
        if write:
            if request.headers.get(ADMIN_HEADER) != "1":
                raise HTTPException(403, "admin writes come from the admin page")
            origin = request.headers.get("origin")
            if origin:
                host = request.headers.get("host", "")
                if urllib.parse.urlsplit(origin).netloc != host:
                    raise HTTPException(403, "that request came from another site")
        if local:
            return {"local": True}
        if not _is_operator(request):
            raise HTTPException(403, "sign in as this node's operator first")
        if not state.admin_sessions().valid(request.cookies.get(ADMIN_COOKIE, ""),
                                            state.operator):
            raise HTTPException(401, "the admin password, please")
        return {"local": False}

    def _admin_password_ok(password: str, request: Request) -> bool:
        creds = state.credentials()
        name = adminlib.username_for(creds, state.operator)
        if not name:
            return False
        try:
            creds.check(name, password,
                        ip=(request.headers.get("cf-connecting-ip")
                            or (request.client.host if request.client else "")))
            return True
        except accountslib.AccountError:
            return False

    @app.get("/admin", response_class=HTMLResponse)
    def admin_page(request: Request):
        try:
            who = _admin_check(request)
        except HTTPException as exc:
            if exc.status_code == 401:
                return RedirectResponse("/admin/login", status_code=303)
            return render(request, "admin_login.html", reason=str(exc.detail),
                          signed_in=signed_in(request) is not None, need_account=True)
        return render(request, "admin.html", local=who["local"])

    @app.get("/admin/login", response_class=HTMLResponse)
    def admin_login_page(request: Request):
        return render(request, "admin_login.html", reason="",
                      signed_in=signed_in(request) is not None,
                      need_account=not _is_operator(request))

    @app.post("/admin/login")
    def admin_login(request: Request, payload: Any = Body(None)):
        if not _is_operator(request):
            return JSONResponse({"detail": "sign in as this node's operator first"},
                                status_code=403)
        said = payload if isinstance(payload, dict) else {}
        if not _admin_password_ok(str(said.get("password", "")), request):
            return JSONResponse({"detail": "that is not the admin password (or too "
                                           "many tries -- wait a few minutes)"},
                                status_code=403)
        token = state.admin_sessions().open(state.operator)
        answer = JSONResponse({"ok": True})
        answer.set_cookie(ADMIN_COOKIE, token, max_age=adminlib.ADMIN_HOURS * 3600,
                          httponly=True, samesite="strict", secure=_over_https(request),
                          path="/admin")
        return answer

    @app.post("/admin/logout")
    def admin_logout(request: Request):
        state.admin_sessions().close(request.cookies.get(ADMIN_COOKIE, ""))
        answer = JSONResponse({"ok": True})
        answer.delete_cookie(ADMIN_COOKIE, path="/admin")
        return answer

    def _admin_wallets() -> list[dict]:
        out = []
        for which, ctx in (("messaging", state.messaging), ("ledger", state.ledger)):
            row = {"which": which, "label": ctx.label, "network": ctx.network}
            try:
                with ctx.rpc() as rpc:
                    got = walletlib.balance(rpc)
                row.update({k: str(v) for k, v in got.items()})
            except Exception as exc:                    # noqa: BLE001 -- node away
                row["error"] = str(exc)[:200]
            out.append(row)
        return out

    def _admin_tag(pubkey: str) -> str:
        try:
            chain = _account_chain()
            address = _account_address(pubkey, chain)
            return (state.token_index(chain).tag_of(address) or "") if address else ""
        except Exception:                               # noqa: BLE001
            return ""

    @app.get("/admin/api/state")
    def admin_state(request: Request):
        who = _admin_check(request)
        register = state.accounts()
        caps = accountslib.limits(state.settings())
        operator = state.operator
        push = state.push() if hasattr(state, "push") else None
        seated = register.seated()
        return JSONResponse({
            "local": who["local"],
            "operator": {"pubkey": operator, "tag": _admin_tag(operator) if operator else "",
                         "password_set": bool(operator and adminlib.username_for(
                             state.credentials(), operator))},
            "seats": {"total": register.seats, "taken": len(seated),
                      "free": register.free()},
            "accounts": [{"pubkey": a.pubkey, "tag": _admin_tag(a.pubkey),
                          "created": a.created, "seen": a.seen,
                          "operator": a.pubkey.lower() == operator}
                         for a in seated],
            "quotas": {"hour": caps["hour"], "bytes": caps["bytes"],
                       "labels": {k: accountslib.LABELS.get(k, k) for k in caps["hour"]}},
            "wallets": _admin_wallets(),
            "faucet": {"topoff": bool(state.setting("faucet_topoff", True)),
                       "mine": bool(state.setting("faucet_mine", True)),
                       "gift": int(state.setting("faucet", faucetlib.GIFT)),
                       "daily": state.setting("faucet_daily"),
                       "real_coins": bool(state.setting("faucet_real_coins", False))},
            "moderation": state.setting("moderation") or {},
            "push": {"available": push is not None,
                     "devices": (len(push.subscribed()) if push is not None else 0)},
            "selling": {"auto_update": bool(state.setting("auto_update", True)),
                        "auto_sell": bool(state.setting("auto_sell", True)),
                        "auto_fill": bool(state.setting("auto_fill", True)),
                        "trade_cut": state.setting("trade_cut")},
            "node": {"version": state.running_version,
                     "public_hosts": list(state.public_hosts)},
        }, headers={"Cache-Control": "no-store"})

    @app.post("/admin/api/seats")
    def admin_seats(request: Request, payload: Any = Body(None)):
        _admin_check(request, write=True)
        said = payload if isinstance(payload, dict) else {}
        try:
            seats = int(said.get("seats"))
            if not 1 <= seats <= 10_000:
                raise ValueError
        except (TypeError, ValueError):
            return JSONResponse({"detail": "a number of seats from 1 to 10,000"},
                                status_code=400)
        state.set_setting("seats", seats)
        state.accounts().seats = seats           # the open register, not only the next one
        return JSONResponse({"ok": True, "seats": seats})

    @app.post("/admin/api/release")
    def admin_release(request: Request, payload: Any = Body(None)):
        _admin_check(request, write=True)
        pubkey = str((payload or {}).get("pubkey", "")).strip().lower()
        if pubkey == state.operator:
            return JSONResponse({"detail": "that is the operator's own seat"},
                                status_code=400)
        state.accounts().release(pubkey)
        return JSONResponse({"ok": True})

    @app.post("/admin/api/quotas")
    def admin_quotas(request: Request, payload: Any = Body(None)):
        _admin_check(request, write=True)
        said = payload if isinstance(payload, dict) else {}
        numbers = {}
        try:
            for kind, value in (said.get("hour") or {}).items():
                if kind not in accountslib.PER_HOUR:
                    raise ValueError(f"no such allowance: {kind}")
                numbers[f"quota:{kind}"] = _count(str(value), kind, accountslib.CEILING)
            if "bytes" in said:
                numbers["quota:bytes"] = _count(str(said["bytes"]), "bytes a day",
                                                accountslib.BYTE_CEILING)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        for key, value in numbers.items():
            state.set_setting(key, value)
        return JSONResponse({"ok": True})

    @app.post("/admin/api/settings")
    def admin_settings(request: Request, payload: Any = Body(None)):
        """The switches and numbers: faucet, screening, selling, updates."""
        _admin_check(request, write=True)
        said = payload if isinstance(payload, dict) else {}
        try:
            if "faucet_gift" in said:
                gift = walletlib.parse_amount(str(said["faucet_gift"]))
                state.set_setting("faucet", gift)
            if "faucet_daily" in said:
                text = str(said["faucet_daily"]).strip()
                state.set_setting("faucet_daily",
                                  None if text in ("", "none") else walletlib.parse_amount(text))
            for flag in ("auto_update", "auto_sell", "auto_fill", "faucet_topoff", "faucet_mine"):
                if flag in said:
                    state.set_setting(flag, bool(said[flag]))
            if "moderation" in said:
                mod = said["moderation"] or {}
                if mod.get("url") and mod.get("model"):
                    state.set_setting("moderation", {"url": str(mod["url"]).strip(),
                                                     "model": str(mod["model"]).strip()})
                else:
                    state.set_setting("moderation", None)
        except (ValueError, walletlib.WalletError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse({"ok": True})

    _admin_prepared: dict[str, tuple[float, str, Any]] = {}

    @app.post("/admin/api/send")
    def admin_send(request: Request, payload: Any = Body(None)):
        """Prepare a send from the node's own wallet: the fee is shown before
        anything goes (the same two steps as the wallet page)."""
        _admin_check(request, write=True)
        said = payload if isinstance(payload, dict) else {}
        try:
            ctx = _context(str(said.get("which", "")))
            sats = walletlib.parse_amount(str(said.get("amount", "")))
            to = _tag_address(state, str(said.get("to", "")), mainnet=ctx.is_mainnet)
            with ctx.rpc() as rpc:
                prepared = walletlib.prepare_send(rpc, to, sats)
        except Exception as exc:                        # noqa: BLE001
            return JSONResponse({"detail": str(exc)}, status_code=400)
        ticket = secrets.token_urlsafe(16)
        now = time.time()
        for key in [k for k, v in _admin_prepared.items() if now - v[0] > 600]:
            _admin_prepared.pop(key, None)
        _admin_prepared[ticket] = (now, str(said.get("which")), prepared)
        return JSONResponse({"ticket": ticket, "to": to,
                             "amount": f"{sats / 1e8:.8f}".rstrip("0").rstrip("."),
                             "fee": f"{prepared.fee_sats / 1e8:.8f}".rstrip("0").rstrip("."),
                             "network": ctx.label})

    @app.post("/admin/api/send/confirm")
    def admin_send_confirm(request: Request, payload: Any = Body(None)):
        """Broadcast a prepared send. From outside, the password again, every time."""
        who = _admin_check(request, write=True)
        said = payload if isinstance(payload, dict) else {}
        if not who["local"] and not _admin_password_ok(str(said.get("password", "")),
                                                       request):
            return JSONResponse({"detail": "the admin password, to send the node's coins"},
                                status_code=403)
        held = _admin_prepared.pop(str(said.get("ticket", "")), None)
        if held is None or time.time() - held[0] > 600:
            return JSONResponse({"detail": "that send has expired -- prepare it again"},
                                status_code=400)
        try:
            with _context(held[1]).rpc() as rpc:
                txid = walletlib.broadcast(rpc, held[2])
        except Exception as exc:                        # noqa: BLE001
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse({"txid": txid})

    @app.post("/admin/api/receive")
    def admin_receive(request: Request, payload: Any = Body(None)):
        _admin_check(request, write=True)
        try:
            with _context(str((payload or {}).get("which", ""))).rpc() as rpc:
                return JSONResponse({"address": walletlib.receive_address(rpc, "admin")})
        except Exception as exc:                        # noqa: BLE001
            return JSONResponse({"detail": str(exc)}, status_code=400)

    # --- the Cloudflare tunnel wizard (2026-09-25; arcade/tunnel.py) -----

    _tunnel_wizard: dict[str, Any] = {}

    def _wizard():
        from .. import tunnel as tunnellib
        if "w" not in _tunnel_wizard:
            _tunnel_wizard["w"] = tunnellib.Wizard()
        return _tunnel_wizard["w"]

    @app.get("/admin/api/tunnel")
    def admin_tunnel_status(request: Request):
        who = _admin_check(request)
        if not who["local"]:
            return JSONResponse({"detail": "the tunnel is set up at the node machine"},
                                status_code=403)
        got = _wizard().status()
        got.update({"public_hosts": list(state.public_hosts),
                    "pages_host": state.pages_hostname or ""})
        return JSONResponse(got, headers={"Cache-Control": "no-store"})

    @app.post("/admin/api/tunnel")
    def admin_tunnel_step(request: Request, payload: Any = Body(None)):
        """One step of the wizard. At the node machine only: it changes this machine."""
        from .. import tunnel as tunnellib
        who = _admin_check(request, write=True)
        if not who["local"]:
            return JSONResponse({"detail": "the tunnel is set up at the node machine"},
                                status_code=403)
        said = payload if isinstance(payload, dict) else {}
        step = str(said.get("step", ""))
        w = _wizard()
        port = int(request.url.port or 8420)
        try:
            if step == "install":
                return JSONResponse({"ok": True, "cloudflared": w.install()})
            if step == "login":
                return JSONResponse({"ok": True, "url": w.login_start()})
            if step == "create":
                return JSONResponse({"ok": True, **w.create(str(said.get("name", "")))})
            if step == "route":
                return JSONResponse({"ok": True, "hostname": w.route(
                    str(said.get("tunnel", "")), str(said.get("hostname", "")))})
            if step == "config":
                hosts = [h for h in said.get("hostnames") or [] if isinstance(h, str) and h]
                return JSONResponse({"ok": True, "config": w.write_config(
                    str(said.get("id", "")), hosts, port)})
            if step == "service":
                return JSONResponse({"ok": True, **w.install_service()})
            if step == "settings":
                app_host = str(said.get("app", "")).strip().lower()
                pages = str(said.get("pages", "")).strip().lower()
                if app_host:
                    hosts = [app_host] + [h for h in state.public_hosts if h != app_host]
                    state.set_setting("public_hosts", hosts)
                if pages:
                    state.set_setting("pages_host", pages)
                return JSONResponse({"ok": True, "public_hosts": list(state.public_hosts)})
            if step == "verify":
                return JSONResponse(tunnellib.verify(str(said.get("hostname", ""))))
            if step == "quick":
                return JSONResponse({"ok": True, "url": w.quick(port)})
            if step == "quick_stop":
                w.quick_stop()
                return JSONResponse({"ok": True})
        except tunnellib.TunnelError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except Exception as exc:                          # noqa: BLE001
            return JSONResponse({"detail": f"that step failed: {exc}"}, status_code=400)
        return JSONResponse({"detail": "no such step"}, status_code=400)

    @app.post("/admin/api/operator")
    def admin_operator(request: Request, payload: Any = Body(None)):
        """Name the operator account. At the node machine only."""
        who = _admin_check(request, write=True)
        if not who["local"]:
            return JSONResponse({"detail": "the operator is named at the node machine"},
                                status_code=403)
        pubkey = str((payload or {}).get("pubkey", "")).strip().lower()
        if state.accounts().account(pubkey) is None:
            return JSONResponse({"detail": "no account with that key has a seat here"},
                                status_code=400)
        state.claim_operator(pubkey)
        state.admin_sessions().close_all()
        return JSONResponse({"ok": True})

    @app.post("/admin/api/password")
    def admin_password(request: Request, payload: Any = Body(None)):
        """Set or change the remote-access password. At the node machine only."""
        who = _admin_check(request, write=True)
        if not who["local"]:
            return JSONResponse({"detail": "the password is set at the node machine"},
                                status_code=403)
        operator = state.operator
        if not operator:
            return JSONResponse({"detail": "name the operator account first"},
                                status_code=400)
        password = str((payload or {}).get("password", ""))
        if len(password) < 10:
            return JSONResponse({"detail": "at least 10 characters -- this guards the "
                                           "node's coins from anywhere"}, status_code=400)
        creds = state.credentials()
        name = adminlib.username_for(creds, operator) or "operator"
        try:
            creds.set(name, password, operator)
        except accountslib.AccountError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        state.admin_sessions().close_all()           # every old session is gone
        return JSONResponse({"ok": True})

    @app.post("/auth/operator")
    def auth_claim_operator(request: Request):
        """Make the signed-in account this node's operator.

        Only from the machine itself. The whole security of it is that
        becoming the operator requires already being where the node is --
        so it is refused for any request the door would call public, which
        includes anything that crossed an edge whatever Host it claims.

        Claimed once and then only by the operator: a node that lets the
        next person who signs in take it over is a node anybody can take
        over by signing in.
        """
        if _from_outside(request):
            return JSONResponse(
                {"detail": "this is set from the machine the node runs on, "
                           "not from outside it"}, status_code=403)
        account = signed_in(request)
        if account is None:
            return JSONResponse(
                {"detail": "sign in first, with the words you want this "
                           "node to answer to"}, status_code=403)
        held = state.operator
        if held and held != account.pubkey.lower():
            return JSONResponse(
                {"detail": "this node already has an operator. Sign in as "
                           "them to change it."}, status_code=403)
        state.claim_operator(account.pubkey)
        return JSONResponse({"operator": account.pubkey})

    @app.get("/auth/who")
    def auth_who(request: Request):
        """Who this browser is, for the page to draw. Never anybody else."""
        account = signed_in(request)
        register = state.accounts()
        if account is None:
            return JSONResponse({"pubkey": None, "free": register.free(),
                                 "seats": register.seats})
        return JSONResponse({"pubkey": account.pubkey, "tag": "",
                             "created": account.created, "seen": account.seen,
                             "free": register.free(), "seats": register.seats,
                             "operator": account.pubkey.lower() == state.operator,
                             "claimable": not state.operator
                             and not _from_outside(request)})

    # --- making the next one --------------------------------------------------
    #
    # Every instance serves the application and a copy of its index, so a
    # clone can make the next clone. A network of nodes that all send people
    # back to one website is one website away from being no network at all.
    #
    # The three source names are the ones the installer already fetches, so
    # any node can be the place somebody installs from with no new option to
    # point at one.

    def _packed_source():
        """This instance's own source, packed if it has not been."""
        said = bootstraplib.source(state.home)
        revision = state.running_version or "unknown"
        if said is None or said.get("revision") != revision:
            try:
                said = bootstraplib.source_archive(
                    state.home, state.checkout, revision)
            except bootstraplib.BootstrapError:
                return None
        return said

    def _served_bootstrap(network: str):
        """The published index copy for a chain, remade if it has gone stale.

        The name comes out of a URL, so a chain this node does not index is
        an answer rather than an exception.
        """
        context = next((c for c in state.token_chains if c.network == network),
                       None)
        if context is None:
            return None
        index = state.ledger_index_path(context)
        tip = state.ledger_tips.get(network) or state.tips.get(network)
        return bootstraplib.current(state.home, network, index,
                                    context.params.index_start, tip)

    # --- identity by key: who runs which arcade (arcade/instance.py) -------------

    @app.get("/.well-known/dogecoinarcade.json")
    def instance_claim():
        """This arcade's own claim, for a directory visitor's browser to check
        against the chain: the fee address that announces it, and its revision.
        Open to any origin on purpose -- it is the one file another site's page
        must be able to read -- and it says nothing that is not public."""
        with state.store() as store:
            mine = [dict(r) for r in store.instances(state.messaging.network)
                    if r["address"] == state.derived_address]
        return JSONResponse(
            {"fee_address": state.derived_address or "",
             "revision": state.running_version or "",
             "domains": list(state.public_hosts),
             "announced": [{"domain": r["domain"], "txid": r["txid"],
                            "height": r["height"]} for r in mine]},
            headers={"Access-Control-Allow-Origin": "*",
                     "Cache-Control": "no-store"})

    @app.get("/instances", response_class=HTMLResponse)
    def instances_page(request: Request):
        """Every arcade that has said on the chain who runs it."""
        with state.store() as store:
            rows = [dict(r) for r in store.instances(state.messaging.network)]
        return render(request, "instances.html", rows=rows,
                      fee_address=state.derived_address or "",
                      revision=state.running_version or "",
                      hosts=list(state.public_hosts), when=_when)

    @app.post("/instances/announce")
    def instances_announce(request: Request, domain: str = Form(""),
                           csrf_token: str = Form("")):
        """The operator says, on the chain, that this arcade is theirs."""
        try:
            check_csrf(csrf_token)
            said = state.announce_instance(domain)
            state.flash(f"Announced {said['domain']} at revision {said['revision']} "
                        f"from {said['fee_address']} ({said['txid'][:16]}...). It shows "
                        "in the directory once its block lands.", "ok")
        except Exception as exc:                       # noqa: BLE001 -- said to the operator
            state.flash(f"Not announced: {exc}", "err")
        return RedirectResponse("/instances", status_code=303)

    @app.get("/clone", response_class=HTMLResponse)
    def clone_page(request: Request):
        copies = []
        for context in state.token_chains:
            try:
                snapshot = _served_bootstrap(context.network)
            except Exception:
                snapshot = None
            if snapshot is not None:
                copies.append({"chain": context, "snap": snapshot})
        return render(request, "clone.html", source=_packed_source(),
                      copies=copies, here=_origin(request),
                      seats=state.accounts().seats)

    @app.get("/source.tar.gz")
    def clone_source():
        said = _packed_source()
        if said is None:
            raise HTTPException(503, "this instance cannot pack its own source")
        return Response(
            (bootstraplib.folder(state.home) / bootstraplib.SOURCE_NAME).read_bytes(),
            media_type="application/gzip",
            headers={"Content-Disposition":
                     f'attachment; filename="{bootstraplib.SOURCE_NAME}"',
                     "Cache-Control": "no-cache"})

    @app.get("/source.tar.gz.sha256")
    def clone_source_sum():
        said = _packed_source()
        if said is None:
            raise HTTPException(503, "this instance cannot pack its own source")
        return Response(f"{said['sha256']}  {bootstraplib.SOURCE_NAME}\n",
                        media_type="text/plain")

    @app.get("/source.rev")
    def clone_source_revision():
        said = _packed_source()
        if said is None:
            raise HTTPException(503, "this instance cannot pack its own source")
        return Response(f"{said['revision']}\n", media_type="text/plain")

    @app.get("/bootstrap/{name}")
    def clone_bootstrap(name: str):
        """A copy of one index, or the manifest describing it."""
        for suffix, kind in ((".sqlite.gz", "application/gzip"),
                             (".json", "application/json")):
            if name.endswith(suffix):
                network = name[:-len(suffix)]
                break
        else:
            raise HTTPException(404, "no such bootstrap")
        snapshot = _served_bootstrap(network)
        if snapshot is None:
            raise HTTPException(
                404, f"there is no {network} index here to copy yet")
        path = snapshot.path if suffix == ".sqlite.gz" else snapshot.manifest
        return Response(path.read_bytes(), media_type=kind, headers={
            "Content-Disposition": f'attachment; filename="{path.name}"',
            # Named by the height it was taken at, so a cached copy is never
            # served as if it were newer than it is.
            "X-Arcade-Height": str(snapshot.height),
            "Cache-Control": "no-cache"})

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
                                              chain.network, bid, own=own,
                                              cut=_node_cut(state))
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
                             back="/exchange?tab=market",
                             where=f"/exchange/sell/{row['txid']}",
                             amount=amount, kind=kind, property_id=property_id)

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
                             back="/exchange?tab=market",
                             where=f"/exchange/sell/{row['txid']}")

    def _listing_groups() -> tuple[list[dict[str, Any]], str]:
        """What this node is advertising on somebody else's signature.

        The chain is asked whether each piece is still where the listing says
        it is, and the answer is kept apart from the question: `asked` says this
        node read a chain, and `held` says what it read. Collapsed into one, a
        node whose daemon is down would tell a seller their piece sold -- and
        `piece_held` returning None means "not there", which is only the same
        thing as "spent" to a node that actually looked (arcade/listings.py).
        """
        groups, book_error = [], ""
        for chain in state.token_chains:
            try:
                # Looking is what keeps the book's idea of time current, the way
                # reading the feed is what marks it read. `expired` costs nothing
                # to write and unlocks nothing -- it says this node stopped
                # advertising, which is the only thing an expiry ever was.
                state.listings.expire_due(chain.network)
                rows = state.listings.open_listings(chain.network)
            except Exception as exc:
                book_error = f"the listing book could not be read: {exc}"
                rows = []
            if not rows:
                continue
            asked, why, held = True, "", {}
            try:
                with chain.rpc() as rpc:
                    for row in rows:
                        held[row["id"]] = listingslib.piece_held(rpc, row)
            except Exception as exc:
                asked, why = False, str(exc)
            named = _tags_for([row["owner"] for row in rows])
            groups.append({
                "chain": chain, "asked": asked, "why": why,
                "rows": [{
                    "id": row["id"],
                    "what": row["what"],
                    "seller": row["owner"],
                    "tag": named.get(row["owner"], ""),
                    "price": f"{int(row['price']) / listingslib.COIN:.8f}".rstrip("0").rstrip("."),
                    "piece": f"{row['input']['txid'][:16]}…:{row['input']['vout']}",
                    "held": held.get(row["id"]),
                    "listed": row["created"],
                    "left": describe_duration(max(0, int(row["expires"] - time.time()))),
                } for row in rows],
            })
        return groups, book_error

    @app.get("/listings", response_class=HTMLResponse)
    def listings_page(request: Request):
        """The legs this node is holding a signature for, and nothing else.

        Nothing on this page is this node's offer to sell. Every row is a
        signature somebody made in their own browser and handed over: one coin
        of theirs in, one payment out, the price in the bytes they signed. This
        node can complete that or stand out of the way, and it cannot spend the
        piece -- which is the whole reason the book exists separate from
        `state.offers`, where every row is this wallet's own signature.

        So the page has to be the place where what a listing IS gets said. A
        seller cannot withdraw one from here, and the honest words for why are
        on the page rather than in a manual: cancelling is spending the piece,
        and a signature that has already left a browser stays good until a block
        spends it, expiry or no expiry.
        """
        groups, book_error = _listing_groups()
        return render(request, "listings.html", groups=groups,
                      book_error=book_error)

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
                                "other_chains": [c for c in state.token_chains
                                                 if c is not chain],
                                "shops": [], "node_error": None, "owned": set(),
                                "offers_in": [], "offers_out": [], "tags": {}}
        try:
            data["shops"] = _shop_listings(index, chain)
        except Exception as exc:
            data["node_error"] = f"the index could not be read: {exc}"
        # Whose pieces this page is about, which is the one question a
        # marketplace cannot answer twice. Every table below that says "yours"
        # is cut out of this set, so on a public copy it has to be the address
        # of whoever is looking: the node's wallet is a stranger's wallet here,
        # and showing an account the offers standing on it -- with a button to
        # answer them -- is showing them another person's post (D-172).
        data["viewer"] = "wallet"
        if _public_request(request):
            data["viewer"] = "account"
            looking = signed_in(request)
            here = _account_address(looking.pubkey, chain) if looking else ""
            data["owned"] = {here} if here else set()
        else:
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
_MAINNET_VERSIONS = frozenset({NETWORKS["main"].pubkeyhash_version})


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


#: What this node takes of a trade it made the offer for, as a percentage of
#: the price (§1d). Zero is the default for the same reason every other
#: number a node runs on is: the operator says what theirs charges, and a node
#: that never sets one asks nothing of anybody.
TRADE_CUT = 0.0


def _node_cut(state) -> dict:
    """What this node asks of a trade it made the offer for (§1d).

    The RATE, never an amount: an offer carries the percentage and each node
    works the number out of the price itself, so neither side is taking the
    other's word for what the fee was. And the address is this node's own --
    the one its overview page already shows -- because a cut that goes where
    nobody can see it beforehand is not a cut, it is a leak.
    """
    bps = swaplib.cut_bps(state.setting("trade_cut", TRADE_CUT))
    return {"bps": bps, "to": state.derived_address} if bps else {}


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

    What counts as detectable moved on 2026-09-25, and the direction matters.
    The question used to be "is this a mainnet-shaped address", answered from
    every params object in the file -- which on the day it was written meant a
    Dogecoin `D…` address passed the check on a Pepecoin mainnet send, and the
    coins would have gone somewhere this chain cannot even see. A version byte
    identifies a *family*, not a chain: Dogecoin testnet and Pepecoin testnet
    share 113, and this regtest shares 111 with Litecoin testnet. So the check is
    now "is this an address on one of the two chains this arcade moves coins
    on", which is the only question a send can act on.
    """
    wanted = (NETWORKS["main"],) if mainnet else (NETWORKS["test"], NETWORKS["regtest"])
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
    return ("that address is not one this arcade can pay -- it belongs to a "
            "chain this node does not run, and coins sent there cannot come "
            "back.")


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


#: What a POST decided, waiting for the GET that will show it. Keyed by a
#: token in the redirect, kept for ten minutes, and never popped on read --
#: a confirmation page has to survive being reloaded, which is the whole
#: point of putting it behind a GET.
_HELD: dict[str, tuple[float, dict[str, Any]]] = {}
HELD_SECONDS = 600


def _hold(**context: Any) -> str:
    now = time.time()
    for token, (at, _) in list(_HELD.items()):
        if now - at > HELD_SECONDS:
            _HELD.pop(token, None)
    token = secrets.token_urlsafe(9)
    _HELD[token] = (now, context)
    return token


def _picked_up(token: str) -> dict[str, Any]:
    at_and_what = _HELD.get(token or "")
    if at_and_what is None:
        return {}
    at, what = at_and_what
    if time.time() - at > HELD_SECONDS:
        _HELD.pop(token, None)
        return {}
    return what


def _when(ts: int | None = None) -> str:
    """A timestamp, or now when called with nothing."""
    moment = dt.datetime.now() if ts is None else dt.datetime.fromtimestamp(ts)
    return moment.strftime("%Y-%m-%d %H:%M")

"""The API an inscribed page can call, and the bytes it can pull in.

Why this exists
---------------
An inscription is not only a picture. It can be an HTML page, and that page can
want the block height, its own metadata, or another inscription -- a library
inscribed once and reused by everything after it. Ordinals calls this recursion
and serves it at `/content/<id>` and a family of `/r/...` endpoints; this is the
same idea against this chain, with the same paths where the meaning matches, so
anyone who has written for one can read this without a manual.

Two doors, one vocabulary
-------------------------
The same questions are answerable over the node-to-node channel (`da_*` in
web/rpc.py) and from a page in a sandboxed frame. The difference is what each
is allowed to do, not what it is allowed to ask: the RPC has the cookie and can
spend; this cannot do anything at all. Every endpoint here is a read.

What a page may see
-------------------
Everything on the chain, which is everything anyone with an index already has:
blocks, inscriptions, token balances at any address. Also this wallet's own
addresses and holdings -- a marketplace page cannot work without knowing who is
looking. That last part is the one thing the chain does not already say, so it
has a switch, and the switch is off for nobody by default.

Why it is safe to serve inscribed HTML at all
---------------------------------------------
It is not, without care. Inscribed code is written by strangers and runs in the
browser of a wallet that can spend. It is served into an iframe with `sandbox`
and no `allow-same-origin`, so it has an opaque origin: no cookies, no access to
the page around it, no navigating the top window. The endpoints here send
`Access-Control-Allow-Origin: *` so that frame can still fetch them, and they
are the only same-origin URLs that do -- everything else in the wallet refuses
a cross-origin read because it says nothing about CORS at all.
"""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from .. import media

#: Sent on every answer here, and nowhere else in the application: a sandboxed
#: frame has an opaque origin, so without it an inscribed page cannot read its
#: own metadata, let alone another inscription.
CORS = {"Access-Control-Allow-Origin": "*"}

#: What a browser is allowed to do with bytes a stranger wrote. `sandbox` in a
#: Content-Security-Policy applies to the RESPONSE, whatever frame it lands in,
#: so it holds even if somebody opens the URL directly in a tab.
CONTENT_HEADERS = {
    **CORS,
    # `sandbox` is what matters: an opaque origin, no cookies, no reach into
    # the page around it. The source rules are about where the page may FETCH
    # from -- this machine and nowhere else, so an inscription cannot phone
    # home about who looked at it. Inline script and style are allowed because
    # an inscribed page is normally one file, and in an opaque origin an inline
    # script is no more dangerous than a same-origin one: there is nothing of
    # ours for it to reach.
    "Content-Security-Policy": (
        "sandbox allow-scripts allow-pointer-lock; "
        "default-src 'self' data: blob:; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob:; "
        "style-src 'self' 'unsafe-inline' data:; "
        "img-src 'self' data: blob:; media-src 'self' data: blob:; "
        "font-src 'self' data:; connect-src 'self'; "
        "frame-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'"),
    "X-Content-Type-Options": "nosniff",
    # The frame's URL carries the viewer's ticket (`?v=`), and the page's own
    # fetches have to carry it back as their Referer for /r/wallet to answer
    # as the viewer. Its opaque origin makes every fetch cross-origin, which
    # the default policy strips to the bare origin. Nothing leaves this node by
    # it: connect-src and every other source above are 'self'.
    "Referrer-Policy": "unsafe-url",
    "Cache-Control": "public, max-age=31536000, immutable",
}

_HOST = re.compile(r"^[A-Za-z0-9.-]+(:[0-9]{1,5})?$")


def own_origin(csp: str, proto: str, host: str) -> str:
    """A sandboxed page's policy, with this host named beside every 'self'.

    `sandbox` gives the page an opaque origin, and browsers disagree about
    what 'self' means then: Chrome still matches the URL's host, Safari
    matches nothing -- so on an iPhone every fetch and every picture an
    inscribed page asked of this node was refused (2026-09-27: the
    Skull Squad board, #59, read "Block ?" with an empty grid). Naming the
    origin the browser asked for says the same thing in a way both read alike,
    and it is still this host and nowhere else.
    """
    if not csp.startswith("sandbox") or not _HOST.match(host or ""):
        return csp
    proto = "https" if proto == "https" else "http"
    return csp.replace("'self'", f"'self' {proto}://{host.lower()}")


#: Content types we will hand to a browser as-is. Anything else is served as a
#: download rather than rendered: an inscription is arbitrary bytes, and
#: guessing at them is how a text file becomes a script.
RENDERABLE = (
    "image/", "video/", "audio/", "text/plain", "text/html", "text/css",
    "application/json", "application/javascript", "text/javascript",
    "application/pdf", "font/", "model/",
)


def _json(payload: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers=CORS)


def _missing(what: str) -> JSONResponse:
    return _json({"error": what}, status=404)


#: A transaction id, and the reason a number cannot be mistaken for one.
TXID_LENGTH = 64


def _key(value: str) -> str | int:
    """An inscription is named by its txid, or by the number people say.

    Length decides, not shape: a transaction id is 64 characters and a hex
    string can be all digits. Asking `isdigit()` first sent
    `0000...0001` -- a perfectly ordinary txid -- to be looked up as
    inscription number 1.
    """
    text = str(value).strip()
    if len(text) == TXID_LENGTH:
        return text.lower()
    if text.isdigit():
        return int(text)
    return text.lower()


def content(index: Any, key: str, download: bool = False) -> Response:
    """The bytes of one inscription, with the type its creator gave it."""
    found = index.inscription_content(_key(key))
    if found is None:
        row = index.inscription(_key(key))
        if row is None:
            return _missing("no such inscription")
        return _json({
            "error": "this node did not keep the content of that inscription",
            "sha256": row["sha256"], "length": row["content_len"],
        }, status=404)

    content_type, body = found
    content_type = media.standard_type(content_type)   # x-javascript is JavaScript too
    safe = content_type if content_type.startswith(RENDERABLE) else None
    headers = dict(CONTENT_HEADERS)
    if download or safe is None:
        headers["Content-Disposition"] = "attachment"
    return Response(body, media_type=safe or "application/octet-stream",
                    headers=headers)


def describe(row: dict, tags: dict | None = None) -> dict:
    """One inscription, as a page sees it. Never the bytes.

    The creator's JSON comes with it, parsed. It was not here at first, which
    meant a gallery of a hundred inscriptions had to make a hundred more calls
    to find out what any of them were -- and the obvious endpoint, the one
    named after the inscription, was the one that did not answer the obvious
    question. It is small by design and the caller chose how many rows to ask
    for, so it costs what the caller asked for.
    """
    raw = row.get("json") or ""
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = None          # inscribed as something that is not JSON
    tags = tags or {}
    return {
        "id": row["txid"],
        "number": row["number"],
        "creator": row["creator"],
        "owner": row["owner"],
        # Who those addresses are called, as of THIS NODE'S TIP. A tag is a
        # claim on the chain and claims move: the wallet's own send page warns
        # that one can change between blocks. Null when nobody holds it. Show
        # it, do not store it against a piece -- an address is what a piece is
        # owned by, a tag is what its owner is called today (D-074).
        "creatortag": tags.get(row["creator"]) or None,
        "ownertag": tags.get(row["owner"]) or None,
        "block": row["block_height"],
        "contenttype": row["content_type"],
        "length": row["content_len"],
        "sha256": row["sha256"],
        "transactions": row["chunks"],
        "held": bool(row["held"]),
        "json": parsed,
        "rawjson": raw,
        "collection": row.get("collection"),
        "edition": row.get("edition"),
    }


def describe_collection(row: dict) -> dict:
    """One collection: who made it, what it is called, how much of it there is."""
    return {
        "creator": row["creator"],
        "name": row["collection"],
        "count": row["count"],
        "firstnumber": row["first_number"],
        "lastnumber": row["last_number"],
        "firstedition": row["first_edition"],
        "lastedition": row["last_edition"],
        "cover": row.get("cover_txid"),
        "covertype": row.get("cover_type"),
        # The size #1 sealed the set at; null for a set with no limit.
        "supply": row.get("supply"),
    }


def holding(row: dict) -> dict:
    """One token balance, as a page sees it.

    `balance` is the string a person would read -- "10,005" or
    "1000.00000000" -- and `units` is the integer the ledger actually holds, so
    a page doing arithmetic never has to parse the display form back.
    """
    units = int(row.get("units", row.get("balance", 0)))
    divisible = bool(row.get("divisible"))
    display = row.get("display")
    if display is None:
        from ..ledger import format_amount
        display = format_amount(units, divisible)
    return {
        "propertyid": row.get("property_id", row.get("propertyid")),
        "name": row.get("name", ""),
        "balance": display,
        "units": units,
        "divisible": divisible,
        # Who issued it, so a game can count only its own tokens (2026-10-01).
        "issuer": row.get("issuer", ""),
    }


def metadata(index: Any, key: str) -> JSONResponse:
    """The JSON the creator inscribed, parsed if it parses.

    Ordinals hands back hex-encoded CBOR and leaves the decoding to the page.
    Ours is JSON because that is what it was written as, and handing a page a
    string it has to decode before it can use it is a step with no purpose.
    """
    row = index.inscription(_key(key))
    if row is None:
        return _missing("no such inscription")
    text = row["json"] or ""
    try:
        return _json({"json": json.loads(text) if text else None, "raw": text})
    except ValueError:
        return _json({"json": None, "raw": text})

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
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response

#: Sent on every answer here, and nowhere else in the application: a sandboxed
#: frame has an opaque origin, so without it an inscribed page cannot read its
#: own metadata, let alone another inscription.
CORS = {"Access-Control-Allow-Origin": "*"}

#: What a browser is allowed to do with bytes a stranger wrote. `sandbox` in a
#: Content-Security-Policy applies to the RESPONSE, whatever frame it lands in,
#: so it holds even if somebody opens the URL directly in a tab.
CONTENT_HEADERS = {
    **CORS,
    "Content-Security-Policy": "sandbox allow-scripts; default-src 'self' data: blob:",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "public, max-age=31536000, immutable",
}

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
    return _json({"error": what}, status_code=404)


def _key(value: str) -> str | int:
    """An inscription is named by its txid, or by the number people say."""
    text = str(value).strip()
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
    safe = content_type if content_type.startswith(RENDERABLE) else None
    headers = dict(CONTENT_HEADERS)
    if download or safe is None:
        headers["Content-Disposition"] = "attachment"
    return Response(body, media_type=safe or "application/octet-stream",
                    headers=headers)


def describe(row: dict) -> dict:
    """One inscription, as a page sees it. Never the bytes."""
    return {
        "id": row["txid"],
        "number": row["number"],
        "creator": row["creator"],
        "owner": row["owner"],
        "block": row["block_height"],
        "contenttype": row["content_type"],
        "length": row["content_len"],
        "sha256": row["sha256"],
        "transactions": row["chunks"],
        "held": bool(row["held"]),
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

"""MoonBrix 2 on the web (arcade/moonbrix.py, docs/moonbrix.md, tbuuol 2026-11-10). KOTIA ONLY.

    GET /moonbrix                     the TLDs and their spaces, folders and files, the properties and who holds
                                      them, the open offers, the inscriptions and the log; a signed-in account sees
                                      what its own Kotia address holds
    GET /moonbrix?q=rpg.lwq           the explorer: a TLD (`lwq`), a space (`rpg.lwq`, `19.55.66.132.lwq`), a folder or
                                      file in one (`rpg.lwq/www/index.html`), a ticker, a txid or an address
    GET /moonbrix/explorer            the same explorer as a page of its own, in tbuuol's Syfy-UI look (Atlantis and
                                      OASIS): an address is dialled like a gate address
    GET /moonbrix/find.json?q=        what a search names (moonbrix.find), for the explorer
    GET /moonbrix.json                the whole state, with the height it is read to
    GET /moonbrix/content/<txid>      an inscription's file, from the chain and checked against its SHA-256,
                                      sandboxed like /content
    GET /account/moonbrix             what the signed-in account's Kotia address holds (needs a session)

Everything here is read from the public chain; nothing is anybody's secret. Only an arcade that runs Kotia's Core
itself has it (the unspent set says which blocks carry data); a borrowing arcade says so.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .. import moonbrix as moonbrixlib
from .content import CONTENT_HEADERS


def register(app, state, *, render, signed_in, account_address) -> None:
    def kotia():
        """(the Kotia chain, its MoonBrix store or None, why there is none)."""
        chain = next((c for c in (getattr(state, "addons", None) or []) if getattr(c.params, "moonbrix", False)), None)
        if chain is None:
            return None, None, "Kotia is not turned on here (Admin -> Chains)."
        if chain.borrowed:
            return chain, None, ("Kotia is borrowed here. MoonBrix is read only by an arcade that runs Kotia's Core "
                                 "itself.")
        return chain, state.moonbrix(chain), ""

    def holdings(request: Request, chain, mb):
        account = signed_in(request)
        if account is None or chain is None or mb is None:
            return account, "", []
        address = account_address(account.pubkey, chain)
        return account, address, (moonbrixlib.balances(mb.document(), address) if address else [])

    @app.get("/moonbrix", response_class=HTMLResponse)
    def moonbrix_page(request: Request, q: str = ""):
        chain, mb, why = kotia()
        doc = mb.document() if mb is not None else moonbrixlib.empty()
        q = q.strip()[:300]
        account, address, held = holdings(request, chain, mb)
        return render(request, "moonbrix.html", why=why, height=mb.height if mb is not None else None, doc=doc,
                      events=list(reversed(doc["log"]))[:200], account_signed=account is not None, address=address,
                      held=held, units=moonbrixlib.units, q=q, found=moonbrixlib.find(doc, q) if q else [])

    @app.get("/moonbrix/explorer", response_class=HTMLResponse)
    def moonbrix_explorer():
        from .app import nonced
        # A plain file, not a template: its inline script gets this request's nonce as it is sent.
        page = Path(__file__).with_name("moonbrix_explorer.html").read_text(encoding="utf-8")
        return HTMLResponse(nonced(page), headers={"Cache-Control": "no-store"})

    @app.get("/moonbrix/find.json")
    def moonbrix_find(q: str = ""):
        chain, mb, why = kotia()
        if mb is None:
            return JSONResponse({"error": why, "found": []}, status_code=404)
        q = q.strip()[:300]
        return {"q": q, "height": mb.height, "found": moonbrixlib.find(mb.document(), q) if q else []}

    @app.get("/moonbrix.json")
    def moonbrix_json():
        chain, mb, why = kotia()
        if mb is None:
            return JSONResponse({"error": why}, status_code=404)
        return {"chain": chain.network, "height": mb.height, "best": mb.best, **mb.document()}

    @app.get("/moonbrix/content/{txid}")
    def moonbrix_content(txid: str):
        chain, mb, why = kotia()
        if mb is None:
            raise HTTPException(404, why)
        if len(txid) != 64 or any(ch not in "0123456789abcdef" for ch in txid):
            raise HTTPException(400, "a transaction id is 64 hex characters")
        doc = mb.document()
        if txid not in doc["inscriptions"]:
            raise HTTPException(404, "no MoonBrix inscription has that id")
        try:
            with chain.rpc() as rpc:
                kind, data = moonbrixlib.content(doc, moonbrixlib.fetch_tx_by_rpc(rpc, chain.params), txid)
        except (KeyError, moonbrixlib.Rejected) as exc:
            raise HTTPException(404, str(exc)) from None
        return Response(data, media_type=kind, headers=CONTENT_HEADERS)

    @app.get("/account/moonbrix")
    def account_moonbrix(request: Request):
        chain, mb, why = kotia()
        account, address, held = holdings(request, chain, mb)
        if account is None:
            raise HTTPException(403, "sign in first")
        return {"address": address, "height": mb.height if mb is not None else None, "held": held, "why": why}

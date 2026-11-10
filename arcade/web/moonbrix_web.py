"""MoonBrix on the web (arcade/moonbrix.py, tbuuol 2026-11-10). KOTIA ONLY.

    GET /moonbrix                     the TLDs and their IPs, the properties and who holds them, the inscriptions and
                                      the event log, as this arcade's copy of MoonBrix's Kurator reads them; a
                                      signed-in account sees what its own Kotia address holds
    GET /moonbrix.json                the same five files as MoonBrix keeps them, with the height they are read to
    GET /moonbrix/content/<txid>      a meta inscription's file, its chunks put together, sandboxed like /content
    GET /account/moonbrix             what the signed-in account's Kotia address holds (needs a session)

Everything here is read from the public chain; nothing is anybody's secret. Only an arcade that runs Kotia's Core
itself has it (the unspent set says which blocks carry data); a borrowing arcade says so.
"""
from __future__ import annotations

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
    def moonbrix_page(request: Request):
        chain, mb, why = kotia()
        doc = mb.document() if mb is not None else moonbrixlib.empty()
        account, address, held = holdings(request, chain, mb)
        return render(request, "moonbrix.html", why=why, height=mb.height if mb is not None else None, doc=doc,
                      events=list(reversed(doc["lib"])), skipped=mb.skipped() if mb is not None else [],
                      quirks=moonbrixlib.QUIRKS, account_signed=account is not None, address=address, held=held,
                      ip=moonbrixlib.ip, text=moonbrixlib.text)

    @app.get("/moonbrix.json")
    def moonbrix_json():
        chain, mb, why = kotia()
        if mb is None:
            return JSONResponse({"error": why}, status_code=404)
        return {"chain": chain.network, "height": mb.height, "best": mb.best, **mb.document(),
                "skipped": mb.skipped(), "quirks": list(moonbrixlib.QUIRKS)}

    @app.get("/moonbrix/content/{txid}")
    def moonbrix_content(txid: str):
        chain, mb, why = kotia()
        if mb is None:
            raise HTTPException(404, why)
        if len(txid) != 64 or any(ch not in "0123456789abcdef" for ch in txid):
            raise HTTPException(400, "a transaction id is 64 hex characters")
        if not any(i["TXID"] == txid for i in mb.document()["ins"]):
            raise HTTPException(404, "no MoonBrix inscription has that id")
        try:
            with chain.rpc() as rpc:
                kind, data = moonbrixlib.inscription(rpc, chain.params, txid)
        except (KeyError, moonbrixlib.Unreadable) as exc:
            raise HTTPException(404, str(exc)) from None
        return Response(data, media_type=kind, headers=CONTENT_HEADERS)

    @app.get("/account/moonbrix")
    def account_moonbrix(request: Request):
        chain, mb, why = kotia()
        account, address, held = holdings(request, chain, mb)
        if account is None:
            raise HTTPException(403, "sign in first")
        return {"address": address, "height": mb.height if mb is not None else None, "held": held, "why": why}

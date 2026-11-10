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
    POST /account/moonbrix/offer      a MoonBrix command from the signed-in account, built as an unsigned offer the
                                      browser checks byte by byte and signs (wallet.js sendMoonBrix)

Everything here is read from the public chain; nothing is anybody's secret. Only an arcade that runs Kotia's Core
itself has it (the unspent set says which blocks carry data); a borrowing arcade says so.
"""
from __future__ import annotations

import json
from pathlib import Path

from typing import Any

from fastapi import Body, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .. import moonbrix as moonbrixlib
from ..funding import FundingError
from ..txbuild import p2pkh_script
from .content import CONTENT_HEADERS


def register(app, state, *, render, signed_in, account_address, build=None, payee=None, pay_script=None) -> None:
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
                      held=held, units=moonbrixlib.units, q=q, found=moonbrixlib.find(doc, q) if q else [],
                      form=moonbrixlib.FORM, targets={n: t for n, _f, t in moonbrixlib.OPS.values()},
                      can_send=build is not None and mb is not None)

    @app.get("/moonbrix/explorer", response_class=HTMLResponse)
    def moonbrix_explorer():
        from .app import nonced
        # A plain file, not a template: its inline script gets this request's nonce as it is sent.
        page = Path(__file__).with_name("moonbrix_explorer.html").read_text(encoding="utf-8")
        return HTMLResponse(nonced(page), headers={"Cache-Control": "no-store"})

    @app.get("/moonbrix/tx/{txid}")
    def moonbrix_tx(txid: str):
        """How deep a Kotia transaction is (0: waiting in the mempool). The inscribe of a big file waits on it: each
        part is up to 80 kB, and Kotia's mempool takes no more than 101 kB of unconfirmed ancestors."""
        chain, mb, why = kotia()
        if chain is None or chain.borrowed:
            raise HTTPException(404, why or "Kotia is not read here")
        if len(txid) != 64 or any(ch not in "0123456789abcdef" for ch in txid):
            raise HTTPException(400, "a transaction id is 64 hex characters")
        try:
            with chain.rpc() as rpc:
                got = rpc.call("getrawtransaction", txid, 1)
        except Exception:
            raise HTTPException(404, "this node does not know that transaction") from None
        return {"txid": txid, "confirmations": int(got.get("confirmations") or 0)}

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

    @app.post("/account/moonbrix/offer")
    def account_moonbrix_offer(request: Request, payload: Any = Body(None)):
        """A MoonBrix command from this account, built and held as an offer; nothing is signed or sent here.

        The node only builds: the browser encodes the same command itself and refuses a transaction whose OP_RETURNs
        or payment are not exactly that. What MoonBrix would make of the command is tried against the state read so
        far and said as a `warning`, never a refusal: on Kotia what a person writes is theirs to send (tbuuol)."""
        account = signed_in(request)
        if account is None:
            raise HTTPException(403, "sign in first")
        chain, mb, why = kotia()
        if chain is None or build is None:
            return JSONResponse({"detail": why or "this arcade cannot send MoonBrix commands"}, status_code=400)
        said = payload if isinstance(payload, dict) else {}
        name = str(said.get("op") or "")
        if name not in moonbrixlib.CODES:
            return JSONResponse({"detail": f"{name or 'that'} is not a MoonBrix command"}, status_code=400)
        sender = account_address(account.pubkey, chain)
        if not sender:
            return JSONResponse({"detail": f"this account has no {chain.label} address yet"}, status_code=400)
        try:
            data = moonbrixlib.encode(name, *moonbrixlib.fields_from_json(name, said.get("fields")))
            if len(data) > moonbrixlib.MAX_SEND:
                raise ValueError(f"that command is {len(data)} bytes; one transaction carries "
                                 f"{moonbrixlib.MAX_SEND}. A bigger file goes as parts.")
            doc = mb.document() if mb is not None else None
            target, value = _target(name, said, doc, sender)
            dust = int(getattr(chain.params, "dust_limit", 0) or 100_000)
            outputs = moonbrixlib.send_outputs(data, p2pkh_script(sender), dust,
                                               pay_script(target, chain) if target else None, value)
            warning = _dry_run(doc, mb, data, sender, outputs, chain) if doc is not None else ""
            what = f"MoonBrix {name}" + (f" to {target}" if target else "")
            offer = build(account, chain, outputs, what)
        except (FundingError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse({**offer, "op": name, "target": target, "target_value": max(value, dust) if target else 0,
                             "warning": warning, "bytes": len(data)})

    def _target(name: str, said: dict, doc, sender: str) -> tuple[str, int]:
        """(who the command's target is, what it is paid). A reserve or a pay goes to the offer's seller with what
        is due (`pay_all`: a reserve that pays the whole share at once); every other command that names a target
        goes to the address or @name given, paid the smallest output Kotia relays."""
        wants = moonbrixlib.OPS[moonbrixlib.CODES[name]][2]
        fields = said.get("fields") or []
        if name in ("reserve", "pay"):
            if doc is None:
                raise ValueError("this arcade does not read MoonBrix, so it cannot say what is due")
            if name == "reserve":
                offer = doc["offers"].get(str(fields[0]))
                if offer is None:
                    raise ValueError("no open offer has that id")
                amount = int(fields[1])
                share = offer["price"] if said.get("pay_all") else offer["deposit"]
                return offer["seller"], -(-share * amount // max(1, offer["amount"]))
            r = doc["reservations"].get(str(fields[0]))
            if r is None:
                raise ValueError("no reservation has that id (paid, or its window closed)")
            return doc["offers"][r["offer"]]["seller"], r["due"] - r["paid"]
        to = str(said.get("to") or "").strip()
        if not wants or (wants == "maybe" and not to):
            return "", 0
        address, _tag = payee(to, chain_of())
        if address == sender:
            raise ValueError("the target is the first address other than yours: send it to somebody else")
        return address, 0

    def chain_of():
        return kotia()[0]

    def _dry_run(doc, mb, data, sender, outputs, chain) -> str:
        """What MoonBrix would make of the command were it the next one: "" when it would be applied."""
        if moonbrixlib.decode(data)[0] == moonbrixlib.CODES["assemble"]:
            return ""                                   # its parts are on their way, not in a block yet
        said = [(moonbrixlib.address_of(script, chain.params), value) for value, script in outputs]
        said.append((sender, 0))                        # its change
        try:
            moonbrixlib.Book(json.loads(json.dumps(doc))).apply((mb.height or 0) + 1, 1 << 30, "f" * 64, data,
                                                                sender, said)
        except moonbrixlib.Rejected as exc:
            return str(exc)
        except moonbrixlib.MoonBrixError:
            return ""
        return ""

    @app.get("/account/moonbrix")
    def account_moonbrix(request: Request):
        chain, mb, why = kotia()
        account, address, held = holdings(request, chain, mb)
        if account is None:
            raise HTTPException(403, "sign in first")
        return {"address": address, "height": mb.height if mb is not None else None, "held": held, "why": why}

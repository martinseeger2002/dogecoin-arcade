"""The DigiDollar-test launchpad's web side (arcade/ddpad.py): its page, a mint, and the arcade's chains for its tick.

`/bytelings` is the page: the wall of pieces, how many are left, Mint, and this account's own mints. `/ddpad/mint`
holds a piece and offers the account a DigiDollar test send of the price to the pad, built by the Wallet's own send
(app.py `dd_send`); the browser signs it as any send and /account/sign broadcasts it, calling the offer's `done`.
A thread ticks the pad every TICK seconds while it has a book: payments checked, pieces delivered and inscribed, one
broadcast at a time, each signed with the pad's own key here.
"""
from __future__ import annotations

import contextlib
import logging
import threading
import time
from typing import Any, Callable

from fastapi import Body, Request
from fastapi.responses import JSONResponse, Response

from .. import ddpad as padlib
from .. import digidollar as digidollib
from .. import fees
from .. import funding as fundinglib
from .. import ord_inscribe as oi
from .. import utxos as utxoslib
from ..rpc import RpcError
from ..script import b58check_encode, hash160

log = logging.getLogger("arcade.web")

TICK = 15
#: A broadcast of the pad's that the index has not caught up with after this long is given up on (dropped by the
#: network): the next tick builds from what the index says.
FLIGHT_SECONDS = 2 * 3600


class ArcadeEnv:
    """The pad's view of this arcade's chains (ddpad.Env): the pieces' chain through this arcade's index and Core,
    the payments' chain through its Core."""

    def __init__(self, state, pad: padlib.Pad, key: padlib.PadKey, chain, pay_chain):
        self.state, self.pad, self.key, self.chain, self.pay_chain = state, pad, key, chain, pay_chain
        self.address = b58check_encode(chain.params.pubkeyhash_version, hash160(key.pubkey))
        self.pay_key = digidollib.output_key(key.pubkey)
        self.pay_address = digidollib.address(pay_chain.network, key.pubkey)
        #: The pad's own broadcasts the index has not filed yet: what each spent, and its change.
        self.flights: list[dict] = []
        self._watched = False

    def _index(self):
        return self.state.token_index(self.chain)

    def watch(self) -> None:
        """The index follows the pad's address from now on (its coins are what the pad spends)."""
        if self._watched:
            return
        index = self._index()
        with contextlib.closing(index.open()) as db:
            utxoslib.watch(db, self.address, int(index.indexed_height() or 0), why="ddpad")
        self._watched = True

    # --- payments ----------------------------------------------------------------------------------------------

    def payment(self, txid: str) -> tuple[str, int]:
        try:
            tx = self.pay_chain.rpc().call("getrawtransaction", txid, True)
        except RpcError:
            return "missing", 0
        cents = sum(c for _vout, c, k in digidollib.dollars_out(tx) if k == self.pay_key)
        return ("confirmed" if int(tx.get("confirmations") or 0) >= 1 else "waiting"), cents

    # --- the pieces' chain -------------------------------------------------------------------------------------

    def settled(self, txid: str) -> bool:
        try:
            return int(self.chain.rpc().call("getrawtransaction", txid, True).get("confirmations") or 0) >= 1
        except RpcError:
            return False

    def ready(self) -> bool:
        if not self.flights:
            return True
        with contextlib.closing(self._index().open()) as db:
            unspent = {(u["txid"], int(u["vout"])) for u in utxoslib.unspent(db, self.address)}
        now = time.time()
        self.flights = [f for f in self.flights if (f["spent"] & unspent or not self.settled(f["txid"]))
                        and now - f["at"] < FLIGHT_SECONDS]
        return not self.flights

    def _send(self, unsigned, what: str) -> str:
        sigs = [self.key.sign(bytes.fromhex(h)).hex() for h in unsigned.sighashes]
        raw = fundinglib.assemble(unsigned, sigs, self.key.pubkey)
        txid = self.chain.rpc().call("sendrawtransaction", raw)
        log.info("ddpad: %s in %s", what, txid)
        return txid

    def _flown(self, txid: str, unsigned) -> None:
        self.flights.append({"txid": txid, "at": time.time(),
                             "spent": {(c["txid"], int(c["vout"])) for c in unsigned.inputs}})

    def inscribe(self, content_type: str, content: bytes) -> dict:
        self.watch()
        params = self.chain.params
        p = oi.plan(self.chain.network, params, content_type, content, self.key.pubkey, self.address)
        lock_script = oi.p2sh(p.locks[0])
        with contextlib.closing(self._index().open()) as db:
            commit = fundinglib.build(db, params, self.address, [(p.commit_value, lock_script)],
                                      rate=fees.MIN_FEE_PER_KB, what="inscribe a piece",
                                      exclude=self.pad.held_outpoints())
        vout = next(n for n, (_v, s) in enumerate(commit.outputs) if s == lock_script)
        commit_txid = self._send(commit, "commit")
        self._flown(commit_txid, commit)
        coin, first = {"txid": commit_txid, "vout": vout}, ""
        for i in range(p.reveals):
            txid = self._send(oi.build_reveal(params, p, i, coin), f"reveal {i + 1} of {p.reveals}")
            first = first or txid
            coin = {"txid": txid, "vout": 0}
        return {"iid": f"{first}i0", "outpoint": f"{coin['txid']}:0", "value": oi.POSTAGE, "commit": commit_txid}

    def deliver(self, iid: str, outpoint: str, value: int, to: str) -> str:
        self.watch()
        txid, _, vout = outpoint.partition(":")
        coin = {"txid": txid, "vout": int(vout), "value": int(value), "address": self.address}
        with contextlib.closing(self._index().open()) as db:
            unsigned = fundinglib.build_doginal_send(db, self.chain.params, self.address, coin, iid, to,
                                                     rate=fees.MIN_FEE_PER_KB, exclude=self.pad.held_outpoints())
        sent = self._send(unsigned, f"send {iid} to {to}")
        self._flown(sent, unsigned)
        return sent


def register(app, state, *, signed_in: Callable, signed_in_account: Callable, account_chain: Callable,
             account_chains: Callable,
             account_address: Callable, dd_chain: Callable, dd_owner: Callable, dd_send: Callable, offers,
             render: Callable) -> None:
    pad = padlib.Pad(state.home)
    key = padlib.PadKey(state.home)
    envs: dict[tuple, ArcadeEnv] = {}

    def chain_of(network: str):
        return next((c for c in getattr(state, "all_chains", ()) or () if c.network == network), None)

    def env() -> ArcadeEnv | None:
        cfg = pad.config()
        chain, pay = chain_of(cfg["chain"]), chain_of(cfg["pay_chain"])
        if chain is None or pay is None:
            return None
        k = (chain.network, pay.network)
        if k not in envs:
            envs[k] = ArcadeEnv(state, pad, key, chain, pay)
        if pad.path.exists() and not envs[k]._watched:
            # from the first moment, so the DGB sent to fund the pad is seen (the index is never retroactive)
            try:
                envs[k].watch()
            except Exception as exc:                      # noqa: BLE001 -- tried again on the next ask
                log.info("ddpad: could not watch the pad's address yet: %s", exc)
        return envs[k]

    def tick_forever() -> None:
        while True:
            time.sleep(TICK if envs else 1)
            try:
                if not pad.path.exists():
                    continue
                e = env()
                if e is None:
                    continue
                for line in pad.tick(e):
                    log.info("ddpad: %s", line)
            except Exception as exc:                      # noqa: BLE001 -- said, and tried again next tick
                log.warning("ddpad: %s", exc)

    if pad.path.exists():
        threading.Thread(target=tick_forever, daemon=True, name="ddpad").start()

    @app.get("/bytelings")
    def ddpad_page(request: Request):
        cfg = pad.config()
        return render(request, "ddpad.html", pad_name=cfg["name"], pay_chain=cfg["pay_chain"],
                      price=cfg["price_cents"], chain=account_chain())

    @app.get("/ddpad/state")
    def ddpad_state(request: Request):
        cfg = pad.config()
        e = env() if pad.path.exists() else None
        account = signed_in(request)
        return JSONResponse({
            "name": cfg["name"], "open": bool(cfg["open"]), "price_cents": cfg["price_cents"],
            "per_wallet": cfg["per_wallet"], "chain": cfg["chain"], "pay_chain": cfg["pay_chain"],
            "pad_address": e.address if e else "", "pay_address": e.pay_address if e else "",
            "counts": pad.counts() if pad.path.exists() else {"total": 0, "inscribed": 0, "left": 0, "minted": 0},
            "pieces": [{k: p[k] for k in ("number", "iid", "inscribed", "taken")} for p in pad.pieces()]
            if pad.path.exists() else [],
            "evolve": pad.evolve(), "mine": pad.mints_of(account.pubkey) if account is not None and
            pad.path.exists() else []}, headers={"Cache-Control": "no-store"})

    @app.get("/ddpad/picture/{number}")
    def ddpad_picture(number: int):
        got = pad.picture(number) if pad.path.exists() else None
        if got is None:
            return Response(status_code=404)
        ctype, content = got
        return Response(content, media_type=ctype, headers={"Cache-Control": "public, max-age=86400"})

    @app.post("/ddpad/mint")
    def ddpad_mint(request: Request, payload: Any = Body(None)):
        """Hold a random piece for this account and offer its payment: nothing is broadcast here."""
        account = signed_in_account(request)
        said = payload if isinstance(payload, dict) else {}
        cfg = pad.config()
        e = env()
        if e is None:
            return JSONResponse({"detail": f"this arcade has no {cfg['chain']} and {cfg['pay_chain']} for the pad"},
                                status_code=400)
        pay_ch = dd_chain(cfg["pay_chain"])
        _, pub = dd_owner(account, pay_ch, str(said.get("pubkey") or ""))
        piece_chain = next((c for c in account_chains() if c.network == cfg["chain"]), None)
        to = account_address(account.pubkey, piece_chain) if piece_chain is not None else ""
        if not to:
            return JSONResponse({"detail": "this account has no DigiByte address yet: open the Wallet once, then "
                                           "come back"}, status_code=400)
        try:
            mint = pad.hold(account.pubkey, to, digidollib.address(pay_ch.network, pub))
        except padlib.PadError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        try:
            unsigned = dd_send(account, pay_ch, pub, e.pay_address, int(cfg["price_cents"]))
        except Exception as exc:                          # noqa: BLE001 -- the piece goes back, the reason is said
            pad.let_go(mint["id"], "its payment could not be built")
            return JSONResponse({"detail": str(exc)}, status_code=400)
        offer = offers.add(account.pubkey, pay_ch.network, unsigned, unsigned.what,
                           done=lambda txid: pad.paying(mint["id"], txid),
                           check=lambda: pad.still_held(mint["id"]))
        return {"offer": offer.id, "mint": mint["id"], "chain": pay_ch.network, **unsigned.as_json()}

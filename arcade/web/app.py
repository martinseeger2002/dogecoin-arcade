"""The DogecoinArcade web interface.

Messenger only, for now. The Exchange, NFT and Inscription sections are shown
but disabled, because the engines behind them (M3-M5) do not exist yet -- a menu
that pretends otherwise would be worse than one that says so.
"""

from __future__ import annotations

import datetime as dt
import html
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..config import MainnetRefused, WrongChain
from ..messaging.envelope import build_key_announcement
from ..messaging.keys import Identity, KeyError_, fingerprint_of, save_identity
from ..messaging.miner import Miner, MiningError
from ..messaging.scanner import Scanner
from ..messaging.sender import MessageSender, SendError, plan_message
from .state import AppState

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Sections that exist, and sections that do not. Shown honestly rather than
# hidden, so the shape of the finished product is visible.
#: (path, label, chain, built). `chain` is shown in the interface on every page,
#: because a user who cannot tell whether an action spends testnet or real coins
#: is one misclick from a bad day (D-012).
NAV = [
    ("/",             "Overview",     None,        True),
    ("/inbox",        "Inbox",        "testnet",   True),
    ("/compose",      "Compose",      "testnet",   True),
    ("/keys",         "Keys",         "testnet",   True),
    ("/wallet",       "Wallets",      None,        True),
    ("/tokens",       "Tokens",       "mainnet",   False),
    ("/nfts",         "NFTs",         "mainnet",   False),
    ("/exchange",     "Exchange",     "mainnet",   False),
    ("/inscriptions", "Inscriptions", "mainnet",   False),
]


def create_app(state: AppState) -> FastAPI:
    app = FastAPI(title="DogecoinArcade", docs_url=None, redoc_url=None)

    def render(request: Request, template: str, **context: Any) -> HTMLResponse:
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
        return TEMPLATES.TemplateResponse(template, base)

    def check_csrf(token: str) -> None:
        import secrets as _s
        if not _s.compare_digest(token or "", state.csrf_token):
            raise ValueError("stale form -- reload the page and try again")

    def messaging_status() -> dict[str, Any]:
        """Testnet node health plus funding, which only the Messenger needs."""
        status = state.messaging.status()
        if status.get("online"):
            try:
                with state.messaging.rpc() as rpc:
                    status["funding"] = Miner(rpc, state.messaging.params).status()
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
        return render(request, "overview.html",
                      messaging=messaging_status(), ledger=ledger_status(), stats=stats)

    # --- identity -------------------------------------------------------------

    @app.post("/unlock")
    def unlock(request: Request, passphrase: str = Form(""), csrf_token: str = Form("")):
        try:
            check_csrf(csrf_token)
            state.unlock(passphrase)
        except (KeyError_, ValueError) as exc:
            state.flash(str(exc), "err")
        return RedirectResponse("/", status_code=303)

    @app.post("/lock")
    def lock(request: Request, csrf_token: str = Form("")):
        try:
            check_csrf(csrf_token)
            state.lock()
        except ValueError as exc:
            state.flash(str(exc), "err")
        return RedirectResponse("/", status_code=303)

    @app.post("/keygen")
    def keygen(request: Request, passphrase: str = Form(""), confirm: str = Form(""),
               csrf_token: str = Form("")):
        try:
            check_csrf(csrf_token)
            if passphrase != confirm:
                raise KeyError_("passphrases do not match")
            if len(passphrase) < 8:
                raise KeyError_("use at least 8 characters: this key is long-lived")
            identity = Identity.generate()
            save_identity(state.key_path, identity, passphrase)
            state.identity = identity
        except (KeyError_, ValueError) as exc:
            state.flash(str(exc), "err")
        return RedirectResponse("/", status_code=303)

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
                raise ValueError("unlock your identity first")
            if not body:
                raise ValueError("nothing to send")
            with state.store() as store:
                keys = store.all_keys()
                row = store.key_for(recipient)
            if row is None:
                raise ValueError(f"no announced key for {recipient}; scan first")
            plan = plan_message(state.identity, bytes(row["pubkey"]), body.encode())
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
                raise ValueError("unlock your identity first")
            with state.store() as store:
                row = store.key_for(recipient)
            if row is None:
                raise ValueError(f"no announced key for {recipient}")

            plan = plan_message(state.identity, bytes(row["pubkey"]), body.encode())
            with state.messaging.rpc() as rpc:
                funding = Miner(rpc, state.messaging.params).status()
                if not funding.funded:
                    raise ValueError(f"cannot send: {funding.describe()}")
                sender = MessageSender(rpc, state.messaging.params)
                address = _funded_address(rpc, state)
                prepared = [sender.prepare(address, p) for p in plan.chunk_payloads]
                if confirmed == "yes":
                    broadcast_txids = [sender.broadcast(p) for p in prepared]
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
                raise ValueError("unlock your identity first")
            payload = build_key_announcement(state.identity.public_bytes)
            with state.messaging.rpc() as rpc:
                if not Miner(rpc, state.messaging.params).status().funded:
                    raise ValueError("no spendable coins yet -- see Wallet")
                sender = MessageSender(rpc, state.messaging.params)
                prepared = sender.prepare(_funded_address(rpc, state), payload, class_c=True)
                if confirmed == "yes":
                    txid = sender.broadcast(prepared)
        except Exception as exc:
            error = str(exc)
        keys = []
        if state.store_path.exists():
            with state.store() as store:
                keys = store.all_keys()
        return render(request, "keys.html", keys=keys, error=error,
                      prepared=prepared, txid=txid)

    # --- wallet ---------------------------------------------------------------

    @app.get("/wallet", response_class=HTMLResponse)
    def wallet(request: Request):
        return render(request, "wallet.html",
                      messaging=messaging_status(), ledger=ledger_status())

    @app.post("/scan")
    def scan(request: Request, csrf_token: str = Form("")):
        try:
            check_csrf(csrf_token)
            with state.rpc() as rpc, state.store() as store:
                scanner = Scanner(rpc, state.messaging.params, store, identity=state.identity)
                result = scanner.scan(max_blocks=5000)
                state.flash(f"Scanned {result}", "ok")
        except Exception as exc:
            state.flash(f"Scan failed: {exc}", "err")
        return RedirectResponse("/", status_code=303)

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
        except (MiningError, Exception) as exc:
            state.flash(f"Mining failed: {exc}", "err")
        return RedirectResponse("/wallet", status_code=303)

    # --- not yet built --------------------------------------------------------

    UNBUILT = {
        "/tokens": ("Tokens", "M2/M3",
                    "Fungible properties: balances, issuance, transfers and "
                    "send-to-owners. The state engine exists; this view does not."),
        "/nfts": ("NFTs", "M4",
                  "Non-fungible properties, range transfers, and the issuer and "
                  "holder data slots."),
        "/exchange": ("Exchange", "M3",
                      "Two-sided MetaDEx order book: bids, asks, on-chain matching, "
                      "partial fills and price charts."),
        "/inscriptions": ("Inscriptions", "M5",
                          "Chunked file inscription over Class B, with a sandboxed "
                          "viewer that verifies content hashes before display."),
    }

    def _unbuilt(request: Request, path: str) -> HTMLResponse:
        section, milestone, detail = UNBUILT[path]
        return render(request, "unbuilt.html", section=section, milestone=milestone,
                      detail=detail, ledger=ledger_status())

    @app.get("/tokens", response_class=HTMLResponse)
    def tokens(request: Request):
        return _unbuilt(request, "/tokens")

    @app.get("/nfts", response_class=HTMLResponse)
    def nfts(request: Request):
        return _unbuilt(request, "/nfts")

    @app.get("/exchange", response_class=HTMLResponse)
    def exchange(request: Request):
        return _unbuilt(request, "/exchange")

    @app.get("/inscriptions", response_class=HTMLResponse)
    def inscriptions(request: Request):
        return _unbuilt(request, "/inscriptions")

    return app


def _funded_address(rpc, state) -> str:
    """Pick an address holding spendable coins.

    Class B seeds its obfuscation with the sender address and the sender is
    "largest input by sum", so the address must genuinely hold the inputs --
    picking an arbitrary wallet address would produce an unreadable message.
    """
    best, best_value = None, 0.0
    for utxo in rpc.call("listunspent", 1, 9_999_999):
        if float(utxo["amount"]) > best_value:
            best, best_value = utxo["address"], float(utxo["amount"])
    if best is None:
        raise SendError("no spendable outputs; see Wallet")
    return best


def _when(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

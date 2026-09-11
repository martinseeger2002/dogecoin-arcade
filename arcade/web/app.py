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

from .. import wallet as walletlib
from ..config import MainnetRefused, WrongChain
from ..messaging import contact, vault
from ..messaging.envelope import build_key_announcement
from ..messaging.keys import (
    Identity, KeyError_, change_passphrase, fingerprint_of, generate_passphrase,
    passphrase_bits, save_identity,
)
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
        # Request first: the older (name, context) signature is deprecated.
        return TEMPLATES.TemplateResponse(request, template, base)

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
        # Offer a generated passphrase when there is no identity yet. Generated
        # is the default because the usual failure is a weak chosen one, and this
        # key has no recovery path at all.
        suggested = generate_passphrase() if not state.has_key else None
        stats = {}
        if state.store_path.exists():
            with state.store() as store:
                stats = store.stats()
        code = None
        if state.unlocked:
            code = contact.encode(state.messaging.network, state.identity.public_bytes)
        return render(request, "overview.html", suggested=suggested, contact_code=code,
                      messaging=messaging_status(), ledger=ledger_status(), stats=stats)

    # --- identity -------------------------------------------------------------

    @app.post("/unlock")
    def unlock(request: Request, passphrase: str = Form(""), remember: str = Form(""),
               csrf_token: str = Form("")):
        try:
            check_csrf(csrf_token)
            state.unlock(passphrase, remember=bool(remember))
        except (KeyError_, ValueError) as exc:
            state.flash(str(exc), "err")
        return RedirectResponse("/", status_code=303)

    @app.post("/reveal")
    def reveal(request: Request, csrf_token: str = Form("")):
        """Show the remembered passphrase. The retrieval path."""
        try:
            check_csrf(csrf_token)
            passphrase = state.reveal_passphrase()
            if passphrase:
                state.flash(f"Your passphrase is:  {passphrase}", "reveal")
            else:
                state.flash(
                    "This passphrase is not saved on this computer. If you have "
                    "lost it, it cannot be recovered.", "err")
        except ValueError as exc:
            state.flash(str(exc), "err")
        return RedirectResponse("/", status_code=303)

    @app.post("/forget")
    def forget(request: Request, csrf_token: str = Form("")):
        try:
            check_csrf(csrf_token)
            if vault.forget(state.home, state.messaging.network):
                state.flash(
                    "Removed from this computer's credential store. You will be "
                    "asked for the passphrase from now on -- make sure you have it.",
                    "ok")
            else:
                state.flash("It was not saved on this computer.", "info")
        except ValueError as exc:
            state.flash(str(exc), "err")
        return RedirectResponse("/", status_code=303)

    @app.post("/change-passphrase")
    def change_pass(request: Request, current: str = Form(""), new: str = Form(""),
                    confirm: str = Form(""), csrf_token: str = Form("")):
        try:
            check_csrf(csrf_token)
            # If it is saved on this machine, the user does not need to know it.
            if not current:
                current = state.reveal_passphrase() or ""
            if not current:
                raise KeyError_(
                    "enter your current passphrase. It is not saved on this "
                    "computer, so it cannot be filled in for you."
                )
            new = new.strip()
            if new != confirm:
                raise KeyError_("the new passphrases do not match")
            identity = change_passphrase(state.key_path, current, new)
            state.identity = identity
            if state.passphrase_remembered:
                vault.remember(state.home, state.messaging.network, new)
            state.flash("Passphrase changed.", "ok")
        except (KeyError_, ValueError) as exc:
            state.flash(str(exc), "err")
        return RedirectResponse("/", status_code=303)

    @app.post("/reset-identity")
    def reset_identity(request: Request, understand: str = Form(""),
                       csrf_token: str = Form("")):
        """Discard the identity and start again.

        The only route available when a passphrase is forgotten and was never
        saved: the secret key is encrypted with it, so there is nothing to
        recover. This does not "reset" anything -- it abandons one identity and
        makes another, and the interface says so before doing it.
        """
        try:
            check_csrf(csrf_token)
            if understand != "yes":
                raise ValueError("tick the box to confirm you understand what is lost")
            if state.key_path.exists():
                # Keep the old key rather than deleting it: the passphrase may
                # yet turn up, and deleting it would make that useless.
                import time
                archive = state.key_path.with_name(
                    f"{state.key_path.stem}.old-{int(time.time())}.key")
                state.key_path.replace(archive)
                vault.forget(state.home, state.messaging.network)
                state.lock()
                state.flash(
                    f"The old identity has been set aside as {archive.name} in case "
                    "the passphrase turns up. Create a new one below.", "ok")
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
               saved: str = Form(""), remember: str = Form(""), mode: str = Form("chosen"),
               csrf_token: str = Form("")):
        try:
            check_csrf(csrf_token)
            passphrase = passphrase.strip()
            if not passphrase:
                raise KeyError_("a passphrase is required")

            # Which path the user took comes from the form, not from inspecting
            # the passphrase. Inferring it from entropy was wrong: the
            # conservative estimate for a chosen passphrase caps at exactly 60,
            # so any self-chosen passphrase of 30-odd characters containing a
            # hyphen was misread as generated and then demanded a checkbox the
            # chosen-passphrase form does not show. There was no way out of it.
            if mode == "generated":
                if not saved:
                    raise KeyError_(
                        "tick the box to confirm you have saved the passphrase -- "
                        "it cannot be shown again"
                    )
            else:
                if passphrase != confirm:
                    raise KeyError_("passphrases do not match")
                if len(passphrase) < 8:
                    raise KeyError_("use at least 8 characters: this key is long-lived")
            identity = Identity.generate()
            save_identity(state.key_path, identity, passphrase)
            state.identity = identity
            # Remember where the chain was. Nothing written before this instant
            # can be addressed to a key that did not yet exist, so scanning need
            # never look further back -- the difference between a few hundred
            # blocks and every block ever mined.
            try:
                with state.messaging.rpc() as rpc:
                    height = rpc.get_block_count()
                with state.store() as store:
                    store.set_meta(f"identity_height:{state.messaging.network}", str(height))
            except Exception:
                pass   # only an optimisation; a missing value just scans further back
            if remember:
                try:
                    vault.remember(state.home, state.messaging.network, passphrase)
                    state.flash(
                        "Identity created, and the passphrase is saved on this "
                        "computer so you will not be asked for it again. You can "
                        "view it any time from this page.", "ok")
                except RuntimeError as exc:
                    state.flash(
                        f"Identity created, but the passphrase could not be saved: {exc}. "
                        "Keep your written copy.", "err")
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
            recipient_key = _resolve_recipient(state, recipient)
            plan = plan_message(state.identity, recipient_key, body.encode())
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
            recipient_key = _resolve_recipient(state, recipient)
            plan = plan_message(state.identity, recipient_key, body.encode())
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

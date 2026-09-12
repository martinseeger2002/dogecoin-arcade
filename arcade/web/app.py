"""The DogecoinArcade web interface.

Messenger only, for now. The Exchange, NFT and Inscription sections are shown
but disabled, because the engines behind them (M3-M5) do not exist yet -- a menu
that pretends otherwise would be worse than one that says so.
"""

from __future__ import annotations

import base64
import datetime as dt
import time
import html
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    HTMLResponse, JSONResponse, RedirectResponse, Response,
)
from fastapi.templating import Jinja2Templates

from .. import backup, media, wallet as walletlib
from ..config import NETWORKS, MainnetRefused, WrongChain
from ..messaging import contact, content, group
from ..script import b58check_decode
from ..messaging.derive import DerivationError, derive_identity
from ..messaging.envelope import MAX_ANNOUNCE_NAME, build_key_announcement
from ..messaging.keys import fingerprint_of
from ..messaging.miner import Miner, MiningError
from ..messaging.scanner import Scanner
from ..messaging.sender import (
    MessageSender, SendError, describe_duration, estimate_send_seconds,
    funded_address, plan_message, record_sent, recent_block_seconds,
)
from .state import AppState

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Sections that exist, and sections that do not. Shown honestly rather than
# hidden, so the shape of the finished product is visible.
#: (path, label, chain, built). `chain` is shown in the interface on every page,
#: because a user who cannot tell whether an action spends testnet or real coins
#: is one misclick from a bad day (D-012).
NAV = [
    ("/",             "Overview",     None,        True),
    ("/messages",     "Messages",     "testnet",   True),
    ("/contacts",     "Address book", None,        True),
    ("/groups",       "Public",       None,        True),
    ("/backup",       "Backup",       None,        True),
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
                      messaging=messaging_status(), ledger=ledger_status(), stats=stats)

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

    @app.get("/messages", response_class=HTMLResponse)
    def messages(request: Request):
        threads = []
        if state.store_path.exists() and state.unlocked:
            with state.store() as store:
                threads = store.conversations(state.identity.fingerprint)
        return render(request, "messages.html", threads=threads, thread=None,
                      peer=None, when=_when, fingerprint_of=fingerprint_of)

    @app.get("/messages/{peer_hex}", response_class=HTMLResponse)
    def conversation(request: Request, peer_hex: str):
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
                    store.thread(state.identity.fingerprint, peer_key))
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
    async def send_in_thread(request: Request, peer_hex: str, body: str = Form(""),
                             confirmed: str = Form(""), csrf_token: str = Form(""),
                             share_profile: str = Form(""),
                             attachment: UploadFile | None = File(None),
                             attached_name: str = Form(""),
                             attached_type: str = Form(""),
                             attached_b64: str = Form("")):
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
                file_bytes = await attachment.read()
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
                # Only the first chunk is built for the preview. The rest cannot
                # be: each one spends the change of the one before it, so its
                # input does not exist until that one is broadcast. Building them
                # all up front -- which this did -- produced transactions that
                # spent the same output twice.
                prepared = [sender.prepare(address, plan.chunk_payloads[0])]

                # A plain message on testnet goes straight out. There is nothing
                # to weigh up: the coins are free, it is one transaction, and it
                # is gone the instant it is broadcast either way -- a review step
                # only makes sending feel like filing paperwork.
                #
                # A file is different and keeps its confirmation: it costs dust
                # that can never be spent again, and a chunked send waits a block
                # between transactions, so it can take minutes. Both of those are
                # worth knowing BEFORE rather than discovering after.
                immediate = (not file_bytes
                             and plan.transactions == 1
                             and not state.messaging.is_mainnet)
                if immediate or confirmed == "yes":
                    txids = sender.send_all(address, plan.chunk_payloads)
                    # Keep our own plaintext: the sealed box is to the recipient,
                    # so we could never read this back off the chain ourselves.
                    with state.store() as store:
                        record_sent(store, txids[0], peer_key,
                                    state.identity.fingerprint,
                                    (body or f"[sent {file_name}]").encode())
                    state.flash(f"Sent in {len(txids)} transaction(s).", "ok")
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
                    store.thread(state.identity.fingerprint, bytes.fromhex(peer_hex)))
                peer = {"pubkey": bytes.fromhex(peer_hex), "hex": peer_hex,
                        "name": store.contact_name(bytes.fromhex(peer_hex)),
                        "contact_id": (lambda r: r["id"] if r else None)(
                            store.contact_by_key(bytes.fromhex(peer_hex))),
                        "fingerprint": fingerprint_of(bytes.fromhex(peer_hex)),
                        "code": contact.encode(state.messaging.network, bytes.fromhex(peer_hex))}
        # What the confirmation needs to say about a file: how long, and why.
        timing = None
        if plan is not None and file_bytes:
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
            }
        return render(request, "messages.html", threads=threads, thread=items, peer=peer,
                      when=_when, fingerprint_of=fingerprint_of, prepared=prepared,
                      plan=plan, draft=body, error=error, timing=timing,
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
                prepared = [sender.prepare(address, p) for p in plan.chunk_payloads]
                if confirmed == "yes":
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
    def contacts_page(request: Request, edit: int | None = None):
        people, editing = [], None
        if state.store_path.exists():
            with state.store() as store:
                people = [_contact_view(row) for row in store.contacts()]
                if edit:
                    row = store.contact_by_id(edit)
                    editing = _contact_view(row) if row else None
        published = []
        if state.store_path.exists():
            with state.store() as store:
                published = []
                rows = store.unknown_published_keys()
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
                        # The name as published, read from the announcement
                        # itself rather than from a contact row. Looking it up in
                        # the address book could only ever find names for people
                        # already in it, which is precisely who this list leaves
                        # out.
                        "name": row["name"] or "",
                    })
        return render(request, "contacts.html", people=people, editing=editing,
                      published=published, when=_when,
                      announce_limit=MAX_ANNOUNCE_NAME)

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
        for label, value, want_mainnet in (
            ("Testnet", testnet_address, False), ("Mainnet", mainnet_address, True)
        ):
            if value:
                problem = _check_address(value, mainnet=want_mainnet)
                if problem:
                    state.flash(f"{label} address: {problem}", "err")
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

    @app.post("/profile")
    def set_profile(request: Request, name: str = Form(""), csrf_token: str = Form("")):
        try:
            check_csrf(csrf_token)
            state.set_profile_name(name)
            state.flash(
                f"New contacts will see you as {name.strip()}." if name.strip()
                else "Your name will no longer be sent to new contacts.", "ok")
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except ValueError as exc:
            state.flash(str(exc), "err")
        return RedirectResponse("/contacts", status_code=303)

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
        })

    # --- public group posts ---------------------------------------------------
    # The one part of this application that is NOT encrypted, and the interface
    # says so on every screen where a post can be written. Runs on both chains:
    # D-010 keeps the *Messenger* on testnet permanently, but a public post
    # carries no key material and reveals nothing that publishing it does not
    # already reveal, so mainnet is a cost decision rather than a safety one.

    @app.get("/groups", response_class=HTMLResponse)
    def groups(request: Request, which: str = "messaging", channel: str = ""):
        chain = state.ledger if which == "ledger" else state.messaging
        channel = (channel or group.DEFAULT_CHANNEL).strip() or group.DEFAULT_CHANNEL
        posts, channels, balance = [], [], None
        if state.store_path.exists():
            with state.store() as store:
                posts = _with_media(store, store.group_posts(chain.network, channel))
                channels = store.group_channels(chain.network)
        try:
            with chain.rpc() as rpc:
                balance = float(rpc.call("getbalance") or 0)
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except Exception:
            balance = None
        return render(request, "groups.html", which=which, chain=chain,
                      channel=channel, posts=posts, channels=channels,
                      balance=balance, when=_when,
                      room=group.max_text_bytes(channel, state.profile_name),
                      nickname=state.profile_name)

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
    async def group_post_send(request: Request, which: str = Form("messaging"),
                              channel: str = Form(""), text: str = Form(""),
                              confirmed: str = Form(""), csrf_token: str = Form(""),
                              attachment: UploadFile | None = File(None),
                              attached_name: str = Form(""),
                              attached_type: str = Form(""),
                              attached_b64: str = Form("")):
        chain = state.ledger if which == "ledger" else state.messaging
        channel = (channel or group.DEFAULT_CHANNEL).strip() or group.DEFAULT_CHANNEL
        prepared, error, plan = None, None, None
        file_bytes, file_name, file_type = b"", attached_name, attached_type
        try:
            check_csrf(csrf_token)
            if attachment is not None and attachment.filename:
                file_bytes = await attachment.read()
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
                if confirmed == "yes":
                    if plan.transactions == 1:
                        txids = [sender.broadcast(prepared)]
                    else:
                        txids = sender.send_all(address, plan.payloads)
                    with state.store() as store:
                        store.add_group_post(
                            chain.network, channel, txids[0], 0, int(time.time()),
                            address, state.profile_name, text, mine=True,
                            file_name=group._safe_name(file_name) if file_bytes else "",
                            file_type=file_type if file_bytes else "",
                            file_data=file_bytes or None)
                    state.flash(
                        f"Posted to #{channel} in "
                        f"{len(txids)} transaction{'' if len(txids) == 1 else 's'}. "
                        f"It is public and permanent.", "ok")
                    return RedirectResponse(
                        f"/groups?which={which}&channel={channel}", status_code=303)
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except Exception as exc:
            error = str(exc)

        posts, channels, balance = [], [], None
        if state.store_path.exists():
            with state.store() as store:
                posts = _with_media(store, store.group_posts(chain.network, channel))
                channels = store.group_channels(chain.network)
        try:
            with chain.rpc() as rpc:
                balance = float(rpc.call("getbalance") or 0)
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
        except Exception:
            balance = None
        return render(request, "groups.html", which=which, chain=chain,
                      channel=channel, posts=posts, channels=channels,
                      balance=balance, when=_when, prepared=prepared, error=error,
                      plan=plan, draft=text, room=group.max_text_bytes(channel, state.profile_name),
                      nickname=state.profile_name,
                      attached_b64=base64.b64encode(file_bytes).decode() if file_bytes else "",
                      attached_name=file_name, attached_type=file_type)

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
            payload = build_key_announcement(
                state.identity.public_bytes, home_hash, state.profile_name)
            with state.messaging.rpc() as rpc:
                if not Miner(rpc, state.messaging.params).status().funded:
                    raise ValueError("no spendable coins yet -- see Wallet")
                sender = MessageSender(rpc, state.messaging.params)
                prepared = sender.prepare(
                    funded_address(rpc, prefer=home), payload, class_c=True,
                    change_address=home)
                if confirmed == "yes":
                    txid = sender.broadcast(prepared)
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
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
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
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
        except HTTPException:
            raise          # a rejected form is a 400, not an error page
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
        if message_id and not item.get("mine"):
            summary = store.attachment_summary(message_id)
            if summary is not None:
                info = {"id": message_id, "name": summary["name"],
                        "type": summary["content_type"], "size": summary["size"],
                        "kind": None, "label": ""}
                # Decide from the bytes, here, once -- the template must never be
                # in a position to render something on a sender's say-so.
                row = store.attachment_for(message_id)
                if row is not None:
                    found = media.renderable(bytes(row["data"]))
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


def _when(ts: int | None = None) -> str:
    """A timestamp, or now when called with nothing."""
    moment = dt.datetime.now() if ts is None else dt.datetime.fromtimestamp(ts)
    return moment.strftime("%Y-%m-%d %H:%M")

"""This arcade's side of the welcome (arcade/welcome.py): asking for its first PEP test, noticing it was paid, and
paying another arcade's ask from the node's own wallet.

- **The ask** (`tick`, every minute from mesh/welcomeshare.py): an arcade whose payout address on the messaging chain
  holds less than welcome.AMOUNT, and whose node wallet holds less too, asks once, as soon as it links to another
  arcade. Setting `welcome:asked` remembers it, so it never asks by itself again; after ASK_DAYS without an answer the
  operator may ask once more by hand (`ask_again`).
- **Paid** (`tick` too): the asker looks at each payment it was told of with its own Core (or the one it borrows while
  it catches up): one in a block that pays its address closes the ask, with every such payment in that first block.
  Coins that came another way (a block it mined) close it with no payments named, so nobody scores.
- **Paying** (`send`): 1 PEP test from the node's Core wallet on the messaging chain, then the `sent` record.
"""
from __future__ import annotations

import contextlib
import logging
import time

from .. import welcome as welcomelib

log = logging.getLogger("arcade.web")

ASKED = "welcome:asked"


class WelcomeError(ValueError):
    """Why a payment or an ask is not made, in words for the operator."""


def chain_of(state):
    """The messaging chain as the index knows it (Pepecoin test)."""
    return next(c for c in state.token_chains if c.network == state.messaging.network)


def my_address(state) -> str:
    try:
        return state.payout_address(state.messaging) or ""
    except Exception:                                  # noqa: BLE001
        return ""


def address_coins(state, address: str) -> int:
    """What `address` holds on the messaging chain, by the index (koinu). 0 when it cannot be read."""
    from .. import utxos as utxoslib
    if not address:
        return 0
    try:
        with contextlib.closing(state.token_index(chain_of(state)).open()) as db:
            return sum(int(c["value"]) for c in utxoslib.unspent(db, address))
    except Exception:                                  # noqa: BLE001 -- a node still building its index
        return 0


def wallet_coins(state) -> int:
    """The node's own Core wallet on the messaging chain (koinu); 0 when it cannot be read."""
    try:
        with state.messaging.rpc() as rpc:
            return int(round(float(rpc.call("getbalance")) * 100_000_000))
    except Exception:                                  # noqa: BLE001
        return 0


def funded(state) -> bool:
    """Whether this arcade has its first PEP test: on its payout address, or in its node wallet."""
    return address_coins(state, my_address(state)) >= welcomelib.AMOUNT or wallet_coins(state) >= welcomelib.AMOUNT


def _my_tag(state) -> str:
    """The operator's @name as a record carries it: shaped by the tag rules, else none."""
    from ..mesh.operators import said_tag
    try:
        return said_tag(state.operator_tag() or "")
    except Exception:                                  # noqa: BLE001
        return ""


def _pays(tx: dict, address: str) -> bool:
    for out in tx.get("vout") or []:
        spk = out.get("scriptPubKey") or {}
        if address == spk.get("address") or address in (spk.get("addresses") or []):
            return True
    return False


def _look(state, txids: list[str], address: str) -> list[tuple[str, str, int]]:
    """(txid, block hash, height) for each payment in a block that pays `address`."""
    found = []
    with state.messaging.rpc() as rpc:
        for txid in txids:
            try:
                tx = rpc.call("getrawtransaction", txid, 1)
                block = tx.get("blockhash") or ""
                if not block or not _pays(tx, address):
                    continue
                height = int(rpc.call("getblockheader", block)["height"])
                found.append((txid, block, height))
            except Exception:                          # noqa: BLE001 -- not in a block yet, or not here
                continue
    return found


def tick(state, book: welcomelib.Book, key, linked: int, now: float | None = None) -> list[dict]:
    """This arcade's own new records: its ask, or its ask closed. `linked`: how many arcades it links to now."""
    now = time.time() if now is None else now
    me = bytes(key.verify_key).hex()
    if book.closed_of(me) is not None:
        return []
    ask = book.ask_of(me)
    address = my_address(state)
    if ask is None:
        if state.setting(ASKED) or not linked or not address or funded(state):
            return []
        return [r for r in (_ask(state, book, key, address, now),) if r]
    found = _look(state, [s["txid"] for s in book.sent_to(me)], ask["addr"])
    paid = welcomelib.paid_block(found)
    if paid is None and not funded(state):
        return []
    block, height, txids = paid if paid else ("", 0, [])
    closed = book.add(welcomelib.sign({"k": "closed", "at": int(now), "block": block, "height": height,
                                       "txids": txids}, key), now)
    if closed is not None:
        log.info("welcome: paid%s", f" in block {height} ({len(txids)} payment(s))" if txids else " another way")
    return [closed] if closed else []


def _ask(state, book, key, address: str, now: float) -> dict | None:
    rec = book.add(welcomelib.sign({"k": "ask", "at": int(now), "addr": address, "tag": _my_tag(state)}, key),
                   now)
    if rec is not None:
        state.set_setting(ASKED, int(now))
        log.info("welcome: asked the mesh for 1 PEP test at %s", address)
    return rec


def ask_again(state, book, key, now: float | None = None) -> dict:
    """The operator's one ask by hand, once the first went unanswered for ASK_DAYS (or was never made)."""
    now = time.time() if now is None else now
    me = bytes(key.verify_key).hex()
    if book.closed_of(me) is not None:
        raise WelcomeError("this arcade was welcomed already")
    if book.ask_of(me) is not None:
        raise WelcomeError("this arcade's ask is still open")
    if funded(state):
        raise WelcomeError("this arcade has its PEP test already")
    address = my_address(state)
    if not address:
        raise WelcomeError("this arcade has no address on Pepecoin test yet")
    rec = _ask(state, book, key, address, now)
    if rec is None:
        raise WelcomeError("that ask could not be made")
    return rec


def send(state, book: welcomelib.Book, key, asker: str, now: float | None = None) -> dict:
    """Pay `asker`'s open ask 1 PEP test from the node's wallet: the `sent` record (kept, not yet passed on)."""
    from .. import blockminer
    now = time.time() if now is None else now
    me = bytes(key.verify_key).hex()
    ask = book.ask_of(asker)
    if ask is None or book.closed_of(asker) is not None:
        raise WelcomeError("that arcade's ask is closed or gone")
    if asker == me:
        raise WelcomeError("that is this arcade's own ask")
    if any(s["a"] == me for s in book.sent_to(asker)):
        raise WelcomeError("this arcade has paid that ask already")
    try:
        blockminer.output_script(ask["addr"], state.messaging.params)
    except Exception as exc:                          # noqa: BLE001
        raise WelcomeError(f"that ask's address is not a Pepecoin test address: {exc}") from exc
    try:
        with state.messaging.rpc() as rpc:
            txid = str(rpc.call("sendtoaddress", ask["addr"], welcomelib.AMOUNT / 100_000_000))
    except Exception as exc:                          # noqa: BLE001
        raise WelcomeError(f"the node's wallet could not pay: {exc}") from exc
    rec = book.add(welcomelib.sign({"k": "sent", "at": int(now), "ask": asker, "txid": txid,
                                    "tag": _my_tag(state)}, key), now)
    if rec is None:
        raise WelcomeError(f"paid in {txid}, but the record could not be kept")
    log.info("welcome: paid %s's ask in %s", asker[:12], txid)
    return rec


def view(state, book: welcomelib.Book | None, key, now: float | None = None) -> dict:
    """What Admin shows: this arcade's own welcome, the open asks of others, and the board."""
    now = time.time() if now is None else now
    address = my_address(state)
    coins = address_coins(state, address)
    if book is None or key is None:
        return {"mesh": False, "net": state.messaging.network, "address": address, "coins": coins,
                "funded": funded(state),
                "mine": None, "open": [], "scores": []}
    me = bytes(key.verify_key).hex()
    tags = book.tags()
    ask, closed = book.ask_of(me), book.closed_of(me)
    sent_to_me = book.sent_to(me)
    mine = {"asked": bool(state.setting(ASKED)), "open": ask is not None and closed is None,
            "at": (ask or {}).get("at"), "closed": closed is not None,
            "senders": [tags.get(s["a"]) or s["a"][:12] for s in sent_to_me],
            "welcomed_by": [tags.get(s["a"]) or s["a"][:12] for s in sent_to_me
                            if closed and s["txid"] in closed["txids"]],
            "can_ask": ask is None and closed is None}
    rows = []
    for a in book.open_asks():
        if a["a"] == me:
            continue
        paid = book.sent_to(a["a"])
        rows.append({"node": a["a"], "tag": a["tag"], "address": a["addr"], "at": a["at"],
                     "paid_by_me": any(s["a"] == me for s in paid),
                     "paid_by": [tags.get(s["a"]) or s["a"][:12] for s in paid]})
    board = welcomelib.scores(book.all())
    for row in board:
        row["me"] = row["node"] == me
        row["node"] = row["node"][:12]
    return {"mesh": True, "me": me[:12], "net": state.messaging.network, "address": address, "coins": coins,
            "funded": funded(state),
            "mine": mine, "open": rows, "scores": board, "amount": welcomelib.AMOUNT}

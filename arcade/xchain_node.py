"""The node's side of the cross-chain book (arcade/xchain.py): where deposits are
sent, whether they have arrived, and the transactions that pay people out.

Everything here reads or writes a chain; xchain.Book keeps the bookkeeping. The
watcher calls `Clerk.tick()` every pass: deposits that have their confirmations
open their orders (and meet the book), and what the book owes is built and sent.

Safety, in the order it is checked (2026-10-06: "there shouldn't be a
limit, but you need to make sure there are no bugs"):

* A deposit counts only when it is on the chain with CONFIRMATIONS and delivers
  EXACTLY what its order says to this node's exchange address. A deposit that
  delivers something else is refunded as it arrived and the order fails.
* A payout is built only if the wallet still holds everything the node owes
  everybody on that chain plus this payout's fee (`_solvent`). Otherwise it
  waits, and the status page says why -- it never pays one person out of what
  is held for another.
* A payout's inputs are locked in the wallet the moment it is built, so nothing
  else this node builds can spend them before the saved bytes are broadcast.
* Broadcasting saved bytes that are already in the pool or a block is success.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from . import payload as P
from . import inscriptions as inscriptionlib
from . import tokens as tokenlib
from . import wallet as walletlib
from .xchain import AWAITING, CANCELLED, CONFIRMATIONS, OPEN, Book

log = logging.getLogger(__name__)

COIN = 100_000_000
DEPOSIT_PATIENCE = 24 * 3600     # a deposit not seen on its chain after this fails its order
LABEL = "arcade-xchain"


class Clerk:
    def __init__(self, state: Any, book: Book):
        self.state, self.book = state, book

    # --- the two chains ---------------------------------------------------------

    @property
    def testnet(self):
        return self.state.messaging

    @property
    def main(self):
        return self.state.ledger

    def chain(self, which: str):
        return self.main if which == "main" else self.testnet

    def address(self, which: str) -> str:
        """This node's exchange address on `which`, made once and remembered."""
        key = f"xchain:address:{which}"
        known = self.state.setting(key)
        if known:
            return known
        with self.chain(which).rpc() as rpc:
            made = rpc.call("getnewaddress", LABEL)
        self.state.set_setting(key, made)
        return made

    # --- deposits -----------------------------------------------------------------

    def check_deposit(self, order: dict) -> tuple[str, str, int]:
        """("ok" | "wait" | "wrong", why, what actually arrived). `wrong` means
        it is on the chain, confirmed, and is not what the order says."""
        txid, kind = order["deposit_txid"], order["kind"]
        if order["side"] == "buy" or kind == "coin":
            which = "main" if order["side"] == "buy" else "testnet"
            want = order["pepe"] if order["side"] == "buy" else order["amount"]
            to = self.address(which)
            try:
                with self.chain(which).rpc() as rpc:
                    tx = rpc.call("getrawtransaction", txid, 1)
            except Exception:
                return "wait", "not seen on the chain yet", 0
            got = 0
            for out in tx.get("vout", []):
                spk = out.get("scriptPubKey") or {}
                addrs = spk.get("addresses") or ([spk["address"]] if spk.get("address") else [])
                if to in addrs:
                    got += int(round(float(out.get("value", 0)) * COIN))
            if (tx.get("confirmations") or 0) < CONFIRMATIONS[which]:
                return "wait", f"{tx.get('confirmations') or 0} of {CONFIRMATIONS[which]} confirmations", got
            if got != want:
                return "wrong", f"it paid {got} to the exchange address, the order says {want}", got
            return "ok", "", got
        index = self.state.token_index(self.testnet)
        height = index.indexed_height() or 0
        to = self.address("testnet")
        if kind == "token":
            tx = index.transaction(txid)
            if tx is None:
                return "wait", "not in a block yet", 0
            confs = height - tx["block_height"] + 1
            if confs < CONFIRMATIONS["testnet"]:
                return "wait", f"{confs} of {CONFIRMATIONS['testnet']} confirmations", 0
            got = int(tx.get("amount") or 0)
            right = (tx.get("valid") and tx.get("message_type") == 0 and str(tx.get("property_id")) == str(order["asset"])
                     and tx.get("reference") == to)
            if not right:
                return "wrong", "it is not a send of this token to the exchange address", (
                    got if tx.get("valid") and tx.get("reference") == to and str(tx.get("property_id")) == str(order["asset"]) else 0)
            if got != order["amount"]:
                return "wrong", f"it sent {got}, the order says {order['amount']}", got
            return "ok", "", got
        # an NFT: the move the engine filed for this transaction
        with index.open() as db:
            moved = db.conn.execute(
                "SELECT * FROM inscription_move WHERE txid=? AND inscription=?",
                (txid, order["asset"])).fetchone()
        if moved is None:
            return "wait", "not in a block yet", 0
        confs = height - moved["block_height"] + 1
        if confs < CONFIRMATIONS["testnet"]:
            return "wait", f"{confs} of {CONFIRMATIONS['testnet']} confirmations", 0
        if moved["to_address"] != to:
            return "wrong", "it moved the NFT somewhere other than the exchange address", 0
        return "ok", "", 1

    def watch_deposits(self) -> int:
        opened = 0
        for order in self.book.awaiting():
            state, why, got = self.check_deposit(order)
            if state == "ok":
                self.book.count_deposit(order["id"])
                opened += 1
            elif state == "wrong":
                self.book.fail_deposit(order["id"], why)
                self.book.refund_received(order, got)
            elif time.time() - order["created"] > DEPOSIT_PATIENCE and why.startswith("not"):
                self.book.fail_deposit(order["id"], "the deposit never reached the chain")
        # a deposit that confirmed after its order was cancelled goes back
        for order in self.book.cancelled_with_deposit():
            state, _, got = self.check_deposit(order)
            if state == "ok":
                self.book.refund_late(order["id"])
            elif state == "wrong":
                self.book.refund_received(order, got)
        return opened

    # --- paying out -----------------------------------------------------------------

    def _solvent(self, payout: dict, fee_room: int) -> None:
        """Raise unless the wallet holds all the node owes on this chain plus this fee."""
        owed = self.book.owed()
        if payout["chain"] == "main" or payout["kind"] == "coin":
            which = payout["chain"]
            need = sum(v for (c, k, _), v in owed.items() if c == which and k in ("pepe", "coin")) + fee_room
            with self.chain(which).rpc() as rpc:
                have = int(round(float(rpc.call("getbalance") or 0) * COIN))
            if have < need:
                raise RuntimeError(f"the {which} wallet holds {have} and owes {need} with this fee; "
                                   f"waiting for the operator to top it up")
        elif payout["kind"] == "token":
            index = self.state.token_index(self.testnet)
            have = index.balance(self.address("testnet"), int(payout["asset"]))
            need = owed.get(("testnet", "token", payout["asset"]), 0)
            if have < need:
                raise RuntimeError(f"the exchange address holds {have} of token {payout['asset']} "
                                   f"and owes {need}")
        else:
            index = self.state.token_index(self.testnet)
            row = index.inscription(payout["asset"])
            if row is None or row["owner"] != self.address("testnet"):
                raise RuntimeError("the exchange address does not hold that NFT")

    FUEL = 2 * COIN                  # testnet coins the exchange address keeps for its own fees
    TOP_UP = 5.0

    def _fuelled(self, rpc) -> None:
        """A token or NFT payout is sent FROM the exchange address, which pays its own
        fee: keep testnet coins there, topped up from this node's wallet (found by the
        end-to-end test: a fresh exchange address could not pay out at all). Raises,
        so the payout waits a pass, while a top-up confirms."""
        addr = self.address("testnet")
        spendable = sum(int(round(float(u.get("amount", 0)) * COIN))
                        for u in rpc.call("listunspent", 1, 9_999_999, [addr]) or [])
        if spendable >= self.FUEL:
            return
        pending = sum(int(round(float(u.get("amount", 0)) * COIN))
                      for u in rpc.call("listunspent", 0, 0, [addr]) or [])
        if pending < self.FUEL:
            rpc.call("sendtoaddress", addr, self.TOP_UP)
        raise RuntimeError("topping up the exchange address with testnet coins for fees; "
                           "the payout goes out once that confirms")

    def build(self, payout: dict) -> tuple[str, str]:
        which = payout["chain"]
        chain = self.chain(which)
        with chain.rpc() as rpc:
            if payout["kind"] in ("pepe", "coin"):
                self._solvent(payout, fee_room=COIN // 10)
                prepared = walletlib.prepare_send(rpc, payout["to_addr"], int(payout["amount"]))
            elif payout["kind"] == "token":
                self._solvent(payout, 0)
                self._fuelled(rpc)
                prepared = tokenlib.TokenSender(rpc, chain.params).prepare(
                    self.address("testnet"), tokenlib.send_payload(int(payout["asset"]), int(payout["amount"])),
                    payout["to_addr"])
            elif payout["kind"] == "nft":
                self._solvent(payout, 0)
                self._fuelled(rpc)
                body = P.AnyData(data=inscriptionlib.Transfer(
                    txid=bytes.fromhex(payout["asset"])).encode()).encode()
                prepared = tokenlib.TokenSender(rpc, chain.params).prepare(
                    self.address("testnet"), body, payout["to_addr"])
            else:
                raise RuntimeError(f"unknown payout kind {payout['kind']}")
            # hold its inputs: nothing else this node builds may spend them first
            spent = [{"txid": i["txid"], "vout": i["vout"]}
                     for i in (prepared.decoded or {}).get("vin", []) if "txid" in i]
            if spent:
                rpc.call("lockunspent", False, spent)
        return prepared.hex, prepared.txid

    def broadcast(self, payout: dict) -> str:
        with self.chain(payout["chain"]).rpc() as rpc:
            try:
                return rpc.call("sendrawtransaction", payout["raw"])
            except Exception as exc:
                text = str(exc).lower()
                if "already in block chain" in text or "already known" in text or "txn-already" in text:
                    return payout["txid"]
                try:                                    # already ours on the chain?
                    rpc.call("getrawtransaction", payout["txid"], 1)
                    return payout["txid"]
                except Exception:
                    raise exc from None

    def tick(self) -> dict:
        self.book.sweep_unsigned()
        opened = self.watch_deposits()
        sent = self.book.pay(self.build, self.broadcast)
        return {"opened": opened, "sent": len(sent)}

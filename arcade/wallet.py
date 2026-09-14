"""Sending and receiving coins through a node's wallet.

Deliberately does NOT use `sendtoaddress`. That RPC builds, signs and broadcasts
in one step, which would skip the only moment a person can check what they are
about to do. Instead:

    createrawtransaction -> fundrawtransaction -> signrawtransaction
    -> decoderawtransaction -> show the caller -> sendrawtransaction

Nothing is broadcast until a caller has seen the decoded transaction with its
outputs and fee and said yes. On mainnet that is the difference between a typo
and a loss.

This module never holds a private key: the node's wallet funds and signs.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from . import fees
from .rpc import RpcClient

COIN = 100_000_000


class WalletError(Exception):
    """The transaction could not be built, or the request made no sense."""


@dataclass
class PreparedSend:
    """A signed transaction, decoded, awaiting approval."""

    hex: str
    txid: str
    decoded: dict[str, Any]
    fee_sats: int
    size: int
    destination: str
    amount_sats: int
    change_sats: int

    @property
    def amount(self) -> Decimal:
        return Decimal(self.amount_sats) / COIN

    @property
    def fee(self) -> Decimal:
        return Decimal(self.fee_sats) / COIN

    @property
    def total(self) -> Decimal:
        return (Decimal(self.amount_sats) + Decimal(self.fee_sats)) / COIN


def parse_amount(text: str) -> int:
    """Parse a user-entered amount into satoshis.

    Decimal rather than float throughout: 0.1 + 0.2 in binary floating point is
    not 0.3, and a rounding error in a monetary amount is not acceptable even
    when it is small.
    """
    text = (text or "").strip().replace(",", "")
    if not text:
        raise WalletError("enter an amount")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise WalletError(f"{text!r} is not a number") from None
    if value <= 0:
        raise WalletError("the amount must be greater than zero")
    scaled = value * COIN
    if scaled != scaled.to_integral_value():
        raise WalletError("amounts cannot be finer than 0.00000001")
    return int(scaled)


def balance(rpc: RpcClient) -> dict[str, Decimal]:
    """Spendable and maturing balances, or an explanation if there is no wallet."""
    info = rpc.call("getwalletinfo")
    return {
        "spendable": Decimal(str(info.get("balance", 0))),
        "immature": Decimal(str(info.get("immature_balance", 0))),
        "unconfirmed": Decimal(str(info.get("unconfirmed_balance", 0))),
    }


def receive_address(rpc: RpcClient, label: str = "") -> str:
    """A fresh address to receive on.

    Fresh each time rather than reused: an address reused across payments links
    them together permanently for anyone reading the chain.
    """
    try:
        return str(rpc.call("getnewaddress", label))
    except Exception as exc:
        raise WalletError(f"could not get an address: {exc}") from None


def prepare_send(rpc: RpcClient, destination: str, amount_sats: int,
                 subtract_fee: bool = False) -> PreparedSend:
    """Build, fund and sign a payment. Does NOT broadcast."""
    if not destination.strip():
        raise WalletError("enter a destination address")

    validation = rpc.call("validateaddress", destination)
    if not validation.get("isvalid"):
        raise WalletError(
            f"{destination} is not a valid address on this chain. Check you have "
            "not pasted a mainnet address into testnet, or the reverse."
        )

    amount = Decimal(amount_sats) / COIN
    raw = rpc.call("createrawtransaction", [], {destination: float(amount)})

    options: dict[str, Any] = {}
    if subtract_fee:
        options["subtractFeeFromOutputs"] = [0]
    try:
        funded = fees.fund(rpc, raw, options)
    except Exception as exc:
        raise WalletError(
            f"could not fund the transaction: {exc}. The wallet may not hold "
            "enough spendable coins -- newly mined coins take 240 blocks to mature."
        ) from None

    # Change goes back where the coins came from. Left to itself the node parks
    # change on a fresh address, which is fine for a wallet that is only a
    # wallet -- and quietly empties the messaging identity: one plain send from
    # the Wallet page spent its 9,978-coin output and left it holding 5, and
    # the next picture could not be sent. Every message and token send in the
    # arcade already keeps change on its sender; this makes the plain send do
    # the same. Fund once to learn which coins the node chose, then fund again
    # with those exact inputs and the change address named.
    change_to = _largest_input_address(rpc, funded["hex"])
    if change_to is not None:
        chosen = [{"txid": vin["txid"], "vout": vin["vout"]}
                  for vin in rpc.call("decoderawtransaction", funded["hex"])["vin"]]
        raw = rpc.call("createrawtransaction", chosen, {destination: float(amount)})
        try:
            funded = fees.fund(rpc, raw, {**options, "changeAddress": change_to})
        except Exception as exc:
            raise WalletError(f"could not fund the transaction: {exc}") from None

    signed = rpc.call("signrawtransaction", funded["hex"])
    if not signed.get("complete"):
        raise WalletError(f"signing failed: {signed.get('errors')}")

    decoded = rpc.call("decoderawtransaction", signed["hex"])
    fee_sats = int(round(float(funded.get("fee", 0)) * COIN))

    sent = 0
    change = 0
    for out in decoded.get("vout", []):
        addresses = out.get("scriptPubKey", {}).get("addresses") or []
        value = int(round(float(out["value"]) * COIN))
        if destination in addresses:
            sent += value
        else:
            change += value

    return PreparedSend(
        hex=signed["hex"], txid=decoded["txid"], decoded=decoded,
        fee_sats=fee_sats, size=len(signed["hex"]) // 2,
        destination=destination, amount_sats=sent, change_sats=change,
    )


def _largest_input_address(rpc: RpcClient, funded_hex: str) -> str | None:
    """The wallet address that put the most value into a funded transaction."""
    decoded = rpc.call("decoderawtransaction", funded_hex)
    spent = {(vin["txid"], int(vin["vout"])) for vin in decoded.get("vin", [])}
    totals: dict[str, int] = {}
    for utxo in rpc.call("listunspent", 0, 9_999_999) or []:
        if (utxo["txid"], int(utxo["vout"])) in spent and utxo.get("address"):
            totals[utxo["address"]] = (totals.get(utxo["address"], 0)
                                       + int(round(float(utxo["amount"]) * COIN)))
    if not totals:
        return None
    return max(totals, key=lambda name: totals[name])


def broadcast(rpc: RpcClient, prepared: PreparedSend) -> str:
    """Send it. The caller must already have shown `prepared` and been told yes."""
    return str(rpc.call("sendrawtransaction", prepared.hex))


def recent(rpc: RpcClient, count: int = 15) -> list[dict[str, Any]]:
    """Recent wallet transactions, newest first."""
    try:
        entries = rpc.call("listtransactions", "*", count, 0)
    except Exception:
        return []
    return list(reversed(entries))

"""Building, funding, signing and broadcasting message transactions.

The flow follows the brief exactly, and the confirmation step is not optional:

    1. build our outputs          (this module)
    2. fundrawtransaction         (node adds inputs and change)
    3. signrawtransaction         (node signs -- we never hold keys)
    4. decoderawtransaction       (node decodes what we are about to send)
    5. show the caller the decoded transaction and the fee
    6. sendrawtransaction         ONLY after explicit confirmation
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import Params, require_messaging_network
from ..encoding import MAX_CLASS_B_PAYLOAD, encode_class_b, encode_class_c, max_class_c_payload
from ..payload import AnyData
from ..rpc import RpcClient
from ..txbuild import build_raw_tx, multisig_script, op_return_script, p2pkh_script
from .envelope import (
    Header, TYPE_CHUNK, TYPE_SINGLE, new_message_id, seal_ciphertext, seal_message,
)
from .keys import Identity

COIN = 100_000_000

# Three different thresholds exist, and using the wrong one produces a
# transaction the wallet silently refuses to build:
#
#   DEFAULT_HARD_DUST_LIMIT   0.001 PEP  policy/policy.h:81   relay/standardness
#   DEFAULT_DUST_LIMIT        0.01  PEP  policy/policy.h:70   extra-fee rule
#   DEFAULT_DISCARD_THRESHOLD 0.01  PEP  wallet/wallet.h:68   the WALLET's floor
#
# We fund through the node's wallet, so the binding constraint is the wallet's
# discard threshold, not the relay limit. fundrawtransaction fails with
# "Transaction amount too small" for anything below it. Found by the
# standardness tests, not by reading the source.
#
# The cost is softened by the fact that every Class B multisig output embeds the
# sender's own redeeming pubkey (encoding.cpp:37), so this value stays
# **spendable by the sender** and can be swept. Only the single marker output is
# genuinely spent.
OUTPUT_VALUE = 1_000_000     # 0.01 PEP
HARD_DUST = OUTPUT_VALUE     # kept as an alias; see above for why it is not 0.001


class SendError(Exception):
    """A transaction could not be built, funded or signed."""


@dataclass
class PreparedTx:
    """A signed transaction, decoded and ready for the caller to approve."""

    hex: str
    txid: str
    decoded: dict[str, Any]
    fee_sats: int
    size: int
    outputs: int

    @property
    def fee_coins(self) -> float:
        return self.fee_sats / COIN

    def summary(self) -> str:
        lines = [
            f"  txid      {self.txid}",
            f"  size      {self.size:,} bytes",
            f"  outputs   {self.outputs}",
            f"  fee       {self.fee_coins:.8f}  ({self.fee_sats:,} sats,"
            f" {self.fee_sats / max(self.size,1) * 1000 / COIN * COIN:.2f} sats/kB)",
        ]
        return "\n".join(lines)


@dataclass
class MessagePlan:
    """What sending a message will cost, before anything is built."""

    payload_bytes: int
    transactions: int
    chunked: bool
    packets: int = 0
    multisig_outputs: int = 0
    est_size: int = 0
    est_fee_sats: int = 0
    est_dust_sats: int = 0
    chunk_payloads: list[bytes] = field(default_factory=list)

    @property
    def est_total_coins(self) -> float:
        return (self.est_fee_sats + self.est_dust_sats) / COIN


def plan_message(
    sender: Identity, recipient_public: bytes, message: bytes
) -> MessagePlan:
    """Encrypt once, then decide how it must be carried.

    Encryption happens **before** chunking, never per chunk: per-chunk encryption
    would multiply the 132-byte overhead by the chunk count and expose the chunk
    structure to observers.
    """
    single = seal_message(sender, recipient_public, Header(type=TYPE_SINGLE), message)

    if len(single) <= MAX_CLASS_B_PAYLOAD - 4:      # 4 = AnyData header
        packets = -(-len(single) // 30)
        outs = -(-packets // 2)
        size = 148 + outs * 113 + 34 + 34 + 10
        return MessagePlan(
            payload_bytes=len(single), transactions=1, chunked=False,
            packets=packets, multisig_outputs=outs, est_size=size,
            est_fee_sats=int(size / 1000 * COIN / 100),
            est_dust_sats=(outs + 1) * OUTPUT_VALUE,
            chunk_payloads=[single],
        )

    # Too large for one transaction: chunk the finished ciphertext.
    msg_id = new_message_id()
    # Seal ONCE, then split the ciphertext. Framing is added per chunk below, so
    # seal_ciphertext (not seal_message) is used -- the latter would prepend a
    # header that the first chunk would then carry twice.
    body = seal_ciphertext(
        sender, recipient_public, Header(type=TYPE_CHUNK, msg_id=msg_id), message
    )
    capacity = MAX_CLASS_B_PAYLOAD - 4 - 18          # AnyData header + chunk header
    pieces = [body[i : i + capacity] for i in range(0, len(body), capacity)]

    payloads = []
    total = len(pieces)
    for index, piece in enumerate(pieces):
        # Countdown, Doginals-style: the FINAL chunk carries 0, so completion is
        # self-describing and an abandoned chain is always distinguishable.
        # clen records this chunk's exact ciphertext length so Class B padding on
        # the last chunk can be discarded.
        header = Header(
            type=TYPE_CHUNK, msg_id=msg_id, countdown=total - index - 1, clen=len(piece)
        )
        payloads.append(header.encode() + piece)

    outs_each = -(-(-(-capacity // 30)) // 2)
    size_each = 148 + outs_each * 113 + 34 + 34 + 10
    return MessagePlan(
        payload_bytes=len(body), transactions=total, chunked=True,
        packets=-(-len(body) // 30), multisig_outputs=outs_each * total,
        est_size=size_each * total,
        est_fee_sats=int(size_each * total / 1000 * COIN / 100),
        est_dust_sats=(outs_each + 1) * OUTPUT_VALUE * total,
        chunk_payloads=payloads,
    )


class MessageSender:
    """Builds and broadcasts message transactions through a node's wallet."""

    def __init__(self, rpc: RpcClient, params: Params):
        require_messaging_network(params)     # D-010, enforced here as well as in the CLI
        self.rpc = rpc
        self.params = params

    # --- building -------------------------------------------------------------

    def _class_b_outputs(self, sender_address: str, payload: bytes) -> list[tuple[int, bytes]]:
        """Marker output plus obfuscated multisig outputs carrying the payload."""
        pubkey = self._pubkey_for(sender_address)
        anydata = AnyData(data=payload).encode()
        groups = encode_class_b(sender_address, pubkey, anydata)

        # Derived from a fixed phrase, so every installation agrees without
        # configuration. See config.MARKER_SEED.
        outputs = [(OUTPUT_VALUE, p2pkh_script(self.params.marker))]
        for group in groups:
            outputs.append((OUTPUT_VALUE, multisig_script(list(group.keys), group.required)))
        return outputs

    def _pubkey_for(self, address: str) -> bytes:
        """The sender's own public key, for the redeeming slot of each multisig.

        Including it keeps every data output **spendable by the sender**
        (encoding.cpp:37), so the dust is recoverable rather than burned.
        """
        info = self.rpc.call("validateaddress", address)
        pubkey_hex = info.get("pubkey")
        if not pubkey_hex:
            raise SendError(
                f"the wallet has no public key for {address}; the address must be "
                "one the wallet owns and has used"
            )
        return bytes.fromhex(pubkey_hex)

    def _select_inputs(self, address: str, target: int) -> list[tuple[str, int]]:
        """Pick outputs belonging to `address` worth at least `target`.

        Class B seeds its obfuscation keystream with the SENDER address, and the
        sender is determined by "largest input by sum" (omnicore.cpp:984-991). If
        we let the wallet choose inputs freely it may fund from other addresses,
        the computed sender then differs from the one we seeded with, and the
        message is **permanently unreadable by anyone**. So we choose the inputs.
        """
        unspent = self.rpc.call("listunspent", 1, 9_999_999, [address])
        chosen: list[tuple[str, int]] = []
        total = 0
        for utxo in sorted(unspent, key=lambda u: -float(u["amount"])):
            chosen.append((utxo["txid"], int(utxo["vout"])))
            total += int(round(float(utxo["amount"]) * COIN))
            if total >= target:
                return chosen
        raise SendError(
            f"{address} holds {total / COIN:.8f}, which is short of the "
            f"{target / COIN:.8f} this transaction needs. Fund that address, or "
            f"pass --address for one that is funded."
        )

    def _verify_sender(self, decoded: dict[str, Any], expected: str) -> None:
        """Confirm the funded transaction really resolves to the seeded sender.

        Belt and braces over _select_inputs. A mismatch here would produce a
        message nobody can ever read, sitting on chain forever, so it must fail
        loudly before broadcast rather than after.
        """
        sums: dict[str, int] = {}
        for vin in decoded.get("vin", []):
            if "txid" not in vin:
                continue
            prev = self.rpc.call("getrawtransaction", vin["txid"], True)
            out = prev["vout"][int(vin["vout"])]
            addresses = out.get("scriptPubKey", {}).get("addresses") or []
            if not addresses:
                continue
            value = int(round(float(out["value"]) * COIN))
            sums[addresses[0]] = sums.get(addresses[0], 0) + value

        best, best_value = "", 0
        for address in sorted(sums):          # matches std::map iteration order
            if sums[address] > best_value:
                best, best_value = address, sums[address]

        if best != expected:
            raise SendError(
                f"the funded transaction resolves to sender {best!r}, but the payload "
                f"was obfuscated for {expected!r}. Broadcasting it would produce a "
                f"message nobody could read. Fund {expected} directly and retry."
            )

    def prepare(self, sender_address: str, payload: bytes, class_c: bool = False) -> PreparedTx:
        """Build, fund and sign one transaction. Does NOT broadcast."""
        if class_c:
            # Wrap in AnyData exactly as the Class B path does. The carrier
            # differs; the payload format must not, or the scanner cannot read
            # one of them. (It could not: key announcements were silently skipped
            # as an unknown payload type until an integration test caught it.)
            anydata = AnyData(data=payload).encode()
            if len(anydata) > max_class_c_payload():
                raise SendError(
                    f"payload of {len(payload)} bytes ({len(anydata)} wrapped) exceeds "
                    f"Class C capacity ({max_class_c_payload()})"
                )
            outputs = [(0, op_return_script(encode_class_c(anydata)))]
        else:
            outputs = self._class_b_outputs(sender_address, payload)

        # Choose our own inputs for Class B so the sender cannot drift; Class C
        # does not obfuscate, so the wallet may fund it however it likes.
        if class_c:
            inputs: list[tuple[str, int]] = []
        else:
            needed = sum(value for value, _ in outputs) + COIN     # outputs + fee headroom
            inputs = self._select_inputs(sender_address, needed)

        raw = build_raw_tx(inputs, outputs)

        # Return change to the sender address, on BOTH carriers. Otherwise every
        # send drains the address the obfuscation is seeded with -- including a
        # Class C key announcement, which does not obfuscate but happily spends
        # that address's coins and sends the change elsewhere. The next Class B
        # message then either fails outright or silently resolves to a different
        # sender. It also keeps the messaging identity's funds in one place.
        # (fundrawtransaction options, rpcwallet.cpp:2791.)
        funded = self.rpc.call("fundrawtransaction", raw, {"changeAddress": sender_address})
        if not funded or "hex" not in funded:
            raise SendError("fundrawtransaction failed; is the wallet funded?")
        fee_sats = int(round(float(funded.get("fee", 0)) * COIN))

        signed = self.rpc.call("signrawtransaction", funded["hex"])
        if not signed.get("complete"):
            raise SendError(f"signing failed: {signed.get('errors')}")

        decoded = self.rpc.call("decoderawtransaction", signed["hex"])
        if not class_c:
            self._verify_sender(decoded, sender_address)
        return PreparedTx(
            hex=signed["hex"],
            txid=decoded["txid"],
            decoded=decoded,
            fee_sats=fee_sats,
            size=len(signed["hex"]) // 2,
            outputs=len(decoded.get("vout", [])),
        )

    def broadcast(self, prepared: PreparedTx) -> str:
        """Send. Callers MUST have shown `prepared` to the user and got approval."""
        return str(self.rpc.call("sendrawtransaction", prepared.hex))

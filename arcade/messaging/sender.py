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

HARD_DUST = 100_000          # 0.001 PEP, policy/policy.h:81 -- what standardness enforces
COIN = 100_000_000


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
            est_dust_sats=(outs + 1) * HARD_DUST,
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
    capacity = MAX_CLASS_B_PAYLOAD - 4 - 16          # AnyData header + chunk header
    pieces = [body[i : i + capacity] for i in range(0, len(body), capacity)]

    payloads = []
    total = len(pieces)
    for index, piece in enumerate(pieces):
        # Countdown, Doginals-style: the FINAL chunk carries 0, so completion is
        # self-describing and an abandoned chain is always distinguishable.
        header = Header(type=TYPE_CHUNK, msg_id=msg_id, countdown=total - index - 1)
        payloads.append(header.encode() + piece)

    outs_each = -(-(-(-capacity // 30)) // 2)
    size_each = 148 + outs_each * 113 + 34 + 34 + 10
    return MessagePlan(
        payload_bytes=len(body), transactions=total, chunked=True,
        packets=-(-len(body) // 30), multisig_outputs=outs_each * total,
        est_size=size_each * total,
        est_fee_sats=int(size_each * total / 1000 * COIN / 100),
        est_dust_sats=(outs_each + 1) * HARD_DUST * total,
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
        if self.params.marker_address is None:
            raise SendError("no marker address configured for this network")

        pubkey = self._pubkey_for(sender_address)
        anydata = AnyData(data=payload).encode()
        groups = encode_class_b(sender_address, pubkey, anydata)

        outputs = [(HARD_DUST, p2pkh_script(self.params.marker_address))]
        for group in groups:
            outputs.append((HARD_DUST, multisig_script(list(group.keys), group.required)))
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

    def prepare(self, sender_address: str, payload: bytes, class_c: bool = False) -> PreparedTx:
        """Build, fund and sign one transaction. Does NOT broadcast."""
        if class_c:
            if len(payload) > max_class_c_payload():
                raise SendError(
                    f"payload of {len(payload)} bytes exceeds Class C capacity "
                    f"({max_class_c_payload()})"
                )
            outputs = [(0, op_return_script(encode_class_c(payload)))]
        else:
            outputs = self._class_b_outputs(sender_address, payload)

        raw = build_raw_tx([], outputs)

        funded = self.rpc.call("fundrawtransaction", raw)
        if not funded or "hex" not in funded:
            raise SendError("fundrawtransaction failed; is the wallet funded?")
        fee_sats = int(round(float(funded.get("fee", 0)) * COIN))

        signed = self.rpc.call("signrawtransaction", funded["hex"])
        if not signed.get("complete"):
            raise SendError(f"signing failed: {signed.get('errors')}")

        decoded = self.rpc.call("decoderawtransaction", signed["hex"])
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

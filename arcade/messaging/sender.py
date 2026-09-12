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

import logging
import time

from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import Params, require_messaging_network
from ..encoding import MAX_CLASS_B_PAYLOAD, encode_class_b, encode_class_c, max_class_c_payload
from ..payload import AnyData
from ..rpc import RpcClient
from ..txbuild import build_raw_tx, multisig_script, op_return_script, p2pkh_script

log = logging.getLogger(__name__)

#: What counts as spendable when building a message.
#:
#: Zero, deliberately and consistently. `funded_address` has to see unconfirmed
#: change or it reports an address as empty moments after that address received
#: the change from your last send; `send_all` has to spend it to continue a chunk
#: chain. The two used different values for a while and disagreed about what was
#: available -- the address selector picked an address the input selector then
#: called empty, and the send failed with "holds 0.00000000" naming the very
#: address it had just chosen.
SPENDABLE_MINCONF = 0

from .envelope import (
    overhead_for,
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
    #: What the outputs themselves cost, excluding the fee and excluding change.
    #: The confirm screen quoted the fee alone while the composer quoted the dust
    #: alone -- both true, neither the total, and a user reading either one
    #: underestimates. a test machine posted for 0.04568 having been shown 0.00568 and
    #: "about 0.03". This is exact rather than estimated: it is the sum of the
    #: outputs this builds, known before the wallet funds anything.
    dust_sats: int = 0

    @property
    def total_sats(self) -> int:
        """Everything the sender gives up: fee plus unspendable outputs."""
        return self.fee_sats + self.dust_sats

    @property
    def total_coins(self) -> float:
        return self.total_sats / COIN

    @property
    def dust_coins(self) -> float:
        return self.dust_sats / COIN

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
    #: Set only for a chunked message. Identifies the chain, and is what makes a
    #: partial send resumable: re-sealing the same text produces a different id,
    #: so the remaining chunks have to be the ones sealed originally.
    msg_id: bytes | None = None

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
    # Decide on SIZE before sealing as a single transaction. Sealing first was a
    # real limit: a single-transaction header carries the ciphertext length in a
    # uint16, so anything over about 64 KB raised "exceeds a uint16" and could
    # not be planned at all -- even though chunking handles it perfectly well,
    # and a private attachment may be up to 5 MB. Any ordinary photo hit this.
    would_be = len(message) + overhead_for(Header(type=TYPE_SINGLE))
    if would_be > MAX_CLASS_B_PAYLOAD - 4:
        return _plan_chunked(sender, recipient_public, message)

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

    return _plan_chunked(sender, recipient_public, message)


def _plan_chunked(sender: Identity, recipient_public: bytes,
                  message: bytes) -> MessagePlan:
    """Seal once and split the ciphertext across linked transactions."""
    msg_id = new_message_id()
    # Seal ONCE, then split the ciphertext. Framing is added per chunk below, so
    # seal_ciphertext (not seal_message) is used -- the latter would prepend a
    # header that the first chunk would then carry twice.
    body = seal_ciphertext(
        sender, recipient_public, Header(type=TYPE_CHUNK, msg_id=msg_id), message
    )
    capacity = MAX_CLASS_B_PAYLOAD - 4 - 18          # AnyData header + chunk header

    # Split evenly rather than greedily. Filling each chunk to capacity in turn
    # made a message one byte over the ceiling into a 14.8 KB transaction
    # followed by a 796-byte one -- a test machine measured exactly that. The first
    # transaction is then always the largest possible, and it is the one most
    # likely to meet a relay or mempool limit. Dust tracks the total payload
    # rather than the number of transactions, so evening the split halves the
    # worst-case transaction size and costs nothing.
    count = -(-len(body) // capacity)
    even = -(-len(body) // count)
    pieces = [body[i : i + even] for i in range(0, len(body), even)]

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
        msg_id=msg_id,
        payload_bytes=len(body), transactions=total, chunked=True,
        packets=-(-len(body) // 30), multisig_outputs=outs_each * total,
        est_size=size_each * total,
        est_fee_sats=int(size_each * total / 1000 * COIN / 100),
        est_dust_sats=(outs_each + 1) * OUTPUT_VALUE * total,
        chunk_payloads=payloads,
    )


class MessageSender:
    """Builds and broadcasts message transactions through a node's wallet."""

    def __init__(self, rpc: RpcClient, params: Params, *, public_only: bool = False):
        """`public_only` is the single, narrow exception to D-010.

        Encrypted messaging is testnet-only, permanently, and that guard stays
        exactly where it was. A **public group post** is a different thing: it
        carries no key material, is not sealed to anybody, and reveals nothing
        that publishing it does not already reveal -- so it may run on mainnet,
        where it spends real coins.

        The flag is deliberately opt-in and named for what it permits rather than
        what it disables, so a call site that wants mainnet has to say out loud
        that it is sending something public. Nothing in the encrypted path passes
        it, and a test asserts that.
        """
        if not public_only:
            require_messaging_network(params)  # D-010, enforced here and in the CLI
        self.rpc = rpc
        self.params = params
        self.public_only = public_only

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

    def split_outputs(self, address: str, count: int, amount_sats: int) -> PreparedTx:
        """Cut one large output into many small ones, all on `address`.

        Why this matters more than it sounds: chunks of a long message have to
        chain through change outputs *only because there is one output to spend*.
        Each chunk takes it, and the next must wait a block for the change. Give
        the address thirty separate outputs and the chunks become independent
        transactions that can all go at once -- minutes of waiting collapse to
        seconds, and there is no unconfirmed ancestor chain to hit a mempool
        limit either.

        Everything stays on one address on purpose. Class B seeds its
        obfuscation with the sender and the sender is "largest input by sum", so
        spreading the coins across addresses would break message encoding; this
        spreads the *outputs* and leaves the address alone.
        """
        if count < 2:
            raise SendError("splitting means at least two outputs.")
        if amount_sats < int(0.02 * COIN):
            raise SendError(
                "each piece needs to be worth more than the dust threshold, or "
                "the wallet will refuse to spend it later.")

        script = p2pkh_script(address)
        outputs = [(amount_sats, script) for _ in range(count)]
        needed = amount_sats * count + COIN            # pieces plus fee headroom
        inputs = self._select_inputs(address, needed)

        raw = build_raw_tx(inputs, outputs)
        funded = self.rpc.call("fundrawtransaction", raw,
                               {"changeAddress": address})
        if not funded or "hex" not in funded:
            raise SendError("could not fund the split")
        signed = self.rpc.call("signrawtransaction", funded["hex"])
        if not signed.get("complete"):
            raise SendError("the wallet could not sign the split")
        decoded = self.rpc.call("decoderawtransaction", signed["hex"])
        return PreparedTx(
            hex=signed["hex"], txid=decoded["txid"], decoded=decoded,
            fee_sats=int(round(float(funded.get("fee", 0)) * COIN)),
            size=len(signed["hex"]) // 2,
            outputs=len(decoded.get("vout", [])),
            # No dust_sats here on purpose. A split's outputs go back to the
            # SAME address as spendable change, so they are not a cost -- only
            # the fee is. Reporting them as dust would say a split costs twelve
            # coins when it costs a fraction of one.
        )

    #: What one Class B chunk costs: about 110 outputs of dust plus a fee. Split
    #: pieces are sized from this rather than a round number, because a large
    #: file needs thousands of them and 10 coins apiece would need more than a
    #: wallet is likely to hold.
    CHUNK_COST_SATS = 2 * COIN

    def ensure_outputs(self, address: str, wanted: int,
                       each_sats: int = CHUNK_COST_SATS,
                       on_progress: Callable[[str, int, int], None] | None = None,
                       confirm_timeout: float = 900.0) -> bool:
        """Make sure `address` has `wanted` confirmed outputs before a long send.

        A file large enough to chunk otherwise waits a block between every
        transaction. Splitting first costs ONE wait, however many chunks there
        are: the split confirms once, and then all the chunks go at once. Six
        transactions measured at about four minutes chained, against one block
        plus two seconds this way.

        Returns True if it split and waited, False if there was already enough.
        Never splits for a message that fits one transaction -- there is nothing
        to gain and it would spend a fee for nothing.
        """
        if wanted < 2:
            return False
        have = self.spendable_outputs(address, at_least=each_sats // 2)
        if have >= wanted:
            return False

        # A few spare, so the next message does not have to do this again --
        # but never more than the wallet can pay for. Asking for two thousand
        # pieces of a wallet that holds a few hundred coins would simply fail,
        # and failing to split is better handled by splitting less: the chunks
        # that do get their own output go at once, and the rest chain as before.
        try:
            balance = int(round(float(self.rpc.call("getbalance")) * COIN))
        except Exception:
            balance = 0
        affordable = max(0, (balance - COIN) // each_sats)
        pieces = min(wanted + 4, affordable)
        if pieces < 2:
            return False              # nothing useful to do; chain instead
        if on_progress is not None:
            on_progress(
                f"splitting the wallet into {pieces} pieces so all "
                f"{wanted} transactions can go at once", 0, wanted)
        prepared = self.split_outputs(address, pieces, each_sats)
        txid = self.broadcast(prepared)

        if on_progress is not None:
            on_progress("waiting for the split to confirm -- about one block, "
                        "once, instead of one per transaction", 0, wanted)
        # Unconfirmed outputs are descendants of the split, so spending them
        # rebuilds the chain this exists to avoid. The wait is the whole point.
        self._await_confirmation(txid, confirm_timeout)
        return True

    def spendable_outputs(self, address: str, at_least: int = 0) -> int:
        """How many separate outputs `address` has that are worth spending."""
        try:
            unspent = self.rpc.call("listunspent", SPENDABLE_MINCONF, 9_999_999,
                                    [address]) or []
        except Exception:
            return 0
        return sum(1 for u in unspent
                   if int(round(float(u["amount"]) * COIN)) >= at_least)

    def _select_inputs(self, address: str, target: int,
                       minconf: int = SPENDABLE_MINCONF,
                       exclude: frozenset = frozenset()) -> list[tuple[str, int]]:
        """Pick outputs belonging to `address` worth at least `target`.

        Class B seeds its obfuscation keystream with the SENDER address, and the
        sender is determined by "largest input by sum" (omnicore.cpp:984-991). If
        we let the wallet choose inputs freely it may fund from other addresses,
        the computed sender then differs from the one we seeded with, and the
        message is **permanently unreadable by anyone**. So we choose the inputs.
        """
        unspent = self.rpc.call("listunspent", minconf, 9_999_999, [address])
        chosen: list[tuple[str, int]] = []
        total = 0
        # Smallest sufficient first, so a wallet that has been split does not
        # break a large output to pay for a small message and collapse back to
        # having one output. Excluded outpoints are ones another transaction in
        # this same send has already claimed.
        candidates = [u for u in unspent
                      if (u["txid"], int(u["vout"])) not in exclude]
        big_enough = [u for u in candidates
                      if int(round(float(u["amount"]) * COIN)) >= target]
        ordered = (sorted(big_enough, key=lambda u: float(u["amount"]))
                   if big_enough else
                   sorted(candidates, key=lambda u: -float(u["amount"])))
        for utxo in ordered:
            chosen.append((utxo["txid"], int(utxo["vout"])))
            total += int(round(float(utxo["amount"]) * COIN))
            if total >= target:
                return chosen
        # Name an address that can actually pay. Saying "fund that address" to
        # someone whose wallet holds a thousand coins points them at the one
        # action that is not the problem -- the coins are simply somewhere else,
        # and the single-address rule above is why that matters. listunspent
        # already has what is needed to say something useful.
        alternative = self._largest_funded_address(target, excluding=address)
        if alternative:
            name, held = alternative
            raise SendError(
                f"{address} holds {total / COIN:.8f}, which is short of the "
                f"{target / COIN:.8f} this transaction needs. Your coins are on a "
                f"different address: {name} holds {held / COIN:.8f}. Use that one."
            )
        raise SendError(
            f"no single address in this wallet holds the {target / COIN:.8f} this "
            f"transaction needs. Inputs must all come from one address, because "
            f"the sender address is what the message is encoded against, so a "
            f"balance spread thinly across many addresses cannot be used as it "
            f"stands. Consolidate some coins onto one address first."
        )

    def _largest_funded_address(self, target: int,
                                excluding: str | None = None) -> tuple[str, int] | None:
        """The address holding the most spendable value, if it can meet `target`."""
        totals: dict[str, int] = {}
        for utxo in self.rpc.call("listunspent", 1, 9_999_999) or []:
            name = utxo.get("address")
            if not name or name == excluding:
                continue
            totals[name] = totals.get(name, 0) + int(round(float(utxo["amount"]) * COIN))
        if not totals:
            return None
        name = max(totals, key=lambda k: totals[k])
        return (name, totals[name]) if totals[name] >= target else None

    def send_all(self, sender_address: str, payloads: list[bytes],
                 on_progress: Callable[[str, int, int], None] | None = None,
                 approve: Callable[[int, int, PreparedTx], bool] | None = None,
                 on_broadcast: Callable[[int, int, str], None] | None = None,
                 confirm_timeout: float = 3600.0,
                 ) -> list[str]:
        """Send every chunk of one message, in order, waiting between them.

        Each transaction after the first spends the change of the one before, so
        they must be built and broadcast one at a time: chunk N+1's input does
        not exist until chunk N is broadcast. Both front ends got this wrong in
        different ways -- the CLI looped with no pause and could not see the
        change it had just made, and the web interface built every chunk up front,
        which would have had two transactions spending the same output.

        When no waiting is needed
        -------------------------
        If the sending address has several separate outputs -- see
        `split_outputs` -- each chunk funds itself from a different one and they
        all go at once. A 40 KB attachment that took four minutes takes seconds.
        Chunks only chain, and only wait, when there is nothing independent left
        to spend.

        Why the fallback waits rather than spending unconfirmed change
        -------------------------------------------------------------
        Spending 0-conf change would be faster and works for short messages, but
        it fails outright past about six chunks. A Class B chunk is roughly
        14.8 KB, and `DEFAULT_ANCESTOR_SIZE_LIMIT` is 101 KB
        (dogecoin/src/validation.h:76), so a seventh unconfirmed link is rejected
        by the node -- not merely at risk of eviction, but refused. An approach
        that works up to six chunks and then breaks is worse than one that is
        uniformly slow, because a partial send is permanently unreadable: the
        chunks already on chain cannot be taken back, and nothing can complete
        the message later.

        Input selection itself still allows unconfirmed coins, so sending two
        separate messages in a row does not make the second wait on the first's
        change. It is only *within* a message that the wait is required.
        """
        txids: list[str] = []
        total = len(payloads)
        # Outputs already claimed by an earlier transaction in this same send.
        # Without this every chunk would select the same largest output and the
        # second would be a double spend of the first.
        used: set[tuple[str, int]] = set()

        for index, payload in enumerate(payloads, 1):
            prepared = None
            if index > 1:
                # Try to fund from a CONFIRMED output nothing in this send has
                # touched. Confirmed matters: an unconfirmed output is a
                # descendant of whatever created it, so spending a set of
                # unconfirmed siblings rebuilds the very chain this is avoiding
                # and hits the mempool descendant limit. A freshly split wallet
                # therefore does nothing until the split confirms -- which is a
                # block, once, rather than a block per chunk.
                try:
                    prepared = self.prepare(sender_address, payload,
                                            minconf=1, exclude=frozenset(used))
                except SendError:
                    prepared = None

                if prepared is None:
                    # Nothing independent left, so fall back to chaining: this
                    # chunk must spend the previous one's change, which means
                    # waiting for it.
                    if on_progress is not None:
                        on_progress(
                            f"waiting for transaction {index - 1} of {total} to "
                            f"confirm before sending {index}", index, total)
                    try:
                        self._await_confirmation(txids[-1], confirm_timeout)
                    except Exception as exc:
                        raise PartialSend(
                            f"{exc} {len(txids)} of {total} are already on the "
                            f"chain and cannot be taken back. An incomplete "
                            f"message can never be read by anyone.",
                            txids, total) from None
                    used.clear()

            if prepared is None:
                try:
                    prepared = self.prepare(sender_address, payload,
                                            exclude=frozenset(used))
                except Exception as exc:
                    if txids:
                        raise PartialSend(
                            f"could not build transaction {index} of {total}: "
                            f"{exc} {len(txids)} of {total} are already on the "
                            f"chain and cannot be taken back. An incomplete "
                            f"message can never be read by anyone.",
                            txids, total) from None
                    raise

            if approve is not None and not approve(index, total, prepared):
                if txids:
                    raise PartialSend(
                        f"stopped after {len(txids)} of {total}. Those are on the "
                        f"chain already and the message can never be read.",
                        txids, total)
                return []

            txid = self.broadcast(prepared)
            txids.append(txid)
            for vin in prepared.decoded.get("vin", []):
                if "txid" in vin:
                    used.add((vin["txid"], int(vin.get("vout", 0))))
            # Reported before anything else can fail, so a caller can write the
            # progress down.
            if on_broadcast is not None:
                on_broadcast(index, total, txid)
            if on_progress is not None:
                on_progress(f"broadcast {index} of {total}: {txid}", index, total)
        return txids

    def _await_confirmation(self, txid: str, timeout: float) -> None:
        """Block until `txid` has a confirmation, or say why it did not."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = self.rpc.call("getrawtransaction", txid, 1)
                if int(raw.get("confirmations") or 0) >= 1:
                    return
            except Exception:
                pass          # not indexed yet; it is in the mempool
            time.sleep(5.0)
        raise SendError(
            f"transaction {txid} had no confirmation after "
            f"{int(timeout / 60)} minutes.")

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

    def prepare(self, sender_address: str, payload: bytes, class_c: bool = False,
                minconf: int = SPENDABLE_MINCONF,
                change_address: str | None = None,
                exclude: frozenset = frozenset()) -> PreparedTx:
        """Build, fund and sign one transaction. Does NOT broadcast.

        `minconf=0` lets this spend change that is still unconfirmed, which is
        required to continue a chunk chain and wrong anywhere else -- see
        `send_all`.
        """
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

        # Choose our own inputs on BOTH carriers.
        #
        # Class B has to, because it seeds its obfuscation with the sender and
        # the sender is "largest input by sum". Class C does not obfuscate, so
        # this used to let the wallet fund it freely -- and that was a mistake
        # with a long tail. A key announcement is the one transaction whose
        # *sender* is the whole point: it is what binds the key to an address,
        # and readers find it by that address. Funding it from wherever meant the
        # announcement was filed under whichever address happened to hold the
        # largest input, which is never the address the user was told to share.
        # a test machine measured all three being different at once.
        needed = sum(value for value, _ in outputs) + COIN         # outputs + fee headroom
        inputs = self._select_inputs(sender_address, needed, minconf=minconf,
                                     exclude=exclude)

        raw = build_raw_tx(inputs, outputs)

        # Return change to the sender address, on BOTH carriers. Otherwise every
        # send drains the address the obfuscation is seeded with -- including a
        # Class C key announcement, which does not obfuscate but happily spends
        # that address's coins and sends the change elsewhere. The next Class B
        # message then either fails outright or silently resolves to a different
        # sender. It also keeps the messaging identity's funds in one place.
        # (fundrawtransaction options, rpcwallet.cpp:2791.)
        funded = self.rpc.call(
            "fundrawtransaction", raw,
            {"changeAddress": change_address or sender_address})
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
            # What the sender loses to the outputs themselves, as opposed to
            # `outputs` above, which counts what is on the wire including
            # change. Zero for Class C, whose single output carries no value.
            dust_sats=sum(value for value, _ in outputs),
        )

    def broadcast(self, prepared: PreparedTx) -> str:
        """Send. Callers MUST have shown `prepared` to the user and got approval."""
        return str(self.rpc.call("sendrawtransaction", prepared.hex))


class PartialSend(SendError):
    """Some chunks are on chain and the rest cannot be sent.

    Carries what was broadcast, because at this point the message is unreadable
    by anyone and the only useful thing left is to say so precisely.
    """

    def __init__(self, message: str, txids: list[str], total: int):
        super().__init__(message)
        self.txids = txids
        self.total = total


def funded_address(rpc, prefer: str | None = None, need: int = COIN,
                   mainnet: bool = False) -> str:
    """An address in this wallet that actually holds spendable coins.

    `prefer` is tried first -- the messaging identity address, in practice. Using
    it whenever it can pay is what makes a message's sender attribution match the
    address the user was told to hand out, since attribution follows the inputs.

    Not `getnewaddress`. A fresh address holds nothing by definition, so using
    one produces "that address holds 0.00000000, fund it" while the wallet is
    full -- pointing the user at the one action that is not the problem. a test machine hit
    exactly that with 999.99 PEP available.

    It also has to be an address the inputs really come from: Class B seeds its
    obfuscation with the sender, and the sender is "largest input by sum", so an
    arbitrary wallet address would produce a message nobody can read.
    """
    totals: dict[str, float] = {}
    for utxo in rpc.call("listunspent", 0, 9_999_999) or []:
        name = utxo.get("address")
        if name:
            totals[name] = totals.get(name, 0.0) + float(utxo.get("amount", 0))
    # The preference only wins if it can actually pay. `need` defaults to a
    # coin, which covers a short message; anything larger falls back to the
    # address that holds the most and reports clearly if that is short too.
    # Without this guard the preference was unconditional and happily returned an
    # address holding nothing -- which is the bug it existed to prevent.
    preferred_total = totals.get(prefer or "", 0.0) * COIN
    if prefer and preferred_total > 0 and preferred_total >= need:
        return prefer
    best = max(totals, key=lambda k: totals[k]) if totals else None
    best_value = totals.get(best, 0.0) if best else 0.0
    if best is None:
        raise SendError(
            "this wallet has no coins to spend. Send some to a receiving address "
            "from the Wallets page first."
            if mainnet else
            "this wallet has no spendable coins yet. Mine a block to fund it, "
            "then wait for the coins to mature."
        )
    return best


def record_sent(store, txid: str, recipient_key: bytes, sender_fp: str,
                body: bytes, recipient_addr: str = "", file_name: str = "",
                file_type: str = "", file_data: bytes | None = None) -> None:
    """Write down a message we just sent, on whichever front end sent it.

    A sealed message cannot be read back off the chain by its sender, so this
    local copy is the only one that will ever exist. Missing it does not lose the
    message for the recipient -- it loses it for *us*, which is why a machine
    that sent by CLI showed a conversation with its own half absent.

    Failure here must never look like a failed send: the transaction is already
    on the chain and irreversible by the time this is called.
    """
    try:
        store.add_sent(txid, recipient_key, recipient_addr, sender_fp, body,
                       file_name=file_name, file_type=file_type,
                       file_data=file_data)
    except Exception:                      # pragma: no cover - storage only
        log.warning("could not record sent message %s locally", txid, exc_info=True)


def recent_block_seconds(rpc, samples: int = 25) -> tuple[float, float]:
    """(typical, slow) gap between recent blocks, in seconds.

    Measured from the chain rather than assumed from its target, because the two
    differ a great deal. Pepecoin testnet aims at a minute; over the last thirty
    blocks the median gap was 35 seconds, the mean 49, and the worst 373. An
    estimate built on the nominal figure would be confidently wrong in both
    directions.

    Returns the median and roughly the 90th percentile, so a caller can say
    "about this long, sometimes rather more" instead of pretending to a
    precision the chain does not have.
    """
    try:
        tip = rpc.get_block_count()
        times = []
        for height in range(max(0, tip - samples), tip + 1):
            times.append(rpc.get_block(rpc.get_block_hash(height)).get("time", 0))
        gaps = sorted(b - a for a, b in zip(times, times[1:]) if b > a)
    except Exception:
        gaps = []
    if not gaps:
        return 60.0, 180.0          # the target, as a last resort
    median = gaps[len(gaps) // 2]
    slow = gaps[min(len(gaps) - 1, int(len(gaps) * 0.9))]
    return float(median), float(max(slow, median))


def estimate_send_seconds(transactions: int, typical: float, slow: float,
                          independent_outputs: int = 0) -> tuple[int, int]:
    """How long sending `transactions` transactions will take.

    Only waits cost time; broadcasting is immediate. A chunk waits when it has to
    spend the previous chunk's change, and it does not when it can fund itself
    from a confirmed output of its own -- see `split_outputs`. So a wallet with
    enough separate outputs sends a long message in seconds rather than minutes,
    which is measured rather than hoped: six transactions took 2 seconds split,
    against about four minutes chained.
    """
    waits = max(0, transactions - 1 - max(0, independent_outputs - 1))
    return int(waits * typical), int(waits * slow)


def estimate_readable_seconds(transactions: int, typical: float) -> int:
    """How long until every chunk is CONFIRMED, not merely broadcast.

    Broadcasting a split send is instant, and `estimate_send_seconds` says so
    correctly. But a chunked message cannot be read until the last chunk is in a
    block, and large chunks do not share one: a test machine's two 8,855-byte public
    chunks went out in the same second and confirmed in testnet blocks
    1,484,210 and 1,484,212 -- two apart, not together -- and on regtest three
    10.7 KB chunks took one block each. Two independent observations, one on
    each chain, so this is not a regtest artefact.

    Assumes one chunk per block, which matches both and errs towards over-
    stating rather than promising a message will be readable sooner than it is.
    A single-transaction message is one block like anything else.
    """
    return int(max(1, transactions) * typical)


def describe_duration(seconds: int) -> str:
    """A duration a person can act on. Deliberately coarse."""
    if seconds < 45:
        return "under a minute"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"about {minutes} minute{'' if minutes == 1 else 's'}"
    hours = seconds / 3600
    return f"about {hours:.1f} hours"

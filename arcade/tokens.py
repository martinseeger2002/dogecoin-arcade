"""Building token transactions: create, send, grant, revoke, change issuer.

The same shape as sending coins (arcade.wallet) and messages
(arcade.messaging.sender), because the reason is the same:

    build our outputs -> fundrawtransaction -> signrawtransaction
    -> decoderawtransaction -> show the caller -> sendrawtransaction

Nothing here broadcasts. `prepare` returns a signed, decoded transaction and
the caller shows it before `broadcast` -- on the ledger chain these spend real
coins and, once a token exists, move real value.

What is on the wire
-------------------
The **raw Omni payload**, not an AnyData envelope. Messaging wraps everything in
type 200 because the Messenger's formats are its own; a token transaction is
the protocol itself and the engine (arcade.state) reads it as such. Class C
when it fits an OP_RETURN, Class B otherwise -- only a long-named issuance
needs Class B.

Who the sender is
-----------------
The engine takes a Class C sender from the FIRST input and a Class B sender
from the largest input by address (tx.py:determine_sender, after
omnicore.cpp:957-1021). Both are satisfied the same way as messaging does it:
choose inputs from the sending address ourselves, let the wallet top up if it
must, put the change back on the sender, and re-derive the sender from the
signed transaction before handing it back. A token send from the wrong address
is a send of somebody else's tokens -- invalid, and paid for.

Who the recipient is
--------------------
An output to the recipient worth the wallet's dust floor. With change back on
the sender the reference rule (tx.py:determine_reference) skips that first
sender output as change and takes the last remaining one, which is the
recipient. Without a recipient there is only the change, and the reference is
the sender: a grant then lands on the issuer ("assume grant to self"), and a
creation ignores it.
"""

from __future__ import annotations

import json as jsonlib
from dataclasses import dataclass
from typing import Any

from . import fees
from . import payload as P
from .config import Params
from .encoding import (
    MAX_CLASS_B_PAYLOAD, encode_class_b, encode_class_c, max_class_c_payload,
)
from .messaging.sender import COIN, OUTPUT_VALUE, MessageSender, SendError
from .rpc import RpcClient
from .state import (
    ECOSYSTEM_MAIN, ECOSYSTEM_TEST, MAX_AMOUNT, PROPERTY_DIVISIBLE, PROPERTY_INDIVISIBLE,
)
from .script import parse_output
from .tx import (
    EncodingClass, PrevOut, TxError, _parsed_outputs, determine_reference, determine_sender,
)
from .txbuild import build_raw_tx, multisig_script, op_return_script, p2pkh_script


#: How long a description may be on the way IN. `details` caps what it reads
#: at 400, but the cap that matters is this one: an issuance is paid for by the
#: byte, for ever, and nobody should be able to buy two thousand characters
#: that nothing will ever show (a test machine).
MAX_ABOUT = 200


def icon_in(text: str) -> str:
    """The inscription a token's fields name, or "" if they name none.

    A token gets its face from an inscription on the same chain, so it is
    served by whichever node is looking at it and belongs to nobody's CDN
    (D-098). The reading is `inscriptions.inscription_in`, shared with a
    collection's thumbnail: the same question asked twice.
    """
    from . import inscriptions as I

    return I.inscription_in(text)


def details(prop: dict[str, Any]) -> dict[str, str]:
    """What a token says about itself: its description, its icon, its link.

    Omni gives an issuance five strings and no more, so a token that wants an
    icon has to say so in one of them. `data` is the description, and it is
    read three ways: a BARE INSCRIPTION ID -- the whole field, which is the
    smallest thing that can name an icon and is what a token with no
    description writes; a JSON OBJECT, for one that wants both::

        {"about": "100 goofcoins", "icon": "<txid>",
         "url": "https://goofcoin.example"}

    and anything else is the description, which is what `data` has always
    been and what every other Omni tool shows.

    An icon named in `url` is read too, so pasting a /content/ link into the
    link field does the obvious thing rather than nothing.

    Nothing here is consensus: the property row on the chain is unchanged,
    and a node that has never heard of this convention shows the same token
    with the JSON as its description. What is read is a whitelist, because
    this is issuer-supplied text on its way to a page.
    """
    data = str(prop.get("data") or "")
    link = str(prop.get("url") or "")
    about, icon, url = data, "", link
    alone = icon_in(data)
    if alone and alone == data.strip().lower():
        about, icon = "", alone            # the field is the id and nothing else
    elif data.strip().startswith("{"):
        try:
            found = jsonlib.loads(data)
        except ValueError:
            found = None
        if isinstance(found, dict):
            about = str(found.get("about") or found.get("description") or "")[:400]
            icon = icon_in(str(found.get("icon") or ""))
            said = str(found.get("url") or "")
            if said.lower().startswith(("https://", "http://")):
                url = said
    if not icon:
        icon = icon_in(link)
    if icon and icon_in(url) == icon:
        url = ""            # the link WAS the icon; it is not also a website
    return {"about": " ".join(about.split())[:400], "icon": icon, "url": url}


def data_with_icon(about: str, icon: str) -> str:
    """The `data` string an issuance carries, given what the form was told.

    As few bytes as will say it: plain text when there is no icon, so a token
    that does not want one costs exactly what it did before; the bare id when
    there is an icon and nothing to say about it; the object only when both
    are wanted.

    None of these fits one OP_RETURN and no shortening would -- an id is 64
    characters and a Class C payload is 76 for the WHOLE issuance, name and
    all -- so a token with an icon is carried as Class B, which costs the
    multisig encoding and some sweepable dust. Said plainly in the guide
    rather than implied to be a budget somebody can manage by writing less
    (a test machine). What the shortening buys is fewer of those outputs.
    """
    about = " ".join((about or "").split())[:MAX_ABOUT]
    icon = icon_in(icon)
    if not icon:
        return about
    if not about:
        return icon
    return jsonlib.dumps({"about": about, "icon": icon}, separators=(",", ":"))


class TokenError(Exception):
    """The transaction could not be built, or the request made no sense."""


@dataclass
class PreparedTokenTx:
    """A signed token transaction, decoded and awaiting approval."""

    hex: str
    txid: str
    decoded: dict[str, Any]
    encoding_class: str            # "C" or "B"
    sender: str
    reference: str | None
    fee_sats: int
    #: The outputs this builds, excluding change: the recipient's dust and,
    #: for Class B, the marker and data outputs.
    dust_sats: int
    size: int
    #: The Class B marker address, so the outputs list can name that output.
    marker: str | None = None
    #: What this is for ("send", "grant", ...), so a broadcast can be listed
    #: as such wherever it was prepared.
    what: str = ""

    @property
    def total_sats(self) -> int:
        return self.fee_sats + self.dust_sats

    @property
    def fee_coins(self) -> float:
        return self.fee_sats / COIN

    @property
    def dust_coins(self) -> float:
        return self.dust_sats / COIN

    @property
    def total_coins(self) -> float:
        return self.total_sats / COIN

    @property
    def outputs(self) -> list[dict[str, Any]]:
        """Each output as the confirm screen shows it: value, and where."""
        rows = []
        for vout in self.decoded.get("vout", []):
            spk = vout.get("scriptPubKey", {})
            kind = spk.get("type", "")
            addresses = spk.get("addresses") or []
            # A multisig data output lists the sender's own key among its
            # addresses, which is not the same as being change; the wallet can
            # spend it, and that is all the label promises.
            address = addresses[0] if addresses and kind != "multisig" else None
            if kind == "nulldata":
                where = "token data (OP_RETURN)"
            elif kind == "multisig":
                where = "token data (multisig, spendable by you)"
            elif address == self.marker:
                where = f"{address} (Class B marker)"
            elif address:
                where = address
            else:
                where = kind or "unknown"
            rows.append({"value": float(vout.get("value", 0)), "where": where,
                         "is_change": address == self.sender,
                         "is_recipient": address is not None and address == self.reference})
        return rows


class TokenSender:
    """Builds and broadcasts token transactions through the ledger node's wallet."""

    def __init__(self, rpc: RpcClient, params: Params):
        self.rpc = rpc
        self.params = params
        # Input selection, the wallet's public-key lookup and the largest-input
        # check are the messaging sender's, and they are wanted unchanged:
        # `public_only` is what lets it exist on mainnet at all (D-014), and
        # nothing here encrypts anything.
        self._inputs = MessageSender(rpc, params, public_only=True)

    # --- building -------------------------------------------------------------

    def prepare(self, sender: str, payload: bytes, reference: str | None = None,
                ) -> PreparedTokenTx:
        """Build, fund and sign one token transaction. Does NOT broadcast."""
        if reference is not None and reference == sender:
            # Legal, and the reference rule would resolve it as change anyway;
            # refusing is clearer than a send-to-self that shows as no send.
            raise TokenError("the recipient is the sending address itself.")
        try:
            P.decode(payload)                 # never put an unreadable payload on chain
        except P.PayloadError as exc:
            raise TokenError(f"refusing to send an unreadable payload: {exc}") from exc

        if len(payload) <= max_class_c_payload():
            encoding_class = "C"
            outputs = [(0, op_return_script(encode_class_c(payload)))]
        else:
            if len(payload) > MAX_CLASS_B_PAYLOAD:
                raise TokenError(f"payload of {len(payload)} bytes is too large for Class B")
            encoding_class = "B"
            pubkey = self._inputs._pubkey_for(sender)
            outputs = [(OUTPUT_VALUE, p2pkh_script(self.params.marker))]
            for group in encode_class_b(sender, pubkey, payload):
                outputs.append((OUTPUT_VALUE, multisig_script(list(group.keys), group.required)))
        if reference is not None:
            outputs.append((OUTPUT_VALUE, p2pkh_script(reference)))

        needed = sum(value for value, _ in outputs) + COIN        # outputs + fee headroom
        try:
            inputs = self._inputs._select_inputs(sender, needed)
        except SendError as exc:
            raise TokenError(str(exc)) from exc

        raw = build_raw_tx(inputs, outputs)
        funded = fees.fund(self.rpc, raw, {"changeAddress": sender})
        if not funded or "hex" not in funded:
            raise TokenError("fundrawtransaction failed; is the wallet funded?")
        fee_sats = int(round(float(funded.get("fee", 0)) * COIN))

        signed = self.rpc.call("signrawtransaction", funded["hex"])
        if not signed.get("complete"):
            raise TokenError(f"signing failed: {signed.get('errors')}")
        decoded = self.rpc.call("decoderawtransaction", signed["hex"])

        self._verify(decoded, encoding_class, sender, reference)
        return PreparedTokenTx(
            hex=signed["hex"],
            txid=decoded["txid"],
            decoded=decoded,
            encoding_class=encoding_class,
            sender=sender,
            reference=reference,
            fee_sats=fee_sats,
            dust_sats=sum(value for value, _ in outputs),
            size=len(signed["hex"]) // 2,
            marker=self.params.marker if encoding_class == "B" else None,
        )

    def _verify(self, decoded: dict[str, Any], encoding_class: str, sender: str,
                reference: str | None) -> None:
        """Re-derive sender and recipient from the signed transaction.

        Through the engine's own functions (tx.py), not a re-statement of the
        rules here, so what is checked is exactly what the indexer will do with
        the transaction once it is in a block.
        """
        def lookup(txid: str, index: int) -> PrevOut:
            prev = self.rpc.call("getrawtransaction", txid, True)
            out = prev["vout"][index]
            parsed = parse_output(out["scriptPubKey"]["hex"],
                                  int(round(float(out["value"]) * COIN)), self.params)
            return PrevOut(address=parsed.address, value=parsed.value, type=parsed.type)

        klass = EncodingClass.C if encoding_class == "C" else EncodingClass.B
        try:
            found_sender, _ = determine_sender(decoded, klass, lookup)
        except TxError as exc:
            raise TokenError(f"the funded transaction cannot be read back: {exc}") from exc
        if found_sender != sender:
            raise TokenError(
                f"the funded transaction resolves to sender {found_sender!r}, not "
                f"{sender!r}; the protocol would read it as theirs. Fund {sender} "
                f"directly and retry."
            )
        outputs = _parsed_outputs(decoded, self.params)
        found = determine_reference(outputs, sender, self.params.marker)
        expected = reference if reference is not None else (
            sender if any(o.address is not None and o.address != self.params.marker
                          for o in outputs) else None)
        if found != expected:
            raise TokenError(
                f"the funded transaction resolves to recipient {found!r}, not "
                f"{expected!r}; refusing to broadcast a send that would go astray."
            )

    def broadcast(self, prepared: PreparedTokenTx) -> str:
        """Send. Callers MUST have shown `prepared` to the user and had a yes."""
        return str(self.rpc.call("sendrawtransaction", prepared.hex))


# --- payloads -----------------------------------------------------------------
#
# The forms speak in names and amounts; the chain speaks in these. Each one
# validates what the engine would reject, so a mistake is refused before it is
# paid for -- an invalid token transaction still costs its fee and is recorded
# for ever as "this did nothing, and here is why".

MAX_NAME = 255


def _text(value: str, what: str, required: bool = False) -> str:
    value = (value or "").strip()
    if required and not value:
        raise TokenError(f"a {what} is needed.")
    if "\x00" in value:
        raise TokenError(f"the {what} cannot contain a NUL character.")
    if len(value.encode("utf-8")) > MAX_NAME:
        raise TokenError(f"the {what} is too long ({MAX_NAME} bytes at most).")
    return value


def issuance_payload(*, name: str, divisible: bool, managed: bool, amount: int | None,
                     test_ecosystem: bool = False, category: str = "",
                     subcategory: str = "", url: str = "", data: str = "") -> bytes:
    """Type 50 or 54. `amount` is in token units and ignored when managed.

    `test_ecosystem` is Omni's second numbering space (properties from
    2^31+1), a protocol field that exists on every chain. The interface never
    sets it: on testnet everything is already a test token, and on mainnet a
    "test" token that costs real fees is a confusion, not a feature. It stays
    here because the engine must read tokens others create there.
    """
    fields = dict(
        ecosystem=ECOSYSTEM_TEST if test_ecosystem else ECOSYSTEM_MAIN,
        property_type=PROPERTY_DIVISIBLE if divisible else PROPERTY_INDIVISIBLE,
        previous_property_id=0,
        category=_text(category, "category"),
        subcategory=_text(subcategory, "subcategory"),
        name=_text(name, "name", required=True),
        url=_text(url, "URL"),
        data=_text(data, "description"),
    )
    if managed:
        return P.IssuanceManaged(**fields).encode()
    if amount is None or not 0 < amount <= MAX_AMOUNT:
        raise TokenError("a fixed-supply token needs a supply between 1 and the maximum.")
    return P.IssuanceFixed(amount=amount, **fields).encode()


def send_payload(property_id: int, amount: int) -> bytes:
    if not 0 < amount <= MAX_AMOUNT:
        raise TokenError("the amount is out of range.")
    return P.SimpleSend(property_id=property_id, amount=amount).encode()


def grant_payload(property_id: int, amount: int, note: str = "") -> bytes:
    if not 0 < amount <= MAX_AMOUNT:
        raise TokenError("the amount is out of range.")
    return P.Grant(property_id=property_id, amount=amount, text=_text(note, "note")).encode()


def revoke_payload(property_id: int, amount: int, note: str = "") -> bytes:
    if not 0 < amount <= MAX_AMOUNT:
        raise TokenError("the amount is out of range.")
    return P.Revoke(property_id=property_id, amount=amount, text=_text(note, "note")).encode()


def change_issuer_payload(property_id: int) -> bytes:
    return P.ChangeIssuer(property_id=property_id).encode()

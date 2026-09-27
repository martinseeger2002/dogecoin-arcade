"""Omni-style transaction payloads: encode and decode.

Every layout here was read out of Omni Core's `src/omnicore/createpayload.cpp`
(master, commit 1c0ae8ae) rather than from documentation, and each carries a line
reference. Getting one field order wrong here silently corrupts every downstream
milestone, so the layouts are declarative and round-trip tested exhaustively.

Wire format
-----------
All integers are **big-endian**. Omni calls SwapByteOrder* before pushing, which
on a little-endian host converts to big-endian.

Every payload begins with:
    version  uint16
    type     uint16

Strings are NUL-terminated and truncated to 255 bytes, except:
  * type 200 (AnyData), whose data is the raw remainder with no terminator
  * types 185/186, whose trailing address is exactly 21 raw bytes

Scope
-----
Only the transaction types Arcade implements are registered (docs/DECISIONS.md
D-008). Decoding an out-of-scope or unknown type raises `UnknownMessageType`
rather than returning a partial result -- hard rule #2: never skip silently.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any, ClassVar

MAX_STRING = 255
ADDRESS_BYTES = 21  # version byte + hash160, checksum stripped (createpayload.cpp:31-45)


class PayloadError(Exception):
    """The payload is malformed and cannot be decoded."""


class OutOfScopeMessageType(PayloadError):
    """A real Omni type that Arcade deliberately does not implement.

    Distinguished from UnknownMessageType so operators can tell "we chose not to
    support this" from "we have never heard of this". Both still stop the
    indexer -- hard rule #2.
    """

    def __init__(self, message_type: int, version: int, reason: str):
        self.message_type = message_type
        self.version = version
        super().__init__(
            f"Omni message type {message_type} ({reason}) is out of scope for Arcade"
        )


def _out_of_scope(message_type: int, version: int, reason: str) -> "OutOfScopeMessageType":
    return OutOfScopeMessageType(message_type, version, reason)


class UnknownMessageType(PayloadError):
    """A transaction type Arcade does not implement.

    Raised rather than ignored. An indexer that skips an unrecognised type
    silently diverges from every other implementation, with no symptom until
    someone compares consensus hashes.
    """

    def __init__(self, message_type: int, version: int):
        self.message_type = message_type
        self.version = version
        super().__init__(f"unsupported Omni message type {message_type} (version {version})")


# --- primitive field codecs ---------------------------------------------------


class _Reader:
    """Sequential big-endian reader with bounds checking."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise PayloadError(
                f"payload truncated: wanted {n} byte(s) at offset {self.pos}, "
                f"only {len(self.data) - self.pos} remain"
            )
        chunk = self.data[self.pos : self.pos + n]
        self.pos += n
        return chunk

    def uint(self, n: int) -> int:
        return int.from_bytes(self.take(n), "big")

    def cstr(self) -> str:
        """Read a NUL-terminated string.

        Omni's parser tolerates a missing terminator at the end of the payload
        (tx.cpp reads into fixed buffers), so we accept that too rather than
        rejecting transactions a reference implementation would have accepted.
        """
        end = self.data.find(b"\x00", self.pos)
        if end == -1:
            chunk = self.data[self.pos :]
            self.pos = len(self.data)
        else:
            chunk = self.data[self.pos : end]
            self.pos = end + 1
        return chunk.decode("utf-8", errors="replace")

    def rest(self) -> bytes:
        chunk = self.data[self.pos :]
        self.pos = len(self.data)
        return chunk

    @property
    def exhausted(self) -> bool:
        return self.pos >= len(self.data)


def _u(value: int, n: int, name: str) -> bytes:
    limit = 1 << (n * 8)
    if not 0 <= value < limit:
        raise PayloadError(f"{name}={value} does not fit in uint{n * 8}")
    return value.to_bytes(n, "big")


def _cstr(value: str, name: str) -> bytes:
    raw = value.encode("utf-8")[:MAX_STRING]
    if b"\x00" in raw:
        raise PayloadError(f"{name} contains an embedded NUL")
    return raw + b"\x00"


# --- messages -----------------------------------------------------------------

_REGISTRY: dict[int, type["Message"]] = {}


def register(cls: type["Message"]) -> type["Message"]:
    if cls.TYPE in _REGISTRY:
        raise RuntimeError(f"message type {cls.TYPE} registered twice")
    _REGISTRY[cls.TYPE] = cls
    return cls


@dataclass
class Message:
    """Base class. Subclasses declare TYPE and implement _pack/_unpack."""

    TYPE: ClassVar[int]
    VERSION: ClassVar[int] = 0

    version: int = field(default=0, kw_only=True)

    def encode(self) -> bytes:
        return _u(self.version, 2, "version") + _u(self.TYPE, 2, "type") + self._pack()

    def _pack(self) -> bytes:  # pragma: no cover - abstract
        raise NotImplementedError

    @classmethod
    def _unpack(cls, reader: _Reader, version: int) -> "Message":  # pragma: no cover - abstract
        raise NotImplementedError


def decode(payload: bytes) -> Message:
    """Decode a payload into a Message, or raise.

    Raises PayloadError for anything malformed and UnknownMessageType for types
    outside Arcade's scope.
    """
    if len(payload) < 4:
        raise PayloadError(f"payload too short: {len(payload)} byte(s), need at least 4")
    reader = _Reader(payload)
    version = reader.uint(2)
    message_type = reader.uint(2)

    cls = _REGISTRY.get(message_type)
    if cls is None:
        reason = OUT_OF_SCOPE.get(message_type)
        raise UnknownMessageType(message_type, version) if reason is None else _out_of_scope(
            message_type, version, reason
        )
    return cls._unpack(reader, version)


# --- sends --------------------------------------------------------------------


@register
@dataclass
class SimpleSend(Message):
    """Type 0. createpayload.cpp:CreatePayload_SimpleSend"""

    TYPE: ClassVar[int] = 0
    property_id: int = 0
    amount: int = 0

    def _pack(self) -> bytes:
        return _u(self.property_id, 4, "property_id") + _u(self.amount, 8, "amount")

    @classmethod
    def _unpack(cls, r: _Reader, version: int) -> "SimpleSend":
        return cls(version=version, property_id=r.uint(4), amount=r.uint(8))


@register
@dataclass
class SendToOwners(Message):
    """Type 3. Version-dependent layout.

    createpayload.cpp:CreatePayload_SendToOwners -- version is 0 when
    distribution_property equals property_id, in which case the field is OMITTED
    from the wire entirely; version 1 includes it.
    """

    TYPE: ClassVar[int] = 3
    property_id: int = 0
    amount: int = 0
    distribution_property: int = 0

    def _pack(self) -> bytes:
        body = _u(self.property_id, 4, "property_id") + _u(self.amount, 8, "amount")
        if self.version != 0:
            body += _u(self.distribution_property, 4, "distribution_property")
        return body

    def encode(self) -> bytes:
        # Version is derived from the data, exactly as Omni derives it.
        self.version = 0 if self.distribution_property == self.property_id else 1
        return super().encode()

    @classmethod
    def _unpack(cls, r: _Reader, version: int) -> "SendToOwners":
        property_id = r.uint(4)
        amount = r.uint(8)
        distribution = r.uint(4) if version != 0 else property_id
        return cls(
            version=version,
            property_id=property_id,
            amount=amount,
            distribution_property=distribution,
        )


@register
@dataclass
class SendAll(Message):
    """Type 4. createpayload.cpp:CreatePayload_SendAll"""

    TYPE: ClassVar[int] = 4
    ecosystem: int = 1

    def _pack(self) -> bytes:
        return _u(self.ecosystem, 1, "ecosystem")

    @classmethod
    def _unpack(cls, r: _Reader, version: int) -> "SendAll":
        return cls(version=version, ecosystem=r.uint(1))


@register
@dataclass
class SendNonFungible(Message):
    """Type 5. createpayload.cpp:CreatePayload_SendNonFungible"""

    TYPE: ClassVar[int] = 5
    property_id: int = 0
    token_start: int = 0
    token_end: int = 0

    def _pack(self) -> bytes:
        return (
            _u(self.property_id, 4, "property_id")
            + _u(self.token_start, 8, "token_start")
            + _u(self.token_end, 8, "token_end")
        )

    @classmethod
    def _unpack(cls, r: _Reader, version: int) -> "SendNonFungible":
        return cls(
            version=version, property_id=r.uint(4), token_start=r.uint(8), token_end=r.uint(8)
        )


# --- distributed exchange -----------------------------------------------------


@register
@dataclass
class DExSell(Message):
    """Type 20, version 1. createpayload.cpp:CreatePayload_DExSell"""

    TYPE: ClassVar[int] = 20
    VERSION: ClassVar[int] = 1
    property_id: int = 0
    amount_for_sale: int = 0
    amount_desired: int = 0
    time_limit: int = 0
    min_fee: int = 0
    sub_action: int = 0

    def __post_init__(self) -> None:
        if self.version == 0:
            self.version = self.VERSION

    def _pack(self) -> bytes:
        return (
            _u(self.property_id, 4, "property_id")
            + _u(self.amount_for_sale, 8, "amount_for_sale")
            + _u(self.amount_desired, 8, "amount_desired")
            + _u(self.time_limit, 1, "time_limit")
            + _u(self.min_fee, 8, "min_fee")
            + _u(self.sub_action, 1, "sub_action")
        )

    @classmethod
    def _unpack(cls, r: _Reader, version: int) -> "DExSell":
        return cls(
            version=version,
            property_id=r.uint(4),
            amount_for_sale=r.uint(8),
            amount_desired=r.uint(8),
            time_limit=r.uint(1),
            min_fee=r.uint(8),
            sub_action=r.uint(1),
        )


@register
@dataclass
class DExAccept(Message):
    """Type 22. createpayload.cpp:CreatePayload_DExAccept"""

    TYPE: ClassVar[int] = 22
    property_id: int = 0
    amount: int = 0

    def _pack(self) -> bytes:
        return _u(self.property_id, 4, "property_id") + _u(self.amount, 8, "amount")

    @classmethod
    def _unpack(cls, r: _Reader, version: int) -> "DExAccept":
        return cls(version=version, property_id=r.uint(4), amount=r.uint(8))


# --- MetaDEx ------------------------------------------------------------------


@dataclass
class _MetaDExPair(Message):
    """Shared layout for types 25 and 26."""

    property_id_for_sale: int = 0
    amount_for_sale: int = 0
    property_id_desired: int = 0
    amount_desired: int = 0

    def _pack(self) -> bytes:
        return (
            _u(self.property_id_for_sale, 4, "property_id_for_sale")
            + _u(self.amount_for_sale, 8, "amount_for_sale")
            + _u(self.property_id_desired, 4, "property_id_desired")
            + _u(self.amount_desired, 8, "amount_desired")
        )

    @classmethod
    def _unpack(cls, r: _Reader, version: int):
        return cls(
            version=version,
            property_id_for_sale=r.uint(4),
            amount_for_sale=r.uint(8),
            property_id_desired=r.uint(4),
            amount_desired=r.uint(8),
        )


@register
@dataclass
class MetaDExTrade(_MetaDExPair):
    """Type 25 -- the order-book message.

    There is no "side" field, and that is the point: a bid and an ask are the
    same message with the pair reversed. Price is amount_desired/amount_for_sale
    and must be handled as an exact rational, never a float.
    """

    TYPE: ClassVar[int] = 25


@register
@dataclass
class MetaDExCancelPrice(_MetaDExPair):
    """Type 26. Cancels open trades at one exact price."""

    TYPE: ClassVar[int] = 26


@register
@dataclass
class MetaDExCancelPair(Message):
    """Type 27. createpayload.cpp:CreatePayload_MetaDExCancelPair"""

    TYPE: ClassVar[int] = 27
    property_id_for_sale: int = 0
    property_id_desired: int = 0

    def _pack(self) -> bytes:
        return _u(self.property_id_for_sale, 4, "property_id_for_sale") + _u(
            self.property_id_desired, 4, "property_id_desired"
        )

    @classmethod
    def _unpack(cls, r: _Reader, version: int) -> "MetaDExCancelPair":
        return cls(version=version, property_id_for_sale=r.uint(4), property_id_desired=r.uint(4))


@register
@dataclass
class MetaDExCancelEcosystem(Message):
    """Type 28. createpayload.cpp:CreatePayload_MetaDExCancelEcosystem"""

    TYPE: ClassVar[int] = 28
    ecosystem: int = 1

    def _pack(self) -> bytes:
        return _u(self.ecosystem, 1, "ecosystem")

    @classmethod
    def _unpack(cls, r: _Reader, version: int) -> "MetaDExCancelEcosystem":
        return cls(version=version, ecosystem=r.uint(1))


@register
@dataclass
class MetaDExTake(Message):
    """Type 29. Take this standing order: the taker's side, signed alone.

    Types 25-28 say what a maker wants. This one is the first thing on the book
    that a person on the OTHER side files: the taker is the sender, and the
    maker is not in this transaction at all. A swap needs both signatures --
    an input is a signature, and that is what makes one transaction where a
    coin leg and a token leg move together (D-048) -- which is also why a
    resting ask could only be filled while its maker's tab was awake. The
    reserve type 25 put behind an order is the maker's consent, already given,
    already on the chain: this says "that one, this much", and pays the
    order's own address what its own price makes it.

    It carries no coin amount on purpose. The outputs say what was paid and the
    order says what it costs; a third place for the same number is two places
    for it to disagree, and the engine compares the two it has.

    48 bytes -- the widest thing a Class C envelope holds here is 76.
    """

    TYPE: ClassVar[int] = 29
    property_id: int = 0
    amount: int = 0
    order: bytes = b""

    def _pack(self) -> bytes:
        if len(self.order) != 32:
            raise PayloadError(f"an order is named by a 32-byte txid, not "
                               f"{len(self.order)} bytes")
        return (_u(self.property_id, 4, "property_id")
                + _u(self.amount, 8, "amount") + self.order)

    @classmethod
    def _unpack(cls, r: _Reader, version: int) -> "MetaDExTake":
        return cls(version=version, property_id=r.uint(4), amount=r.uint(8),
                   order=r.take(32))


# --- property creation --------------------------------------------------------


@dataclass
class _IssuanceCommon(Message):
    """Fields shared by types 50 and 54, in wire order."""

    ecosystem: int = 1
    property_type: int = 2
    previous_property_id: int = 0
    category: str = ""
    subcategory: str = ""
    name: str = ""
    url: str = ""
    data: str = ""

    def _header(self) -> bytes:
        return (
            _u(self.ecosystem, 1, "ecosystem")
            + _u(self.property_type, 2, "property_type")
            + _u(self.previous_property_id, 4, "previous_property_id")
        )

    def _strings(self) -> bytes:
        return (
            _cstr(self.category, "category")
            + _cstr(self.subcategory, "subcategory")
            + _cstr(self.name, "name")
            + _cstr(self.url, "url")
            + _cstr(self.data, "data")
        )

    @staticmethod
    def _read_common(r: _Reader) -> dict[str, Any]:
        return {
            "ecosystem": r.uint(1),
            "property_type": r.uint(2),
            "previous_property_id": r.uint(4),
            "category": r.cstr(),
            "subcategory": r.cstr(),
            "name": r.cstr(),
            "url": r.cstr(),
            "data": r.cstr(),
        }


@register
@dataclass
class IssuanceFixed(_IssuanceCommon):
    """Type 50.

    Note the field order: `amount` comes **after** the five strings, not before
    them (createpayload.cpp:CreatePayload_IssuanceFixed). Putting it first is the
    obvious mistake, and it decodes garbage rather than failing loudly.
    """

    TYPE: ClassVar[int] = 50
    amount: int = 0

    def _pack(self) -> bytes:
        return self._header() + self._strings() + _u(self.amount, 8, "amount")

    @classmethod
    def _unpack(cls, r: _Reader, version: int) -> "IssuanceFixed":
        common = cls._read_common(r)
        return cls(version=version, amount=r.uint(8), **common)


@register
@dataclass
class IssuanceManaged(_IssuanceCommon):
    """Type 54. Same as 50 but with no amount -- supply comes from grants."""

    TYPE: ClassVar[int] = 54

    def _pack(self) -> bytes:
        return self._header() + self._strings()

    @classmethod
    def _unpack(cls, r: _Reader, version: int) -> "IssuanceManaged":
        return cls(version=version, **cls._read_common(r))


# --- managed property operations ----------------------------------------------


@dataclass
class _PropertyAmountText(Message):
    """Shared layout for types 55 and 56: property, amount, NUL-terminated text."""

    property_id: int = 0
    amount: int = 0
    text: str = ""

    def _pack(self) -> bytes:
        return (
            _u(self.property_id, 4, "property_id")
            + _u(self.amount, 8, "amount")
            + _cstr(self.text, "text")
        )

    @classmethod
    def _unpack(cls, r: _Reader, version: int):
        property_id = r.uint(4)
        amount = r.uint(8)
        # The trailing string is optional in practice: older senders omit it.
        text = r.cstr() if not r.exhausted else ""
        return cls(version=version, property_id=property_id, amount=amount, text=text)


@register
@dataclass
class Grant(_PropertyAmountText):
    """Type 55. `text` is Omni's `info` field."""

    TYPE: ClassVar[int] = 55


@register
@dataclass
class Revoke(_PropertyAmountText):
    """Type 56. `text` is Omni's `memo` field."""

    TYPE: ClassVar[int] = 56


@dataclass
class _PropertyOnly(Message):
    """Shared layout for every message whose body is just a property id."""

    property_id: int = 0

    def _pack(self) -> bytes:
        return _u(self.property_id, 4, "property_id")

    @classmethod
    def _unpack(cls, r: _Reader, version: int):
        return cls(version=version, property_id=r.uint(4))


@register
@dataclass
class ChangeIssuer(_PropertyOnly):
    """Type 70."""

    TYPE: ClassVar[int] = 70


@register
@dataclass
class EnableFreezing(_PropertyOnly):
    """Type 71."""

    TYPE: ClassVar[int] = 71


@register
@dataclass
class DisableFreezing(_PropertyOnly):
    """Type 72."""

    TYPE: ClassVar[int] = 72


@register
@dataclass
class AddDelegate(_PropertyOnly):
    """Type 73. Omni Core only -- never existed on Litecoin."""

    TYPE: ClassVar[int] = 73


@register
@dataclass
class RemoveDelegate(_PropertyOnly):
    """Type 74. Omni Core only."""

    TYPE: ClassVar[int] = 74


# --- freezing -----------------------------------------------------------------


@dataclass
class _FreezeBase(Message):
    """Shared layout for types 185 and 186.

    The trailing address is 21 **raw** bytes -- base58-decoded, checksum stripped
    (createpayload.cpp:AddressToBytes, :31-45). It is not NUL-terminated and not
    base58 on the wire.
    """

    property_id: int = 0
    amount: int = 0
    address_bytes: bytes = b""

    def _pack(self) -> bytes:
        if len(self.address_bytes) != ADDRESS_BYTES:
            raise PayloadError(
                f"address_bytes must be exactly {ADDRESS_BYTES} bytes, got {len(self.address_bytes)}"
            )
        return (
            _u(self.property_id, 4, "property_id")
            + _u(self.amount, 8, "amount")
            + self.address_bytes
        )

    @classmethod
    def _unpack(cls, r: _Reader, version: int):
        property_id = r.uint(4)
        amount = r.uint(8)
        address_bytes = r.take(ADDRESS_BYTES)
        return cls(
            version=version, property_id=property_id, amount=amount, address_bytes=address_bytes
        )


@register
@dataclass
class FreezeTokens(_FreezeBase):
    """Type 185."""

    TYPE: ClassVar[int] = 185


@register
@dataclass
class UnfreezeTokens(_FreezeBase):
    """Type 186."""

    TYPE: ClassVar[int] = 186


# --- data and NFTs ------------------------------------------------------------


@register
@dataclass
class AnyData(Message):
    """Type 200 -- the inscription carrier.

    The data is the raw remainder of the payload with **no** terminator
    (createpayload.cpp:CreatePayload_AnyData), so it is byte-transparent: any
    sequence survives a round trip, NULs included. That is what makes it usable
    for arbitrary file chunks.
    """

    TYPE: ClassVar[int] = 200
    data: bytes = b""

    def _pack(self) -> bytes:
        return self.data

    @classmethod
    def _unpack(cls, r: _Reader, version: int) -> "AnyData":
        return cls(version=version, data=r.rest())


@register
@dataclass
class SetNonFungibleData(Message):
    """Type 201.

    `data` is truncated to 255 bytes by Omni (createpayload.cpp:110), which is why
    an NFT holds a *pointer* to an inscription rather than the content itself.
    `issuer` selects which slot is written: 1 = issuer data, 0 = holder data
    (nftdb.h:17-18).
    """

    TYPE: ClassVar[int] = 201
    property_id: int = 0
    token_start: int = 0
    token_end: int = 0
    issuer: int = 0
    data: str = ""

    def _pack(self) -> bytes:
        return (
            _u(self.property_id, 4, "property_id")
            + _u(self.token_start, 8, "token_start")
            + _u(self.token_end, 8, "token_end")
            + _u(self.issuer, 1, "issuer")
            + _cstr(self.data, "data")
        )

    @classmethod
    def _unpack(cls, r: _Reader, version: int) -> "SetNonFungibleData":
        return cls(
            version=version,
            property_id=r.uint(4),
            token_start=r.uint(8),
            token_end=r.uint(8),
            issuer=r.uint(1),
            data=r.cstr() if not r.exhausted else "",
        )


# --- system messages ----------------------------------------------------------


@register
@dataclass
class DeactivateFeature(Message):
    """Type 65533. Version is 65535 (createpayload.cpp:CreatePayload_DeactivateFeature)."""

    TYPE: ClassVar[int] = 65533
    VERSION: ClassVar[int] = 65535
    feature_id: int = 0

    def __post_init__(self) -> None:
        if self.version == 0:
            self.version = self.VERSION

    def _pack(self) -> bytes:
        return _u(self.feature_id, 2, "feature_id")

    @classmethod
    def _unpack(cls, r: _Reader, version: int) -> "DeactivateFeature":
        return cls(version=version, feature_id=r.uint(2))


@register
@dataclass
class ActivateFeature(Message):
    """Type 65534."""

    TYPE: ClassVar[int] = 65534
    VERSION: ClassVar[int] = 65535
    feature_id: int = 0
    activation_block: int = 0
    min_client_version: int = 0

    def __post_init__(self) -> None:
        if self.version == 0:
            self.version = self.VERSION

    def _pack(self) -> bytes:
        return (
            _u(self.feature_id, 2, "feature_id")
            + _u(self.activation_block, 4, "activation_block")
            + _u(self.min_client_version, 4, "min_client_version")
        )

    @classmethod
    def _unpack(cls, r: _Reader, version: int) -> "ActivateFeature":
        return cls(
            version=version,
            feature_id=r.uint(2),
            activation_block=r.uint(4),
            min_client_version=r.uint(4),
        )


@register
@dataclass
class Alert(Message):
    """Type 65535."""

    TYPE: ClassVar[int] = 65535
    VERSION: ClassVar[int] = 65535
    alert_type: int = 0
    expiry_value: int = 0
    message: str = ""

    def __post_init__(self) -> None:
        if self.version == 0:
            self.version = self.VERSION

    def _pack(self) -> bytes:
        return (
            _u(self.alert_type, 2, "alert_type")
            + _u(self.expiry_value, 4, "expiry_value")
            + _cstr(self.message, "message")
        )

    @classmethod
    def _unpack(cls, r: _Reader, version: int) -> "Alert":
        return cls(
            version=version,
            alert_type=r.uint(2),
            expiry_value=r.uint(4),
            message=r.cstr() if not r.exhausted else "",
        )


# Types deliberately NOT implemented (docs/DECISIONS.md D-008 and the final scope
# table). Listed so the error message can say "out of scope" rather than the less
# helpful "unknown".
OUT_OF_SCOPE: dict[int, str] = {
    2: "restricted send",
    10: "savings mark",
    11: "savings compromised",
    12: "rate-limited mark",
    15: "automatic dispensary",
    31: "notification",
    40: "offer/accept a bet",
    51: "crowdsale (dropped, D-008)",
    52: "promote property",
    53: "close crowdsale (dropped, D-008)",
}


def supported_types() -> list[int]:
    return sorted(_REGISTRY)

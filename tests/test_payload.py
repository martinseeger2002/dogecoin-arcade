"""Round-trip and edge-case behaviour of the payload codec."""

import random

import pytest

from arcade import payload as P

# Every registered type with a factory that fills its fields from a seed, so a
# single loop can exercise all of them rather than hand-writing 26 near-identical
# tests (and forgetting one).
FACTORIES = {
    P.SimpleSend: lambda r: P.SimpleSend(property_id=r(2**32), amount=r(2**64)),
    P.SendToOwners: lambda r: P.SendToOwners(
        property_id=r(2**32), amount=r(2**64), distribution_property=r(2**32)
    ),
    P.SendAll: lambda r: P.SendAll(ecosystem=r(2**8)),
    P.SendNonFungible: lambda r: P.SendNonFungible(
        property_id=r(2**32), token_start=r(2**64), token_end=r(2**64)
    ),
    P.DExSell: lambda r: P.DExSell(
        property_id=r(2**32),
        amount_for_sale=r(2**64),
        amount_desired=r(2**64),
        time_limit=r(2**8),
        min_fee=r(2**64),
        sub_action=r(2**8),
    ),
    P.DExAccept: lambda r: P.DExAccept(property_id=r(2**32), amount=r(2**64)),
    P.MetaDExTrade: lambda r: P.MetaDExTrade(
        property_id_for_sale=r(2**32),
        amount_for_sale=r(2**64),
        property_id_desired=r(2**32),
        amount_desired=r(2**64),
    ),
    P.MetaDExCancelPrice: lambda r: P.MetaDExCancelPrice(
        property_id_for_sale=r(2**32),
        amount_for_sale=r(2**64),
        property_id_desired=r(2**32),
        amount_desired=r(2**64),
    ),
    P.MetaDExCancelPair: lambda r: P.MetaDExCancelPair(
        property_id_for_sale=r(2**32), property_id_desired=r(2**32)
    ),
    P.MetaDExCancelEcosystem: lambda r: P.MetaDExCancelEcosystem(ecosystem=r(2**8)),
    P.ChangeIssuer: lambda r: P.ChangeIssuer(property_id=r(2**32)),
    P.EnableFreezing: lambda r: P.EnableFreezing(property_id=r(2**32)),
    P.DisableFreezing: lambda r: P.DisableFreezing(property_id=r(2**32)),
    P.AddDelegate: lambda r: P.AddDelegate(property_id=r(2**32)),
    P.RemoveDelegate: lambda r: P.RemoveDelegate(property_id=r(2**32)),
    P.FreezeTokens: lambda r: P.FreezeTokens(
        property_id=r(2**32), amount=r(2**64), address_bytes=bytes(r(256) for _ in range(21))
    ),
    P.UnfreezeTokens: lambda r: P.UnfreezeTokens(
        property_id=r(2**32), amount=r(2**64), address_bytes=bytes(r(256) for _ in range(21))
    ),
    P.DeactivateFeature: lambda r: P.DeactivateFeature(feature_id=r(2**16)),
    P.ActivateFeature: lambda r: P.ActivateFeature(
        feature_id=r(2**16), activation_block=r(2**32), min_client_version=r(2**32)
    ),
}


@pytest.mark.parametrize("cls", list(FACTORIES), ids=lambda c: c.__name__)
def test_round_trip_over_many_random_values(cls):
    """encode -> decode -> encode must be a fixed point for every type."""
    rng = random.Random(f"arcade-{cls.__name__}")
    for _ in range(200):
        message = FACTORIES[cls](rng.randrange)
        raw = message.encode()
        decoded = P.decode(raw)
        assert type(decoded) is cls
        assert decoded.encode() == raw, f"round trip changed bytes for {cls.__name__}"
        assert decoded == message


def test_anydata_is_byte_transparent():
    """Type 200 must survive arbitrary bytes, NULs and all -- it carries files."""
    rng = random.Random("anydata")
    for length in (0, 1, 30, 76, 255, 1000, 7646):
        blob = bytes(rng.randrange(256) for _ in range(length))
        decoded = P.decode(P.AnyData(data=blob).encode())
        assert decoded.data == blob, f"AnyData corrupted a {length}-byte blob"


def test_anydata_preserves_trailing_nuls():
    """A regression guard: trailing NULs are real data in type 200, not padding."""
    blob = b"file-content\x00\x00\x00"
    assert P.decode(P.AnyData(data=blob).encode()).data == blob


@pytest.mark.parametrize("text", ["", "a", "x" * 255, "unicode: é中文"])
def test_strings_round_trip(text):
    decoded = P.decode(P.Grant(property_id=1, amount=2, text=text).encode())
    assert decoded.text == text[:255]


def test_strings_are_truncated_at_255_like_omni():
    long = "z" * 300
    decoded = P.decode(P.Grant(property_id=1, amount=2, text=long).encode())
    assert len(decoded.text) == 255


def test_issuance_amount_comes_after_the_strings():
    """Pins the easy-to-invert field order in type 50 (amount is LAST)."""
    raw = P.IssuanceFixed(
        ecosystem=1, property_type=2, previous_property_id=0,
        category="c", subcategory="s", name="n", url="u", data="d", amount=0xDEADBEEF,
    ).encode()
    assert raw.endswith((0xDEADBEEF).to_bytes(8, "big"))


def test_send_to_owners_version_is_derived_from_the_data():
    same = P.SendToOwners(property_id=7, amount=1, distribution_property=7)
    other = P.SendToOwners(property_id=7, amount=1, distribution_property=9)
    assert P.decode(same.encode()).version == 0
    assert P.decode(other.encode()).version == 1
    # v0 omits the field entirely, so it is 4 bytes shorter.
    assert len(other.encode()) == len(same.encode()) + 4


# --- failure modes ------------------------------------------------------------


def test_unknown_type_raises_rather_than_returning_none():
    raw = (0).to_bytes(2, "big") + (9999).to_bytes(2, "big")
    with pytest.raises(P.UnknownMessageType) as exc:
        P.decode(raw)
    assert exc.value.message_type == 9999


@pytest.mark.parametrize("type_id,label", sorted(P.OUT_OF_SCOPE.items()))
def test_out_of_scope_types_are_distinguished_from_unknown(type_id, label):
    """'We chose not to support this' must read differently from 'never heard of it'."""
    raw = (0).to_bytes(2, "big") + type_id.to_bytes(2, "big")
    with pytest.raises(P.OutOfScopeMessageType) as exc:
        P.decode(raw)
    assert "out of scope" in str(exc.value)


def test_crowdsale_types_are_out_of_scope():
    """D-008 dropped them; they must not silently decode."""
    for type_id in (51, 53):
        with pytest.raises(P.OutOfScopeMessageType):
            P.decode((0).to_bytes(2, "big") + type_id.to_bytes(2, "big"))


@pytest.mark.parametrize("truncate_to", [0, 1, 2, 3])
def test_too_short_payload_raises(truncate_to):
    with pytest.raises(P.PayloadError, match="too short"):
        P.decode(b"\x00" * truncate_to)


def test_truncated_body_raises_with_offset():
    full = P.SimpleSend(property_id=1, amount=2).encode()
    with pytest.raises(P.PayloadError, match="truncated"):
        P.decode(full[:-3])


def test_oversized_field_is_rejected_not_wrapped():
    with pytest.raises(P.PayloadError, match="uint32"):
        P.SimpleSend(property_id=2**32, amount=1).encode()


def test_freeze_address_must_be_exactly_21_bytes():
    with pytest.raises(P.PayloadError, match="21 bytes"):
        P.FreezeTokens(property_id=1, amount=1, address_bytes=b"\x00" * 20).encode()


def test_all_in_scope_types_are_registered():
    """The registry must match the scope table in docs/DECISIONS.md D-008."""
    expected = {0, 3, 4, 5, 20, 22, 25, 26, 27, 28, 50, 54, 55, 56,
                70, 71, 72, 73, 74, 185, 186, 200, 201, 65533, 65534, 65535}
    assert set(P.supported_types()) == expected

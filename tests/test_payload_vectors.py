"""Payload encoding checked against Omni Core's OWN test vectors.

Every expected hex string here was lifted verbatim from
`omnicore/src/omnicore/test/create_payload_tests.cpp` and
`obfuscation_tests.cpp`. This is the closest thing Ribbit has to a reference
oracle: if these pass, our wire format is byte-identical to the implementation
that has been running on Bitcoin since 2013.

The marker differs (`rbit` vs `omni`) but the marker is not part of the payload,
so these vectors apply unchanged.
"""

import pytest

from ribbit import payload as P
from ribbit.encoding import prepare_obfuscated_hashes

# --- obfuscation: omnicore/src/omnicore/test/obfuscation_tests.cpp ------------

OBFUSCATION_SEED = "1CdighsfdfRcj4ytQSskZgQXbUEamuMUNF"
OBFUSCATION_EXPECTED = {
    1: "1D9A3DE5C2E22BF89A1E41E6FEDAB54582F8A0C3AE14394A59366293DD130C59",
    2: "0800ED44F1300FB3A5980ECFA8924FEDB2D5FDBEF8B21BBA6526B4FD5F9C167C",
    3: "7110A59D22D5AF6A34B7A196DAE7CCC0F27354B34E257832B9955611A9D79B06",
    4: "AA3F890D32864BEA31EE9BD57D2247D8F8CE07B5ABAED9372F0B8999D28DB963",
}


@pytest.mark.parametrize("index,expected", sorted(OBFUSCATION_EXPECTED.items()))
def test_obfuscation_matches_reference(index, expected):
    hashes = prepare_obfuscated_hashes(OBFUSCATION_SEED, 5)
    assert hashes[index].hex().upper() == expected


# --- payloads: omnicore/src/omnicore/test/create_payload_tests.cpp ------------

ADDRESS_EXODUS_BYTES = bytes.fromhex("00946cb2e08075bcbaf157e47bcb67eb2b2339d242")

VECTORS: list[tuple[str, P.Message, str]] = [
    (
        "simple_send",
        P.SimpleSend(property_id=1, amount=100_000_000),
        "00000000000000010000000005f5e100",
    ),
    (
        "send_to_owners_v0",
        P.SendToOwners(property_id=1, amount=100_000_000, distribution_property=1),
        "00000003000000010000000005f5e100",
    ),
    (
        "send_to_owners_v1",
        P.SendToOwners(property_id=1, amount=100_000_000, distribution_property=3),
        "00010003000000010000000005f5e10000000003",
    ),
    ("send_all", P.SendAll(ecosystem=2), "0000000402"),
    (
        "dex_offer",
        P.DExSell(
            property_id=1,
            amount_for_sale=100_000_000,
            amount_desired=20_000_000,
            time_limit=10,
            min_fee=10_000,
            sub_action=1,
        ),
        "00010014000000010000000005f5e1000000000001312d000a000000000000271001",
    ),
    (
        "metadex_trade",
        P.MetaDExTrade(
            property_id_for_sale=1,
            amount_for_sale=250_000_000,
            property_id_desired=31,
            amount_desired=5_000_000_000,
        ),
        "0000001900000001000000000ee6b2800000001f000000012a05f200",
    ),
    (
        "metadex_cancel_price",
        P.MetaDExCancelPrice(
            property_id_for_sale=1,
            amount_for_sale=250_000_000,
            property_id_desired=31,
            amount_desired=5_000_000_000,
        ),
        "0000001a00000001000000000ee6b2800000001f000000012a05f200",
    ),
    (
        "metadex_cancel_pair",
        P.MetaDExCancelPair(property_id_for_sale=1, property_id_desired=31),
        "0000001b000000010000001f",
    ),
    ("metadex_cancel_ecosystem", P.MetaDExCancelEcosystem(ecosystem=1), "0000001c01"),
    (
        "dex_accept",
        P.DExAccept(property_id=1, amount=130_000_000),
        "00000016000000010000000007bfa480",
    ),
    (
        "create_property",
        P.IssuanceFixed(
            ecosystem=1,
            property_type=1,
            previous_property_id=0,
            category="Companies",
            subcategory="Bitcoin Mining",
            name="Quantum Miner",
            url="builder.bitwatch.co",
            data="",
            amount=1_000_000,
        ),
        "0000003201000100000000436f6d70616e69657300426974636f696e204d696e696e67"
        "005175616e74756d204d696e6572006275696c6465722e62697477617463682e636f00"
        "0000000000000f4240",
    ),
    (
        "create_managed_property",
        P.IssuanceManaged(
            ecosystem=1,
            property_type=1,
            previous_property_id=0,
            category="Companies",
            subcategory="Bitcoin Mining",
            name="Quantum Miner",
            url="builder.bitwatch.co",
            data="",
        ),
        "0000003601000100000000436f6d70616e69657300426974636f696e204d696e696e67"
        "005175616e74756d204d696e6572006275696c6465722e62697477617463682e636f00"
        "00",
    ),
    (
        "grant_tokens",
        P.Grant(property_id=8, amount=1000, text="First Milestone Reached!"),
        "000000370000000800000000000003e84669727374204d696c6573746f6e6520526561"
        "636865642100",
    ),
    (
        "change_property_manager",
        P.ChangeIssuer(property_id=13),
        "000000460000000d",
    ),
    ("enable_freezing", P.EnableFreezing(property_id=4), "0000004700000004"),
    ("disable_freezing", P.DisableFreezing(property_id=4), "0000004800000004"),
    (
        "freeze_tokens",
        P.FreezeTokens(property_id=4, amount=1000, address_bytes=ADDRESS_EXODUS_BYTES),
        "000000b90000000400000000000003e800946cb2e08075bcbaf157e47bcb67eb2b2339d242",
    ),
    (
        "unfreeze_tokens",
        P.UnfreezeTokens(property_id=4, amount=1000, address_bytes=ADDRESS_EXODUS_BYTES),
        "000000ba0000000400000000000003e800946cb2e08075bcbaf157e47bcb67eb2b2339d242",
    ),
    ("add_delegate", P.AddDelegate(property_id=21), "0000004900000015"),
    ("remove_delegate", P.RemoveDelegate(property_id=21), "0000004a00000015"),
    (
        "anydata",
        P.AnyData(data=bytes.fromhex("646578782032303230")),
        "000000c8646578782032303230",
    ),
    ("feature_deactivation", P.DeactivateFeature(feature_id=1), "fffffffd0001"),
    (
        "feature_activation",
        P.ActivateFeature(feature_id=1, activation_block=370_000, min_client_version=999),
        "fffffffe00010005a550000003e7",
    ),
    (
        "alert_block",
        P.Alert(alert_type=1, expiry_value=300_000, message="test"),
        "ffffffff0001000493e07465737400",
    ),
    (
        "alert_blockexpiry",
        P.Alert(alert_type=2, expiry_value=1_439_528_630, message="test"),
        "ffffffff000255cd76b67465737400",
    ),
    (
        "alert_minclient",
        P.Alert(alert_type=3, expiry_value=900_100, message="test"),
        "ffffffff0003000dbc047465737400",
    ),
]


@pytest.mark.parametrize("name,message,expected", VECTORS, ids=[v[0] for v in VECTORS])
def test_encode_matches_omni_reference(name, message, expected):
    assert message.encode().hex() == expected


@pytest.mark.parametrize("name,message,expected", VECTORS, ids=[v[0] for v in VECTORS])
def test_decode_reference_vector_round_trips(name, message, expected):
    decoded = P.decode(bytes.fromhex(expected))
    assert decoded.encode().hex() == expected, "re-encoding a decoded vector must be identical"
    assert type(decoded) is type(message)

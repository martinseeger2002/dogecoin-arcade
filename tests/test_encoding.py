"""Class B and Class C container encodings."""

import random

import pytest

from ribbit import payload as P
from ribbit.config import MARKER
from ribbit.encoding import (
    MAX_CLASS_B_PAYLOAD,
    PACKET_DATA,
    ClassBOutput,
    EncodingError,
    class_c_script_size,
    max_class_c_payload,
    decode_class_b,
    decode_class_c,
    encode_class_b,
    encode_class_c,
    is_valid_compressed_pubkey,
    prepare_obfuscated_hashes,
)

SENDER = "1CdighsfdfRcj4ytQSskZgQXbUEamuMUNF"
# A real compressed secp256k1 point (the generator).
REDEEM_KEY = bytes.fromhex("0279BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798")


# --- Class C ------------------------------------------------------------------


def test_class_c_round_trip():
    payload = P.SimpleSend(property_id=1, amount=100).encode()
    data = encode_class_c(payload)
    assert data.startswith(MARKER)
    assert decode_class_c(data) == payload


def test_class_c_ignores_other_protocols():
    """Most OP_RETURNs on-chain are not ours; that is normal, not an error."""
    assert decode_class_c(b"omni" + b"\x00" * 10) is None
    assert decode_class_c(b"") is None
    assert decode_class_c(b"ord" + b"\x01") is None


def test_class_c_script_size_accounts_for_the_pushdata_opcode():
    """A direct push costs 1 byte up to 75; OP_PUSHDATA1 costs 2 beyond that."""
    assert class_c_script_size(75) == 77      # OP_RETURN + push + 75
    assert class_c_script_size(76) == 79      # OP_RETURN + OP_PUSHDATA1 + len + 76
    assert class_c_script_size(80) == 83      # exactly the Pepecoin limit


def test_class_c_enforces_the_datacarrier_limit():
    """MAX_OP_RETURN_RELAY = 83 allows 80 data bytes, so 76 after our 4-byte marker."""
    assert max_class_c_payload(83) == 76
    assert len(encode_class_c(b"x" * 76)) == 80
    with pytest.raises(EncodingError, match="too large"):
        encode_class_c(b"x" * 77)


def test_class_c_capacity_for_anydata_is_72_bytes():
    """The number that drives the inscription cost model in the design doc."""
    header = 4                                        # version + type
    assert max_class_c_payload(83) - header == 72     # bytes of file data per transaction
    biggest = P.AnyData(data=b"x" * 72).encode()
    assert class_c_script_size(len(encode_class_c(biggest))) <= 83
    with pytest.raises(EncodingError):
        encode_class_c(P.AnyData(data=b"x" * 73).encode())


# --- Class B ------------------------------------------------------------------


@pytest.mark.parametrize("size", [1, 29, 30, 31, 59, 60, 61, 300, 1020, 7646, MAX_CLASS_B_PAYLOAD])
def test_class_b_round_trip_at_boundaries(size):
    payload = bytes(random.Random(size).randrange(256) for _ in range(size))
    outputs = encode_class_b(SENDER, REDEEM_KEY, payload)
    decoded = decode_class_b(SENDER, [list(o.keys) for o in outputs])

    # Padding is preserved, not stripped -- see decode_class_b's docstring.
    assert len(decoded) % PACKET_DATA == 0
    assert decoded[:size] == payload
    assert decoded[size:] == b"\x00" * (len(decoded) - size)


def test_class_b_packs_two_data_keys_per_output():
    payload = b"x" * (PACKET_DATA * 5)   # 5 packets -> 3 outputs (2, 2, 1)
    outputs = encode_class_b(SENDER, REDEEM_KEY, payload)
    assert len(outputs) == 3
    assert [len(o.data_keys) for o in outputs] == [2, 2, 1]
    # Every output is a 1-of-N bare multisig including the sender's key.
    for output in outputs:
        assert output.keys[0] == REDEEM_KEY
        assert output.required == 1


def test_class_b_keys_are_valid_curve_points():
    """Invalid keys make the multisig output non-standard and unrelayable."""
    outputs = encode_class_b(SENDER, REDEEM_KEY, b"y" * 200)
    for output in outputs:
        for key in output.keys:
            assert is_valid_compressed_pubkey(key), f"fabricated key is off-curve: {key.hex()}"


def test_class_b_respects_the_255_packet_maximum():
    with pytest.raises(EncodingError, match="exceeds the Class B maximum"):
        encode_class_b(SENDER, REDEEM_KEY, b"z" * (MAX_CLASS_B_PAYLOAD + 1))


def test_class_b_max_payload_is_7650_bytes():
    """The capacity figure the inscription design depends on."""
    assert MAX_CLASS_B_PAYLOAD == 255 * 30 == 7650


def test_class_b_anydata_capacity_is_7646_bytes():
    """7,646 = 7,650 minus the 4-byte payload header. ~7.5x the v1 text-field trick."""
    biggest = P.AnyData(data=b"d" * 7646).encode()
    assert len(biggest) == MAX_CLASS_B_PAYLOAD
    outputs = encode_class_b(SENDER, REDEEM_KEY, biggest)
    assert len(outputs) == 128
    recovered = P.decode(decode_class_b(SENDER, [list(o.keys) for o in outputs]))
    assert recovered.data[:7646] == b"d" * 7646


def test_class_b_wrong_sender_is_detected_not_silently_garbled():
    """Deobfuscation with the wrong address must fail loudly."""
    outputs = encode_class_b(SENDER, REDEEM_KEY, b"secret payload here")
    with pytest.raises(EncodingError, match="sequence number"):
        decode_class_b("1EXoDusjGwvnjZUyKkxZ4UHEf77z6A5S4P", [list(o.keys) for o in outputs])


def test_class_b_refuses_empty_payload():
    with pytest.raises(EncodingError, match="empty payload"):
        encode_class_b(SENDER, REDEEM_KEY, b"")


def test_class_b_refuses_invalid_redeeming_key():
    with pytest.raises(EncodingError, match="not a valid compressed"):
        encode_class_b(SENDER, b"\x02" + b"\xff" * 32, b"data")


def test_class_b_decode_refuses_too_many_packets():
    fake = [[REDEEM_KEY, REDEEM_KEY, REDEEM_KEY] for _ in range(200)]
    with pytest.raises(EncodingError, match="exceeds the maximum"):
        decode_class_b(SENDER, fake)


def test_class_b_obfuscation_actually_obfuscates():
    """The payload must not appear in the clear in the output keys."""
    plain = b"THIS-SHOULD-NOT-BE-VISIBLE-OK"
    outputs = encode_class_b(SENDER, REDEEM_KEY, plain)
    blob = b"".join(b"".join(o.keys) for o in outputs)
    assert plain not in blob


def test_full_stack_payload_through_class_b():
    """A real message type, end to end through the Class B container."""
    message = P.MetaDExTrade(
        property_id_for_sale=3, amount_for_sale=10**8,
        property_id_desired=4, amount_desired=25 * 10**7,
    )
    outputs = encode_class_b(SENDER, REDEEM_KEY, message.encode())
    decoded = P.decode(decode_class_b(SENDER, [list(o.keys) for o in outputs]))
    assert decoded == message

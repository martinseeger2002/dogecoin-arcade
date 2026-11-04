"""The protocol, recorded: fixed inputs and what `main` makes of them (ARCADE Lite phase 0, docs/arcade-lite/CHARTER.md).

Every value here is something another arcade signs, checks or parses. Any edition, Lite or full, in any language, must
give the same bytes for the same inputs, or it is not on the same network. `tests/test_conformance.py` fails when the
code stops matching vectors.json; a deliberate protocol change regenerates it in the same commit and says so in
COLLAB-NOTES.

    python tests/conformance/make_vectors.py      # rewrites tests/conformance/vectors.json
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
VECTORS = HERE / "vectors.json"
sys.path.insert(0, str(HERE.parent.parent))

from arcade import accounts, config, encoding, index_digest, payload, release, script  # noqa: E402
from arcade.mesh import announce, link  # noqa: E402
from arcade.messaging import derive, envelope  # noqa: E402
from arcade.messaging.keys import Identity  # noqa: E402

#: Fixed inputs. Seeds are counters hashed, never secrets anyone uses.
H160 = hashlib.new("sha256", b"conformance hash160").digest()[:20]
SEED32 = hashlib.sha256(b"conformance seed").digest()
#: Ed25519 / X25519 test keys, derived from fixed seeds.
KEY_A = hashlib.sha256(b"conformance key A").digest()
KEY_B = hashlib.sha256(b"conformance key B").digest()
#: A compressed public key (the secp256k1 generator point), for Class B's redeeming key.
G_PUB = bytes.fromhex("0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798")

#: Params fields that only say where this machine keeps things, not what the chain is.
LOCAL_FIELDS = {"rpc_port", "datadir_subdir", "node_dir", "node_conf", "label"}


def _plain(value):
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (frozenset, set, tuple)):
        return [_plain(v) for v in (sorted(value) if isinstance(value, (frozenset, set)) else value)]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, Path):
        return str(value)
    if dataclasses.is_dataclass(value):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    return value


def chains() -> dict:
    """Each network's consensus-facing parameters, and the addresses one hash160 makes there."""
    out = {}
    for name, p in sorted(config.NETWORKS.items()):
        params = {f.name: _plain(getattr(p, f.name)) for f in dataclasses.fields(p) if f.name not in LOCAL_FIELDS}
        addresses = {"p2pkh": script.b58check_encode(p.pubkeyhash_version, H160),
                     "p2sh": script.b58check_encode(p.scripthash_version, H160),
                     "marker": config.derive_marker_address(p)}
        if getattr(p, "bech32_hrp", None):
            addresses["p2wpkh"] = script.p2wpkh_address(p, H160)
        out[name] = {"params": params, "addresses": addresses}
    return out


def signing() -> dict:
    """What a person's key signs: the login message, the messaging identity, and the identity a signature derives."""
    fake_signature = hashlib.sha512(b"conformance signature").digest()[:65]
    identity = Identity.from_secret_bytes(derive._hkdf_sha256(fake_signature, derive.HKDF_INFO))
    return {
        "marker_seed": config.MARKER_SEED.hex(),
        "login_message": [{"origin": o, "nonce": n, "bytes": accounts.login_message(o, n).hex()}
                          for o, n in (("https://pepecoinarcade.com", "00ff" * 8), ("http://127.0.0.1:8420", "ab" * 16))],
        "identity": {"message": derive.DERIVATION_MESSAGE, "hkdf_info": derive.HKDF_INFO.hex(),
                     "signature": fake_signature.hex(), "public": identity.public_bytes.hex()},
    }


def mesh() -> dict:
    """The mesh wire: HELLO, the key schedule, the AUTH proof, a binary room frame, and an on-chain announcement."""
    import nacl.bindings as sodium
    import nacl.public
    import nacl.signing

    eph_a, eph_b = nacl.public.PrivateKey(KEY_A), nacl.public.PrivateKey(KEY_B)
    hello_a = link.Hello(network="pepecoin-test", ephemeral=bytes(eph_a.public_key))
    hello_b = link.Hello(network="pepecoin-test", ephemeral=bytes(eph_b.public_key))
    shared = sodium.crypto_scalarmult(bytes(eph_a), bytes(eph_b.public_key))
    transcript = hashlib.blake2b(hello_a.encode() + hello_b.encode(), digest_size=32).digest()
    signer = nacl.signing.SigningKey(KEY_A)
    pub = {"o": bytes(signer.verify_key).hex(), "sig": "11" * 64, "q": 7, "hops": 2, "k": "msg",
           "r": "room-1", "m": "member-1", "c": {"b": 2, "a": 1}, "d": ["0a0b0c0d", "01020304"]}
    key_hex = bytes(signer.verify_key).hex()
    return {
        "version": link.VERSION, "magic": link.MAGIC.hex(), "max_frame": link.MAX_FRAME,
        "auth_context": link._AUTH_CONTEXT.hex(),
        "hello": hello_a.encode().hex(),
        "frame": link._frame(b"hello").hex(),
        "shared": shared.hex(), "transcript": transcript.hex(),
        "key_dialer": link._derive(shared, transcript, b"arcm-dialer").hex(),
        "key_listener": link._derive(shared, transcript, b"arcm-listener").hex(),
        "auth_proof_dialer": signer.sign(link._AUTH_CONTEXT + b"D" + transcript).signature.hex(),
        "pub_frame": {"header": pub, "blob": "beef", "bytes": link._pack_pub(pub, bytes.fromhex("beef")).hex()},
        "announce": [{"host": h, "port": port, "key": key_hex, "bytes": announce.build(h, port, key_hex).hex()}
                     for h, port in (("1.2.3.4", 8421), ("wss://mesh.example.com", 443))],
    }


def envelopes() -> dict:
    """The cleartext header in front of every on-chain arcade message, one per type."""
    out = {}
    for name in sorted(n for n in dir(envelope) if n.startswith("TYPE_")):
        t = getattr(envelope, name)
        header = envelope.Header(type=t, msg_id=bytes(range(8)), countdown=3, clen=1234)
        try:
            out[name] = header.encode().hex()
        except Exception as exc:                          # noqa: BLE001 -- a type this header can't carry
            out[name] = f"error: {type(exc).__name__}"
    return {"magic": envelope.MAGIC.hex(), "version": envelope.VERSION, "headers": out}


def payloads() -> dict:
    """Token protocol messages (Omni layout) as bytes."""
    samples = [
        payload.SimpleSend(property_id=3, amount=12_345_678),
        payload.SendToOwners(property_id=3, amount=100, distribution_property=4),
        payload.SendNonFungible(property_id=9, token_start=1, token_end=10),
        payload.IssuanceFixed(ecosystem=2, property_type=1, category="Games", subcategory="Arcade", name="CONF",
                              url="https://example.com", data="conformance", amount=21_000_000),
        payload.MetaDExTrade(property_id_for_sale=3, amount_for_sale=500, property_id_desired=1, amount_desired=7),
        payload.Grant(property_id=5, amount=42, text="grant"),
    ]
    return {type(m).__name__: m.encode().hex() for m in samples}


def class_bc() -> dict:
    """Class B (obfuscated multisig) and Class C (OP_RETURN) carriers for one payload."""
    sender = script.b58check_encode(config.NETWORKS["test"].pubkeyhash_version, H160)
    data = bytes(range(40))
    outputs = encoding.encode_class_b(sender, G_PUB, data, rng=SEED32)
    return {
        "sender": sender, "payload": data.hex(),
        "obfuscation": [h.hex() for h in encoding.prepare_obfuscated_hashes(sender, 3)[1:]],
        "class_b": [_plain(o) for o in outputs],
        "class_c": encoding.encode_class_c(data).hex(),
    }


def releases() -> dict:
    """A signed release manifest: what the updater checks before it installs anything."""
    secret = hashlib.sha256(b"conformance release key").hexdigest()
    signed = release.sign(secret, "0123456789abcdef0123456789abcdef01234567", "ab" * 32, published=1_762_000_000.0)
    return {"canonical": release._canonical(release._body(signed["revision"], signed["sha256"],
                                                          signed["published"])).decode(),
            "manifest": signed, "notice": release.build_notice("0123456789abcdef0123456789abcdef01234567").hex()}


def indexes() -> dict:
    """How two arcades compare an index: the record lines, the range digest and the checkpoint rule."""
    digests = {100: "aa" * 32, 105: "bb" * 32, 230: "cc" * 32}
    return {
        "tables": _plain(index_digest.TABLES), "property": _plain(index_digest.PROPERTY),
        "settled": index_digest.SETTLED, "every": index_digest.EVERY,
        "record": index_digest._record("balance", {"a": "x", "b": None, "c": 5}, ("a", "b", "c")),
        "range_digest": {"digests": {str(k): v for k, v in digests.items()}, "lo": 100, "hi": 200,
                         "digest": index_digest.range_digest(digests, 100, 200)},
        "checkpoints": [[i, f, index_digest.checkpoint(i, f)] for i, f in ((5_000_000, 1_000), (1_500, 1_000), (900, 0))],
    }


def compute() -> dict:
    return {"chains": chains(), "signing": signing(), "mesh": mesh(), "envelope": envelopes(),
            "payload": payloads(), "class_bc": class_bc(), "release": releases(), "index": indexes()}


if __name__ == "__main__":
    VECTORS.write_text(json.dumps(compute(), indent=1, sort_keys=True) + "\n")
    print(f"wrote {VECTORS}")

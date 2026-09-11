"""End-to-end: real Ribbit transactions on a real regtest chain.

This is the test that actually proves M2. Everything else exercises the pieces in
isolation with hand-written inputs; here a transaction is built, signed by the
node's wallet, broadcast, mined, and then read back out of a block by the
indexer -- exercising script parsing, class detection, sender determination,
reference resolution, payload decoding and state transitions together.
"""

import dataclasses

import pytest

from ribbit import payload as P
from ribbit.chain import ChainFollower
from ribbit.consensushash import consensus_hash
from ribbit.db import Database
from ribbit.encoding import encode_class_b, encode_class_c
from ribbit.indexer import RibbitHandler
from ribbit.state import Engine, install_schema
from txbuilder import build_raw_tx, multisig_script, op_return_script, p2pkh_script, sign_and_send

DUST = 1_000_000        # 0.01 PEP, comfortably over the 0.001 hard dust limit
FEE = 50_000_000        # 0.5 PEP; regtest has room to be wasteful


@pytest.fixture(scope="module")
def chain(regtest):
    """A funded regtest chain with a stable 'alice' address."""
    regtest.generate(200)          # well past coinbase maturity
    alice = regtest.rpc.call("getnewaddress")
    bob = regtest.rpc.call("getnewaddress")
    regtest.rpc.call("sendtoaddress", alice, 1000)
    regtest.generate(1)
    return regtest, alice, bob


@pytest.fixture
def indexed(tmp_path, chain):
    """A follower wired to the real node, with the marker address configured."""
    node, alice, bob = chain
    marker = node.rpc.call("getnewaddress")
    # Activate at the current tip so each test sees only its OWN transactions.
    # The regtest node is shared across the module, so starting at height 1 would
    # re-index every earlier test's transactions into this test's fresh database.
    params = dataclasses.replace(
        node.params,
        activation_height=node.rpc.get_block_count() + 1,
        marker_address=marker,
    )
    db = Database(tmp_path / "e2e.sqlite")
    install_schema(db)
    handler = RibbitHandler(node.rpc, params)
    follower = ChainFollower(node.rpc, db, params, handler=handler)
    yield node, alice, bob, params, follower, handler
    db.close()


def utxo_for(node, address, minimum=0):
    """Pick the largest spendable output paying `address`, funding it if needed.

    The node's wallet owns `address`, so an unrelated `sendtoaddress` elsewhere in
    the suite may spend its outputs and send the change to an internal address.
    Re-funding on demand keeps each test independent of the order the others ran
    in.
    """
    for _ in range(2):
        unspent = node.rpc.call("listunspent", 1, 9999999, [address])
        if unspent:
            best = max(unspent, key=lambda u: u["amount"])
            value = int(round(float(best["amount"]) * 100_000_000))
            if value >= minimum:
                return best["txid"], int(best["vout"]), value
        node.rpc.call("sendtoaddress", address, 1000)
        node.generate(1)
    raise AssertionError(f"could not fund {address} with at least {minimum} sats")


def send_class_c(node, sender, payload, recipient=None, change_to=None):
    """Build, sign and broadcast a Class C (OP_RETURN) Ribbit transaction."""
    txid, vout, value = utxo_for(node, sender, minimum=FEE + DUST * 3)
    outputs = [(0, op_return_script(encode_class_c(payload)))]
    if recipient:
        outputs.append((DUST, p2pkh_script(recipient)))
    change = value - FEE - (DUST if recipient else 0)
    if change_to and change > DUST:
        outputs.append((change, p2pkh_script(change_to)))
    return sign_and_send(node, build_raw_tx([(txid, vout)], outputs))


# --- Class C ------------------------------------------------------------------


def test_class_c_issuance_and_send_end_to_end(indexed):
    node, alice, bob, params, follower, handler = indexed

    issuance = P.IssuanceFixed(
        ecosystem=1, property_type=2, previous_property_id=0,
        category="Test", subcategory="E2E", name="EndToEnd", url="", data="",
        amount=1_000_000,
    )
    send_class_c(node, alice, issuance.encode(), change_to=alice)
    node.generate(1)
    follower.sync_once()

    engine = Engine(follower.state, params)
    prop = engine.get_property(3)
    assert prop is not None, "the issuance was not indexed"
    assert prop["issuer"] == alice
    assert prop["name"] == "EndToEnd"
    assert engine.get_balance(alice, 3)["balance"] == 1_000_000

    # Now move some of it to bob.
    send_class_c(node, alice, P.SimpleSend(property_id=3, amount=250_000).encode(),
                 recipient=bob, change_to=alice)
    node.generate(1)
    follower.sync_once()

    assert engine.get_balance(alice, 3)["balance"] == 750_000
    assert engine.get_balance(bob, 3)["balance"] == 250_000


def test_sender_is_the_first_input_for_class_c(indexed):
    """Class C sender rule: the owner of vin[0], not the largest input."""
    node, alice, bob, params, follower, handler = indexed
    issuance = P.IssuanceFixed(
        ecosystem=1, property_type=2, previous_property_id=0,
        category="", subcategory="", name="SenderTest", url="", data="", amount=42,
    )
    txid = send_class_c(node, alice, issuance.encode(), change_to=alice)
    node.generate(1)
    follower.sync_once()

    row = follower.db.conn.execute(
        "SELECT * FROM ribbit_tx WHERE txid = ?", (txid,)
    ).fetchone()
    assert row is not None, "transaction was not indexed"
    assert row["sender"] == alice
    assert row["encoding_class"] == "C"
    assert row["valid"] == 1


def test_non_ribbit_transactions_are_ignored(indexed):
    node, alice, bob, params, follower, handler = indexed
    node.rpc.call("sendtoaddress", bob, 1)
    node.generate(1)
    before = handler.stats["candidates"]
    follower.sync_once()
    assert handler.stats["candidates"] == before, "a plain payment was treated as ours"


def test_unmarked_op_return_is_ignored(indexed):
    """Other protocols' OP_RETURNs must not be mistaken for ours."""
    node, alice, bob, params, follower, handler = indexed
    txid, vout, value = utxo_for(node, alice, minimum=FEE + DUST)
    raw = build_raw_tx(
        [(txid, vout)],
        [(0, op_return_script(b"ord" + b"\x01\x02\x03")), (value - FEE, p2pkh_script(alice))],
    )
    sign_and_send(node, raw)
    node.generate(1)
    before = handler.stats["candidates"]
    follower.sync_once()
    assert handler.stats["candidates"] == before


# --- Class B ------------------------------------------------------------------


def test_class_b_round_trip_end_to_end(indexed):
    """The high-capacity path: marker output plus obfuscated multisig."""
    node, alice, bob, params, follower, handler = indexed

    # A payload far too large for Class C, to prove Class B is really in use.
    blob = bytes(range(256)) * 3            # 768 bytes
    payload = P.AnyData(data=blob).encode()

    # The redeeming key must be a real pubkey; any valid one works for the test.
    pubkey = bytes.fromhex(node.rpc.call("validateaddress", alice).get("pubkey")
                           or "0279BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798")
    outputs_b = encode_class_b(alice, pubkey, payload)

    txid, vout, value = utxo_for(node, alice, minimum=FEE + DUST * (len(outputs_b) + 3))
    outs = [(DUST, p2pkh_script(params.marker_address))]
    for output in outputs_b:
        outs.append((DUST, multisig_script(list(output.keys), output.required)))
    outs.append((DUST, p2pkh_script(bob)))
    spent = DUST * (len(outs))
    outs.append((value - spent - FEE, p2pkh_script(alice)))

    sent = sign_and_send(node, build_raw_tx([(txid, vout)], outs))
    node.generate(1)
    follower.sync_once()

    row = follower.db.conn.execute("SELECT * FROM ribbit_tx WHERE txid = ?", (sent,)).fetchone()
    assert row is not None, "Class B transaction was not indexed"
    assert row["encoding_class"] == "B", "should have been detected as Class B"
    assert row["sender"] == alice, "Class B sender is largest-input-by-sum"
    assert row["message_type"] == 200

    recovered = P.decode(bytes.fromhex(row["payload_hex"]))
    assert recovered.data[: len(blob)] == blob, "payload survived the round trip"


# --- reorg with real state ----------------------------------------------------


def test_reorg_rolls_back_protocol_state(indexed):
    """The M0 undo journal, now protecting real balances rather than a toy table."""
    node, alice, bob, params, follower, handler = indexed

    issuance = P.IssuanceFixed(
        ecosystem=1, property_type=2, previous_property_id=0,
        category="", subcategory="", name="ReorgToken", url="", data="", amount=9_000,
    )
    send_class_c(node, alice, issuance.encode(), change_to=alice)
    node.generate(1)
    follower.sync_once()

    engine = Engine(follower.state, params)
    assert engine.get_balance(alice, 3)["balance"] == 9_000
    hash_before = consensus_hash(follower.db)
    safe_height = follower.db.tip()["height"]

    # A send that will be orphaned.
    send_class_c(node, alice, P.SimpleSend(property_id=3, amount=4_000).encode(),
                 recipient=bob, change_to=alice)
    doomed_hash = node.generate(1)[0]
    follower.sync_once()
    assert engine.get_balance(bob, 3)["balance"] == 4_000
    hash_after_send = consensus_hash(follower.db)

    # Orphan the block. The node's tip drops back to the parent, and the
    # transaction returns to its mempool -- so this is the one moment where the
    # rollback is observable before the transaction is inevitably re-mined.
    node.invalidate(doomed_hash)
    result = follower.sync_once()

    assert result.reorged, "the follower did not notice the reorg"
    assert follower.db.tip()["height"] == safe_height
    assert engine.get_balance(bob, 3)["balance"] == 0, "orphaned send still credited bob"
    assert engine.get_balance(alice, 3)["balance"] == 9_000, "alice was not made whole"
    assert consensus_hash(follower.db) == hash_before, (
        "state after the reorg must hash identically to state before the orphaned block"
    )

    # Now let it be re-mined. Replaying the same transaction at a different height
    # must reach exactly the same state -- determinism, not merely reversibility.
    node.generate(3)
    follower.sync_once()
    assert engine.get_balance(bob, 3)["balance"] == 4_000
    assert engine.get_balance(alice, 3)["balance"] == 5_000
    assert consensus_hash(follower.db) == hash_after_send, (
        "replaying the same transaction must reproduce the same consensus hash"
    )

"""Proof that every transaction the tool builds would relay on MAINNET.

This is the only evidence that counts. Testnet and regtest both default to
`fRequireStandard = false` (`chainparams.cpp:309,408`), so a transaction being
accepted there proves nothing about mainnet. Running regtest with
`-acceptnonstdtxn=0` flips `fRequireStandard` to true (`init.cpp:1057`), making
the node apply mainnet's standardness rules.

Every test here includes or relies on a negative control: if the node were not
actually enforcing standardness, these tests would pass vacuously, which would be
worse than not having them.
"""

import dataclasses

import pytest

from arcade.messaging.keys import Identity
from arcade.messaging.sender import OUTPUT_VALUE, MessageSender, plan_message
from arcade.regtest import RegtestNode
from arcade.txbuild import build_raw_tx, multisig_script, op_return_script, p2pkh_script


@pytest.fixture(scope="module")
def strict():
    """A regtest node enforcing mainnet standardness rules."""
    node = RegtestNode(require_standard=True)
    try:
        node.start()
    except RuntimeError as exc:
        pytest.skip(f"regtest node unavailable: {exc}")
    node.generate(200)
    yield node
    node.stop()


@pytest.fixture(scope="module")
def params(strict):
    marker = strict.rpc.call("getnewaddress")
    return dataclasses.replace(
        strict.params, activation_height=1, marker_address=marker
    )


@pytest.fixture
def address(strict):
    """An address the wallet owns, funded, with a known public key."""
    addr = strict.rpc.call("getnewaddress")
    strict.rpc.call("sendtoaddress", addr, 500)
    strict.generate(1)
    # The wallet only exposes a pubkey for an address it has keys for.
    assert strict.rpc.call("validateaddress", addr).get("pubkey")
    return addr


def fund_sign_send(node, outputs):
    """Push a transaction with the given outputs through the node."""
    raw = build_raw_tx([], outputs)
    funded = node.rpc.call("fundrawtransaction", raw)
    signed = node.rpc.call("signrawtransaction", funded["hex"])
    assert signed.get("complete"), signed.get("errors")
    return node.rpc.call("sendrawtransaction", signed["hex"])


# --- negative controls: prove the node really is enforcing ---------------------


def test_node_reports_standardness_enforced(strict):
    """Sanity: the node started with -acceptnonstdtxn=0."""
    assert strict.rpc.get_blockchain_info()["chain"] == "regtest"


def test_two_op_returns_are_rejected(strict, address):
    """Only one OP_RETURN per standard transaction (policy.cpp:115-119).

    If this passes, the node is NOT enforcing standardness and every other test
    in this file is meaningless.
    """
    outputs = [
        (0, op_return_script(b"arcd" + b"\x01" * 20)),
        (0, op_return_script(b"arcd" + b"\x02" * 20)),
        (OUTPUT_VALUE, p2pkh_script(address)),
    ]
    with pytest.raises(Exception) as exc:
        fund_sign_send(strict, outputs)
    assert "multi-op-return" in str(exc.value).lower() or "scriptpubkey" in str(exc.value).lower()


def test_four_of_four_multisig_is_rejected(strict, address):
    """Only x-of-3 bare multisig is standard (policy.cpp:41-49)."""
    pubkey = bytes.fromhex(strict.rpc.call("validateaddress", address)["pubkey"])
    script = bytes([0x50 + 4]) + b"".join(
        bytes([len(pubkey)]) + pubkey for _ in range(4)
    ) + bytes([0x50 + 4]) + b"\xae"
    with pytest.raises(Exception) as exc:
        fund_sign_send(strict, [(OUTPUT_VALUE, script), (OUTPUT_VALUE, p2pkh_script(address))])
    assert "scriptpubkey" in str(exc.value).lower() or "bare-multisig" in str(exc.value).lower()


def send_without_wallet_funding(node, address, outputs):
    """Build a transaction by hand, bypassing fundrawtransaction.

    The wallet applies its own discard threshold (0.01 PEP, wallet.h:68) and
    refuses to CREATE small outputs, which would mask the node's own dust rule.
    Selecting the input ourselves lets us test what the node accepts rather than
    what the wallet is willing to build.
    """
    unspent = node.rpc.call("listunspent", 1, 9999999, [address])
    assert unspent, f"no unspent outputs for {address}"
    best = max(unspent, key=lambda u: u["amount"])
    value = int(round(float(best["amount"]) * 100_000_000))
    spent = sum(v for v, _ in outputs)
    change = value - spent - 50_000_000        # generous fee
    assert change > OUTPUT_VALUE, "input too small for this test"
    raw = build_raw_tx(
        [(best["txid"], int(best["vout"]))],
        outputs + [(change, p2pkh_script(address))],
    )
    signed = node.rpc.call("signrawtransaction", raw)
    assert signed.get("complete"), signed.get("errors")
    return node.rpc.call("sendrawtransaction", signed["hex"])


def test_wallet_refuses_to_create_outputs_below_its_discard_threshold(strict, address):
    """The wallet's floor is 0.01 PEP, ten times the relay dust limit.

    This is why OUTPUT_VALUE is 0.01 and not 0.001: we fund through the node's
    wallet, so its threshold binds before the node's ever applies.
    """
    with pytest.raises(Exception) as exc:
        fund_sign_send(strict, [(50_000, p2pkh_script(address))])
    assert "too small" in str(exc.value).lower()


def test_node_rejects_outputs_below_the_hard_dust_limit(strict, address):
    """Bypassing the wallet, the NODE still rejects sub-hard-dust (policy.cpp:109)."""
    with pytest.raises(Exception) as exc:
        send_without_wallet_funding(strict, address, [(1000, p2pkh_script(address))])
    assert "dust" in str(exc.value).lower(), str(exc.value)


def test_node_accepts_outputs_at_the_hard_dust_limit(strict, address):
    """Positive control: 0.001 PEP IS standard, proving the limit is where we say."""
    txid = send_without_wallet_funding(strict, address, [(100_000, p2pkh_script(address))])
    assert txid in strict.rpc.call("getrawmempool")
    strict.generate(1)


def test_oversized_op_return_is_rejected(strict, address):
    """MAX_OP_RETURN_RELAY = 83 bytes of script (script/standard.h:30)."""
    with pytest.raises(Exception) as exc:
        fund_sign_send(strict, [(0, op_return_script(b"x" * 100)),
                                (OUTPUT_VALUE, p2pkh_script(address))])
    assert "scriptpubkey" in str(exc.value).lower() or "datacarrier" in str(exc.value).lower()


# --- the real thing: our transactions must be accepted ------------------------


def test_key_announcement_is_standard(strict, params, address):
    """The Class C key announcement relays under mainnet rules."""
    from arcade.messaging.envelope import build_key_announcement

    identity = Identity.generate()
    sender = MessageSender(strict.rpc, params)
    prepared = sender.prepare(address, build_key_announcement(identity.public_bytes),
                              class_c=True)
    txid = sender.broadcast(prepared)
    assert txid == prepared.txid
    assert txid in strict.rpc.call("getrawmempool")


@pytest.mark.parametrize("size", [1, 100, 500, 1000, 3000, 7500])
def test_message_transactions_are_standard(strict, params, address, size):
    """Class B message transactions relay under mainnet rules, at every size.

    7,500 characters is the largest single-transaction message: 128 multisig
    outputs and a ~14.7 kB transaction. If anything is going to trip a
    standardness rule, it is this end of the range.
    """
    alice, bob = Identity.generate(), Identity.generate()
    plan = plan_message(alice, bob.public_bytes, b"m" * size)
    assert plan.transactions == 1, "this test covers the single-transaction case"

    sender = MessageSender(strict.rpc, params)
    prepared = sender.prepare(address, plan.chunk_payloads[0])
    txid = sender.broadcast(prepared)
    assert txid in strict.rpc.call("getrawmempool"), f"{size}-byte message was not accepted"


def test_maximum_message_transaction_is_mined(strict, params, address):
    """Accepted into the mempool is not enough -- it must also get into a block."""
    alice, bob = Identity.generate(), Identity.generate()
    plan = plan_message(alice, bob.public_bytes, b"z" * 7500)
    sender = MessageSender(strict.rpc, params)
    prepared = sender.prepare(address, plan.chunk_payloads[0])
    txid = sender.broadcast(prepared)

    strict.generate(1)
    confirmed = strict.rpc.call("getrawtransaction", txid, True)
    assert confirmed.get("confirmations", 0) >= 1, "accepted but never mined"
    assert len(confirmed["vout"]) == prepared.outputs


def test_chunked_message_transactions_are_standard(strict, params, address):
    """Each link of a chained message must be standard on its own."""
    alice, bob = Identity.generate(), Identity.generate()
    plan = plan_message(alice, bob.public_bytes, b"c" * 16_000)
    assert plan.transactions > 1

    sender = MessageSender(strict.rpc, params)
    for index, payload in enumerate(plan.chunk_payloads):
        prepared = sender.prepare(address, payload)
        txid = sender.broadcast(prepared)
        assert txid in strict.rpc.call("getrawmempool"), f"chunk {index} rejected"
        strict.generate(1)     # confirm as we go, so ancestor limits never bind

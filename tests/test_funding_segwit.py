"""Layer 3b (node half): building and assembling a native-segwit send.

The browser half (coins.js verifyOffer using sighashWitness) and the final
"litecoind accepts it" check are node 2's; this holds the node's half headless:
the right scriptCode and amount go into each input's digest, change comes back
on a segwit output, the input is flagged for the browser, and assemble puts the
signature in the witness.
"""

from arcade import funding, script, utxos
from arcade.config import NETWORKS
from arcade.db import Database
from arcade.state import install_schema

P = NETWORKS["ltctest"]
KH = bytes.fromhex("751e76e8199196d454941c45d1b3a323f1433bd6")
SEGWIT_ADDR = script.p2wpkh_address(P, KH)                 # tltc1q...
LEGACY_ADDR = script.b58check_encode(P.pubkeyhash_version, KH)   # m/n...
RECIPIENT = script.p2wpkh_script(bytes.fromhex("22" * 20))


def test_spend_for_reads_segwit_from_the_bech32_address():
    legacy = funding.spend_for(LEGACY_ADDR, P)
    assert legacy["kind"] == "p2pkh"
    sw = funding.spend_for(SEGWIT_ADDR, P)
    assert sw["kind"] == "p2wpkh"
    assert sw["script_code"] == funding.witness_scriptcode(KH)
    assert sw["change_script"] == script.p2wpkh_script(KH)


def test_native_segwit_is_filed_spendable():
    assert script.OutputType.WITNESS_V0_KEYHASH in utxos.SPENDABLE
    parsed = script.parse_output(script.p2wpkh_script(KH).hex(), 0, P)
    assert parsed.type in utxos.SPENDABLE
    assert parsed.address == SEGWIT_ADDR


def test_build_signs_a_segwit_input_over_its_bip143_digest(tmp_path):
    db = Database(tmp_path / "index.sqlite")
    install_schema(db)
    utxos.watch(db, SEGWIT_ADDR, 0)
    db.conn.execute(
        "INSERT INTO utxo (txid, vout, address, value, height) VALUES (?,?,?,?,?)",
        ("11" * 32, 0, SEGWIT_ADDR, 500_000, 1))

    unsigned = funding.build(db, P, SEGWIT_ADDR, [(100_000, RECIPIENT)], rate=0)

    assert unsigned.inputs[0].get("segwit") == "p2wpkh"
    # every sighash is the BIP143 one over this coin's own amount:
    for n, coin in enumerate(unsigned.inputs):
        want = funding.sighash_witness(
            unsigned.inputs, unsigned.outputs, n,
            funding.witness_scriptcode(KH), int(coin["value"])).hex()
        assert unsigned.sighashes[n] == want
    # change (if any) comes back on a segwit output, not a legacy one:
    change_outs = [spk for value, spk in unsigned.outputs if spk != RECIPIENT]
    if change_outs:
        assert change_outs[0] == script.p2wpkh_script(KH)
    # the browser is told it is segwit and its amount:
    js = unsigned.as_json()
    assert js["inputs"][0]["segwit"] == "p2wpkh"
    assert js["inputs"][0]["value"] == 500_000


def test_assemble_puts_the_signature_in_the_witness():
    unsigned = funding.Unsigned(
        raw="", inputs=[{"txid": "33" * 32, "vout": 0, "value": 500_000,
                         "address": SEGWIT_ADDR, "segwit": "p2wpkh"}],
        outputs=[(100_000, RECIPIENT)], sighashes=["00" * 32], signed_from=0)
    sig = bytes.fromhex("3045022100") + b"\x01"
    pubkey = b"\x02" * 33
    raw = funding.assemble(unsigned, [sig.hex()], pubkey)
    assert raw[:8] == "01000000"            # version
    assert raw[8:12] == "0001"              # BIP144 marker + flag
    # the scriptSig is empty (00) and the witness has two items:
    assert raw.endswith("00000000")         # locktime 0
    assert sig.hex() in raw and pubkey.hex() in raw


def test_a_legacy_send_is_unchanged(tmp_path):
    # Same build for a legacy address still produces legacy P2PKH sighashes and
    # no segwit flag, so nothing about the old path moved.
    db = Database(tmp_path / "index.sqlite")
    install_schema(db)
    utxos.watch(db, LEGACY_ADDR, 0)
    db.conn.execute(
        "INSERT INTO utxo (txid, vout, address, value, height) VALUES (?,?,?,?,?)",
        ("44" * 32, 0, LEGACY_ADDR, 500_000, 1))
    unsigned = funding.build(db, P, LEGACY_ADDR, [(100_000, RECIPIENT)], rate=0)
    assert "segwit" not in unsigned.inputs[0]
    from arcade.txbuild import p2pkh_script
    want = funding.sighash(unsigned.inputs, unsigned.outputs, 0,
                           p2pkh_script(LEGACY_ADDR)).hex()
    assert unsigned.sighashes[0] == want

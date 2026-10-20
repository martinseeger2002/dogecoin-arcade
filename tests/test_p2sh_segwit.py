"""P2SH-nested segwit (BIP49, m/49') as the Wallet card's middle address type on L test.

The node half headless, beside test_funding_segwit.py (native) and
test_node2_segwit_app.py (the routes): the address hides its key hash, so the
account registers it with its pubkey; a send from it is built over the same
BIP143 digest as native segwit, its change goes back to the same P2SH address,
and assemble puts the redeemScript in the scriptSig and the signature in the
witness.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_node2_segwit_app import _ready, THEIRS                 # noqa: E402
from test_funding import _pubkey                                 # noqa: E402

import pytest                                                    # noqa: E402

from arcade import funding, script, utxos                        # noqa: E402
from arcade.config import NETWORKS                               # noqa: E402
from arcade.db import Database                                   # noqa: E402
from arcade.state import install_schema                          # noqa: E402

P = NETWORKS["ltctest"]
PUB = _pubkey(int("49" * 32, 16))
KH = script.hash160(PUB)
NESTED = script.p2sh_p2wpkh_address(P, KH)                      # Q...
NESTED_SPK = funding.p2sh_script(script.hash160(script.p2sh_p2wpkh_redeem(KH)))
RECIPIENT = script.p2wpkh_script(bytes.fromhex("22" * 20))


def test_the_nested_address_is_l_tests_q_address():
    assert P.scripthash_version == 58 and NESTED.startswith("Q")
    parsed = script.parse_output(NESTED_SPK.hex(), 0, P)
    assert parsed.type in utxos.SPENDABLE and parsed.address == NESTED


def test_spend_for_takes_the_key_hash_and_checks_it_is_that_address():
    rule = funding.spend_for(NESTED, P, KH)
    assert rule["kind"] == "p2sh-p2wpkh"
    assert rule["script_code"] == funding.witness_scriptcode(KH)
    assert rule["change_script"] == NESTED_SPK
    with pytest.raises(funding.FundingError):
        funding.spend_for(NESTED, P, bytes(20))
    # without a key hash a P2SH sender is what it was before: not segwit
    assert funding.spend_for(NESTED, P)["kind"] == "p2pkh"


def test_build_signs_a_nested_input_over_its_bip143_digest(tmp_path):
    db = Database(tmp_path / "index.sqlite")
    install_schema(db)
    utxos.watch(db, NESTED, 0)
    db.conn.execute(
        "INSERT INTO utxo (txid, vout, address, value, height) VALUES (?,?,?,?,?)",
        ("55" * 32, 0, NESTED, 500_000, 1))

    unsigned = funding.build(db, P, NESTED, [(100_000, RECIPIENT)], rate=0, keyhash=KH)

    assert unsigned.inputs[0]["segwit"] == "p2sh-p2wpkh"
    want = funding.sighash_witness(unsigned.inputs, unsigned.outputs, 0,
                                   funding.witness_scriptcode(KH), 500_000).hex()
    assert unsigned.sighashes == [want]
    assert [spk for _, spk in unsigned.outputs] == [RECIPIENT, NESTED_SPK]
    assert unsigned.as_json()["inputs"][0]["segwit"] == "p2sh-p2wpkh"


def test_assemble_pushes_the_redeem_script_and_witnesses_the_signature():
    unsigned = funding.Unsigned(
        raw="", inputs=[{"txid": "66" * 32, "vout": 1, "value": 500_000,
                         "address": NESTED, "segwit": "p2sh-p2wpkh"}],
        outputs=[(100_000, RECIPIENT)], sighashes=["00" * 32], signed_from=0)
    sig = bytes.fromhex("3044022001") + b"\x01"
    raw = bytes.fromhex(funding.assemble(unsigned, [sig.hex()], PUB))
    redeem = script.p2sh_p2wpkh_redeem(KH)
    assert raw[4:6] == b"\x00\x01"                               # BIP144 marker + flag
    # one input: outpoint, then a scriptSig that is exactly <push 22> 0014<keyhash>
    at = 4 + 2 + 1 + 36
    assert raw[at:at + 24] == bytes([23, 22]) + redeem
    # the witness: two items, the signature and the pubkey
    assert raw.endswith(bytes([2, len(sig)]) + sig + bytes([33]) + PUB + bytes(4))


def test_register_needs_the_pubkey_the_address_is_made_from(client, monkeypatch):
    app, _, _ = _ready(client, monkeypatch)
    for bad in ({}, {"pubkey": "02" * 33}, {"pubkey": "zz"}):
        r = app.post("/account/segwit/register",
                     json={"chain": "ltctest", "address": NESTED, **bad})
        assert r.status_code == 400, (bad, r.text)
    r = app.post("/account/segwit/register",
                 json={"chain": "ltctest", "address": NESTED, "pubkey": PUB.hex()})
    assert r.status_code == 200 and r.json()["kind"] == "p2sh-p2wpkh", r.text
    # once registered, a locked wallet (no pubkey) may say it again
    assert app.post("/account/segwit/register",
                    json={"chain": "ltctest", "address": NESTED}).status_code == 200


def test_nested_and_native_are_registered_side_by_side(client, monkeypatch):
    app, _, _ = _ready(client, monkeypatch)
    native = script.p2wpkh_address(P, script.hash160(_pubkey(int("84" * 32, 16))))
    assert app.post("/account/segwit/register",
                    json={"chain": "ltctest", "address": native}).status_code == 200
    assert app.post("/account/segwit/register",
                    json={"chain": "ltctest", "address": NESTED,
                          "pubkey": PUB.hex()}).status_code == 200
    other = script.p2sh_p2wpkh_address(P, script.hash160(_pubkey(int("48" * 32, 16))))
    r = app.post("/account/segwit/register",
                 json={"chain": "ltctest", "address": other,
                       "pubkey": _pubkey(int("48" * 32, 16)).hex()})
    assert r.status_code == 409, r.text


def test_send_from_the_nested_address_builds_a_bip143_offer(client, monkeypatch):
    app, state, chain = _ready(client, monkeypatch)
    assert app.post("/account/segwit/register",
                    json={"chain": "ltctest", "address": NESTED,
                          "pubkey": PUB.hex()}).status_code == 200
    with state.token_index(chain).open() as db:
        db.conn.execute("INSERT OR REPLACE INTO utxo(txid,vout,address,value,height) VALUES (?,?,?,?,?)",
                        ("ef" * 32, 0, NESTED, 3 * 100_000_000, 1))
        db.conn.commit()
    bal = app.get("/account/segwit/balance", params={"address": NESTED, "network": "ltctest"}).json()
    assert bal["balance"] == 3 * 100_000_000 and bal["watched"] is True

    offered = app.post("/account/send", json={"to": THEIRS, "amount": "1", "chain": "ltctest",
                                              "from": NESTED})
    assert offered.status_code == 200, offered.text
    offer = offered.json()
    assert all(i["segwit"] == "p2sh-p2wpkh" and i["address"] == NESTED for i in offer["inputs"])
    from arcade.rawchain import read_tx
    tx = read_tx(bytes.fromhex(offer["raw"]))
    outputs = list(tx.vout)
    inputs = [{"txid": i["txid"], "vout": i["vout"], "value": c["value"], "sequence": i["sequence"]}
              for i, c in zip(tx.vin, offer["inputs"])]
    for n, c in enumerate(offer["inputs"]):
        want = funding.sighash_witness(inputs, outputs, n, funding.witness_scriptcode(KH),
                                       int(c["value"]), version=tx.version, locktime=tx.locktime).hex()
        assert offer["sighashes"][n] == want, n
    assert any(spk == NESTED_SPK for _, spk in outputs), "change back to the P2SH address"


def test_send_from_a_nested_address_this_account_never_registered(client, monkeypatch):
    app, _, _ = _ready(client, monkeypatch)
    r = app.post("/account/send", json={"to": THEIRS, "amount": "1", "chain": "ltctest",
                                        "from": NESTED})
    assert r.status_code == 400 and "register" in r.json()["detail"], r.text


# --- paying TO each kind (2026-10-20, tbuuol: "every type to every type") ----------

LEGACY_FROM = script.b58check_encode(111, script.hash160(_pubkey(int("36" * 32, 16))))  # _ready's L test address
NATIVE_PUB = _pubkey(int("84" * 32, 16))
NATIVE = script.p2wpkh_address(P, script.hash160(NATIVE_PUB))
TO_LEGACY = THEIRS
TO_NESTED = script.p2sh_p2wpkh_address(P, bytes(range(80, 100)))
TO_NATIVE = script.p2wpkh_address(P, bytes(range(100, 120)))
TO_WSH = script.segwit_encode("tltc", 0, bytes(range(32)))
TO_TAPROOT = script.segwit_encode("tltc", 1, bytes(range(32)))


def _three_funded(app, state, chain):
    assert app.post("/account/segwit/register",
                    json={"chain": "ltctest", "address": NESTED, "pubkey": PUB.hex()}).status_code == 200
    assert app.post("/account/segwit/register",
                    json={"chain": "ltctest", "address": NATIVE}).status_code == 200
    with state.token_index(chain).open() as db:
        for n, addr in enumerate((LEGACY_FROM, NESTED, NATIVE)):
            db.conn.execute("INSERT OR REPLACE INTO utxo(txid,vout,address,value,height) VALUES (?,?,?,?,?)",
                            (f"{n + 1:02x}" * 32, 0, addr, 5 * 100_000_000, 1))
        db.conn.commit()


@pytest.mark.parametrize("to,want", [
    (TO_LEGACY, bytes([0x76, 0xa9, 0x14]) + bytes(range(60, 80)) + bytes([0x88, 0xac])),
    (TO_NESTED, bytes([0xa9, 0x14]) + script.hash160(script.p2sh_p2wpkh_redeem(bytes(range(80, 100))))
     + bytes([0x87])),
    (TO_NATIVE, bytes([0x00, 0x14]) + bytes(range(100, 120))),
    (TO_WSH, bytes([0x00, 0x20]) + bytes(range(32))),
])
def test_every_kind_pays_every_kind(client, monkeypatch, to, want):
    app, state, chain = _ready(client, monkeypatch)
    _three_funded(app, state, chain)
    from arcade.rawchain import read_tx
    for source, flag in ((None, None), (NESTED, "p2sh-p2wpkh"), (NATIVE, "p2wpkh")):
        asked = {"to": to, "amount": "1", "chain": "ltctest", **({"from": source} if source else {})}
        r = app.post("/account/send", json=asked)
        assert r.status_code == 200, (source, to, r.text)
        offer = r.json()
        assert offer["to"] == to
        assert all(i.get("segwit") == flag for i in offer["inputs"]), (source, offer["inputs"])
        outs = list(read_tx(bytes.fromhex(offer["raw"])).vout)
        assert outs[0] == (100_000_000, want), (source, to)


def test_a_taproot_recipient_is_refused_for_what_it_is(client, monkeypatch):
    app, state, chain = _ready(client, monkeypatch)
    _three_funded(app, state, chain)
    r = app.post("/account/send", json={"to": TO_TAPROOT, "amount": "1", "chain": "ltctest"})
    assert r.status_code == 400 and "Taproot" in r.json()["detail"], r.text


def test_a_broken_bech32_is_an_address_with_a_bad_checksum_not_a_long_tag(client, monkeypatch):
    app, state, chain = _ready(client, monkeypatch)
    _three_funded(app, state, chain)
    broken = TO_NATIVE[:-1] + ("q" if TO_NATIVE[-1] != "q" else "p")
    r = app.post("/account/send", json={"to": broken, "amount": "1", "chain": "ltctest"})
    assert r.status_code == 400 and "checksum" in r.json()["detail"], r.text


def test_only_the_coin_send_pays_segwit_and_never_on_real_coins():
    from types import SimpleNamespace
    from arcade.web.app import _check_address
    ltctest = SimpleNamespace(network="ltctest", params=P, label="Litecoin testnet")
    ltc = SimpleNamespace(network="ltc", params=NETWORKS["ltc"], label="Litecoin")
    assert _check_address(TO_NATIVE, mainnet=ltctest, segwit_ok=True) is None
    assert _check_address(TO_NATIVE, mainnet=ltctest) is not None        # other sends: unchanged
    real = script.p2wpkh_address(NETWORKS["ltc"], bytes(range(20)))
    assert _check_address(real, mainnet=ltc, segwit_ok=True) is not None
    assert _check_address(TO_NATIVE, mainnet=ltc, segwit_ok=True) is not None  # wrong chain

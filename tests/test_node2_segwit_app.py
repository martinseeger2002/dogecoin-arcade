"""Node 2's in-app check of team/segwit-send-wiring: the register, balance and send routes on L test.

register -> the node watches the account's tltc1q... address -> a coin there -> /account/send with
`from` builds an offer whose inputs are flagged segwit and whose sighashes are BIP143 over each
coin's own amount (which the browser parity harness already holds equal to coins.js verifyOffer).
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_me_page import _seat                                   # noqa: E402
from test_litecoin_testnet import _with_ltc_test                  # noqa: E402
from test_funding import _pubkey                                 # noqa: E402

from arcade import funding, script                               # noqa: E402
from arcade.config import NETWORKS                               # noqa: E402

P = NETWORKS["ltctest"]
KH = script.hash160(_pubkey(int("35" * 32, 16)))
MINE = script.p2wpkh_address(P, KH)
SOMEONE = script.p2wpkh_address(P, bytes(range(40, 60)))
THEIRS = script.b58check_encode(111, bytes(range(60, 80)))


def _ready(client, monkeypatch):
    app, state = client
    chain = _with_ltc_test(state, monkeypatch, ready=True)
    _seat(app)
    # as sign-up leaves it: a P test coin address, whose L test twin is the account's legacy L test address
    pep_test = script.b58check_encode(113, script.hash160(_pubkey(int("36" * 32, 16))))
    assert app.post("/account/address", json={"address": pep_test}).status_code == 200
    ltc_test = script.b58check_encode(111, script.hash160(_pubkey(int("36" * 32, 16))))
    r = app.post("/account/address", json={"address": ltc_test, "chain": "ltctest"})
    assert r.status_code == 200, r.text
    return app, state, chain


def test_register_watches_the_segwit_address_and_balance_reads_it(client, monkeypatch):
    app, state, chain = _ready(client, monkeypatch)
    said = app.post("/account/segwit/register", json={"chain": "ltctest", "address": MINE})
    assert said.status_code == 200, said.text
    assert said.json()["watching"] == MINE
    bal = app.get("/account/segwit/balance", params={"address": MINE, "network": "ltctest"}).json()
    assert bal["watched"] is True and bal["balance"] == 0


def test_register_refuses_what_is_not_a_native_segwit_address_here(client, monkeypatch):
    app, _, _ = _ready(client, monkeypatch)
    for bad in (THEIRS, MINE.replace("tltc", "ltc"), "tltc1qnotanaddress", ""):
        r = app.post("/account/segwit/register", json={"chain": "ltctest", "address": bad})
        assert r.status_code == 400, (bad, r.text)


def test_register_takes_one_segwit_address_per_account_and_chain(client, monkeypatch):
    """The cap on the gap: an account cannot grow the watch list with strangers'
    addresses. (It still never proves it holds the m/84' key.)"""
    app, _, _ = _ready(client, monkeypatch)
    assert app.post("/account/segwit/register",
                    json={"chain": "ltctest", "address": MINE}).status_code == 200
    assert app.post("/account/segwit/register",
                    json={"chain": "ltctest", "address": MINE}).status_code == 200
    for n in range(3):
        other = script.p2wpkh_address(P, bytes([n + 1]) * 20)
        assert app.post("/account/segwit/register",
                        json={"chain": "ltctest", "address": other}).status_code == 409


def test_send_from_the_segwit_address_builds_a_bip143_offer(client, monkeypatch):
    app, state, chain = _ready(client, monkeypatch)
    assert app.post("/account/segwit/register", json={"chain": "ltctest", "address": MINE}).status_code == 200
    index = state.token_index(chain)
    with index.open() as db:
        db.conn.execute("INSERT OR REPLACE INTO utxo(txid,vout,address,value,height) VALUES (?,?,?,?,?)",
                        ("cd" * 32, 1, MINE, 3 * 100_000_000, 1))
        db.conn.commit()
    bal = app.get("/account/segwit/balance", params={"address": MINE, "network": "ltctest"}).json()
    assert bal["balance"] == 3 * 100_000_000

    offered = app.post("/account/send", json={"to": THEIRS, "amount": "1", "chain": "ltctest",
                                              "from": MINE})
    assert offered.status_code == 200, offered.text
    offer = offered.json()          # the offer's fields are the top level ("offer" is its id)
    assert offer["inputs"] and all(i.get("segwit") == "p2wpkh" for i in offer["inputs"]), offer["inputs"]
    assert all(i["address"] == MINE for i in offer["inputs"])
    # the sighashes are BIP143 over each coin's own amount:
    from arcade.rawchain import read_tx
    tx = read_tx(bytes.fromhex(offer["raw"]))
    outputs = list(tx.vout)
    inputs = [{"txid": i["txid"], "vout": i["vout"], "value": c["value"], "sequence": i["sequence"]}
              for i, c in zip(tx.vin, offer["inputs"])]
    for n, c in enumerate(offer["inputs"]):
        want = funding.sighash_witness(inputs, outputs, n, funding.witness_scriptcode(KH),
                                       int(c["value"]), version=tx.version, locktime=tx.locktime).hex()
        assert offer["sighashes"][n] == want, n
    # change comes back to the segwit address, never to a legacy script of the same key:
    legacy = bytes([0x76, 0xa9, 0x14]) + KH + bytes([0x88, 0xac])
    assert all(spk != legacy for _, spk in outputs)
    assert any(spk == script.p2wpkh_script(KH) for _, spk in outputs), "segwit change expected"


def test_send_from_refuses_a_non_segwit_from(client, monkeypatch):
    app, _, _ = _ready(client, monkeypatch)
    r = app.post("/account/send", json={"to": THEIRS, "amount": "1", "chain": "ltctest",
                                        "from": THEIRS})
    assert r.status_code == 400, r.text


def test_send_from_refuses_a_segwit_address_this_account_never_registered(client, monkeypatch):
    app, _, _ = _ready(client, monkeypatch)
    assert app.post("/account/segwit/register",
                    json={"chain": "ltctest", "address": MINE}).status_code == 200
    r = app.post("/account/send", json={"to": THEIRS, "amount": "1", "chain": "ltctest",
                                        "from": SOMEONE})
    assert r.status_code == 400 and "register" in r.json()["detail"], r.text


def test_the_segwit_routes_pass_the_public_door():
    from arcade.web import door
    assert "/account/segwit/register" in door.PUBLIC_POST
    assert "/account/segwit/balance" in door.PUBLIC_PAGES

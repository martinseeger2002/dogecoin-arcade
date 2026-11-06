"""Imported wallets, the node's half (plan item 18, 2026-10-20: "We should be able to import any with private
key or 12 word seed phrase, even if that wallet has not been used in Dogecoin arcade").

The key never comes here. What the node does, and what these hold still: an address is registered only with its
key's signature over the account (wallet_imports.check_proof); at most CAP_PER_CHAIN on a chain; an account reads and
spends only its own addresses; a send from an imported wallet takes only its coins and gives the change back to it;
one read-back per chain, and an import that would start a second keeps nothing; the sealed record round-trips and
the main vault blob is never touched.
"""
import hashlib
import json
import pathlib
import sys
import threading
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_me_page import _seat                                   # noqa: E402
from test_litecoin_testnet import _with_ltc_test                  # noqa: E402

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec, utils   # noqa: E402

from arcade import script, txbuild, wallet_imports as W          # noqa: E402
from arcade.config import NETWORKS                               # noqa: E402
from arcade.rpc import RpcError                                  # noqa: E402

P = NETWORKS["ltctest"]
ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
SOMEONE = script.b58check_encode(111, bytes(range(60, 80)))


def _key(secret: int, compressed: bool = True):
    priv = ec.derive_private_key(secret, ec.SECP256K1())
    fmt = serialization.PublicFormat.CompressedPoint if compressed else serialization.PublicFormat.UncompressedPoint
    return priv, priv.public_key().public_bytes(serialization.Encoding.X962, fmt)


def _sign(priv, digest: bytes) -> bytes:
    r, s = utils.decode_dss_signature(priv.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256()))))
    return utils.encode_dss_signature(r, min(s, ORDER - s))


def _proof(account: str, secret: int, kind: str = "p2pkh", network: str = "ltctest", params=P,
           compressed: bool = True, sign_for: str | None = None) -> dict:
    priv, pub = _key(secret, compressed)
    address = W.address_of(params, pub, kind)
    signature = _sign(priv, W.proof_digest(sign_for or account, network, address, pub.hex()))
    return {"chain": network, "address": address, "kind": kind, "pubkey": pub.hex(), "signature": signature.hex()}


def _tip(state, chain, height: int) -> None:
    """The index has read up to `height`, so a newly watched address has a past to read back."""
    with state.token_index(chain).open() as db:
        db.conn.execute("INSERT OR REPLACE INTO block (height, hash, prev_hash, time, tx_count, processed_at) "
                        "VALUES (?,?,?,?,?,?)", (height, f"{height:064x}", "00" * 32, 1, 0, 1))
        db.conn.commit()


class _Rpc:
    """A Core that reads empty blocks, with no `scantxoutset`; `gate` holds every block read until it is set."""

    def __init__(self, gate=None):
        self.gate = gate

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def call(self, method, *args):
        if method == "scantxoutset":
            raise RpcError(-32601, "Method not found")
        return None

    def get_block_hash(self, height):
        return f"{height:064x}"

    def get_block(self, block_hash, verbosity):
        if self.gate is not None:
            self.gate.wait(20)
        return {"tx": []}


def _ready(client, monkeypatch, *, online: bool = False, gate=None):
    app, state = client
    chain = _with_ltc_test(state, monkeypatch, ready=True)
    # Every import's past is read by the shared walk (2026-10-25; the only way since 2026-11-03, when the Core rescan
    # and its two-node rule went).
    if online:
        from arcade.web.state import ChainContext
        real_status, real_rpc = ChainContext.status, ChainContext.rpc
        monkeypatch.setattr(ChainContext, "status",
                            lambda self: {"online": True} if self.is_ltc_test else real_status(self))
        monkeypatch.setattr(ChainContext, "rpc", lambda self: _Rpc(gate) if self.is_ltc_test else real_rpc(self))
    account = _seat(app)
    return app, state, chain, account


def _wait_ready(app, address, seconds=20.0):
    until = time.time() + seconds
    while time.time() < until:
        said = app.post("/account/imported/rescan", json={"chain": "ltctest", "address": address}).json()
        if said.get("state") == "ready":
            return said
        time.sleep(0.1)
    raise AssertionError(f"{address} never finished: {said}")


# --- the proof -----------------------------------------------------------------------------------------------------

def test_an_import_with_its_key_s_signature_is_listed_and_watched(client, monkeypatch):
    app, state, chain, account = _ready(client, monkeypatch)
    asked = _proof(account, 0x1111)
    said = app.post("/account/imported/address", json=asked)
    assert said.status_code == 200, said.text
    # Nothing indexed yet: watched, but its past is not known -- it waits for a read-back (2026-11-04: an empty index
    # once made this "ready" with nothing, the operator's Flopcoin import).
    assert said.json()["state"] != "ready"
    listed = W.entries(state.setting(W.setting_key("ltctest", account)))
    assert [e["a"] for e in listed] == [asked["address"]]
    with state.token_index(chain).open() as db:
        assert asked["address"] in __import__("arcade.utxos", fromlist=["x"]).watching(db)
    view = app.get("/account/imported").json()
    assert [w["address"] for w in view["chains"]["ltctest"]] == [asked["address"]]
    # Asked again: the same wallet, not a second one.
    again = app.post("/account/imported/address", json=asked)
    assert again.status_code == 200 and again.json()["already"] is True
    assert len(W.entries(state.setting(W.setting_key("ltctest", account)))) == 1


def test_an_import_is_refused_without_the_key(client, monkeypatch):
    app, state, _, account = _ready(client, monkeypatch)
    good = _proof(account, 0x2222)
    for bad in (
        {**good, "signature": _proof(account, 0x3333)["signature"]},          # another key signed
        _proof(account, 0x2222, sign_for="ab" * 32),                         # signed for another account
        {**good, "address": SOMEONE},                                         # an address that is not this key's
        {**good, "signature": ""},
        {**good, "pubkey": "02" + "00" * 31},
    ):
        r = app.post("/account/imported/address", json=bad)
        assert r.status_code == 400, (bad, r.text)
    assert not W.entries(state.setting(W.setting_key("ltctest", account)))


def test_an_uncompressed_key_and_the_segwit_kinds_register_too(client, monkeypatch):
    app, _, _, account = _ready(client, monkeypatch)
    for secret, kind, compressed in ((0x4441, "p2pkh", False), (0x4442, "p2wpkh", True),
                                     (0x4443, "p2sh-p2wpkh", True)):
        r = app.post("/account/imported/address", json=_proof(account, secret, kind, compressed=compressed))
        assert r.status_code == 200, (kind, r.text)
    # SegWit from an uncompressed key is no address at all.
    with pytest.raises(W.ImportRefused):
        W.address_of(P, _key(0x4444, False)[1], "p2wpkh")
    # SegWit on a real-coin chain is refused, as the account's own SegWit is.
    with pytest.raises(W.ImportRefused):
        W.address_of(NETWORKS["main"], _key(0x4445)[1], "p2wpkh")


def test_at_most_twenty_on_a_chain(client, monkeypatch):
    app, state, _, account = _ready(client, monkeypatch)
    for n in range(W.CAP_PER_CHAIN):
        assert app.post("/account/imported/address", json=_proof(account, 0x5000 + n)).status_code == 200
    r = app.post("/account/imported/address", json=_proof(account, 0x5100))
    assert r.status_code == 400 and "at most 20" in r.json()["detail"], r.text
    assert len(W.entries(state.setting(W.setting_key("ltctest", account)))) == W.CAP_PER_CHAIN


# --- reading and spending: only the account's own ------------------------------------------------------------------

def test_another_account_cannot_read_or_spend_your_imported_address(client, monkeypatch):
    app, state, chain, alice = _ready(client, monkeypatch)
    hers = _proof(alice, 0x6001)
    assert app.post("/account/imported/address", json=hers).status_code == 200
    with state.token_index(chain).open() as db:
        db.conn.execute("INSERT INTO utxo(txid,vout,address,value,height) VALUES (?,?,?,?,?)",
                        ("ab" * 32, 0, hers["address"], 5 * 100_000_000, 1))
        db.conn.commit()
    mine = app.get("/account/coins", params={"chain": "ltctest", "address": hers["address"]})
    assert mine.status_code == 200 and mine.json()["coins"][0]["address"] == hers["address"]
    assert app.get("/account/activity", params={"chain": "ltctest", "address": hers["address"]}).status_code == 200

    _seat(app)                                                     # somebody else signs in, in this browser
    for path in ("/account/coins", "/account/activity"):
        r = app.get(path, params={"chain": "ltctest", "address": hers["address"]})
        assert r.status_code == 403, (path, r.text)
    assert app.post("/account/imported/rescan",
                    json={"chain": "ltctest", "address": hers["address"]}).status_code == 403
    sent = app.post("/account/send", json={"to": SOMEONE, "amount": "1", "chain": "ltctest",
                                           "from": hers["address"]})
    assert sent.status_code == 400, sent.text
    assert "offer" not in sent.json()
    view = app.get("/account/imported").json()
    assert view["chains"].get("ltctest") == [] and view["blob"] == ""


def test_a_send_from_an_imported_wallet_takes_only_its_coins_and_gives_change_back_to_it(client, monkeypatch):
    app, state, chain, account = _ready(client, monkeypatch)
    main = script.b58check_encode(111, script.hash160(_key(0x7000)[1]))
    assert app.post("/account/address", json={"address": main, "chain": "ltctest"}).status_code == 200
    imported = _proof(account, 0x7001)
    assert app.post("/account/imported/address", json=imported).status_code == 200
    with state.token_index(chain).open() as db:
        for n, (address, value) in enumerate([(main, 50), (imported["address"], 3), (imported["address"], 4)]):
            db.conn.execute("INSERT INTO utxo(txid,vout,address,value,height) VALUES (?,?,?,?,?)",
                            (f"{n + 1:02x}" * 32, 0, address, value * 100_000_000, 1))
        db.conn.commit()
    offered = app.post("/account/send", json={"to": SOMEONE, "amount": "5", "chain": "ltctest",
                                              "from": imported["address"]})
    assert offered.status_code == 200, offered.text
    offer = offered.json()
    assert offer["inputs"] and all(i["address"] == imported["address"] for i in offer["inputs"])
    from arcade.rawchain import read_tx
    outputs = list(read_tx(bytes.fromhex(offer["raw"])).vout)
    ours = script.b58check_decode(imported["address"])[1]
    change = [spk for _, spk in outputs if spk == bytes([0x76, 0xa9, 0x14]) + ours + bytes([0x88, 0xac])]
    assert change, "the change goes back to the imported address"
    assert not any(script.b58check_decode(main)[1] in spk for _, spk in outputs)
    # More than it holds is refused: the main wallet's 50 are never borrowed.
    too_much = app.post("/account/send", json={"to": SOMEONE, "amount": "8", "chain": "ltctest",
                                               "from": imported["address"]})
    assert too_much.status_code == 400, too_much.text


# --- one read-back per chain ----------------------------------------------------------------------------------------

def test_an_import_during_a_read_back_is_kept_and_read_back_next(client, monkeypatch):
    """2026-10-21: an import while a read-back runs is not refused -- it waits its turn, back to back. The
    read-back is the shared walk: the first wallet's walk is held, a second wallet is imported meanwhile, and both
    are ready once the walking is done."""
    from arcade import coinwalk
    gate = threading.Event()
    app, state, chain, account = _ready(client, monkeypatch, online=True)
    _tip(state, chain, P.messaging_start_height + 40)

    def read(self, c, top, bottom, addresses, progress=None):
        gate.wait(20)
        return []
    monkeypatch.setattr(coinwalk.Reader, "read", read)
    try:
        first = _proof(account, 0x8001)
        said = app.post("/account/imported/address", json=first)
        assert said.status_code == 200, said.text
        assert said.json()["state"] == "rescan"
        until = time.time() + 10
        while not app.get("/account/imported/busy?chain=ltctest").json()["busy"] and time.time() < until:
            time.sleep(0.05)
        busy = app.get("/account/imported/busy?chain=ltctest").json()
        assert busy["busy"] and busy["says"].startswith("A rescan is running on this coin right now. You can still "
                                                         "import: your wallet joins the next rescan")
        second = _proof(account, 0x8002)
        queued = app.post("/account/imported/address", json=second)
        assert queued.status_code == 200, queued.text
        assert queued.json()["state"] == "rescan"
        listed = [e["a"] for e in W.entries(state.setting(W.setting_key("ltctest", account)))]
        assert listed == [first["address"], second["address"]]
    finally:
        gate.set()
    assert _wait_ready(app, first["address"])["state"] == "ready"
    assert _wait_ready(app, second["address"])["state"] == "ready"
    until = time.time() + 10
    while app.get("/account/imported/busy?chain=ltctest").json()["busy"] and time.time() < until:
        time.sleep(0.05)
    assert app.get("/account/imported/busy?chain=ltctest").json()["busy"] is False


class _WalletCore:
    """A 1.14 Core with a wallet: importmulti holds until `gate` is set (the rescan), then lists one coin each."""

    def __init__(self, gate, tip=500):
        self.gate, self.tip, self.batches = gate, tip, []
        self._timeout = 120.0

    def close(self):
        pass

    def call(self, method, *args):
        if method == "getwalletinfo":
            return {"walletversion": 130000}
        if method == "importmulti":
            self.batches.append(sorted(r["scriptPubKey"]["address"] for r in args[0]))
            assert args[1] == {"rescan": True} and all(r["watchonly"] and r["timestamp"] == 0 for r in args[0])
            self.gate.wait(20)
            return [{"success": True} for _ in args[0]]
        if method == "getblockcount":
            return self.tip
        if method == "validateaddress":                # not watched before its first import
            return {"isvalid": True, "iswatchonly": any(args[0] in b for b in self.batches)}
        if method == "listreceivedbyaddress":
            return []
        if method == "listunspent":
            return [{"txid": hashlib.sha256(a.encode()).hexdigest(), "vout": 0, "address": a, "confirmations": 200,
                     "amount": 1.5, "scriptPubKey": txbuild.p2pkh_script(a).hex()}
                    for i, a in enumerate(args[2])]
        raise AssertionError(method)


def test_with_no_node_to_read_the_chain_the_read_back_waits_its_turn(client, monkeypatch):
    app, state, chain, account = _ready(client, monkeypatch)            # no Core here, none to borrow
    _tip(state, chain, P.messaging_start_height + 40)
    asked = _proof(account, 0x9001)
    said = app.post("/account/imported/address", json=asked)
    assert said.status_code == 200, said.text
    assert said.json()["state"] == "none"
    assert said.json()["says"].startswith("There is no node to borrow right now")
    # Kept (it is queued, not refused), and said the same way when asked again.
    assert [e["a"] for e in W.entries(state.setting(W.setting_key("ltctest", account)))] == [asked["address"]]
    again = app.post("/account/imported/rescan", json={"chain": "ltctest", "address": asked["address"]})
    assert again.json()["state"] == "none"


def test_an_address_already_watched_from_the_floor_is_ready_at_once(client, monkeypatch):
    app, state, chain, account = _ready(client, monkeypatch)
    asked = _proof(account, 0x9101)
    from arcade import utxos
    with state.token_index(chain).open() as db:
        utxos.watch(db, asked["address"], P.messaging_start_height, "seen before")
        db.conn.commit()
    _tip(state, chain, P.messaging_start_height + 40)
    said = app.post("/account/imported/address", json=asked)
    assert said.json()["state"] == "ready" and said.json()["says"].startswith("Imported. This arcade was already")


# --- removing one ---------------------------------------------------------------------------------------------------

def test_removing_an_imported_wallet_stops_watching_it_unless_somebody_else_wants_it(client, monkeypatch):
    app, state, chain, alice = _ready(client, monkeypatch)
    from arcade import utxos
    hers = _proof(alice, 0xa001)
    assert app.post("/account/imported/address", json=hers).status_code == 200
    bob = _seat(app)
    assert app.post("/account/imported/address", json=_proof(bob, 0xa001)).status_code == 200
    gone = app.post("/account/imported/forget", json={"chain": "ltctest", "address": hers["address"]}).json()
    assert gone["removed"] is True and gone["watched"] is True             # alice still has it
    with state.token_index(chain).open() as db:
        assert hers["address"] in utxos.watching(db)
    assert app.get("/account/coins", params={"chain": "ltctest", "address": hers["address"]}).status_code == 403
    # Not his any more, so removing it again changes nothing.
    assert app.post("/account/imported/forget",
                    json={"chain": "ltctest", "address": hers["address"]}).json()["removed"] is False


def test_removing_the_last_holder_stops_watching(client, monkeypatch):
    app, state, chain, account = _ready(client, monkeypatch)
    from arcade import utxos
    asked = _proof(account, 0xa101)
    assert app.post("/account/imported/address", json=asked).status_code == 200
    gone = app.post("/account/imported/forget", json={"chain": "ltctest", "address": asked["address"]}).json()
    assert gone == {"address": asked["address"], "removed": True, "watched": False}
    with state.token_index(chain).open() as db:
        assert asked["address"] not in utxos.watching(db)
    assert state.setting(W.setting_key("ltctest", account)) is None


# --- the sealed record, and the vault it sits beside ----------------------------------------------------------------

SEALED = {"kdf": "PBKDF2-SHA512", "iterations": 600000, "salt": "00" * 16, "nonce": "11" * 12, "sealed": "22" * 40}
VAULT = {"kdf": "PBKDF2-SHA512", "iterations": 600000, "salt": "aa" * 16, "nonce": "bb" * 12, "sealed": "cc" * 60}


def test_the_sealed_record_round_trips_and_the_vault_blob_is_untouched(client):
    from nacl.signing import SigningKey
    from test_imports_everywhere import _seat as seat_with, _sig
    app, state = client
    key = SigningKey(b"\x21" * 32)
    account = seat_with(app, key)
    text = json.dumps(SEALED, separators=(",", ":"))
    # No vault here: nothing to share a password with.
    assert app.post("/account/imported", json={"blob": text, "version": 1,
                                               "signature": _sig(key, 1, text)}).status_code == 404
    state.vault().put("importer", account, SOMEONE, json.dumps(VAULT))
    before = app.get("/signin/importer").json()

    put = app.post("/account/imported", json={"blob": text, "version": 1, "signature": _sig(key, 1, text)})
    assert put.status_code == 200, put.text
    stamp = put.json()["updated"]
    got = app.get("/account/imported").json()
    assert json.loads(got["blob"]) == SEALED and got["updated"] == stamp and got["version"] == 1
    assert got["vault"] == VAULT
    # Written since it was read: refused, never overwritten.
    other = json.dumps({**SEALED, "sealed": "33" * 40}, separators=(",", ":"))
    stale = app.post("/account/imported", json={"blob": other, "version": 1, "signature": _sig(key, 1, other)})
    assert stale.status_code == 409 and stale.json()["updated"] == stamp
    # Not a sealed record: refused.
    for bad in ({"kdf": "none"}, {**SEALED, "extra": 1}, {**SEALED, "sealed": "xyz"}, {**SEALED, "iterations": 10}):
        bad_text = json.dumps(bad, separators=(",", ":"))
        assert app.post("/account/imported", json={"blob": bad_text, "version": 2,
                                                   "signature": _sig(key, 2, bad_text)}).status_code == 400, bad
    newer = json.dumps({**SEALED, "sealed": "44" * 40}, separators=(",", ":"))
    assert app.post("/account/imported", json={"blob": newer, "version": 2,
                                               "signature": _sig(key, 2, newer)}).status_code == 200
    # The main vault: byte for byte what it was, through every write above.
    assert app.get("/signin/importer").json() == before
    assert state.vault().get("importer")["blob"] == json.dumps(VAULT)
    # Another account sees none of it.
    _seat(app)
    assert app.get("/account/imported").json()["blob"] == ""


def test_the_main_wallet_s_name_is_a_setting_of_its_own(client, monkeypatch):
    app, state, _, account = _ready(client, monkeypatch)
    said = app.post("/account/imported/name", json={"chain": "ltctest", "name": "  Spending   money "}).json()
    assert said["name"] == "Spending money"
    assert app.get("/account/imported").json()["names"]["ltctest"] == "Spending money"
    app.post("/account/imported/name", json={"chain": "ltctest", "name": ""})
    assert app.get("/account/imported").json()["names"]["ltctest"] == ""


def test_the_routes_need_a_session():
    from fastapi.testclient import TestClient  # noqa: F401 -- the routes answer 403 with no account
    from arcade.web import door
    for path in ("/account/imported", "/account/imported/address", "/account/imported/rescan",
                 "/account/imported/forget", "/account/imported/name"):
        assert path in door.PUBLIC_POST, path
    assert "/account/imported" in door.PUBLIC_PAGES


def test_signed_out_is_refused(client):
    app, _ = client
    assert app.get("/account/imported").status_code == 403
    assert app.post("/account/imported/address", json={}).status_code == 403
    assert app.post("/account/imported", json={"blob": SEALED}).status_code == 403


# --- the words a person reads ---------------------------------------------------------------------------------------

def test_a_wait_is_said_in_words():
    assert W.say_wait(None) == "a few minutes"
    assert W.say_wait(30) == "a few minutes"
    assert W.say_wait(25 * 60) == "about 25 minutes"
    assert W.say_wait(70 * 60) == "about an hour"
    assert W.say_wait(5 * 3600) == "about 5 hours"
    assert W.say_wait(3 * 86400) == "about 3 days"


def test_the_listed_entries_are_read_defensively():
    assert W.entries(None) == [] and W.entries("not json") == [] and W.entries([{"a": 1}]) == []
    assert W.entries([{"a": "x", "k": "p2pkh"}]) == [{"a": "x", "k": "p2pkh", "p": "", "t": 0}]
    assert W.entries([{"a": "x", "k": "taproot"}]) == []


def test_no_wallet_is_imported_on_pep_test(client, monkeypatch):
    """2026-10-21: "No imports for pep test"."""
    app, state, chain, account = _ready(client, monkeypatch)
    from arcade.config import NETWORKS as N
    asked = _proof(account, 0xC001, network="test", params=N["test"])
    refused = app.post("/account/imported/address", json=asked)
    assert refused.status_code == 400, refused.text      # "cannot be imported" where PEP TEST runs
    assert not W.entries(state.setting(W.setting_key("test", account)))


class _BusyCore(_WalletCore):
    """A Core still at a rescan sent before an arcade restart: its queue full until `free`, peers since `up`."""

    def __init__(self, gate, up, watched=(), history=()):
        super().__init__(gate)
        self.free, self.up, self.watched, self.history = False, up, set(watched), set(history)

    def call(self, method, *args):
        from arcade.rpc import RpcTransportError
        if not self.free:
            raise RpcTransportError("non-JSON response from dogecoind (HTTP 500): 'Work queue depth exceeded'")
        if method == "getpeerinfo":
            return [{"conntime": int(self.up)}, {"conntime": int(self.up) + 500}]
        if method == "validateaddress":
            return {"isvalid": True, "iswatchonly": args[0] in self.watched}
        if method == "listreceivedbyaddress":
            return [{"address": a, "txids": ["ab" * 32] if a in self.history else []} for a in self.watched]
        return super().call(method, *args)


def test_a_busy_core_is_waited_for_and_a_rescan_it_already_ran_is_not_sent_again(tmp_path):
    """2026-10-21: an arcade restart cut the connection to a Dogecoin importmulti the Core went on with; the
    retries met a full queue and counted as failures, and a free Core would have been sent the whole-chain rescan
    again (hours). A busy Core is CoreBusy, and a rescan sent before that ran to its end is not sent twice."""
    from arcade import utxos
    from arcade.ledger import LedgerIndex
    gate = threading.Event()
    gate.set()
    index = LedgerIndex(tmp_path / "t.sqlite", P, lambda: None)
    a = W.address_of(P, _key(0xB001)[1], "p2pkh")
    sent_at = time.time() - 3600
    core = _BusyCore(gate, up=sent_at - 86400, watched={a})
    kept = {}
    with pytest.raises(utxos.CoreBusy):
        utxos.seed_from_core_wallet(index, core, P, [a], 500, sent={"at": sent_at, "addresses": [a]},
                                    remember=kept.update)
    assert core.batches == []
    # Busy with nothing remembered (a restart cut the connection to the rescan it sent): remembered as sent now.
    fresh = {}
    with pytest.raises(utxos.CoreBusy):
        utxos.seed_from_core_wallet(index, core, P, [a], 500, sent=None, remember=fresh.update)
    assert fresh["addresses"] == [a] and abs(fresh["at"] - time.time()) < 5
    core.free = True
    filed = utxos.seed_from_core_wallet(index, core, P, [a], 500, sent={"at": sent_at, "addresses": [a]},
                                        remember=kept.update)
    assert filed == {a: 1} and core.batches == [], "listed with no second rescan"
    # Restarted since the send (no peer that old): sent again, and remembered first.
    core.up = time.time() - 60
    utxos.seed_from_core_wallet(index, core, P, [a], 500, sent={"at": sent_at, "addresses": [a]},
                                remember=kept.update)
    assert core.batches == [[a]] and kept["addresses"] == [a]


def test_a_rescan_that_found_history_is_not_sent_again_though_the_peers_are_new(tmp_path):
    """2026-10-22, "looks like rescan started over": a 22-hour Dogecoin rescan drops and makes its peers anew,
    so the peer test said "restarted" and the whole chain was sent again. The wallet already holding transactions for
    the address is proof enough that a rescan ran: its coins are listed, nothing is rescanned."""
    from arcade import utxos
    from arcade.ledger import LedgerIndex
    gate = threading.Event()
    gate.set()
    index = LedgerIndex(tmp_path / "t.sqlite", P, lambda: None)
    a = W.address_of(P, _key(0xB101)[1], "p2pkh")
    sent_at = time.time() - 3600
    core = _BusyCore(gate, up=time.time() - 60, watched={a}, history={a})     # peers newer than the send
    core.free = True
    filed = utxos.seed_from_core_wallet(index, core, P, [a], 500, sent={"at": sent_at, "addresses": [a]},
                                        remember=lambda said: None)
    assert filed == {a: 1} and core.batches == [], "listed with no second rescan"
    # Watched but no history, and the peers say restarted: rescanned, as before (it may have been cut short).
    empty = _BusyCore(gate, up=time.time() - 60, watched={a})
    empty.free = True
    utxos.seed_from_core_wallet(index, empty, P, [a], 500, sent={"at": sent_at, "addresses": [a]},
                                remember=lambda said: None)
    assert empty.batches == [[a]]


def test_one_node_of_a_chain_is_enough_to_import_on_it(client, monkeypatch):
    """2026-11-03 ("Why do we have to have two cores on the arcade mesh in order to import a wallet ... a leftover
    rule from when we were re-scanning with the node?" -- "Okay cool"): the two-node rule went with the Core rescan.
    A test arcade stands alone with one node of the chain, and the import is open: the walk reads with one reader."""
    from arcade.web import app as webapp
    app, state, chain, account = _ready(client, monkeypatch)
    assert not hasattr(webapp, "IMPORT_CORES_MIN")
    assert "ltctest" not in app.get("/account/imported").json()["import_closed"]
    got = app.post("/account/imported/address", json=_proof(account, 0x9201))
    assert got.status_code == 200, got.text
    assert "import_closed" in app.get("/me/wallet").text, "PEP TEST is still greyed the same way"
    admin = app.get("/admin/api/imports?chain=ltctest", headers={"host": "127.0.0.1:8420", "x-arcade-admin": "1"})
    assert admin.status_code == 200, admin.text
    assert "walk" not in admin.json()["settings"], "no switch back to a Core rescan"

def test_an_import_is_found_by_the_shared_walk_and_is_ready(client, monkeypatch):
    """2026-10-25: an imported wallet is found by the arcades walking the blocks (no Core rescan); the walk's
    coins are checked here and the wallet says ready."""
    from arcade import coinlist, coinwalk
    app, state, chain, account = _ready(client, monkeypatch, online=True)
    _tip(state, chain, P.messaging_start_height + 40)
    walked = []
    monkeypatch.setattr(coinwalk.Reader, "read",
                        lambda self, c, top, bottom, addresses, progress=None: walked.append((top, bottom)) or [])
    first = _proof(account, 0x8101)
    said = app.post("/account/imported/address", json=first)
    assert said.status_code == 200, said.text
    assert _wait_ready(app, first["address"])["state"] == "ready"
    assert walked and min(b for _, b in walked) == 0, "walked from block 0 (in chunks since 2026-11-05)"
    with state.token_index(chain).open() as db:
        assert coinlist.covers(coinlist.coverage(db, first["address"], P.messaging_start_height + 40), 0,
                               P.messaging_start_height + 40)


def test_an_import_before_the_chain_s_index_started_waits_for_it_then_walks_from_block_zero(client, monkeypatch):
    """2026-11-04, "I imported my flop key, but no balance showed up yet": Flopcoin's floor was two days ahead,
    the index held no block, and the import was "ready" at once with nothing (its watch said since 0). Now it waits,
    says why, and walks blocks 0..top as soon as the index holds a block."""
    from arcade import coinwalk
    app, state, chain, account = _ready(client, monkeypatch, online=True)
    walked = []
    monkeypatch.setattr(coinwalk.Reader, "read",
                        lambda self, c, top, bottom, addresses, progress=None: walked.append((top, bottom)) or [])
    asked = _proof(account, 0x8301)
    said = app.post("/account/imported/address", json=asked).json()
    assert said["state"] == "rescan" and "has not started yet" in said["says"], said
    time.sleep(0.3)
    assert walked == [], "no walk of blocks 0..0"
    again = app.post("/account/imported/rescan", json={"chain": "ltctest", "address": asked["address"]}).json()
    assert again["state"] != "ready"
    _tip(state, chain, P.messaging_start_height + 40)
    assert _wait_ready(app, asked["address"])["state"] == "ready"
    assert walked and min(b for _, b in walked) == 0 and max(t for t, _ in walked) == P.messaging_start_height + 40, \
        walked


# --- a sign-in reads only what it needs (2026-11-05: @apple's unlock, 7,000 coins, took 12-18 s) --------------

def test_a_sign_in_reads_the_record_without_reading_any_coins(client, monkeypatch):
    app, state, chain, account = _ready(client, monkeypatch)
    asked = _proof(account, 0x1111)
    assert app.post("/account/imported/address", json=asked).status_code == 200
    full = app.get("/account/imported").json()
    assert [w["address"] for w in full["chains"]["ltctest"]] == [asked["address"]]
    import arcade.utxos as U
    read = []
    monkeypatch.setattr(U, "unspent", lambda *a, **k: read.append(a) or [])
    light = app.get("/account/imported?record=1").json()
    assert read == [], "opening the record reads no imported address's coins"
    assert light["record_only"] is True and "chains" not in light and "busy" not in light
    for key in ("blob", "updated", "version", "signed", "siblings", "vault", "tag", "host"):
        assert light[key] == full[key], key


def test_the_addresses_a_sign_in_checks_come_without_balances(client, monkeypatch):
    app, state = client
    account = _seat(app)
    full = app.get("/account").json()
    import arcade.utxos as U
    monkeypatch.setattr(U, "balance", lambda *a, **k: (_ for _ in ()).throw(AssertionError("a balance was read")))
    light = app.get("/account?only=addresses")
    assert light.status_code == 200, light.text
    said = light.json()
    assert said["pubkey"] == account
    assert {c["network"]: c["address"] for c in said["chains"]} == {c["network"]: c["address"] for c in full["chains"]}
    assert all("balance" not in c for c in said["chains"])


def test_lazy_wallets_read_coins_only_for_the_wallet_open(client, monkeypatch):
    app, state, chain, account = _ready(client, monkeypatch)
    one, two = _proof(account, 0x1111), _proof(account, 0x2222)
    for asked in (one, two):
        assert app.post("/account/imported/address", json=asked).status_code == 200
    import arcade.utxos as U
    real, read = U.unspent, []
    monkeypatch.setattr(U, "unspent", lambda db, address, *a, **k: read.append(address) or real(db, address, *a, **k))
    said = app.get(f"/account/imported?lazy=1&chain=ltctest&address={one['address']}").json()
    rows = {r["address"]: r for r in said["chains"]["ltctest"]}
    assert read == [one["address"]], "only the open wallet's coins are read"
    assert rows[one["address"]].get("lazy") is None and rows[one["address"]]["spendable"] == 0
    assert rows[two["address"]]["lazy"] is True and rows[two["address"]]["spendable"] is None
    assert "state" in rows[two["address"]], "its read-back state is still said"
    read.clear()
    none_open = app.get("/account/imported?lazy=1").json()
    assert read == [] and all(r["lazy"] for r in none_open["chains"]["ltctest"])
    read.clear()
    app.get("/account/imported")
    assert sorted(read) == sorted([one["address"], two["address"]]), "without lazy, every one as before"

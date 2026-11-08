"""An @name pays a mainnet DigiDollar (2026-11-08, SadFrog: "add the protocol so names resolve on mainnet too").

The name chain shows only an account's test-coin key, so a mainnet DigiDollar address (DD...) is made from the key the
account publishes in its own profile: the key announcement's ANNOUNCE_TAG_DD_KEY section, with a BIP-340 proof by that
key over the network, the name's address and the key (arcade/ddaddress.py). It is used only when the proof checks, the
announcement was signed by the name's own address, and the key is the DigiByte address that announcement published.
"""
import hashlib
import json
import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client  # noqa: F401,E402

from arcade import ddaddress as ddlib  # noqa: E402
from arcade import taproot  # noqa: E402
from arcade.messaging import envelope as E  # noqa: E402
from arcade.script import b58check_encode, hash160  # noqa: E402
from arcade.web.app import create_app  # noqa: E402

OUTSIDE = {"host": "arcade.example.com", "cf-connecting-ip": "198.51.100.9"}
NAME_SECRET, MAIN_SECRET, THIEF_SECRET = 0x41 * 7 + 11, 0x5EED * 1013, 0x7EEF * 977


def _pub(secret: int) -> bytes:
    x, y = taproot.mul(secret)
    return bytes([2 + (y & 1)]) + x.to_bytes(32, "big")


def _schnorr(secret: int, message: bytes) -> bytes:
    """BIP-340 signing, as coins.js signSchnorr does (a fixed nonce is fine for a test: verification never sees it)."""
    x, y = taproot.mul(secret)
    d = secret if y % 2 == 0 else taproot.N - secret
    k0 = int.from_bytes(hashlib.sha256(d.to_bytes(32, "big") + message).digest(), "big") % taproot.N
    rx, ry = taproot.mul(k0)
    k = k0 if ry % 2 == 0 else taproot.N - k0
    px = x.to_bytes(32, "big")
    e = int.from_bytes(taproot.tagged("BIP0340/challenge", rx.to_bytes(32, "big") + px + message), "big") % taproot.N
    return rx.to_bytes(32, "big") + ((k + e * d) % taproot.N).to_bytes(32, "big")


NAME_KEY, MAIN_KEY, THIEF_KEY = _pub(NAME_SECRET), _pub(MAIN_SECRET), _pub(THIEF_SECRET)
NAME_HASH = hash160(NAME_KEY)


def _proof(secret: int, name_hash: bytes = NAME_HASH, network: str = "dgb") -> dict:
    key = _pub(secret)
    return {"key": key.hex(), "sig": _schnorr(secret, ddlib.proof_message(network, name_hash, key)).hex()}


def _tx(key: bytes = NAME_KEY, sighash: str = "[ALL]") -> dict:
    return {"vin": [{"scriptSig": {"asm": f"3044{sighash} {key.hex()}"}}]}


# --- the bytes ---------------------------------------------------------------------------------------------------------

def test_the_announcement_carries_the_key_and_its_proof_last():
    sig = bytes(range(64))
    body = E.build_key_announcement(b"\x01" * 32, hash160=NAME_HASH, tag="apple", doge_hash160=b"\x33" * 20,
                                    chains={"dgb": hash160(MAIN_KEY)}, dd_keys={"dgb": (MAIN_KEY, sig)})
    said = E.parse_announced_extras(body)
    assert said["dd_keys"] == {"dgb": (MAIN_KEY, sig)} and said["chains"] == {"dgb": hash160(MAIN_KEY)}
    assert body.endswith(bytes([E.ANNOUNCE_TAG_DD_KEY, 3]) + b"dgb" + MAIN_KEY + sig), "last"
    # an older reader stops at the section it does not know, having read the rest
    assert E.parse_announced_extras(body[:-(102)])["chains"] == {"dgb": hash160(MAIN_KEY)}
    assert E.parse_announced_extras(body[:-1])["dd_keys"] == {}, "a cut record is not read"
    with pytest.raises(E.EnvelopeError):
        E.build_key_announcement(b"\x01" * 32, hash160=NAME_HASH, dd_keys={"dgb": (MAIN_KEY[1:], sig)})


def test_the_store_keeps_the_records_newest_first(client):
    _, state = client
    one = json.dumps({"dgb": _proof(MAIN_SECRET)})
    with state.store() as store:
        store.add_key_announcement("a1" * 32, "nApple", b"\x01" * 32, "fp", 10, 1, stated=True, dd_keys=one)
        store.add_key_announcement("a2" * 32, "nApple", b"\x01" * 32, "fp", 11, 2, stated=True)
        store.add_key_announcement("a3" * 32, "nApple", b"\x01" * 32, "fp", 0, 0, stated=True, dd_keys=one)
        assert [r["txid"] for r in store.dd_key_records("nApple", "dgb")] == ["a3" * 32, "a1" * 32], "pool first"
        assert store.dd_key_records("nApple", "dgbtest") == [] and store.dd_key_records("nPear", "dgb") == []
        store.add_key_announcement("a1" * 32, "nApple", b"\x01" * 32, "fp", 10, 1, stated=True)   # older rescan
        assert len(store.dd_key_records("nApple", "dgb")) == 2, "a reader that saw nothing does not take it down"


# --- the proof ---------------------------------------------------------------------------------------------------------

def test_a_proved_record_gives_the_key():
    record = _proof(MAIN_SECRET)
    assert ddlib.proof_ok("dgb", NAME_HASH, MAIN_KEY, bytes.fromhex(record["sig"]))
    assert ddlib.proved("dgb", NAME_HASH, record, _tx()) == MAIN_KEY
    assert ddlib.proved("dgb", NAME_HASH, record, _tx(), published=hash160(MAIN_KEY)) == MAIN_KEY


def test_a_forged_record_is_refused():
    good = _proof(MAIN_SECRET)
    # a wrong signature: somebody else's key, "proved" by a signature that is not that key's
    stolen = {"key": MAIN_KEY.hex(), "sig": _proof(THIEF_SECRET)["sig"]}
    assert ddlib.proved("dgb", NAME_HASH, stolen, _tx()) is None
    assert ddlib.proved("dgb", NAME_HASH, {**good, "sig": "00" * 64}, _tx()) is None
    # a proof made for another name, another network, or garbage
    assert ddlib.proved("dgb", hash160(THIEF_KEY), good, _tx(THIEF_KEY)) is None
    assert ddlib.proved("dgb", NAME_HASH, _proof(MAIN_SECRET, network="dgbtest"), _tx()) is None
    assert ddlib.proved("dgb", NAME_HASH, {"key": "zz"}, _tx()) is None
    # a thief's own key, properly proved for the name, but the name's address never signed the announcement
    theirs = _proof(THIEF_SECRET)
    assert ddlib.proved("dgb", NAME_HASH, theirs, _tx(THIEF_KEY)) is None
    assert ddlib.proved("dgb", NAME_HASH, good, _tx(sighash="[NONE|ANYONECANPAY]")) is None, "signed every output"
    # the key is not the DigiByte address the same announcement published
    assert ddlib.proved("dgb", NAME_HASH, good, _tx(), published=hash160(THIEF_KEY)) is None


# --- the window's answer -------------------------------------------------------------------------------------------------

class _Index:
    def __init__(self, claims):
        self.claims = claims

    def claim_of(self, tag):
        return self.claims.get(tag)

    def address_of(self, tag):
        return (self.claims.get(tag) or {}).get("address")


class _Rpc:
    def __init__(self, txs):
        self.txs, self.asked = txs, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def call(self, method, txid, verbose):
        assert method == "getrawtransaction"
        self.asked.append(txid)
        if txid not in self.txs:
            raise RuntimeError("No such transaction")
        return self.txs[txid]


@pytest.fixture
def window(app_state, monkeypatch):
    app_state.set_setting("public_hosts", ["arcade.example.com"])
    version = app_state.messaging.params.pubkeyhash_version
    apple = b58check_encode(version, NAME_HASH)
    monkeypatch.setattr(type(app_state), "token_index",
                        lambda self, chain: _Index({"apple": {"address": apple, "claimed_txid": "cc" * 32}}))
    rpc = _Rpc({})
    monkeypatch.setattr(app_state.messaging, "rpc", lambda: rpc)
    yield TestClient(create_app(app_state)), app_state, apple, rpc
    app_state.set_setting("public_hosts", [])


def _ask(app, to="@apple", chain="dgb"):
    return app.get("/lite/api/ddaddress", params={"to": to, "chain": chain}, headers=OUTSIDE)


def test_no_record_says_so_plainly(window):
    app, *_ = window
    said = _ask(app)
    assert said.status_code == 400
    assert said.json()["detail"] == ("@apple hasn't published a mainnet DigiDollar address yet: ask them for their "
                                     "DigiDollar address (DD...)")
    assert _ask(app, "@nobody").json()["detail"] == "nobody holds @nobody"


def test_a_published_proved_record_resolves_to_the_owners_dd_address(window):
    app, state, apple, rpc = window
    dgb_address = b58check_encode(30, hash160(MAIN_KEY))
    with state.store() as store:
        store.add_key_announcement("d1" * 32, apple, b"\x01" * 32, "fp", 50, 1, stated=True,
                                   chain_addresses=json.dumps({"dgb": dgb_address}),
                                   dd_keys=json.dumps({"dgb": _proof(MAIN_SECRET)}))
    rpc.txs["d1" * 32] = _tx()
    said = _ask(app)
    assert said.status_code == 200, said.text
    assert said.json() == {"address": ddlib.address("dgb", MAIN_KEY), "tag": "apple"}
    assert said.json()["address"].startswith("DD")
    assert _ask(app).json() == said.json() and rpc.asked == ["d1" * 32], "read once, then kept"


def test_a_forged_record_never_redirects_the_payment(window):
    app, state, apple, rpc = window
    rows = {
        "f1" * 32: (_tx(THIEF_KEY), _proof(THIEF_SECRET), ""),                        # not sent by @apple's address
        "f2" * 32: (_tx(), {"key": THIEF_KEY.hex(), "sig": _proof(MAIN_SECRET)["sig"]}, ""),   # proof is not the key's
        "f3" * 32: (_tx(), _proof(MAIN_SECRET), b58check_encode(30, hash160(THIEF_KEY))),      # not its DGB address
    }
    with state.store() as store:
        for n, (txid, (tx, record, published)) in enumerate(rows.items()):
            rpc.txs[txid] = tx
            store.add_key_announcement(txid, apple, b"\x01" * 32, "fp", 60 + n, 1, stated=True,
                                       chain_addresses=json.dumps({"dgb": published}) if published else "",
                                       dd_keys=json.dumps({"dgb": record}))
    refused = _ask(app)
    assert refused.status_code == 400 and "hasn't published" in refused.json()["detail"]
    # an older genuine record still counts: a newer forgery cannot hide it
    with state.store() as store:
        store.add_key_announcement("e0" * 32, apple, b"\x01" * 32, "fp", 40, 1, stated=True,
                                   dd_keys=json.dumps({"dgb": _proof(MAIN_SECRET)}))
    rpc.txs["e0" * 32] = _tx()
    assert _ask(app).json()["address"] == ddlib.address("dgb", MAIN_KEY)


def test_test_chains_are_unchanged(window):
    app, state, apple, rpc = window
    # @apple's test-coin key, read from its claim as before; a published mainnet key plays no part there
    rpc.txs["cc" * 32] = _tx()
    with state.store() as store:
        store.add_key_announcement("d1" * 32, apple, b"\x01" * 32, "fp", 50, 1, stated=True,
                                   dd_keys=json.dumps({"dgb": _proof(MAIN_SECRET)}))
    rpc.txs["d1" * 32] = _tx()
    said = _ask(app, chain="dgbtest")
    assert said.status_code == 200 and said.json()["address"] == ddlib.address("dgbtest", NAME_KEY)
    typed = ddlib.address("dgb", THIEF_KEY)
    assert _ask(app, typed).json()["address"] == typed, "a typed DD... is paid as typed"


# --- writing it --------------------------------------------------------------------------------------------------------

def _dgb_here(state, monkeypatch):
    from arcade.web.state import ChainContext
    chain = ChainContext(network="dgb", role="ledger", label="DigiByte", conf=pathlib.Path("/nonexistent/d.conf"),
                         borrowed=True)
    monkeypatch.setattr(state, "more", {"dgb": chain}, raising=False)
    monkeypatch.setattr(type(state), "chain_ready", lambda self, c: True)
    return chain


def test_the_profile_carries_the_key_only_with_a_proof_that_checks(client, monkeypatch):
    from test_me_page import _seat
    from arcade.web import app as webapp
    app, state = client
    _dgb_here(state, monkeypatch)
    _seat(app)
    net = state.messaging.network
    home = b58check_encode(state.messaging.params.pubkeyhash_version, NAME_HASH)
    assert app.post("/account/address", json={"address": home, "chain": net,
                                               "coin_pubkey": NAME_KEY.hex()}).status_code == 200
    app.post("/account/address", json={"address": b58check_encode(30, hash160(MAIN_KEY)), "chain": "dgb",
                                       "coin_pubkey": MAIN_KEY.hex()})
    built = []
    real = webapp.envelopelib.build_key_announcement

    def spy(*a, **kw):
        built.append(kw.get("dd_keys"))
        return real(*a, **kw)

    monkeypatch.setattr(webapp.envelopelib, "build_key_announcement", spy)
    good = {"network": "dgb", **_proof(MAIN_SECRET)}
    app.post("/account/profile", json={"key": "01" * 32, "bio": "hi", "dd": good})      # funding may refuse; built first
    assert built[-1] == {"dgb": (MAIN_KEY, bytes.fromhex(good["sig"]))}
    forged = app.post("/account/profile", json={"key": "01" * 32, "dd": {**good, "sig": "00" * 64}})
    assert forged.status_code == 400 and "proof does not check" in forged.json()["detail"]
    assert app.post("/account/announce", json={"key": "01" * 32, "dd": {**good, "network": "dgbtest"}}
                    ).status_code == 400
    # a key that is not this account's DigiByte address is left out, and the rest goes
    app.post("/account/profile", json={"key": "01" * 32, "dd": {"network": "dgb", **_proof(THIEF_SECRET)}})
    assert built[-1] == {}
    app.post("/account/profile", json={"key": "01" * 32})
    assert built[-1] == {}, "nothing sent, nothing published"


def test_an_announcement_without_the_key_is_published_again(client, monkeypatch):
    from test_me_page import _seat
    app, state = client
    _dgb_here(state, monkeypatch)
    _seat(app)
    net = state.messaging.network
    home = b58check_encode(state.messaging.params.pubkeyhash_version, NAME_HASH)
    dgb = b58check_encode(30, hash160(MAIN_KEY))
    assert app.post("/account/address", json={"address": home, "chain": net}).status_code == 200
    app.post("/account/address", json={"address": dgb, "chain": "dgb", "coin_pubkey": MAIN_KEY.hex()})
    chains = json.dumps({"dgb": dgb})
    with state.store() as store:
        store.add_key_announcement("b1" * 32, home, b"\x01" * 32, "fp", 100, 1, stated=True, chain_addresses=chains)
    said = app.get("/account").json()
    assert said["announced"] is False and said["announcing"] is False, "out of date: the page publishes it again"
    with state.store() as store:
        store.add_key_announcement("b2" * 32, home, b"\x01" * 32, "fp", 101, 2, stated=True, chain_addresses=chains,
                                   dd_keys=json.dumps({"dgb": _proof(MAIN_SECRET)}))
    assert app.get("/account").json()["announced"] is True


def test_the_arcadecore_window_finishes_publishing_too(app_state):
    """The full arcade's pages republish an out-of-date announcement (base.html); somebody who only uses ArcadeCore
    gets the same check in its window, for their account only: not a stranger's window, not the node's wallet."""
    from nacl.signing import SigningKey
    from test_auth_web import _sign_in
    app_state.set_setting("public_hosts", ["arcade.example.com"])
    try:
        app = TestClient(create_app(app_state))
        assert "arcade.keycheck" not in app.get("/lite", headers=OUTSIDE).text, "a stranger"
        assert "arcade.keycheck" not in app.get("/lite", headers={"host": "127.0.0.1:8420"}).text, "the node's wallet"
        assert _sign_in(app, SigningKey.generate(), headers=OUTSIDE).status_code == 200
        page = app.get("/lite", headers=OUTSIDE).text
        check = page[page.index('LAST = "arcade.keycheck"'):]
        check = check[:check.index("</script>")]
        assert "mail.announce(keys" in check and "setInterval(" in check and "wallet.opened(TAGCHAIN)" in check
    finally:
        app_state.set_setting("public_hosts", [])

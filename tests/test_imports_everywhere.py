"""Imported wallets on every arcade an account uses (2026-10-21: "an imported wallet needs to be automatically
broadcast to other arcades that you are signed in with. So all of your wallet balances persist over all arcades that
you sign into").

What these hold still: every version of the sealed record is signed by the account's login key and numbered, and no
arcade takes one that is not signed by the pubkey of ITS OWN vault row, or that is no newer than its own; the mesh
carries new versions and catches an arcade up when a link comes up, naming accounts only by a hash; an account brought
to an arcade brings its record; another arcade tells an imported address's coins only on a proof its key signed for
that asker, this minute; /signin/{tag}/coins still lists the main addresses only.
"""
import asyncio
import contextlib
import json
import pathlib
import sys
import time

import pytest
from fastapi.testclient import TestClient
from nacl.signing import SigningKey

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_litecoin_testnet import _with_ltc_test                  # noqa: E402
from test_wallet_imports import _key, _proof, _sign, P, SOMEONE    # noqa: E402

from arcade import accounts as accountslib, utxos, wallet_imports as W   # noqa: E402
from arcade.mesh.importshare import ImportShare, StateRecords     # noqa: E402
from arcade.web import app as appmod                              # noqa: E402
from arcade.web.app import create_app                             # noqa: E402
from arcade.web.state import AppState, ChainContext                # noqa: E402

LOCAL = {"host": "127.0.0.1:8420"}
SEALED = {"kdf": "PBKDF2-SHA512", "iterations": 600000, "salt": "00" * 16, "nonce": "11" * 12, "sealed": "22" * 40}
VAULT = {"kdf": "PBKDF2-SHA512", "iterations": 600000, "salt": "aa" * 16, "nonce": "bb" * 12, "sealed": "cc" * 60}


def _text(**over) -> str:
    return json.dumps({**SEALED, **over}, separators=(",", ":"))


def _sig(key: SigningKey, version: int, text: str) -> str:
    pk = key.verify_key.encode().hex()
    return key.sign(W.record_digest(pk, version, W.blob_hash(text))).signature.hex()


def _arcade(tmp_path, name):
    (tmp_path / name).mkdir()
    st = AppState(home=tmp_path / name,
                  messaging=ChainContext(network="regtest", role="messaging", label="Testnet",
                                         datadir=pathlib.Path("/nonexistent")),
                  ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                                      datadir=pathlib.Path("/nonexistent")))
    return st, TestClient(create_app(st))


def _seat(app, key: SigningKey) -> str:
    challenge = app.get("/auth/challenge", headers=LOCAL).json()
    signature = key.sign(accountslib.login_message(challenge["origin"], challenge["nonce"])).signature
    said = app.post("/auth/login", headers=LOCAL, json={"pubkey": key.verify_key.encode().hex(),
                                                        "nonce": challenge["nonce"], "signature": signature.hex(),
                                                        "join": True})
    assert said.status_code == 200, said.text
    return key.verify_key.encode().hex()


# --- the signature ----------------------------------------------------------------------------------------------------

def test_a_version_is_taken_only_signed_by_the_account_and_newer(tmp_path, no_nodes):
    state, _ = _arcade(tmp_path, "a")
    key, other = SigningKey(b"\x01" * 32), SigningKey(b"\x02" * 32)
    pk = key.verify_key.encode().hex()
    store = state.imported_wallets()
    one, two = _text(sealed="31" * 40), _text(sealed="32" * 40)
    # The account's own browser: version 1, signed by its key.
    with pytest.raises(accountslib.AccountError):
        store.put(pk, one, 1, _sig(other, 1, one))                      # another key
    with pytest.raises(accountslib.AccountError):
        store.put(pk, one, 1, _sig(key, 2, one))                        # signed for another version
    with pytest.raises(accountslib.AccountError):
        store.put(pk, one, 1, _sig(key, 1, two))                        # signed over another blob
    with pytest.raises(accountslib.AccountError):
        store.put(pk, one, 1, "")
    assert store.put(pk, one, 1, _sig(key, 1, one))["version"] == 1
    with pytest.raises(accountslib.ImportedMoved):
        store.put(pk, two, 1, _sig(key, 1, two))                        # not one more than the version kept
    with pytest.raises(accountslib.ImportedMoved):
        store.put(pk, two, 3, _sig(key, 3, two))
    # From another arcade: newer and signed only.
    assert store.adopt(pk, two, 2, _sig(key, 2, two)) == "taken"
    assert store.get(pk)["version"] == 2 and store.get(pk)["blob"] == two
    assert store.adopt(pk, one, 1, _sig(key, 1, one)) == "older"
    assert store.adopt(pk, two, 2, _sig(key, 2, two)) == "same"
    with pytest.raises(accountslib.AccountError):
        store.adopt(pk, _text(sealed="33" * 40), 3, _sig(other, 3, _text(sealed="33" * 40)))   # wrong pubkey
    with pytest.raises(accountslib.AccountError):
        store.adopt(pk, _text(sealed="33" * 40), 3, _sig(key, 3, _text(sealed="34" * 40)))    # tampered blob
    with pytest.raises(accountslib.AccountError):
        store.adopt(pk, "not sealed", 3, _sig(key, 3, "not sealed"))
    assert store.get(pk)["version"] == 2, "nothing refused changed what is kept"


def test_a_copy_written_here_is_put_aside_never_lost(tmp_path, no_nodes):
    """Two arcades took a change each before hearing of the other's: the one written here is kept aside for the
    browser to fold in, and so is a signed copy of the same version with other contents."""
    state, _ = _arcade(tmp_path, "a")
    key = SigningKey(b"\x03" * 32)
    pk = key.verify_key.encode().hex()
    store = state.imported_wallets()
    mine, theirs, same_v = _text(sealed="41" * 40), _text(sealed="42" * 40), _text(sealed="43" * 40)
    store.put(pk, mine, 1, _sig(key, 1, mine))
    assert store.adopt(pk, theirs, 2, _sig(key, 2, theirs)) == "taken"
    assert [s["digest"] for s in store.siblings(pk)] == [W.blob_hash(mine)]
    assert store.adopt(pk, same_v, 2, _sig(key, 2, same_v)) == "aside"
    assert {s["digest"] for s in store.siblings(pk)} == {W.blob_hash(mine), W.blob_hash(same_v)}
    assert store.get(pk)["blob"] == theirs
    # The browser folds them in and says so with its key: then they go.
    folded = _text(sealed="44" * 40)
    digests = [W.blob_hash(mine), W.blob_hash(same_v)]
    with pytest.raises(accountslib.AccountError):
        store.put(pk, folded, 3, _sig(key, 3, folded), merged=digests, merged_sig="00" * 64)
    merged_sig = key.sign(W.merged_digest(pk, 3, digests)).signature.hex()
    store.put(pk, folded, 3, _sig(key, 3, folded), merged=digests, merged_sig=merged_sig)
    assert store.siblings(pk) == []
    # A version taken from another arcade replacing one that was itself taken: nothing aside.
    four, five = _text(sealed="45" * 40), _text(sealed="46" * 40)
    store.adopt(pk, four, 4, _sig(key, 4, four))
    store.adopt(pk, five, 5, _sig(key, 5, five))
    assert [s["digest"] for s in store.siblings(pk)] == [W.blob_hash(folded)], "only the one written here"


def test_copies_holding_nothing_new_go_with_no_new_version(tmp_path, no_nodes):
    """The copy this arcade wrote before another's newer version came usually holds nothing new: the browser drops it
    with its key's word over the version kept, and no version is saved -- or two arcades would answer each other's
    versions with ever newer ones at every unlock."""
    state, app = _arcade(tmp_path, "a")
    key = SigningKey(b"\x0b" * 32)
    pk = _seat(app, key)
    state.vault().put("folder", pk, SOMEONE, json.dumps(VAULT))
    store = state.imported_wallets()
    mine, theirs = _text(sealed="47" * 40), _text(sealed="48" * 40)
    store.put(pk, mine, 1, _sig(key, 1, mine))
    store.adopt(pk, theirs, 2, _sig(key, 2, theirs))
    digests = [W.blob_hash(mine)]
    bad = SigningKey(b"\x0c" * 32).sign(W.merged_digest(pk, 2, digests)).signature.hex()
    assert app.post("/account/imported/siblings/drop",
                    json={"merged": digests, "merged_signature": bad}).status_code == 400
    stale = key.sign(W.merged_digest(pk, 1, digests)).signature.hex()
    assert app.post("/account/imported/siblings/drop",
                    json={"merged": digests, "merged_signature": stale}).status_code == 400, "over another version"
    assert len(store.siblings(pk)) == 1
    good = key.sign(W.merged_digest(pk, 2, digests)).signature.hex()
    said = app.post("/account/imported/siblings/drop", json={"merged": digests, "merged_signature": good})
    assert said.status_code == 200 and said.json()["dropped"] == 1
    assert store.siblings(pk) == [] and store.get(pk)["version"] == 2 and store.get(pk)["blob"] == theirs


def test_a_record_from_before_versions_reads_as_version_0_unsigned(tmp_path, no_nodes):
    state, _ = _arcade(tmp_path, "a")
    conn = state.accounts().conn
    conn.execute("DROP TABLE IF EXISTS imported_wallets")
    conn.execute("CREATE TABLE imported_wallets (pubkey TEXT PRIMARY KEY, blob TEXT NOT NULL, updated INTEGER NOT NULL)")
    key = SigningKey(b"\x04" * 32)
    pk = key.verify_key.encode().hex()
    old = _text(sealed="51" * 40)
    conn.execute("INSERT INTO imported_wallets VALUES (?,?,?)", (pk, old, 1000))
    store = state.imported_wallets()
    have = store.get(pk)
    assert (have["version"], have["signed"], have["origin"]) == (0, False, "here")
    assert store.signed() == [], "an unsigned record is never offered to other arcades"
    again = _text(sealed="52" * 40)
    assert store.put(pk, again, 1, _sig(key, 1, again))["version"] == 1, "saved again, signed, as version 1"
    assert store.get(pk)["signed"]


def test_the_route_takes_a_signed_next_version_and_says_where_it_stands(tmp_path, no_nodes):
    state, app = _arcade(tmp_path, "a")
    key = SigningKey(b"\x05" * 32)
    pk = _seat(app, key)
    state.vault().put("roamer", pk, SOMEONE, json.dumps(VAULT))
    one = _text(sealed="61" * 40)
    assert app.post("/account/imported", json={"blob": one, "base": 0}).status_code == 400, "unsigned: refused"
    assert app.post("/account/imported", json={"blob": one, "version": 1,
                                               "signature": _sig(SigningKey(b"\x06" * 32), 1, one)}).status_code == 400
    put = app.post("/account/imported", json={"blob": one, "version": 1, "signature": _sig(key, 1, one)})
    assert put.status_code == 200, put.text
    got = app.get("/account/imported").json()
    assert (got["blob"], got["version"], got["signed"], got["tag"], got["siblings"]) == (one, 1, True, "roamer", [])
    stale = app.post("/account/imported", json={"blob": one, "version": 1, "signature": _sig(key, 1, one)})
    assert stale.status_code == 409 and stale.json()["version"] == 1
    # Public, like the sealed vault: blob, version and signature, which anybody can check against the account.
    pub = TestClient(create_app(state)).get("/signin/roamer/imported").json()
    assert (pub["blob"], pub["version"], pub["pubkey"]) == (one, 1, pk)
    W.check_record(pk, pub["version"], pub["blob"], pub["signature"])
    assert app.get("/signin/nobody/imported").status_code == 404


# --- over the mesh ----------------------------------------------------------------------------------------------------

class _Link:
    def __init__(self, peer_id):
        self.peer_id, self.far, self.share, self.tamper, self.sent = peer_id, None, None, None, []

    async def send(self, header, blob=b""):
        self.sent.append(header.get("op"))
        if self.tamper and header.get("op") == "rc":
            blob = self.tamper(blob)
        await self.share.on_frame(self.far, header, blob)


class _Node:
    def __init__(self):
        self.links = {}


def _wire(a_share, b_share):
    """Two arcades linked: what one sends on its link, the other hears on its own."""
    to_b, to_a = _Link("node-b"), _Link("node-a")
    to_b.far, to_a.far = to_a, to_b
    to_b.share, to_a.share = b_share, a_share
    a_share.node, b_share.node = _Node(), _Node()
    a_share.node.links = {"node-b": to_b}
    b_share.node.links = {"node-a": to_a}
    return to_b, to_a


def test_a_new_version_goes_to_a_linked_arcade_that_holds_the_account(tmp_path, no_nodes):
    a, _ = _arcade(tmp_path, "a")
    b, _ = _arcade(tmp_path, "b")
    c, _ = _arcade(tmp_path, "c")
    key = SigningKey(b"\x07" * 32)
    pk = key.verify_key.encode().hex()
    for st in (a, b):                                   # a and b hold the account; c does not
        st.vault().put("roamer", pk, SOMEONE, json.dumps(VAULT))
    sa, sb, sc = (ImportShare(StateRecords(st)) for st in (a, b, c))
    to_b, _ = _wire(sa, sb)
    one = _text(sealed="71" * 40)
    a.imported_wallets().put(pk, one, 1, _sig(key, 1, one))

    async def go():
        sa.loop = sb.loop = asyncio.get_running_loop()
        await sa._say_all({"op": "an", "h": W.account_hash(pk), "v": 1, "d": W.blob_hash(one)})
        # c, linked to a, never asks: it does not hold the account.
        to_c, _ = _wire(sa, sc)
        await sa._say_all({"op": "an", "h": W.account_hash(pk), "v": 1, "d": W.blob_hash(one)})
        return to_c
    to_c = asyncio.run(go())
    assert b.imported_wallets().get(pk)["blob"] == one and b.imported_wallets().get(pk)["version"] == 1
    assert sb.taken == 1
    assert "gt" not in to_c.far.sent, "c holds no such account: it never asks"
    assert c.imported_wallets().get(pk) is None
    assert pk not in json.dumps(to_b.sent), "the frames name accounts by a hash"


def test_an_arcade_that_was_offline_catches_up_when_the_link_comes_up(tmp_path, no_nodes):
    a, _ = _arcade(tmp_path, "a")
    b, _ = _arcade(tmp_path, "b")
    key, other = SigningKey(b"\x08" * 32), SigningKey(b"\x09" * 32)
    pk, pk2 = key.verify_key.encode().hex(), other.verify_key.encode().hex()
    for st in (a, b):
        st.vault().put("roamer", pk, SOMEONE, json.dumps(VAULT))
    a.vault().put("onlya", pk2, SOMEONE, json.dumps(VAULT))
    old, new = _text(sealed="81" * 40), _text(sealed="82" * 40)
    b.imported_wallets().adopt(pk, old, 1, _sig(key, 1, old))
    a.imported_wallets().adopt(pk, new, 3, _sig(key, 3, new))
    lone = _text(sealed="83" * 40)
    a.imported_wallets().adopt(pk2, lone, 1, _sig(other, 1, lone))
    sa, sb = ImportShare(StateRecords(a)), ImportShare(StateRecords(b))
    to_b, to_a = _wire(sa, sb)

    async def go():
        await sa.link_up(to_b)
        await sb.link_up(to_a)
    asyncio.run(go())
    assert b.imported_wallets().get(pk)["version"] == 3 and b.imported_wallets().get(pk)["blob"] == new
    assert a.imported_wallets().get(pk)["version"] == 3, "and never the older one back"
    assert b.imported_wallets().get(pk2) is None, "an account b does not hold is not taken"
    # What a listed: hashes, never pubkeys.
    listed = StateRecords(a).listed()
    assert sorted(listed) == sorted([(W.account_hash(pk), 3), (W.account_hash(pk2), 1)])


def test_a_tampered_or_forged_record_is_refused(tmp_path, no_nodes):
    a, _ = _arcade(tmp_path, "a")
    b, _ = _arcade(tmp_path, "b")
    key, forger = SigningKey(b"\x0a" * 32), SigningKey(b"\x0b" * 32)
    pk = key.verify_key.encode().hex()
    for st in (a, b):
        st.vault().put("roamer", pk, SOMEONE, json.dumps(VAULT))
    good = _text(sealed="91" * 40)
    b.imported_wallets().adopt(pk, good, 1, _sig(key, 1, good))
    sa, sb = ImportShare(StateRecords(a)), ImportShare(StateRecords(b))
    to_b, to_a = _wire(sa, sb)
    # a's copy is signed by another key (a's vault, say, was given the wrong pubkey): b checks with ITS vault's key.
    bad = _text(sealed="92" * 40)
    a.imported_wallets().conn.execute(
        "INSERT INTO imported_wallets (pubkey, blob, updated, version, sig, origin) VALUES (?,?,?,?,?,'peer')",
        (pk, bad, 1, 5, _sig(forger, 5, bad)))

    async def go():
        await sa.link_up(to_b)
    asyncio.run(go())
    assert b.imported_wallets().get(pk)["blob"] == good and sb.refused >= 1
    # A real version, changed on the way: its hash no longer matches, never taken.
    real = _text(sealed="93" * 40)
    a.imported_wallets().conn.execute("UPDATE imported_wallets SET blob = ?, version = 6, sig = ? WHERE pubkey = ?",
                                      (real, _sig(key, 6, real), pk))
    to_b.tamper = lambda blob: blob.replace(b"93", b"94", 1)     # on its way from a to b

    async def again():
        await sa.link_up(to_b)
    asyncio.run(again())
    assert b.imported_wallets().get(pk)["blob"] == good
    to_b.tamper = None
    asyncio.run(again())
    assert b.imported_wallets().get(pk)["blob"] == real, "untampered, it is taken"


def test_a_linked_arcade_that_says_too_much_is_not_read(tmp_path, no_nodes):
    from arcade.mesh import importshare
    b, _ = _arcade(tmp_path, "b")
    sb = ImportShare(StateRecords(b))
    asked = []

    class Link:
        peer_id = "noisy"

        async def send(self, header, blob=b""):
            asked.append(header)
    rows = [[f"{i:032x}", 1] for i in range(importshare.ROWS_PER_FRAME)]

    async def go():
        for _ in range(importshare.HEARD_PER_MINUTE // importshare.ROWS_PER_FRAME + 2):
            await sb.on_frame(Link(), {"t": "iw", "op": "ls", "rows": rows}, b"")
    asyncio.run(go())
    assert sb._heard["noisy"] and len(sb._heard["noisy"]) <= importshare.HEARD_PER_MINUTE
    assert asked == [], "none of them held here"


# --- bringing an account ----------------------------------------------------------------------------------------------

def _known(state, host):
    with state.store() as store:
        store.add_mesh_announcement(f"t-{host}", state.messaging.network, "nX", f"wss://{host}/mesh", 443,
                                    "cd" * 32, 0, 0)


def test_an_account_brought_here_brings_its_signed_record(tmp_path, no_nodes, monkeypatch):
    home, home_app = _arcade(tmp_path, "home")
    here, here_app = _arcade(tmp_path, "here")
    key = SigningKey(b"\x0c" * 32)
    pk = key.verify_key.encode().hex()
    home.vault().put("roamer", pk, SOMEONE, json.dumps(VAULT))
    rec = _text(sealed="a1" * 40)
    home.imported_wallets().adopt(pk, rec, 4, _sig(key, 4, rec))
    _known(here, "home.example.com")
    monkeypatch.setattr(appmod, "_host_is_public", lambda host: True)

    class Answer:
        def __init__(self, r):
            self.status_code, self._r = r.status_code, r

        def json(self):
            return self._r.json()
    served = {"answer": None}

    def get(url, timeout=0, headers=None, **kwargs):
        assert kwargs.get("allow_redirects") is False
        assert url == "https://home.example.com/signin/roamer/imported"
        got = home_app.get(url.replace("https://home.example.com", ""))
        return served["answer"](got) if served["answer"] else Answer(got)
    monkeypatch.setattr(appmod.requests, "get", get)
    _seat(here_app, key)
    here.vault().put("roamer", pk, SOMEONE, json.dumps(VAULT))
    # A source this arcade does not know is never asked.
    said = here_app.post("/account/imported/bring", json={"from": "https://elsewhere.example.com"}).json()
    assert said["asked"] is False
    # A forged answer (signed by another key) is refused.

    class Forged(Answer):
        def json(self):
            body = dict(self._r.json())
            body["signature"] = _sig(SigningKey(b"\x0d" * 32), body["version"], body["blob"])
            return body
    served["answer"] = Forged
    said = here_app.post("/account/imported/bring", json={"from": "https://home.example.com"}).json()
    assert said["outcome"] == "refused" and here.imported_wallets().get(pk) is None
    served["answer"] = None
    appmod_wait = here_app.post("/account/imported/bring", json={"from": "https://home.example.com"}).json()
    assert appmod_wait["asked"] is False, "one try a minute"
    monkeypatch.setattr(time, "time", lambda real=time.time: real() + 120)
    said = here_app.post("/account/imported/bring", json={"from": "https://home.example.com"}).json()
    assert said["outcome"] == "taken" and said["version"] == 4
    assert here.imported_wallets().get(pk)["blob"] == rec


# --- imported coins, privately ----------------------------------------------------------------------------------------

def _two_ltc(tmp_path, monkeypatch):
    home, home_app = _arcade(tmp_path, "home")
    here, here_app = _arcade(tmp_path, "here")
    _with_ltc_test(home, monkeypatch, ready=True)
    _with_ltc_test(here, monkeypatch, ready=True)
    return home, home_app, here, here_app


def _coins_proof(secret, tag, address, asker, minute=None, network="ltctest"):
    priv, pub = _key(secret)
    minute = int(time.time() // 60) if minute is None else minute
    return {"network": network, "address": address, "kind": "p2pkh", "pubkey": pub.hex(), "minute": minute,
            "asker": asker, "signature": _sign(priv, W.coins_digest(tag, network, address, asker, minute)).hex()}


def test_an_imported_address_s_coins_are_told_only_on_its_key_s_proof(tmp_path, no_nodes, monkeypatch):
    home, home_app, _, _ = _two_ltc(tmp_path, monkeypatch)
    key = SigningKey(b"\x0e" * 32)
    pk = _seat(home_app, key)
    home.vault().put("roamer", pk, SOMEONE, json.dumps(VAULT))
    asked = _proof(pk, 0x4141)
    assert home_app.post("/account/imported/address", json=asked).status_code == 200
    address = asked["address"]
    chain = home.ltc_test
    with contextlib.closing(home.token_index(chain).open()) as db:
        db.conn.execute("INSERT INTO utxo (txid, vout, address, value, height) VALUES (?,?,?,?,?)",
                        ("aa" * 32, 0, address, 5 * 100_000_000, 20))
        db.conn.commit()
    _known(home, "here.example.com")
    stranger = TestClient(create_app(home))
    url = "/signin/roamer/imported-coins"
    good = _coins_proof(0x4141, "roamer", address, "here.example.com")
    said = stranger.post(url, json=good)
    assert said.status_code == 200, said.text
    assert [c["txid"] for c in said.json()["coins"]] == ["aa" * 32]
    # A stale minute, another key, an asker this arcade does not know, another account's tag, an address that is
    # not that account's import: nothing told.
    stale = _coins_proof(0x4141, "roamer", address, "here.example.com", minute=int(time.time() // 60) - 9)
    assert stranger.post(url, json=stale).status_code == 403
    wrong = {**good, "signature": _coins_proof(0x4242, "roamer", address, "here.example.com")["signature"]}
    assert stranger.post(url, json=wrong).status_code == 403
    assert stranger.post(url, json=_coins_proof(0x4141, "roamer", address, "nobody.example.com")).status_code == 403
    assert stranger.post(url, json={**good, "asker": "nobody.example.com"}).status_code == 403
    home.vault().put("other", "ef" * 32, SOMEONE, json.dumps(VAULT))
    assert stranger.post("/signin/other/imported-coins",
                         json=_coins_proof(0x4141, "other", address, "here.example.com")).status_code == 404
    priv, pub = _key(0x4343)
    not_imported = W.address_of(P, pub, "p2pkh")
    assert stranger.post(url, json=_coins_proof(0x4343, "roamer", not_imported, "here.example.com")).status_code == 404
    # The by-tag coins answer still lists the main addresses only.
    listed = stranger.get("/signin/roamer/coins").json()["chains"]
    assert address not in json.dumps(listed)
    # A door a stranger may knock on, by shape only.
    from arcade.web import door
    assert door.public_path(url, "POST") and not door.public_path("/signin/roamer/other", "POST")


def test_a_wallet_in_the_record_is_registered_on_another_arcade_with_its_coins(tmp_path, no_nodes, monkeypatch):
    """The browser on `here` registers a wallet the record names (sync), and `here` asks `home` for its coins with
    the proof the key signed for `here`; each is checked against here's own chain before it is filed."""
    home, home_app, here, here_app = _two_ltc(tmp_path, monkeypatch)
    key = SigningKey(b"\x0f" * 32)
    pk = _seat(home_app, key)
    home.vault().put("roamer", pk, SOMEONE, json.dumps(VAULT))
    asked = _proof(pk, 0x5151)
    address = asked["address"]
    assert home_app.post("/account/imported/address", json=asked).status_code == 200
    with contextlib.closing(home.token_index(home.ltc_test).open()) as db:
        for txid in ("aa" * 32, "bb" * 32):
            db.conn.execute("INSERT INTO utxo (txid, vout, address, value, height) VALUES (?,?,?,?,?)",
                            (txid, 0, address, 5 * 100_000_000, 20))
        db.conn.commit()
    _known(home, "here.example.com")
    _known(here, "home.example.com")
    here.set_setting("public_hosts", ["here.example.com"])
    _seat(here_app, key)
    here.vault().put("roamer", pk, SOMEONE, json.dumps(VAULT))
    rec = _text(sealed="b1" * 40)
    monkeypatch.setattr(appmod, "_host_is_public", lambda host: True)

    class Answer:
        def __init__(self, r):
            self.status_code, self._r = r.status_code, r

        def json(self):
            return self._r.json()
    posted = []

    def post(url, json=None, timeout=0, headers=None, **kwargs):
        assert kwargs.get("allow_redirects") is False
        posted.append(url)
        return Answer(TestClient(create_app(home)).post(url.replace("https://home.example.com", ""), json=json))
    monkeypatch.setattr(appmod.requests, "post", post)

    class Rpc:                                  # here's own chain: aa is real and unspent, bb is spent
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def call(self, method, *args):
            if method == "gettxout":
                return {"confirmations": 3, "value": 5, "scriptPubKey": {"addresses": [address]}} \
                    if args[0] == "aa" * 32 else None
            return None

        def get_block_count(self):
            return 100
    monkeypatch.setattr(type(here.ltc_test), "rpc", lambda self: Rpc())
    sync = {**asked, "sync": True,
            "coins": {k: v for k, v in _coins_proof(0x5151, "roamer", address, "here.example.com").items()
                      if k in ("minute", "signature", "asker")}}
    refused = here_app.post("/account/imported/address", json=sync)
    assert refused.status_code == 400, "a sync needs a signed record here first"
    here.imported_wallets().adopt(pk, rec, 1, _sig(key, 1, rec))
    view = here_app.get("/account/imported").json()
    assert view["host"] == "here.example.com" and view["tag"] == "roamer"
    said = here_app.post("/account/imported/address", json=sync)
    assert said.status_code == 200, said.text
    assert said.json()["state"] == "ready" and said.json()["from"] == "home.example.com"
    assert posted == ["https://home.example.com/signin/roamer/imported-coins"]
    with contextlib.closing(here.token_index(here.ltc_test).open()) as db:
        assert [c["txid"] for c in utxos.unspent(db, address)] == ["aa" * 32], "only what here's chain says"
    wallets = here_app.get("/account/imported").json()["chains"]["ltctest"]
    assert [w["address"] for w in wallets] == [address] and wallets[0]["spendable"] == 5 * 100_000_000


def test_re_registering_the_record_s_wallets_has_an_allowance_of_its_own(tmp_path, no_nodes, monkeypatch):
    state, app = _arcade(tmp_path, "a")
    _with_ltc_test(state, monkeypatch, ready=True)
    key = SigningKey(b"\x10" * 32)
    pk = _seat(app, key)
    state.vault().put("roamer", pk, SOMEONE, json.dumps(VAULT))
    rec = _text(sealed="c1" * 40)
    state.imported_wallets().put(pk, rec, 1, _sig(key, 1, rec))
    state.set_setting(f"import_day:{pk}", [int(time.time())] * W.PER_DAY)          # the day's imports used up
    assert app.post("/account/imported/address", json=_proof(pk, 0x6161)).status_code == 429
    said = app.post("/account/imported/address", json={**_proof(pk, 0x6161), "sync": True})
    assert said.status_code == 200, said.text
    state.set_setting(f"import_sync_day:{pk}", [int(time.time())] * W.PER_DAY)     # and that one too: bounded
    assert app.post("/account/imported/address", json={**_proof(pk, 0x6262), "sync": True}).status_code == 429


def test_the_new_routes_are_through_the_door():
    from arcade.web import door
    assert "/account/imported/bring" in door.PUBLIC_POST
    assert door.public_path("/signin/roamer/imported")

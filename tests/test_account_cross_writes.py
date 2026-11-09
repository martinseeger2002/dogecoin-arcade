"""No account writes another's (2026-11-09, security review): every POST, PUT, PATCH and DELETE route under /account,
asked by account A with account B's key, address and ids in each field the route reads, changes nothing of B's.

The routes are enumerated from the app, and the fields each one reads from its body (`said.get("...")`) from its own
source, so a route or a field added later is swept too. Each field is tried with each of B's identifiers: B's sign-in
key, B's address and coin key, B's API key id and B's LLM key hash (every one of them is B's own thing in the home
before the sweep starts). A asks from the node's own machine and from outside.

What fails: anything of B's that changes, goes or appears -- a setting kept under B's sign-in key or address, or a
row of any database in the arcade's home naming B's key or address and not A's. A route may refuse (4xx), or answer
about A's own things: a row of A's that names B's address or ids (A declines B's bid, lets B ring) is A's to keep.
Those, and every route that answered 2xx while handed B's identifiers, are printed for review (pytest -s).

The sweep found /account/address and /account/segwit/register taking B's address as A's (2026-11-09); the two tests
at the end hold their fix.
"""
from __future__ import annotations

import inspect
import json
import pathlib
import re
import sqlite3
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_funding import _pubkey                                 # noqa: E402
from test_web import app_state                                   # noqa: F401,E402

from arcade.config import NETWORKS                               # noqa: E402
from arcade.script import b58check_encode, hash160               # noqa: E402

WRITES = {"POST", "PUT", "PATCH", "DELETE"}
LOCAL = {"host": "127.0.0.1:8420"}
OUTSIDE = {"host": "arcade.example.com", "cf-connecting-ip": "198.51.100.9", "origin": "https://arcade.example.com"}
#: What a route reads from its body, by name.
FIELD = re.compile(r"\b(?:said|payload|body|data|asked)\.get\(\s*[\"']([A-Za-z_]\w*)[\"']"
                   r"|\b(?:said|payload|body|data|asked)\[\s*[\"']([A-Za-z_]\w*)[\"']\s*\]")


def _sign_in(client, headers) -> str:
    from nacl.signing import SigningKey

    from arcade import accounts
    key = SigningKey.generate()
    challenge = client.get("/auth/challenge", headers=headers).json()
    signature = key.sign(accounts.login_message(challenge["origin"], challenge["nonce"])).signature
    answer = client.post("/auth/login", headers=headers, json={
        "pubkey": key.verify_key.encode().hex(), "nonce": challenge["nonce"], "signature": signature.hex(),
        "join": True})
    assert answer.status_code == 200, answer.text
    return key.verify_key.encode().hex()


def _of(home: pathlib.Path, ids: list[str]) -> set:
    """Every setting, every row of the home's databases and every other JSON file there that names one of `ids`."""
    ids = [i.lower() for i in ids if i]
    out = set()

    def names(text: str) -> bool:
        text = text.lower()
        return any(i in text for i in ids)

    settings = home / "settings.json"
    if settings.exists():
        for key, value in json.loads(settings.read_text() or "{}").items():
            text = json.dumps([key, value], sort_keys=True)
            if names(text):
                out.add(("settings.json", key, text))
    for path in sorted(home.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix in (".sqlite", ".db", ".sqlite3"):
            try:
                conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
                for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                    for row in conn.execute(f'SELECT * FROM "{table}"'):
                        text = repr([c.hex() if isinstance(c, (bytes, memoryview)) else c for c in row])
                        if names(text):
                            out.add((path.name, table, text))
                conn.close()
            except sqlite3.DatabaseError:
                continue
        elif path.suffix == ".json" and path.name != "settings.json":
            text = path.read_text(errors="replace")
            if names(text):
                out.add((path.name, "", text))
    return out


def _bs(row: tuple, b_marks: tuple, a_marks: tuple) -> bool:
    """Whether a row is B's: a setting kept under B's key or address, or a database row naming B's key or address
    and not A's. A's own rows may name B (a declined bid of B's, an allow-list); a row naming both is not B's alone."""
    where, key, text = row
    owner = (key if where == "settings.json" else text).lower()
    return any(m.lower() in owner for m in b_marks) and not any(m.lower() in owner for m in a_marks)


def _routes(app) -> list[tuple[str, str, list[str]]]:
    """(method, path, the body fields the route's own code reads) for every write under /account."""
    from fastapi.routing import APIRoute
    found = {}
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith("/account"):
            try:
                source = inspect.getsource(route.endpoint)
            except (OSError, TypeError):
                source = ""
            fields = sorted({a or b for a, b in FIELD.findall(source)})
            for method in route.methods & WRITES:
                found[(method, route.path)] = fields
    return [(m, p, f) for (m, p), f in sorted(found.items())]


@pytest.mark.parametrize("where", ["here", "outside"])
def test_no_account_route_writes_another_accounts_things(app_state, where):
    from fastapi.testclient import TestClient

    from arcade.web.app import create_app
    state = app_state
    state.set_setting("public_hosts", ["arcade.example.com"])
    app = create_app(state)
    b = TestClient(app, raise_server_exceptions=False)
    a = TestClient(app, raise_server_exceptions=False)
    asks = LOCAL if where == "here" else OUTSIDE
    b_pub = _sign_in(b, LOCAL)
    a_pub = _sign_in(a, asks)

    # B's own things, each kept under B's sign-in key.
    test = NETWORKS["test"]
    b_addr = b58check_encode(test.pubkeyhash_version, hash160(_pubkey(int("b0" * 32, 16))))
    b_coin = _pubkey(int("b1" * 32, 16)).hex()
    assert b.post("/account/address", headers=LOCAL, json={"address": b_addr, "coin_pubkey": b_coin}).status_code == 200
    a_addr = b58check_encode(test.pubkeyhash_version, hash160(_pubkey(int("a0" * 32, 16))))
    assert a.post("/account/address", headers=asks, json={"address": a_addr}).status_code == 200
    made = b.post("/account/keys", headers=LOCAL, json={"name": "b's key"})
    b_kid = str(made.json().get("kid") or made.json().get("id") or "") if made.status_code == 200 else ""
    b_llm = ""
    if b.post("/account/llm/keys/new", headers=LOCAL, json={"name": "b's llm key"}).status_code == 200:
        listed = b.get("/account/llm/keys", headers=LOCAL).json().get("keys") or []
        b_llm = str((listed[0] if listed else {}).get("hash") or "")
    for path, body in (("/account/deskbg", {"bg": "teal"}), ("/account/homebg", {"bg": "teal"}),
                       ("/account/favorites", {"favorites": ["arcade"]}), ("/account/homeapps", {"apps": ["arcade"]})):
        b.post(path, headers=LOCAL, json=body)
    ids = [x for x in (b_pub, b_addr, b_coin, b_kid, b_llm) if x]
    b_marks, a_marks = (b_pub, b_addr), (a_pub, a_addr)
    values = list(ids) + [[x] for x in ids]

    routes = _routes(app)
    assert len(routes) > 100, "the sweep found the account routes"
    wrote, accepted, mentions, broke = [], {}, [], []
    for method, path, fields in routes:
        url = path.replace("{kid}", b_kid or "0")
        before = _of(state.home, ids)
        bodies = [{}, {f: b_pub for f in fields}, {f: b_addr for f in fields}]
        bodies += [{f: v, "chain": "test"} for f in fields for v in values]
        for body in bodies:
            r = a.request(method, url, headers=asks, json=body)
            if r.status_code >= 500:
                broke.append(f"{method} {path} {r.status_code}")
            elif r.status_code < 400 and body:
                accepted.setdefault(f"{method} {path}", set()).update(set(body) - {"chain"})
        after = _of(state.home, ids)
        changed = [row for row in (before ^ after) if _bs(row, b_marks, a_marks)]
        if changed:
            wrote.append((method, path, sorted(changed)[:3]))
        mentions += [(method, path, row[0], row[1], row[2][:160]) for row in sorted(after - before)
                     if not _bs(row, b_marks, a_marks)]
    print(f"\n[{where}] A={a_pub[:12]} B={b_pub[:12]}; {len(routes)} routes")
    print("2xx with B's identifiers:")
    for route, said in sorted(accepted.items()):
        print(f"  {route}: {sorted(said)}")
    print("A's new rows naming B's address or ids:")
    for one in mentions:
        print("  ", one)
    print("5xx:", sorted(set(broke)))
    assert not wrote, f"routes that changed account B's things when asked by account A: {wrote}"


def test_an_account_cannot_file_another_accounts_address_as_its_own(app_state):
    """/account/address takes an address on the browser's word. Account A filing account B's address used to be
    taken, and then what this node keeps by address was A's on B's address (the cross-account sweep found A's mintpad
    "announced" list written under B's address, 2026-11-09). Now it is refused, on every chain, and B's address stays
    B's."""
    from fastapi.testclient import TestClient

    from arcade.web.app import create_app
    app = create_app(app_state)
    a, b = TestClient(app), TestClient(app)
    a_pub, b_pub = _sign_in(a, LOCAL), _sign_in(b, LOCAL)
    test = NETWORKS["test"]
    b_addr = b58check_encode(test.pubkeyhash_version, hash160(_pubkey(int("b0" * 32, 16))))
    a_addr = b58check_encode(test.pubkeyhash_version, hash160(_pubkey(int("a0" * 32, 16))))
    assert b.post("/account/address", json={"address": b_addr}).status_code == 200
    assert a.post("/account/address", json={"address": a_addr}).status_code == 200
    r = a.post("/account/address", json={"address": b_addr})
    assert r.status_code == 409 and "another account" in r.json()["detail"], r.text
    assert app_state.setting(f"address:{a_pub}") == a_addr and app_state.setting(f"address:{b_pub}") == b_addr
    assert b.post("/account/address", json={"address": b_addr}).status_code == 200, "its own, again: still fine"


def test_an_account_cannot_register_another_accounts_segwit_address(app_state, monkeypatch):
    """The same for /account/segwit/register (native and nested), on Litecoin testnet: an address another account
    registered is refused, and its own registration stands."""
    from fastapi.testclient import TestClient

    from arcade import script
    from arcade.web.app import create_app
    from test_litecoin_testnet import _with_ltc_test
    _with_ltc_test(app_state, monkeypatch, ready=True)
    app = create_app(app_state)
    a, b = TestClient(app), TestClient(app)
    _sign_in(a, LOCAL)
    b_pub = _sign_in(b, LOCAL)
    p = NETWORKS["ltctest"]
    native = script.p2wpkh_address(p, hash160(_pubkey(int("84" * 32, 16))))
    nested_key = _pubkey(int("49" * 32, 16))
    nested = script.p2sh_p2wpkh_address(p, hash160(nested_key))
    assert b.post("/account/segwit/register", json={"chain": "ltctest", "address": native}).status_code == 200
    assert b.post("/account/segwit/register", json={"chain": "ltctest", "address": nested,
                                                    "pubkey": nested_key.hex()}).status_code == 200
    r = a.post("/account/segwit/register", json={"chain": "ltctest", "address": native})
    assert r.status_code == 409 and "another account" in r.json()["detail"], r.text
    r = a.post("/account/segwit/register", json={"chain": "ltctest", "address": nested, "pubkey": nested_key.hex()})
    assert r.status_code == 409, r.text
    assert app_state.setting(f"segwit_watch:{b_pub}:ltctest") == native

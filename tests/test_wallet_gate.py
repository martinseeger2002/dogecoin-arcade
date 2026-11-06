"""A bearer operator key is not the operator at the machine.

POST /admin/api/send/confirm already asks for the admin password when the caller is not local, and an
operator key is not local. The routes that print keys, import keys, or sign or broadcast a spend from the
node wallet now use that same check. A person at the machine does not, and a read with the key still does.
"""
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from nacl.signing import SigningKey

sys.path.insert(0, str(Path(__file__).parent))
from test_web import app_state  # noqa: F401,E402
from test_auth_web import _sign_in  # noqa: E402

from arcade.web.app import create_app  # noqa: E402

PASSWORD = "correct horse battery"
ADDRESS = "PognhfhGxiSNPrYLQYUaT5bMsVbgumzc6i"
OUTSIDE = {"host": "arcade.example.com", "cf-connecting-ip": "198.51.100.9"}
WRITE = {"x-arcade-admin": "1"}
ADMIN = {"host": "127.0.0.1:8420", "x-arcade-admin": "1"}


class WalletRpc:
    """Stands in for a chain's Core. Records that a route reached it, and never talks to a node."""

    def __init__(self):
        self.opened = False
        self.calls: list[str] = []

    def reset(self) -> None:
        for thread in threading.enumerate():
            if thread.name == "arcade-importprivkey":
                thread.join(2)
        self.opened = False
        self.calls.clear()

    def __enter__(self):
        self.opened = True
        return self

    def __exit__(self, *args):
        return False

    def call(self, method, *args, **kwargs):
        self.opened = True
        self.calls.append(str(method))
        # A restore stops the node and then waits until getblockcount answers. Answering that one
        # lets the route finish; every key or spend call still fails before a real node is touched.
        if method == "getblockcount":
            return 1
        raise RuntimeError(f"wallet rpc {method}")


def _wait(rpc: WalletRpc) -> bool:
    deadline = time.time() + 2
    while time.time() < deadline:
        if rpc.opened:
            return True
        time.sleep(0.02)
    return rpc.opened


# kind: form posts a form, json posts JSON, console is /api/v1/run for a key and /lite/api/console at the machine.
# rpc: the handler reaches the node wallet when the gate lets it through.
# write: the route already requires the admin-page header, which a local caller sends and a key does not.
ROUTES = [
    {"name": "backup print", "kind": "form", "path": "/backup/messaging/print",
     "body": {"understand": "yes"}, "rpc": True},
    {"name": "backup import-key", "kind": "form", "path": "/backup/messaging/import-key",
     "body": {"key": "cNotARealKey"}, "rpc": True, "thread": True},
    {"name": "backup save", "kind": "form", "path": "/backup/messaging/save",
     "body": {}, "rpc": True},
    {"name": "backup restore", "kind": "form", "path": "/backup/messaging/restore",
     "body": {}, "rpc": True},  # source filled in once the temp wallet file exists
    {"name": "wallet send", "kind": "form", "path": "/wallet/send",
     "body": {"which": "messaging", "destination": ADDRESS, "amount": "1", "confirmed": "yes"}, "rpc": True},
    {"name": "wallet split", "kind": "form", "path": "/wallet/split", "body": {}, "rpc": True},
    {"name": "tokens send", "kind": "form", "path": "/tokens/send",
     "body": {"property_id": "1", "amount": "1", "recipient": ADDRESS, "confirmed": "yes"}, "rpc": True},
    {"name": "node wallet send", "kind": "json", "path": "/node/wallet/send",
     "body": {"to": ADDRESS, "amount": "1", "chain": "regtest"}, "rpc": True, "write": True},
    {"name": "node wallet token send", "kind": "json", "path": "/node/wallet/token/send",
     "body": {"to": ADDRESS, "amount": "1", "chain": "regtest", "property_id": 1}, "rpc": False, "write": True},
    {"name": "node wallet nft send", "kind": "json", "path": "/node/wallet/nft/send",
     "body": {"to": ADDRESS, "piece": "ab" * 32, "chain": "regtest"}, "rpc": False, "write": True},
    {"name": "admin prepare", "kind": "json", "path": "/admin/api/send",
     "body": {"which": "messaging", "to": ADDRESS, "amount": "1"}, "rpc": True, "write": True},
    {"name": "console send", "kind": "console", "line": f"send messaging {ADDRESS} 1", "rpc": True},
]


@pytest.fixture
def gated(app_state, monkeypatch, tmp_path):
    app_state.set_setting("public_hosts", ["arcade.example.com"])
    data = tmp_path / "node"
    (data / "regtest").mkdir(parents=True)
    app_state.messaging.datadir = data
    source = tmp_path / "incoming.dat"
    source.write_bytes(b"\x00" * 64)
    app = TestClient(create_app(app_state))
    signer = SigningKey.generate()
    assert _sign_in(app, signer).status_code == 200
    pubkey = signer.verify_key.encode().hex()
    assert app.post("/admin/api/operator", headers=ADMIN, json={"pubkey": pubkey}).status_code == 200
    assert app.post("/admin/api/password", headers=ADMIN, json={"password": PASSWORD}).status_code == 200
    made = app.post("/admin/api/keys", headers=ADMIN, json={"name": "remote"})
    assert made.status_code == 200, made.text
    key = made.json()["key"]
    rpc = WalletRpc()

    def factory():
        rpc.opened = True
        return rpc

    monkeypatch.setattr(app_state, "chain_ready", lambda chain: True)
    for chain in (app_state.messaging, app_state.ledger, *(getattr(app_state, "addons", None) or ())):
        if chain is not None:
            monkeypatch.setattr(chain, "rpc", factory)
    yield app, app_state, key, rpc, source
    app_state.set_setting("public_hosts", [])


def _hit(app, route, *, headers, password, csrf, local, source):
    if route["kind"] == "form":
        data = dict(route["body"], csrf_token=csrf)
        if route["name"] == "backup restore":
            data["source"] = str(source)
            data["understand"] = "yes"
        if password is not None:
            data["password"] = password
        extra = WRITE if local and route.get("write") else {}
        return app.post(route["path"], data=data, headers={**headers, **extra}, follow_redirects=False)
    if route["kind"] == "json":
        body = dict(route["body"])
        if password is not None:
            body["password"] = password
        extra = WRITE if local and route.get("write") else {}
        return app.post(route["path"], json=body, headers={**headers, **extra})
    body = {"line": route["line"]}
    if password is not None:
        body["password"] = password
    if local:
        return app.post("/lite/api/console", json=body,
                        headers={"x-arcade-lite": "1", "x-arcade-admin": "1"})
    return app.post("/api/v1/run", json=body, headers=headers)


def _refused(response, route) -> None:
    if route["kind"] == "console":
        body = response.json()
        assert response.status_code == 200, response.text
        assert body["ok"] is False
        assert "admin password" in body["error"]
    else:
        assert response.status_code == 403, response.text
        assert "admin password" in response.json()["detail"]


def _allowed(response, route, rpc: WalletRpc) -> None:
    if route["kind"] == "console":
        body = response.json()
        assert "admin password" not in str(body.get("error") or ""), body
    else:
        assert response.status_code != 403, response.text[:400]
    if route["rpc"]:
        if route.get("thread"):
            assert _wait(rpc), "the import never asked the wallet"
        assert rpc.opened, response.text[:400]


@pytest.mark.parametrize("route", ROUTES, ids=[r["name"] for r in ROUTES])
def test_a_bearer_key_cannot_spend_or_read_keys_without_the_admin_password(gated, route):
    app, state, key, rpc, source = gated
    bearer = {**OUTSIDE, "authorization": f"Bearer {key}"}

    # (c) a local operator, no key, no password: the route runs as it did.
    rpc.reset()
    local = _hit(app, route, headers={}, password=None, csrf=state.csrf_token, local=True, source=source)
    _allowed(local, route, rpc)

    # (b) the key plus the right admin password: past the gate, and the wallet is asked when this route asks it.
    rpc.reset()
    opened = _hit(app, route, headers=bearer, password=PASSWORD, csrf=state.csrf_token, local=False, source=source)
    _allowed(opened, route, rpc)

    # (a) the key from outside, no password: refused, and the wallet RPC is not called.
    rpc.reset()
    bare = _hit(app, route, headers=bearer, password=None, csrf=state.csrf_token, local=False, source=source)
    _refused(bare, route)
    assert rpc.opened is False and rpc.calls == [], rpc.calls

    # (d) a wrong password: refused the same way, still without a wallet RPC.
    rpc.reset()
    wrong = _hit(app, route, headers=bearer, password="wrong wrong wrong", csrf=state.csrf_token,
                 local=False, source=source)
    _refused(wrong, route)
    assert rpc.opened is False and rpc.calls == [], rpc.calls


def test_a_key_still_reads_and_confirm_stays_at_the_machine(gated):
    """Read-only use of the key is unchanged, and console confirm is not opened up by the password."""
    app, state, key, rpc, source = gated
    bearer = {**OUTSIDE, "authorization": f"Bearer {key}"}
    assert app.get("/api/v1/whoami", headers=bearer).json()["as"] == "operator"
    assert app.get("/admin/api/modules", headers=bearer).status_code == 200
    rpc.reset()
    listed = app.post("/api/v1/run", headers=bearer, json={"line": "listunspent messaging"})
    body = listed.json()
    assert "admin password" not in str(body.get("error") or "")
    # listunspent is a read of coins, so it may ask the node; it must not be the spend gate.
    confirmed = app.post("/api/v1/run", headers=bearer,
                         json={"line": "confirm nosuch", "password": PASSWORD}).json()
    assert confirmed["ok"] is False and "machine" in confirmed["error"]
    assert "sendrawtransaction" not in rpc.calls

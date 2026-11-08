"""The Wallet's DigiDollar send waits for the operator's mainnet switch, as the module's own sends and mints do
(2026-11-08, security review: /account/digidollar/send sent real DigiDollars with `ddmainnet` off)."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_account_claim import _sign_in                          # noqa: E402
from test_bells_luckycoin import _with                           # noqa: E402
import signup_proof as SP                                         # noqa: E402

from arcade import modhost                                        # noqa: E402
from arcade.config import NETWORKS                               # noqa: E402
from arcade.script import b58check_encode, hash160               # noqa: E402


def _ask(app, state, net):
    priv, pub = SP.coin_key()
    return pub, lambda: app.post("/account/digidollar/send", json={
        "chain": net, "pubkey": pub.hex(), "amount": "5.00", "to": "DD" + "1" * 30})


def test_a_mainnet_wallet_send_waits_for_ddmainnet(client, monkeypatch):
    app, state = client
    _with(state, monkeypatch, "dgb", "dgbtest")
    pubkey = _sign_in(app)
    asks = {}
    for net in ("dgb", "dgbtest"):
        pub, ask = _ask(app, state, net)
        state.set_setting(f"address:{net}:{pubkey}", b58check_encode(NETWORKS[net].pubkeyhash_version, hash160(pub)))
        asks[net] = ask
    off = asks["dgb"]()
    assert off.status_code == 400 and "mainnet DigiDollar sends are off" in off.json()["detail"], off.text
    test = asks["dgbtest"]()
    assert "sends are off" not in test.text, "the test chain is open"
    switch = state.home / modhost.DATA / "digidollar" / "mainnet.json"
    switch.parent.mkdir(parents=True, exist_ok=True)
    switch.write_text(json.dumps({"on": True}))                       # ddmainnet on yes
    on = asks["dgb"]()
    assert "sends are off" not in on.text, on.text
    switch.write_text(json.dumps({"on": False}))
    assert "sends are off" in asks["dgb"]().text

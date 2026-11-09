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
from arcade.lite import commands as cmdlib                        # noqa: E402

PILOT = pathlib.Path(__file__).resolve().parent.parent / "examples" / "packages" / "digidollar"
from arcade.config import NETWORKS                               # noqa: E402
from arcade.script import b58check_encode, hash160               # noqa: E402


def _module_on(state):
    """The Wallet's send is the DigiDollar module's `ddsend` (since #116), so the module is on here."""
    op = cmdlib.Caller(state, "operator", local=True)
    cmdlib.run_line(op, f"installmodule {PILOT}")
    cmdlib.run_line(op, "install digidollar")


def _ask(app, state, net):
    priv, pub = SP.coin_key()
    return pub, lambda: app.post("/account/digidollar/send", json={
        "chain": net, "pubkey": pub.hex(), "amount": "5.00", "to": "DD" + "1" * 30})


def test_a_mainnet_wallet_send_waits_for_ddmainnet(client, monkeypatch):
    app, state = client
    _with(state, monkeypatch, "dgb", "dgbtest")
    _module_on(state)
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


def test_a_send_to_ones_own_digidollar_address_is_said_plainly(client, monkeypatch):
    """2026-11-08: the review refused it as "not the send you typed"; the arcade says what it is before building it."""
    from arcade import digidollar
    app, state = client
    _with(state, monkeypatch, "dgbtest")
    _module_on(state)
    pubkey = _sign_in(app)
    priv, pub = SP.coin_key()
    state.set_setting(f"address:dgbtest:{pubkey}", b58check_encode(NETWORKS["dgbtest"].pubkeyhash_version, hash160(pub)))
    said = app.post("/account/digidollar/send", json={"chain": "dgbtest", "pubkey": pub.hex(), "amount": "5.00",
                                                      "to": digidollar.address("dgbtest", pub)})
    assert said.status_code == 400 and "your own DigiDollar address" in said.json()["detail"], said.text


def test_the_wallet_holds_a_digidollar_fee_to_its_cap():
    import re
    from arcade.ddlib import ddtx
    page = (pathlib.Path(__file__).resolve().parent.parent / "arcade/web/templates/my_wallet.html").read_text()
    assert int(re.search(r"const DD_MAX_FEE = (\d+);", page).group(1)) == ddtx.MAX_FEE
    assert "read.fee) > DD_MAX_FEE" in page

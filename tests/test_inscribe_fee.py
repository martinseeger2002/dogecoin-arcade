"""The operator's fee on every inscription (2026-10-27).

SadFrog: "The node operator should collect a fee of any inscription being inscribed - maybe 2 cents worth of the
what ever crypto is being used to inscribe". What is tested here (app._operator_fee, Admin -> Trading -> Inscription
fee):

* a per-chain amount the operator sets (`inscribe_fee:<net>`) is the least any inscription pays, the rate (0.5% by
  default) still taking more of a big one; with the rate Off it is a flat fee;
* unset, the least is the chain's own dust line;
* an ord pays it once, on its commit, and never on a reveal; the quote says it;
* the same file asked again before its split went out pays it in the rebuilt split too;
* Admin reads and saves it.
"""

import base64
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_account_real_coins import _fund                       # noqa: E402
from test_mainnet_create import OPERATOR, _open_both, _operated, _inscribe   # noqa: E402
from test_ord_inscribe import _fake_doge                         # noqa: E402
from test_doginal_wallet import _doge_account                    # noqa: E402
from test_funding import _pubkey                                 # noqa: E402

from arcade import fees, rawchain                                # noqa: E402
from arcade import node_trader                                   # noqa: E402
from arcade import ord_inscribe as oi                            # noqa: E402
from arcade.script import b58check_encode, hash160               # noqa: E402
from test_mainnet_create import MAIN                             # noqa: E402

COIN = 100_000_000
ADMIN = {"X-Arcade-Admin": "1"}


def test_a_flat_amount_per_chain_is_the_least_an_inscription_pays(client, monkeypatch):
    app, state = client
    _open_both(app)
    _fund(app, state, MAIN)
    _operated(state, monkeypatch)
    state.set_setting("inscribe_fee:main", 5 * COIN)              # "about 2 cents" of PEP, whatever that is
    fee = _inscribe(app, "main").json()["operator_fee"]
    assert fee["value"] == 5 * COIN and fee["minimum"] == 5 * COIN and fee["permille"] == 5 and fee["at_minimum"]
    state.set_setting("mainnet_create_fee_permille", 0)
    said = _inscribe(app, "main").json()
    assert said["operator_fee"]["value"] == 5 * COIN and said["operator_fee"]["permille"] == 0, "the rate Off: flat"
    assert (5 * COIN).to_bytes(8, "little").hex() in said["raw"]
    state.set_setting("inscribe_fee:main", 0)
    assert _inscribe(app, "main").json()["operator_fee"] is None, "rate Off and no amount: no fee"


def test_unset_the_least_is_the_chains_own_dust_line(client, monkeypatch):
    app, state = client
    _open_both(app)
    _fund(app, state, MAIN)
    _operated(state, monkeypatch)
    fee = _inscribe(app, "main").json()["operator_fee"]
    assert fee["value"] == fees.dust_limit(state.ledger.params if state.ledger.network == "main"
                                           else next(c for c in state.token_chains if c.network == "main").params)


def test_the_same_file_asked_again_pays_it_in_the_rebuilt_split(client, monkeypatch):
    app, state = client
    _open_both(app)
    _fund(app, state, MAIN, value=5000 * COIN)
    _operated(state, monkeypatch)
    state.set_setting("inscribe_fee:main", 3 * COIN)
    body = {"content": base64.b64encode(bytes(range(256)) * 60).decode(),
            "content_type": "application/octet-stream", "chain": "main"}
    first = app.post("/account/inscribe", json=body).json()
    assert first["split"] == "offered" and first["operator_fee"]["value"] == 3 * COIN
    again = app.post("/account/inscribe", json=body).json()
    assert again.get("resumed") and again["split"] == "offered", again
    assert again["operator_fee"]["value"] == 3 * COIN, "asking again is no way round the fee"
    assert (3 * COIN).to_bytes(8, "little").hex() in again["raw"]


def test_an_ord_pays_it_once_on_its_commit_and_the_quote_says_so(client, monkeypatch, tmp_path):
    app, state = client
    doge, _ = _doge_account(app, state, monkeypatch, tmp_path)
    sent, mined = _fake_doge(monkeypatch, doge)
    to = b58check_encode(doge.params.pubkeyhash_version, hash160(_pubkey(int("77" * 32, 16))))
    monkeypatch.setattr(node_trader, "fee_payee", lambda state, chain: to if chain.network == "doge" else "")
    state.set_setting("inscribe_fee:doge", COIN // 10)            # 0.1 DOGE: about 2 cents at $0.20
    key = _pubkey(int("31" * 32, 16))
    data = os.urandom(3000)
    q = app.post("/account/ord/quote", json={"chain": "doge", "files": [{"size": 3000, "content_type": "image/png"}] * 3})
    assert q.status_code == 200 and q.json()["operator"] == 3 * (COIN // 10), q.text
    c = app.post("/account/ord/inscribe", json={"chain": "doge", "content": base64.b64encode(data).decode(),
                                                "content_type": "image/png", "name": "a.png", "pubkey": key.hex()})
    assert c.status_code == 200, c.text
    c = c.json()
    assert c["operator_fee"]["value"] == COIN // 10 and c["operator_fee"]["address"] == to
    assert c["costs"]["operator_fee"] == COIN // 10
    from arcade.txbuild import p2pkh_script
    done = app.post("/account/sign", json={"offer": c["offer"], "signatures": ["30" * 71] * len(c["sighashes"]),
                                           "pubkey": key.hex()})
    assert done.status_code == 200, done.text
    outs = rawchain.read_tx(bytes.fromhex(sent[done.json()["txid"]])).vout
    assert (COIN // 10, p2pkh_script(to)) in outs, "one output of the commit, to the operator's Dogecoin address"
    mined[done.json()["txid"]] = 1
    r = app.post("/account/ord/inscribe", json={"run": c["run"]}).json()
    reveal = bytes.fromhex(r["raw"])
    assert len(rawchain.read_tx(reveal).vout) == 1, "a reveal stays one in, one out: no fee there"


def test_admin_reads_and_saves_it(client, monkeypatch):
    app, state = client
    got = app.get("/admin/api/state", headers=ADMIN).json()["inscribe_fee"]
    assert got["permille"] == 5 and "main" in got["chains"]
    saved = app.post("/admin/api/settings", headers=ADMIN,
                     json={"inscribe_fee": {"permille": "0", "chains": {"main": "4.5"}}})
    assert saved.status_code == 200, saved.text
    got = app.get("/admin/api/state", headers=ADMIN).json()["inscribe_fee"]
    assert got["permille"] == 0 and got["chains"]["main"]["set"] == 4.5 and got["chains"]["main"]["floor"] == 4.5
    assert state.setting("inscribe_fee:main") == 450_000_000
    bad = app.post("/admin/api/settings", headers=ADMIN, json={"inscribe_fee": {"permille": "50"}})
    assert bad.status_code == 400
    app.post("/admin/api/settings", headers=ADMIN, json={"inscribe_fee": {"chains": {"main": ""}}})
    assert state.setting("inscribe_fee:main") == 0, "emptied: back to the dust line"

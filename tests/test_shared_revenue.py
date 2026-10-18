"""A tenth of the revenue made on a borrowed chain goes to the arcade whose Core it is (2026-10-18: "If a
node operator is using a shared node 10% of the revenue should go to the operator of the shared node"): the
operator's fee is split in the same transaction, a mined block pays it in its coinbase (test_blockminer), and a
miner on another machine asks /rpc/work/<chain> whom to pay."""
import base64
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))
from test_web import app_state, client  # noqa: F401,E402

from arcade import fees  # noqa: E402
from arcade.config import NETWORKS  # noqa: E402
from arcade.mesh import coreshare  # noqa: E402
from arcade.web import app as appmod, rpc as botrpc  # noqa: E402

SHARER = "PXvoM7Gm6SzY8EsQSBNGkStULwALfWxW7W"          # a Pepecoin mainnet address (version 56)


class _Share:
    def payee(self, net, valid=lambda a: True):
        return SHARER if valid(SHARER) else None


def test_a_fee_on_a_borrowed_chain_pays_the_sharer_a_tenth(monkeypatch):
    monkeypatch.setattr(coreshare, "CURRENT", _Share())
    borrowed = SimpleNamespace(borrowed=True, network="main", params=NETWORKS["main"])
    here = SimpleNamespace(borrowed=False, network="main", params=NETWORKS["main"])
    assert appmod._sharer_cut(borrowed, 50_000_000) == (5_000_000, SHARER)
    assert appmod._sharer_cut(here, 50_000_000) == (0, ""), "a chain run here owes nobody"
    assert appmod._sharer_cut(borrowed, fees.DUST_LIMIT) == (0, ""), "a tenth under dust is not paid"
    ltc = SimpleNamespace(borrowed=True, network="ltc", params=NETWORKS["ltc"])
    assert appmod._sharer_cut(ltc, 50_000_000) == (0, ""), "an address of another chain is never paid"


def test_a_miner_elsewhere_asks_the_arcade_for_work_with_its_cookie(client):
    app, state = client
    user, secret = botrpc.read_cookie(state.write_rpc_cookie())
    auth = {"Authorization": "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode()}
    net = state.messaging.network
    assert app.post(f"/rpc/work/{net}", json={"method": "getworkshare"}).status_code == 401
    said = app.post(f"/rpc/work/{net}", json={"method": "getworkshare", "id": 3}, headers=auth).json()
    assert said["result"] == {"address": None, "percent": 10} and said["id"] == 3, "its own Core: nobody's tenth"
    refused = app.post(f"/rpc/work/{net}", json={"method": "getbalance"}, headers=auth)
    assert refused.status_code == 404 and "not a mining call" in refused.json()["error"]["message"]
    assert app.post("/rpc/work/main", json={"method": "getworkshare"}, headers=auth).status_code == 404


def test_the_panel_mines_test_coins_only_and_to_an_address_of_that_chain(client):
    from test_admin import LOCAL, WRITE
    app, state = client
    net = state.messaging.network
    listed = app.get("/admin/api/mine", headers=LOCAL).json()
    assert net in [c["net"] for c in listed["chains"]] and not listed["running"]
    assert app.post("/admin/api/mine", headers={**LOCAL, **WRITE}, json={"net": "main"}).status_code == 400
    bad = app.post("/admin/api/mine", headers={**LOCAL, **WRITE},
                   json={"net": net, "address": "DHBKqCfLs1oS6ZNvt4pT3oeC5aYzk7z1BM"})
    assert bad.status_code == 400 and "address" in bad.json()["detail"]
    page = app.get("/admin", headers=LOCAL).text
    assert 'id="minego"' in page and "a tenth of every block" in page


def test_coins_paid_to_this_arcade_land_in_the_operator_accounts_wallet(client):
    """2026-10-18: "there should be no extra step". A sharer's tenth, fees on a borrowed chain and mined
    coins go to the operator account's own address on that chain -- the one its browser gave, else the same key's
    address as its Wallet derives it -- not to a key that would have to be brought into another wallet."""
    from types import SimpleNamespace
    from arcade.script import b58check_decode, b58check_encode
    _app, state = client
    pubkey = "ab" * 32
    test_addr = b58check_encode(NETWORKS["test"].pubkeyhash_version, bytes(range(20)))
    main_addr = b58check_encode(NETWORKS["main"].pubkeyhash_version, bytes(range(1, 21)))
    state.set_setting("operator", pubkey)
    try:
        if not state.operator:
            state.claim_operator(pubkey)
        state.set_setting(f"address:{pubkey}", test_addr)
        state.set_setting(f"address:main:{pubkey}", main_addr)
        ltctest = SimpleNamespace(network="ltctest", params=NETWORKS["ltctest"], borrowed=True)
        ltc = SimpleNamespace(network="ltc", params=NETWORKS["ltc"], borrowed=True)
        got = state.payout_address(ltctest)
        assert b58check_decode(got) == (NETWORKS["ltctest"].pubkeyhash_version, bytes(range(20))), "its testnet key"
        assert b58check_decode(state.payout_address(ltc))[1] == bytes(range(1, 21)), "its mainnet key"
        given = b58check_encode(NETWORKS["ltctest"].pubkeyhash_version, bytes(range(5, 25)))
        state.set_setting(f"address:ltctest:{pubkey}", given)
        assert state.payout_address(ltctest) == given, "the address its browser gave comes first"
        assert state.home_address(ltctest) == given, "and is a borrowed chain's home address"
    finally:
        state.set_setting("operator", "")

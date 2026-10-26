"""The operator's admin panel.

2026-09-25: at the node machine it needs no password and is where the
remote password is set; from anywhere else it needs the operator account AND
that password, and sending the node's coins asks for the password again. The
operator is otherwise an account like any other.
"""

import pathlib
import sys
import time

import pytest
from nacl.signing import SigningKey

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_auth_web import _sign_in                               # noqa: E402

from arcade import adminwindow                                   # noqa: E402

LOCAL = {"host": "127.0.0.1:8420"}
EDGE = {"host": "node.dogecoinarcade.com", "cf-ray": "abc-LHR"}
WRITE = {"x-arcade-admin": "1"}
PASSWORD = "correct horse battery"


@pytest.fixture
def public(client):
    app, state = client
    state.set_setting("public_hosts", ["node.dogecoinarcade.com"])
    yield app, state
    state.set_setting("public_hosts", [])


def _operator(app, state):
    """An account, made the operator at the node machine, with a password."""
    key = SigningKey.generate()
    assert _sign_in(app, key, headers=EDGE).status_code == 200
    pubkey = key.verify_key.encode().hex()
    assert app.post("/admin/api/operator", headers={**LOCAL, **WRITE},
                    json={"pubkey": pubkey}).status_code == 200
    assert app.post("/admin/api/password", headers={**LOCAL, **WRITE},
                    json={"password": PASSWORD}).status_code == 200
    return pubkey


def test_at_the_node_machine_it_opens_without_a_password(public):
    app, state = public
    page = app.get("/admin", headers=LOCAL)
    assert page.status_code == 200 and "The node's coins" in page.text
    got = app.get("/admin/api/state", headers=LOCAL).json()
    assert got["local"] is True
    assert {"seats", "accounts", "quotas", "wallets", "faucet"} <= set(got)
    assert 'href="/admin"' in app.get("/", headers=LOCAL).text, "an Admin tab"


def test_writes_need_the_admin_header_and_this_origin(public):
    """So a page on another site cannot drive the no-password panel."""
    app, state = public
    assert app.post("/admin/api/seats", headers=LOCAL, json={"seats": 7}).status_code == 403
    assert app.post("/admin/api/seats", json={"seats": 7}, headers={
        **LOCAL, **WRITE, "origin": "https://evil.example"}).status_code == 403
    assert app.post("/admin/api/seats", headers={**LOCAL, **WRITE},
                    json={"seats": 7}).status_code == 200
    assert state.setting("seats") == 7 and state.accounts().seats == 7


def test_limits_faucet_and_switches_are_saved(public):
    app, state = public
    ok = app.post("/admin/api/quotas", headers={**LOCAL, **WRITE},
                  json={"hour": {"post": 5, "message": 0}, "bytes": 1234})
    assert ok.status_code == 200
    assert state.setting("quota:post") == 5 and state.setting("quota:message") == 0
    assert state.setting("quota:bytes") == 1234
    assert app.post("/admin/api/settings", headers={**LOCAL, **WRITE}, json={
        "faucet_gift": "50", "auto_update": False,
        "moderation": {"url": "http://m:1/v1", "model": "q"}}).status_code == 200
    assert state.setting("faucet") == 50 * 10**8
    assert state.setting("auto_update") is False
    assert state.setting("moderation") == {"url": "http://m:1/v1", "model": "q"}
    app.post("/admin/api/settings", headers={**LOCAL, **WRITE}, json={"moderation": {}})
    assert state.setting("moderation") is None


def test_a_screening_token_is_kept_hidden_and_saving_keeps_what_the_form_does_not_show(public):
    """2026-09-30: "a place where a person can enter a vLLM address and access
    token". The token is saved, never sent back, and survives a save that does
    not retype it; settings the form does not show (priority) survive too."""
    app, state = public
    state.set_setting("moderation", {"url": "http://m:1/v1", "model": "q", "priority": -10})
    assert app.post("/admin/api/settings", headers={**LOCAL, **WRITE}, json={
        "moderation": {"url": "http://m:1/v1", "model": "q", "key": "sekrit"}}).status_code == 200
    assert state.setting("moderation") == {"url": "http://m:1/v1", "model": "q",
                                           "priority": -10, "key": "sekrit"}
    shown = app.get("/admin/api/state", headers={**LOCAL, **WRITE}).text
    assert "sekrit" not in shown and '"has_key":true' in shown.replace(" ", "")
    app.post("/admin/api/settings", headers={**LOCAL, **WRITE}, json={
        "moderation": {"url": "http://m:2/v1", "model": "q", "key": ""}})
    assert state.setting("moderation")["key"] == "sekrit", "a blank box keeps the token"
    app.post("/admin/api/settings", headers={**LOCAL, **WRITE}, json={
        "moderation": {"url": "http://m:2/v1", "model": "q", "clear_key": True}})
    assert "key" not in state.setting("moderation")
    bad = app.post("/admin/api/moderation/test", headers={**LOCAL, **WRITE},
                   json={"url": "http://127.0.0.1:9/v1"})
    assert bad.status_code == 400 and "no answer" in bad.json()["detail"]


def test_a_stranger_gets_nothing_from_outside(public):
    app, state = public
    assert app.get("/admin/api/state", headers=EDGE).status_code in (403, 404)
    page = app.get("/admin/login", headers=EDGE)
    assert page.status_code == 200 and "operator" in page.text


def test_from_outside_it_takes_the_operator_account_and_the_password(public):
    app, state = public
    _operator(app, state)
    # signed in as the operator, but no admin password yet this session
    assert app.get("/admin/api/state", headers=EDGE).status_code == 401
    assert app.post("/admin/login", headers=EDGE,
                    json={"password": "wrong wrong wrong"}).status_code == 403
    assert app.post("/admin/login", headers=EDGE,
                    json={"password": PASSWORD}).status_code == 200
    got = app.get("/admin/api/state", headers=EDGE).json()
    assert got["local"] is False and got["operator"]["password_set"] is True
    # ...and still cannot rename the operator or change the password from out here
    assert app.post("/admin/api/password", headers={**EDGE, **WRITE},
                    json={"password": "x" * 20}).status_code == 403
    # sending the node's coins asks for the password again
    refused = app.post("/admin/api/send/confirm", headers={**EDGE, **WRITE},
                       json={"ticket": "whatever"})
    assert refused.status_code == 403 and "password" in refused.json()["detail"]


def test_outside_the_operator_is_an_account_plus_admin_not_the_node(public):
    """The operator's session no longer opens the node's own pages from outside;
    everything the node does from there is in /admin, behind the password."""
    app, state = public
    _operator(app, state)
    app.post("/admin/login", headers=EDGE, json={"password": PASSWORD})
    assert app.get("/wallet", headers=EDGE).status_code == 404
    page = app.get("/me", headers=EDGE).text
    assert 'href="/me/messages"' in page and 'href="/admin"' in page


def test_a_new_password_signs_every_remote_session_out(public):
    app, state = public
    _operator(app, state)
    app.post("/admin/login", headers=EDGE, json={"password": PASSWORD})
    assert app.get("/admin/api/state", headers=EDGE).status_code == 200
    app.post("/admin/api/password", headers={**LOCAL, **WRITE},
             json={"password": "another long password"})
    assert app.get("/admin/api/state", headers=EDGE).status_code == 401


def test_the_admin_password_is_twelve_characters_at_least(public):
    """Was 10 until 2026-10-25; now the floor every new password has (issue #12)."""
    from arcade import accounts

    app, state = public
    _operator(app, state)
    refused = app.post("/admin/api/password", headers={**LOCAL, **WRITE}, json={"password": "x" * 11})
    assert refused.status_code == 400 and "at least 12 characters" in refused.json()["detail"]
    assert app.post("/admin/login", headers=EDGE, json={"password": PASSWORD}).status_code == 200, \
        "the old one still opens"
    assert app.post("/admin/api/password", headers={**LOCAL, **WRITE},
                    json={"password": "x" * accounts.MIN_PASSWORD}).status_code == 200


def test_the_startup_window_points_at_the_panel_and_installs(tmp_path, monkeypatch):
    monkeypatch.setattr(adminwindow.platform, "system", lambda: "Linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    url = adminwindow.admin_url(8420)
    assert url == "http://127.0.0.1:8420/admin"
    assert url in adminwindow.message(url, True)
    path = adminwindow.install(8420)
    assert path == tmp_path / "autostart" / "dogecoinarcade-admin.desktop"
    assert "--port 8420" in path.read_text()
    assert adminwindow.uninstall() and not path.exists()


def test_chains_run_here_or_borrowed_with_testnet_and_one_more_here(public, monkeypatch):
    """2026-10-17: Cores shared between arcades. Testnet always runs here, and at least one more;
    sharing is one box per chain that runs here; which Cores run here applies at a restart."""
    app, state = public
    got = app.get("/admin/api/state", headers=LOCAL).json()["chains"]
    nets = {r["net"]: r for r in got["rows"]}
    assert {state.messaging.network, "main", "doge", "ltc", "ltctest"} <= set(nets)
    assert nets[state.messaging.network]["required"]
    assert nets["main"]["share"] is True, "shared unless the operator unticks it (2026-10-18)"
    app.post("/admin/api/cores", headers={**LOCAL, **WRITE}, json={"share": {"main": False}})
    assert state.setting("share:main") is False
    assert {r["net"]: r for r in app.get("/admin/api/state", headers=LOCAL).json()["chains"]["rows"]}["main"]["share"] is False
    assert nets["main"]["now"] == "here" and nets["doge"]["now"] in ("here", "off")
    refused = app.post("/admin/api/cores", headers={**LOCAL, **WRITE},
                       json={"modes": {"main": "borrow", "doge": "borrow"}})
    assert refused.status_code == 400 and "at least one more" in refused.json()["detail"]
    ok = app.post("/admin/api/cores", headers={**LOCAL, **WRITE},
                  json={"modes": {"doge": "borrow"}, "share": {"main": True, "doge": True}}).json()
    assert ok["restart"] is (nets["doge"]["now"] != "borrow")
    assert state.setting("core:doge") == "borrow" and state.setting("share:main") is True
    assert not state.setting("share:doge"), "a borrowed chain is not shared onward"
    rows = {r["net"]: r for r in app.get("/admin/api/state", headers=LOCAL).json()["chains"]["rows"]}
    assert rows["doge"]["chosen"] == "borrow" and rows["main"]["share"] is True
    assert app.post("/admin/api/cores", headers={**LOCAL, **WRITE}, json={"modes": {"main": "off"}}).status_code == 400
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    assert app.post("/admin/api/restart", headers={**LOCAL, **WRITE}).status_code == 409
    assert 'id="chains"' in app.get("/admin", headers=LOCAL).text


def test_the_ords_tab_lists_each_chain_with_its_official_start_and_saves_its_mode(public):
    """Admin -> Ords (2026-10-20): each chain's own ord index, its official first-inscription height, how far
    it is, and walk / help / off for each."""
    app, state = public
    page = app.get("/admin", headers=LOCAL)
    assert page.status_code == 200 and 'data-tab="ords"' in page.text and 'id="ordcoins"' in page.text \
        and 'id="ordchain"' in page.text, "a coin for each chain, then that chain's panel (2026-10-20)"
    assert 'id="oprows"' in page.text, "Network -> Other operators"
    assert app.post("/admin/api/ords", headers={**LOCAL, **WRITE}, json={"net": "doge", "mode": "fly"}).status_code == 400
    assert app.post("/admin/api/ords", headers={**LOCAL, **WRITE}, json={"net": "btc", "mode": "off"}).status_code == 400
    assert app.post("/admin/api/ords", headers=LOCAL, json={"net": "main", "mode": "help"}).status_code == 403
    assert state.ord_mode("main") == "off" and state.ord_mode("doge") == "help", "off until turned on; Dogecoin helps"
    assert app.post("/admin/api/ords", headers={**LOCAL, **WRITE}, json={"net": "main", "mode": "help"}).json()["ok"]
    assert state.ord_mode("main") == "help"
    from arcade import doginal_index as dx
    rows = {r["net"]: r for r in app.get("/admin/api/ords", headers=LOCAL).json()["rows"]}
    assert rows, "every chain this arcade reads that has ords"
    for net, r in rows.items():
        assert r["base"] == dx.STARTS[net] and r["source"]
    assert rows["main"]["mode"] == "help"
    # 2026-10-22, the operator: the four choices "are not registering the clicks". Each of them, chosen right after a read
    # (the status is kept 20 s), shows at once.
    for mode in ("walk", "read", "off", "help"):
        assert app.post("/admin/api/ords", headers={**LOCAL, **WRITE}, json={"net": "main", "mode": mode}).json()["ok"]
        again = {r["net"]: r for r in app.get("/admin/api/ords", headers=LOCAL).json()["rows"]}
        assert again["main"]["mode"] == mode, mode
    assert all(isinstance(r["disk"], int) and r["disk"] >= 0 for r in rows.values()), "each index's size on disk"
    assert all(isinstance(r["copies"], list) and isinstance(r["others"], list) for r in rows.values()), "the ring"
    assert all(r["icon"].startswith("/coin-") for r in rows.values()), "each chain's coin"
    # 2026-10-21: before the tip is known (just started) no percentage, never "61,750,500% indexed"
    for r in rows.values():
        assert r["walked_pct"] is None or 0 <= r["walked_pct"] <= 100
        assert all(c["frac"] is None or 0 <= c["frac"] <= 1 for c in r["copies"])
        if not r["tip_known"]:
            assert r["walked_pct"] is None and all(c["frac"] is None for c in r["copies"])
    said = app.get("/admin/api/ords", headers=LOCAL).json()
    assert "free" in said and "me" in said, "free disk, and this arcade's own @name for the ring"
    assert app.get("/admin/api/ords", headers=EDGE).status_code != 200, "never shown to a stranger"
    assert "rows" in app.get("/admin/api/operators", headers=LOCAL).json()
    state.set_setting("ord_index:main", None)
    # Cores for ords (2026-10-20): a number from 1 to this machine's.
    import os
    assert app.post("/admin/api/ords", headers={**LOCAL, **WRITE}, json={"cores": 0}).status_code == 400
    assert app.post("/admin/api/ords", headers={**LOCAL, **WRITE},
                    json={"cores": (os.cpu_count() or 1) + 1}).status_code == 400
    assert app.post("/admin/api/ords", headers={**LOCAL, **WRITE}, json={"cores": 1}).json()["cores"] == 1
    said = app.get("/admin/api/ords", headers=LOCAL).json()
    assert said["cores"] == 1 and said["machine"] == (os.cpu_count() or 1) and "ordcoressave" in page.text
    state.set_setting("ord_cores", None)
    # Watch only (was Read only), and the lookup box (2026-10-20).
    assert app.post("/admin/api/ords", headers={**LOCAL, **WRITE}, json={"net": "main", "mode": "read"}).json()["ok"]
    assert state.ord_mode("main") == "read" and 'Watch only' in page.text and 'id="ordlookq"' in page.text
    assert app.get("/admin/api/ords/lookup?net=main&q=nonsense", headers=LOCAL).status_code == 400
    said = app.get("/admin/api/ords/lookup?net=main&q=" + "ab" * 32 + "i0", headers=LOCAL)
    assert said.status_code == 409 and "linked arcade" in said.json()["detail"], "no walker linked in a test"
    # Ticks, names and addresses go to the same box (2026-10-20); a walker answers them, a reader asks one.
    for q in ("wow!", "jon.doge", "nYW2BPLENpu2nGa7WCExvzxD3hQYueULFa"):
        got = app.get("/admin/api/ords/lookup?net=main&q=" + q, headers=LOCAL)
        assert got.status_code == 409, (q, got.text)
    assert app.get("/admin/api/ords/names?net=main", headers=LOCAL).status_code == 409, "names: walkers only"
    state.set_setting("ord_index:main", "walk")
    names = app.get("/admin/api/ords/names?net=main", headers=LOCAL).json()
    assert names["names"] == 0 and set(names["readings"]) == {"spec", "json", "doge2023"}
    assert app.get("/admin/api/ords/lookup?net=main&q=wow!", headers=LOCAL).json() == {
        "token": None, "from": "this arcade", "walked_to": 186920}
    assert 'id="ordnamesgo"' in page.text
    state.set_setting("ord_index:main", None)


def test_the_first_operator_signs_up_with_the_offered_fruit_at_the_machine(public):
    """2026-10-20: a new arcade's operator gets a fruit name automatically (arcade/fruit.py,
    mesh/fruitshare.py). At the machine, with no operator yet, the offered fruit is free to that sign-up, which
    becomes the operator and asks the fruit's holder to send the name. From outside it is nobody's to take."""
    from nacl.signing import SigningKey
    from arcade.script import b58check_encode, hash160
    app, state = public
    state.set_setting("fruit_offer", {"name": "mango", "from": "holder-node"})
    asked = []

    class Share:
        def ask_to_give(self, to):
            asked.append(to)

    class Mesh:
        fruitshare = Share()
    was = getattr(state, "mesh", None)
    state.mesh = Mesh()
    try:
        assert 'value="mango"' in app.get("/join", headers=LOCAL).text
        assert 'value="mango"' not in app.get("/join", headers=EDGE).text
        assert app.get("/signup/mango", headers=LOCAL).json()["fruit"] is True
        assert "fruit" not in app.get("/signup/mango", headers=EDGE).json()
        key = SigningKey.generate()
        pubkey = key.verify_key.encode().hex()
        chain = state.messaging
        address = b58check_encode(chain.params.pubkeyhash_version, hash160(b"\x02" + key.verify_key.encode()))
        said = app.post("/signup", headers=LOCAL, json={"tag": "mango", "pubkey": pubkey, "address": address,
                                                        "blob": {"sealed": "x"}})
        assert said.status_code == 200, said.text
        assert said.json()["fruit"] is True
        assert state.operator == pubkey
        assert asked == [address]
    finally:
        state.mesh = was
        state.set_setting("fruit_offer", None)


def test_a_core_here_with_its_wallet_switched_off_is_held_with_the_arcades_own_key():
    """2026-10-20: a test machine's Dogecoin Core runs with disablewallet=1; every wallet call said "Method not found" and the
    operator's Wallet showed no Dogecoin. Such a Core is treated as a borrowed chain is: the arcade's own key holds
    the coins (keywallet.py), on every chain but P test. A Core with a wallet keeps its own."""
    from arcade.rpc import RpcError
    from arcade.web.state import AppState

    class Rpc:
        def __init__(self, wallet):
            self.wallet = wallet

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def call(self, method, *params):
            if not self.wallet:
                raise RpcError(-32601, "Method not found", method)
            return {"walletversion": 130000}

    class Chain:
        is_addon, borrowed = True, False

        def __init__(self, net, wallet):
            self.network, self._wallet = net, wallet

        def rpc(self):
            return Rpc(self._wallet)

    class Holder:
        KEYED_SECONDS = 300
        messaging = Chain("test", wallet=False)
    holder = Holder()
    assert AppState.keyed(holder, Chain("doge", wallet=False)) is True
    assert AppState.keyed(holder, Chain("main", wallet=False)) is True, "chain-agnostic: P main too"
    assert AppState.keyed(holder, Holder.messaging) is False, "P test: the identity key itself lives in its wallet"
    assert AppState.keyed(holder, Chain("ltc", wallet=True)) is False
    borrowed = Chain("ltctest", wallet=True)
    borrowed.borrowed = True
    assert AppState.keyed(holder, borrowed) is True


def test_the_offered_fruit_passes_the_claimed_name_check_and_nothing_else_does(public, monkeypatch):
    """SadFrog's /signup refuses a name the chain says another address holds (team/signin-hardening). The operator's
    offered fruit is held on the chain by the fruit stand until it is sent, so it alone goes through (2026-10-20)."""
    from nacl.signing import SigningKey
    from arcade.script import b58check_encode, hash160
    app, state = public
    state.set_setting("fruit_offer", {"name": "papaya", "from": "holder-node"})
    held = {"papaya": "fruit-stand-address", "someone": "their-address"}

    class Index:
        def address_of(self, tag):
            return held.get(tag)

        def __getattr__(self, name):
            raise AttributeError(name)
    monkeypatch.setattr(type(state), "token_index", lambda self, chain: Index())

    class Mesh:
        class fruitshare:
            @staticmethod
            def ask_to_give(to):
                pass
    was = getattr(state, "mesh", None)
    state.mesh = Mesh()

    def signup(tag):
        key = SigningKey.generate()
        address = b58check_encode(state.messaging.params.pubkeyhash_version, hash160(b"\x02" + key.verify_key.encode()))
        return app.post("/signup", headers=LOCAL, json={"tag": tag, "pubkey": key.verify_key.encode().hex(),
                                                        "address": address, "blob": {"sealed": "x"}})
    try:
        refused = signup("someone")
        assert refused.status_code == 400 and "claimed on the chain" in refused.json()["detail"]
        took = signup("papaya")
        assert took.status_code == 200 and took.json()["fruit"] is True, took.text
    finally:
        state.mesh = was
        state.set_setting("fruit_offer", None)


def _mesh_of(me, links, rtts=None, offers=None, said=None, sharing=None):
    """A mesh as /admin/api/arcades reads it: links and their round trips, who shares which Core, and what each
    linked arcade said about itself (mesh/operators.py)."""
    class Node:
        pass
    node = Node()
    node.links = {p: object() for p in links}
    node.addrs = {}
    node.rtts = lambda: dict(rtts or {})

    class Cores:
        pass
    cores = Cores()
    cores.offers = dict(offers or {})
    cores.status = lambda: dict(sharing or {})

    class Ops:
        pass
    ops = Ops()
    ops.peers = dict(said or {})

    class Mesh:
        pass
    mesh = Mesh()
    mesh.loop, mesh.node_id, mesh.node, mesh.coreshare, mesh.operators = object(), me, node, cores, ops
    return mesh


def test_overview_lists_every_arcade_this_one_first_with_its_cores_ords_and_link(public, monkeypatch):
    """Admin -> Overview's Arcades panel (2026-10-21, approving the mockup): a row per arcade running on the
    mesh, THIS arcade first with its own Cores (shared or not) and @name; every other with what it said, the Cores it
    shares, the ord indexes it holds with how much of the chain each has walked, and its link."""
    from arcade import doginal_index as dx
    app, state = public
    me, fast, slow, gone = "a" * 64, "b" * 64, "c" * 64, "d" * 64
    base = dx.STARTS["main"]

    class Index:
        def __init__(self):
            self.base, self.tip = base, base + 1000 + dx.WALK_SETTLE - 1

        def holders(self):
            return [(fast, base + 500), ("", base + 1000)]

        def mode(self):
            return "walk"
        helped = 0

        class chunks:                                    # 2026-10-21: helping, or read only
            peers = {slow: {"mode": "help", "helped": 4200, "at": time.time()},
                     gone: {"mode": "read", "at": time.time()},
                     me: {"mode": "help", "at": time.time() - 900}}   # stale: not shown
    monkeypatch.setattr(state, "ord_indexes", lambda: {"main": Index()})
    monkeypatch.setattr(state, "operator_tag", lambda: "strawberry")
    state.set_setting("share:regtest", False)
    was = getattr(state, "mesh", None)
    state.mesh = _mesh_of(me, [fast, slow], rtts={fast: 48.2, slow: 1500.0},
                          offers={fast: {"doge": {"h": 1}, "main": {"h": 2}}, gone: {"ltc": {"h": 3}}},
                          said={fast: {"tag": "bigchiefenergy", "hosts": ["app.dogecoinarcade.com"]},
                                gone: {"tag": "faraway", "hosts": []}},
                          sharing={"main": {"sharing": True}, "regtest": {"sharing": False}})
    monkeypatch.setattr(state, "store", _store_announcing(state, [gone]))
    try:
        got = app.get("/admin/api/arcades", headers=LOCAL).json()
    finally:
        state.mesh = was
        state.set_setting("share:regtest", None)
    assert got["ok"] and got["total"] == 4 and got["running"] == 3 and got["linked"] == 2
    rows = got["rows"]
    mine = rows[0]
    assert mine["me"] and mine["tag"] == "strawberry" and mine["node"] == me[:12] and mine["link"] == {"state": "me"}
    cores = {c["net"]: c for c in mine["cores"]}
    assert cores["main"]["shared"] is True and cores["regtest"]["shared"] is False, "shared, or running unshared"
    assert cores["regtest"]["switch"] is False and cores["main"]["icon"] == "/coin-pep.png"
    assert mine["ords"] == [{"net": "main", "label": "Pepecoin main", "icon": "/coin-pep.png", "role": "walk", "pct": 100.0,
                             "walked_to": base + 1000}]
    assert [r["node"] for r in rows[1:]] == [fast[:12], slow[:12], gone[:12]], "linked fastest first, then the rest"
    f, s, g = rows[1:]
    assert f["tag"] == "bigchiefenergy" and f["host"] == "app.dogecoinarcade.com"
    assert [c["net"] for c in f["cores"]] == ["main", "doge"] and all(c["shared"] for c in f["cores"])
    assert f["cores"][1]["icon"] == "/coin-doge.png"
    assert f["ords"][0]["pct"] == 50.0 and f["link"] == {"state": "linked", "ms": 48.2}
    assert s["tag"] == "" and s["cores"] == [] and s["link"]["state"] == "slow", "over a second is slow"
    assert [(o["role"], o["helped"]) for o in s["ords"]] == [("help", 4200)], "helping on the PEP index"
    assert [o["role"] for o in g["ords"]] == ["read"], "read only"
    assert g["link"] == {"state": "none"} and g["cores"] == [], "a Core is known only from an arcade linked now"
    assert g["tag"] == "faraway", "what an arcade said stays shown after its link is down"
    assert app.get("/admin/api/arcades", headers=EDGE).status_code != 200, "never shown to a stranger"


def _store_announcing(state, keys):
    """state.store, with these mesh keys announced on the chain in the last day."""
    import contextlib
    import time as _time
    real = state.store

    @contextlib.contextmanager
    def store():
        with real() as s:
            class Wrapped:
                def __getattr__(self, name):
                    return getattr(s, name)

                def mesh_peers(self, network, since=0):
                    return [{"mesh_key": k, "seen_at": int(_time.time())} for k in keys]
            yield Wrapped()
    return store


def test_overview_without_a_mesh_shows_this_arcade_alone(public):
    app, state = public
    got = app.get("/admin/api/arcades", headers=LOCAL).json()
    assert got["ok"] is False and got["total"] == 1 and len(got["rows"]) == 1 and got["rows"][0]["me"]
    assert all(c["icon"].startswith("/coin-") for c in got["rows"][0]["cores"]), "the arcade's own coin faces"


def test_overview_has_no_balance_or_notification_cards_and_an_arcades_panel(public):
    """2026-10-21: "We should remove all the token balances that's not necessary in overview ... We don't have
    to have a display of accounts with notifications at all." The Coins tab keeps its own wallets list."""
    app, state = public
    page = app.get("/admin", headers=LOCAL).text
    assert "'accounts with notifications')" not in page and "' balance'" not in page, "neither card is drawn"
    assert 'id="arcadesline"' not in page and "other arcade' + " not in page
    assert 'id="arcades"' in page and 'id="arcrows"' in page and "/admin/api/arcades" in page
    assert "Ord indexes" in page and 'src="/coin-pep.png"' in page, "the legend draws the arcade's own coin"
    assert 'id="wallets"' in page and page.count("d.wallets.forEach") == 1, "the Coins tab's list stays"

"""Delegated orders are custodial, so they are capped (2026-10-25, SadFrog: "label it plainly and cap delegated
amounts"). A delegated order's key goes to the seller's node (wallet.js delegateOrder, node_trader.delegate), so its
operator could spend the deposit. One order gives at most node_trader.DELEGATE_CAP on its chain -- 1,000 DOGE,
100,000 PEP, 1 LTC; another real-coin chain its swap BIG line; a test chain no cap -- refused by the page before the
key leaves it and by the node; the operator moves them with delegate_cap:<net>; the page says the key is held."""

import pathlib
import re
import secrets
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                               # noqa: F401,E402
from test_trade_tab import run_wallet_js                              # noqa: E402

from arcade import node_trader as NT, trade as T                     # noqa: E402

COIN = 100_000_000
ROOT = pathlib.Path(__file__).resolve().parent.parent
HELD = ("This arcade holds this order's key while it trades for you: its operator could spend what you deposit. "
        "Only delegate amounts you'd trust this arcade with.")


class _State:
    def __init__(self, home, settings=None):
        self.home, self.settings = home, dict(settings or {})

    def setting(self, key, default=None):
        return self.settings.get(key, default)


def _coin(network, amount):
    return {"network": network, "kind": "coin", "asset": "", "amount": amount}


def _trader(tmp_path, monkeypatch, settings=None):
    monkeypatch.setattr(NT.courier, "mesh_inbox", lambda state: None)
    monkeypatch.setattr(NT.NodeTrader, "_watch_deposit", lambda self, network, address: None)
    return NT.NodeTrader(_State(tmp_path, settings), {}, None)


def _delegate(trader, give, get):
    secret = f"{secrets.randbelow(2 ** 250) + 1:064x}"
    oid = "ord-" + secrets.token_hex(8)
    return trader.delegate(oid, give, get, secret, {give["network"]: "deposit"}, {get["network"]: "payout"},
                           owner="seller")


# --- the caps ---------------------------------------------------------------------------------------------------

def test_the_caps_are_sadfrogs_and_every_other_real_coin_chain_has_its_big_line(tmp_path):
    caps = NT.delegate_caps(_State(tmp_path))
    assert caps["doge"] == 1_000 * COIN and caps["main"] == 100_000 * COIN and caps["ltc"] == 1 * COIN
    for net in ("bel", "lky", "kot", "wjk"):
        assert caps[net] == T.BIG[net], f"{net}: its swap BIG line"
    assert not {"regtest", "test", "ltctest"} & set(caps), "test chains are not capped"
    assert NT.delegate_cap(_State(tmp_path), "regtest") is None


def test_wallet_js_keeps_the_same_caps():
    js = (ROOT / "arcade/web/templates/wallet_js.js").read_text()
    block = re.search(r"DELEGATE_CAP: \{([^}]*)\}", js).group(1)
    got = {k: eval(v.replace("_", "")) for k, v in re.findall(r"(\w+): ([0-9_ *]+)", block)}   # noqa: S307
    assert got == NT.DELEGATE_CAP


def test_wallet_js_works_out_every_cap_and_refusal_as_the_node_does(tmp_path):
    cases = []
    for net, cap in NT.delegate_caps(_State(tmp_path)).items():
        cases += [_coin(net, cap), _coin(net, cap + 1), _coin(net, 1)]
    cases += [_coin("regtest", 10 ** 15), {"network": "main", "kind": "token", "asset": "150", "amount": 10 ** 15},
              {"network": "doge", "kind": "nft", "asset": "ab" * 32, "amount": 1}]
    zero = {"doge": 0, "main": 50_000_000}                          # an operator's own lines: none, and 0.5 PEP
    cases_zero = [_coin("doge", 1), _coin("main", 50_000_000), _coin("main", 50_000_001)]
    got = run_wallet_js(tmp_path, """
        const out = {defaults: w.delegateCapDefaults(), plain: [], set: []};
        for (const g of data.cases) out.plain.push(await w.delegateOver(g));
        for (const g of data.zero) out.set.push(await w.delegateOver(g, data.caps));
        return out;""", {"cases": cases, "zero": cases_zero, "caps": zero})
    assert got["defaults"] == NT.delegate_caps(_State(tmp_path)), "the page's defaults are the node's"

    def python(give, settings=None):
        try:
            NT.check_delegate_cap(_State(tmp_path, settings), give)
        except ValueError as exc:
            return str(exc)
        return ""
    assert got["plain"] == [python(g) for g in cases], "refused alike, word for word"
    assert sum(1 for r in got["plain"] if r) == len(NT.delegate_caps(_State(tmp_path))), "just the one over each cap"
    assert "at most 1,000 DOGE per order" in got["plain"][[c["amount"] for c in cases].index(1_000 * COIN + 1)]
    settings = {f"delegate_cap:{k}": v for k, v in zero.items()}
    assert got["set"] == [python(g, settings) for g in cases_zero]
    assert got["set"][0].startswith("this arcade takes no delegated DOGE orders") and "at most 0.5 PEP" in got["set"][2]


def test_wallet_js_refuses_an_over_cap_delegated_order_before_anything_leaves_the_page(tmp_path):
    """listOrder throws at once: no key derived, nothing asked of the node (wallet `{}` has no seed to derive from)."""
    got = run_wallet_js(tmp_path, """
        const said = [];
        for (const [give, delegated] of data) {
          try { await w.listOrder({}, give, {network: "regtest", amount: 1}, {approved: true, delegated}); said.push("listed"); }
          catch (e) { said.push(String(e.message || e)); }
        }
        return said;""", [[_coin("doge", 1_000 * COIN + 1), True], [_coin("ltc", 2 * COIN), True],
                          [_coin("doge", 1_000 * COIN), True], [_coin("doge", 5_000 * COIN), False]])
    assert got[0] == ("You can't list this swap: this arcade holds a delegated order's key, so it takes at most "
                      "1,000 DOGE per order: list less, or split it.")
    assert "at most 1 LTC per order" in got[1]
    assert "at most" not in got[2] and "at most" not in got[3], "at the cap, and a page's own order, go on (and fail later here)"


# --- the node ----------------------------------------------------------------------------------------------------

def test_the_node_takes_an_order_at_the_cap_and_refuses_one_over_it(tmp_path, monkeypatch):
    trader = _trader(tmp_path, monkeypatch)
    pep = _coin("regtest", 3 * COIN)
    for net, cap in (("doge", 1_000 * COIN), ("main", 100_000 * COIN), ("ltc", COIN), ("bel", T.BIG["bel"])):
        other = pep if net != "regtest" else _coin("doge", COIN)
        assert len(_delegate(trader, _coin(net, cap), other)) == 33, f"{net}: at the cap"
        with pytest.raises(ValueError, match="holds a delegated order's key, so it takes at most"):
            _delegate(trader, _coin(net, cap + 1), other)
    with pytest.raises(ValueError, match="at most 1,000 DOGE per order"):
        _delegate(trader, _coin("doge", 2_000 * COIN), pep)
    assert _delegate(trader, _coin("regtest", 10 ** 15), _coin("doge", COIN)), "a test chain is not capped"
    token = {"network": "main", "kind": "token", "asset": "150", "amount": 10 ** 15}
    assert _delegate(trader, token, _coin("doge", COIN)), "a token is not priced here, so not capped"
    with trader._db() as conn:
        held = conn.execute("SELECT COUNT(*) FROM delegated").fetchone()[0]
    assert held == 6, "nothing over a cap was kept"


def test_the_operator_moves_a_cap_and_zero_takes_none(tmp_path, monkeypatch):
    trader = _trader(tmp_path, monkeypatch, {"delegate_cap:doge": 5_000 * COIN, "delegate_cap:ltc": 0})
    assert _delegate(trader, _coin("doge", 5_000 * COIN), _coin("regtest", COIN))
    with pytest.raises(ValueError, match="at most 5,000 DOGE"):
        _delegate(trader, _coin("doge", 5_000 * COIN + 1), _coin("regtest", COIN))
    with pytest.raises(ValueError, match="takes no delegated LTC orders"):
        _delegate(trader, _coin("ltc", 1), _coin("regtest", COIN))


def test_a_real_coin_chain_with_no_line_takes_no_delegated_order(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "BIG", {k: v for k, v in T.BIG.items() if k != "wjk"})
    assert NT.delegate_cap(_State(tmp_path), "wjk") == 0
    with pytest.raises(ValueError, match="takes no delegated WJK orders"):
        NT.check_delegate_cap(_State(tmp_path), _coin("wjk", 1))


def test_the_route_refuses_an_over_cap_order_before_it_keeps_the_key(client, monkeypatch):
    from test_dogecoin_network import _with_doge
    from test_me_page import _seat
    app, state = client
    _with_doge(state, monkeypatch, ready=True)
    _seat(app)
    kept = []
    monkeypatch.setattr(type(state.node_trader), "delegate", lambda self, *a, **k: kept.append(a))
    secret = f"{secrets.randbelow(2 ** 250) + 1:064x}"
    order = lambda n: {"id": "ord-" + secrets.token_hex(8), "give": _coin("doge", n),   # noqa: E731
                       "get": _coin(state.messaging.network, COIN)}
    over = app.post("/account/orders/delegate", json={"order": order(1_000 * COIN + 1), "key": secret,
                                                       "deposit": {}, "payout": {}})
    assert over.status_code == 400 and "at most 1,000 DOGE per order" in over.json()["detail"], over.text
    at = app.post("/account/orders/delegate", json={"order": order(1_000 * COIN), "key": secret,
                                                     "deposit": {}, "payout": {}})
    assert at.status_code == 400 and "at most" not in at.json()["detail"], "at the cap it goes on to the next check"
    assert not kept
    state.set_setting("delegate_cap:doge", 2_000 * COIN)
    assert "at most 2,000 DOGE" in app.post("/account/orders/delegate", json={
        "order": order(3_000 * COIN), "key": secret, "deposit": {}, "payout": {}}).json()["detail"]
    caps = app.get("/account").json()["delegate_cap"]
    assert caps["doge"] == 2_000 * COIN and caps["ltc"] == COIN, "the page reads the node's own caps"


def test_the_admin_sets_the_caps(client):
    from test_admin import LOCAL, WRITE
    app, state = client
    went = app.post("/admin/api/settings", headers={**LOCAL, **WRITE}, json={"delegate_cap": {"doge": "250", "ltc": "0"}})
    assert went.status_code == 200, went.text
    shown = app.get("/admin/api/state", headers=LOCAL).json()["delegate_cap"]
    assert shown["doge"] == 250 and shown["ltc"] == 0 and shown["main"] == 100_000
    assert NT.delegate_caps(state)["doge"] == 250 * COIN and NT.delegate_caps(state)["ltc"] == 0
    assert 'id="savedelegatecap"' in app.get("/admin", headers=LOCAL).text


# --- what the page says ------------------------------------------------------------------------------------------

def test_the_listing_says_the_arcade_holds_a_delegated_orders_key():
    page = (ROOT / "arcade/web/templates/_tab_swaps.html").read_text()
    words = re.search(r'const HELD_WORDS = "([^"]*)"\s*\+\s*"([^"]*)";', page)
    assert words and words.group(1) + words.group(2) == HELD
    assert re.search(r"\? \"Yours moves to this order's own address now.*?\+ HELD_WORDS", page, re.S), \
        "in the words the List dialog shows for a delegated order"
    assert page.count("if (await overCap(give)) return;") == 3, "every List box checks the cap before it asks"


def test_an_accounts_exchange_page_carries_the_warning(client):
    from test_funding import _pubkey
    from test_me_page import _seat
    from arcade.script import b58check_encode, hash160
    app, state = client
    _seat(app)
    mine = b58check_encode(state.messaging.params.pubkeyhash_version, hash160(_pubkey(int("31" * 32, 16))))
    assert app.post("/account/address", json={"address": mine, "chain": state.messaging.network}).status_code == 200
    page = app.get("/exchange?tab=swaps").text
    assert "HELD_WORDS" in page and "This arcade holds this order's key while it trades for you" in page

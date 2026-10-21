"""Two facilitating nodes trade PEP for DOGE with nothing but the Messenger and the chains
(arcade/node_trader.py; 2026-10-13: node-facilitated trades "just like a regular
lock script that the client side does").

Node B stands an order (2 DOGE for 3 PEP). Node A proposes against it. After that the
test only mines blocks and runs each node's message scanner, as the watcher does; every
step -- accept, lock, lock, claim (revealing the secret), claim -- is the traders' own.
"""

import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_htlc import Key, doge                                        # noqa: E402,F401

from arcade import trade as T                                          # noqa: E402

COIN = 100_000_000


def _ctx(node, network):
    """A chain context pointed at a regtest node, named as the arcade names the chain."""
    from arcade.web.state import ChainContext

    class Pointed(ChainContext):
        def credentials(self):
            return node.rpc._creds

        @property
        def params(self):
            return node.params

    return Pointed(network=network, role="ledger", label=network, datadir=node.datadir)


@pytest.fixture
def market(tmp_path, regtest, doge):
    """Two nodes, each with its own home and messaging identity; PEP regtest carries the
    messages and one leg, Dogecoin regtest the other."""
    from arcade.messaging.keys import Identity
    from arcade.messaging.sender import funded_address
    from arcade.node_trader import NodeTrader
    from arcade.web.state import AppState, ChainContext

    for rpc in (regtest.rpc, doge.rpc):
        if float(rpc.call("getbalance")) < 50:
            rpc.call("generate", 101)
    traders = []
    for name in ("a", "b"):
        pep = _ctx(regtest, "regtest")
        state = AppState(home=tmp_path / name, messaging=pep,
                         ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                                             datadir=pathlib.Path("/nonexistent")))
        state.identity = Identity.generate()
        # Pin the address this node's messages are paid from, as a live node's identity
        # address is: funded with ordinary change (never the mined coins a block pays).
        home = regtest.rpc.call("getnewaddress")
        regtest.rpc.call("sendtoaddress", home, 25)
        with state.store() as store:
            store.set_meta(f"identity_address:{pep.network}", home)
        traders.append(NodeTrader(state, {"regtest": pep, "doge": _ctx(doge, "doge")}, pep))
    # Trade talk goes over the mesh only (2026-10-17): both nodes on one, in-process here, each
    # holding its rooms as a running node does on every tick.
    from test_trade_tab_browser import SharedRelay
    from arcade import trade_courier
    relay = SharedRelay()
    for t in traders:
        t.state._talk_relay = relay
        trade_courier.mesh_inbox(t.state)
    regtest.rpc.call("generate", 1)
    return traders[0], traders[1], regtest.rpc, doge.rpc


#: Blocks are mined to an address outside every wallet here: a mined block pays bare
#: pay-to-pubkey, which no message may be funded from, and a wallet that kept them would
#: offer them first (as a live node that does not mine never has).
_AWAY = Key().address


def _mine(*rpcs):
    for rpc in rpcs:
        rpc.call("generatetoaddress", 1, _AWAY)


def _scan(trader):
    from arcade.messaging.scanner import Scanner
    st = trader.state
    with st.messaging.rpc() as node, st.store() as store:
        scanner = Scanner(node, st.messaging.params, store, identity=st.identity)
        scanner.scan()            # what was mined
        scanner.scan_mempool()    # and what is on its way, as the watcher reads both


def test_two_nodes_trade_pep_for_doge_over_the_messenger(market):
    a, b, pep, dg = market
    # B stands an order: it gives 2 DOGE (its own trade key) for 3 PEP.
    b.stand("order-dogepep", {"network": "doge", "kind": "coin", "amount": 2 * COIN},
            {"network": "regtest", "kind": "coin", "amount": 3 * COIN})
    give = a.leg("regtest", 3 * COIN, b.key.pubkey)             # A gives 3 PEP to B
    get = T.Leg("doge", "coin", "", 2 * COIN, b.key.pubkey.hex(), a.key.pubkey.hex())
    trade = a.propose(b.state.identity.public_bytes, "order-dogepep", give, get)

    log = []
    for _ in range(40):
        for t in (a, b):
            _scan(t)
            log.extend((("a" if t is a else "b"),) + r for r in t.tick())
        _mine(pep, dg)
        if a.book.get(trade.id).state == "done" and (b.book.get(trade.id) or trade).state == "done":
            break
    steps = [(who, step) for who, _, step, _ in log]
    assert a.book.get(trade.id).state == "done", log[-6:]
    assert b.book.get(trade.id).state == "done", log[-6:]
    assert ("a", T.LOCK) in steps and ("b", T.LOCK) in steps, "both locked"
    assert steps.index(("a", T.LOCK)) < steps.index(("b", T.LOCK)), "the initiator locked first"
    assert ("a", T.CLAIM) in steps and ("b", T.CLAIM) in steps, "both claimed"
    assert steps.index(("a", T.CLAIM)) < steps.index(("b", T.CLAIM)), \
        "the responder claimed only after the initiator revealed the secret"


def test_a_proposal_against_no_order_is_cancelled_and_costs_nothing(market):
    a, b, pep, dg = market
    give = a.leg("regtest", 3 * COIN, b.key.pubkey)
    get = T.Leg("doge", "coin", "", 2 * COIN, b.key.pubkey.hex(), a.key.pubkey.hex())
    trade = a.propose(b.state.identity.public_bytes, "order-nonexist", give, get)
    for _ in range(3):
        for t in (a, b):
            _scan(t)
            t.tick()
        _mine(pep)
    assert a.book.get(trade.id).state == "aborted", "A walks away; it never locked"
    assert b.book.get(trade.id) is None, "B never took it on"


def test_the_first_taker_wins_and_a_second_is_refused(market):
    """2026-10-19: "no two users can fill the same order". B accepts A's first proposal; A's second, on the
    same order while the first swap runs, is refused, and the order is off B's book until the swap ends."""
    a, b, pep, dg = market
    b.stand("order-once", {"network": "doge", "kind": "coin", "amount": 2 * COIN},
            {"network": "regtest", "kind": "coin", "amount": 3 * COIN})
    give = a.leg("regtest", 3 * COIN, b.key.pubkey)
    get = T.Leg("doge", "coin", "", 2 * COIN, b.key.pubkey.hex(), a.key.pubkey.hex())
    first = a.propose(b.state.identity.public_bytes, "order-once", give, get)
    second = a.propose(b.state.identity.public_bytes, "order-once", give, get)
    for _ in range(3):
        for t in (a, b):
            _scan(t)
            t.tick()
        _mine(pep)
    assert b.book.get(first.id) is not None, "the first is taken on"
    assert b.book.get(second.id) is None, "the second is refused"
    assert a.book.get(second.id).state == "aborted"
    assert b._being_taken("order-once")


def test_the_switch_off_means_the_node_does_nothing(market):
    a, b, pep, dg = market
    b.enabled = lambda: False
    assert b.tick() == [], "node-facilitated trades off: the node trader is idle"


def _gold(trader, holder, units):
    """A token both nodes' ledgers know, with `units` of it at `holder` (as test_escrow)."""
    from test_account_tokens import _balance, _token
    for st in {id(t.state): t.state for t in trader}.values():
        _token(st, property_id=150, name="Gold")
        _balance(st, holder, 150, units)
    return 150


def _ledgers(*traders):
    for t in traders:
        t.state.token_index(t.state.messaging).sync()


def _settled(check, rpc, *traders, rounds=10):
    """`check()` once the claim is mined and indexed: "done" can come while it is still in the pool (a busy
    machine under -n 8 is slow to put it in a block), so mine and read again a few times before asking."""
    for _ in range(rounds):
        _ledgers(*traders)
        if check():
            return True
        _mine(rpc)
    _ledgers(*traders)
    return check()


def test_two_nodes_trade_a_token_for_doge(market, monkeypatch):
    """B gives 40 Gold on PEP regtest for A's 2 DOGE: the token goes into a lock with the
    coin that pays its way out, and the ledger -- not a message -- says it is there."""
    a, b, pep, dg = market
    home = pep.call("getnewaddress")
    pep.call("sendtoaddress", home, 5)
    monkeypatch.setattr(b.state, "home_address",
                        lambda chain: home if chain.network == "regtest" else "", raising=False)
    pid = _gold((a, b), home, 100 * COIN)
    _mine(pep)
    _ledgers(a, b)
    b.stand("order-gold", {"network": "regtest", "kind": "token", "asset": str(pid), "amount": 40 * COIN},
            {"network": "doge", "kind": "coin", "amount": 2 * COIN})
    give = a.leg("doge", 2 * COIN, b.key.pubkey)
    get = T.Leg("regtest", "token", str(pid), 40 * COIN, b.key.pubkey.hex(), a.key.pubkey.hex())
    trade = a.propose(b.state.identity.public_bytes, "order-gold", give, get)
    log = []
    for _ in range(40):
        for t in (a, b):
            _scan(t)
            _ledgers(t)
            log.extend((("a" if t is a else "b"),) + r for r in t.tick())
        _mine(pep, dg)
        if a.book.get(trade.id).state == "done" and (b.book.get(trade.id) or trade).state == "done":
            break
    assert a.book.get(trade.id).state == "done", log[-6:]
    assert b.book.get(trade.id).state == "done", log[-6:]
    index = a.state.token_index(a.state.messaging)
    claimed_to = a._address("regtest")
    _settled(lambda: int(index.balance(claimed_to, pid)) == 40 * COIN, pep, a, b)
    assert int(index.balance(claimed_to, pid)) == 40 * COIN, "A holds the Gold"
    assert int(index.balance(home, pid)) == 60 * COIN, "B's home kept the rest"


def test_a_token_lock_the_ledger_has_not_read_yet_is_waited_for_not_walked_from(market):
    """A lagging index must not make a good token lock look wrong (asset_at -> None)."""
    from arcade import htlc as H
    a, b, pep, dg = market
    _, h = H.make_secret()
    leg = T.Leg("regtest", "token", "150", 40, Key().pub.hex(), Key().pub.hex())
    index = a.state.token_index(a.state.messaging)
    _ledgers(a)
    with index.open() as db:
        tip = int(db.tip()["height"])
    assert T.asset_at(index, leg, "nAddressXXXXXXXXXXXXXXXXXXXXXXXXXXX", tip + 5) is None
    assert T.asset_at(index, leg, "nAddressXXXXXXXXXXXXXXXXXXXXXXXXXXX", None) is None
    assert T.asset_at(index, leg, "nAddressXXXXXXXXXXXXXXXXXXXXXXXXXXX", tip) is False


def test_two_nodes_trade_an_nft_for_doge(market, monkeypatch):
    from test_escrow import _piece
    a, b, pep, dg = market
    home = pep.call("getnewaddress")
    pep.call("sendtoaddress", home, 5)
    monkeypatch.setattr(b.state, "home_address",
                        lambda chain: home if chain.network == "regtest" else "", raising=False)
    sword = "b1" * 32
    _mine(pep)
    _ledgers(a, b)
    for t in (a, b):
        _piece(t.state, sword, home)
    give = a.leg("doge", 2 * COIN, b.key.pubkey)
    get = T.Leg("regtest", "nft", sword, 1, b.key.pubkey.hex(), a.key.pubkey.hex())
    b.stand("order-sword", {"network": "regtest", "kind": "nft", "asset": sword, "amount": 1},
            {"network": "doge", "kind": "coin", "amount": 2 * COIN})
    trade = a.propose(b.state.identity.public_bytes, "order-sword", give, get)
    log = []
    for _ in range(40):
        for t in (a, b):
            _scan(t)
            _ledgers(t)
            log.extend((("a" if t is a else "b"),) + r for r in t.tick())
        _mine(pep, dg)
        if a.book.get(trade.id).state == "done" and (b.book.get(trade.id) or trade).state == "done":
            break
    assert a.book.get(trade.id).state == "done", log[-6:]
    assert b.book.get(trade.id).state == "done", log[-6:]
    owner = lambda: a.state.token_index(a.state.messaging).inscription(sword)["owner"]
    _settled(lambda: owner() == a._address("regtest"), pep, a)
    assert owner() == a._address("regtest")


def _doginals_on_doge(dg, tmp_path, traders, postage):
    """Two small coins in the Dogecoin wallet, each carrying a Doginal by the fake index's word: (txid, vout of the
    one traded, vout of a decoy -- ANOTHER Doginal, the smallest coin there, which must never pay a fee)."""
    from arcade import doginals
    home, decoy_home = dg.call("getnewaddress"), dg.call("getnewaddress")
    # Under the wallet's own send floor (0.01 DOGE), over relay's hard dust line (0.001): built raw, as an inscriber does.
    src = max(dg.call("listunspent", 1), key=lambda u: u["amount"])
    rest = round(src["amount"] * COIN) - 2 * postage - COIN // 50
    raw = dg.call("createrawtransaction", [{"txid": src["txid"], "vout": src["vout"]}],
                  {home: postage / COIN, decoy_home: postage / COIN, dg.call("getrawchangeaddress"): rest / COIN})
    txid = dg.call("sendrawtransaction", dg.call("signrawtransaction", raw)["hex"])
    outs = dg.call("getrawtransaction", txid, 1)["vout"]
    vout = next(o["n"] for o in outs if home in o["scriptPubKey"].get("addresses", []))
    decoy = next(o["n"] for o in outs if decoy_home in o["scriptPubKey"].get("addresses", []))

    def fake(t):
        found = [{"vout": vout, "inscription_id": f"{txid}i0", "sat_offset": 0},
                 {"vout": decoy, "inscription_id": f"{txid}i1", "sat_offset": 0}] if t == txid else []
        return {"txid": t, "inscriptions": found}
    for n, t in enumerate(traders):
        t._doginals = doginals.Doginals(tmp_path / f"dg-{n}.sqlite", fetch=fake)
    _mine(dg)
    return txid, vout, decoy


def test_two_nodes_trade_a_doginal_for_pep(market, tmp_path):
    """[Claude 2026-10-15] the operator: "use the atomic swap feature we have to trade doginals." B gives a Doginal on
    Dogecoin regtest for A's 3 PEP, through the same locks and steps as any leg; the Doginals source is faked (never a real one).
    The Doginal goes into B's lock first and whole (output 0, its reserve output 1) and comes out to A first and whole,
    the reserve paying the claim."""
    from arcade import htlc as H
    from arcade.node_trader import _spender
    a, b, pep, dg = market
    postage = 100_000                                              # 0.001 DOGE, as Doginals usually sit
    txid, vout, decoy = _doginals_on_doge(dg, tmp_path, (a, b), postage)
    ins = f"{txid}i0"
    b.stand("order-doginal", {"network": "doge", "kind": "doginal", "asset": ins, "amount": 1},
            {"network": "regtest", "kind": "coin", "amount": 3 * COIN})
    give = a.leg("regtest", 3 * COIN, b.key.pubkey)
    get = T.Leg("doge", "doginal", ins, 1, b.key.pubkey.hex(), a.key.pubkey.hex())
    trade = a.propose(b.state.identity.public_bytes, "order-doginal", give, get)
    log = []
    for _ in range(40):
        for t in (a, b):
            _scan(t)
            log.extend((("a" if t is a else "b"),) + r for r in t.tick())
        _mine(pep, dg)
        if a.book.get(trade.id).state == "done" and (b.book.get(trade.id) or trade).state == "done":
            break
    assert a.book.get(trade.id).state == "done", log[-6:]
    assert b.book.get(trade.id).state == "done", log[-6:]
    with b._db() as conn:
        lock_txid = conn.execute("SELECT my_lock FROM link WHERE id=?", (trade.id,)).fetchone()[0].split(":")[0]
    lock = dg.call("getrawtransaction", lock_txid, 1)
    assert (lock["vin"][0]["txid"], lock["vin"][0]["vout"]) == (txid, vout), "the Doginal's coin is the lock's input 0"
    assert round(lock["vout"][0]["value"] * COIN) == postage and round(lock["vout"][1]["value"] * COIN) == H.DOGINAL_RESERVE
    assert (txid, decoy) not in [(v["txid"], v["vout"]) for v in lock["vin"]], "no other Doginal paid the lock's fee"
    claim = _spender(dg, {"txid": lock_txid, "vout": 0}, {})
    assert [(v["txid"], v["vout"]) for v in claim["vin"]] == [(lock_txid, 0), (lock_txid, 1)]
    assert len(claim["vout"]) == 1 and round(claim["vout"][0]["value"] * COIN) == postage, \
        "the Doginal's coin came out whole, the inscription at offset 0 of output 0"
    assert claim["vout"][0]["scriptPubKey"]["addresses"] == [a._address("doge")]


def test_a_locked_doginal_refunds_first_and_whole_after_the_lock_time(market, tmp_path):
    """The refund branch on a real Dogecoin node: the Doginal's lock spent by its refunder, lock:0 and lock:1 in, the
    Doginal's coin whole out as output 0, the reserve the fee -- refused before the lock time, accepted after it."""
    from arcade import htlc as H
    from arcade.rpc import RpcError
    a, b, pep, dg = market
    postage = 100_000
    _, h = H.make_secret()
    back = dg.call("getnewaddress")
    for when, locktime in (("before", int(time.time()) + 7200), ("after", int(time.time()) - 7200)):
        (tmp_path / when).mkdir()
        txid, vout, decoy = _doginals_on_doge(dg, tmp_path / when, (b,), postage)
        leg = T.Leg("doge", "doginal", f"{txid}i0", 1, b.key.pubkey.hex(), a.key.pubkey.hex())
        redeem = H.redeem_script(h, a.key.pubkey, b.key.pubkey, locktime)
        lock_txid = b._lock_doginal(leg, redeem, dg)
        lock = dg.call("getrawtransaction", lock_txid, 1)
        assert (lock["vin"][0]["txid"], lock["vin"][0]["vout"]) == (txid, vout)
        assert (txid, decoy) not in [(v["txid"], v["vout"]) for v in lock["vin"]]
        _mine(dg)
        coin = {"txid": lock_txid, "vout": 0, "value": postage}
        reserve = {"txid": lock_txid, "vout": 1, "value": H.DOGINAL_RESERVE}
        refund = H.refunded(*H.doginal_out(coin, reserve, back), redeem, b.key.sign)
        if when == "before":
            with pytest.raises(RpcError):
                dg.call("sendrawtransaction", refund)
            continue
        spent = dg.call("getrawtransaction", dg.call("sendrawtransaction", refund), 1)
        assert [(v["txid"], v["vout"]) for v in spent["vin"]] == [(lock_txid, 0), (lock_txid, 1)]
        assert len(spent["vout"]) == 1 and round(spent["vout"][0]["value"] * COIN) == postage
        assert spent["vout"][0]["scriptPubKey"]["addresses"] == [back]

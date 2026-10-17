"""Trade steps over the PEP testnet Messenger (arcade/trade_messages.py, trade_courier.py).

2026-10-13: "Can't we use the Pepe coin test net node to node communication
protocol? That that's already built in." The format first -- every step checked on the
way out and on the way in -- then two real nodes on one regtest chain: one says hello to
the other's messaging key, the other reads it off the chain and answers.
"""

import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_htlc import Key                                              # noqa: E402

from arcade import htlc as H, trade as T, trade_messages as M          # noqa: E402

COIN = 100_000_000


def _legs():
    a, b = Key(), Key()
    give = T.Leg("test", "coin", "", 3 * COIN, a.pub.hex(), b.pub.hex())
    get = T.Leg("doge", "coin", "", 2 * COIN, b.pub.hex(), a.pub.hex())
    return give, get


def test_every_step_round_trips_and_says_only_what_it_should():
    give, get = _legs()
    _, h = H.make_secret()
    now = int(time.time())
    steps = [
        M.build("hello", "trade-0001", order="order-0001"),
        M.build("here", "trade-0001", order="order-0001"),
        M.build("propose", "trade-0001", order="order-0001", give=M.leg_json(give),
                get=M.leg_json(get), secret_hash=h.hex(), t1=now + 7200, t2=now + 3600),
        M.build("accept", "trade-0001"),
        M.build("locked", "trade-0001", network="test", txid="ab" * 32, vout=0),
        M.build("claimed", "trade-0001", network="doge", txid="cd" * 32),
        M.build("refunded", "trade-0001", network="test", txid="ef" * 32),
        M.build("cancel", "trade-0001", reason="changed my mind"),
    ]
    assert [M.parse(s)["step"] for s in steps] == list(M.STEPS)
    said = M.parse(steps[2])
    assert M.leg_from(said["give"]) == give and M.leg_from(said["get"]) == get


@pytest.mark.parametrize("bad", [
    b"not json", b'{"t":"chat"}', b'{"t":"trade","v":2,"id":"trade-0001","step":"hello","order":"order-0001"}',
    b'{"t":"trade","v":1,"id":"x","step":"hello","order":"order-0001"}',
    b'{"t":"trade","v":1,"id":"trade-0001","step":"dance"}',
    b'{"t":"trade","v":1,"id":"trade-0001","step":"locked","network":"mars","txid":"' + b"ab" * 32 + b'","vout":0}',
    b'{"t":"trade","v":1,"id":"trade-0001","step":"locked","network":"test","txid":"zz","vout":0}',
])
def test_what_is_not_a_trade_step_is_refused(bad):
    with pytest.raises(M.TradeMessageError):
        M.parse(bad)


def test_a_proposal_must_have_real_legs_and_sane_times():
    give, get = _legs()
    _, h = H.make_secret()
    now = int(time.time())
    with pytest.raises(M.TradeMessageError):     # the responder's time after the initiator's
        M.build("propose", "trade-0001", order="order-0001", give=M.leg_json(give),
                get=M.leg_json(get), secret_hash=h.hex(), t1=now + 3600, t2=now + 7200)
    with pytest.raises(M.TradeMessageError):     # nothing for nothing
        M.build("propose", "trade-0001", order="order-0001", give=M.leg_json(give),
                get=M.leg_json(get) | {"amount": 0}, secret_hash=h.hex(), t1=now + 7200, t2=now + 3600)
    other_key = Key()                            # a person may use another key on another chain
    M.build("propose", "trade-0001", order="order-0001", give=M.leg_json(give),
            get=M.leg_json(get) | {"taker_key": other_key.pub.hex()}, secret_hash=h.hex(),
            t1=now + 7200, t2=now + 3600)


def test_a_responder_agrees_only_to_exactly_its_own_terms():
    give, get = _legs()
    _, h = H.make_secret()
    now = int(time.time())
    said = M.parse(M.build("propose", "trade-0001", order="order-0001", give=M.leg_json(give),
                           get=M.leg_json(get), secret_hash=h.hex(), t1=now + 7200, t2=now + 3600))
    mine = T.Trade("trade-0001", T.RESPONDER, give=get, get=give, secret_hash=h.hex(),
                   initiator_locktime=now + 7200, responder_locktime=now + 3600)
    assert M.same_terms(said, mine)
    cheaper = T.Trade("trade-0001", T.RESPONDER, give=get,
                      get=T.Leg(give.network, "coin", "", give.amount - 1, give.giver_key, give.taker_key),
                      secret_hash=h.hex(), initiator_locktime=now + 7200, responder_locktime=now + 3600)
    assert not M.same_terms(said, cheaper)


# --- two nodes, one chain ----------------------------------------------------------------

@pytest.fixture
def two_nodes(tmp_path, regtest):
    """Two arcade nodes -- two homes, two messaging identities -- on one regtest chain."""
    from arcade.messaging.keys import Identity
    from arcade.web.state import AppState, ChainContext

    class Pointed(ChainContext):
        def credentials(self):
            return regtest.rpc._creds

        @property
        def params(self):
            return regtest.params

    nodes = []
    for name in ("a", "b"):
        chain = Pointed(network="regtest", role="messaging", label="Testnet", datadir=regtest.datadir)
        state = AppState(home=tmp_path / name, messaging=chain,
                         ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                                             datadir=pathlib.Path("/nonexistent")))
        state.identity = Identity.generate()
        nodes.append(state)
    if float(regtest.rpc.call("getbalance")) < 50:
        regtest.rpc.call("generate", 101)
    # Mined coins pay bare pay-to-pubkey, which no message may be funded from (a reader
    # could not tell who sent it): give the wallet ordinary change to send from.
    from arcade.messaging.sender import funded_address
    regtest.rpc.call("sendtoaddress", funded_address(regtest.rpc), 20)
    regtest.rpc.call("generate", 1)
    return nodes[0], nodes[1], regtest.rpc


def _read(state, rpc):
    from arcade.messaging.scanner import Scanner
    with state.messaging.rpc() as node, state.store() as store:
        Scanner(node, state.messaging.params, store, identity=state.identity).scan_mempool()


def test_two_nodes_say_hello_and_here_over_the_messenger(two_nodes):
    from arcade import trade_courier as C
    a, b, rpc = two_nodes
    C.send(a, a.messaging, b.identity.public_bytes, M.build("hello", "trade-0001", order="order-0001"))
    _read(b, rpc)
    got = C.inbox(b, b.messaging)
    assert [m["step"]["step"] for m in got] == ["hello"], got
    assert got[0]["sender"] == a.identity.public_bytes.hex(), "authenticated as node A"
    C.send(b, b.messaging, bytes.fromhex(got[0]["sender"]),
           M.build("here", "trade-0001", order="order-0001"))
    _read(a, rpc)
    back = C.inbox(a, a.messaging)
    assert [m["step"]["step"] for m in back] == ["here"]
    assert C.inbox(b, b.messaging, after_id=got[-1]["id"]) == [], "a cursor resumes exactly"

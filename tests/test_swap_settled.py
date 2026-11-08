"""Rule 9 of web/docs/exchange-safety.md (2026-11-08, security review): a swap acts only on a lock that cannot be
taken back. A borrowed chain waits for a confirmation, and proves it; a replaceable lock in the mempool is waited out;
The operator can make every swap wait."""
import pathlib
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_trade import _seen, _trade                                  # noqa: E402

from arcade import node_trader as NT, trade as T                     # noqa: E402
from arcade.mesh import coreshare                                     # noqa: E402

COIN = 100_000_000


class _State:
    def __init__(self, settings=None, chains=()):
        self.settings, self.all_chains = dict(settings or {}), list(chains)

    def setting(self, key, default=None):
        return self.settings.get(key, default)


def _small(net):
    a, b = "02" + "11" * 32, "02" + "22" * 32
    return T.Leg(net, "coin", "", COIN, a, b), T.Leg("doge", "coin", "", COIN, b, a)


def test_a_borrowed_chain_waits_for_a_confirmation_even_for_a_small_swap(monkeypatch):
    monkeypatch.setattr(NT, "borrowed", lambda chain: chain.network == "ltc")
    state = _State(chains=[SimpleNamespace(network="ltc"), SimpleNamespace(network="doge")])
    give, get = _small("ltc")
    assert T.confirmations_for(give, get, NT.swap_big(state)) == 0, "the operator's line: zero below it"
    lines = NT.swap_lines(state)
    assert lines["ltc"] == 1 and lines["doge"] == T.BIG["doge"], "only the borrowed chain's line moves"
    assert T.confirmations_for(give, get, lines) == 1
    assert NT.swap_big(state)["ltc"] == T.BIG["ltc"], "Admin's own lines are untouched"


def test_the_operator_can_make_every_swap_wait():
    state = _State({NT.CONFIRM_ALL: True}, chains=[SimpleNamespace(network="doge", borrowed=False)])
    give, get = _small("doge")
    assert set(NT.swap_lines(state).values()) == {1}
    assert T.confirmations_for(give, get, NT.swap_lines(state)) == 1


def test_a_replaceable_lock_in_the_mempool_is_waited_out():
    now = int(time.time())
    for role in (T.INITIATOR, T.RESPONDER):
        tr, *_ = _trade(role)
        mine = _seen(tr, False)
        theirs = _seen(tr, True, conf=0)
        acted = T.next_step(tr, T.Facts(mine=mine, theirs=theirs), now)[0]
        assert acted in (T.CLAIM, T.LOCK, T.WAIT)
        theirs.replaceable = True
        assert T.next_step(tr, T.Facts(mine=mine, theirs=theirs), now)[0] == T.WAIT, role
        theirs.confirmations = 1
        assert T.next_step(tr, T.Facts(mine=mine, theirs=theirs), now)[0] == acted, "confirmed: as before"


class _Rpc:
    def __init__(self, sequence, confirmations=0):
        self.tx = {"vout": [{"value": 1.0, "scriptPubKey": {"hex": "a914" + "00" * 20 + "87"}}],
                   "vin": [{"txid": "cd" * 32, "vout": 0, "sequence": sequence}], "confirmations": confirmations}

    def call(self, method, *params):
        if method == "getrawtransaction":
            return self.tx
        if method == "gettxout":
            return {"value": 1.0}
        raise AssertionError(method)


def test_observe_says_whether_a_lock_in_the_mempool_can_be_replaced():
    lock = {"txid": "ab" * 32, "vout": 0}
    assert NT.observe(_Rpc(0xFFFFFFFD), lock, set()).replaceable is True
    assert NT.observe(_Rpc(0xFFFFFFFE), lock, set()).replaceable is False
    assert NT.observe(_Rpc(0xFFFFFFFF), lock, set()).replaceable is False
    assert NT.observe(_Rpc(0xFFFFFFFD, confirmations=1), lock, set()).replaceable is False, "confirmed: final"


class _Lent:
    net = "ltc"
    _params = None

    def __init__(self, txids):
        self.txids = txids

    def call(self, method, *params):
        assert method == "getblock"
        return {"tx": list(self.txids)}


def test_a_borrowed_confirmation_is_proved_and_agreed(monkeypatch):
    lock = "ab" * 32
    monkeypatch.setattr(coreshare, "lenders", lambda net: [])
    assert NT.proved_depth(_Lent([lock]), lock, "bb" * 32, 4) == 4, "in the block the lender named"
    assert NT.proved_depth(_Lent(["cd" * 32]), lock, "bb" * 32, 4) == 0, "not in it: unconfirmed"
    assert NT.proved_depth(_Lent([lock]), lock, "", 4) == 0, "no block named: unconfirmed"

    said = {"one": 6, "two": 2}

    class Lender:
        def __init__(self, peer):
            self.peer = peer

        def call(self, method, *params):
            if self.peer not in said:
                raise coreshare.RpcTransportError("no such block")
            return {"confirmations": said[self.peer]}
    monkeypatch.setattr(coreshare, "lenders", lambda net: [("one", 1), ("two", 1)])
    monkeypatch.setattr(coreshare, "lent_rpc", lambda peer, params: Lender(peer))
    assert NT.proved_depth(_Lent([lock]), lock, "bb" * 32, 6) == 2, "the lowest count of the lenders asked"
    monkeypatch.setattr(coreshare, "lenders", lambda net: [("one", 1), ("three", 1)])
    assert NT.proved_depth(_Lent([lock]), lock, "bb" * 32, 6) == 0, "a lender that does not know the block"


def test_the_safety_page_names_rule_9():
    page = (pathlib.Path(__file__).resolve().parent.parent / "arcade/web/docs/exchange-safety.md").read_text()
    assert "## Rule 9: a swap acts only on a lock that cannot be taken back" in page
    assert "test_swap_settled.py" in page

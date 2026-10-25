"""One definition of the ord chains (arcade/ord_chains.py, 2026-10-25): which chains' ords are read, which are sent,
listed and bought here, and which may be swapped through lock scripts -- asked by every route instead of a
("doge", "main") written by hand in each."""

import pytest

from arcade import ord_chains, ord_inscribe, trade_messages, trade_orders
from arcade.config import NETWORKS

INS = "ab" * 32 + "i0"


def test_the_words_are_the_creator_tabs_words():
    for net, rules in ord_inscribe.RULES.items():
        assert ord_chains.word(net) == rules.label
    assert [ord_chains.tab(n) for n in ("doge", "main")] == ["doginals", "pepinals"], "the old tab URLs stay"
    assert ord_chains.chain_name("doge") == "Dogecoin" and ord_chains.chain_name("main") == "Pepecoin"
    assert ord_chains.said(("doge", "main"), verb="live on") == "Doginals live on Dogecoin and Pepinals on Pepecoin main"


def test_no_lock_is_offered_where_none_can_be_made():
    for net in ("doge", "main"):
        assert ord_chains.swappable(net), net
    # Wojakcoin never enforces CHECKLOCKTIMEVERIFY; Bellscoin's P2SH byte is Dogecoin's "D" (htlc.address_of refuses).
    assert not NETWORKS["wjk"].cltv_enforced and not ord_chains.swappable("wjk")
    assert not ord_chains.swappable("bel") and not ord_chains.swappable("kot") and not ord_chains.swappable("nope")


def _leg(network):
    return {"network": network, "kind": "doginal", "asset": INS, "amount": 1, "giver_key": "02" + "11" * 32,
            "taker_key": "03" + "22" * 32}


@pytest.mark.parametrize("net", ["doge", "main", "lky", "ltc", "ltctest", "test"])
def test_a_doginal_leg_is_read_on_each_swappable_chain(net):
    assert trade_messages.leg_from(_leg(net)).network == net
    assert trade_orders.one_chain_offer({"network": net, "kind": "doginal"}, {"network": net, "kind": "coin"})


@pytest.mark.parametrize("net", ["wjk", "bel", "kot"])
def test_a_doginal_leg_is_refused_where_it_cannot_be_locked(net):
    with pytest.raises(trade_messages.TradeMessageError, match="Dogecoin"):
        trade_messages.leg_from(_leg(net))
    with pytest.raises(trade_orders.OrderError, match="Dogecoin"):
        trade_orders._side({"network": net, "kind": "doginal", "asset": INS, "amount": 1})
    assert not trade_orders.one_chain_offer({"network": net, "kind": "doginal"}, {"network": net, "kind": "coin"})

"""Sends are one-at-a-time per wallet, not one-at-a-time per node.

`begin_send` used to be a single lock, which was right when the node had one
wallet and started blocking the wrong things the moment it had more than one
(docs/multi-user.md §3). These are the properties the lanes have to keep: a
wallet never races itself, a stranger never waits for it, and a shutdown still
waits for everything.
"""

from pathlib import Path

from arcade.web.state import AppState, ChainContext, NODE_LANE


def make_state(tmp_path):
    """An AppState with no real node behind it, same as test_state.py builds."""
    return AppState(
        home=tmp_path,
        messaging=ChainContext(network="regtest", role="messaging",
                               label="Testnet", datadir=Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                            datadir=Path("/nonexistent")))


def test_one_wallet_cannot_send_twice_at_once(tmp_path):
    state = make_state(tmp_path)
    assert state.begin_send() is True
    assert state.begin_send() is False, "two sends from one wallet must not run"
    state.end_send()
    assert state.begin_send() is True, "the wallet is free again"
    state.end_send()


def test_two_wallets_send_together(tmp_path):
    """The point of the whole change: these two have no coin in common.

    An account's inscription run and the node's own post used to serialize
    behind one lock, which bought nothing and made a stranger wait for
    somebody else's forty-transaction chain.
    """
    state = make_state(tmp_path)
    mine = "account 02aaaa"
    assert state.begin_send() is True              # the node's wallet
    assert state.begin_send(mine) is True          # an account's, at the same time
    assert state.begin_send(mine) is False         # but not twice itself
    assert state.begin_send() is False
    state.end_send()
    state.end_send(mine)


def test_releasing_one_lane_does_not_free_another(tmp_path):
    """`end_send` with no lane is the node's, and only the node's.

    A release that could hand over somebody else's lane would let an account
    spend from a coin the node still believes it is spending.
    """
    state = make_state(tmp_path)
    mine = "account 02bbbb"
    assert state.begin_send(mine) is True
    state.end_send()                                # the node never held it
    assert state.begin_send(mine) is False, "that lane is still claimed"
    state.end_send(mine)
    assert state.begin_send(mine) is True
    state.end_send(mine)


def test_a_page_can_say_who_is_busy(tmp_path):
    """The refusal is worth less if the page cannot name what it waits for."""
    state = make_state(tmp_path)
    assert state.sends.held() == []
    state.begin_send()
    state.begin_send("account 02cccc")
    assert sorted(state.sends.held()) == sorted([NODE_LANE, "account 02cccc"])
    state.end_send("account 02cccc")
    assert state.sends.held() == [NODE_LANE]
    state.end_send()
    assert state.sends.held() == []


def test_shutdown_waits_for_an_account_not_just_the_node(tmp_path):
    """A graceful restart that missed an account's send is the old bug.

    BOXA caught a send thread writing to the store after a restart had let go
    of the port, so two processes shared a database for a few seconds. Lanes
    make that easy to reintroduce -- wait for the node's, ignore the rest.
    """
    state = make_state(tmp_path)
    assert state.begin_send("account 02dddd") is True
    assert state.begin_shutdown(grace=0.2) is False, "an account is still sending"
    state.end_send("account 02dddd")
    assert state.begin_shutdown(grace=0.2) is True
    assert state.begin_send("account 02dddd") is False, "shutting down"


def test_a_second_person_is_not_somebody_elses_double_click(tmp_path):
    """The double-click guard is per wallet, and this is why it had to be.

    The digest covers the peer and the bytes and never the sender, so with one
    shared record a second account writing the same words to the same friend
    inside the window would be told their message was already sent. It was not.
    """
    state = make_state(tmp_path)
    digest = "d" * 64
    state.note_send(digest)
    assert state.is_repeat_send(digest) is True
    assert state.is_repeat_send(digest, "account 02eeee") is False, \
        "a different wallet sending the same text is a different person"
    state.note_send(digest, "account 02eeee")
    assert state.is_repeat_send(digest, "account 02eeee") is True
    assert state.is_repeat_send("e" * 64, "account 02eeee") is False

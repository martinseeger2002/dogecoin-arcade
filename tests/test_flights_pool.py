"""A coin the node's mempool already spends is never offered again.

Live, 2026-09-25: an account whose post was stuck in the mempool (dust change
nobody relayed) could not post at all. This process had forgotten that post --
in-flight records last IN_FLIGHT_SECONDS and do not survive a restart -- so the
builder picked the same coin every time and the node answered
"txn-mempool-conflict". The flights now also ask the node.
"""

from arcade.web.account import Flights

STUCK = ("ab" * 32, 4)


def test_the_mempool_s_spends_are_excluded_even_when_nothing_was_remembered():
    flights = Flights(pool=lambda network: frozenset({STUCK}))
    assert STUCK in flights.spent_by("aa" * 32, "test")


def test_a_node_that_cannot_be_asked_leaves_the_remembered_spends():
    def away(network):
        raise ConnectionError("node away")
    flights = Flights(pool=away)
    assert flights.spent_by("aa" * 32, "test") == frozenset()


def test_without_a_pool_nothing_changes():
    assert Flights().spent_by("aa" * 32, "test") == frozenset()

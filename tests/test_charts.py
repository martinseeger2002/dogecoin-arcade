

def test_a_trade_ahead_of_the_clock_is_still_on_the_chart():
    """A block may be timed ahead of this machine's clock (2026-10-14): its trade is drawn, not dropped off the right edge."""
    from arcade import charts
    now = 1_790_000_000
    slots = charts.candles([{"when": now + 3 * 3600, "price": 2.0, "size": 4.0}], buckets=30, span=3600, now=now)
    assert sum(s["count"] for s in slots) == 1

"""Prices, drawn from what actually traded.

There is no order book here (D-037), so there is no book to draw. What there
is is every swap the chain has read: two legs and a block time. A price is
one leg divided by the other, and a candle is the open, high, low and close
of the prices inside one span of time.

The gaps matter as much as the trades. A day with no trade is drawn as a day
with no trade -- an empty slot, not a line ruled to the next one -- because a
chart that interpolates is telling you about a price nobody paid (D-039).
"""

from __future__ import annotations

import time
from typing import Any, Iterable

from . import inscriptions as I
from .ledger import COIN

#: How many candles a chart has, and how long each one is. A month of days,
#: which is what a chain this quiet can fill without the chart being mostly
#: empty; a busier chain would want an hour a candle and the same code.
DEFAULT_BUCKETS = 30
DAY = 86_400


def _units(leg: Any) -> float:
    """A leg as a number, in whole coins or whole tokens."""
    return (leg.amount or 0) / COIN


def token_prices(trades: Iterable[dict], property_id: int) -> list[dict]:
    """Every trade of this token against coins, as coins per token.

    A token swapped for another token has no price in coins and is left out
    rather than guessed at.
    """
    out = []
    for trade in trades:
        legs = (trade["give"], trade["take"])
        token = next((l for l in legs if l.kind == I.LEG_TOKEN
                      and l.property_id == property_id), None)
        coins = next((l for l in legs if l.kind == I.LEG_COINS), None)
        if token is None or coins is None or not token.amount:
            continue
        out.append({"when": trade["when"], "height": trade["height"],
                    "price": _units(coins) / _units(token),
                    "size": _units(token), "txid": trade["txid"]})
    return out


#: Passed as `collection` to mean "the pieces that belong to no collection",
#: which None cannot mean because None already means "every piece".
STANDALONE = object()


def nft_prices(trades: Iterable[dict], index: Any,
               collection: Any = None,
               property_id: int | None = None) -> list[dict]:
    """Every NFT sale, as what one piece went for in ONE currency.

    One currency, because putting coins and tokens on one axis is adding
    pounds to metres. `property_id` prices in that token; without it, in
    coins. Filtered to one collection when asked, which is what a
    collection's own chart is.
    """
    out = []
    for trade in trades:
        legs = (trade["give"], trade["take"])
        piece = next((l for l in legs if l.kind == I.LEG_INSCRIPTION), None)
        if piece is None:
            continue
        if property_id is None:
            paid = next((l for l in legs if l.kind == I.LEG_COINS), None)
        else:
            paid = next((l for l in legs if l.kind == I.LEG_TOKEN
                         and l.property_id == property_id), None)
        if paid is None:
            continue
        if collection is not None:
            try:
                row = index.inscription(piece.txid.hex()) if index else None
            except Exception:
                row = None
            has = (row or {}).get("collection") or None
            if collection is STANDALONE:
                if has is not None:
                    continue
            elif has != collection:
                continue
        out.append({"when": trade["when"], "height": trade["height"],
                    "price": _units(paid), "size": 1, "txid": trade["txid"]})
    return out


def nft_markets(trades: Iterable[dict], index: Any) -> list[dict]:
    """One market per (collection, currency) that has actually traded.

    A collection is a market: what a Goofball goes for says nothing about
    what a Doge Punk goes for, and one chart of both is a chart of neither.
    Pieces that belong to no collection are a market of their own -- they
    are single things, and that is what they have in common (D-040).
    """
    seen: dict[tuple[str | None, str, int | None], int] = {}
    for trade in trades:
        legs = (trade["give"], trade["take"])
        piece = next((l for l in legs if l.kind == I.LEG_INSCRIPTION), None)
        if piece is None:
            continue
        collection = None
        try:
            row = index.inscription(piece.txid.hex()) if index else None
            collection = (row or {}).get("collection") or None
        except Exception:
            collection = None
        for leg in legs:
            if leg.kind == I.LEG_COINS:
                key = (collection, "coins", None)
            elif leg.kind == I.LEG_TOKEN:
                key = (collection, "token", leg.property_id)
            else:
                continue
            seen[key] = seen.get(key, 0) + 1
    return [{"collection": collection, "kind": kind, "property_id": pid,
             "trades": n}
            for (collection, kind, pid), n in sorted(seen.items(),
                                                     key=lambda kv: -kv[1])]


def nft_currencies(trades: Iterable[dict]) -> list[dict]:
    """What NFTs have been paid for with, most traded first.

    A chain where every piece sold for one token should chart that token
    rather than draw an empty coins chart and call it the market.
    """
    counts: dict[tuple[str, int | None], int] = {}
    for trade in trades:
        legs = (trade["give"], trade["take"])
        if not any(l.kind == I.LEG_INSCRIPTION for l in legs):
            continue
        for leg in legs:
            if leg.kind == I.LEG_COINS:
                counts[("coins", None)] = counts.get(("coins", None), 0) + 1
            elif leg.kind == I.LEG_TOKEN:
                key = ("token", leg.property_id)
                counts[key] = counts.get(key, 0) + 1
    return [{"kind": kind, "property_id": pid, "trades": n}
            for (kind, pid), n in sorted(counts.items(), key=lambda kv: -kv[1])]


def candles(points: Iterable[dict], buckets: int = DEFAULT_BUCKETS,
            span: int = DAY, now: float | None = None) -> list[dict]:
    """Open, high, low, close and volume per span, oldest first.

    Every bucket is returned, traded or not. An empty one carries `count` 0
    and no prices at all -- the drawing decides how to say "nothing here",
    and cannot accidentally say "the price was flat".
    """
    now = time.time() if now is None else now
    end = (int(now) // span + 1) * span
    start = end - buckets * span
    slots: list[dict] = [{"start": start + n * span, "span": span,
                          "open": None, "high": None, "low": None,
                          "close": None, "volume": 0.0, "count": 0}
                         for n in range(buckets)]
    for point in sorted(points, key=lambda p: (p["when"], p.get("height", 0))):
        when = int(point["when"] or 0)
        if when < start or when >= end:
            continue
        slot = slots[min(buckets - 1, (when - start) // span)]
        price = float(point["price"])
        if slot["count"] == 0:
            slot.update(open=price, high=price, low=price)
        slot["high"] = max(slot["high"], price)
        slot["low"] = min(slot["low"], price)
        slot["close"] = price
        slot["volume"] += float(point.get("size") or 0)
        slot["count"] += 1
    return slots


def last_and_change(points: list[dict], now: float | None = None,
                    window: int = DAY) -> dict[str, Any]:
    """The last price, and how far it has moved in a day.

    The comparison is the last trade BEFORE the window, not the first inside
    it: a pair that traded once yesterday and once today has moved, and a
    pair whose only trade is inside the window has not moved at all -- it has
    a price and nothing to compare it with (D-048).
    """
    import time as _time

    if not points:
        return {"last": None, "change": None, "trades": 0, "volume": 0.0}
    now = _time.time() if now is None else now
    ordered = sorted(points, key=lambda p: (p["when"], p.get("height", 0)))
    inside = [p for p in ordered if p["when"] >= now - window]
    before = [p for p in ordered if p["when"] < now - window]
    last = ordered[-1]["price"]
    was = before[-1]["price"] if before else (inside[0]["price"] if inside else None)
    change = None if not was or not before else (last - was) / was * 100
    return {"last": last, "change": change, "trades": len(inside),
            "volume": sum(float(p.get("size") or 0) for p in inside)}


def day(points: list[dict], now: float | None = None,
        window: int = DAY) -> dict[str, Any]:
    """The numbers a market table quotes: last, move, high, low, what traded.

    `last_and_change` decides the first two and is left to (D-048); this adds
    the day's range and its two volumes. `volume` is in the thing being sold
    -- tokens, or pieces -- and `coins` is what was paid for them, because a
    market is read in both and one cannot be worked out from the other when
    the price moved.
    """
    stats = dict(last=None, change=None, trades=0, volume=0.0,
                 high=None, low=None, coins=0.0)
    stats.update(last_and_change(points, now=now, window=window))
    import time as _time

    now = _time.time() if now is None else now
    inside = [p for p in points if p["when"] >= now - window]
    if inside:
        prices = [p["price"] for p in inside]
        stats["high"], stats["low"] = max(prices), min(prices)
        stats["coins"] = sum(float(p.get("size") or 0) * p["price"] for p in inside)
    return stats


def summary(points: list[dict]) -> dict[str, Any]:
    """What the chart says in words: last, and the move over its span."""
    if not points:
        return {"last": None, "first": None, "change": None, "trades": 0}
    ordered = sorted(points, key=lambda p: (p["when"], p.get("height", 0)))
    first, last = ordered[0]["price"], ordered[-1]["price"]
    change = None if not first else (last - first) / first * 100
    return {"last": last, "first": first, "change": change,
            "trades": len(ordered),
            "volume": sum(float(p.get("size") or 0) for p in ordered)}

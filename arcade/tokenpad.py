"""The page a token sells itself from.

A token launchpad is the mintpad's idea (mintpad.py) with the wall taken out:
an inscription whose JSON names a shop offering a lot of one token for a
price, and a page that is its counter. The buying half, the seller's
shopkeeper and the buyer's approval are all the code that already exists --
what differs is only what the shop gives.

Why a page at all, when an ask does for an NFT: a token sale is not one
thing, it is a thousand identical things, and what a buyer wants is a shop
that will sell them a lot at a fixed price for as long as the issuer holds
any. The order book is the other half of that -- a book is where a price is
discovered, a launchpad is where a supply is distributed -- and a new token
has nobody on the other side of its book yet (D-107).

The page is inscribed from the address that holds the tokens, because a
shop's seller is the shop inscription's own owner and every buyer's node
checks that that address still holds what it sells.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .mintpad import MintpadError, take_of          # noqa: F401  (re-exported)

#: The counter, with %%PLACEHOLDERS%%. Package data, like the mintpad's.
TEMPLATE = Path(__file__).parent / "web" / "templates" / "tokenpad.html"


def page(name: str, lot: str, price: str, icon: str = "", about: str = "") -> bytes:
    """The launchpad for one token, as the bytes that go on the chain."""
    name = (name or "").strip()
    if not name:
        raise MintpadError("a launchpad needs the token's name")
    text = TEMPLATE.read_text(encoding="utf-8")
    for mark, value in (("%%TOKEN%%", _escape(name)),
                        ("%%LOT%%", _escape(lot or "")),
                        ("%%PRICE%%", _escape(price or "")),
                        ("%%ABOUT%%", _escape(about or "")),
                        ("%%ICON%%", _escape(icon or ""))):
        text = text.replace(mark, value)
    if "%%" in text:
        raise MintpadError("the launchpad template has a placeholder left in it")
    return text.encode("utf-8")


def _escape(value: str) -> str:
    """Whatever somebody called their token is about to go into HTML."""
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def shop_json(node: str, property_id: int, name: str, lot: str,
              take: dict[str, Any]) -> str:
    """The inscription's JSON field: the name, and the shop beside it."""
    if not (node or "").strip():
        raise MintpadError("a launchpad needs this node's contact code, so a "
                           "buyer has somewhere to ask")
    if not str(lot or "").strip():
        raise MintpadError("say how many go in one sale")
    return json.dumps({
        "name": f"{(name or '').strip()} Launchpad",
        "shop": {"node": node.strip(),
                 "listings": [{"give": {"token": int(property_id),
                                        "amount": str(lot).strip()},
                               "take": take}]},
    }, separators=(",", ":"))

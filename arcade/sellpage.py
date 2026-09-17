"""One NFT, put up for sale by an inscription.

A listing is the same machinery a mintpad is (`mintpad.py`): an inscription
whose JSON names a shop (`swap.py`), and this page is its counter. The
difference is what the shop gives -- a mintpad gives a random member of a
collection, a listing gives one named piece -- so the buying half, the
seller's shopkeeper and the approval the buyer sees are all the code that
already exists.

Why an inscription rather than a row in this wallet: a price kept here would
be a price only this machine knows. A listing has to be readable by whoever
is looking at the piece, on their node, without asking anybody -- which is
what "a shop is an inscription, and it closes by being sent away" already
means for a mintpad (D-037). It costs a fee, which is the honest price of
saying something to everybody.

Two rules the page and the JSON have to agree on, or the listing is a lie:

* it is inscribed FROM the address that holds the piece, because a shop's
  seller is the shop inscription's own owner, and a buyer's node checks that
  that address still holds what is being sold (`swap.holds`);
* the give is the piece's txid, so the page shows what would change hands
  rather than a picture somebody chose.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .mintpad import MintpadError, take_of          # noqa: F401  (re-exported)

#: The counter, with %%PLACEHOLDERS%%. Package data, so an installed release
#: has it (pyproject: arcade.web package-data), same as the mintpad's.
TEMPLATE = Path(__file__).parent / "web" / "templates" / "sellpage.html"


class SaleError(Exception):
    """This piece cannot be listed as asked."""


def _txid(txid: str) -> str:
    txid = (txid or "").strip().lower()
    if len(txid) != 64 or any(c not in "0123456789abcdef" for c in txid):
        raise SaleError("a listing names the piece by its inscription txid")
    return txid


def name_of(row: dict[str, Any]) -> str:
    """What to call a piece: what its own JSON calls it, else its number."""
    try:
        data = json.loads(row.get("json") or "")
        name = data.get("name") if isinstance(data, dict) else None
    except (TypeError, ValueError):
        name = None
    if name:
        return str(name)
    number = row.get("number")
    return f"Inscription #{number}" if number is not None else "An inscription"


def page(txid: str, name: str, price: str) -> bytes:
    """The sale page for one piece, as the bytes that go on the chain.

    `price` is only what the page says before a wallet has read the chain:
    the live price comes from the shop's own JSON through `/r/swap.js`, so a
    page whose text and whose listing disagreed would show the listing.
    """
    text = TEMPLATE.read_text(encoding="utf-8")
    for mark, value in (("%%PIECE%%", _txid(txid)),
                        ("%%NAME%%", _escape(name or "This inscription")),
                        ("%%PRICE%%", _escape(price or ""))):
        text = text.replace(mark, value)
    if "%%" in text:
        raise SaleError("the sale page template has a placeholder left in it")
    return text.encode("utf-8")


def _escape(value: str) -> str:
    """A name goes into HTML, and a name is whatever somebody inscribed."""
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def sale_json(node: str, txid: str, take: dict[str, Any], name: str) -> str:
    """The inscription's JSON field: what it is called, and the shop beside it."""
    if not (node or "").strip():
        raise SaleError("a listing needs this node's contact code, so a buyer "
                        "has somewhere to ask")
    return json.dumps({
        "name": f"{name} for sale",
        "shop": {"node": node.strip(),
                 "listings": [{"give": {"inscription": _txid(txid)},
                               "take": take}]},
    }, separators=(",", ":"))

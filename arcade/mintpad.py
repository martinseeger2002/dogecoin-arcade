"""The page a collection sells itself from.

A mintpad is an inscription like any other: an HTML page whose JSON names a
shop (`swap.py`) offering a random item of the creator's collection for a
price. The page draws the collection as a wall of tiles, spins a reel when
somebody mints, and stops on the piece that is now theirs.

It is built here rather than typed by hand because the interesting part is
not the page -- it is that the JSON beside it and the wallet behind it agree
about what is for sale. A collection inscribed through the wizard can offer
itself for sale the moment its last item is on its way, without anybody
writing a line of HTML or a line of JSON (D-036).

The page is the one the Goofball collection used, with the names taken out.
It talks to the wallet it is framed in through `/r/swap.js`; opened as a
bare `/content/` URL it says so rather than half-working.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: The page, with %%NAME%% placeholders. Package data, so an installed
#: release has it (pyproject: arcade.web package-data).
TEMPLATE = Path(__file__).parent / "web" / "templates" / "mintpad.html"


class MintpadError(Exception):
    """The mintpad cannot be built as asked."""


def plural(word: str) -> str:
    """"Goofball" -> "Goofballs". Crude on purpose.

    The alternative is a dictionary of English plurals in a wallet, to make
    one line of a page read slightly better. A collection called "Dice" gets
    "Dices left to mint" and its creator can inscribe their own page.
    """
    word = (word or "").strip()
    if not word:
        return ""
    return word if word.endswith(("s", "S")) else word + "s"


def page(creator: str, collection: str) -> bytes:
    """The mintpad for one collection, as the bytes that go on the chain."""
    name = (collection or "").strip()
    if not name:
        raise MintpadError("a mintpad needs the collection's name")
    if not (creator or "").strip():
        raise MintpadError("a mintpad needs the address that holds the collection")
    text = TEMPLATE.read_text(encoding="utf-8")
    for mark, value in (("%%CREATOR%%", creator.strip()),
                        ("%%COLLECTION%%", name),
                        ("%%TITLE%%", f"{name.upper()} MINTPAD"),
                        ("%%MANY%%", plural(name)),
                        ("%%ONE%%", name)):
        text = text.replace(mark, value)
    if "%%" in text:
        raise MintpadError("the mintpad template has a placeholder left in it")
    return text.encode("utf-8")


def take_of(kind: str, amount: str, property_id: int | None = None) -> dict[str, Any]:
    """What the pad asks for: coins, or an amount of one token.

    Checked here rather than where the form is read, so the rule is in one
    place and a pad built by anything else gets the same answer.
    """
    amount = (amount or "").strip()
    if not amount:
        raise MintpadError("say what one costs")
    try:
        if float(amount) <= 0:
            raise MintpadError("a price is more than nothing")
    except ValueError:
        raise MintpadError(f"{amount!r} is not a price") from None
    if kind == "coins":
        return {"coins": amount}
    if kind == "token":
        if not property_id:
            raise MintpadError("say which token the price is in")
        return {"token": int(property_id), "amount": amount}
    raise MintpadError("a price is in coins or in a token")


def shop_json(node: str, collection: str, take: dict[str, Any]) -> str:
    """The inscription's JSON field: the name, and the shop beside it."""
    name = (collection or "").strip()
    if not (node or "").strip():
        raise MintpadError("a mintpad needs this node's contact code")
    return json.dumps({
        "name": f"{name} Mintpad",
        "shop": {"node": node.strip(),
                 "listings": [{"give": {"collection": name, "pick": "random"},
                               "take": take}]},
    }, separators=(",", ":"))

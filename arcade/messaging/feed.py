"""What people do to each other's posts: like, reply, share, edit, delete, tip.

A post is still a `TYPE_GROUP` payload (`group.py`) -- chunking, attachments,
scanning and storage all work already, and reusing them is what keeps the
index lean. What is new is everything that happens TO a post, and all of it
is one message type with a kind byte rather than six types with six parsers:

    magic4 version1 type1 | kind1 | target32 | text...

One shape, one parser, one table. A reader that does not know a kind ignores
that one and keeps the rest, which is what lets a kind be added later without
anybody's node stopping.

**Why on the chain at all.** A like that lives on the machine that made it is
invisible to the person who was liked, which is the entire point of a like.
So every one of these is a transaction and every node counts the same
numbers. It costs a fee, which is the honest price of that (D-138) -- and the
feed is testnet, where a fee is play money.

**The target is a txid.** A post, a comment, a share: all of them are
transactions, so one field addresses any of them. That is what makes replies
to replies free: the parent of a comment is a comment, and nothing in the
format knows the difference.

**A tip is not a message about a payment, it is the payment.** The same
transaction that moves the coins, the tokens or the piece carries this note
saying which post it was for. One fee, exact amounts, nothing to reconcile --
and because the payment is an ordinary send, a tip needs no new trust. It is
the one kind that rides on the LEDGER chains rather than the messaging one,
which is why tips work on mainnet and the rest of the feed does not.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..config import NETWORKS
from .envelope import MAGIC, VERSION, EnvelopeError, Header, TYPE_FEED_ACT

#: A thumbs-up. No text, and the only thing it can say.
LIKE = 1
#: Taking one back. Its own kind rather than a second LIKE, so a node can
#: count without keeping every like it has ever seen in memory to cancel it.
UNLIKE = 2
#: A comment, on a post or on another comment -- the target says which, and
#: nothing here has to know.
REPLY = 3
#: Somebody else's post, on your own feed, with or without a word about it.
SHARE = 4
#: New words for a post of your own. The chain keeps both; the page shows the
#: newest and says it was edited.
EDIT = 5
#: Hide a post of your own everywhere. The words stay on the chain for ever --
#: nothing can change that -- and no arcade shows them (D-138).
DELETE = 6
#: This transaction's payment was for that post.
TIP = 7

#: The kinds that carry text. Everything else is a bare target.
WITH_TEXT = (REPLY, SHARE, EDIT)

#: The kinds only the post's own author may perform.
AUTHOR_ONLY = (EDIT, DELETE)

KINDS = (LIKE, UNLIKE, REPLY, SHARE, EDIT, DELETE, TIP)

#: Kind names, for pages and for the reasons the index records.
NAMES = {LIKE: "like", UNLIKE: "unlike", REPLY: "reply", SHARE: "share",
         EDIT: "edit", DELETE: "delete", TIP: "tip"}

#: The same map the other way, for a page that has the word and needs the
#: byte. It lives here rather than in the page because a browser that sends a
#: number it typed out itself is a browser that will still send it the day a
#: kind is renamed -- and a wrong byte here is a transaction on the chain.
BY_NAME = {name: kind for kind, name in NAMES.items()}

#: How much a comment may say. Longer than a post is short of, and short
#: enough that a reply still fits one transaction with room for the framing.
MAX_TEXT = 2000

# --- what a post is worth on the feed page ------------------------------------
#
# Decided by 2026-09-23: a tip is worth more than a like; a tip's
# weight rises with the LOGARITHM of its amount, so a hundred times the money
# is worth a little over twice as much rather than a hundred times; and the
# money is normalised per chain by a CONSTANT, never by a price -- a sort
# that consults an oracle is a sort that stops working when the oracle does.
# The same function is a SQL function inside the store (store.py registers
# it), because the ordering has to BE the query, and a card's numbers and
# the page's order must not be two arithmetic that can drift apart.

#: A like, a share, and anything else that is one person saying one thing.
LIKE_VALUE = 1.0

#: Every confirmed tip is worth at least this much -- more than a like, which
#: is the rule as stated, and the reason the log alone cannot carry it.
TIP_FLOOR = 2.0

#: Sats of each chain that count as one unit, which is what the logarithm
#: measures multiples OF. One coin everywhere it runs today: what these hold
#: is the place to raise a chain whose coin makes a one-coin tip a shrug,
#: and the only alternative to a per-chain constant is a live price.
#:
#: Keyed by `Params.name`, which is the same string every feed row is
#: partitioned by and the string a tip's row writes into `paid_on` -- so the
#: chain the money came from is what selects the constant, and a chain added
#: to config is measured here the day it is measured anywhere.
TIP_UNIT_DEFAULT = 100_000_000
TIP_UNITS = {name: TIP_UNIT_DEFAULT for name in NETWORKS}


def tip_value(sats: int, network: str) -> float:
    """What one confirmed tip is worth in the feed's arithmetic.

    `network` is the chain the tip's transaction lives on -- not the chain of
    the post it paid for and not the chain the tipper happened to sign on;
    totals are held per chain because the units differ and adding them across
    chains would be a lie.
    """
    unit = TIP_UNITS.get(network, TIP_UNIT_DEFAULT)
    return TIP_FLOOR + math.log10(1.0 + max(0, int(sats)) / unit)


class FeedError(Exception):
    """This is not a feed action, or not one anybody could act on."""


@dataclass
class Act:
    """One thing done to one post."""

    kind: int
    target: bytes                    # 32 bytes: the txid it is about
    text: str = ""

    @property
    def name(self) -> str:
        return NAMES.get(self.kind, "unknown")

    @property
    def target_hex(self) -> str:
        return self.target.hex()


def build(kind: int, target: str | bytes, text: str = "") -> bytes:
    """The payload for one action. Raises rather than trimming anything."""
    if kind not in KINDS:
        raise FeedError(f"no such feed action: {kind}")
    try:
        raw = bytes.fromhex(target) if isinstance(target, str) else bytes(target)
    except ValueError:
        raise FeedError("a feed action is about a transaction, and that is "
                        "not a txid") from None
    if len(raw) != 32:
        raise FeedError("a feed action is about a transaction, which is 32 bytes")
    said = (text or "").strip()
    if said and kind not in WITH_TEXT:
        raise FeedError(f"a {NAMES[kind]} says nothing but which post it is about")
    encoded = said.encode()
    if len(encoded) > MAX_TEXT:
        # Refused rather than trimmed: truncating here would be silent,
        # permanent and paid for (the rule the announcement name follows).
        raise FeedError(
            f"that is {len(encoded)} bytes and a comment is limited to "
            f"{MAX_TEXT}")
    return Header(type=TYPE_FEED_ACT).encode() + bytes([kind]) + raw + encoded


def parse(payload: bytes) -> Act:
    """Read one, or raise. Never guesses at a kind it does not know."""
    header = Header.decode(payload)
    if header.type != TYPE_FEED_ACT:
        raise EnvelopeError("not a feed action")
    body = payload[header.length:]
    if len(body) < 33:
        raise EnvelopeError("a feed action needs a kind and a target")
    kind = body[0]
    if kind not in KINDS:
        raise EnvelopeError(f"unknown feed action {kind}")
    target = body[1:33]
    text = ""
    if kind in WITH_TEXT:
        # Class B pads with NULs and Omni does not strip them, so the tail is
        # stripped here rather than being shown as a row of nothing.
        text = body[33:].rstrip(b"\x00").decode("utf-8", "replace").strip()
    return Act(kind=kind, target=target, text=text)


def is_feed_act(payload: bytes) -> bool:
    """Whether this payload is one of ours, cheaply and without raising.

    Used by the ledger index, which sees every arcade payload on a chain and
    must be able to say "not mine" without a parser.
    """
    return (len(payload) >= 6 + 33 and payload[:4] == MAGIC
            and payload[4] == VERSION and payload[5] == TYPE_FEED_ACT)

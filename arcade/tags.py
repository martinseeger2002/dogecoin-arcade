"""@tags: one name per address, claimed on the chain and indexed by everyone.

A tag is a handle -- `@robin` -- that belongs to an address. It is claimed by
broadcasting a claim, changed by claiming another, and handed to somebody else
by sending it, exactly as an inscription is. The index decides who holds what by
replaying the chain, so every node agrees without anyone being asked to trust a
directory.

Rules, and why each one
-----------------------
* **One tag to an address, one address to a tag.** A name that points at two
  people is not a name, and an address with two names gives a reader two
  answers to the same question.
* **First claim wins, in chain order.** The only ordering every node already
  agrees on.
* **Lower case, a limited alphabet, a length.** `@robin` and `@robın` must
  not be two different people as far as a reader is concerned; the smaller the
  alphabet, the fewer pairs of names that look identical and are not.
* **Changing your tag frees the old one.** Holding names you no longer use is
  how a namespace fills up with nothing.

It rides in the same envelope as an inscription because it is the same kind of
thing: an object on the ledger, owned by an address, moved by its owner saying
so, and invisible to a client too old to know about it.
"""

from __future__ import annotations

import re

from .inscriptions import MAGIC, VERSION, InscriptionError

KIND_CLAIM = 3
KIND_TRANSFER = 4

MIN_LENGTH = 2
MAX_LENGTH = 24

#: Lower case, digits and underscore. No hyphen: it is the character most often
#: mistaken for the several dashes that are not it. No dots: they read as
#: domains and invite a hierarchy that does not exist here.
ALLOWED = re.compile(r"^[a-z0-9_]+$")

#: Names the arcade will not let anyone take, because somebody reading them
#: would reasonably believe they were official.
RESERVED = frozenset({
    "admin", "administrator", "arcade", "dogecoinarcade", "support", "help",
    "official", "system", "root", "moderator", "mod", "staff", "team",
    "pepecoin", "dogecoin", "null", "none", "anonymous", "me", "you",
})


class TagError(Exception):
    """The tag is not one anybody can have."""


def normalise(text: str) -> str:
    """The canonical form: no leading @, lower case, no surrounding space.

    Case folds because a reader does not distinguish `@the operator` from `@robin`
    and neither should the ledger.
    """
    text = (text or "").strip()
    if text.startswith("@"):
        text = text[1:]
    return text.strip().lower()


def validate(text: str) -> str:
    """Return the canonical tag, or say exactly what is wrong with it."""
    tag = normalise(text)
    if not tag:
        return ""
    if len(tag) < MIN_LENGTH:
        raise TagError(f"a tag needs at least {MIN_LENGTH} characters.")
    if len(tag) > MAX_LENGTH:
        raise TagError(f"a tag is at most {MAX_LENGTH} characters.")
    if not ALLOWED.match(tag):
        raise TagError("a tag can use a-z, 0-9 and _ only. Nothing else is "
                       "allowed, so that two tags can never look alike.")
    if tag in RESERVED:
        raise TagError(f"@{tag} is reserved: somebody reading it would take it "
                       f"for an official account.")
    return tag


def encode(tag: str, kind: int = KIND_CLAIM) -> bytes:
    """One claim or one transfer, ready to go in a transaction."""
    canonical = validate(tag)
    if not canonical:
        raise TagError("name the tag.")
    raw = canonical.encode()
    return MAGIC + bytes([VERSION, kind, len(raw)]) + raw


def parse(payload: bytes) -> tuple[int, str]:
    """(kind, tag) for a tag payload. Raises if it is not one."""
    if len(payload) < 7 or payload[:4] != MAGIC:
        raise InscriptionError("not a tag payload")
    version, kind, length = payload[4], payload[5], payload[6]
    if version != VERSION:
        raise InscriptionError(f"tag version {version} is not readable here")
    if kind not in (KIND_CLAIM, KIND_TRANSFER):
        raise InscriptionError(f"unknown tag kind {kind}")
    raw = payload[7:7 + length]
    if len(raw) != length:
        raise InscriptionError("truncated tag")
    tag = raw.decode("utf-8", "replace")
    # Validated on the way in as well as on the way out: a payload that names
    # something no one could have claimed is invalid, not merely odd.
    if normalise(tag) != tag or not ALLOWED.match(tag or "x!"):
        raise InscriptionError("that is not a well-formed tag")
    return kind, tag


def display(tag: str | None, address: str = "", short: int = 12) -> str:
    """What to show for a sender: their tag if they have one, else the address.

    Everything that names a person goes through here, so a machine with a
    stale index shows an address rather than the wrong name.
    """
    if tag:
        return f"@{tag}"
    if not address:
        return "anonymous"
    return address if len(address) <= short * 2 else f"{address[:short]}…"

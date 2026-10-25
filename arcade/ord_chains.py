"""The chains whose ord inscriptions this arcade reads, and which of them it moves (2026-10-25).

One place for what used to be written as `("doge", "main")` in a dozen routes: Doginals on Dogecoin and Pepinals on
Pepecoin mainnet were the only ords an account could send, list on the Exchange or put in a swap, and every route
said so by hand. Now each route asks here.

* `WORDS` -- every chain whose ords are read (the wallet shows them, the Creator tab inscribes them): what one is
  called, what its chain is called, and how the wallet names its coins. ord_inscribe.RULES takes its labels from it.
* `MOVED` -- the chains whose ords are SENT, LISTED and BOUGHT here (the /account/doginal* routes, the Exchange tab
  of each). Every other chain in WORDS is shown only.
* `swappable` -- the ones of those an ord may also be swapped on, through lock scripts. A lock refunds through
  CHECKLOCKTIMEVERIFY, so never on a chain that does not enforce it (Wojakcoin, config.require_cltv), and it is a
  P2SH address, so never where the script-hash byte is another chain's key-hash byte (Bellscoin's 30 is Dogecoin's
  "D", config.require_p2sh_payable): htlc.address_of refuses both, and a swap that can never lock is not offered.

Whether this NODE reads a chain's ords right now is a different question (`ready`): the chain's guard
(funding.guard_on) exists only where the node runs that chain.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import NETWORKS, unpayable_versions


@dataclass(frozen=True)
class OrdWords:
    word: str        # one inscription: "Doginal"
    chain: str       # its chain, as a page says it: "Dogecoin"
    coins: str       # the wallet's word for that chain's coins: "Reading your PEP coins..."


#: Every chain whose ords the arcade reads, in the order a page offers them.
WORDS = {
    "doge": OrdWords("Doginal", "Dogecoin", "Dogecoin"),
    "main": OrdWords("Pepinal", "Pepecoin", "PEP"),
    "bel": OrdWords("Bellinal", "Bellscoin", "Bellscoin"),
    "lky": OrdWords("Luckinal", "Luckycoin", "Luckycoin"),
    "wjk": OrdWords("Wojakinal", "Wojakcoin", "Wojakcoin"),
    # Pepecoin testnet, for the Creator tab's tries with coins that cost nothing.
    "test": OrdWords("Pepinal", "Pepecoin test", "PEP TEST"),
}

#: The chains whose ords are sent, listed and bought here: Doginals, Pepinals, and Bellinals (2026-10-25; Bells 3 signs
#: legacy inputs as Dogecoin does, so the SINGLE|ANYONECANPAY listing digest is the same). Kotia has no ords (timed
#: transactions, Params.tx_time); Luckycoin and Wojakcoin are shown only.
MOVED = ("doge", "main", "bel")


def word(net: str, default: str = "Inscription") -> str:
    """What one ord on `net` is called: "Doginal", "Pepinal", "Bellinal"."""
    got = WORDS.get(str(net))
    return got.word if got else default


def chain_name(net: str) -> str:
    """The chain, as the ord pages say it: "Dogecoin", "Pepecoin", "Bellscoin"."""
    got = WORDS.get(str(net))
    return got.chain if got else (NETWORKS[net].label if net in NETWORKS else str(net))


def coin(net: str) -> str:
    """The chain's coin: "DOGE", "PEP", "BEL"."""
    return NETWORKS[net].coin if net in NETWORKS else ""


def tab(net: str) -> str:
    """The Exchange tab its ords are listed on: "doginals", "pepinals", "bellinals"."""
    return f"{word(net).lower()}s"


def moved(net: str) -> bool:
    """Whether ords on `net` are sent, listed and bought here (MOVED)."""
    return str(net) in MOVED


def swappable(net: str) -> bool:
    """Whether an ord on `net` may be one side of a swap: moved here, and a lock script can be made there."""
    params = NETWORKS.get(str(net))
    return bool(moved(net) and params is not None and getattr(params, "cltv_enforced", True)
                and not unpayable_versions(params))


def ready(net: str) -> bool:
    """Whether THIS node reads `net`'s ords right now: moved, and its guard is set up (funding.guard_on)."""
    from . import funding
    return moved(net) and funding.guard_on(str(net)) is not None


def tab_nets() -> dict[str, str]:
    """{Exchange tab: chain} for every moved chain."""
    return {tab(n): n for n in MOVED}


def said(nets=None, verb: str = "") -> str:
    """Which ords live where, in words: "Doginals live on Dogecoin and Pepinals on Pepecoin main"."""
    parts = [f"{word(n)}s on {NETWORKS[n].label}" for n in (MOVED if nets is None else nets)]
    if verb and parts:
        parts[0] = parts[0].replace(" on ", f" {verb} ", 1)
    return _listed(parts, "and")


def chains_said(nets=None) -> str:
    """The chains themselves, in words: "Dogecoin or Pepecoin main"."""
    return _listed([NETWORKS[n].label for n in (MOVED if nets is None else nets)], "or") or "no chain"


def _listed(parts: list[str], last: str) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + f" {last} " + parts[-1]


def swap_nets() -> tuple[str, ...]:
    return tuple(n for n in MOVED if swappable(n))


def counter(net: str) -> str:
    """The coin an ord on `net` is first priced in on its swap market: the other of Dogecoin and Pepecoin mainnet."""
    return "main" if net == "doge" else "doge"


def wallet_table() -> dict:
    """What the Wallet's ord view needs, by chain: [word, coin, coins word] and what it may do with one."""
    return {n: {"words": [w.word, coin(n), w.coins], "send": moved(n), "list": moved(n), "swap": swappable(n),
                "tab": tab(n), "counter": counter(n)}
            for n, w in WORDS.items() if n != "test"}

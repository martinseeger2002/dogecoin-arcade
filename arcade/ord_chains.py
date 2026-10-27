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
    slug: str = ""   # its Exchange tab, where the word is another chain's too: "pepinalstest"; else "doginals"


#: Every chain whose ords the arcade reads, in the order a page offers them.
WORDS = {
    "doge": OrdWords("Doginal", "Dogecoin", "Dogecoin"),
    "main": OrdWords("Pepinal", "Pepecoin", "PEP"),
    "bel": OrdWords("Bellinal", "Bellscoin", "Bellscoin"),
    "lky": OrdWords("Luckinal", "Luckycoin", "Luckycoin"),
    "wjk": OrdWords("Wojakinal", "Wojakcoin", "Wojakcoin"),
    # Litecoin ords (ord-litecoin), mainnet and testnet: those held at a legacy "L..." / "m..." address (2026-10-25).
    "ltc": OrdWords("LTC ordinal", "Litecoin", "Litecoin", "ltcordinals"),
    "ltctest": OrdWords("LTC ordinal", "Litecoin testnet", "LTC TEST", "ltctestordinals"),
    # Pepecoin testnet, for the Creator tab's tries with coins that cost nothing.
    "test": OrdWords("Pepinal", "Pepecoin test", "PEP TEST", "pepinalstest"),
}

#: The chains whose ords are sent, listed and bought here: Doginals, Pepinals, and Bellinals (2026-10-25; Bells 3 signs
#: legacy inputs as Dogecoin does, so the SINGLE|ANYONECANPAY listing digest is the same), then Luckinals, Wojakinals,
#: Litecoin ords and Pepecoin testnet's the same way (legacy inputs, the same digest; Litecoin's proven on regtest,
#: tests/test_ord_chains_more.py). Wojakinals are never swapped (no CLTV, `swappable`). Kotia has no ords
#: (timed transactions, Params.tx_time).
MOVED = ("doge", "main", "bel", "lky", "wjk", "ltc", "ltctest", "test")


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
    """The Exchange tab its ords are listed on: "doginals", "pepinals", "bellinals", "pepinalstest"."""
    got = WORDS.get(str(net))
    return (got.slug if got and got.slug else f"{word(net).lower()}s").replace(" ", "")


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
    """The coin an ord on `net` is first priced in on its swap market: the other of Dogecoin and Pepecoin mainnet, and
    on a testnet the other testnet (Pepecoin's and Litecoin's), so a test ord is never first priced in real coins."""
    if net in NETWORKS and not NETWORKS[net].real_coins:
        return "ltctest" if net == "test" else "test"
    return "main" if net == "doge" else "doge"


def wallet_table() -> dict:
    """What the Wallet's ord view needs, by chain: [word, coin, coins word] and what it may do with one."""
    return {n: {"words": [w.word, coin(n), w.coins], "send": moved(n), "list": moved(n), "swap": swappable(n),
                "tab": tab(n), "counter": counter(n)}
            for n, w in WORDS.items()}


#: The Explore tab (2026-10-25: "an explore tab that has tabs for every coin the node has ordinals/omni on and if
#: both separate drop downs to view which protocol - starting with doginals, drc-20s, then pepinals, then bellinals
#: and then litecoin ordinals, litecoin omni, and then the rest"): the coins in that order, each with its protocols.
#: kind: "ord" (inscriptions), "token" (the JSON -20 tokens the chain's ord index reads), "omni" (Omni Layer).
#: DigiByte (2026-10-26) with the rest, before the testnets: a chain whose index this arcade does not have is skipped, so it
#: is listed before DigiByte's chain lands (PR #29).
EXPLORE_ORDER = ("doge", "main", "bel", "ltc", "lky", "wjk", "dgb", "ltctest", "test")
OMNI_NETS = ("ltc", "ltctest")


def token_word(net: str) -> str:
    """The -20 tokens the ord index reads on `net` (ord_rules.token_protocol): PRC-20, BEL-20, DRC-20 ..."""
    from .ord_rules import token_protocol
    return token_protocol(net).upper()


def explore_protocols(net: str) -> list[dict]:
    """`net`'s protocols on the Explore tab, in order: its ords, its tokens, then Omni where the chain has it."""
    w = WORDS.get(str(net))
    if w is None:
        return []
    out = [{"key": "ords", "label": f"{w.word}s", "kind": "ord"},
           {"key": "tokens", "label": token_word(net), "kind": "token"}]
    if net in OMNI_NETS:
        out.append({"key": "omni", "label": "Omni", "kind": "omni"})
    return out

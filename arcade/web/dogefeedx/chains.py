"""Chain pills for DogeFeedX. A guess from the address shape, never a payment check.

Only non-EVM layer-one tickers are named. An EVM name, a Robinhood chain, any
``0x`` address, XRP/XRPL, and Solana are ``unknown``. The same rules live in
``templates/dogefeedx/_chain.js.html``; keep the two together.

A base58 string does not uniquely name a chain (``arcade/config.py``: Bells P2SH
and Dogecoin P2PKH are both ``D``, Luckycoin can wear ``L``). An explicit chain
field wins. The prefix is only the fallback, and it follows the letter each
chain already wears in that file.
"""
from __future__ import annotations

import re

TICKERS = ("DOGE", "PEPE", "LTC", "BTC", "BEL", "DGB", "LKY", "KOT", "WJK")

# Public names and the chain strings a listing already puts on a card.
NAMES = {
    "doge": "DOGE",
    "dogecoin": "DOGE",
    "dogecoin-l1": "DOGE",
    "dogecoin-sns": "DOGE",
    "doginals": "DOGE",
    "pepe": "PEPE",
    "pepecoin": "PEPE",
    "pep": "PEPE",
    "ltc": "LTC",
    "litecoin": "LTC",
    "btc": "BTC",
    "bitcoin": "BTC",
    "bel": "BEL",
    "bells": "BEL",
    "bellscoin": "BEL",
    "dgb": "DGB",
    "digibyte": "DGB",
    "lky": "LKY",
    "lucky": "LKY",
    "luckycoin": "LKY",
    "kot": "KOT",
    "kotia": "KOT",
    "wjk": "WJK",
    "wojak": "WJK",
    "wojakcoin": "WJK",
    "flop": "FLOP",
    "flopcoin": "FLOP",
}

# No badge, no colour. A 0x address is refused in ``_from_address`` as well.
EVM = frozenset({
    "eth", "ethereum", "evm", "bsc", "binance", "polygon", "matic", "avax",
    "avalanche", "base", "arbitrum", "arb", "optimism", "op", "fantom", "ftm",
    "cronos", "gnosis", "linea", "scroll", "zksync", "blast", "mantle", "celo",
    "moonbeam", "robinhood", "robinhood-chain",
})

_B58 = re.compile(r"[1-9A-HJ-NP-Za-km-z]+")

# First character of a legacy address, for a typical 26–35 character string.
_PREFIX = {
    "D": "DOGE",
    "P": "PEPE",
    "L": "LTC",
    "M": "LTC",
    "Q": "LTC",
    "m": "LTC",
    "n": "LTC",
    "2": "LTC",
    "1": "BTC",
    "3": "BTC",
    "B": "BEL",
    "K": "LKY",
    "W": "WJK",
    "S": "DGB",
    "F": "FLOP",
}


def _key(chain: str) -> str:
    return re.sub(r"[\s_]+", "-", (chain or "").strip().lower())


def _refused(key: str) -> bool:
    """XRP/XRPL and Solana are not named."""
    if "xrp" in key or "ripple" in key or "solana" in key:
        return True
    return key == "sol" or key.startswith("sol-") or key.endswith("-sol")


def _named(chain: str) -> str:
    """A ticker, ``unknown`` for a refused name, or ``''`` when the field is empty or unused."""
    key = _key(chain)
    if not key:
        return ""
    if key in EVM or "robinhood" in key or key.startswith("0x") or _refused(key):
        return "unknown"
    if key in NAMES:
        return NAMES[key]
    upper = (chain or "").strip().upper()
    if upper in TICKERS:
        return upper
    return ""


def _from_address(address: str) -> str:
    text = (address or "").strip()
    if not text or text.startswith("@"):
        return "unknown"
    low = text.lower()
    if low.startswith("0x"):
        return "unknown"
    if low.startswith("bc1"):
        return "BTC"
    if low.startswith("tltc1") or low.startswith("ltc1"):
        return "LTC"
    if low.startswith("dgb1"):
        return "DGB"
    if low.startswith("bel1"):
        return "BEL"
    if not _B58.fullmatch(text):
        return "unknown"
    n = len(text)
    # A classic r-address is not one of the named chains.
    if text[0] == "r" and 25 <= n <= 35:
        return "unknown"
    if 26 <= n <= 35:
        found = _PREFIX.get(text[0])
        if found:
            return found
    return "unknown"


def chain_label(address: str = "", chain: str = "") -> str:
    """The pill text. The chain field wins, including when it says the address is EVM."""
    named = _named(chain)
    if named:
        return named
    return _from_address(address)


def is_evm(chain: str = "", network: str = "") -> bool:
    """True when a market row names a chain this module does not list.

    That is an EVM name, a Robinhood chain, XRP/XRPL, or Solana. Empty is not refused.
    """
    for part in (chain, network):
        if _named(part) == "unknown":
            return True
    return False

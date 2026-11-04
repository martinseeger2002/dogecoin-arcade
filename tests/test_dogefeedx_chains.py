"""Chain pills name non-EVM layer ones. A 0x address, and every EVM name, is unknown."""
from pathlib import Path

import pytest

from arcade.web.dogefeedx.chains import TICKERS, chain_label, is_evm

ROOT = Path(__file__).resolve().parents[1]
CHAIN_JS = ROOT / "arcade" / "web" / "templates" / "dogefeedx" / "_chain.js.html"


def _a(prefix: str, n: int = 34) -> str:
    return prefix + ("x" * (n - len(prefix)))


@pytest.mark.parametrize(
    ("address", "chain", "expected"),
    [
        (_a("D"), "", "DOGE"),
        (_a("P"), "", "PEPE"),
        (_a("L"), "", "LTC"),
        (_a("M"), "", "LTC"),
        (_a("m"), "", "LTC"),
        ("ltc1qexampleaddressxxxx", "", "LTC"),
        ("tltc1qexampleaddress", "", "LTC"),
        (_a("1"), "", "BTC"),
        (_a("3"), "", "BTC"),
        ("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", "", "BTC"),
        (_a("S", 44), "", "unknown"),
        (_a("D", 44), "", "unknown"),
        (_a("D"), "sol", "unknown"),
        (_a("D"), "solana", "unknown"),
        ("", "SOL", "unknown"),
        (_a("r"), "", "unknown"),
        (_a("r", 25), "", "unknown"),
        (_a("D"), "xrp", "unknown"),
        (_a("D"), "xrpl", "unknown"),
        ("", "ripple", "unknown"),
        ("", "XRP", "unknown"),
        (_a("B"), "", "BEL"),
        ("bel1qexampleaddress", "", "BEL"),
        (_a("K"), "", "LKY"),
        (_a("W"), "", "WJK"),
        (_a("S"), "", "DGB"),
        ("dgb1qexampleaddress", "", "DGB"),
        ("0x742d35Cc6634C0532925a3b844Bc454e4438f44e", "", "unknown"),
        ("0Xabc", "", "unknown"),
        ("@alice", "", "unknown"),
        ("not an address", "", "unknown"),
        ("", "", "unknown"),
        (_a("D"), "eth", "unknown"),
        (_a("D"), "ethereum", "unknown"),
        (_a("D"), "evm", "unknown"),
        (_a("r"), "Robinhood Chain", "unknown"),
        ("", "polygon", "unknown"),
        ("", "base", "unknown"),
        ("0x" + "ab" * 20, "DOGE", "DOGE"),
        ("", "dogecoin-l1", "DOGE"),
        ("", "doginals", "DOGE"),
        ("", "PEPE", "PEPE"),
        ("", "sol", "unknown"),
        ("", "solana", "unknown"),
        ("", "kot", "KOT"),
        ("", "kotia", "KOT"),
    ],
)
def test_chain_label(address, chain, expected):
    assert chain_label(address, chain) == expected


def test_evm_marker_rejects_robinhood_and_ignores_a_blank():
    assert is_evm("evm")
    assert is_evm("doginals", "Robinhood Chain")
    assert is_evm("", "ethereum")
    assert is_evm("xrp")
    assert is_evm("", "xrpl")
    assert is_evm("ripple")
    assert is_evm("sol")
    assert is_evm("", "solana")
    assert not is_evm("")
    assert not is_evm("dogecoin-l1")
    assert not is_evm("doginals", "Doginal (Dogecoin L1)")


def test_chain_script_matches_the_non_evm_list():
    js = CHAIN_JS.read_text()
    for ticker in TICKERS:
        assert ticker in js
    assert "indexOf('0x') === 0) return 'unknown'" in js
    assert "robinhood" in js
    assert "'ETH'" not in js and '"ETH"' not in js
    assert "XRP" not in js and "XRPL" not in js and "SOL" not in js
    for needle in ("dfx-chain-eth", "dfx-chain-evm", "dfx-chain-robinhood", "dfx-chain-xrp", "dfx-chain-sol"):
        assert needle not in js
    assert "indexOf('xrp')" in js and "indexOf('ripple')" in js and "indexOf('solana')" in js

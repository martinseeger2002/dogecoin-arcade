"""Litecoin safety before L main opens (SadFrog's side, 2026-10-18): an account's Litecoin
transaction pays Litecoin's own fee and dust rules, not Pepecoin's; a real-money review says LTC;
Litecoin addresses this arcade cannot pay are refused for what they are; and the node trader never
spends an inscribed Litecoin coin."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state                                   # noqa: F401,E402

from arcade import fees, funding                                # noqa: E402
from arcade.config import NETWORKS                               # noqa: E402
from arcade.script import b58check_encode                        # noqa: E402
from arcade.txbuild import multisig_script, p2pkh_script         # noqa: E402

LTC, MAIN, DOGE = NETWORKS["ltc"], NETWORKS["main"], NETWORKS["doge"]
HASH = bytes(range(20))


def test_litecoin_has_its_own_rate_and_dust_and_the_dogecoin_family_keeps_theirs():
    assert fees.rate_for(LTC, fees.MIN_FEE_PER_KB) == 10_000, "0.0001 LTC/kB, not Pepecoin's 0.01"
    assert fees.rate_for(NETWORKS["ltctest"], fees.MIN_FEE_PER_KB) == 10_000
    for same in (MAIN, DOGE, NETWORKS["test"]):
        assert fees.rate_for(same, fees.MIN_FEE_PER_KB) == fees.MIN_FEE_PER_KB, same.name
        assert fees.dust_limit(same) == fees.DUST_LIMIT and same.soft_dust
    assert fees.rate_for(LTC, 123_456) == 123_456, "a caller's own rate is kept"
    assert fees.dust_limit(LTC) == 10_000 and not LTC.soft_dust and not NETWORKS["ltctest"].soft_dust
    assert fees.dust_limit(None) == fees.DUST_LIMIT


def test_no_soft_dust_fee_on_litecoin():
    small = [(100_000, multisig_script([b"\x02" + b"\x11" * 32] * 3, 1))]   # a Class B output, 0.001
    with_dust = funding.price(1, small, 10_000, change=True, params=MAIN)
    without = funding.price(1, small, 10_000, change=True, params=LTC)
    assert with_dust - without == fees.DUST_LIMIT, "Pepecoin's surcharge, and only there"
    assert funding.price(1, small, 10_000, change=True) == with_dust, "no params: as before"


def test_an_account_litecoin_build_pays_litecoins_fee_and_keeps_its_change():
    me = b58check_encode(LTC.pubkeyhash_version, HASH)
    coin = {"txid": "ab" * 32, "vout": 0, "value": 10_000_000, "address": me}       # 0.1 LTC
    payload = [(100_000, multisig_script([b"\x02" + b"\x11" * 32] * 3, 1)), (0, b"\x6a\x04post")]
    ltc = funding.build_one(LTC, me, coin, payload, fees.MIN_FEE_PER_KB, what="a post")
    pep = funding.build_one(MAIN, b58check_encode(MAIN.pubkeyhash_version, HASH),
                            dict(coin, address=""), payload, fees.MIN_FEE_PER_KB, what="a post")
    assert ltc.fee * 50 < pep.fee, f"{ltc.fee} vs {pep.fee}: Litecoin is not charged Pepecoin's rate"
    assert ltc.fee < 10_000, "well under 0.0001 LTC for a small post"
    assert ltc.change == 10_000_000 - 100_000 - ltc.fee and ltc.outputs[-1] == (ltc.change, p2pkh_script(me))


def test_litecoin_change_under_pepecoins_dust_line_is_kept_not_given_to_the_miner():
    me = b58check_encode(LTC.pubkeyhash_version, HASH)
    coin = {"txid": "cd" * 32, "vout": 1, "value": 600_000, "address": me}          # 0.006 LTC
    built = funding.build_one(LTC, me, coin, [(0, b"\x6a\x04post")], fees.MIN_FEE_PER_KB)
    assert built.change > 500_000, "0.005 LTC of change is change on Litecoin, not a fee"


def test_litecoin_addresses_this_arcade_cannot_pay_are_named(app_state):
    from arcade.web.app import _check_address
    from arcade.web.state import ChainContext
    ltc = ChainContext(network="ltc", role="ledger", label="Litecoin")
    mweb = _check_address("ltcmweb1qq0hc8n4uq5qa0yfmxhxlq8m7n4j5sjhtdxvdm2zelw3vqdn7j8t6jqhd7e0ym", mainnet=ltc)
    assert mweb and "MWEB" in mweb
    seg = _check_address("ltc1qg82y3c8ytgsvkmq0qy7zgw0qx9fuf8u9sqd0wq", mainnet=ltc)
    assert seg and "SegWit or Taproot" in seg and "ltc1" in seg
    tseg = _check_address("tltc1qg82y3c8ytgsvkmq0qy7zgw0qx9fuf8u9pwzxpn", mainnet=ltc)
    assert tseg and "Litecoin testnet" in tseg
    assert _check_address(b58check_encode(48, HASH), mainnet=ltc) is None, "an L… address pays"
    assert "Dogecoin" in (_check_address(b58check_encode(30, HASH), mainnet=ltc) or "")


def test_the_real_money_review_says_ltc_on_litecoin():
    from arcade.web.app import TEMPLATE_DIR
    page = (TEMPLATE_DIR / "base.html").read_text()
    assert "Real LTC" in page and "costs.chain === 'ltc'" in page
    swaps = (TEMPLATE_DIR / "wallet_js.js").read_text()
    # One table of coin words since 2026-10-22 (Bellscoin and Luckycoin joined), read by both the fee lines and the review.
    assert 'ltc: "LTC"' in swaps and "COIN_WORD[n]" in swaps and "COIN_WORD[give.network]" in swaps
    assert "'ledger', 'doge', 'ltc'" in (TEMPLATE_DIR / "wallet.html").read_text()


def test_the_node_trader_sets_inscribed_litecoin_coins_aside(monkeypatch):
    from arcade import node_trader

    class Guard:
        def spendable_known(self, coins):
            return [c for c in coins if c["txid"] != "aa" * 32]          # the first coin is inscribed

    calls = []

    class Rpc:
        def call(self, method, *args):
            calls.append((method, *args))
            if method == "listunspent":
                return [{"txid": "aa" * 32, "vout": 0, "amount": 0.001},
                        {"txid": "bb" * 32, "vout": 0, "amount": 1.0}]
            return "cc" * 32

    monkeypatch.setitem(funding.GUARD_BY_NET, "ltc", Guard())
    trader = node_trader.NodeTrader.__new__(node_trader.NodeTrader)
    trader._doginals = None
    trader._send_coins(Rpc(), "ltc", b58check_encode(48, HASH), 50_000_000)
    locked = [c for c in calls if c[0] == "lockunspent"]
    assert locked and locked[0][1] is False and locked[0][2] == [{"txid": "aa" * 32, "vout": 0}]
    assert locked[-1][1] is True, "let go after the one send"
    calls.clear()
    trader._send_coins(Rpc(), "main", b58check_encode(56, HASH), 50_000_000)
    assert [c[0] for c in calls] == ["sendtoaddress"], "Pepecoin mainnet sends as it always did"

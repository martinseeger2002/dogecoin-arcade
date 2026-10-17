"""The browser's half of a Doginal listing and purchase (coins.js verifyDoginalAsk / verifyDoginalBuy) works out the
same digests as arcade/doginal_market.py, and refuses a node that asks for something else [Claude 2026-10-15]."""

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
pytest.importorskip("selenium", reason="browser tests need selenium")
from test_coins_browser import browser, loaded, served             # noqa: F401,E402
from test_doginal_market import BUYER, BUYER_KEY, SELLER, SELLER_KEY, _listing, market  # noqa: F401,E402
from test_funding import _pubkey                                    # noqa: E402

from arcade import doginal_market as M, fees                       # noqa: E402
from arcade.config import NETWORKS                                  # noqa: E402

RUN = """
const done = arguments[arguments.length - 1];
const [fn, said, pubkey, address] = arguments;
coins[fn](said, {pubkey: coins.unhex(pubkey), address, version: 30})
  .then((got) => done({ok: true, got: got.hashes ? got.hashes : coins.hex(got)}),
        (e) => done({ok: false, error: String(e.message || e)}));
"""


def _run(browser, fn, said, key, address):
    return browser.execute_async_script(RUN, fn, said, _pubkey(key).hex(), address)


def test_the_browser_signs_the_same_listing_digest_and_refuses_another(loaded, market):
    browser, _, _ = loaded
    ask, _ = _listing()
    said = json.loads(json.dumps(ask.as_json()))
    got = _run(browser, "verifyDoginalAsk", said, SELLER_KEY, SELLER)
    assert got == {"ok": True, "got": ask.digest}
    cheaper = dict(said, price=said["price"] - 1)
    assert not _run(browser, "verifyDoginalAsk", cheaper, SELLER_KEY, SELLER)["ok"], "a price the digest is not over"
    assert not _run(browser, "verifyDoginalAsk", said, BUYER_KEY, BUYER)["ok"], "another address's listing"


def test_the_browser_checks_the_purchase_layout_and_its_digests(loaded, market):
    browser, _, _ = loaded
    _, row = _listing()
    p = M.build_purchase(None, NETWORKS["doge"], BUYER, row, rate=fees.MIN_FEE_PER_KB)
    said = {**p.as_json(), "seller": SELLER, "price": row["price"]}
    got = _run(browser, "verifyDoginalBuy", said, BUYER_KEY, BUYER)
    assert got == {"ok": True, "got": p.sighashes}
    assert not _run(browser, "verifyDoginalBuy", dict(said, price=row["price"] + 1), BUYER_KEY, BUYER)["ok"]
    assert not _run(browser, "verifyDoginalBuy", dict(said, seller=BUYER), BUYER_KEY, BUYER)["ok"]
    swapped = [dict(i) for i in said["inputs"]]
    swapped[2]["value"] += 1                      # output 1 would no longer be the whole inscribed coin
    assert not _run(browser, "verifyDoginalBuy", dict(said, inputs=swapped), BUYER_KEY, BUYER)["ok"]

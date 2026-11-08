"""ArcadeCore's Assets does with tokens, ords and NFTs everything the website Wallet does (2026-11-08, SadFrog: "there
shouldnt be a differentiation between wallets now it should all be the same thing - when you change the theme, thats it
everything is styled differently but the availability and capability doesnt change", "if things are getting chosen
chose the lites version of things"). Each flow in Firefox against the arcade's own routes, as
test_doginal_wallet_browser's ArcadeCore test does: what is held, picked for Send or reviewed under Assets with every
cost listed, and nothing signed on Cancel."""

import contextlib
import pathlib
import sys
import time

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_doginal_wallet_browser import (ANSWERS, INS, THEM, _until, browser, reads, served,   # noqa: E402,F401
                                         signed_in)
from test_end_to_end import chain                                # noqa: E402,F401  (the regtest node, gap 8)

from selenium.webdriver.common.by import By                      # noqa: E402

from arcade import funding                                       # noqa: E402

PASSWORD = "a password nobody needs to guess for this test"


def _assets(browser, base, net="doge"):
    """ArcadeCore at its Assets tab, on `net`'s wallet (loaded afresh: from /lite#send, /lite#assets is only a jump)."""
    browser.get("about:blank")
    browser.get(f"{base}/lite#assets")
    _until(browser, "const b = document.getElementById('a-chain');"
                    f" return b && [...b.options].some((o) => o.value === '{net}')")
    browser.execute_script("const b = document.getElementById('a-chain'); b.value = arguments[0];"
                           " b.dispatchEvent(new Event('change'));", net)


def _unlock_if_asked(browser, form="w-unlock"):
    if browser.find_element(By.ID, form).is_displayed():
        box = "w-password" if form == "w-unlock" else "a-password"
        browser.find_element(By.ID, box).send_keys(PASSWORD)
        browser.find_element(By.CSS_SELECTOR, f"#{form} button[type=submit], #{form} button").click()


def _send_review(browser):
    """Review… on the Send tab; the password if the wallet is shut in this tab; the terms, or what was said instead."""
    browser.find_element(By.CSS_SELECTOR, "#w-form button[type=submit]").click()
    _until(browser, "return !document.getElementById('w-review').hidden || !document.getElementById('w-unlock').hidden"
                    " || !document.getElementById('w-said').hidden")
    _unlock_if_asked(browser)
    return _until(browser, "return (!document.getElementById('w-review').hidden"
                           " && document.getElementById('w-terms').textContent)"
                           " || (!document.getElementById('w-said').hidden && 'SAID ' + document.getElementById('w-said').textContent)")


def _assets_review(browser):
    """Under Assets: the password if asked, then the review's terms (or what was said instead)."""
    _until(browser, "return !document.getElementById('a-review').hidden || !document.getElementById('a-said').hidden")
    if browser.find_element(By.ID, "a-unlock").is_displayed():
        browser.find_element(By.ID, "a-password").send_keys(PASSWORD)
        browser.find_element(By.CSS_SELECTOR, "#a-unlock button[type=submit]").click()
    return _until(browser, "return (!document.getElementById('a-review').hidden && !document.getElementById('a-go').hidden"
                           " && document.getElementById('a-terms').textContent)"
                           " || (!document.getElementById('a-said').hidden && 'SAID ' + document.getElementById('a-said').textContent)")


def _account_address(browser, network):
    """The signed-in account's address on `network`, as /account says it."""
    return _until(browser, "const r = new XMLHttpRequest(); r.open('GET', '/account', false); r.send();"
                           " const c = (JSON.parse(r.responseText).chains || []).find((x) => x.network === arguments[0]);"
                           " return c && c.address" .replace("arguments[0]", repr(network)))


@contextlib.contextmanager
def _setting(state, name, value):
    was = state.setting(name, None)
    state.set_setting(name, value)
    try:
        yield
    finally:
        state.set_setting(name, was)


def test_arcade_tokens_are_starred_managed_and_sent_from_assets_and_sends_list(browser, served, signed_in):
    """Gap 1: the arcade's tokens under Assets, with the full Wallet's stars (its Exchange market's) and the issuer's
    Manage, and Send: from the token's row, or from Send's own list, reviewed with the token, the payee and every cost."""
    base, state = served
    with state.token_index(state.doge).open() as db:
        db.conn.execute("INSERT OR REPLACE INTO property(property_id, ecosystem, property_type, issuer, name, managed,"
                        " total_tokens, creation_txid, creation_block) VALUES (?,?,?,?,?,?,?,?,?)",
                        (77, 1, 2, signed_in, "Coretoken", 1, 10**12, "aa" * 32, 1))
        db.conn.execute("INSERT OR REPLACE INTO balance(address, property_id, balance) VALUES (?,?,?)",
                        (signed_in, 77, 5_00000000))
        db.conn.commit()
    browser.execute_script("localStorage.clear()")
    _assets(browser, base)
    row = _until(browser, "return [...document.querySelectorAll('#a-tokens tr')].find((r) => r.textContent.includes('Coretoken'))")
    assert "#77" in row.text and "you made it" in row.text and "5" in row.text, row.text
    browser.execute_script("document.querySelector('#a-tokens .a-star').click()")
    _until(browser, "return document.querySelector('#a-tokens .a-star').getAttribute('aria-pressed') === 'true'")
    kept = browser.execute_script("const r = new XMLHttpRequest(); r.open('GET', '/account/favorites', false); r.send();"
                                  " return r.responseText + (localStorage.getItem('arcade-favorites') || '')")
    assert "/exchange/pair/77@doge" in kept, "the star is the token's Exchange market's, kept as the Exchange keeps it"
    # The issuer's Manage: the token's own page, in the window.
    browser.execute_script("[...document.querySelectorAll('#a-tokens button')].find((b) => b.textContent === 'Manage').click()")
    # (in the module tab that holds the Tokens pages, when there is one; else in the window's own page frame)
    _until(browser, "return [...document.querySelectorAll('main section.frame')].some((p) => !p.hidden"
                    " && /\\/tokens\\/77$/.test(p.querySelector('iframe').getAttribute('src') || ''))")
    browser.find_element(By.CSS_SELECTOR, '.toolbar [data-tab="assets"]').click()
    browser.execute_script("[...document.querySelectorAll('#a-tokens button')].find((b) => b.textContent === 'Send').click()")
    _until(browser, "return !document.querySelector('main [data-panel=\"send\"]').hidden"
                    " && document.getElementById('w-what').selectedOptions[0].textContent.includes('Coretoken')")
    assert browser.find_element(By.ID, "w-amount").is_displayed(), "a token is sent by the amount"
    assert "Coretoken" in browser.execute_script("return document.querySelector('[data-unit]').textContent")
    browser.find_element(By.ID, "w-to").send_keys(THEM)
    browser.find_element(By.ID, "w-amount").send_keys("9")
    said = _send_review(browser)
    assert said.startswith("SAID") and "more than you have" in said, said
    browser.find_element(By.ID, "w-amount").clear()
    browser.find_element(By.ID, "w-amount").send_keys("1.5")
    terms = _send_review(browser)
    assert THEM in terms and "1.5 Coretoken" in terms and "Network fee" in terms and "Leaves your wallet" in terms, terms
    assert browser.find_element(By.ID, "w-warn").is_displayed(), "Dogecoin is real coins: said so"
    browser.find_element(By.ID, "w-cancel").click()
    # Send's own list: back to coins.
    browser.execute_script("const s = document.getElementById('w-what'); s.value = ''; s.dispatchEvent(new Event('change'))")
    _until(browser, "return !/Coretoken/.test(document.querySelector('[data-unit]').textContent)")


def test_drc20_balances_show_under_assets_and_send_from_send(browser, served, signed_in):
    """Gap 2: DRC-20 on Dogecoin, from the chain's ord index as the full Wallet reads them (on hand, transferable, on
    its way), and Send for one, the amount checked against what is on hand before anything is asked."""
    base, state = served

    class Index:
        tip = 6_000_000

        def lookup(self, q):
            if q["what"] == "balances":
                return {"balances": [{"tick": "dogi", "units": "150000", "transferable_units": "2500", "decimals": 2}],
                        "walked_to": 6_000_000}
            if q["what"] == "numbers":
                return {"numbers": {}, "walked_to": 6_000_000}
            raise ValueError("no such question")
    real = state.ord_indexes
    state.ord_indexes = lambda: {"doge": Index()}
    try:
        browser.execute_script("localStorage.clear()")
        _assets(browser, base)
        row = _until(browser, "return !document.getElementById('a-t20-box').hidden"
                              " && [...document.querySelectorAll('#a-t20 tr')].find((r) => r.textContent.includes('dogi'))")
        assert "DRC-20" in browser.find_element(By.ID, "a-t20-word").text
        assert "1,500" in row.text and "25 transferable" in row.text, row.text
        browser.execute_script("[...document.querySelectorAll('#a-t20 button')].find((b) => b.textContent === 'Send').click()")
        _until(browser, "return !document.querySelector('main [data-panel=\"send\"]').hidden"
                        " && document.getElementById('w-what').selectedOptions[0].textContent.includes('dogi')")
        assert "DRC-20" in browser.execute_script("return document.getElementById('w-what').selectedOptions[0].textContent")
        browser.find_element(By.ID, "w-to").send_keys(THEM)
        browser.find_element(By.ID, "w-amount").send_keys("1500")
        said = _send_review(browser)
        assert said.startswith("SAID") and "more than you have: 1,475 dogi" in said, "the transferable is set aside"
        browser.find_element(By.ID, "w-amount").clear()
        browser.find_element(By.ID, "w-amount").send_keys("12.505")
        said = _send_review(browser)
        assert said.startswith("SAID") and "2 decimal places" in said, said
    finally:
        state.ord_indexes = real


def test_the_ord_view_has_kinds_collections_order_go_to_and_each_ords_card(browser, served, signed_in, reads):
    """Gap 6: the full Wallet's ord view in Assets: All, Pictures & pages and Text with their counts, the collection and
    the order, Go to #number, and the card with the collection's facts, the content link and Send."""
    base, state = served
    txs = [f"{n:064x}" for n in range(0x7800, 0x7800 + 6)]
    numbers = {t + "i0": 91_000 + i for i, t in enumerate(txs)}
    for t in txs:
        ANSWERS[t] = {"txid": t, "inscriptions": [{"vout": 0, "inscription_id": t + "i0", "sat_offset": 0}]}
    with state.token_index(state.doge).open() as db:
        for t in txs:
            db.conn.execute("INSERT OR REPLACE INTO utxo(txid,vout,address,value,height) VALUES (?,?,?,?,?)",
                            (t, 0, signed_in, 100_000, 3))
        db.conn.commit()
    real_ctype = getattr(funding.GUARD, "ctype_of", None)
    funding.GUARD.ctype_of = lambda iid: "text/plain" if iid[:64] == txs[0] else "image/png"
    punk = txs[2] + "i0"

    class Index:
        tip = 6_000_000

        def lookup(self, q):
            if q["what"] == "numbers":
                return {"numbers": {i: {"number": numbers[i], "height": 4_700_000, "size": 900,
                                        "content_type": "text/plain" if i[:64] == txs[0] else "image/png"}
                                    for i in q["ids"] if i in numbers}, "walked_to": 6_000_000}
            if q["what"] == "number":
                i = q["id"]
                return {"number": {"inscription": {"id": i, "number": numbers.get(i, 0), "height": 4_700_000,
                                                   "content_type": "image/png", "size": 900, "output": f"{i[:64]}:0",
                                                   "offset": 0, "owner": signed_in}}}
            if q["what"] == "balances":
                return {"balances": [], "walked_to": 6_000_000}
            raise ValueError("no such question")

    class Known:
        def in_collection(self, chain, iid):
            if iid == punk:
                return {"slug": "corepunks", "collection": "CorePunks", "position": 7, "name": "CorePunk #7",
                        "source": "nintondo"}
            return None

        def collection(self, chain, slug):
            return {"supply": 10000, "owners": 2210, "created": 1677000000}
    real = (state.ord_indexes, getattr(state, "known_collections", None))
    state.ord_indexes = lambda: {"doge": Index()}
    state.known_collections = lambda: Known()
    try:
        state.set_setting("node_trades", False)
        browser.execute_script("localStorage.clear()")
        _assets(browser, base)
        _until(browser, "return !document.getElementById('a-bar').hidden && document.getElementById('a-coll')"
                        " && [...document.getElementById('a-coll').options].some((o) => /CorePunks/.test(o.textContent))", 40)
        bar = browser.find_element(By.ID, "a-bar").text
        assert "All" in bar and "Pictures & pages" in bar and "Text" in bar, bar
        # Text only: the one text inscription among them.
        browser.execute_script("document.querySelector('#a-bar [data-kind=\"text\"]').click()")
        _until(browser, f"return document.querySelector('#a-grid figure[data-ord=\"{txs[0]}i0\"]')"
                        " && [...document.querySelectorAll('#a-grid figure')].every((f) => !f.querySelector('iframe'))")
        browser.execute_script("document.querySelector('#a-bar [data-kind=\"all\"]').click()")
        # Earliest first: the lowest number of all leads.
        browser.execute_script("const s = document.getElementById('a-order'); s.value = 'old'; s.dispatchEvent(new Event('change'))")
        _until(browser, "return document.querySelector('#a-grid figure') && document.getElementById('a-order').value === 'old'")
        assert "old" in browser.execute_script("return localStorage.getItem('arcade.wallet.dgview:doge')"), \
            "kept where the full Wallet keeps it"
        # Go to #number: its card, with its collection's facts and the content link.
        browser.execute_script(f"const g = document.getElementById('a-go-to'); g.value = '#{numbers[punk]}';"
                               " g.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter'}))")
        card = _until(browser, "const d = document.getElementById('a-card');"
                               " return d.classList.contains('open') && /Open content/.test(d.textContent) && d.textContent")
        assert f"#{numbers[punk]:,}" in card and "CorePunks" in card and "10,000 pieces" in card, card
        assert "Send" in card and "In Explore" in card
        assert "List" not in card, "selling while away is off here, so the arcade would refuse a listing: none offered"
        link = browser.execute_script("return [...document.querySelectorAll('#a-card a')].find((a) =>"
                                      " a.textContent.startsWith('Open content')).getAttribute('href')")
        assert link == f"/content/{punk}"
        browser.execute_script("document.querySelector('#a-card [data-close]').click()")
        browser.execute_script("const s = document.getElementById('a-order'); s.value = 'new'; s.dispatchEvent(new Event('change'))")
    finally:
        state.ord_indexes = real[0]
        if real[1] is not None:
            state.known_collections = real[1]
        if real_ctype is not None:
            funding.GUARD.ctype_of = real_ctype


def test_an_ord_is_listed_from_its_card_after_a_review_of_the_ask(browser, served, signed_in, monkeypatch):
    """Gap 3: with selling while away on, List on an ord's card asks the arcade for the ask (/account/doginals/list),
    checks it is that coin at that price paid to this wallet (coins.verifyDoginalAsk), and shows the price and the payee
    before anything is signed. Cancel signs nothing."""
    base, state = served

    class Rpc:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def call(self, method, *args):
            if method == "gettxout":
                from arcade.txbuild import p2pkh_script
                return {"value": 0.001, "scriptPubKey": {"hex": p2pkh_script(signed_in).hex()}}
            raise RuntimeError(method)
    monkeypatch.setattr(type(state.doge), "rpc", lambda self: Rpc())
    with _setting(state, "node_trades", True):
        browser.execute_script("localStorage.clear()")
        _assets(browser, base)
        _until(browser, "return /on Dogecoin/.test(document.getElementById('a-status').textContent)"
                        " && document.querySelector('#a-grid figure')")
        for _ in range(10):
            fig = browser.execute_script(f"return document.querySelector('#a-grid figure[data-ord=\"{INS}\"]')")
            if fig or not browser.find_element(By.ID, "a-next").is_displayed() \
                    or browser.find_element(By.ID, "a-next").get_attribute("disabled") is not None:
                break
            page = browser.find_element(By.ID, "a-page").text
            browser.find_element(By.ID, "a-next").click()
            _until(browser, f"return document.getElementById('a-page').textContent !== {page!r}")
        assert fig, "the account's Doginal"
        browser.execute_script(f"[...document.querySelector('#a-grid figure[data-ord=\"{INS}\"]').querySelectorAll('button')]"
                               ".find((b) => b.textContent === 'List').click()")
        _until(browser, "return document.getElementById('a-card').classList.contains('open') && document.getElementById('a-card-price')")
        browser.find_element(By.ID, "a-card-price").send_keys("42")
        browser.execute_script("[...document.querySelectorAll('#a-card button')].find((b) => b.textContent === 'List').click()")
        terms = _assets_review(browser)
        assert not terms.startswith("SAID"), terms
        assert "42 DOGE" in terms and signed_in in terms and "nothing now" in terms, terms
        assert browser.find_element(By.ID, "a-warn").is_displayed()
        browser.find_element(By.ID, "a-cancel").click()
        assert "Nothing was signed" in browser.find_element(By.ID, "a-said").text


def test_a_chain_whose_ords_are_not_moved_from_here_shows_them_without_send(browser, served, signed_in, monkeypatch):
    """Gap 7: Assets follows each chain's rules for its ords (ord_chains.wallet_table), as the full Wallet does: where
    one is not sent from the arcade, it is shown, with no Send and no List, and says why."""
    from arcade import ord_chains
    base, _ = served
    real = ord_chains.wallet_table
    monkeypatch.setattr(ord_chains, "wallet_table",
                        lambda: {n: {**v, "send": False, "list": False} if n == "doge" else v for n, v in real().items()})
    browser.execute_script("localStorage.clear()")
    _assets(browser, base)
    _until(browser, f"return document.querySelector('#a-grid figure')")
    notes = _until(browser, "return !document.getElementById('a-notes').hidden && document.getElementById('a-notes').textContent")
    assert "not ready yet" in notes, notes
    assert not browser.execute_script("return [...document.querySelectorAll('#a-grid button')].some((b) =>"
                                      " b.textContent === 'Send' || b.textContent === 'List')")


def test_arcade_nfts_by_collection_sold_unlisted_and_sent(browser, served, signed_in):
    """Gaps 4 and 6: the arcade's NFTs grouped by collection, a collection's pieces searched by number, and each one's
    sale: Sell (while selling while away is on) lists what the signature says and files it, the listing is cancelled
    (/account/list/cancel) and an older price taken off (/account/nft/sell) each after a review of every cost, and Send
    picks a piece for the Send tab."""
    base, state = served
    pieces = [(f"{0x9100 + n:064x}", 5000 + n) for n in range(3)]
    with state.token_index(state.doge).open() as db:
        for txid, number in pieces:
            db.conn.execute("INSERT OR REPLACE INTO inscription(txid, number, creator, owner, block_height, position,"
                            " content_type, content_len, sha256, json, chunks) VALUES (?,?,?,?,?,?,?,?,?,?,1)",
                            (txid, number, signed_in, signed_in, 100, 0, "image/png", 1234, "00" * 32, "{}"))
        # An older price its holder put on the chain (an ask), on the third.
        db.conn.execute("INSERT OR REPLACE INTO nft_ask(txid, block_height, position, inscription, seller, take_kind,"
                        " take_property, take_amount) VALUES (?,?,?,?,?,?,?,?)",
                        ("a5" * 32, 100, 1, pieces[2][0], signed_in, 1, None, 2 * 10**8))
        db.conn.commit()
    piece = lambda n: f"#a-nfts figure[data-piece=\\\"{pieces[n][0]}\\\"]"       # noqa: E731
    press = lambda n, text: browser.execute_script(                                  # noqa: E731
        f"[...document.querySelector('{piece(n)}').querySelectorAll('button')].find((b) => b.textContent === '{text}').click()")
    with _setting(state, "node_trades", True):
        browser.execute_script("localStorage.clear()")
        _assets(browser, base)
        _until(browser, "return !document.getElementById('a-nfts-box').hidden && document.querySelector('#a-nfts .a-colls button')")
        assert "3 pieces" in browser.find_element(By.ID, "a-nfts").text
        browser.execute_script("document.querySelector('#a-nfts .a-colls button').click()")
        _until(browser, f"return document.querySelector('{piece(0)}')")
        assert "Showing 3 of 3" in browser.find_element(By.ID, "a-nfts").text
        browser.find_element(By.ID, "a-piece-q").send_keys(str(pieces[1][1]))
        _until(browser, "return document.querySelectorAll('#a-nfts figure').length === 1")
        browser.find_element(By.ID, "a-piece-q").clear()
        browser.execute_script("document.getElementById('a-piece-q').dispatchEvent(new Event('input'))")
        _until(browser, "return document.querySelectorAll('#a-nfts figure').length === 3")
        # Sell: the price, the review of what the signature says, and filed on Sell it.
        press(0, "Sell")
        _until(browser, "return !document.getElementById('a-sell').hidden")
        browser.find_element(By.ID, "a-sell-price").send_keys("3")
        browser.find_element(By.CSS_SELECTOR, "#a-sell button[type=submit]").click()
        terms = _assets_review(browser)
        assert "3 DOGE" in terms and "anybody, for 30 days" in terms and "a signature, not a transaction" in terms, terms
        assert "The signature says" in terms and "takes 3" in terms, "read from the leg's own bytes"
        browser.find_element(By.ID, "a-cancel").click()
        # Filed as /account/list/sign files it (that route asks a Dogecoin node this test has none of): the same leg.
        leg = browser.execute_async_script("const done = arguments[1]; import('/wallet.js').then((w) =>"
                                           " w.offerListing(arguments[0], '3', 'doge')).then(done, (e) => done({error: String(e)}))",
                                           pieces[0][0])
        assert "raw" in leg, leg
        now = time.time()
        state.listings.add({"id": "c0" * 8, "network": "doge", "owner": signed_in, "leg": leg["raw"],
                            "input": leg["inputs"][0], "payload": leg["payload"],
                            "coin": leg["inputs"][1] if len(leg["inputs"]) > 1 else None,
                            "output": leg["outputs"][-1], "fee": leg["fee"], "price": 3 * 10**8, "what": "",
                            "created": now, "expires": now + 86400})
        _assets(browser, base)
        _until(browser, "return document.querySelector('#a-nfts .a-colls button')")
        browser.execute_script("document.querySelector('#a-nfts .a-colls button').click()")
        # Listed: Cancel listing, reviewed with every cost of spending its coins back.
        _until(browser, f"return document.querySelector('{piece(0)}') && [...document.querySelector('{piece(0)}')"
                        ".querySelectorAll('button')].some((b) => b.textContent === 'Cancel listing')")
        assert "for sale" in browser.find_element(By.CSS_SELECTOR, piece(0).replace('\\"', '"')).text
        press(0, "Cancel listing")
        terms = _assets_review(browser)
        assert "Cancelling" in terms and "Network fee" in terms and "Leaves your wallet" in terms, terms
        browser.find_element(By.ID, "a-cancel").click()
        assert "Nothing was signed" in browser.find_element(By.ID, "a-said").text
        # The older price: taken off on the chain, reviewed the same way (from a coin of its own: the reviews above
        # hold theirs for a while, as any offer does).
        with state.token_index(state.doge).open() as db:
            db.conn.execute("INSERT OR REPLACE INTO utxo(txid,vout,address,value,height) VALUES (?,?,?,?,?)",
                            ("c1" * 32, 0, signed_in, 2 * 10**8, 3))
            db.conn.commit()
        assert "asking" in browser.find_element(By.CSS_SELECTOR, piece(2).replace('\\"', '"')).text
        press(2, "Take the old price off")
        terms = _assets_review(browser)
        assert "Taking off" in terms and "Network fee" in terms and "Leaves your wallet" in terms, terms
        browser.find_element(By.ID, "a-cancel").click()
        # Send picks a piece for the Send tab.
        press(1, "Send")
        _until(browser, "return !document.querySelector('main [data-panel=\"send\"]').hidden"
                        f" && document.getElementById('w-asset').textContent === '#{pieces[1][1]:,}'")
        browser.find_element(By.ID, "w-asset-clear").click()


def test_escrows_are_taken_back_from_assets_after_a_review(browser, served, signed_in):
    """Gap 5: what a game holds in escrow for the account, and Take back once it is open, reviewed first and then
    handed to wallet.reclaimEscrow, which checks each move home before it signs it."""
    from arcade import escrow as escrowlib
    base, state = served
    browser.get(f"{base}/lite#assets")
    mine = _account_address(browser, state.messaging.network)
    where = "2N" + "1" * 33
    escrowlib.Book(state.home / "escrows.sqlite").add({
        "address": where, "network": state.messaging.network, "game": "ab" * 32, "owner": mine,
        "owner_pubkey": "02" + "11" * 32, "referee": "", "unlock": int(time.time()) - 60, "redeem": "51"})
    with state.token_index(state.messaging).open() as db:
        db.conn.execute("INSERT OR REPLACE INTO inscription(txid, number, creator, owner, block_height, position,"
                        " content_type, content_len, sha256, json, chunks) VALUES (?,?,?,?,?,?,?,?,?,?,1)",
                        ("e5" * 32, 777, where, where, 100, 0, "image/png", 10, "00" * 32, "{}"))
        db.conn.commit()
    _assets(browser, base)
    row = _until(browser, "return !document.getElementById('a-escrows-box').hidden && document.getElementById('a-escrows').textContent")
    assert "1 NFT" in row and "yours to take back" in row, row
    browser.execute_script("[...document.querySelectorAll('#a-escrows button')].find((b) => b.textContent === 'Take back').click()")
    terms = _assets_review(browser)
    assert "1 NFT" in terms and "the network fee, from the escrow's own coin" in terms, terms
    browser.find_element(By.ID, "a-go").click()
    said = _until(browser, "return !document.getElementById('a-said').hidden && document.getElementById('a-said').textContent")
    assert said, "the arcade's answer is said: here it cannot reach a chain"


def test_doginals_at_the_testnet_keys_address_are_brought_home_from_assets(browser, served, signed_in):
    """Gap 5: Doginals sent to the account's testnet key's Dogecoin address show under Assets, and with no DOGE there
    for the fee, its button fills in the Send tab to pay that fee first, reviewed there like any send."""
    from arcade import utxos as U
    from arcade.script import b58check_decode, b58check_encode
    base, state = served
    browser.get(f"{base}/lite#assets")
    tag = _account_address(browser, state.messaging.network)
    stray = b58check_encode(30, b58check_decode(tag)[1])
    txid = "5d" * 32
    ANSWERS[txid] = {"txid": txid, "inscriptions": [{"vout": 0, "inscription_id": txid + "i0", "sat_offset": 0}]}
    with contextlib.closing(state.token_index(state.doge).open()) as db:
        U.watch(db, stray, 0, "test")
        db.conn.execute("INSERT OR REPLACE INTO utxo(txid,vout,address,value,height) VALUES (?,?,?,?,?)",
                        (txid, 0, stray, 100_000, 3))
        db.conn.commit()
    _assets(browser, base)
    text = _until(browser, "return document.getElementById('a-stray').textContent", 40)
    for _ in range(40):
        if "sent to" in text:
            break
        browser.execute_script("const b = document.getElementById('a-chain'); b.dispatchEvent(new Event('change'));")
        time.sleep(1)
        text = browser.find_element(By.ID, "a-stray").text
    assert stray in text and "Pepecoin test key" in text, text
    browser.execute_script("document.querySelector('#a-stray button').click()")
    _until(browser, "return !document.querySelector('main [data-panel=\"send\"]').hidden"
                    f" && document.getElementById('w-to').value === '{stray}' && document.getElementById('w-amount').value")


def test_inscribe_opens_the_arcades_inscriber_in_the_window(browser, served, signed_in):
    """Gap 4: Inscribe, the full arcade's own inscriber (_account_inscribe.html on /me/nfts), opened in the window."""
    base, _ = served
    _assets(browser, base)
    browser.find_element(By.ID, "a-inscribe").click()
    _until(browser, "const p = document.querySelector('main [data-panel=\"a-page\"]');"
                    " return p && !p.hidden && p.querySelector('iframe').getAttribute('src') === '/me/nfts#inscribing'")
    browser.switch_to.frame(browser.find_element(By.CSS_SELECTOR, 'main [data-panel="a-page"] iframe'))
    try:
        _until(browser, "return document.getElementById('inscribing') && document.getElementById('inscribe-it')")
        assert browser.execute_script("return document.body.hasAttribute('data-lite')"), "it wears the window's look"
    finally:
        browser.switch_to.default_content()


@pytest.fixture(scope="module")
def node_served(tmp_path_factory, chain):
    """An arcade on a real regtest node, served for Firefox: the operator's own machine, with the node's wallet."""
    import threading
    import uvicorn
    from fastapi.testclient import TestClient
    from test_doginal_wallet_browser import _free_port
    from test_tokens_web import RegtestContext
    from arcade.web import app as app_module
    from arcade.web.app import create_app
    from arcade.web.state import AppState
    node, alice, bob = chain
    app_module._LEDGER_ADDRESSES.clear()
    ledger = RegtestContext(network="regtest", role="ledger", label="Testnet", node=node,
                            activation=node.rpc.get_block_count() + 1, marker=node.rpc.call("getnewaddress"))
    state = AppState(home=tmp_path_factory.mktemp("corenode"), ledger=ledger,
                     messaging=RegtestContext(network="regtest", role="messaging", label="Testnet", node=node))
    app = create_app(state)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error",
                                           timeout_graceful_shutdown=1.0))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    else:
        pytest.skip("test server did not start")
    yield f"http://127.0.0.1:{port}", TestClient(app), state, node, alice, bob
    server.should_exit = True
    thread.join(timeout=5)


def test_the_node_wallets_tokens_and_nfts_are_under_assets_and_its_token_goes_from_send(browser, node_served):
    """Gap 8: the operator's node wallet in ArcadeCore has Assets, as the website's /wallet has: its tokens and NFTs
    (/node/wallet/tokens, /nfts/*), and Send for either, built and signed by the node (/node/wallet/token|nft/send) under a
    ticket, reviewed with the fee and what leaves, and sent only on Send."""
    import re
    from test_tokens_web import mine_and_index
    base, app, state, node, alice, bob = node_served
    form = dict(csrf_token=state.csrf_token, sender=alice, name="Core Node Token", supply="1000", kind="fixed",
                units="divisible")
    page = app.post("/tokens/create", data=form).text
    txid = re.search(r'name="confirmed" value="([0-9a-f]{64})"', page).group(1)
    app.post("/tokens/create", data={**form, "confirmed": txid}, follow_redirects=False)
    mine_and_index(node, state)
    (prop,) = state.token_index(state.ledger).properties()
    # One of the node's NFTs (its index's row, on the node's own address).
    with state.token_index(state.ledger).open() as db:
        db.conn.execute("INSERT OR REPLACE INTO inscription(txid, number, creator, owner, block_height, position,"
                        " content_type, content_len, sha256, json, chunks) VALUES (?,?,?,?,?,?,?,?,?,?,1)",
                        ("ab" * 32, 4242, alice, alice, 1, 0, "image/png", 10, "00" * 32, "{}"))
        db.conn.commit()

    browser.get("about:blank")
    browser.delete_all_cookies()
    _assets(browser, base, "regtest")
    row = _until(browser, "return [...document.querySelectorAll('#a-tokens tr')].find((r) => r.textContent.includes('Core Node Token'))")
    assert "1,000" in row.text, row.text
    assert not browser.find_elements(By.CSS_SELECTOR, "#a-tokens .a-star"), "the node's wallet keeps no favorites"
    _until(browser, "return !document.getElementById('a-nfts-box').hidden && document.querySelector('#a-nfts .a-colls button')")
    browser.execute_script("document.querySelector('#a-nfts .a-colls button').click()")
    fig = _until(browser, f"return document.querySelector('#a-nfts figure[data-piece=\"{'ab' * 32}\"]')")
    assert [b.text for b in fig.find_elements(By.TAG_NAME, "button") if b.text in ("Send", "Sell")] == ["Send"], \
        "the node's NFTs are sent from here; selling is an account's"
    # The NFT: built by the node, reviewed, not sent.
    browser.execute_script("arguments[0].querySelector('.a-acts button').click()", fig)
    _until(browser, "return !document.querySelector('main [data-panel=\"send\"]').hidden"
                    " && document.getElementById('w-asset').textContent === '#4,242'")
    browser.find_element(By.ID, "w-to").send_keys(bob)
    terms = _send_review(browser)
    assert bob in terms and "#4,242" in terms and "Leaves the wallet" in terms, terms
    browser.find_element(By.ID, "w-cancel").click()
    # The token: from Send's own list, reviewed, and sent on Send.
    browser.execute_script("const s = document.getElementById('w-what');"
                           " s.value = [...s.options].find((o) => o.textContent.includes('Core Node Token')).value;"
                           " s.dispatchEvent(new Event('change'))")
    _until(browser, "return document.querySelector('[data-unit]').textContent === 'Core Node Token'")
    browser.find_element(By.ID, "w-to").clear()
    browser.find_element(By.ID, "w-to").send_keys(bob)
    browser.find_element(By.ID, "w-amount").send_keys("250")
    terms = _send_review(browser)
    assert bob in terms and "250 Core Node Token" in terms and "Network fee" in terms and "Leaves the wallet" in terms, terms
    assert node.rpc.call("getrawmempool") == [], "a review sends nothing"
    browser.find_element(By.ID, "w-go").click()
    said = _until(browser, "return !document.getElementById('w-said').hidden && document.getElementById('w-said').textContent")
    assert said.startswith("Sent. Transaction"), said
    mine_and_index(node, state)
    assert state.token_index(state.ledger).balance(bob, prop["property_id"]) == 250 * 10**8

"""ArcadeCore's wallet does what the website's Wallet does, in a real browser (2026-11-08, SadFrog: "there shouldnt be a
differentiation between wallets now it should all be the same thing - when you change the theme, thats it everything is
styled differently but the availability and capability doesnt change", "if things are getting chosen chose the lites
version of things"): imported wallets, the account's own page as a tab that wears the look, the wallets an account
hides, Transactions with sends waiting for their block, what is on its way and older pages, Send's helpers, Backup's
check and recovery page, every address type counted in Balances, and DigiDollar's activity the same in both looks.

The arcade here has the tag chain (regtest), a mainnet chain (main) and DigiByte test, as the DigiDollar wallet test
serves it; coins are written into the index as a block would file them."""

import json
import pathlib
import sys
import time

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import browsers                                                  # noqa: E402
from test_digidollar_wallet_browser import _serve               # noqa: E402

from selenium.webdriver.common.by import By                      # noqa: E402
from selenium.webdriver.support.ui import Select                 # noqa: E402

from arcade import script                                        # noqa: E402

TAG = "paritytester"
PASSWORD = "a password nobody needs to guess for this test"


@pytest.fixture(scope="module")
def browser():
    driver = browsers.launch()
    driver.set_window_size(1200, 950)
    driver.set_script_timeout(120)
    yield driver
    driver.quit()


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    base, state, server, thread = _serve(tmp_path_factory.mktemp("parity"), True)
    yield base, state
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def me(browser, served):
    """One account for the module, signed up and told to the node, with its words (Backup checks them)."""
    base, _ = served
    browser.get(f"{base}/join")
    browser.delete_all_cookies()
    got = browser.execute_async_script("""
        const done = arguments[2];
        import("/wallet.js").then(async (w) => {
          const made = await w.signUp(arguments[0], arguments[1], {network: "regtest", version: 111});
          const me = await w.opened({network: "regtest", version: 111});
          await w.tellTheNode(me);
          const said = await w.state();
          done({ok: true, phrase: (made.wallet && made.wallet.phrase) || made.phrase || "", pubkey: said.pubkey,
                addresses: Object.fromEntries((said.chains || []).map((c) => [c.network, c.address]))});
        }).catch((e) => done({ok: false, error: String(e.message || e)}));
        """, TAG, PASSWORD)
    assert got.get("ok"), got
    return got


def _until(browser, check, what, seconds=60):
    until = time.time() + seconds
    while time.time() < until:
        try:
            if check():
                return
        except Exception:                                   # noqa: BLE001 -- not drawn yet
            pass
        time.sleep(0.25)
    raise AssertionError(f"{what}\n{browser.find_element(By.TAG_NAME, 'body').text[:2000]}")


def _el(browser, id_):
    """The element with that id, once a form drawn after an answer has it."""
    _until(browser, lambda: browser.find_elements(By.ID, id_), f"#{id_} is drawn")
    return browser.find_element(By.ID, id_)


def _lite(browser, base, tab="send", query=""):
    browser.get("about:blank")                                  # a page of its own, never only a new #hash
    browser.get(f"{base}/lite{query}#{tab}")
    _until(browser, lambda: browser.execute_script(
        "return [...document.querySelectorAll('#w-chain-send option')].some(o => o.value === 'regtest')"),
        "the wallets are read")


def _pick_chain(browser, network):
    Select(browser.find_element(By.ID, "w-chain-send")).select_by_value(network)
    _until(browser, lambda: browser.find_element(By.ID, "w-chain-send").get_attribute("value") == network,
           f"{network} is picked")


def _which(browser, box="w-which-send"):
    return browser.execute_script(
        "const b = document.getElementById(arguments[0]); return {value: b.value, "
        "options: [...b.options].map(o => o.text)}", box)


def _stub(browser, routes: dict):
    """Answer fetches whose URL starts with a key from here (the value: JSON, or a list of JSON answered in turn),
    and keep each URL asked in window.asked."""
    browser.execute_script("""
        const routes = arguments[0], real = window.fetch;
        window.asked = [];
        window.fetch = (url, opts) => {
          const u = String(url);
          for (const [start, answer] of Object.entries(routes)) {
            if (u.startsWith(start)) {
              window.asked.push(u + (opts && opts.body ? " " + opts.body : ""));
              const one = Array.isArray(answer) ? (answer.length > 1 ? answer.shift() : answer[0]) : answer;
              return Promise.resolve(new Response(JSON.stringify(one.body || one),
                {status: one.status || 200, headers: {"Content-Type": "application/json"}}));
            }
          }
          return real(url, opts);
        };""", routes)


def _wif(key: bytes) -> str:
    return script.b58check_encode(0xf1, key + b"\x01")             # the tag chain's (regtest)


def _password_if_asked(browser, box_id, go_id):
    _until(browser, lambda: not browser.find_elements(By.ID, go_id)
           or browser.find_element(By.ID, box_id).is_displayed()
           or browser.find_element(By.ID, "w-wproblem").is_displayed(), "done or asked for the password")
    if browser.find_elements(By.ID, go_id) and browser.find_element(By.ID, box_id).is_displayed():
        browser.find_element(By.ID, box_id).send_keys(PASSWORD)
        browser.find_element(By.ID, go_id).click()
    _until(browser, lambda: not browser.find_elements(By.ID, go_id),
           "finished: " + browser.execute_script("return (document.getElementById('w-wproblem') || {}).textContent || ''"))


def test_arcadecore_imports_a_wallet_sends_from_it_renames_and_removes_it(browser, served, me):
    """Plan item 18 in ArcadeCore: the Wallets window imports a private key, From picks it in Send, Receive and
    Transactions, Balances lists it, Send asks the node for a send from its address, a rename shows in the website's
    Wallet too (one pick, one name, both looks), and Remove takes it off the list."""
    base, state = served
    _lite(browser, base)
    _pick_chain(browser, "regtest")
    assert browser.find_element(By.ID, "w-which-send").is_displayed()
    browser.find_element(By.CSS_SELECTOR, '[data-panel="send"] [data-wallets]').click()
    _until(browser, lambda: browser.find_element(By.ID, "w-wallets").is_displayed(), "the Wallets window opens")
    browser.find_element(By.ID, "w-wimport").click()
    Select(_el(browser, "w-imp-by")).select_by_value("wif")
    wif = _wif(bytes.fromhex("11" * 32))
    browser.find_element(By.ID, "w-imp-key").send_keys(wif)
    browser.find_element(By.ID, "w-imp-go").click()
    _password_if_asked(browser, "w-imp-pass", "w-imp-go")
    want = browser.execute_async_script("""
        const done = arguments[1];
        import("/coins.js").then(async (c) => done(await c.address((await c.fromWif(arguments[0], "regtest")).pubkey, 111)));
        """, wif)
    _until(browser, lambda: _which(browser)["options"][-1].startswith("Wallet "), "the import is listed")
    picked = _which(browser)
    assert picked["value"] != "main" and len(picked["options"]) == 2, picked
    assert "imported" in browser.find_element(By.ID, "w-wstate").text, "said in the window"
    rows = browser.find_elements(By.CSS_SELECTOR, "#w-wlist tr")
    assert len(rows) == 2 and rows[1].get_attribute("aria-selected") == "true", [r.text for r in rows]
    browser.find_element(By.CSS_SELECTOR, "#w-wallets [data-close]").click()

    # Receive shows its own address; Balances lists it under its chain.
    browser.find_element(By.CSS_SELECTOR, '.toolbar [data-tab="receive"]').click()
    _until(browser, lambda: browser.find_element(By.ID, "w-address").text == want, "Receive shows the imported address")
    assert _which(browser, "w-which-receive")["value"] == picked["value"]
    browser.find_element(By.CSS_SELECTOR, '.toolbar [data-tab="send"]').click()
    _until(browser, lambda: "Wallet " in browser.find_element(By.ID, "w-ibalances").text, "Balances lists it")

    # Send: the node is asked for a send FROM the imported address.
    _stub(browser, {"/account/send": {"status": 400, "body": {"detail": "no coins to send yet"}}})
    browser.find_element(By.ID, "w-to").send_keys("@someone")
    browser.find_element(By.ID, "w-amount").send_keys("1")
    browser.find_element(By.CSS_SELECTOR, '#w-form button[type="submit"]').click()
    _until(browser, lambda: "no coins to send yet" in browser.find_element(By.ID, "w-said").text, "the node answered")
    asked = browser.execute_script("return window.asked")
    assert asked and json.loads(asked[0].split(" ", 1)[1])["from"] == want, asked

    # Renamed here, it is renamed in the website's Wallet, which shows the same wallet picked.
    browser.find_element(By.CSS_SELECTOR, '[data-panel="send"] [data-wallets]').click()
    browser.find_element(By.ID, "w-wrename").click()
    box = _el(browser, "w-ren-to")
    box.clear()
    box.send_keys("Savings")
    browser.find_element(By.ID, "w-ren-go").click()
    _password_if_asked(browser, "w-ren-pass", "w-ren-go")
    _until(browser, lambda: _which(browser)["options"][-1].startswith("Savings"), "renamed")
    browser.execute_script("localStorage.setItem(arguments[0], JSON.stringify('regtest'))", f"arcade.wallet.net.{TAG}")
    browser.get(f"{base}/me/wallet")
    _until(browser, lambda: browser.execute_script("return document.body.dataset.walletReady === 'yes'"), "the Wallet")
    _until(browser, lambda: browser.find_element(By.ID, "wpickname").text.startswith("Savings"),
           "the website's Wallet has the same wallet picked, by its new name")

    # Removed from the list in ArcadeCore.
    _lite(browser, base)
    _pick_chain(browser, "regtest")
    browser.find_element(By.CSS_SELECTOR, '[data-panel="send"] [data-wallets]').click()
    _until(browser, lambda: browser.find_element(By.ID, "w-wallets").is_displayed(), "the Wallets window opens")
    browser.find_element(By.ID, "w-wremove").click()
    _el(browser, "w-rem-go").click()
    _password_if_asked(browser, "w-rem-pass", "w-rem-go")
    _until(browser, lambda: len(_which(browser)["options"]) == 1 and _which(browser)["value"] == "main", "removed")


def test_arcadecore_transactions_pin_sends_show_whats_on_its_way_and_older_pages(browser, served, me):
    """Transactions as the website lists them: a send this browser made waits on top until its block lands (kept under
    the website's own name for it), the pool's arrivals are under "On its way", and "Show older" reads the next page."""
    base, _ = served
    browser.get(f"{base}/lite")
    browser.execute_script("""localStorage.setItem(arguments[0], JSON.stringify([{txid: "ab".repeat(32), network: "regtest",
        from: "", to: "mxwhoever", tag: "carol", amount: 25000000, fee: 1000, at: Date.now(), what: ""}]));
        localStorage.removeItem(arguments[1]);""", f"arcade.wallet.sent.{TAG}", f"arcade.wallet.pick.{TAG}")
    _lite(browser, base, "transactions")
    _pick_chain(browser, "regtest")
    row = lambda h, n: {"txid": f"{n:064x}", "height": h, "time": 1700000000 + h, "net": 100000, "confirmations": 3}
    _stub(browser, {
        "/account/activity": [{"rows": [row(200 - i, i + 1) for i in range(50)], "more": True, "since": 1, "tip": 202},
                              {"rows": [row(140, 999)], "more": False, "since": 1, "tip": 202}],
        "/r/pending/": {"incoming": [{"kind": "token", "amount": 5, "name": "PEPE"}], "outgoing": []},
    })
    browser.find_element(By.CSS_SELECTOR, '.toolbar [data-tab="transactions"]').click()
    _until(browser, lambda: len(browser.find_elements(By.CSS_SELECTOR, "#w-history tr")) == 51, "50 rows and the pending send")
    first = browser.find_element(By.CSS_SELECTOR, "#w-history tr").text
    assert "waiting for a block" in first and "@carol" in first, first
    _until(browser, lambda: "5 PEPE" in browser.find_element(By.ID, "w-way").text, "On its way lists the arrival")
    assert browser.find_element(By.ID, "w-way-box").is_displayed()
    browser.find_element(By.ID, "w-older").click()
    _until(browser, lambda: len(browser.find_elements(By.CSS_SELECTOR, "#w-history tr")) == 52, "the older page is added")
    asked = [u for u in browser.execute_script("return window.asked") if u.startswith("/account/activity")]
    assert "before=151" in asked[-1], asked
    assert not browser.find_element(By.ID, "w-older").is_displayed(), "no more pages"

    # Its block lands: the pin goes, and it is said once.
    landed = {"rows": [{"txid": "ab" * 32, "height": 203, "time": 1700000300, "net": -25001000, "confirmations": 1}],
              "more": False, "since": 1, "tip": 203}
    _stub(browser, {"/account/activity": landed, "/r/pending/": {"incoming": [], "outgoing": []}})
    browser.find_element(By.CSS_SELECTOR, '.toolbar [data-tab="transactions"]').click()
    _until(browser, lambda: "is in a block" in browser.find_element(By.ID, "w-landed").text, "the landing is said")
    assert "waiting" not in browser.find_element(By.ID, "w-history").text
    assert json.loads(browser.execute_script("return localStorage.getItem(arguments[0])",
                                             f"arcade.wallet.sent.{TAG}"))[0]["landed"] is True


def test_arcadecore_send_suggests_names_reads_a_qr_and_takes_a_pay_link(browser, served, me):
    """Send's helpers: Pay to suggests the @names this browser paid and saved; Scan QR reads a payment code (here from
    a photo, the way a browser with no camera reads one) into To and Amount; and a Pay link (?to=@name) fills To."""
    base, _ = served
    _lite(browser, base, "send", "?to=@dave")
    _until(browser, lambda: browser.find_element(By.ID, "w-to").get_attribute("value") == "@dave", "the Pay link fills To")
    assert "to=" not in browser.current_url, "the link leaves the address bar"

    browser.execute_script("""localStorage.setItem(arguments[0], JSON.stringify([{txid: "cd".repeat(32), network: "regtest",
        from: "", to: "mxsomeone", tag: "erin", amount: 1, fee: 1, at: Date.now(), what: "", landed: true}]))""",
                           f"arcade.wallet.sent.{TAG}")
    _pick_chain(browser, "regtest")
    to = browser.find_element(By.ID, "w-to")
    to.clear()
    browser.find_element(By.ID, "w-amount").click()
    to.click()
    _until(browser, lambda: "@erin" in browser.execute_script(
        "return [...document.querySelectorAll('#w-people option')].map(o => o.value)"), "Pay to suggests @erin")

    # A payment code in a photo: the scanner's own reader (jsQR) reads it into the form.
    browser.find_element(By.ID, "w-scan").click()
    _until(browser, lambda: browser.find_elements(By.CSS_SELECTOR, ".wl-scanbox input[type=file]"), "the scanner opens")
    # The photo is handed to the scanner's own file box as a picked file would be.
    browser.execute_async_script("""const done = arguments[0];
        const q = qrcode(0, "M"); q.addData("pepecoin:mxpaidhere?amount=2.5"); q.make();
        fetch(q.createDataURL(8, 4)).then((r) => r.blob()).then((blob) => {
          const box = document.querySelector(".wl-scanbox input[type=file]"), dt = new DataTransfer();
          dt.items.add(new File([blob], "code.gif", {type: "image/gif"}));
          box.files = dt.files; box.dispatchEvent(new Event("change")); done();
        });""")
    _until(browser, lambda: browser.find_element(By.ID, "w-to").get_attribute("value") == "mxpaidhere", "To is read")
    assert browser.find_element(By.ID, "w-amount").get_attribute("value") == "2.5"
    assert not browser.find_elements(By.CSS_SELECTOR, ".wl-scanbox")


def test_arcadecore_backup_checks_the_words_and_offers_the_recovery_page(browser, served, me):
    """Backup as the website's: not backed up is said (on Send too), the words typed back are checked here against the
    account's own mainnet address and only the answer reaches the arcade, and the recovery page can be saved."""
    base, _ = served
    if not me["phrase"]:
        pytest.skip("signUp did not hand back the words")
    browser.execute_script("sessionStorage.removeItem('arcade.wallet.backupx')")
    _lite(browser, base, "send")
    _until(browser, lambda: browser.find_element(By.ID, "w-backup").is_displayed(), "Send says the words are not backed up")
    browser.find_element(By.ID, "w-backup-go").click()
    _until(browser, lambda: browser.find_element(By.ID, "k-not").is_displayed(), "Backup says so too")
    assert browser.find_element(By.ID, "k-recover").get_attribute("href").endswith("/recover")
    browser.find_element(By.ID, "k-check-words").send_keys("abandon " * 11 + "about")
    browser.find_element(By.ID, "k-check").click()
    _until(browser, lambda: "not the same account" in browser.find_element(By.ID, "k-check-said").text, "wrong words said")
    browser.find_element(By.ID, "k-check-words").clear()
    browser.find_element(By.ID, "k-check-words").send_keys(me["phrase"])
    browser.find_element(By.ID, "k-check").click()
    _until(browser, lambda: browser.find_element(By.ID, "k-done").is_displayed(), "backed up")
    assert not browser.find_element(By.ID, "k-not").is_displayed()
    _lite(browser, base, "send")
    time.sleep(1)
    assert not browser.find_element(By.ID, "w-backup").is_displayed(), "Send no longer says it"


def test_arcadecore_has_the_account_tab_and_hides_what_the_account_hides(browser, served, me):
    """The account's own page (/me: @name, key, profile, background, which wallets show, boosts, sign out everywhere)
    is ArcadeCore's Account tab, wearing the look; a wallet hidden there is left out of Send, Receive and Balances as the
    website's Wallet leaves it out; and on an arcade that opens in ArcadeCore, /me is that tab."""
    base, state = served
    _lite(browser, base, "send")
    assert "main" in browser.execute_script("return [...document.querySelectorAll('#w-chain-send option')].map(o => o.value)")
    browser.find_element(By.XPATH, "//nav[@id='menubar']//button[text()='Settings']").click()
    browser.find_element(By.XPATH, "//button[text()='Your account…']").click()
    _until(browser, lambda: browser.find_element(By.CSS_SELECTOR, '[data-panel="account"] iframe').get_attribute("src")
           .endswith("/me"), "the Account tab opens /me")
    browser.switch_to.frame(browser.find_element(By.CSS_SELECTOR, '[data-panel="account"] iframe'))
    try:
        _until(browser, lambda: browser.execute_script(
            "return document.documentElement.classList.contains('in-window') && document.body.hasAttribute('data-lite')"),
            "the page wears the window's look")
        sheets = browser.execute_script("return [...document.querySelectorAll('link[rel=stylesheet]')].map(l => l.href)")
        assert any(s.endswith("/lite/pages.css") for s in sheets) and any(s.endswith("/lite/theme.css") for s in sheets)
        # Hide the mainnet wallet from here, as Your wallets does.
        hid = browser.execute_async_script("""const done = arguments[0];
            fetch("/account/wallets", {method: "POST", headers: {"Content-Type": "application/json"},
              body: JSON.stringify({network: "main", shown: false})}).then((r) => done(r.status));""")
        assert hid == 200, hid
    finally:
        browser.switch_to.default_content()
    browser.find_element(By.CSS_SELECTOR, '.toolbar [data-tab="send"]').click()
    _until(browser, lambda: "main" not in browser.execute_script(
        "return [...document.querySelectorAll('#w-chain-send option')].map(o => o.value)"), "the hidden wallet goes")
    assert "Mainnet" not in browser.find_element(By.ID, "w-balances").text
    browser.execute_async_script("""const done = arguments[0];
        fetch("/account/wallets", {method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({network: "main", shown: true})}).then(() => done());""")

    from arcade import modules as modulelib
    was = state.setting(modulelib.LOOK_SETTING)
    state.set_setting(modulelib.LOOK_SETTING, "core")
    try:
        browser.get(f"{base}/me")
        _until(browser, lambda: browser.find_element(By.CSS_SELECTOR, '[data-panel="account"]').is_displayed(),
               "/me is the window's Account tab")
        assert browser.find_element(By.CSS_SELECTOR, '.toolbar [data-tab="account"]').get_attribute("aria-selected") == "true"
    finally:
        state.set_setting(modulelib.LOOK_SETTING, was)


def test_balances_count_every_address_type_and_the_pick_is_one_for_both_looks(browser, served, me):
    """Every address type an account holds coins on is counted in Balances (/account `kinds`), and the address type
    picked in ArcadeCore is the one the website's Wallet shows (one pick, kept under ArcadeCore's name for it)."""
    base, state = served
    chain = state.more["dgbtest"]
    own, segwit = me["addresses"]["dgbtest"], "dgbt1qparityaddressforthisaccount0000000"
    state.set_setting(f"segwit_watch:{me['pubkey']}:dgbtest", segwit)
    with state.token_index(chain).open() as db:
        for address in (own, segwit):
            db.conn.execute("INSERT INTO watched (address, since) VALUES (?, 1) ON CONFLICT DO NOTHING", (address,))
        db.conn.execute("INSERT INTO utxo VALUES (?,?,?,?,?)", ("e1" * 32, 0, own, 100_000_000, 5))
        db.conn.execute("INSERT INTO utxo VALUES (?,?,?,?,?)", ("e2" * 32, 0, segwit, 50_000_000, 5))
        db.conn.commit()
    got = browser.execute_async_script("""const done = arguments[0];
        fetch("/account?chains=dgbtest").then((r) => r.json()).then((j) => done(j.chains.find((c) => c.network === "dgbtest")));""")
    assert got["kinds"] == [{"kind": "p2wpkh", "address": segwit, "balance": 50_000_000}], got
    browser.execute_script("Object.keys(localStorage).filter(k => k.startsWith('lite_balances_')).forEach(k => localStorage.removeItem(k))")
    _lite(browser, base, "send")

    def dgb_row():
        for tr in browser.find_elements(By.CSS_SELECTOR, "#w-balances tr"):
            if tr.text.startswith("DigiByte testnet"):
                return tr
    _until(browser, lambda: "1.5 DGB" in dgb_row().text, "Balances counts the SegWit coins with the Legacy ones")
    assert "Native SegWit 0.5" in dgb_row().find_elements(By.TAG_NAME, "td")[1].get_attribute("data-parts")

    # The address type picked here is the website's pick too.
    _pick_chain(browser, "dgbtest")
    _until(browser, lambda: browser.find_element(By.ID, "w-kind-send").is_displayed(), "the address type row")
    Select(browser.find_element(By.ID, "w-kind-send")).select_by_value("p2wpkh")
    assert browser.execute_script("return localStorage.getItem(arguments[0])", f"lite_kind_{TAG}_dgbtest") == "p2wpkh"
    browser.get(f"{base}/me/wallet?coin=dgbtest")
    _until(browser, lambda: browser.execute_script("return document.body.dataset.walletReady === 'yes'"), "the Wallet")
    _until(browser, lambda: "Native SegWit" in browser.find_element(By.ID, "wpickname").text,
           "the website's Wallet shows the address type picked in ArcadeCore")


def test_digidollar_activity_is_the_coins_held_in_both_looks(browser, served, me):
    """DigiDollar's Transactions: the DigiDollars held, coin by coin, from the arcade's index, in ArcadeCore and on the
    website's Wallet alike (ArcadeCore's way)."""
    base, state = served
    from test_digidollar_wallet_browser import PILOT, _pilot
    from arcade.lite import commands as cmdlib
    op = cmdlib.Caller(state, "operator", local=True)
    cmdlib.run_line(op, f"installmodule {PILOT}")
    cmdlib.run_line(op, "install digidollar")
    _, pilot = _pilot()
    pub = bytes.fromhex(browser.execute_async_script("""const done = arguments[0];
        import("/wallet.js").then(async (w) => {
          const me = await w.opened({network: "regtest", version: 111});
          done(Array.from(w.keysOn(me, "dgbtest").pubkey, (b) => b.toString(16).padStart(2, "0")).join(""));
        });"""))
    dd = pilot.dd_address("dgbtest", pub[1:])
    with state.token_index(state.more["dgbtest"]).open() as db:
        db.conn.execute("INSERT INTO dd_watched (address, since, seeded) VALUES (?, 1, 1) ON CONFLICT DO NOTHING", (dd,))
        db.conn.execute("INSERT INTO dd_coin VALUES (?,?,?,?,?)", ("d7" * 32, 1, dd, 4250, 7))
        db.conn.commit()
    browser.execute_script("localStorage.removeItem(arguments[0])", f"arcade.wallet.dd.{TAG}")
    browser.get(f"{base}/me/wallet?coin=dgbtest")
    _until(browser, lambda: browser.execute_script("return document.body.dataset.walletReady === 'yes'"), "the Wallet")
    browser.find_element(By.ID, "coinpick").click()
    browser.find_element(By.CSS_SELECTOR, '#nets button[data-dd="1"]').click()
    _until(browser, lambda: browser.find_elements(By.CSS_SELECTOR, f'#panel-activity [data-ddcoin="{"d7" * 32}"]'),
           "the website lists the coin held")
    assert "$42.50" in browser.find_element(By.ID, "panel-activity").text

    browser.get(f"{base}/lite#transactions")
    _until(browser, lambda: browser.execute_script(
        "return [...document.querySelectorAll('#w-chain-tx option')].some(o => o.value === 'dd:dgbtest')"), "the wallets")
    Select(browser.find_element(By.ID, "w-chain-tx")).select_by_value("dd:dgbtest")
    _until(browser, lambda: "$42.50" in browser.find_element(By.ID, "w-history").text, "ArcadeCore lists the same coin")
    assert "Held" in browser.find_element(By.ID, "w-history").text

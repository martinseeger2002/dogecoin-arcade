"""Signing up the way a person does: a name, a password, and a wallet.

The claim being tested is that those two things are the whole of it, and
that nothing about the result is custodial: the node never sees the
password, never sees the words, and holds bytes it cannot read.
"""

import json
import pathlib
import socket
import sys
import threading
import time

import pytest

pytest.importorskip("selenium",
                    reason="browser tests need selenium: pip install .[dev]")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import browsers                                                  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def browser():
    driver = browsers.launch()
    yield driver
    driver.quit()


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    import uvicorn
    from pathlib import Path
    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    home = tmp_path_factory.mktemp("signup")
    state = AppState(
        home=home,
        messaging=ChainContext(network="regtest", role="messaging",
                               label="Testnet", datadir=Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                            datadir=Path("/nonexistent")),
    )
    port = _free_port()
    config = uvicorn.Config(create_app(state), host="127.0.0.1", port=port,
                            log_level="error", timeout_graceful_shutdown=1.0)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    else:
        pytest.skip("test server did not start")
    yield f"http://127.0.0.1:{port}", state, home
    server.should_exit = True
    thread.join(timeout=5)
    assert not thread.is_alive(), "the server is still serving a keep-alive connection"


@pytest.fixture(scope="module")
def loaded(browser, served):
    base, state, home = served
    browser.get(f"{base}/join")
    browser.set_script_timeout(90)
    ready = browser.execute_async_script("""
        const done = arguments[arguments.length - 1];
        import("/wallet.js").then((m) => { window.w = m; done(true); },
                                  (e) => done(String(e)));""")
    assert ready is True, ready
    return browser, base, state, home


REGTEST_VERSION = 111          # regtest addresses start with m or n


def _sign_up(browser, tag, password="correct horse battery"):
    return browser.execute_async_script("""
        const done = arguments[3];
        window.w.signUp(arguments[0], arguments[1],
                        {network: "regtest", version: arguments[2]})
          .then((r) => done({tag: r.tag, address: r.address,
                             pubkey: r.pubkey, phrase: r.wallet.phrase}),
                (e) => done({error: String(e.message || e)}));""",
        tag, password, REGTEST_VERSION)


def test_a_name_and_a_password_make_a_wallet(loaded):
    browser, base, state, home = loaded
    said = _sign_up(browser, "robin")
    assert "error" not in said, said
    assert said["tag"] == "robin"
    assert len(said["phrase"].split()) == 12, "twelve words, not twenty-four"
    assert said["address"], "and an address of their own"
    assert said["pubkey"] and len(said["pubkey"]) == 64

    # And they are signed in, without a second step.
    who = browser.execute_async_script("""
        const done = arguments[0];
        fetch('/auth/who').then(r => r.json()).then(done);""")
    assert who["pubkey"] == said["pubkey"]


def test_the_node_never_sees_the_password_or_the_words(loaded):
    """The whole claim. What is on the node is ciphertext and parameters."""
    browser, base, state, home = loaded
    said = _sign_up(browser, "quiet", password="a long enough password")
    assert "error" not in said, said

    row = state.vault().get("quiet")
    assert row is not None
    blob = json.loads(row["blob"])
    assert set(blob) == {"kdf", "iterations", "salt", "nonce", "sealed"}
    assert blob["iterations"] >= 600000
    assert "a long enough password" not in row["blob"]
    for word in said["phrase"].split():
        assert word not in row["blob"], "a word of the phrase is in the blob"

    raw = (home / "accounts.sqlite").read_bytes()
    assert b"a long enough password" not in raw
    assert said["phrase"].encode() not in raw


def test_the_same_name_and_password_open_it_again(loaded):
    """On this browser or any other: the blob comes from the node and the
    password opens it here."""
    browser, base, state, home = loaded
    made = _sign_up(browser, "returning")
    assert "error" not in made, made

    browser.delete_all_cookies()
    back = browser.execute_async_script("""
        const done = arguments[3];
        window.w.signIn(arguments[0], arguments[1],
                        {network: "regtest", version: arguments[2]})
          .then((r) => done({tag: r.tag, address: r.address,
                             pubkey: r.pubkey, phrase: r.wallet.phrase}),
                (e) => done({error: String(e.message || e)}));""",
        "returning", "correct horse battery", REGTEST_VERSION)
    assert "error" not in back, back
    assert back["pubkey"] == made["pubkey"], "the same account"
    assert back["address"] == made["address"], "and the same coins"
    assert back["phrase"] == made["phrase"], "and the same words"


def test_a_wrong_password_opens_nothing_and_says_so(loaded):
    browser, base, state, home = loaded
    _sign_up(browser, "guarded")
    browser.delete_all_cookies()
    said = browser.execute_async_script("""
        const done = arguments[3];
        window.w.signIn(arguments[0], arguments[1],
                        {network: "regtest", version: arguments[2]})
          .then((r) => done({ok: true}),
                (e) => done({error: String(e.message || e)}));""",
        "guarded", "not the password", REGTEST_VERSION)
    assert "does not open this wallet" in said["error"]


def test_a_name_somebody_has_is_refused_before_a_wallet_is_made(loaded):
    browser, base, state, home = loaded
    _sign_up(browser, "onlyone")
    again = _sign_up(browser, "ONLYONE")
    assert "already signed up" in again["error"]


def test_a_short_password_is_allowed_and_the_page_says_what_it_costs(loaded):
    """Somebody who wants `1234` gets `1234` (D-157). What they also get is
    a sentence saying their encrypted wallet can be taken from this node
    and opened in seconds -- because that is true, and refusing them would
    not have made it less so."""
    browser, base, state, home = loaded
    said = _sign_up(browser, "brief", password="1234")
    assert "error" not in said, said
    assert state.vault().get("brief") is not None, "the account exists"

    told = browser.execute_async_script("""
        const done = arguments[0];
        done({weak: window.w.strength("1234"),
              middling: window.w.strength("correct horse"),
              strong: window.w.strength("correct horse battery staple!")});""")
    assert told["weak"]["ok"] is False
    assert told["weak"]["level"] == "instantly"
    assert "in seconds" in told["weak"]["says"]
    assert "nothing here will stop you" in told["weak"]["says"]
    assert told["strong"]["ok"] is True


def test_no_password_at_all_is_still_refused(loaded):
    browser, base, state, home = loaded
    said = _sign_up(browser, "empty", password="")
    assert "even a short one" in said["error"]
    assert state.vault().get("empty") is None


def test_a_free_name_can_be_checked_before_typing_a_password(loaded):
    browser, base, state, home = loaded
    _sign_up(browser, "spoken")
    said = browser.execute_async_script("""
        const done = arguments[0];
        Promise.all([window.w.available("spoken"),
                     window.w.available("unspoken"),
                     window.w.available("Not A Name")]).then(done);""")
    taken, free, bad = said
    assert taken["free"] is False and taken["here"] is True
    assert free["free"] is True
    assert bad["free"] is False, "and a name that is not a name is not free"


def test_the_address_is_this_chain_s_and_the_browser_derived_it(loaded):
    """The node is told the address, not asked for it: a node that answered
    could answer with somebody else's, and the coins would be unreachable."""
    from arcade.script import b58check_encode, hash160

    browser, base, state, home = loaded
    said = _sign_up(browser, "derived")
    assert said["address"][0] in "mn", "a regtest address"
    checked = browser.execute_async_script("""
        const done = arguments[2];
        (async () => {
          const signer = await import("/signin.js");
          const coins = await import("/coins.js");
          const seed = await signer.toSeed(arguments[0]);
          const key = await coins.coinKey(seed, "regtest", 0);
          done(await coins.address(key.pubkey, arguments[1]));
        })();""", said["phrase"], REGTEST_VERSION)
    assert checked == said["address"], "the words alone rebuild the address"


# --- the page, not the module -------------------------------------------------

def _ready(browser, base):
    """Wait for the signup page to wire itself up."""
    from selenium.webdriver.common.by import By        # noqa: F401

    browser.get(f"{base}/join")
    for _ in range(120):
        if browser.execute_script(
                "return document.body.dataset.signupReady === 'yes'"):
            return
        time.sleep(0.25)
    raise AssertionError("the signup page never became usable")


def test_the_page_asks_for_two_things(loaded):
    from selenium.webdriver.common.by import By

    browser, base, state, home = loaded
    _ready(browser, base)
    assert browser.find_element(By.ID, "tag").is_displayed()
    assert browser.find_element(By.ID, "pw").is_displayed()
    assert browser.find_element(By.ID, "make").is_displayed()
    # And nothing about keys, seeds or derivation paths.
    body = browser.page_source.lower()
    assert "seed phrase" not in body.split("write these down")[0]
    assert "derivation" not in body and "secp256k1" not in body


def test_a_name_is_checked_as_it_is_typed(loaded):
    """A name refused at the end of a form is a form filled in twice."""
    from selenium.webdriver.common.by import By

    browser, base, state, home = loaded
    _ready(browser, base)
    browser.find_element(By.ID, "tag").send_keys("freshname")
    said = browser.find_element(By.ID, "tag-said")
    for _ in range(40):
        if "free" in said.text:
            break
        time.sleep(0.25)
    assert "@freshname is free" in said.text


def test_making_an_account_shows_the_words_once_and_asks(loaded):
    from selenium.webdriver.common.by import By

    browser, base, state, home = loaded
    _ready(browser, base)
    browser.find_element(By.ID, "tag").send_keys("paperwork")
    browser.find_element(By.ID, "pw").send_keys("a long enough password")
    browser.find_element(By.ID, "make").click()

    for _ in range(80):
        if browser.find_element(By.ID, "written").is_displayed():
            break
        time.sleep(0.25)
    assert browser.find_element(By.ID, "written").is_displayed()
    words = browser.find_elements(By.CSS_SELECTOR, "#words li")
    assert len(words) == 12, "twelve words, shown once"

    # The way in is closed until they say they have written them down.
    go = browser.find_element(By.ID, "done")
    assert go.get_attribute("disabled"), "not until they say so"
    browser.find_element(By.ID, "kept").click()
    assert not go.get_attribute("disabled")

    assert state.vault().get("paperwork") is not None, "and the account exists"


def test_a_taken_name_is_refused_on_the_page(loaded):
    from selenium.webdriver.common.by import By

    browser, base, state, home = loaded
    _ready(browser, base)
    browser.find_element(By.ID, "tag").send_keys("paperwork")
    said = browser.find_element(By.ID, "tag-said")
    for _ in range(40):
        if "signed up" in said.text:
            break
        time.sleep(0.25)
    assert "already signed up" in said.text


def test_a_new_account_is_told_about_its_coins(loaded):
    """The faucet runs at signup, and the page says what happened either
    way -- there being no node here, it says why not rather than nothing."""
    from selenium.webdriver.common.by import By

    browser, base, state, home = loaded
    _ready(browser, base)
    browser.find_element(By.ID, "tag").send_keys("thirsty")
    browser.find_element(By.ID, "pw").send_keys("a long enough password")
    browser.find_element(By.ID, "make").click()
    for _ in range(80):
        if browser.find_element(By.ID, "written").is_displayed():
            break
        time.sleep(0.25)
    gift = browser.find_element(By.ID, "gift")
    assert gift.is_displayed() and gift.text, \
        "the page says something about coins either way"
    # The account exists whether or not the faucet could pay.
    assert state.vault().get("thirsty") is not None


def test_a_file_and_a_password_open_a_wallet_this_node_never_held(
        loaded, tmp_path):
    """The new-device path, which is the reason the blob exists at all.

    The ciphertext is made in the browser because it is WebCrypto and the
    node could not make one if it wanted to -- that is the design, not an
    accident -- so the file is written out here from what the browser
    produced and handed back through the picker, which is what somebody who
    changed computers actually has. Typing the words is not in this test
    because they are not typed on this path, and a node that could be handed
    them would be the hole the path exists to close.
    """
    from selenium.webdriver.common.by import By

    from arcade import seed

    browser, base, state, home = loaded
    # This module's node already holds the other tests' accounts, and a seat
    # refused for that reason would be a test about the fixture.
    state.accounts().seats = max(state.accounts().seats, 40)
    _ready(browser, base)

    phrase = seed.generate()
    pubkey = seed.login_pubkey(phrase)
    # Imported here rather than off `window.w`: the fixture put that there on
    # a page this test has since navigated away from, and a module object does
    # not survive the navigation.
    blob = browser.execute_async_script("""
        const done = arguments[2];
        import("/wallet.js").then((m) => m.seal(arguments[0], arguments[1]))
          .then(done, (e) => done({error: String(e.message || e)}));""",
        phrase, "a long enough password")
    assert "error" not in blob, blob

    # The shape the Backup page actually saves, which is this node's whole
    # answer to GET /signin/{name} rather than the bare blob.
    path = tmp_path / "dogecoinarcade-stranger.json"
    path.write_text(json.dumps({"tag": "stranger", "pubkey": pubkey,
                               "address": "", "blob": blob}, indent=2))

    free = state.accounts().free()
    _ready(browser, base)
    browser.find_element(By.ID, "backup-file").send_keys(str(path))
    browser.find_element(By.ID, "backup-pw").send_keys("a long enough password")
    browser.find_element(By.ID, "open-file").click()
    for _ in range(120):
        if browser.current_url.rstrip("/") == base:
            break
        time.sleep(0.25)
    assert browser.current_url.rstrip("/") == base, \
        "it takes them in the way a sign-in does"

    who = browser.execute_async_script("""
        const done = arguments[0];
        fetch('/auth/who').then((r) => r.json()).then(done);""")
    assert who["pubkey"] == pubkey, "the key in the file, not one made here"
    assert state.accounts().free() == free - 1, "the words took a seat"
    assert state.vault().by_pubkey(pubkey) is None, \
        "this node was shown the wallet, not given it"

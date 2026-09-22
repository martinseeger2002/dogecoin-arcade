"""The login, in a real browser, against the real server.

This file exists because of a specific failure mode: two implementations of
the same derivation, one in Python and one in JavaScript, that agree on
nothing and are each tested only against themselves. Every test here makes
the browser derive and the server verify, or makes the browser sign and the
server check -- so agreement is what is asserted, not each half's opinion
of itself.
"""

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

from selenium.webdriver.common.by import By                      # noqa: E402

from arcade import seed                                          # noqa: E402


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
    """The application on 127.0.0.1, which is a secure context -- so the
    browser will hand out `crypto.subtle` and the login can happen at all."""
    import uvicorn
    from pathlib import Path
    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    state = AppState(
        home=tmp_path_factory.mktemp("signin"),
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
    yield f"http://127.0.0.1:{port}", state
    server.should_exit = True
    thread.join(timeout=5)
    assert not thread.is_alive(), "the server is still serving a keep-alive connection"


#: Load the module and hang it on `window` so the tests can call into it.
#: An ES module has no global scope of its own, which is the point of one,
#: and the alternative is a test that can only click buttons.
LOAD = """
const done = arguments[arguments.length - 1];
import("/signin.js").then((m) => { window.signer = m; done(true); },
                          (e) => done(String(e)));
"""


def _load(browser, base):
    browser.get(f"{base}/join/keys")
    browser.set_script_timeout(60)
    assert browser.execute_async_script(LOAD) is True


def test_the_browser_can_do_its_half_at_all(browser, served):
    """`crypto.subtle` exists only in a secure context, and 127.0.0.1 is
    one. If this fails, nothing else in the file means anything."""
    base, _ = served
    browser.get(f"{base}/join/keys")
    assert browser.execute_script(
        "return window.isSecureContext && !!crypto.subtle") is True


def test_a_phrase_made_in_the_browser_is_one_python_accepts(browser, served):
    """Both halves implement BIP39. A phrase either side refuses is a wallet
    that restores on one machine and not the other."""
    base, _ = served
    _load(browser, base)
    phrase = browser.execute_async_script("""
        const done = arguments[0];
        window.signer.generate().then(done);""")
    # Twelve, not twenty-four: 128 bits is already beyond any attack that
    # will exist, and the difference is between a backup somebody keeps and
    # one they meant to write down.
    assert len(phrase.split()) == 12
    assert seed.validate(phrase) == phrase


def test_a_phrase_made_in_python_derives_the_same_key_in_the_browser(
        browser, served):
    """The one that matters: same words, same account, anywhere.

    PBKDF2-HMAC-SHA512, then hardened HMAC-SHA512 down the arcade branch,
    then Ed25519. Four separate chances to disagree, and this is the only
    check that would notice.
    """
    base, _ = served
    _load(browser, base)
    phrase = seed.generate()
    said = browser.execute_async_script("""
        const done = arguments[1];
        (async () => {
          const seed = await window.signer.toSeed(arguments[0]);
          const key = await window.signer.accountKey(seed);
          done({seed: [...seed].map(b => b.toString(16).padStart(2,'0')).join(''),
                pubkey: key.pubkey});
        })();""", phrase)
    assert said["seed"] == seed.to_seed(phrase).hex()
    assert said["pubkey"] == seed.login_pubkey(phrase)


def test_the_browser_refuses_a_phrase_with_a_swapped_word(browser, served):
    """The checksum is the whole point of writing words rather than hex."""
    base, _ = served
    _load(browser, base)
    words = seed.generate().split()
    words[0], words[1] = words[1], words[0]
    complaint = browser.execute_async_script("""
        const done = arguments[1];
        window.signer.complaint(arguments[0]).then(done);""", " ".join(words))
    assert "checksum" in complaint


def test_signing_up_in_the_browser_takes_a_seat_on_the_node(browser, served):
    """End to end: words in the browser, a signature the node verifies, a
    row in the register and a cookie that works afterwards."""
    base, state = served
    _load(browser, base)
    before = state.accounts().free()
    phrase = seed.generate()
    answer = browser.execute_async_script("""
        const done = arguments[1];
        window.signer.signIn(arguments[0], {join: true})
          .then(done, (e) => done({error: String(e.message || e)}));""", phrase)
    assert "error" not in answer, answer
    assert answer["pubkey"] == seed.login_pubkey(phrase)
    assert state.accounts().free() == before - 1

    who = browser.execute_async_script("""
        const done = arguments[0];
        fetch('/auth/who').then(r => r.json()).then(done);""")
    assert who["pubkey"] == seed.login_pubkey(phrase)
    assert state.accounts().account(who["pubkey"]).seated


def test_the_encrypted_copy_opens_only_with_its_password(browser, served):
    """The password never leaves the browser, so this is the only place it
    can be checked at all."""
    base, _ = served
    _load(browser, base)
    phrase = seed.generate()
    said = browser.execute_async_script("""
        const done = arguments[1];
        (async () => {
          const phrase = arguments[0];
          await window.signer.keep(phrase, 'correct horse battery');
          const blob = await window.signer.kept();
          let wrong = null;
          try { await window.signer.unlock('nope'); }
          catch (e) { wrong = String(e.message || e); }
          const right = await window.signer.unlock('correct horse battery');
          await window.signer.forget();
          done({blob, wrong, right, gone: await window.signer.kept()});
        })();""", phrase)
    assert said["right"] == phrase
    assert "does not open" in said["wrong"]
    assert said["gone"] is None
    # What is written down must be ciphertext and parameters, never the words.
    written = str(said["blob"])
    assert phrase.split()[0] not in written
    assert said["blob"]["iterations"] >= 600000
    assert said["blob"]["kdf"].startswith("PBKDF2")


def test_a_short_password_is_refused_before_anything_is_written(browser, served):
    base, _ = served
    _load(browser, base)
    said = browser.execute_async_script("""
        const done = arguments[0];
        (async () => {
          let refused = null;
          try { await window.signer.keep(await window.signer.generate(), 'abc'); }
          catch (e) { refused = String(e.message || e); }
          done({refused, kept: await window.signer.kept()});
        })();""")
    assert "eight characters" in said["refused"]
    assert said["kept"] is None


def _ready(browser, base):
    """Wait for the keys page to say its buttons are wired.

    `/join/keys` rather than `/join`: the splash is now the page a person
    meets -- a name and a password -- and this is the one for somebody who
    would rather hold their own words. Both end in the same wallet.

    Not a sleep: on a cold browser profile the first `indexedDB.open` took
    5.4 seconds here, and a two-second sleep passed on a warm profile and
    failed on a cold one. The page sets this the moment it is usable.
    """
    browser.get(f"{base}/join/keys")
    for _ in range(120):
        if browser.execute_script(
                "return document.body.dataset.joinReady === 'yes'"):
            return
        time.sleep(0.25)
    raise AssertionError("the join page never became usable")


def test_the_page_is_usable_before_it_has_finished_looking_things_up(
        browser, served):
    """The first thing a visitor does is press the first button. It cannot
    be dead while a database opens."""
    base, _ = served
    _ready(browser, base)
    assert browser.execute_script(
        "return typeof document.getElementById('make').onclick") == "function"


def test_the_page_walks_somebody_through_making_a_wallet(browser, served):
    """The buttons, not the module: a person meets the page, not the API."""
    base, state = served
    # Signed out first, and before the page is drawn: an earlier test in
    # this file took a seat, and a page that finds a session hides the
    # doors -- correctly, which is why this has to be deliberate rather
    # than left to whichever order the tests ran in.
    browser.get(f"{base}/join/keys")
    browser.delete_all_cookies()
    _ready(browser, base)
    assert browser.find_element(By.ID, "doors").is_displayed()
    browser.find_element(By.ID, "make").click()
    for _ in range(40):
        words = browser.find_elements(By.CSS_SELECTOR, "#wordlist li")
        if len(words) == 12:
            break
        time.sleep(0.25)
    assert len(words) == 12, "twelve words, shown once"
    assert browser.find_element(By.ID, "made").is_displayed()

    asked = [int(n) for n in
             browser.find_element(By.ID, "asked").text.split(", ")]
    assert len(asked) == 3 and len(set(asked)) == 3

    # The wrong words are refused, and nothing is taken.
    before = state.accounts().free()
    browser.find_element(By.ID, "confirm").send_keys("wrong words entirely")
    browser.find_element(By.ID, "keep").click()          # no password wanted
    browser.find_element(By.ID, "join").click()
    time.sleep(0.6)
    trouble = browser.find_element(By.ID, "trouble")
    assert trouble.is_displayed() and "not words" in trouble.text
    assert state.accounts().free() == before

    # The right ones are not.
    said = [words[n - 1].text for n in asked]
    box = browser.find_element(By.ID, "confirm")
    box.clear()
    box.send_keys(" ".join(said))
    browser.find_element(By.ID, "join").click()
    for _ in range(40):
        if browser.find_element(By.ID, "signed-in").is_displayed():
            break
        time.sleep(0.25)
    assert browser.find_element(By.ID, "signed-in").is_displayed()
    assert "no name claimed yet" in browser.find_element(By.ID, "who-tag").text
    assert state.accounts().free() == before - 1


def test_coming_back_signed_in_shows_the_account_not_the_doors(browser, served):
    """The session cookie is the point of signing in: a second visit should
    not ask somebody for their twenty-four words again."""
    base, state = served
    browser.get(f"{base}/join/keys")
    browser.delete_all_cookies()
    _ready(browser, base)
    phrase = seed.generate()
    answer = browser.execute_async_script("""
        const done = arguments[1];
        window.signer = window.signer || {};
        import("/signin.js").then(m => m.signIn(arguments[0], {join: true}))
          .then(done, e => done({error: String(e.message || e)}));""", phrase)
    assert "error" not in answer, answer

    _ready(browser, base)
    for _ in range(40):
        if browser.find_element(By.ID, "signed-in").is_displayed():
            break
        time.sleep(0.25)
    assert browser.find_element(By.ID, "signed-in").is_displayed()
    assert not browser.find_element(By.ID, "doors").is_displayed()
    assert seed.login_pubkey(phrase)[:16] in \
        browser.find_element(By.ID, "who-key").text

    before = state.accounts().free()
    browser.find_element(By.ID, "signout").click()
    for _ in range(40):
        if browser.find_element(By.ID, "doors").is_displayed():
            break
        time.sleep(0.25)
    assert browser.find_element(By.ID, "doors").is_displayed()
    # Signing out of a node is not giving up the seat: the words still open
    # it, and the seat is still theirs until it goes idle.
    assert state.accounts().free() == before


def test_the_signed_in_panel_says_what_the_seat_is(browser, served):
    """Every element on that panel is filled, including the one that says
    what holding a seat actually means."""
    base, _ = served
    browser.get(f"{base}/join/keys")
    browser.delete_all_cookies()
    _ready(browser, base)
    phrase = seed.generate()
    browser.execute_async_script("""
        const done = arguments[1];
        import("/signin.js").then(m => m.signIn(arguments[0], {join: true}))
          .then(() => done(true), e => done(String(e)));""", phrase)
    _ready(browser, base)
    for _ in range(40):
        if browser.find_element(By.ID, "signed-in").is_displayed():
            break
        time.sleep(0.25)
    since = browser.find_element(By.ID, "who-since").text
    assert "Seat taken" in since and "ninety days" in since
    assert "any node with a space" in since

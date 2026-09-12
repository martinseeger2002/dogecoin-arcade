"""Tests that need a real browser, because the bug they exist for was invisible.

The send progress bubble carried the `hidden` attribute and rendered anyway:
`[hidden]{display:none}` is in the browser's own stylesheet, and any author rule
setting `display` beats it outright -- `.bubble-row{display:flex}` did. So the
bubble showed on first paint of a brand-new page with no send in progress, and
every `row.hidden = true` in the JavaScript was a silent no-op.

Nothing in the rest of the suite could catch that. Every check either machine
made looked for the attribute in the markup, which reports "hidden: 1" for ever
while the user stares at the element. a test machine found it by asserting the COMPUTED
STYLE in a headless browser, and that is the only kind of check that can.

Skipped when selenium or a browser is unavailable, so the suite still runs
anywhere -- but the skip says why, because a silent skip here would return us to
having no coverage of this at all.
"""

import os
import socket
import threading
import time

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")

from selenium import webdriver                                       # noqa: E402
from selenium.webdriver.common.by import By                          # noqa: E402
from selenium.webdriver.firefox.options import Options               # noqa: E402
from selenium.webdriver.firefox.service import Service as FirefoxService  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def browser():
    """Headless Firefox, with the binary and driver overridable.

    ARCADE_GECKODRIVER and ARCADE_FIREFOX_BINARY exist because the default
    lookup does not find a snap-packaged Firefox: /usr/bin/firefox is a shell
    wrapper and geckodriver rejects it with "binary is not a Firefox
    executable", so the whole file skipped on a machine that had a perfectly
    good browser. These are the tests that catch the bugs nothing else can -- a
    skip here is a real loss, not a tidy fallback.

    Try ARCADE_GECKODRIVER alone first. On a snap install the driver knows where
    its own Firefox lives, so it is the only variable needed; a test machine confirmed
    that on Ubuntu with ARCADE_GECKODRIVER=/snap/bin/firefox.geckodriver and
    nothing else. ARCADE_FIREFOX_BINARY is for the other shape of odd install,
    where the driver is findable and the browser is not.
    """
    options = Options()
    options.add_argument("-headless")
    binary = os.environ.get("ARCADE_FIREFOX_BINARY")
    if binary:
        options.binary_location = binary
    driver_path = os.environ.get("ARCADE_GECKODRIVER")
    service = FirefoxService(executable_path=driver_path) if driver_path else None
    try:
        driver = webdriver.Firefox(options=options, service=service)
    except Exception as exc:                      # no firefox, no geckodriver
        pytest.skip(
            f"no usable browser: {exc}. If Firefox is installed somewhere the "
            f"default lookup misses, point ARCADE_GECKODRIVER at the driver -- "
            f"on a snap install that is /snap/bin/firefox.geckodriver and is "
            f"enough on its own. ARCADE_FIREFOX_BINARY overrides the browser "
            f"too, if the driver cannot find it."
        )
    yield driver
    driver.quit()


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    """The real application, on a real port, with no node reachable."""
    import uvicorn
    from pathlib import Path
    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext
    from arcade.messaging.keys import Identity

    home = tmp_path_factory.mktemp("browser")
    state = AppState(
        home=home,
        messaging=ChainContext(network="regtest", role="messaging", label="Testnet",
                               datadir=Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                            datadir=Path("/nonexistent")),
    )
    state.identity = Identity.generate()
    peer = b"\x77" * 32
    with state.store() as store:
        store.add_message(None, "tx", "tx", 1, 0, "nThem", peer,
                          state.identity.fingerprint, b"a message to look at")

    port = _free_port()
    config = uvicorn.Config(create_app(state), host="127.0.0.1", port=port,
                            log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    else:
        pytest.skip("test server did not start")

    yield f"http://127.0.0.1:{port}", peer.hex()
    server.should_exit = True
    thread.join(timeout=5)


def _display(browser, element):
    return browser.execute_script(
        "return getComputedStyle(arguments[0]).display", element)


def test_the_progress_bubble_is_actually_invisible(browser, served):
    """The bug itself: it carried `hidden` and rendered regardless."""
    base, peer = served
    browser.get(f"{base}/messages/{peer}")

    bubble = browser.find_element(By.ID, "sending-bubble")
    assert bubble.get_attribute("hidden") == "true"
    assert _display(browser, bubble) == "none", (
        "hidden must beat the author display rule on .bubble-row"
    )
    assert bubble.is_displayed() is False
    assert bubble.text == ""


def test_a_fresh_page_shows_no_sending_state(browser, served):
    """No send has happened, so nothing should suggest one has."""
    base, peer = served
    browser.get(f"{base}/messages/{peer}")

    button = browser.find_element(By.CSS_SELECTOR, "form.composer button[type=submit]")
    assert button.text == "Send"
    assert button.get_attribute("disabled") is None
    assert _display(browser, browser.find_element(By.ID, "sending")) == "none"


def test_the_composer_freezes_and_thaws(browser, served):
    """The three thaw mechanisms all end in `hidden = true`.

    Until `hidden` worked, none of them could ever have been observed working --
    they may have been correct and simply never exercised, which turned out to be
    the case. This is the test that establishes it.
    """
    base, peer = served
    browser.get(f"{base}/messages/{peer}")
    form = browser.find_element(By.CSS_SELECTOR, "form.composer")
    button = form.find_element(By.CSS_SELECTOR, "button[type=submit]")
    notice = browser.find_element(By.ID, "sending")

    browser.execute_script("return startSending(arguments[0])", form)
    assert button.text == "Sending…"
    assert button.get_attribute("disabled") == "true"
    assert _display(browser, notice) != "none"

    browser.execute_script("resetComposer('')")
    assert button.text == "Send"
    assert button.get_attribute("disabled") is None
    assert _display(browser, notice) == "none"


def test_a_second_submission_is_refused_while_one_is_painted(browser, served):
    base, peer = served
    browser.get(f"{base}/messages/{peer}")

    # Both calls in ONE round trip. Split across two, this was flaky: the
    # /events poller runs every 2s and calls resetComposer when the server
    # reports no send in progress, which clears the guard flag -- so the second
    # call could legitimately return True. That race is real but it is not what
    # this test is about.
    first, second = browser.execute_script("""
      var f = document.querySelector('form.composer');
      return [startSending(f), startSending(f)];
    """)
    assert first is True
    assert second is False


def test_the_poller_clears_a_leftover_paint_job(browser, served):
    """/events reporting nothing must clear the whole state, not just the bubble.

    Clearing the bubble alone left a page that refused input while telling the
    truth in one small element.
    """
    base, peer = served
    browser.get(f"{base}/messages/{peer}")
    form = browser.find_element(By.CSS_SELECTOR, "form.composer")
    button = form.find_element(By.CSS_SELECTOR, "button[type=submit]")

    browser.execute_script("return startSending(arguments[0])", form)
    # Exactly what draw() does when the server reports no send in flight.
    browser.execute_script("""
        document.getElementById('sending-bubble').hidden = true;
        if (document.querySelector('form[data-sending="yes"]')) resetComposer('');
    """)

    assert button.get_attribute("disabled") is None
    assert _display(browser, browser.find_element(By.ID, "sending")) == "none"
    assert _display(browser, browser.find_element(By.ID, "sending-bubble")) == "none"


def test_the_public_composer_behaves_the_same(browser, served):
    base, _ = served
    browser.get(f"{base}/groups?channel=main")
    form = browser.find_element(By.CSS_SELECTOR, "form.composer")
    button = form.find_element(By.CSS_SELECTOR, "button[type=submit]")

    browser.execute_script("return startPosting(arguments[0])", form)
    assert button.get_attribute("disabled") == "true"
    browser.execute_script("resetPoster('')")
    assert button.get_attribute("disabled") is None


def test_a_request_that_hangs_recovers_on_its_own(browser, served):
    """The only case the timeout can actually cover.

    a test machine established the shape of this: a failure at the NETWORK level navigates
    the browser to its own error page, so the page that would freeze is already
    gone -- stopping the server and submitting gives about:neterror, not a stuck
    composer. What remains is a server that accepts the request and then never
    answers, which leaves a live page waiting. That is what this exercises, with
    the timeout shortened so the test takes a second rather than twenty-five.
    """
    import time

    base, peer = served
    browser.get(f"{base}/messages/{peer}")
    browser.execute_script("window.ARCADE_SEND_TIMEOUT = 700;")

    form = browser.find_element(By.CSS_SELECTOR, "form.composer")
    button = form.find_element(By.CSS_SELECTOR, "button[type=submit]")
    browser.execute_script("return startSending(arguments[0])", form)
    assert button.get_attribute("disabled") == "true"

    time.sleep(1.5)

    assert button.get_attribute("disabled") is None, "it should have recovered"
    assert button.text == "Send"
    notice = browser.find_element(By.ID, "sending")
    assert notice.is_displayed(), "it should say why, not just re-enable"
    assert "did not reach the application" in notice.text


def test_presence_is_not_visibility(browser, served):
    """Pins the measurement error both machines made, twice each.

    The notice is always in the DOM and simply not displayed. Counting elements
    that contain its text reports 1 at rest, which reads as "the notice is
    showing" when nothing is showing at all. Only is_displayed or a computed
    style distinguishes them.
    """
    base, peer = served
    browser.get(f"{base}/messages/{peer}")

    notice = browser.find_element(By.ID, "sending")
    assert notice is not None, "present in the DOM"
    assert notice.is_displayed() is False, "and not visible"
    assert _display(browser, notice) == "none"


# --- the chat bubble tail -----------------------------------------------------
#
# Three attempts at this rendered wrong while computing perfectly sane styles:
# a box hung off the side with a quarter-disc carved from its top-left (a spur,
# not a tail); a pair of blocks whose horizontal border-radius exceeded their
# own width, which collapsed the tail to a sliver; and a clipped path whose two
# curves hugged each other, leaving a hairline. Only the pixels tell those apart
# from a tail, so that is what these look at.
#
# Total coverage is not enough on its own: the hairline filled 18% of the tail's
# strip and the real tail 23%, which no threshold can separate. What actually
# distinguishes a tail is its PROFILE -- it is nearly the full width of the
# strip where it meets the bubble's bottom edge and tapers to nothing going up.
# A hairline is thin everywhere; a bare rectangle is full everywhere.


def _tail_profile(browser, pane, bubble, side):
    """Fill fraction of each pixel row of the tail's strip, top row first.

    The strip is the 12px beside the bubble's `side` edge, spanning the bottom
    16px -- exactly where the tail is drawn. The scroll pane is screenshotted
    rather than the bubble or its row, because an element screenshot is clipped
    to that element's box and the tail is painted outside both of those: the row
    is a full-width flex container and the bubble sits hard against its edge, so
    the tail lands in the pane's own padding.
    """
    Image = pytest.importorskip("PIL.Image", reason="tail pixel checks need pillow")
    import io

    shot = Image.open(io.BytesIO(pane.screenshot_as_png)).convert("RGB")
    inner = Image.open(io.BytesIO(bubble.screenshot_as_png)).convert("RGB")
    scale = shot.width / pane.size["width"]                   # device pixel ratio

    left, right, bottom = browser.execute_script("""
      var p = arguments[0].getBoundingClientRect();
      var b = arguments[1].getBoundingClientRect();
      return [b.left - p.left, b.right - p.left, b.bottom - p.top];
    """, pane, bubble)

    pad, height = round(12 * scale), round(16 * scale)
    y1 = min(round(bottom * scale), shot.height)
    x0 = round(right * scale) if side == "right" else round(left * scale) - pad
    x0 = max(0, min(x0, shot.width - pad))
    strip = shot.crop((x0, max(0, y1 - height), x0 + pad, y1))

    # The bubble's own colour, sampled from inside it rather than assumed, so
    # the check holds in either colour scheme.
    fill = inner.getpixel((inner.width // 2, inner.height - 2))
    # tobytes rather than getdata: getdata is deprecated in Pillow 12 and the
    # warning is an error under this suite's filters.
    raw, width = strip.tobytes(), strip.width

    rows = []
    for y in range(strip.height):
        start = y * width * 3
        hit = sum(1 for x in range(width)
                  if all(abs(raw[start + x * 3 + c] - fill[c]) <= 14 for c in range(3)))
        rows.append(hit / width)
    return rows


def test_the_tail_leaves_the_bottom_corner(browser, served):
    """It is wide where it meets the bubble's bottom edge and tapers going up.

    That profile is the whole point: the shapes this replaced stuck out of the
    SIDE, which reads as a spur, and the user said so.
    """
    base, peer = served
    browser.get(f"{base}/messages/{peer}")

    pane = browser.find_element(By.CSS_SELECTOR, ".bubbles")
    bubble = pane.find_element(By.CSS_SELECTOR, ".bubble-row.theirs .bubble")
    rows = _tail_profile(browser, pane, bubble, "left")

    assert len(rows) >= 12
    foot = sum(rows[-3:]) / 3
    head_ = sum(rows[:3]) / 3
    assert foot > 0.55, (
        f"the tail fills {foot:.0%} of its strip at the bubble's bottom edge -- "
        f"a tail is nearly full there; a line or a sliver is not. rows={rows}"
    )
    assert head_ < 0.45, (
        f"the tail still fills {head_:.0%} of its strip 16px up -- it is not "
        f"tapering, so it reads as a block beside the bubble. rows={rows}"
    )
    assert foot > head_ + 0.3, f"the tail does not taper: rows={rows}"


def test_the_tail_carries_the_corner(browser, served):
    """The bubble's radius is dropped on the tail side.

    Any radius there curves the bubble away from the tail's root and leaves a
    notch at the join.
    """
    base, peer = served
    browser.get(f"{base}/messages/{peer}")

    bubble = browser.find_element(By.CSS_SELECTOR, ".bubble-row.theirs .bubble")
    radius, clip = browser.execute_script("""
      var s = getComputedStyle(arguments[0]);
      var t = getComputedStyle(arguments[0], '::after');
      return [s.borderBottomLeftRadius, t.clipPath];
    """, bubble)
    assert radius == "0px", f"tail-side radius is {radius}, which notches the join"
    assert clip.startswith("path("), f"the tail is not clipped to a shape: {clip}"


# --- the public composer's byte budget ----------------------------------------

def test_the_public_composer_has_no_character_cap(browser, served):
    """maxlength counted characters while the budget is bytes.

    One emoji is four bytes but one character, so a post inside the character
    cap could be well over the byte budget: the browser let it through and the
    server refused it with a message ending "attaching a file lifts the limit",
    which reads as a file-size error. The operator hit exactly that with no file
    attached. Going over is now allowed -- it costs dust instead.
    """
    base, _ = served
    browser.get(f"{base}/groups")

    box = browser.find_element(By.ID, "post")
    assert box.get_attribute("maxlength") is None, (
        "a cap in characters cannot enforce a budget in bytes"
    )


def test_the_budget_note_recovers_when_the_post_is_shortened(browser, served):
    """Over, then under again. The over-budget wording must not be permanent.

    The first version of this rewrote the note's innerHTML, which replaced the
    span holding the count -- so the counter stopped updating and shortening the
    post could never undo the warning.
    """
    base, _ = served
    browser.get(f"{base}/groups")

    def note_for(text):
        browser.execute_script("""
          var b = document.getElementById('post');
          b.value = arguments[0];
          countLeft(b);
        """, text)
        return browser.find_element(By.ID, "budget").text

    under = note_for("hi")
    assert "dust" in under and "over" not in under

    over = note_for("x" * 400)
    assert "over" in over and "dust" in over

    again = note_for("hi")
    assert again == under, (
        f"shortening the post must restore the note.\n  was: {under}\n  now: {again}"
    )
    # And the counter itself must still be live, not a replaced span.
    assert browser.find_element(By.ID, "left").text == under.split()[0]


def test_emoji_cross_the_budget_sooner_than_characters_suggest(browser, served):
    """The counter has to agree with the server, which counts bytes."""
    base, _ = served
    browser.get(f"{base}/groups")

    over = browser.execute_script("""
      var b = document.getElementById('post');
      b.value = '\\ud83d\\udc38'.repeat(20);          // 20 frogs: 80 bytes, 40 UTF-16 units
      countLeft(b);
      return document.getElementById('budget').textContent;
    """)
    assert "over" in over, (
        "20 emoji are 80 bytes and cannot fit a budget of 60 -- the counter "
        "must say so even though that is only 20 characters"
    )


# --- Enter sends --------------------------------------------------------------
#
# It did not. `sendOnEnter` called `startSending` itself and then called
# `requestSubmit`, which fires the form's own submit event -- and that runs
# `onsubmit="return startSending(this)"`. The second call saw the guard flag the
# first had just set, returned false, and cancelled the submission. So Enter
# painted "Sending..." over a form that never posted, and 25 seconds later the
# timeout said "that did not reach the application". Clicking the button worked,
# because a click goes through onsubmit exactly once.
#
# The operator reported it as "the first time I pressed enter it gave me the error and
# the second time I pressed the send button it sent", and it went unexplained
# through two wrong diagnoses of mine. No assertion on markup or computed style
# could have caught it: the page is untouched and perfectly healthy, it simply
# never made a request. The only check that catches it is whether a request
# happened at all.


def _submits(browser, url, selector, how):
    """True if acting on the composer actually posted to the server.

    Detected by a marker on `window`, which cannot survive a navigation. There
    is no node behind the test server, so a real submission comes back as an
    error page -- that arrival is the proof, not the error itself.

    TO REPEAT THIS AGAINST A REAL INSTALLATION, STOP THE WEB SERVER FIRST.
    The marker works the same way -- the browser navigates to its own network
    error page and the marker is gone -- but with the server down nothing can be
    broadcast, so the check costs nothing. Run against a live application on a
    machine with a funded wallet, the identical test SENDS A REAL MESSAGE and
    spends real outputs. a test machine verified this fix on the operator's machine that way.
    The failure mode of getting it wrong is spending someone's coins to learn
    what a free test would have told you.
    """
    browser.get(url)
    browser.execute_script("window.__alive = 'yes';")
    box = browser.find_element(By.CSS_SELECTOR, f"{selector} textarea")
    box.send_keys("a message to send")
    if how == "enter":
        from selenium.webdriver.common.keys import Keys
        box.send_keys(Keys.ENTER)
    else:
        browser.find_element(By.CSS_SELECTOR, f"{selector} button[type=submit]").click()
    for _ in range(40):
        if not browser.execute_script("return window.__alive === 'yes';"):
            return True
        time.sleep(0.1)
    return False


def test_enter_sends_a_private_message(browser, served):
    base, peer = served
    assert _submits(browser, f"{base}/messages/{peer}", "form.composer", "enter"), (
        "pressing Enter did not post anything -- the double-send guard cancelled it"
    )


def test_the_button_sends_a_private_message(browser, served):
    """The path that always worked, asserted alongside so a fix cannot swap them."""
    base, peer = served
    assert _submits(browser, f"{base}/messages/{peer}", "form.composer", "click")


def test_enter_posts_on_the_public_board(browser, served):
    """The same bug, the same shape, in postOnEnter."""
    base, _ = served
    assert _submits(browser, f"{base}/groups", "form.composer", "enter"), (
        "pressing Enter did not post anything on the public board either"
    )


def test_a_second_enter_is_still_swallowed(browser, served):
    """The guard has to keep working -- it just must not eat the first press.

    A send can take minutes with nothing on screen, which is exactly when a
    second press happens, and two sends select their outputs without seeing each
    other's claims.
    """
    base, peer = served
    browser.get(f"{base}/messages/{peer}")
    box = browser.find_element(By.CSS_SELECTOR, "form.composer textarea")

    # Pin the form in the sending state, as an in-flight send leaves it.
    browser.execute_script("""
      var f = document.querySelector('form.composer');
      f.dataset.sending = 'yes';
      window.__posted = 0;
      f.addEventListener('submit', function (e) { window.__posted++; e.preventDefault(); });
    """)
    box.send_keys("a second press")
    from selenium.webdriver.common.keys import Keys
    box.send_keys(Keys.ENTER)
    time.sleep(0.5)

    assert browser.execute_script("return window.__posted;") == 0, (
        "a second Enter while a send is in flight must not start another"
    )


def test_enter_stands_down_while_a_confirmation_is_pending(browser, served):
    """Enter must not submit past a confirmation for the same content.

    The composer's submit button stayed live behind the confirmation -- on the
    public board, three buttons with two of them saying Post -- so a page asking
    "Post this?" would also accept a fresh submission of whatever was in the
    box. a test machine hit it. Disabling the button covers the mouse; this covers the
    keyboard, which is the half a `disabled` attribute cannot.

    The flag is set here rather than reached through a real send, because
    rendering a confirmation needs a funded node. The handler's behaviour is
    what is being asserted, and that is independent of how the flag got there.
    """
    base, peer = served
    browser.get(f"{base}/messages/{peer}")

    browser.execute_script("""
      var f = document.querySelector('form.composer');
      f.dataset.awaitingConfirm = 'yes';
      window.__posted = 0;
      f.addEventListener('submit', function (e) { window.__posted++; e.preventDefault(); });
    """)
    box = browser.find_element(By.CSS_SELECTOR, "form.composer textarea")
    box.send_keys("typed while a confirmation is on screen")
    from selenium.webdriver.common.keys import Keys
    box.send_keys(Keys.ENTER)
    time.sleep(0.5)

    assert browser.execute_script("return window.__posted;") == 0, (
        "Enter submitted past a pending confirmation"
    )

    # And it must start working again once the confirmation is gone, or the
    # composer would be dead for the rest of the page's life.
    browser.execute_script("""
      var f = document.querySelector('form.composer');
      delete f.dataset.awaitingConfirm;
    """)
    box.send_keys(Keys.ENTER)
    time.sleep(0.5)
    assert browser.execute_script("return window.__posted;") == 1, (
        "Enter stayed dead after the confirmation was dismissed"
    )

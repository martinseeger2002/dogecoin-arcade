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

import socket
import threading
import time

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")

from selenium import webdriver                                       # noqa: E402
from selenium.webdriver.common.by import By                          # noqa: E402
from selenium.webdriver.firefox.options import Options               # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def browser():
    options = Options()
    options.add_argument("-headless")
    try:
        driver = webdriver.Firefox(options=options)
    except Exception as exc:                      # no firefox, no geckodriver
        pytest.skip(f"no usable browser: {exc}")
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
    form = browser.find_element(By.CSS_SELECTOR, "form.composer")

    assert browser.execute_script("return startSending(arguments[0])", form) is True
    assert browser.execute_script("return startSending(arguments[0])", form) is False


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

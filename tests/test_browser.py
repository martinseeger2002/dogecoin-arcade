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
    attached.

    The running byte counter that replaced it has since been removed too -- it
    announced the budget on almost every post, when the only consequence of
    passing it is that the post costs more, and the confirm screen says what it
    costs before anything is sent. What must not come back is a cap that cannot
    measure what it is capping.
    """
    base, _ = served
    browser.get(f"{base}/groups")

    box = browser.find_element(By.ID, "post")
    assert box.get_attribute("maxlength") is None, (
        "a cap in characters cannot enforce a budget in bytes"
    )


def test_small_costs_and_sizes_do_not_round_away(browser, served):
    """A 900-byte file costs 0.16 and used to display as "about 0 in dust".

    `toLocaleString(undefined, {maximumFractionDigits: 0})` rounded every cost
    under half a coin to "0", and `(bytes/1024).toFixed(0) + " KB"` showed a
    102-byte file as "0 KB". a test machine met the 102-byte case; the rest of the range
    was the same bug wearing a less obvious face, which is why these are shared
    helpers in base.html rather than fixed twice.
    """
    base, peer = served
    browser.get(f"{base}/messages/{peer}")

    rows = browser.execute_script("""
      return [102, 900, 4000, 40000].map(function (n) {
        return [n, arcadeSize(n), arcadeCoins(arcadeFileCost(n).coins)];
      });
    """)
    for size, shown_size, shown_cost in rows:
        assert shown_size not in ("0 KB", "0 bytes"), (
            f"{size} bytes displayed as {shown_size!r}"
        )
        assert float(shown_cost.replace(",", "")) > 0, (
            f"{size} bytes costs something but displayed as {shown_cost!r}"
        )

    assert rows[0][1] == "102 bytes", "under 1 KB should be shown in bytes"
    # One number now, dust and fee together: quoting the dust alone and adding
    # "on top of the fee" split one answer into two, neither of which was it.
    assert rows[0][2] == "0.12"
    assert rows[1][2] == "0.25", "this is the case that used to read as free"


# --- phones -------------------------------------------------------------------
# "Make sure it looks good when viewing from a mobile device", now that the
# Remote page exists to make that the normal way to use it. Measured rather than
# eyeballed, because every one of these was invisible on a desktop:
#
#   * the messenger clipped its own composer note mid-sentence -- 818px of 833 --
#     because `height:auto` on a phone left `max-height:820px` standing;
#   * every text field was under 16px, which makes Safari on iOS zoom the page in
#     on the first tap and never zoom back out;
#   * the address book pushed the whole page sideways at 320px, because its
#     cards asked for a 330px minimum.


PHONE_PAGES = ["/", "/messages", "/contacts", "/groups", "/backup",
               "/wallet", "/wallet/tokens", "/wallet/nfts", "/tokens", "/nfts",
               "/compose", "/inbox", "/remote"]


@pytest.fixture(scope="module")
def phone(browser, served):
    """A viewport the width of a phone, which a window cannot be.

    Headless Firefox refuses to make a window narrower than 500 CSS px -- wider
    than any phone made. An iframe is a real viewport: the document inside lays
    out at the iframe's width and its media queries answer to that width, so a
    390px iframe is a 390px phone as far as the page is concerned.
    """
    base, peer = served

    def visit(path, width=390):
        browser.get(base + "/")            # same origin, so the frame can be set
        browser.execute_script(
            """
            document.body.innerHTML = '';
            document.body.style.margin = '0';
            const f = document.createElement('iframe');
            f.id = 'phone'; f.width = arguments[1]; f.height = 800;
            f.style.border = '0'; f.src = arguments[0];
            document.body.appendChild(f);
            """, base + path, width)
        time.sleep(0.6)
        browser.switch_to.frame(browser.find_element(By.ID, "phone"))
        return browser

    yield visit, peer
    browser.switch_to.default_content()


@pytest.mark.parametrize("path", PHONE_PAGES)
@pytest.mark.parametrize("width", [390, 320])
def test_no_page_pushes_a_phone_sideways(phone, path, width):
    """A page wider than the screen moves every element on it, so the text no
    longer fits either. Wide things must scroll on their own."""
    visit, _ = phone
    browser = visit(path, width)
    try:
        scroll_width, view_width, offenders = browser.execute_script("""
            const w = window.innerWidth, out = [];
            for (const el of document.querySelectorAll('body *')) {
              const r = el.getBoundingClientRect();
              if (r.width > 0 && r.height > 0 && r.right > w + 1) {
                out.push(el.tagName.toLowerCase() + '.' +
                  (typeof el.className === 'string' ? el.className : '') +
                  ' right=' + Math.round(r.right));
              }
            }
            return [document.documentElement.scrollWidth, w, out.slice(0, 3)];
        """)
        assert scroll_width <= view_width + 1, (
            f"{path} at {width}px scrolls to {scroll_width}px: {offenders}")
    finally:
        browser.switch_to.default_content()


@pytest.mark.parametrize("path", PHONE_PAGES)
def test_tapping_a_field_does_not_zoom_the_page(phone, path):
    """Safari on iOS zooms in when it focuses a field under 16px and does not
    zoom back, so one tap on any form leaves the wallet magnified and scrolled
    off to one side. Every field here was 13.6-15px."""
    visit, _ = phone
    browser = visit(path)
    try:
        small = browser.execute_script("""
            const out = [];
            for (const el of document.querySelectorAll('input,select,textarea')) {
              if (el.type === 'hidden') continue;
              const size = parseFloat(getComputedStyle(el).fontSize);
              if (size < 16) out.push((el.name || el.tagName) + ' ' + size + 'px');
            }
            return out;
        """)
        assert small == [], f"{path} has fields iOS will zoom for: {small}"
    finally:
        browser.switch_to.default_content()


def test_the_messenger_does_not_cut_off_its_own_composer(phone):
    """The note it clipped was the one saying a message is permanent once sent."""
    visit, peer = phone
    browser = visit(f"/messages/{peer}")
    try:
        clipped = browser.execute_script("""
            const out = [];
            for (const el of document.querySelectorAll('body *')) {
              const s = getComputedStyle(el);
              if ((s.overflow === 'hidden' || s.overflowY === 'hidden')
                  && el.clientHeight > 0 && el.scrollHeight > el.clientHeight + 2) {
                out.push(el.className + ' shows ' + el.clientHeight
                         + ' of ' + el.scrollHeight);
              }
            }
            return out;
        """)
        assert clipped == [], f"cut off on a phone: {clipped}"
        notes = [n.text.strip() for n
                 in browser.find_elements(By.CSS_SELECTOR, ".composer-note")
                 if n.text.strip()]
        assert notes, "the composer says nothing at all"
        assert any("permanent once sent" in n for n in notes), notes
        for note in notes:
            assert note.endswith("."), f"a note ends mid-sentence: {note[-60:]!r}"
    finally:
        browser.switch_to.default_content()


def test_the_navigation_does_not_eat_the_screen(phone):
    """Eleven links wrapped onto four rows: 226px of an 844px phone, before any
    page began, on every page."""
    visit, _ = phone
    browser = visit("/")
    try:
        height, rows = browser.execute_script("""
            const tops = new Set();
            for (const a of document.querySelectorAll('nav a')) {
              const r = a.getBoundingClientRect();
              if (r.width === 0) continue;      // hidden here; it reports top 0
              tops.add(Math.round(r.top));
            }
            return [Math.round(document.querySelector('header')
                     .getBoundingClientRect().height), tops.size];
        """)
        # One scrolling row, so the header costs the same at nine sections as at
        # twenty -- which is the point: every feature still to come used to make
        # this worse.
        assert rows == 1, f"the navigation is on {rows} rows"
        assert height <= 110, f"the header takes {height}px of the screen"
    finally:
        browser.switch_to.default_content()


def test_the_chat_box_is_a_line_to_write_on_not_a_squashed_block(phone):
    """`textarea{min-height:150px}` applied to the chat composer, which asks in
    its own markup to be one line that grows: rows="1", grow() on input, a
    160px ceiling. It came out tall and narrow -- a block shoved into the
    corner beside the Send button rather than something to write on."""
    visit, peer = phone
    browser = visit(f"/messages/{peer}")
    try:
        box, form = browser.execute_script("""
            const t = document.querySelector('form.composer textarea');
            const f = t.closest('form');
            const a = t.getBoundingClientRect(), b = f.getBoundingClientRect();
            return [[a.width, a.height], [b.width, b.height]];
        """)
        assert box[1] < 64, f"the empty chat box is {box[1]}px tall, not one line"
        assert box[0] > form[0] * 0.5, (
            f"the box is {box[0]}px of a {form[0]}px row: the buttons have "
            f"taken more of it than the writing")
    finally:
        browser.switch_to.default_content()


# --- one screen at a time -----------------------------------------------------
# Stacked, the conversation list pushed the conversation itself below the fold
# on every visit, so reading a reply began with scrolling past everyone you have
# ever spoken to. A phone shows the list, then the conversation, with a way back.


def _shown(browser, selector):
    return browser.execute_script(
        """const el = document.querySelector(arguments[0]);
           return el ? getComputedStyle(el).display !== 'none' : null;""", selector)


def test_a_phone_shows_the_conversation_list_on_its_own(phone):
    visit, _ = phone
    browser = visit("/messages")
    try:
        assert _shown(browser, ".threads") is True
        assert _shown(browser, ".convo") is False, (
            "'Select a conversation' is not worth a screen when the list is on it")
    finally:
        browser.switch_to.default_content()


def test_opening_a_conversation_replaces_the_list(phone):
    visit, peer = phone
    browser = visit(f"/messages/{peer}")
    try:
        assert _shown(browser, ".threads") is False
        assert _shown(browser, ".convo") is True
        back = browser.find_element(By.CSS_SELECTOR, ".convo-head .back")
        assert back.is_displayed()
        assert back.get_attribute("href").endswith("/messages")
        assert back.rect["height"] >= 44, "a back button has to be tappable"
        assert back.rect["x"] < 60, "it belongs in the top left corner"
    finally:
        browser.switch_to.default_content()


def test_channels_work_the_same_way(phone):
    visit, _ = phone
    browser = visit("/groups")
    try:
        assert _shown(browser, ".threads") is True
        assert _shown(browser, ".convo") is False
    finally:
        browser.switch_to.default_content()

    browser = visit("/groups?channel=main")
    try:
        assert _shown(browser, ".threads") is False
        assert _shown(browser, ".convo") is True
        back = browser.find_element(By.CSS_SELECTOR, ".convo-head .back")
        assert back.is_displayed()
        assert "/groups" in back.get_attribute("href")
        assert "channel=" not in back.get_attribute("href"), (
            "back has to go to the list, not to the channel it is leaving")
    finally:
        browser.switch_to.default_content()


def test_a_desktop_still_shows_both_and_needs_no_way_back(browser, served):
    """Both panes fit side by side there, which is the better way to read a
    conversation when they do -- and there is nothing to go back to."""
    base, peer = served
    browser.set_window_size(1100, 900)
    browser.get(f"{base}/messages/{peer}")
    assert _shown(browser, ".threads") is True
    assert _shown(browser, ".convo") is True
    assert _shown(browser, ".convo-head .back") is False


def test_posting_from_a_phone_stays_in_the_channel(phone):
    """Reported from a phone, over the tunnel: pressing Post bounced back to the
    channel list and nothing was ever sent.

    Posting is two steps -- the first press prepares and the page comes back
    asking "post this?" -- and that question is drawn inside the channel pane.
    The POST renders from /groups/post, which has no channel in its query
    string, so the pane was hidden and the confirmation with it. The list was
    all that was left on screen, and the post could never be confirmed.
    """
    visit, _ = phone
    browser = visit("/groups?channel=main")
    try:
        assert _shown(browser, ".convo") is True
        box = browser.find_element(By.CSS_SELECTOR, "form.composer textarea")
        box.send_keys("hi all")
        browser.find_element(
            By.CSS_SELECTOR, "form.composer button[type=submit]").click()
        time.sleep(1.2)

        assert _shown(browser, ".convo") is True, (
            "the channel pane vanished on the way back from posting")
        assert _shown(browser, ".threads") is False
        assert browser.execute_script(
            "return document.querySelector('.msgr').classList.contains('open')")
    finally:
        browser.switch_to.default_content()


# --- pictures cost money, so they can be sent smaller ---------------------------
# Every 60 bytes of a picture buys an output that can never be spent again, so a
# photo straight from a phone camera is several coins and several blocks. The
# three sizes are re-encoded in the browser before anything is uploaded, so the
# wallet needs no image library and nothing is uploaded twice.

MAKE_A_PHOTO = """
const done = arguments[0];
const c = document.createElement('canvas');
c.width = 2400; c.height = 1600;
const g = c.getContext('2d');
// Noise, not flat colour: a flat image compresses to nothing and would not
// show the difference between the three settings.
const img = g.createImageData(c.width, c.height);
for (let i = 0; i < img.data.length; i += 4) {
  img.data[i] = (i * 7) % 255; img.data[i+1] = (i * 13) % 255;
  img.data[i+2] = (i * 29) % 255; img.data[i+3] = 255;
}
g.putImageData(img, 0, 0);
c.toBlob(function (blob) {
  const input = document.getElementById('attachment');
  const dt = new DataTransfer();
  dt.items.add(new File([blob], 'photo.png', {type: 'image/png'}));
  input.files = dt.files;
  input.dispatchEvent(new Event('change'));
  done(blob.size);
}, 'image/png');
"""

READ_SIZES = """
const buttons = [...document.querySelectorAll('#picked-sizes button')];
const input = document.getElementById('attachment');
return {
  labels: buttons.map(b => b.textContent.replace(/\\s+/g, ' ').trim()),
  chosen: buttons.filter(b => b.classList.contains('on'))
                 .map(b => b.querySelector('strong').textContent),
  name: input.files[0] ? input.files[0].name : null,
  type: input.files[0] ? input.files[0].type : null,
  bytes: input.files[0] ? input.files[0].size : null,
};
"""


@pytest.mark.parametrize("path,composer", [("/messages/{peer}", "message"),
                                           ("/groups?channel=main", "post")])
def test_a_picture_can_be_sent_at_three_sizes(phone, path, composer):
    visit, peer = phone
    browser = visit(path.replace("{peer}", peer))
    try:
        browser.set_script_timeout(30)
        original = browser.execute_async_script(MAKE_A_PHOTO)
        assert original > 200_000, "the test photo has to be worth shrinking"

        for _ in range(60):
            state = browser.execute_script(READ_SIZES)
            if len(state["labels"]) == 3:
                break
            time.sleep(0.5)

        assert [label.split()[0] for label in state["labels"]] == \
            ["Large", "Medium", "Small"], state["labels"]
        assert state["chosen"] == ["Medium"], "Medium is the sensible default"
        assert "1280×960" in state["labels"][1].replace("&times;", "×") or \
               "1280" in state["labels"][1], state["labels"][1]
        assert "640" in state["labels"][2], state["labels"][2]

        # The form carries the chosen one, not the original.
        assert state["bytes"] < original / 2, (state["bytes"], original)
        assert state["name"] == "photo.jpg" and state["type"] == "image/jpeg"

        # Each button says what that choice costs, which is the reason it exists.
        for label in state["labels"]:
            assert "·" in label, f"no size and cost on {label!r}"
    finally:
        browser.switch_to.default_content()


def test_choosing_large_puts_the_original_back(phone):
    """A choice that cannot be undone is not a choice."""
    visit, peer = phone
    browser = visit(f"/messages/{peer}")
    try:
        browser.set_script_timeout(30)
        original = browser.execute_async_script(MAKE_A_PHOTO)
        for _ in range(60):
            if len(browser.execute_script(READ_SIZES)["labels"]) == 3:
                break
            time.sleep(0.5)

        browser.execute_script("""
            [...document.querySelectorAll('#picked-sizes button')]
              .filter(b => b.textContent.indexOf('Large') === 0)[0].click();""")
        time.sleep(0.4)
        state = browser.execute_script(READ_SIZES)
        assert state["chosen"] == ["Large"]
        assert state["bytes"] == original, "Large is the file as it was"
        assert state["name"] == "photo.png"

        browser.execute_script("""
            [...document.querySelectorAll('#picked-sizes button')]
              .filter(b => b.textContent.indexOf('Small') === 0)[0].click();""")
        time.sleep(0.4)
        small = browser.execute_script(READ_SIZES)
        assert small["chosen"] == ["Small"]
        assert small["bytes"] < state["bytes"] / 4
    finally:
        browser.switch_to.default_content()


def test_a_file_that_is_not_a_picture_is_offered_no_sizes(phone):
    """There is nothing to re-encode, and a button that did nothing would be a
    lie about what was about to be sent."""
    visit, peer = phone
    browser = visit(f"/messages/{peer}")
    try:
        browser.execute_script("""
            const input = document.getElementById('attachment');
            const dt = new DataTransfer();
            dt.items.add(new File(['x'.repeat(9000)], 'notes.txt',
                                  {type: 'text/plain'}));
            input.files = dt.files;
            input.dispatchEvent(new Event('change'));""")
        time.sleep(0.6)
        state = browser.execute_script(READ_SIZES)
        assert state["labels"] == []
        assert state["name"] == "notes.txt", "and it is still the file to send"
    finally:
        browser.switch_to.default_content()


def test_removing_the_picture_forgets_the_sizes(phone):
    visit, peer = phone
    browser = visit(f"/messages/{peer}")
    try:
        browser.set_script_timeout(30)
        browser.execute_async_script(MAKE_A_PHOTO)
        for _ in range(60):
            if len(browser.execute_script(READ_SIZES)["labels"]) == 3:
                break
            time.sleep(0.5)
        browser.execute_script("clearPick()")
        time.sleep(0.3)
        state = browser.execute_script(READ_SIZES)
        assert state["labels"] == [] and state["name"] is None
        assert browser.execute_script(
            "return document.getElementById('picked').hidden") is True
    finally:
        browser.switch_to.default_content()


def test_the_chat_box_survives_the_introduce_checkbox(phone):
    """A first message to somebody new puts "Introduce myself" in the composer
    row. Given a full-width line of its own on a nowrap flex row, it squeezed
    the box beside it down to a sliver -- one character wide."""
    visit, _ = phone
    stranger = "aa" * 32
    browser = visit(f"/messages/{stranger}")
    try:
        assert browser.execute_script(
            "return !!document.querySelector('.composer .introduce')"), (
            "this peer should be new, or the test is not testing anything")
        box, form = browser.execute_script("""
            const t = document.querySelector('form.composer textarea');
            const r = t.getBoundingClientRect();
            const f = t.closest('form').getBoundingClientRect();
            return [[r.width, r.height], [f.width, f.height]];
        """)
        assert box[0] > form[0] * 0.5, (
            f"the box is {box[0]}px of a {form[0]}px row")
        assert box[1] < 64, f"and {box[1]}px tall"
    finally:
        browser.switch_to.default_content()


def test_the_composer_says_each_thing_once(phone):
    """A new contact got the progress line and the privacy note twice: the same
    paragraph printed twice on screen, and two elements with id="sending", of
    which the JavaScript can only ever drive the first."""
    visit, _ = phone
    browser = visit("/messages/" + "bb" * 32)
    try:
        counts = browser.execute_script("""
            const notes = [...document.querySelectorAll('.composer-note')]
              .map(n => n.textContent.replace(/\\s+/g, ' ').trim())
              .filter(t => t.length > 20);
            const dupes = notes.filter((t, i) => notes.indexOf(t) !== i);
            return [dupes, document.querySelectorAll('#sending').length,
                    document.querySelectorAll('[id]').length -
                    new Set([...document.querySelectorAll('[id]')]
                              .map(e => e.id)).size];
        """)
        assert counts[0] == [], f"said twice: {counts[0]}"
        assert counts[1] == 1, f"{counts[1]} elements share id=sending"
        assert counts[2] == 0, "the page has duplicate ids"
    finally:
        browser.switch_to.default_content()


def test_a_picture_can_be_posted_to_the_board_with_nothing_typed(phone):
    """"I should be able to post an image without any text attached, but it's
    not working on the board." The encoder refused an empty text whatever else
    the post carried, so choosing a picture and pressing Post came back saying
    to write something -- for a post that was already complete."""
    visit, _ = phone
    browser = visit("/groups?channel=main")
    try:
        browser.set_script_timeout(30)
        browser.execute_async_script(MAKE_A_PHOTO)
        for _ in range(60):
            if len(browser.execute_script(READ_SIZES)["labels"]) == 3:
                break
            time.sleep(0.5)

        assert browser.execute_script(
            "return document.getElementById('post').value") == "", "nothing typed"
        browser.find_element(
            By.CSS_SELECTOR, "form.composer button[type=submit]").click()
        time.sleep(1.5)

        page = browser.execute_script("return document.body.textContent")
        assert "write something" not in page, (
            "the board still refuses a picture on its own")
        # No node in this fixture, so it cannot get further than trying to pay
        # for it -- which is proof it got past the encoder.
        assert ("node" in page or "Post this" in page or "wallet" in page), page[:300]
    finally:
        browser.switch_to.default_content()


def test_clicking_a_picture_opens_it_rather_than_downloading_it(phone):
    """Downloading is the rarer thing to want and the more annoying to undo --
    a folder full of files you only meant to look at."""
    visit, peer = phone
    browser = visit(f"/messages/{peer}")
    try:
        # Put a picture in the conversation the way a received one looks.
        browser.execute_script("""
            const row = document.createElement('div');
            row.className = 'bubble-row theirs';
            row.innerHTML = '<div class="bubble"><a class="media" id="shot"' +
              ' href="/x?download=1" onclick="arcadeOpenImage(\\'/x\\', \\'cat.jpg\\',' +
              ' \\'/x?download=1\\'); return false">' +
              '<img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" alt="cat.jpg">' +
              '</a></div>';
            (document.querySelector('.bubbles') || document.body)
              .appendChild(row);""")
        browser.find_element(By.ID, "shot").click()
        time.sleep(0.4)

        state = browser.execute_script("""
            const box = document.getElementById('lightbox');
            return box && {
              open: box.classList.contains('on'),
              shown: getComputedStyle(box).display,
              name: box.querySelector('.lightbox-name').textContent,
              save: box.querySelector('.lightbox-save').getAttribute('href'),
              downloads: box.querySelector('.lightbox-save').hasAttribute('download'),
              frozen: document.body.style.overflow,
            };""")
        assert state, "no lightbox was opened"
        assert state["open"] and state["shown"] != "none"
        assert state["name"] == "cat.jpg"
        assert state["save"] == "/x?download=1", "saving is still one press away"
        assert state["downloads"]
        assert state["frozen"] == "hidden", "the page behind must not scroll"

        # Escape closes it, and so does a click on the ground behind.
        browser.execute_script(
            "document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape'}))")
        time.sleep(0.3)
        assert browser.execute_script(
            "return document.getElementById('lightbox').classList.contains('on')") is False
        assert browser.execute_script("return document.body.style.overflow") == ""
    finally:
        browser.switch_to.default_content()


def test_a_picture_still_works_without_javascript():
    """The anchor stays an anchor: middle-click, right-click and a browser with
    scripting off all behave as they did."""
    import pathlib as _p

    for name in ("messages.html", "groups.html"):
        source = _p.Path("arcade/web/templates", name).read_text()
        assert 'class="media" href=' in source, name
        assert "?download=1" in source, name
        assert "return false" in source, name

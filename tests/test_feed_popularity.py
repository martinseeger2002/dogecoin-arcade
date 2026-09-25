"""What puts a post at the top of the feed, and what a card remembers.

The feed used to be newest-first, which rewards posting last. This file is the
arithmetic that replaced it and the two properties that arithmetic has to keep
being true about: the ORDER BY and the number printed on the card are the same
function, and the order is a query rather than a sort of an assembled page.

Both properties are easy to lose quietly. The score lives in SQL (`store.py`)
and the total lives in Python (`feedview.py`) because one has to be an ORDER BY
and the other is drawn per card -- two files, one rule, and nothing stops them
drifting apart except a test that computes the same post both ways. And a page
sorted in Python after being fetched with a LIMIT is a page where a post nobody
was shown outranks one everybody saw, which no single request can show you: it
only ever looks like a slightly odd feed.

So most of this file asks the database and the view the same question about the
same rows. The last test is the one the feature was actually for: two people,
two different amounts, and the feed moving while you watch.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_feed import (arcade, _catch_up, _mined, _named,     # noqa: E402,F401
                               _posted, _sign_in)
from test_funding import _pubkey, _sign                          # noqa: E402

from arcade import feedview                                     # noqa: E402
from arcade.config import NETWORKS                              # noqa: E402
from arcade.messaging import feed                                # noqa: E402
from arcade.messaging.store import MessageStore                  # noqa: E402
from arcade.script import b58check_encode, hash160               # noqa: E402
from fastapi.testclient import TestClient                        # noqa: E402

COIN = 100_000_000
NET = "regtest"
ME = "nMeAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
THEM = "nThemAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
OTHER = "nOtherAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

#: The two accounts' coin keys, fixed scalars like every other account test
#: signs with. The first is the one the rest of the account suite uses.
SECRETS = (0x5151515151515151515151515151515151515151515151515151515151515151,
           0x5252525252525252525252525252525252525252525252525252525252525252)


@pytest.fixture
def store(tmp_path):
    return MessageStore(tmp_path / "m.sqlite")


def _post(store, txid, sender=ME, height=100, text="a post"):
    store.add_group_post(NET, "", txid, height, height * 10, sender, "", text)
    return txid


def _act(store, txid, kind, target, author=ME, height=100, amount=0,
         paid_on="", network=NET, text=""):
    store.add_feed_act(network, txid, kind, target, author, text, height,
                       height * 10, amount=amount, paid_on=paid_on)
    return txid


def _order(store, **kw):
    """The feed's own answer: txids, most-endorsed first."""
    return [r["txid"] for r in store.feed_posts_popular(NET, limit=50, **kw)]


def _score(store, txid):
    """One post's score, read out of the same query the ORDER BY uses."""
    row = store.conn.execute(
        f"SELECT {store._score('g')} AS score FROM group_post g"
        f" WHERE g.txid = ?", (txid,)).fetchone()
    return float(row["score"])


# --- the arithmetic ---------------------------------------------------------

def test_a_like_is_one_and_a_tip_starts_above_it():
    """A tip has to beat a like before its amount means anything, and the
    floor is what buys that: counted raw, a tenth of a coin would be worth
    less than somebody pressing a button for free."""
    assert feed.LIKE_VALUE == 1.0
    assert feed.tip_value(0, NET) == feed.TIP_FLOOR
    assert feed.TIP_FLOOR > feed.LIKE_VALUE


def test_a_hundred_times_the_money_never_doubles_the_endorsement():
    """Damping, because undamped this is an auction and everybody knows it.

    Checked at the decade marks rather than at one point: the shape is the
    decision. One adds about one, a hundred-fold adds about two -- "100x the
    money roughly twice as much" written as arithmetic -- and near zero the
    floor's `+1` presses the first decade flat, which is right: the distance
    between a tenth of a coin and ten coins should not be much of anything,
    and the distance between ten coins and ten thousand should be.
    """
    unit = feed.TIP_UNITS[NET]
    marks = [feed.tip_value(n * unit, NET) for n in (1, 10, 100, 10_000,
                                                     1_000_000)]
    assert marks == sorted(marks), "more money always says more, a little more"
    assert marks[2] < 2 * marks[0], "a hundred times the money, never twice it"
    assert marks[3] - marks[2] == pytest.approx(2.0, abs=0.05)
    assert marks[4] - marks[3] == pytest.approx(2.0, abs=0.05)
    assert marks[1] - marks[0] < 1.0, "and near nothing, less than that"
    assert 10 * marks[0] > feed.tip_value(1000 * unit, NET), \
        "ten people beat one rich one, which is the point"


def test_the_unit_is_a_constant_and_not_a_price():
    """The number a tip is worth may not depend on what a coin buys.

    A price in the score means a post's place changes when somebody sells,
    and a feed whose order moves with a market is a feed that can be moved
    from an exchange. So: one coin per chain, the same on all three, and a
    chain this build has never heard of gets the default rather than a zero.
    """
    assert feed.TIP_UNITS == {name: COIN for name in NETWORKS}
    assert feed.TIP_UNIT_DEFAULT == COIN
    assert feed.tip_value(COIN, "somechain") == feed.tip_value(COIN, NET)


def test_the_arithmetic_survives_a_chain_it_has_not_met():
    for amount in (0, -5, COIN):
        assert feed.tip_value(amount, "nowhere") >= feed.TIP_FLOOR - 1e-9


# --- the query and the card are one rule ------------------------------------

def _acts(store, targets, network=NET):
    """What a page hands the view: this chain's actions, plus the tips that
    were paid on the other one -- which is exactly what `app.py:_shown` does,
    and counting a regtest tip twice would be the same lie in a test."""
    acts = list(store.feed_acts_on(network, targets))
    seen = {a["txid"] for a in acts}
    return acts + [t for t in store.feed_tips_on(targets) if t["txid"] not in seen]


def test_the_order_and_the_card_agree_about_a_post(store):
    """The score in the ORDER BY and the total on the card are two files. That
    is a drift waiting to happen, so the same post is measured both ways."""
    post = _post(store, "ab" * 32, sender=THEM)
    tips = ((COIN, ME, NET), (5 * COIN, OTHER, "main"), (10 * COIN, OTHER, "test"))
    for n, (sats, who, network) in enumerate(tips):
        _act(store, f"{n:02d}" + "cd" * 30, feed.TIP, post, author=who,
             amount=sats, paid_on=network, network=network)
    _act(store, "ff" * 32, feed.LIKE, post, author=OTHER)
    shown = feedview.assemble(store.feed_posts(NET, limit=10),
                              _acts(store, [post]))
    assert _score(store, post) == pytest.approx(
        feed.LIKE_VALUE + sum(feed.tip_value(s, net) for s, _, net in tips))
    # ...and the card's own totals hold the chains apart rather than summing
    # a mainnet coin into a regtest one.
    assert shown[0].tipped == {NET: COIN, "main": 5 * COIN, "test": 10 * COIN}
    assert shown[0].tips == 3 and shown[0].likes == 1


def test_a_card_holds_the_chains_apart_and_a_score_adds_them(store):
    """A card that summed the chains would print a number that is nobody's
    balance. A score has to be one number, and says so by being one."""
    post = _post(store, "ab" * 32, sender=THEM)
    _act(store, "cd" * 32, feed.TIP, post, amount=COIN, paid_on="main",
         network="main")
    _act(store, "ef" * 32, feed.TIP, post, amount=COIN, paid_on=NET,
         network=NET)
    shown = feedview.assemble(store.feed_posts(NET, limit=10),
                              list(store.feed_tips_on([post])))
    assert shown[0].tipped == {"main": COIN, NET: COIN}
    assert _score(store, post) == pytest.approx(2 * feed.tip_value(COIN, NET))


def test_an_unconfirmed_tip_is_pending_and_counts_as_nothing(store):
    """It is shown, and it is not given. A tip can still be dropped, and a
    feed that reordered itself on things that then vanished reads as broken."""
    early = _post(store, "ab" * 32, sender=THEM, height=100)
    later = _post(store, "cd" * 32, sender=OTHER, height=101)
    assert _order(store) == [later, early]
    _act(store, "ef" * 32, feed.TIP, early, amount=1000 * COIN, paid_on=NET,
         height=0)
    assert _order(store) == [later, early], "still waiting for a block"
    shown = feedview.assemble([store.feed_post_by_txid(NET, early)],
                              list(store.feed_tips_on([early])))
    assert shown[0].tips == 1, "shown as pending"
    assert shown[0].tipped == {}, "and not counted as given"


def test_a_tip_row_with_no_chain_written_says_the_chain_it_was_read_on(store):
    """`paid_on` is newer than the rows, and a row written before it still has
    an amount to be worth something: the network the row lives on is the
    honest fallback, because that is the chain the scan read the transaction from."""
    post = _post(store, "ab" * 32, sender=THEM)
    _act(store, "cd" * 32, feed.TIP, post, amount=COIN, paid_on="",
         network="main")
    assert _score(store, post) == pytest.approx(feed.tip_value(COIN, "main"))


# --- who is counted ---------------------------------------------------------

def test_an_authors_own_tip_does_not_move_them(store):
    """With amounts counted, self-tipping is the cheapest road to the top of
    the feed, so it is dropped in a WHERE clause rather than judged."""
    cheap = _post(store, "ab" * 32, sender=THEM, height=100)
    honest = _post(store, "cd" * 32, sender=OTHER, height=101)
    _act(store, "ef" * 32, feed.TIP, cheap, author=THEM, amount=10_000 * COIN,
         paid_on=NET)
    assert _order(store) == [honest, cheap]
    shown = feedview.assemble([store.feed_post_by_txid(NET, cheap)],
                              list(store.feed_tips_on([cheap])))
    assert shown[0].tipped == {} and shown[0].tips == 0


def test_an_unlike_takes_a_like_back_of_course_it_does(store):
    """Newest wins, per person -- the same rule the counts already use, now
    with a place in the page attached to it."""
    post = _post(store, "ab" * 32, sender=THEM)
    other = _post(store, "cd" * 32, sender=OTHER, height=101)
    _act(store, "ef" * 32, feed.LIKE, post, author=ME, height=100)
    assert _order(store) == [post, other]
    _act(store, "f0" * 32, feed.UNLIKE, post, author=ME, height=102)
    assert _order(store) == [other, post]


def test_a_share_is_worth_a_like_and_no_more(store):
    """Sharing is a person putting their name on a post. That is a like's size
    and no more, or the feed would rank recency in a costume."""
    shared = _post(store, "ab" * 32, sender=THEM)
    plain = _post(store, "cd" * 32, sender=OTHER, height=101)
    _act(store, "ef" * 32, feed.SHARE, shared, author=ME)
    assert _score(store, shared) == pytest.approx(1.0)
    assert _order(store) == [shared, plain]


def test_muting_hides_words_and_moves_nothing(store):
    """Mute is not a ranking, and this query is where a ranking lives.

    The first version of `feed_posts_popular` dropped muted authors in its
    WHERE clause, which read well and broke the rule D-138 settled: a mute is
    a gap and an Unmute button, not a missing post, and the gap has to stand
    on a row this page was actually given. It also made the ORDER BY answer to
    whoever happened to mute somebody, so the same feed would order itself
    differently on two nodes with two mute lists. `test_feed_web.py` is the
    page half of this; this is the half that says the order did not move.
    """
    theirs = _post(store, "ab" * 32, sender=THEM)
    mine = _post(store, "cd" * 32, sender=OTHER, height=101)
    _act(store, "ef" * 32, feed.TIP, theirs, author=ME, amount=COIN,
         paid_on=NET)
    assert _order(store) == [theirs, mine]
    store.mute(THEM)
    assert _order(store) == [theirs, mine]
    assert store.muted() == {THEM}


def test_a_page_of_the_feed_is_a_page(store):
    """Twenty-five posts, ten at a time, and each one appears exactly once.

    The interesting half of a popularity order is paging it, and this is the
    quiet case: nothing moves between the pages. The link here carries an id
    and no score, so the store measures that post's score itself -- exact while
    nothing moves, and the next test is what happens when something does.
    """
    made = [_post(store, f"{i:02d}" + "ab" * 30, sender=THEM, height=100 + i,
                   text=f"post {i}") for i in range(25)]
    seen: list[str] = []
    cursor = None
    for _ in range(10):                       # ten is more than the four it takes
        page = store.feed_posts_popular(NET, cursor=cursor, limit=10)
        if not page:
            break
        seen.extend(r["txid"] for r in page)
        cursor = page[-1]["id"]
    assert len(seen) == 25 and set(seen) == set(made)
    assert seen == list(reversed(made)), "nothing endorsed: newest first"


def test_a_tip_landing_mid_scroll_repeats_no_page_and_loops_forever(store):
    """The scroll is halfway down the feed and somebody pays.

    Twenty-five plain posts, the first page read down to post 16, and then the
    post the cursor sits ON is tipped -- the ordinary case, because the post
    you are reading is the one you are most likely to tip.

    Carrying the anchor's score is what makes this come out right. Compared
    against the score as it is now, every post that used to sit below the
    anchor now sits under the anchor's new number and comes round again: the
    reader sees page one a second time, and if the tipping keeps up the
    "Older posts" link never runs out. Compared against the score as it WAS
    when the page was made, the cut stays where the reader stopped and the
    next page is the next ten posts.

    What that buys with is a post that rises from below the cursor to above it
    (`made[3]` here): it cannot appear mid-scroll, because it now belongs to
    ground the reader has passed. It is at the top of the feed the next time
    anybody loads the first page, which is the second half of this test.
    """
    made = [_post(store, f"{i:02d}" + "ab" * 30, height=100 + i,
                   text=f"post {i}") for i in range(25)]
    first = store.feed_posts_popular(NET, limit=10)
    assert [r["txid"] for r in first] == list(reversed(made[15:25]))

    _act(store, "ff" * 32, feed.TIP, made[15], author=OTHER, amount=COIN,
         paid_on=NET)                                  # the cursor's own post
    _act(store, "fe" * 32, feed.TIP, made[3], author=OTHER, amount=1,
         paid_on=NET)                                  # and one far below it

    seen, cursor = [r["txid"] for r in first], first[-1]["id"]
    anchor = first[-1]["score"]
    for _ in range(5):
        page = store.feed_posts_popular(NET, cursor=cursor, anchor=anchor,
                                        limit=10)
        if not page:
            break
        seen.extend(r["txid"] for r in page)
        cursor, anchor = page[-1]["id"], page[-1]["score"]
    assert len(seen) == len(set(seen)) == 24, seen
    assert made[3] not in seen, "it rose past the reader, so this scroll misses it"
    assert _order(store)[:2] == [made[15], made[3]], "and here it is, on reload"


def test_the_cursor_token_carries_both_halves_and_survives_losing_one():
    """The pair the last test depends on has to survive a URL.

    `before=412@7.4` is one query parameter holding two numbers, because a
    link whose two halves travel separately is a link that breaks when either
    is dropped -- and half the links to a feed were written by something that
    only ever knew an id.
    """
    from arcade.web.app import _cursor, _next_cursor

    assert _next_cursor([{"id": 412, "score": 7.4}], "popular") == "412@7.4"
    assert _next_cursor([{"id": 412, "score": 7.4}], "new") == 412
    assert _cursor("412@7.4") == (412, 7.4)
    assert _cursor("412") == (412, None)
    assert _cursor(412) == (412, None)
    assert _cursor(None) == (None, None)
    assert _cursor("nonsense") == (None, None)
    assert _cursor("412@not-a-score") == (412, None)
    # Truncated, never rounded: rounded UP, the cursor would sit above where
    # the reader stopped and would hand back a post they have already read.
    assert _next_cursor([{"id": 1, "score": 2.9999999}], "popular") == "1@2.999999"


def test_a_link_with_only_an_id_in_it_still_pages(store):
    """A bookmark, an old link, a hand-typed URL: the score is not in it.

    Then the store measures that post's score itself, which is exact on the
    first fetch and only drifts if somebody tips while the reader scrolls.
    Saying it in a test is the point: the fallback has to work, because the
    link is out there in the wild and this build is not the one that wrote it.
    """
    made = [_post(store, f"{i:02d}" + "ab" * 30, height=100 + i,
                   text=f"post {i}") for i in range(25)]
    first = store.feed_posts_popular(NET, limit=10)
    second = store.feed_posts_popular(NET, cursor=first[-1]["id"], limit=10)
    assert [r["txid"] for r in second] == list(reversed(made[5:15]))


def test_a_profile_keeps_what_they_said_in_the_order_they_said_it(store):
    """Somebody's own page is a record, not a ranking: the query takes the
    same order and the same mute list, and `newest first` is what a person's
    page is for."""
    early = _post(store, "ab" * 32, sender=THEM, height=100)
    late = _post(store, "cd" * 32, sender=THEM, height=105)
    _act(store, "ef" * 32, feed.TIP, early, author=OTHER, amount=COIN,
         paid_on=NET)
    assert [r["txid"] for r in store.feed_posts(NET, author=THEM, limit=50)] \
        == [late, early]


def test_an_old_store_gets_its_tips_back(store):
    """A column added with a default is a claim about every row that exists.

    Tip amounts used to live in the text, written as `chain:sats` by the one
    path that recorded them. Defaulting the new column to zero would leave
    every tip that machine had ever seen worth nothing -- and worth nothing
    forever, because nothing after the upgrade looks at that text again.
    """
    post = _post(store, "ab" * 32, sender=THEM)
    _act(store, "cd" * 32, feed.TIP, post, text="main:500000000")
    _act(store, "ef" * 32, feed.REPLY, post, text="hello: world")
    _act(store, "f0" * 32, feed.TIP, post, text="nonsense, not a shape")
    store._migrate()
    rows = {r["txid"]: r for r in store.feed_tips_on([post])}
    assert rows["cd" * 32]["amount"] == 5 * COIN
    assert rows["cd" * 32]["paid_on"] == "main"
    assert rows["f0" * 32]["amount"] == 0, "a text that is not that shape"
    # The one tip whose amount cannot be recovered is still a tip that
    # happened, and the floor is what a tip is worth before its amount is
    # known: worth the floor, and not worth the transaction it failed to read.
    assert _score(store, post) == pytest.approx(
        feed.tip_value(5 * COIN, "main") + feed.TIP_FLOOR)


def test_the_totals_are_the_stores_and_not_the_pages(store, tmp_path):
    """Restart-proof: the total is read from what the block watcher wrote, not
    added up while a page is drawn and forgotten."""
    post = _post(store, "ab" * 32, sender=THEM)
    _act(store, "cd" * 32, feed.TIP, post, amount=2 * COIN, paid_on=NET)
    reopened = MessageStore(tmp_path / "m.sqlite")
    assert _score(reopened, post) == pytest.approx(feed.tip_value(2 * COIN, NET))


# --- and the thing the feature was for --------------------------------------

@pytest.fixture
def pair(arcade):
    """Two people on one node, both funded, both named."""
    app, state, rpc = arcade
    rpc.call("generate", 120)
    _catch_up(state)
    people = []
    for secret, tag in zip(SECRETS, ("early", "late")):
        pubkey = _pubkey(secret)
        address = b58check_encode(state.messaging.params.pubkeyhash_version,
                                  hash160(pubkey))
        browser = TestClient(app.app)       # a second machine, its own seat
        _sign_in(browser)
        browser.post("/account/address", json={"address": address,
                                               "coin_pubkey": pubkey.hex()})
        rpc.call("sendtoaddress", address, 200.0)
        _mined(state, rpc)
        _named(browser, state, rpc, secret, pubkey, tag)
        people.append((browser, secret, pubkey, address))
    return app, state, rpc, people


def _pay(browser, state, rpc, where, body, secret, pubkey, mine=True):
    """Offer, sign, broadcast -- and, unless told otherwise, mine it."""
    offered = browser.post(where, json=body)
    assert offered.status_code == 200, offered.text
    offer = offered.json()
    signatures = [_sign(secret, bytes.fromhex(h)).hex()
                  for h in offer["sighashes"]]
    done = browser.post("/account/sign", json={
        "offer": offer["offer"], "signatures": signatures,
        "pubkey": pubkey.hex()})
    assert done.status_code == 200, done.text
    if mine:
        _mined(state, rpc)
    return offer, done.json()


def test_two_amounts_move_the_feed_and_the_cards_carry_them(pair):
    """What "sort by popularity" has to mean before it means anything: two
    people, two different amounts, and the page changing shape as the blocks
    land -- not a score computed somewhere a person never sees.

    The newer post starts on top, because that is what it has. Then the older
    one is tipped fifty coins and the newer one a single coin, and the page
    says so in both places: the order, and the total on the card.
    """
    app, state, rpc, (early, late) = pair
    early_browser, early_secret, early_key, _ = early
    late_browser, late_secret, late_key, _ = late
    older = _posted(early_browser, state, rpc, early_secret, early_key,
                    "said first")
    newer = _posted(late_browser, state, rpc, late_secret, late_key,
                    "said second")

    def feed_as(who):
        page = who.get("/account/feed").json()
        return ([p["txid"] for p in page["posts"]],
                {p["txid"]: p["tipped"] for p in page["posts"]})

    order, totals = feed_as(early_browser)
    assert order[:2] == [newer, older], "nothing has been endorsed yet"
    assert totals[newer] == {} and totals[older] == {}

    # Fifty coins on the older one, one coin on the newer -- both broadcast,
    # neither mined.
    paid, _ = _pay(late_browser, state, rpc, "/account/react",
                   {"txid": older, "kind": feed.TIP, "amount": "50"},
                   late_secret, late_key, mine=False)
    assert paid["paid"] == 50 * COIN
    gave, _ = _pay(early_browser, state, rpc, "/account/react",
                   {"txid": newer, "kind": feed.TIP, "amount": "1"},
                   early_secret, early_key, mine=False)
    assert gave["paid"] == COIN

    order, totals = feed_as(early_browser)
    assert order[:2] == [newer, older], "waiting for a block, still"
    assert totals[newer] == {} and totals[older] == {}, "not given yet"

    _mined(state, rpc)

    order, totals = feed_as(early_browser)
    assert order[:2] == [older, newer], "fifty coins beats one, and beats newest"
    assert totals[older] == {NET: 50 * COIN}
    assert totals[newer] == {NET: COIN}

    # And a stranger with no seat at all reads the same page in the same order
    # with the same numbers on the cards -- the order is the chain's, not the
    # reader's.
    stranger = TestClient(app.app)
    page = stranger.get("/feed")
    assert page.status_code == 200, page.text
    assert page.text.index("said first") < page.text.index("said second")
    assert "50" in page.text.split("said first")[1].split("said second")[0]

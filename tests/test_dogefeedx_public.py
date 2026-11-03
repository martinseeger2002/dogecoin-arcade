"""DogeFeedX reads public dogefeed.app data and does not invent any of it."""
import re
from pathlib import Path

from arcade.web.dogefeedx.public_read import (
    normalize_memes,
    parse_arcade,
    parse_launchpad,
    parse_leaderboard,
    parse_leaderboard_page,
    parse_market,
    parse_meme_count,
    parse_memes,
    parse_profile_card,
    parse_suggestions,
    parse_trending,
)

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "arcade" / "web" / "templates" / "dogefeedx"

HOME = """
<h2 class="px-4 pb-1 pt-3 text-lg">Trending</h2><ul><li><a href="/tag/dogecoin">
<span>1<!-- --> · Trending on DogeFeed</span><span>#<!-- -->dogecoin</span><span>2 posts</span></a></li></ul>
<h2>Who to follow</h2><ul><li><a href="/u/builderdogecoin"><img src="/api/media/local/eden.jpg" alt=""></a>
<a href="/u/builderdogecoin"><span class="font-bold">Eden<!-- --></span></a></li>
<li><a href="/u/billy.dogefeed"><img src="/doginal-img/abc" title="Verified Rare NFT profile picture"></a>
<a href="/u/billy.dogefeed"><span class="font-bold">Billy<!-- --></span></a></li></ul>
<h2>Leaderboard · 7D</h2><ol><li><a href="/u/bot2test"><span>1</span>
<img src="/art/avatar/shiba-sun"><span class="font-bold">Bot2 Test<!-- --></span><span>+<!-- -->8 DOGE</span></a></li>
<li><a href="/u/billy.dogefeed"><span>2</span><img src="/doginal-img/abc" title="Verified Rare NFT">
<span class="font-bold">Billy<!-- --></span><svg aria-label="DNS name"></svg><span>+<!-- -->2 DOGE</span></a></li></ol>
"""

PROFILE = '''<a href="/u/panocha/following"><b>6</b></a><a href="/u/panocha/followers"><b>1</b></a>Joined <!-- -->October 2026'''

MARKET = '''
<p>Collections</p><p class="truncate font-bold ">307</p>
<p>Items</p><p class="truncate font-bold ">7.2M</p>
<p>Live listings</p><p class="truncate font-bold ">74</p>
<p>Total traded</p><p class="truncate font-bold text-emerald-400" data-testid="market-total-traded">1,020.8 DOGE</p>
<a href="/market/devs"><img src="https://cdn.doggy.market/devs.png"><span class="truncate">.devs</span>
Floor <b class="text-cream">—</b> · <!-- -->0<!-- --> listed<!-- --> · <!-- -->0<!-- --> <!-- -->sales</a>
<a href="/market/offers"><span class="truncate">skip</span></a>
'''

BOARD = '''<a href="/leaderboard?range=all">ALL</a></nav><ol><li><a href="/u/latoshi"><span>1</span>
<img src="/api/media/local/lat.jpg"><span class="font-bold">Latoshi<!-- --></span><span>+<!-- -->3 DOGE</span></a></li></ol>
''' + HOME


def test_trending_comes_from_the_public_sidebar():
    rows = parse_trending(HOME)
    assert rows == [{"rank": 1, "tag": "dogecoin", "count": "2 posts"}]


def test_who_to_follow_is_deduped_and_marks_a_verified_picture():
    people = parse_suggestions(HOME)
    assert [p["handle"] for p in people] == ["builderdogecoin", "billy.dogefeed"]
    assert people[1]["verified"] is True
    assert people[0]["name"] == "Eden"


def test_sidebar_earners_keep_a_long_verified_row():
    rows = parse_leaderboard(HOME)
    assert [r["handle"] for r in rows] == ["bot2test", "billy.dogefeed"]
    assert rows[1]["amount"] == "+2 DOGE"
    assert rows[1]["verified"] is True


def test_leaderboard_page_uses_the_main_list_not_the_sidebar():
    rows = parse_leaderboard_page(BOARD)
    assert [r["handle"] for r in rows] == ["latoshi"]
    assert rows[0]["amount"] == "+3 DOGE"


def test_profile_card_reads_the_public_counts():
    card = parse_profile_card(PROFILE, "panocha")
    assert card == {"followers": 1, "following": 6, "joined": "October 2026"}


def test_market_keeps_a_blank_floor_and_the_traded_total():
    parsed = parse_market(MARKET)
    assert parsed["stats"]["volume"] == "1,020.8 DOGE"
    assert parsed["stats"]["collections"] == "307"
    assert [c["slug"] for c in parsed["collections"]] == ["devs"]
    assert parsed["collections"][0]["floor"] == "—"


MEMES = '''<a href="/memes/abc123def" class="card"><img src="/api/media/local/one.webp" alt="0 days without (Lenny, Simpsons)" class="aspect-square">'''

LAUNCH = '''<a data-testid="launchpad-card" href="/launchpad/999club"><img src="/api/media/local/club.jpg" alt="">
<p class="truncate font-black">999 Club</p><p class="text-xs text-white/60">69<!-- --> DOGE · <!-- -->Max 10 per mint</p>
<span data-testid="launchpad-progress">268<!-- --> / <!-- -->1,000<!-- --> minted</span>
<span data-testid="launchpad-progress-pct">26.8%</span></a>'''


def test_public_memes_come_from_the_library_grid():
    rows = parse_memes(MEMES)
    assert rows == [{
        "id": "abc123def",
        "name": "0 days without (Lenny, Simpsons)",
        "image": "/api/media/local/one.webp",
    }]
    assert parse_memes("<div>no grid</div>") == []


def test_meme_json_uses_image_url_and_drops_a_blank():
    rows = normalize_memes({"items": [
        {
            "id": "a", "title": "10 Guy", "imageUrl": "/api/media/local/one.webp",
            "format": "PNG", "uses": 2, "sourceName": "Imgflip", "tags": ["wow"],
        },
        {"id": "b", "title": "No picture", "imageUrl": ""},
        {"id": "c", "title": "Elsewhere", "imageUrl": "https://evil.example/pic.webp"},
    ]})
    assert rows == [{
        "id": "a",
        "name": "10 Guy",
        "image": "/api/media/local/one.webp",
        "format": "PNG",
        "category": "",
        "tags": ["wow"],
        "uses": 2,
        "sourceName": "Imgflip",
    }]


PAYLOAD = (
    r'\"slug\":\"sns-doge\",\"name\":\".doge names\",\"description\":\"DNS\",\"cover\":\"/sns-img/doge\",'
    r'\"verified\":true,\"exclusive\":false,\"mintOpen\":false,\"network\":\"Doginal (Dogecoin L1)\",'
    r'\"items\":1170,\"listed\":3,\"owners\":21,\"sales\":0,\"floorLabel\":\"500 DOGE\",\"volumeLabel\":\"0 DOGE\",'
    r'\"floorSort\":500,\"volumeSort\":0,\"createdAt\":\"2026-10-13T00:00:00.000Z\",\"isDoginal\":true,'
    r'\"isSns\":true,\"snsOrder\":0,\"chain\":\"dogecoin-sns\"},{\"id\":\"x\",\"slug\":\"999club\",'
    r'\"name\":\"999 Club\",\"description\":\"club\",\"cover\":\"/api/media/local/club.jpg\",\"verified\":true,'
    r'\"exclusive\":true,\"mintOpen\":true,\"network\":\"Doginal (Dogecoin L1)\",\"items\":747,\"listed\":0,'
    r'\"owners\":0,\"sales\":0,\"floorLabel\":\"—\",\"volumeLabel\":\"0 DOGE\",\"floorSort\":0,\"volumeSort\":0,'
    r'\"createdAt\":\"2026-10-23T04:38:10.054Z\",\"isDoginal\":true,\"isSns\":false,\"snsOrder\":-1,'
    r'\"chain\":\"dogecoin-l1\"},\"totals\":{\"doginals\":{\"collections\":307,\"items\":7186906,'
    r'\"liveListings\":74,\"itemsLabel\":\"7.2M\",\"listingsLabel\":\"74\",\"totalTraded\":\"1,020.8 DOGE\"},'
    r'\"robinhood\":{\"collections\":2,\"items\":2762,\"liveListings\":184,\"itemsLabel\":\"2.8k\",'
    r'\"listingsLabel\":\"184\",\"totalTraded\":\"108.628059 ETH\"},\"sns\":{\"collections\":6,\"items\":4577,'
    r'\"liveListings\":8,\"itemsLabel\":\"4.6k\",\"listingsLabel\":\"8\",\"totalTraded\":\"400 DOGE\"}},'
    r'\"trades\":[{\"kind\":\"trade\",\"listingId\":\"abc\",\"inscriptionId\":\"\",\"imageUrl\":\"/sns-img/doge\",'
    r'\"network\":\"doginals\",\"number\":1,\"name\":\"anon.doge\",\"collectionSlug\":\"sns-doge\",'
    r'\"collectionName\":\".doge names\",\"priceDoge\":\"10\",\"priceLabel\":\"10 DOGE\",\"usdLabel\":null,'
    r'\"at\":\"2026-11-03T00:00:00.000Z\",\"href\":\"/market/sns-doge/1\"'
)


def test_market_payload_keeps_dns_exclusive_and_a_sale():
    parsed = parse_market(PAYLOAD)
    by = {c["slug"]: c for c in parsed["collections"]}
    assert by["sns-doge"]["sns"] is True
    assert by["sns-doge"]["floor"] == "500 DOGE"
    assert by["999club"]["exclusive"] is True
    assert parsed["stats"]["items"] == "7.2M"
    assert parsed["stats"]["volume"] == "1,020.8 DOGE"
    assert parsed["totals"]["sns"]["collections"] == 6
    assert parsed["totals"]["robinhood"]["totalTraded"].endswith("ETH")
    assert parsed["activity"][0]["price"] == "10 DOGE"
    assert parsed["activity"][0]["href"] == "/market/sns-doge/1"
    assert by["999club"]["created"] == "2026-10-23T04:38:10.054Z"


ARCADE = (
    '<a data-testid="arcade-tile-poker" href="/arcade/poker"><div>'
    + ("x" * 2000)
    + '</div><p class="font-black">Poker</p>'
    + '<p class="text-xs text-white/60">Texas Hold\'em · free chips</p></a>'
    + '<a data-testid="arcade-tile-bark-hunt" href="/arcade/bark-hunt">'
    + '<p class="font-black">Bark Hunt</p>'
    + '<p class="text-xs text-white/50">Save the Doginals</p></a>'
)


def test_arcade_tiles_keep_the_name_after_a_long_picture():
    rows = parse_arcade(ARCADE)
    assert [row["slug"] for row in rows] == ["poker", "bark-hunt"]
    assert rows[0]["name"] == "Poker"
    assert rows[0]["href"] == "/arcade/poker"
    assert rows[0]["detail"] == "Texas Hold'em · free chips"
    assert rows[1]["name"] == "Bark Hunt"
    assert parse_arcade("<div>no games</div>") == []


def test_meme_count_reads_the_public_pager():
    page = 'data-testid="meme-count">968<!-- --> <!-- -->memes<!-- --> · page <!-- -->1<!-- --> of <!-- -->9</span>'
    assert parse_meme_count(page) == {"count": 968, "pages": 9}
    assert parse_meme_count("<div>no pager</div>") is None


def test_launchpad_cards_keep_the_public_mint_counts():
    rows = parse_launchpad(LAUNCH)
    assert rows == [{
        "slug": "999club",
        "name": "999 Club",
        "image": "/api/media/local/club.jpg",
        "detail": "69 DOGE · Max 10 per mint",
        "progress": "268 / 1,000 minted",
        "pct": "26.8%",
    }]
    assert parse_launchpad("<div>nothing minting</div>") == []


def test_runtime_pages_do_not_invent_posts_or_prices():
    banned = ("Doge Lord", "Shiba Master", "Crypto Doge", "Math.random", "leaderboardData", "I run this bitch")
    for name in ("feed.html", "post.html", "profile.html", "explore.html", "market.html", "leaderboard.html"):
        text = (TEMPLATES / name).read_text()
        for needle in banned:
            assert needle not in text, f"{name} still contains {needle}"
    feed = (TEMPLATES / "feed.html").read_text()
    post = (TEMPLATES / "post.html").read_text()
    assert "var API_BASE = '/dogefeedx/api';" in feed
    assert "var API_BASE = 'https://dogefeed.app'" not in feed
    assert "var API_BASE = 'https://dogefeed.app'" not in post
    assert "/feed?limit=" in feed
    assert "/posts/" in post
    assert "data.replies" in post
    market = (TEMPLATES / "market.html").read_text()
    assert "Cheapest" in market and "/market/doginals" in market
    desk = (ROOT / "arcade" / "web" / "templates" / "desktop.html").read_text()
    assert "DFX_DOCK_APPS.indexOf(k) < 0" in desk
    assert "openDfxFeed" in desk and "/dogefeedx/feed" in desk
    assert "openDfxMarket" in desk and "/dogefeedx/market" in desk
    assert "if (inDock.indexOf(k) >= 0) return false;" in desk
    # The dock stands in for the arcade pages it already opens. A second icon is a duplicate.
    for cover in ("'/feed': 'dfxhome'", "'/explore': 'dfxexplore'", "'/me': 'dfxsettings'",
                  "'/me/messages': 'dfxmessages'", "'/wallet': 'dfxwallet'", "'/nfts': 'dfxnfts'",
                  "'/notifications': 'dfxnotifications'", "'/create/ord': 'dfxinscribe'",
                  "'/apps': 'dfxarcade'", "'/mintpad/new': 'dfxlaunchpad'"):
        assert cover in desk, cover
    assert "if (DFX) fav = fav.filter(function (k) { return !DFX_COVERS[tabHref(k)]; });" in desk
    assert "if (DFX) keys = keys.filter(function (k) { return !DFX_COVERS[tabHref(k)]; });" in desk
    assert "function magnifyDock(" in desk and "function dockCount(" in desk and "dfx-badge" in desk
    dock = desk[desk.index("var DFX_DOCK = ["):desk.index("];", desk.index("var DFX_DOCK = ["))]
    assert re.findall(r"name: '([^']+)'", dock) == [
        "Home", "Profile", "Files", "Notifications", "Messages", "Explore", "Market", "Inscribe",
        "Wallet", "Launchpad", "NFTs", "Arcade", "Bookmarks", "Meme Library", "Leaderboard", "Settings",
    ]
    search = (TEMPLATES / "search.html").read_text()
    assert "/dogefeedx/api/market/doginals" in search
    assert "/dogefeedx/api/market/drc20?q=" in search
    assert "/dogefeedx/api/names?q=" in search
    assert "/dogefeedx/api/inscription?id=" in search
    profile = (TEMPLATES / "profile.html").read_text()
    assert "dogefeedx.profile" in profile and "Not sent to dogefeed.app." in profile
    assert 'html[data-skin="dogefeedx"], html[data-skin="dogefeedx"] body' in desk
    assert "[data-skin=\"dogefeedx\"] .dk { top: 28px; left: 0; bottom: 0;" in desk
    assert "[data-skin=\"dogefeedx\"] .dk-dock { display: flex; position: fixed; left: 50%; bottom: 0;" in desk
    files = (TEMPLATES / "files.html").read_text()
    assert "/account/nfts/collections" in files and "/account/nfts/pieces" in files
    assert "/account/token20?chain=" in files and "/account/names" in files
    assert 'id="coll-list"' in files
    memes = (TEMPLATES / "memes.html").read_text()
    assert 'id="meme-file"' in memes and "dogefeedx.memes" in memes and "dogefeedx.draftMeme" in memes
    assert "Pick a picture" in memes
    assert 'id="meme-q"' in memes and "Search memes, tags, creators" in memes
    assert "m.image" in memes and "referrerpolicy=\"no-referrer\"" in memes
    assert "dogefeedx.draftMeme" in feed
    assert "data.nextCursor" in feed
    market = (TEMPLATES / "market.html").read_text()
    assert 'data-chain="robinhood"' in market
    assert "No public list for this category" not in market
    apps = (ROOT / "arcade" / "web" / "dogefeedx" / "apps.py").read_text()
    assert '"nextCursor": data.get("nextCursor")' in apps
    assert "normalize_memes" in apps and "imageUrl" in (ROOT / "arcade" / "web" / "dogefeedx" / "public_read.py").read_text()
    launch = (TEMPLATES / "launchpad.html").read_text()
    assert "No public launch list" in launch
    assert "Doge Punks" not in launch and "Shiba Society" not in launch
    arcade = (TEMPLATES / "arcade.html").read_text()
    assert "Free chips, for fun only" in arcade and "On this arcade" in arcade
    assert "/dogefeedx/api/arcade" in arcade
    explore = (TEMPLATES / "explore.html").read_text()
    assert "Top posts this week" in explore and "Top earners today" in explore
    assert "range=1d" in explore and "document.body.classList.add('df-dogefeed')" in explore
    assert "flex: none" in explore and "flex: none" in feed and "flex: none" in profile
    assert 'class="df-hero-btn primary" href="/me"' in feed
    wallet = (TEMPLATES / "wallet.html").read_text()
    assert "Sample Addresses" not in wallet and "nghHJba6wBoWesbYQfkZiMTicoQANkpdvH" not in wallet
    nfts = (TEMPLATES / "nfts.html").read_text()
    assert "/dogefeedx/api/market/doginals" in nfts and "Total traded" in nfts
    assert 'id="meme-count"' in memes
    assert 'id="lookup-u"' in profile
    assert "boardRange" in (TEMPLATES / "leaderboard.html").read_text()
    assert "parse_arcade" in apps and '"/api/arcade"' in apps
    for name in (
        "feed.html", "explore.html", "market.html", "nfts.html", "leaderboard.html",
        "profile.html", "wallet.html", "launchpad.html", "notifications.html",
        "messages.html", "bookmarks.html", "arcade.html", "memes.html",
    ):
        page = (TEMPLATES / name).read_text()
        assert "Sign in coming soon" not in page
        assert 'href="#" class="df-nav-item' not in page
        assert "inscribe.doginals.org" not in page

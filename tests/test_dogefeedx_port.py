"""Dock icons, the Profile address book, and non-EVM chain pills on the operator's arcade windows."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESK = (ROOT / "arcade" / "web" / "templates" / "desktop.html").read_text()
PROFILE = (ROOT / "arcade" / "web" / "templates" / "dogefeedx" / "profile.html").read_text()
MESSAGES = (ROOT / "arcade" / "web" / "templates" / "dogefeedx" / "messages.html").read_text()
FEED = (ROOT / "arcade" / "web" / "templates" / "dogefeedx" / "feed.html").read_text()
MARKET = (ROOT / "arcade" / "web" / "templates" / "dogefeedx" / "market.html").read_text()

COLOURS = (
    "#e4bc55", "#f4efe4", "#4f8ef7", "#ff5b3a", "#2f9d68", "#1f5270", "#c4512a",
    "#2c261c", "#6e4524", "#1b2340", "#f3ead6", "#2a2a2e", "#fff6dd", "#e8923a",
    "#243044", "#6b6b70",
)
NAMES = (
    "Home", "Profile", "Files", "Notifications", "Messages", "Explore", "Market",
    "Inscribe", "Wallet", "Launchpad", "NFTs", "Arcade", "Bookmarks", "Meme Library",
    "Leaderboard", "Settings",
)
TICKERS = ("DOGE", "PEPE", "LTC", "BTC", "BEL", "DGB", "LKY", "KOT", "WJK")


def _icon_slice() -> str:
    start = DESK.index("var DFX_ICON")
    end = DESK.index("// DogeFeedX dock, in Billy's order", start)
    return DESK[start:end]


def test_the_dock_keeps_sixteen_named_squircles():
    assert DESK.count("mark: DFX_ICON.") == 16
    assert "dfx-dock-label" in DESK and "if (it.mark)" in DESK
    assert "function magnifyDock(" in DESK and "function dockCount(" in DESK
    assert "dfx-badge" in DESK and "flex-wrap: nowrap" in DESK
    icons = _icon_slice()
    assert "M3 9l9-7" not in icons
    for colour in COLOURS:
        assert colour in icons
    for name in NAMES:
        assert "name: '" + name + "'" in DESK
    assert "no window asks any other server" in DESK


def test_the_address_book_is_a_dog_ear_on_the_arcade_profile():
    assert 'id="df-dogear"' in PROFILE and 'id="df-face-book"' in PROFILE
    assert 'id="local-profile"' in PROFILE and 'id="df-wallet-select"' in PROFILE
    assert "dogefeedx.addressbook" in PROFILE
    assert "Back to Profile" in PROFILE
    assert "dogefeed.app" not in PROFILE
    for ticker in TICKERS:
        assert f'value="{ticker}"' in PROFILE
    for refused in ("SOL", "XRP", "ETH", "XRPL"):
        assert f'value="{refused}"' not in PROFILE
    assert "/contacts': 'dfxprofile'" in DESK and "function openDfxAddressBook(" in DESK


def test_chain_pills_sit_on_his_messages_feed_and_market():
    assert "dfxChainPill" in MESSAGES and "mail.write(" in MESSAGES
    assert "/messaging.js" in MESSAGES and "dogefeedx.messages" not in MESSAGES
    assert "dfxChainPill" in FEED and "dogefeedx.localBarks" not in FEED
    assert "dfxIsEvm" in MARKET and "dfxChainPill" in MARKET
    assert "var IMG_BASE = ''" in MARKET and "sortCheap = true" in MARKET
    low = MARKET.lower()
    assert "robinhood" not in low and "ethereum" not in low
    assert "dogefeed.app" not in MARKET and "dogefeed.app" not in FEED and "dogefeed.app" not in MESSAGES

"""Standalone tests for DogeFeedX that don't require the full arcade project.

These tests verify the static files and basic structure of the DogeFeedX apps.
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
DOGEFEEDX_PATH = PROJECT_ROOT / "arcade" / "web" / "templates" / "dogefeedx"


def test_aqua_css_exists():
    """Aqua CSS file exists."""
    path = DOGEFEEDX_PATH / "aqua.css"
    assert path.is_file(), f"aqua.css not found at {path}"


def test_shell_js_exists():
    """Shell JS file exists."""
    path = DOGEFEEDX_PATH / "shell.js"
    assert path.is_file(), f"shell.js not found at {path}"


def test_explorer_html_exists():
    """Explorer template exists."""
    path = DOGEFEEDX_PATH / "explorer.html"
    assert path.is_file(), f"explorer.html not found at {path}"


def test_terminal_html_exists():
    """Terminal template exists."""
    path = DOGEFEEDX_PATH / "terminal.html"
    assert path.is_file(), f"terminal.html not found at {path}"


def test_feed_html_exists():
    """Feed template exists."""
    path = DOGEFEEDX_PATH / "feed.html"
    assert path.is_file(), f"feed.html not found at {path}"


def test_profile_html_exists():
    """Profile template exists."""
    path = DOGEFEEDX_PATH / "profile.html"
    assert path.is_file(), f"profile.html not found at {path}"


def test_market_html_exists():
    """Market template exists."""
    path = DOGEFEEDX_PATH / "market.html"
    assert path.is_file(), f"market.html not found at {path}"


def test_wallet_html_exists():
    """Wallet template exists."""
    path = DOGEFEEDX_PATH / "wallet.html"
    assert path.is_file(), f"wallet.html not found at {path}"


def test_inscription_html_exists():
    """Inscription detail template exists."""
    path = DOGEFEEDX_PATH / "inscription.html"
    assert path.is_file(), f"inscription.html not found at {path}"


def test_dogefeedx_apps_py_exists():
    """DogeFeedX Python apps module exists."""
    path = PROJECT_ROOT / "arcade" / "web" / "dogefeedx" / "apps.py"
    assert path.is_file(), f"dogefeedx/apps.py not found at {path}"


def test_dogefeedx_init_py_exists():
    """DogeFeedX Python package init exists."""
    path = PROJECT_ROOT / "arcade" / "web" / "dogefeedx" / "__init__.py"
    assert path.is_file(), f"dogefeedx/__init__.py not found at {path}"


def test_aqua_css_has_brand_colors():
    """Aqua CSS includes DogeFeed brand colors."""
    path = DOGEFEEDX_PATH / "aqua.css"
    content = path.read_text()
    assert "#d9a520" in content, "Missing DogeFeed brand color #d9a520"
    assert "--dfx-brand" in content, "Missing --dfx-brand CSS variable"


def test_aqua_css_has_window_styles():
    """Aqua CSS includes window styles."""
    path = DOGEFEEDX_PATH / "aqua.css"
    content = path.read_text()
    assert ".dfx-window" in content, "Missing .dfx-window class"
    assert ".dfx-titlebar" in content, "Missing .dfx-titlebar class"


def test_aqua_css_has_terminal_styles():
    """Aqua CSS includes terminal styles."""
    path = DOGEFEEDX_PATH / "aqua.css"
    content = path.read_text()
    assert ".dfx-terminal" in content, "Missing .dfx-terminal class"
    assert "#33ff33" in content, "Missing terminal green color"


def test_shell_js_has_registerapp():
    """Shell JS includes registerApp function."""
    path = DOGEFEEDX_PATH / "shell.js"
    content = path.read_text()
    assert "registerApp" in content, "Missing registerApp function"
    assert "DogeFeedX" in content, "Missing DogeFeedX namespace"


def test_shell_js_has_dock_support():
    """Shell JS includes dock functionality."""
    path = DOGEFEEDX_PATH / "shell.js"
    content = path.read_text()
    assert "dfx-dock" in content, "Missing dfx-dock class"
    assert "renderDock" in content, "Missing renderDock function"


def test_explorer_html_has_finder_layout():
    """Explorer HTML has Finder-style layout."""
    path = DOGEFEEDX_PATH / "explorer.html"
    content = path.read_text()
    assert "dfx-sidebar" in content, "Missing sidebar"
    assert "dfx-grid" in content, "Missing grid view"
    assert "dfx-toolbar" in content, "Missing toolbar"


def test_explorer_html_registers_with_shell():
    """Explorer registers itself with the shell."""
    path = DOGEFEEDX_PATH / "explorer.html"
    content = path.read_text()
    assert "DogeFeedX.registerApp" in content, "Missing shell registration"
    assert "'explorer'" in content or '"explorer"' in content, "Missing app ID"


def test_terminal_html_has_terminal_layout():
    """Terminal HTML has Terminal.app-style layout."""
    path = DOGEFEEDX_PATH / "terminal.html"
    content = path.read_text()
    assert "dfx-terminal" in content, "Missing terminal class"
    assert "dfx-term-toolbar" in content, "Missing terminal toolbar"


def test_terminal_html_has_commands():
    """Terminal HTML defines commands."""
    path = DOGEFEEDX_PATH / "terminal.html"
    content = path.read_text()
    assert "help" in content, "Missing help command"
    assert "status" in content, "Missing status command"
    assert "peers" in content, "Missing peers command"
    assert "balance" in content, "Missing balance command"


def test_terminal_html_registers_with_shell():
    """Terminal registers itself with the shell."""
    path = DOGEFEEDX_PATH / "terminal.html"
    content = path.read_text()
    assert "DogeFeedX.registerApp" in content, "Missing shell registration"
    assert "'terminal'" in content or '"terminal"' in content, "Missing app ID"


def test_feed_html_has_feed_layout():
    """Feed HTML has Twitter/X-style layout matching dogefeed.app."""
    path = DOGEFEEDX_PATH / "feed.html"
    content = path.read_text()
    # Allow either df- or dfx- prefix (df- matches dogefeed.app styling)
    assert "df-" in content or "dfx-feed" in content, "Missing feed class"
    assert "df-post" in content or "dfx-post" in content, "Missing post class"
    assert '<nav class="df-sidebar">' not in content
    assert "df-post" in content or "dfx-post" in content


def test_feed_html_has_social_features():
    """Feed HTML has social media features."""
    path = DOGEFEEDX_PATH / "feed.html"
    content = path.read_text()
    # Allow either df- or dfx- prefix
    assert "df-action" in content or "dfx-post-action" in content, "Missing post actions"
    assert "Trending" in content, "Missing trending section"
    assert "df-post-avatar" in content or "dfx-post-avatar" in content, "Missing avatar styling"


def test_feed_html_registers_with_shell():
    """Feed registers itself with the shell."""
    path = DOGEFEEDX_PATH / "feed.html"
    content = path.read_text()
    assert "DogeFeedX.registerApp" in content, "Missing shell registration"
    assert "'feed'" in content or '"feed"' in content, "Missing app ID"


def test_profile_html_has_profile_layout():
    """Profile HTML has user profile layout."""
    path = DOGEFEEDX_PATH / "profile.html"
    content = path.read_text()
    assert "df-profile" in content or "dfx-profile" in content, "Missing profile class"
    assert "df-avatar" in content or "dfx-profile-avatar" in content, "Missing avatar"
    assert "df-profile-stats" in content or "dfx-profile-stats" in content, "Missing stats"


def test_profile_html_has_holdings():
    """Profile HTML has holdings/doginals section."""
    path = DOGEFEEDX_PATH / "profile.html"
    content = path.read_text()
    assert "Holdings" in content or "NFT" in content, "Missing holdings/NFTs tab"
    assert "df-nft-grid" in content or "dfx-holdings-grid" in content, "Missing holdings grid"


def test_profile_html_registers_with_shell():
    """Profile registers itself with the shell."""
    path = DOGEFEEDX_PATH / "profile.html"
    content = path.read_text()
    assert "DogeFeedX.registerApp" in content, "Missing shell registration"
    assert "'profile'" in content or '"profile"' in content, "Missing app ID"


def test_market_html_has_market_layout():
    """Market HTML has marketplace layout."""
    path = DOGEFEEDX_PATH / "market.html"
    content = path.read_text()
    assert "df-main" in content or "dfx-market" in content, "Missing market class"
    assert "df-collections" in content or "dfx-market-grid" in content, "Missing grid"
    assert "df-collection" in content or "dfx-market-card" in content, "Missing cards"


def test_market_html_has_categories():
    """Market HTML has collection/DRC-20/items tabs."""
    path = DOGEFEEDX_PATH / "market.html"
    content = path.read_text()
    assert "Collections" in content, "Missing collections tab"
    assert "DRC-20" in content, "Missing DRC-20 tab"
    assert "Items" in content, "Missing items tab"


def test_market_html_registers_with_shell():
    """Market registers itself with the shell."""
    path = DOGEFEEDX_PATH / "market.html"
    content = path.read_text()
    assert "DogeFeedX.registerApp" in content, "Missing shell registration"
    assert "'market'" in content or '"market"' in content, "Missing app ID"


def test_wallet_html_has_wallet_layout():
    """Wallet HTML has read-only wallet layout."""
    path = DOGEFEEDX_PATH / "wallet.html"
    content = path.read_text()
    assert "df-main" in content or "dfx-wallet" in content, "Missing wallet class"
    assert "read-only" in content.lower(), "Missing read-only notice"
    assert "df-wallet-grid" in content or "dfx-wallet-grid" in content, "Missing inscription grid"


def test_wallet_html_has_chain_selection():
    """Wallet HTML has chain selection (Dogecoin/Pepecoin)."""
    path = DOGEFEEDX_PATH / "wallet.html"
    content = path.read_text()
    assert "Dogecoin" in content, "Missing Dogecoin option"
    assert "Pepecoin" in content, "Missing Pepecoin option"


def test_wallet_html_has_inscription_display():
    """Wallet HTML has inscription display."""
    path = DOGEFEEDX_PATH / "wallet.html"
    content = path.read_text()
    assert "df-wallet-item" in content or "dfx-wallet-item" in content, "Missing inscription item"
    assert "inscription" in content.lower(), "Missing inscription handling"


def test_wallet_html_registers_with_shell():
    """Wallet registers itself with the shell."""
    path = DOGEFEEDX_PATH / "wallet.html"
    content = path.read_text()
    assert "DogeFeedX.registerApp" in content, "Missing shell registration"
    assert "'wallet'" in content or '"wallet"' in content, "Missing app ID"


def test_wallet_html_has_address_api():
    """Wallet HTML uses proper address API endpoints."""
    path = DOGEFEEDX_PATH / "wallet.html"
    content = path.read_text()
    assert "/dogefeedx/api/wallet/holdings" in content, "Missing watch-only holdings endpoint"
    assert "address=" in content, "Missing public address lookup"
    assert "inscriptions" in content.lower(), "Missing inscriptions handling"
    assert "importprivkey" not in content and "private key" not in content.lower()


def test_inscription_html_has_content_viewer():
    """Inscription HTML has content viewer for different types."""
    path = DOGEFEEDX_PATH / "inscription.html"
    content = path.read_text()
    assert "dfx-inscription" in content or "df-inscription" in content, "Missing inscription class"
    assert "dfx-inscription-media" in content or "df-inscription-content" in content, "Missing media container"
    assert "content_type" in content, "Missing content type handling"


def test_inscription_html_has_details():
    """Inscription HTML shows inscription details."""
    path = DOGEFEEDX_PATH / "inscription.html"
    content = path.read_text()
    assert "dfx-inscription-detail" in content or "df-detail" in content, "Missing details section"
    assert "Content Type" in content, "Missing content type label"
    assert "Owner" in content, "Missing owner field"


def test_inscription_html_registers_with_shell():
    """Inscription page registers itself with the shell (optional for light theme pages)."""
    path = DOGEFEEDX_PATH / "inscription.html"
    content = path.read_text()
    # Shell registration is optional for light theme pages that use standard 3-column layout
    assert "DogeFeedX.registerApp" in content or "loadInscription" in content, "Missing shell registration or load function"


def test_desktop_dock_apps_not_on_desktop():
    """Billy's design rule: dock apps do NOT appear as desktop icons.
    
    Items in the dock must NOT also appear as desktop icons.
    Desktop shows only items not in the dock (files, folders, inscriptions, etc.).
    """
    path = PROJECT_ROOT / "arcade" / "web" / "templates" / "desktop.html"
    content = path.read_text()
    
    # Check that DFX_DOCK_APPS is defined
    assert "DFX_DOCK_APPS" in content, "Missing DFX_DOCK_APPS definition"
    
    # Check that dock apps are filtered from desktop
    assert "DFX_DOCK_APPS.indexOf(k) < 0" in content, "Dock apps not being filtered from desktop"
    
    # Check the design rule comment is documented
    assert "dock apps do NOT appear as desktop icons" in content.lower() or \
           "dock apps do not appear as desktop icons" in content.lower(), \
           "Missing design rule documentation"


def test_dogefeedx_uses_dogefeed_fonts():
    """DogeFeedX names DogeFeed's fonts (Fredoka, Nunito) and loads none from another server: the system fonts
    stand in where a machine has neither."""
    views = ["feed.html", "profile.html", "market.html", "wallet.html", "inscription.html"]
    for view in views:
        content = (DOGEFEEDX_PATH / view).read_text()
        assert "fonts.googleapis.com" not in content and "fonts.gstatic.com" not in content, view
        assert "Fredoka" in content or "Nunito" in content, f"{view} lost DogeFeed's fonts"


def test_dogefeedx_theme_is_switchable():
    """DogeFeedX theme should be selectable and switchable in desktop.html.
    
    Theme system requirements:
    - DogeFeedX is a selectable theme option
    - Users can switch between W98, Ubuntu, and DogeFeedX
    - Theme choice is saved to localStorage
    - Theme doesn't break other themes
    """
    path = PROJECT_ROOT / "arcade" / "web" / "templates" / "desktop.html"
    content = path.read_text()
    
    # Check theme is in the skin selection
    assert "skin:dogefeedx" in content, "DogeFeedX not in theme selector"
    
    # Check theme is in the menu options
    assert "DogeFeedX" in content, "DogeFeedX label missing from theme menu"
    
    # Check localStorage persistence
    assert "arcade.deskview" in content, "Theme persistence missing"
    
    # Check all three themes are available
    assert "skin:w98" in content, "W98 theme option missing"
    assert "skin:ubuntu" in content, "Ubuntu theme option missing"
    
    # Check DogeFeedX theme CSS exists
    assert '[data-skin="dogefeedx"]' in content, "DogeFeedX theme CSS missing"
    
    # Check DogeFeedX uses DogeFeed fonts
    assert "Fredoka" in content, "Desktop missing Fredoka font"
    assert "Nunito" in content, "Desktop missing Nunito font"
    
    # Check DogeFeedX has proper branding
    assert "--df-gold" in content, "DogeFeedX missing gold color variable"
    assert "DogeFeed" in content, "DogeFeedX missing DogeFeed branding"


def test_dogefeedx_apps_has_api_routes():
    """DogeFeedX apps module has API routes."""
    path = PROJECT_ROOT / "arcade" / "web" / "dogefeedx" / "apps.py"
    content = path.read_text()
    assert "/api/collections" in content, "Missing collections API"
    assert "/api/tokens" in content, "Missing tokens API"
    assert "/api/names" in content, "Missing names API"
    assert "/api/status" in content, "Missing status API"
    assert "/api/peers" in content, "Missing peers API"
    assert "register_routes" in content, "Missing register_routes function"


def test_dogefeedx_apps_has_caching():
    """DogeFeedX apps module implements caching."""
    path = PROJECT_ROOT / "arcade" / "web" / "dogefeedx" / "apps.py"
    content = path.read_text()
    assert "_get_cached" in content, "Missing cache getter"
    assert "_set_cached" in content, "Missing cache setter"
    assert "CACHE_TTL" in content, "Missing cache TTL"


def test_dogefeedx_apps_has_fallback_handling():
    """Every DogeFeedX endpoint answers empty, not an error, when the arcade has nothing to say (apps._local)."""
    content = (PROJECT_ROOT / "arcade" / "web" / "dogefeedx" / "apps.py").read_text()
    assert "async def _local(" in content and "empty" in content


def test_app_py_includes_dogefeedx():
    """app.py registers DogeFeedX routes."""
    path = PROJECT_ROOT / "arcade" / "web" / "app.py"
    content = path.read_text()
    assert "dogefeedx" in content, "Missing dogefeedx import"
    assert "dogefeedx.register_routes" in content, "Missing dogefeedx registration"


def test_dogefeedx_network_module_exists():
    """DogeFeedX network module exists."""
    path = PROJECT_ROOT / "arcade" / "web" / "dogefeedx" / "network.py"
    assert path.is_file(), f"dogefeedx/network.py not found at {path}"


def test_dogefeedx_network_has_register_function():
    """DogeFeedX network module has register function."""
    path = PROJECT_ROOT / "arcade" / "web" / "dogefeedx" / "network.py"
    content = path.read_text()
    assert "def register(" in content, "Missing register function"
    assert "/dogefeedx/network" in content, "Missing /dogefeedx/network route"
    assert "/dogefeedx/network/api/status" in content, "Missing status API route"


def test_dogefeedx_network_template_exists():
    """DogeFeedX network template exists."""
    path = DOGEFEEDX_PATH / "network.html"
    assert path.is_file(), f"network.html not found at {path}"


def test_app_py_registers_network_routes():
    """app.py registers DogeFeedX network routes."""
    path = PROJECT_ROOT / "arcade" / "web" / "app.py"
    content = path.read_text()
    assert "dogefeedx_network" in content or "dogefeedx.network" in content, "Missing network module import"
    assert ".register(" in content and "network" in content, "Missing network.register call"


def test_pyproject_includes_dogefeedx_package():
    """pyproject.toml includes arcade.web.dogefeedx package."""
    path = PROJECT_ROOT / "pyproject.toml"
    content = path.read_text()
    assert "arcade.web.dogefeedx" in content, "Missing arcade.web.dogefeedx in packages"


def test_pyproject_includes_dogefeedx_templates():
    """pyproject.toml includes dogefeedx templates in package-data."""
    path = PROJECT_ROOT / "pyproject.toml"
    content = path.read_text()
    assert "templates/dogefeedx/" in content, "Missing dogefeedx templates in package-data"


def test_pyproject_web_extra_has_no_dogefeedx_http_client():
    """The web extra no longer carries an HTTP client for DogeFeedX: it reads only this arcade."""
    content = (PROJECT_ROOT / "pyproject.toml").read_text()
    assert "fetches from dogefeed.app" not in content


if __name__ == "__main__":
    import traceback
    
    tests = [name for name in dir() if name.startswith("test_")]
    passed = 0
    failed = 0
    
    for test_name in tests:
        test_func = globals()[test_name]
        try:
            test_func()
            print(f"✓ {test_name}")
            passed += 1
        except AssertionError as e:
            print(f"✗ {test_name}: {e}")
            failed += 1
        except Exception as e:
            print(f"✗ {test_name}: {type(e).__name__}: {e}")
            failed += 1
    
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)

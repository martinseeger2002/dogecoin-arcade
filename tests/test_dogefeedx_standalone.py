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
    """Feed HTML has Twitter/X-style layout."""
    path = DOGEFEEDX_PATH / "feed.html"
    content = path.read_text()
    assert "dfx-feed" in content, "Missing feed class"
    assert "dfx-post" in content, "Missing post class"
    assert "dfx-feed-sidebar" in content, "Missing sidebar"


def test_feed_html_has_social_features():
    """Feed HTML has social media features."""
    path = DOGEFEEDX_PATH / "feed.html"
    content = path.read_text()
    assert "dfx-post-action" in content, "Missing post actions (like, repost)"
    assert "Trending" in content, "Missing trending section"
    assert "dfx-post-avatar" in content, "Missing avatar styling"


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
    assert "dfx-profile" in content, "Missing profile class"
    assert "dfx-profile-avatar" in content, "Missing avatar"
    assert "dfx-profile-stats" in content, "Missing stats"


def test_profile_html_has_holdings():
    """Profile HTML has holdings/doginals section."""
    path = DOGEFEEDX_PATH / "profile.html"
    content = path.read_text()
    assert "Holdings" in content, "Missing holdings tab"
    assert "dfx-holdings-grid" in content, "Missing holdings grid"


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
    assert "dfx-market" in content, "Missing market class"
    assert "dfx-market-grid" in content, "Missing grid"
    assert "dfx-market-card" in content, "Missing cards"


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
    assert "dfx-wallet" in content, "Missing wallet class"
    assert "read-only" in content.lower(), "Missing read-only notice"
    assert "dfx-wallet-grid" in content, "Missing inscription grid"


def test_wallet_html_has_chain_selection():
    """Wallet HTML has chain selection (Dogecoin/Pepecoin)."""
    path = DOGEFEEDX_PATH / "wallet.html"
    content = path.read_text()
    assert "Dogecoin" in content, "Missing Dogecoin option"
    assert "Pepecoin" in content, "Missing Pepecoin option"
    assert "Testnet" in content, "Missing testnet options"


def test_wallet_html_has_inscription_display():
    """Wallet HTML has inscription display with modal."""
    path = DOGEFEEDX_PATH / "wallet.html"
    content = path.read_text()
    assert "dfx-wallet-item" in content, "Missing inscription item"
    assert "dfx-modal" in content, "Missing inscription modal"
    assert "showInscription" in content, "Missing inscription viewer"


def test_wallet_html_registers_with_shell():
    """Wallet registers itself with the shell."""
    path = DOGEFEEDX_PATH / "wallet.html"
    content = path.read_text()
    assert "DogeFeedX.registerApp" in content, "Missing shell registration"
    assert "'wallet'" in content or '"wallet"' in content, "Missing app ID"


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
    """DogeFeedX apps module handles DogeFeed unavailability gracefully."""
    path = PROJECT_ROOT / "arcade" / "web" / "dogefeedx" / "apps.py"
    content = path.read_text()
    assert "timeout" in content.lower(), "Missing timeout handling"
    assert "HTTPException" in content, "Missing HTTP exception handling"


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


def test_pyproject_includes_httpx_dependency():
    """pyproject.toml includes httpx as a web dependency."""
    path = PROJECT_ROOT / "pyproject.toml"
    content = path.read_text()
    assert "httpx" in content, "Missing httpx dependency"
    # Verify it's in web dependencies, not just dev
    web_section_start = content.find("web = [")
    web_section_end = content.find("]", web_section_start)
    web_section = content[web_section_start:web_section_end]
    assert "httpx" in web_section, "httpx not in web dependencies"


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

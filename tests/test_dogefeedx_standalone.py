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


def test_dogefeedx_py_exists():
    """DogeFeedX Python module exists."""
    path = PROJECT_ROOT / "arcade" / "web" / "dogefeedx.py"
    assert path.is_file(), f"dogefeedx.py not found at {path}"


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


def test_dogefeedx_py_has_api_routes():
    """DogeFeedX Python module has API routes."""
    path = PROJECT_ROOT / "arcade" / "web" / "dogefeedx.py"
    content = path.read_text()
    assert "/api/collections" in content, "Missing collections API"
    assert "/api/status" in content, "Missing status API"
    assert "/api/peers" in content, "Missing peers API"
    assert "register_routes" in content, "Missing register_routes function"


def test_dogefeedx_py_has_caching():
    """DogeFeedX Python module implements caching."""
    path = PROJECT_ROOT / "arcade" / "web" / "dogefeedx.py"
    content = path.read_text()
    assert "_get_cached" in content, "Missing cache getter"
    assert "_set_cached" in content, "Missing cache setter"
    assert "CACHE_TTL" in content, "Missing cache TTL"


def test_dogefeedx_py_has_fallback_handling():
    """DogeFeedX Python module handles DogeFeed unavailability gracefully."""
    path = PROJECT_ROOT / "arcade" / "web" / "dogefeedx.py"
    content = path.read_text()
    assert "timeout" in content.lower(), "Missing timeout handling"
    assert "HTTPException" in content, "Missing HTTP exception handling"


def test_app_py_includes_dogefeedx():
    """app.py registers DogeFeedX routes."""
    path = PROJECT_ROOT / "arcade" / "web" / "app.py"
    content = path.read_text()
    assert "dogefeedx" in content, "Missing dogefeedx import"
    assert "dogefeedx.register_routes" in content, "Missing dogefeedx registration"


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

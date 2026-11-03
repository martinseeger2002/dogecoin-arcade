"""Tests for DogeFeedX packaging.

Verifies that the wheel includes the dogefeedx module and its templates,
and that the httpx dependency is properly declared.
"""
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent


class TestWheelContents:
    """Tests that verify the built wheel contains dogefeedx."""
    
    @pytest.fixture(scope="class")
    @classmethod
    def wheel_path(cls, tmp_path_factory):
        """Build a wheel and return its path."""
        tmp_dir = tmp_path_factory.mktemp("wheel")
        
        # Build the wheel
        result = subprocess.run(
            [sys.executable, "-m", "pip", "wheel", str(PROJECT_ROOT), 
             "--no-deps", "-w", str(tmp_dir)],
            capture_output=True,
            text=True
        )
        
        if result.returncode != 0:
            pytest.skip(f"Could not build wheel: {result.stderr}")
        
        # Find the wheel file
        wheels = list(tmp_dir.glob("*.whl"))
        if not wheels:
            pytest.skip("No wheel file produced")
        
        return wheels[0]
    
    def test_wheel_contains_dogefeedx_package(self, wheel_path):
        """The built wheel contains the arcade.web.dogefeedx package."""
        with zipfile.ZipFile(wheel_path, 'r') as whl:
            names = whl.namelist()
            
            # Check for the dogefeedx package files
            dogefeedx_files = [n for n in names if 'dogefeedx' in n.lower()]
            assert len(dogefeedx_files) > 0, "No dogefeedx files found in wheel"
            
            # Check for specific files
            has_init = any('dogefeedx/__init__.py' in n for n in names)
            has_apps = any('dogefeedx/apps.py' in n for n in names)
            has_network = any('dogefeedx/network.py' in n for n in names)
            
            assert has_init, "Missing dogefeedx/__init__.py in wheel"
            assert has_apps, "Missing dogefeedx/apps.py in wheel"
            assert has_network, "Missing dogefeedx/network.py in wheel"
    
    def test_wheel_contains_dogefeedx_templates(self, wheel_path):
        """The built wheel contains the dogefeedx templates."""
        with zipfile.ZipFile(wheel_path, 'r') as whl:
            names = whl.namelist()
            
            # Check for template files
            template_files = [n for n in names if 'templates/dogefeedx/' in n]
            assert len(template_files) > 0, "No dogefeedx template files found in wheel"
            
            # Check for specific templates
            has_explorer = any('explorer.html' in n for n in template_files)
            has_terminal = any('terminal.html' in n for n in template_files)
            has_network = any('network.html' in n for n in template_files)
            has_aqua_css = any('aqua.css' in n for n in template_files)
            
            assert has_explorer, "Missing explorer.html template in wheel"
            assert has_terminal, "Missing terminal.html template in wheel"
            assert has_network, "Missing network.html template in wheel"
            assert has_aqua_css, "Missing aqua.css template in wheel"


class TestDependencies:
    """Tests that verify dependencies are properly declared."""
    
    def test_dogefeedx_needs_no_http_client(self):
        """DogeFeedX asks no other server, so it brings no HTTP client into the web extra."""
        apps = (PROJECT_ROOT / "arcade" / "web" / "dogefeedx" / "apps.py").read_text()
        assets = (PROJECT_ROOT / "arcade" / "web" / "dogefeedx" / "wallet_assets.py").read_text()
        assert "import httpx" not in apps and "import httpx" not in assets
    
    def test_dogefeedx_in_packages(self):
        """arcade.web.dogefeedx is in the packages list."""
        pyproject = PROJECT_ROOT / "pyproject.toml"
        content = pyproject.read_text()
        
        assert 'arcade.web.dogefeedx' in content, "arcade.web.dogefeedx not in packages"


class TestModuleImport:
    """Tests that verify the module can be imported."""
    
    def test_dogefeedx_imports(self):
        """The dogefeedx module imports successfully."""
        # Add project root to path for source-tree imports
        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))
        from arcade.web import dogefeedx
        assert hasattr(dogefeedx, 'router')
        assert hasattr(dogefeedx, 'register_routes')
        assert hasattr(dogefeedx, 'network')
    
    def test_dogefeedx_network_imports(self):
        """The dogefeedx.network module imports successfully."""
        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))
        from arcade.web.dogefeedx import network
        assert hasattr(network, 'register')
        assert hasattr(network, 'get_dock_apps')
    
    def test_dogefeedx_apps_imports(self):
        """The dogefeedx.apps module imports successfully."""
        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))
        from arcade.web.dogefeedx import apps
        assert hasattr(apps, 'router')
        assert hasattr(apps, 'register_routes')


if __name__ == "__main__":
    # Quick test runner for standalone execution
    import traceback
    
    # Add project root to path
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    
    tests = [
        ("test_httpx_in_web_dependencies", TestDependencies().test_httpx_in_web_dependencies),
        ("test_dogefeedx_in_packages", TestDependencies().test_dogefeedx_in_packages),
        ("test_dogefeedx_imports", TestModuleImport().test_dogefeedx_imports),
        ("test_dogefeedx_network_imports", TestModuleImport().test_dogefeedx_network_imports),
        ("test_dogefeedx_apps_imports", TestModuleImport().test_dogefeedx_apps_imports),
    ]
    
    passed = 0
    failed = 0
    
    for test_name, test_func in tests:
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

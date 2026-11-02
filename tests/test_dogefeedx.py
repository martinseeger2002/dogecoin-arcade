"""Tests for the DogeFeedX apps (Explorer and Terminal).

These tests verify the API endpoints and basic functionality of the DogeFeedX
Finder-style Explorer and Terminal.app-style Terminal.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
from fastapi import FastAPI

from arcade.web import dogefeedx


@pytest.fixture
def app():
    """Create a test app with DogeFeedX routes."""
    test_app = FastAPI()
    test_app.include_router(dogefeedx.router)
    
    def mock_render(request, template, **context):
        from fastapi.responses import HTMLResponse
        return HTMLResponse(f"<html><body>Template: {template}</body></html>")
    
    @test_app.get("/dogefeedx/explorer")
    def dogefeedx_explorer():
        return mock_render(None, "dogefeedx/explorer.html")
    
    @test_app.get("/dogefeedx/terminal")
    def dogefeedx_terminal():
        return mock_render(None, "dogefeedx/terminal.html")
    
    return test_app


@pytest.fixture
def client(app):
    """Create a test client."""
    return TestClient(app)


class TestDogeFeedXStaticFiles:
    """Test static file serving."""
    
    def test_aqua_css_served(self, client):
        """The Aqua CSS file is served."""
        response = client.get("/dogefeedx/aqua.css")
        assert response.status_code == 200
        assert "text/css" in response.headers.get("content-type", "")
        assert "dfx-" in response.text
    
    def test_shell_js_served(self, client):
        """The shell registration JS is served."""
        response = client.get("/dogefeedx/shell.js")
        assert response.status_code == 200
        assert "javascript" in response.headers.get("content-type", "")
        assert "DogeFeedX" in response.text
        assert "registerApp" in response.text
    
    def test_explorer_icon_served(self, client):
        """The Explorer icon is served (SVG fallback)."""
        response = client.get("/dogefeedx/icon-explorer.png")
        assert response.status_code == 200
        assert "svg" in response.headers.get("content-type", "").lower() or "png" in response.headers.get("content-type", "").lower()
    
    def test_terminal_icon_served(self, client):
        """The Terminal icon is served (SVG fallback)."""
        response = client.get("/dogefeedx/icon-terminal.png")
        assert response.status_code == 200
        assert "svg" in response.headers.get("content-type", "").lower() or "png" in response.headers.get("content-type", "").lower()


class TestDogeFeedXCollectionsAPI:
    """Test the collections API."""
    
    @patch('arcade.web.dogefeedx._fetch_dogefeed')
    def test_collections_list(self, mock_fetch, client):
        """Collections are listed from DogeFeed."""
        mock_fetch.return_value = [
            {"id": "coll1", "name": "Test Collection", "item_count": 100, "holder_count": 50},
            {"id": "coll2", "name": "Another Collection", "item_count": 200},
        ]
        
        response = client.get("/dogefeedx/api/collections")
        assert response.status_code == 200
        data = response.json()
        assert "collections" in data
        assert len(data["collections"]) == 2
        assert data["collections"][0]["name"] in ["Another Collection", "Test Collection"]
    
    @patch('arcade.web.dogefeedx._fetch_dogefeed')
    def test_collections_with_search(self, mock_fetch, client):
        """Collections can be searched by name."""
        mock_fetch.return_value = [
            {"id": "coll1", "name": "Doge Dogs", "item_count": 100},
            {"id": "coll2", "name": "Pepecoin Art", "item_count": 200},
        ]
        
        response = client.get("/dogefeedx/api/collections?q=doge")
        assert response.status_code == 200
        data = response.json()
        assert len(data["collections"]) == 1
        assert data["collections"][0]["name"] == "Doge Dogs"
    
    @patch('arcade.web.dogefeedx._fetch_dogefeed')
    def test_collections_pagination(self, mock_fetch, client):
        """Collections support pagination."""
        mock_fetch.return_value = [{"id": f"coll{i}", "name": f"Coll {i}"} for i in range(60)]
        
        response = client.get("/dogefeedx/api/collections?offset=0&limit=20")
        assert response.status_code == 200
        data = response.json()
        assert len(data["collections"]) == 20
        assert data["has_more"] == True


class TestDogeFeedXCollectionItemsAPI:
    """Test the collection items API."""
    
    def test_collection_items_requires_id(self, client):
        """Collection items endpoint requires an ID."""
        response = client.get("/dogefeedx/api/collection/items")
        assert response.status_code == 400
    
    @patch('arcade.web.dogefeedx._fetch_dogefeed')
    def test_collection_items_list(self, mock_fetch, client):
        """Collection items are listed."""
        mock_fetch.return_value = [
            {"id": "inscr1i0", "number": 1, "name": "Item 1"},
            {"id": "inscr2i0", "number": 2, "name": "Item 2"},
        ]
        
        response = client.get("/dogefeedx/api/collection/items?id=coll1")
        assert response.status_code == 200
        data = response.json()
        assert "items" in data
        assert len(data["items"]) == 2
        assert data["collection_id"] == "coll1"


class TestDogeFeedXInscriptionAPI:
    """Test the inscription details API."""
    
    def test_inscription_requires_id(self, client):
        """Inscription endpoint requires an ID."""
        response = client.get("/dogefeedx/api/inscription")
        assert response.status_code == 400
    
    @patch('arcade.web.dogefeedx._fetch_dogefeed')
    def test_inscription_details(self, mock_fetch, client):
        """Inscription details are returned."""
        mock_fetch.return_value = {
            "id": "abc123i0",
            "number": 12345,
            "content_type": "image/png",
            "owner": "D123456789abcdef",
        }
        
        response = client.get("/dogefeedx/api/inscription?id=abc123i0")
        assert response.status_code == 200
        data = response.json()
        assert "inscription" in data
        assert data["inscription"]["id"] == "abc123i0"


class TestDogeFeedXTerminalAPI:
    """Test the Terminal command APIs."""
    
    def test_status_endpoint(self, client):
        """Status endpoint returns arcade info."""
        response = client.get("/dogefeedx/api/status")
        assert response.status_code == 200
        data = response.json()
        assert "arcade" in data
        assert "version" in data
    
    def test_peers_endpoint(self, client):
        """Peers endpoint returns peer list."""
        response = client.get("/dogefeedx/api/peers")
        assert response.status_code == 200
        data = response.json()
        assert "peers" in data
        assert isinstance(data["peers"], list)
    
    def test_whoami_endpoint(self, client):
        """Whoami endpoint returns user info."""
        response = client.get("/dogefeedx/api/whoami")
        assert response.status_code == 200
        data = response.json()
        assert "user" in data
    
    def test_balance_requires_address(self, client):
        """Balance endpoint requires an address."""
        response = client.get("/dogefeedx/api/balance")
        assert response.status_code == 400
    
    def test_lookup_requires_name(self, client):
        """Lookup endpoint requires a name."""
        response = client.get("/dogefeedx/api/lookup")
        assert response.status_code == 400
    
    def test_search_requires_query(self, client):
        """Search endpoint requires a query of at least 2 characters."""
        response = client.get("/dogefeedx/api/search")
        assert response.status_code == 400
        
        response = client.get("/dogefeedx/api/search?q=a")
        assert response.status_code == 400


class TestDogeFeedXShellRegistration:
    """Test the shell registration stub."""
    
    def test_shell_js_has_registerapp(self, client):
        """Shell JS includes the registerApp function."""
        response = client.get("/dogefeedx/shell.js")
        assert response.status_code == 200
        content = response.text
        assert "registerApp" in content
        assert "DogeFeedX" in content
        assert "onReady" in content
    
    def test_shell_js_has_dock_support(self, client):
        """Shell JS includes dock functionality."""
        response = client.get("/dogefeedx/shell.js")
        content = response.text
        assert "dfx-dock" in content
        assert "renderDock" in content


class TestDogeFeedXCaching:
    """Test the caching mechanism."""
    
    def test_cache_helper_set_and_get(self):
        """Cache helper sets and gets values."""
        cache = {}
        dogefeedx._set_cached(cache, "key1", {"value": 123})
        result = dogefeedx._get_cached(cache, "key1")
        assert result == {"value": 123}
    
    def test_cache_helper_expires(self):
        """Cache entries expire after TTL."""
        import time
        cache = {}
        dogefeedx._set_cached(cache, "key1", {"value": 123})
        result = dogefeedx._get_cached(cache, "key1", ttl=0.01)
        assert result == {"value": 123}
        
        time.sleep(0.02)
        result = dogefeedx._get_cached(cache, "key1", ttl=0.01)
        assert result is None
    
    def test_cache_cleanup_on_overflow(self):
        """Cache cleans up when it exceeds 1000 entries."""
        cache = {}
        for i in range(1100):
            dogefeedx._set_cached(cache, f"key{i}", {"value": i})
        assert len(cache) <= 1001


class TestDogeFeedXAquaCSS:
    """Test the Aqua CSS styles."""
    
    def test_aqua_css_has_brand_colors(self, client):
        """Aqua CSS includes DogeFeed brand colors."""
        response = client.get("/dogefeedx/aqua.css")
        content = response.text
        assert "#d9a520" in content or "d9a520" in content
        assert "dfx-brand" in content
    
    def test_aqua_css_has_window_styles(self, client):
        """Aqua CSS includes window styles."""
        response = client.get("/dogefeedx/aqua.css")
        content = response.text
        assert "dfx-window" in content
        assert "dfx-titlebar" in content
        assert "dfx-content" in content
    
    def test_aqua_css_has_terminal_styles(self, client):
        """Aqua CSS includes terminal styles."""
        response = client.get("/dogefeedx/aqua.css")
        content = response.text
        assert "dfx-terminal" in content
        assert "#33ff33" in content

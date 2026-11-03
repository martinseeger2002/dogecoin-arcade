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


@pytest.fixture
def local(monkeypatch):
    """A stand-in for the arcade's data (app.py dfx_local): the endpoints read only this."""
    import types
    from arcade.web.dogefeedx import apps
    rows = [{"id": "doge-dogs", "name": "Doge Dogs", "item_count": 100},
            {"id": "pepe-art", "name": "Pepecoin Art", "item_count": 200}]
    seen = []

    def collections(offset=0, limit=50, q="", sort="name"):
        seen.append(("collections", offset, limit, q, sort))
        got = [r for r in rows if q.lower() in r["name"].lower()]
        return {"collections": got[offset:offset + limit], "total": len(got), "offset": offset, "limit": limit,
                "has_more": len(got) > offset + limit}

    def collection_items(slug, offset=0, limit=50):
        seen.append(("items", slug))
        return {"items": [{"id": "a" * 64 + "i0", "number": 1, "name": "Item 1"}], "collection_id": slug,
                "offset": offset, "limit": limit, "has_more": False}

    def inscription(iid):
        seen.append(("inscription", iid))
        return {"inscription": {"id": iid, "number": 12345, "content_type": "image/png", "image_url": f"/content/{iid}"}}

    fake = types.SimpleNamespace(collections=collections, collection_items=collection_items, inscription=inscription,
                                 seen=seen)
    monkeypatch.setattr(apps, "LOCAL", fake)
    apps._cache.clear()
    yield fake
    apps._cache.clear()


class TestDogeFeedXCollectionsAPI:
    """Collections come from the arcade's own database (the provider), never another server."""

    def test_collections_list(self, client, local):
        data = client.get("/dogefeedx/api/collections").json()
        assert [c["name"] for c in data["collections"]] == ["Doge Dogs", "Pepecoin Art"]
        assert data["source"] == "arcade" and data["image_base"] == "/content"

    def test_collections_with_search(self, client, local):
        data = client.get("/dogefeedx/api/collections?q=doge").json()
        assert [c["name"] for c in data["collections"]] == ["Doge Dogs"]
        assert ("collections", 0, 50, "doge", "name") in local.seen

    def test_collections_limits_are_bounded(self, client, local):
        client.get("/dogefeedx/api/collections?offset=-5&limit=5000&sort=bogus")
        assert ("collections", 0, 200, "", "name") in local.seen

    def test_no_provider_answers_empty(self, client, monkeypatch):
        from arcade.web.dogefeedx import apps
        monkeypatch.setattr(apps, "LOCAL", None)
        data = client.get("/dogefeedx/api/collections").json()
        assert data["collections"] == [] and data["source"] == "arcade"


class TestDogeFeedXCollectionItemsAPI:
    def test_collection_items_requires_id(self, client):
        assert client.get("/dogefeedx/api/collection/items").status_code == 400
        assert client.get("/dogefeedx/api/collection/items?id=../x").status_code == 400

    def test_collection_items_list(self, client, local):
        data = client.get("/dogefeedx/api/collection/items?id=doge-dogs").json()
        assert len(data["items"]) == 1 and data["collection_id"] == "doge-dogs"
        assert ("items", "doge-dogs") in local.seen


class TestDogeFeedXInscriptionAPI:
    def test_inscription_requires_id(self, client):
        assert client.get("/dogefeedx/api/inscription").status_code == 400
        assert client.get("/dogefeedx/api/inscription?id=abc123i0").status_code == 400

    def test_inscription_details(self, client, local):
        iid = "b" * 64 + "i0"
        data = client.get(f"/dogefeedx/api/inscription?id={iid}").json()
        assert data["inscription"]["id"] == iid
        assert data["inscription"]["image_url"] == f"/content/{iid}"


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
        dogefeedx.apps._set_cached(cache, "key1", {"value": 123})
        result = dogefeedx.apps._get_cached(cache, "key1")
        assert result == {"value": 123}
    
    def test_cache_helper_expires(self):
        """Cache entries expire after TTL."""
        import time
        cache = {}
        dogefeedx.apps._set_cached(cache, "key1", {"value": 123})
        result = dogefeedx.apps._get_cached(cache, "key1", ttl=0.01)
        assert result == {"value": 123}
        
        time.sleep(0.02)
        result = dogefeedx.apps._get_cached(cache, "key1", ttl=0.01)
        assert result is None
    
    def test_cache_cleanup_on_overflow(self):
        """Cache cleans up when it exceeds 1000 entries."""
        cache = {}
        for i in range(1100):
            dogefeedx.apps._set_cached(cache, f"key{i}", {"value": i})
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

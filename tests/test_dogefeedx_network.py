"""Tests for DogeFeedX Network app.

Tests the Network app module that shows node status, sync progress,
and mesh peers with Mac OS X Aqua styling.
"""
from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from arcade.web.state import AppState, ChainContext
from arcade.web.dogefeedx import network


@pytest.fixture
def mock_chain():
    """Create a mock chain context."""
    chain = MagicMock(spec=ChainContext)
    chain.network = "test"
    chain.letter = "T"
    chain.family = "PEP"
    chain.is_addon = False
    chain.is_mainnet = False
    chain.borrowed = False
    chain.title = "Testnet"

    mock_params = MagicMock()
    mock_params.label = "Pepecoin Test"
    chain.params = mock_params

    return chain


@pytest.fixture
def mock_state(mock_chain, tmp_path):
    """Create a mock AppState."""
    state = MagicMock(spec=AppState)
    state.home = tmp_path
    state.messaging = mock_chain
    state.ledger = mock_chain
    state.addons = []

    def chain_sync(chain):
        return {
            "ready": True,
            "online": True,
            "blocks": 100000,
            "headers": 100000,
            "progress": 1.0,
        }
    state.chain_sync = chain_sync

    mesh = MagicMock()
    mesh.node = MagicMock()
    mesh.node.links = {"abc123": MagicMock(), "def456": MagicMock()}
    mesh.operators = MagicMock()
    mesh.operators.status.return_value = [
        {"node": "abc123", "tag": "testoperator", "linked": True, "hosts": ["test.example.com"]},
    ]
    state.mesh = mesh

    return state


@pytest.fixture
def app(mock_state):
    """Create a FastAPI app with the network module registered."""
    app = FastAPI()
    network.clear_dock_apps()
    network.register(app, mock_state)
    return app


@pytest.fixture
def client(app):
    """Create a test client."""
    return TestClient(app)


class TestNetworkModule:
    """Tests for the network module."""

    def test_register_adds_dock_app(self, app):
        """Register should add the app to the dock."""
        apps = network.get_dock_apps()
        assert len(apps) == 1
        assert apps[0]["id"] == "dogefeedx.network"
        assert apps[0]["name"] == "Network"
        assert apps[0]["href"] == "/dogefeedx/network"

    def test_clear_dock_apps(self):
        """clear_dock_apps should remove all registered apps."""
        network._dock_apps.append({"id": "test", "name": "Test", "href": "/test", "icon": "/test.png"})
        network.clear_dock_apps()
        assert len(network.get_dock_apps()) == 0


class TestNetworkPage:
    """Tests for the network page endpoint."""

    def test_network_page_renders(self, client):
        """The network page should render successfully."""
        resp = client.get("/dogefeedx/network")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        assert "Network" in resp.text
        assert "dfx-network" in resp.text

    def test_network_page_has_aqua_styling(self, client):
        """The page should have Mac OS X Aqua styling elements."""
        resp = client.get("/dogefeedx/network")
        assert "dfx-titlebar" in resp.text
        assert "dfx-traffic-lights" in resp.text
        assert "linear-gradient" in resp.text


class TestNetworkStatusAPI:
    """Tests for the /api/status endpoint."""

    def test_status_returns_nodes(self, client):
        """Status endpoint should return node information."""
        resp = client.get("/dogefeedx/network/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data
        assert isinstance(data["nodes"], list)

    def test_status_returns_mesh_peers(self, client):
        """Status endpoint should return mesh peer information."""
        resp = client.get("/dogefeedx/network/api/status")
        data = resp.json()
        assert "mesh_peers" in data
        assert isinstance(data["mesh_peers"], list)

    def test_status_returns_trusted_nodes(self, client):
        """Status endpoint should return trusted node information."""
        resp = client.get("/dogefeedx/network/api/status")
        data = resp.json()
        assert "trusted_nodes" in data
        assert isinstance(data["trusted_nodes"], list)

    def test_status_returns_timestamp(self, client):
        """Status endpoint should return a timestamp."""
        resp = client.get("/dogefeedx/network/api/status")
        data = resp.json()
        assert "timestamp" in data
        assert isinstance(data["timestamp"], (int, float))

    def test_status_no_cache(self, client):
        """Status endpoint should have no-store cache control."""
        resp = client.get("/dogefeedx/network/api/status")
        assert resp.headers.get("cache-control") == "no-store"


class TestNetworkChainPeersAPI:
    """Tests for the /api/peers/{network} endpoint."""

    def test_chain_peers_returns_network(self, client):
        """Chain peers endpoint should return the network name."""
        resp = client.get("/dogefeedx/network/api/peers/test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["network"] == "test"

    def test_chain_peers_returns_peers_list(self, client):
        """Chain peers endpoint should return a peers list."""
        resp = client.get("/dogefeedx/network/api/peers/test")
        data = resp.json()
        assert "peers" in data
        assert isinstance(data["peers"], list)

    def test_chain_peers_unknown_network(self, client):
        """Chain peers endpoint should 404 for unknown networks."""
        resp = client.get("/dogefeedx/network/api/peers/unknown_chain")
        assert resp.status_code == 404


class TestNetworkSyncAPI:
    """Tests for the /api/sync/{network} endpoint."""

    def test_sync_requires_post(self, client):
        """Sync endpoint should only accept POST."""
        resp = client.get("/dogefeedx/network/api/sync/test")
        assert resp.status_code == 405

    def test_sync_unknown_network(self, client):
        """Sync endpoint should 404 for unknown networks."""
        resp = client.post("/dogefeedx/network/api/sync/unknown_chain")
        assert resp.status_code == 404

    def test_sync_already_synced(self, client, mock_state):
        """Sync endpoint should indicate if already synced."""
        resp = client.post("/dogefeedx/network/api/sync/test")
        assert resp.status_code == 200
        data = resp.json()
        assert "synced" in data.get("detail", "").lower() or "status" in data


class TestNetworkIcon:
    """Tests for the network icon endpoint."""

    def test_icon_returns_svg(self, client):
        """Icon endpoint should return an SVG."""
        resp = client.get("/dogefeedx/network/icon-32.png")
        assert resp.status_code == 200
        assert "image/svg+xml" in resp.headers.get("content-type", "")

    def test_icon_is_cacheable(self, client):
        """Icon should be cacheable."""
        resp = client.get("/dogefeedx/network/icon-32.png")
        assert "max-age" in resp.headers.get("cache-control", "")

    def test_icon_has_network_graphic(self, client):
        """Icon should have network-related graphic elements."""
        resp = client.get("/dogefeedx/network/icon-32.png")
        assert b"circle" in resp.content
        assert b"line" in resp.content


class TestNodeStatus:
    """Tests for node status calculation."""

    def test_synced_node_shows_100_percent(self, client, mock_state):
        """A synced node should show 100% progress."""
        resp = client.get("/dogefeedx/network/api/status")
        data = resp.json()
        node = data["nodes"][0]
        assert node["progress"] == 1.0
        assert node["progress_pct"] == 100.0

    def test_syncing_node_shows_progress(self, mock_state, tmp_path):
        """A syncing node should show partial progress."""
        def syncing_chain_sync(chain):
            return {
                "ready": False,
                "online": True,
                "blocks": 50000,
                "headers": 100000,
                "progress": 0.5,
            }
        mock_state.chain_sync = syncing_chain_sync

        app = FastAPI()
        network.clear_dock_apps()
        network.register(app, mock_state)
        client = TestClient(app)

        resp = client.get("/dogefeedx/network/api/status")
        data = resp.json()
        node = data["nodes"][0]
        assert node["progress"] == 0.5
        assert node["behind"] == 50000

    def test_offline_node_shows_offline(self, mock_state, tmp_path):
        """An offline node should show as offline."""
        def offline_chain_sync(chain):
            return {
                "ready": False,
                "online": False,
                "blocks": 0,
                "headers": 0,
                "progress": 0.0,
            }
        mock_state.chain_sync = offline_chain_sync

        app = FastAPI()
        network.clear_dock_apps()
        network.register(app, mock_state)
        client = TestClient(app)

        resp = client.get("/dogefeedx/network/api/status")
        data = resp.json()
        node = data["nodes"][0]
        assert node["online"] is False


class TestMeshPeers:
    """Tests for mesh peer information."""

    def test_mesh_peers_include_tag(self, client):
        """Mesh peers should include operator tag if available."""
        resp = client.get("/dogefeedx/network/api/status")
        data = resp.json()
        assert len(data["mesh_peers"]) > 0
        peer = data["mesh_peers"][0]
        assert "tag" in peer

    def test_mesh_peers_include_hosts(self, client):
        """Mesh peers should include hosts if available."""
        resp = client.get("/dogefeedx/network/api/status")
        data = resp.json()
        assert len(data["mesh_peers"]) > 0
        peer = data["mesh_peers"][0]
        assert "hosts" in peer


class TestTrustedNodes:
    """Tests for trusted node information."""

    def test_trusted_nodes_from_mesh_links(self, client):
        """Trusted nodes should come from mesh links."""
        resp = client.get("/dogefeedx/network/api/status")
        data = resp.json()
        assert len(data["trusted_nodes"]) > 0

    def test_trusted_node_has_id(self, client):
        """Each trusted node should have an ID."""
        resp = client.get("/dogefeedx/network/api/status")
        data = resp.json()
        for node in data["trusted_nodes"]:
            assert "id" in node


class TestDockRegistration:
    """Tests for the dock registration system."""

    def test_register_with_dock_stores_app(self):
        """register_with_dock should store app information."""
        network.clear_dock_apps()
        network.register_with_dock("test.app", "Test App", "/test", "/test.png")

        apps = network.get_dock_apps()
        assert len(apps) == 1
        assert apps[0]["id"] == "test.app"
        assert apps[0]["name"] == "Test App"
        assert apps[0]["href"] == "/test"
        assert apps[0]["icon"] == "/test.png"

    def test_multiple_apps_can_register(self):
        """Multiple apps should be able to register."""
        network.clear_dock_apps()
        network.register_with_dock("app1", "App 1", "/app1", "/app1.png")
        network.register_with_dock("app2", "App 2", "/app2", "/app2.png")

        apps = network.get_dock_apps()
        assert len(apps) == 2

    def test_get_dock_apps_returns_copy(self):
        """get_dock_apps should return a copy, not the original list."""
        network.clear_dock_apps()
        network.register_with_dock("test", "Test", "/test", "/test.png")

        apps1 = network.get_dock_apps()
        apps2 = network.get_dock_apps()
        assert apps1 is not apps2
        assert apps1 == apps2


class TestMultipleChains:
    """Tests for handling multiple blockchain chains."""

    def test_multiple_chains_all_listed(self, mock_state, tmp_path):
        """All chains (messaging, ledger, addons) should be listed."""
        addon_chain = MagicMock(spec=ChainContext)
        addon_chain.network = "doge"
        addon_chain.letter = "D"
        addon_chain.family = "DOGE"
        addon_chain.is_addon = True
        addon_chain.is_mainnet = True
        addon_chain.borrowed = False
        addon_chain.params = MagicMock()
        addon_chain.params.label = "Dogecoin"

        mock_state.addons = [addon_chain]

        app = FastAPI()
        network.clear_dock_apps()
        network.register(app, mock_state)
        client = TestClient(app)

        resp = client.get("/dogefeedx/network/api/status")
        data = resp.json()
        networks = [n["network"] for n in data["nodes"]]
        assert "test" in networks
        assert "doge" in networks

    def test_unique_nodes_no_duplicates(self, mock_state, tmp_path):
        """Duplicate networks should be filtered out."""
        mock_state.messaging = mock_state.ledger

        app = FastAPI()
        network.clear_dock_apps()
        network.register(app, mock_state)
        client = TestClient(app)

        resp = client.get("/dogefeedx/network/api/status")
        data = resp.json()
        networks = [n["network"] for n in data["nodes"]]
        assert networks.count("test") == 1


class TestErrorHandling:
    """Tests for error handling."""

    def test_mesh_error_returns_empty_list(self, mock_state, tmp_path):
        """Mesh errors should return empty lists, not fail."""
        mock_state.mesh.operators.status.side_effect = Exception("Test error")

        app = FastAPI()
        network.clear_dock_apps()
        network.register(app, mock_state)
        client = TestClient(app)

        resp = client.get("/dogefeedx/network/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["mesh_peers"] == []

    def test_no_mesh_returns_empty_lists(self, mock_state, tmp_path):
        """No mesh should return empty peer lists."""
        mock_state.mesh = None

        app = FastAPI()
        network.clear_dock_apps()
        network.register(app, mock_state)
        client = TestClient(app)

        resp = client.get("/dogefeedx/network/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["mesh_peers"] == []
        assert data["trusted_nodes"] == []

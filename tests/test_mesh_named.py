"""The mesh on an arcade's own domain (2026-10-19: "Since we have a domain, we can have a named tunnel through
our domain, but if somebody doesn't have a domain connected, it should just use a temporary Cloudflare tunnel")."""
import asyncio
import pathlib
import sys
import time

import nacl.signing

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from arcade.mesh import service as S                           # noqa: E402
from test_claim_browser import _free_port                       # noqa: E402
from test_realtime_rejoin_browser import Server                 # noqa: E402
from test_talk_relay import _mesh, _until                       # noqa: E402
from test_web import app_state                                  # noqa: E402,F401


def test_a_node_dials_another_through_its_web_apps_mesh_socket(app_state, monkeypatch):
    """B links to A by A's web app (wss://<A's public name>/mesh in life), which hands the link to A's mesh."""
    from arcade.web.app import create_app
    monkeypatch.setattr(S, "TUNNEL_FIRST", 0.1)
    named = []
    a = S.MeshService("arcade-test", nacl.signing.SigningKey.generate(), listen_host="127.0.0.1", listen_port=0,
                      verify=lambda *x: True, named_url=lambda: "wss://app.example.com/mesh",
                      announce=lambda host, port: named.append((host, port))).start()
    b = _mesh()
    app_state.mesh = a
    port = _free_port()
    srv = Server(create_app(app_state), port)
    try:
        assert a.node.ws_port, "a named address keeps the mesh's WebSocket port, with no cloudflared at all"
        b.node.add_address(f"ws://127.0.0.1:{port}/mesh", 443, a.node_id)
        assert _until(lambda: a.node_id in b.node.links, 20), "B linked to A through A's web app"
        assert _until(lambda: a.tunnel_url == "wss://app.example.com/mesh", 5)
        assert _until(lambda: named == [("wss://app.example.com/mesh", 443)], 5), "announced once, the fixed name"
        assert a._tunnel_proc is None, "no quick tunnel beside it"
    finally:
        srv.stop(); a.stop(); b.stop()


def test_the_name_is_the_arcades_public_host_and_none_without_one():
    class St:
        pages_hostname = "pages.example.com"
        public_hosts = ("pages.example.com", "app.example.com", "node.example.com")
    assert S.own_name(St) == "wss://app.example.com/mesh", "the first public name that is not the pages' own"
    St.public_hosts = ()
    assert S.own_name(St) is None, "no domain: the quick tunnel, as before"


def test_a_new_arcade_meets_its_first_peer_by_address_alone(app_state, monkeypatch):
    """2026-11-02: a fresh install connects to the arcade it came from "before their arcade is published via the
    mesh ID", so its Pepecoin testnet syncs from the others at once. It is handed one address (installer --first-peer,
    web/__main__.py mesh_peers) and nothing else: no node id, no announcement on chain, nothing synced, not listening."""
    from arcade.web.app import create_app
    monkeypatch.setattr(S, "TUNNEL_FIRST", 0.1)
    a = S.MeshService("arcade-test", nacl.signing.SigningKey.generate(), listen_host="127.0.0.1", listen_port=0,
                      verify=lambda *x: True, named_url=lambda: "wss://app.example.com/mesh",
                      announce=lambda host, port: None).start()
    app_state.mesh = a
    port = _free_port()
    srv = Server(create_app(app_state), port)
    b = S.MeshService("arcade-test", nacl.signing.SigningKey.generate(), listen_port=None,
                      peers=[(f"ws://127.0.0.1:{port}/mesh", 443)], verify=lambda *x: True).start()
    try:
        assert _until(lambda: a.node_id in b.node.links, 25), "the newcomer linked by the address alone"
        entry = b.node.addrs[(f"ws://127.0.0.1:{port}/mesh", 443)]
        assert entry["id"] == a.node_id and entry.get("pinned"), "and learned who it is from the link"
    finally:
        srv.stop(); a.stop(); b.stop()


def test_a_first_peer_is_read_as_an_arcades_address(tmp_path, capsys):
    from arcade.web.__main__ import mesh_peers, peer_address
    assert peer_address("https://app.dogecoinarcade.com") == ("wss://app.dogecoinarcade.com/mesh", 443)
    assert peer_address("wss://pepecoinarcade.com/mesh") == ("wss://pepecoinarcade.com/mesh", 443)
    assert peer_address("192.168.1.20:8421") == ("192.168.1.20", 8421)
    assert peer_address("[2001:db8::1]:8421") == ("2001:db8::1", 8421)
    (tmp_path / "first-peer").write_text("wss://app.dogecoinarcade.com/mesh\n")
    assert mesh_peers(["192.168.1.20:8421", "http://plain.example"], tmp_path) == [
        ("192.168.1.20", 8421), ("wss://app.dogecoinarcade.com/mesh", 443)]
    assert "skipping peer 'http://plain.example'" in capsys.readouterr().out
    assert mesh_peers(["https://app.dogecoinarcade.com"], tmp_path) == [("wss://app.dogecoinarcade.com/mesh", 443)], \
        "the same arcade twice is dialed once"
    assert mesh_peers([], tmp_path / "nowhere") == []

"""The LoRa gateway run by the arcade (web/lora.py, 2026-10-24): off by default; on, one thread holds the radio
and the Gateway; a radio that goes away is opened again on the same Gateway; Admin -> Meshtastic's routes; the T-Deck
firmware served only when it matches the release's sha256; and the LoRa badge carried over the mesh."""
import hashlib
import json
import pathlib
import sys
import threading
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

pytest.importorskip("meshtastic", reason="the LoRa gateway needs the [lora] extra")
pytest.importorskip("coincurve", reason="the LoRa gateway needs the [lora] extra")

from arcade.lora import link as linklib                          # noqa: E402
from arcade.web import lora as L, loraweb as LW                  # noqa: E402

LOCAL = {"host": "127.0.0.1:8420"}
WRITE = {**LOCAL, "X-Arcade-Admin": "1"}


class FakeIface:
    def __init__(self):
        self.myInfo = type("I", (), {"my_node_num": 0x433C8154})()
        self.nodes = {"!a": {"lastHeard": int(time.time())}, "!b": {"lastHeard": 1}}
        self.nodesByNum = {0x433C8154: {"user": {"longName": "PBR gateway", "shortName": "PBR", "hwModel": "HELTEC_V3"}}}


class FakeLink:
    made: list = []

    def __init__(self, *, port=None, ble=None, host=None):
        self.port, self.iface, self.my_node = port, FakeIface(), 0x433C8154
        self.stats = {"sent": 3, "acked": 2, "failed": 1}
        self.heard, self.frames, self.closed, self.sent = [], [], False, []
        FakeLink.made.append(self)

    def on_heard(self, h):
        self.heard.append(h)

    def on_frame(self, h):
        self.frames.append(h)

    def send(self, node, frame):
        self.sent.append((node, frame))
        return True

    def broadcast(self, frame):
        self.sent.append(("all", frame))

    def close(self):
        self.closed = True


@pytest.fixture
def radio(tmp_path, monkeypatch):
    port = tmp_path / "ttyUSB0"
    port.write_text("")
    FakeLink.made = []
    monkeypatch.setattr(linklib, "MeshLink", FakeLink)
    monkeypatch.setattr(L, "RETRY_EVERY", 0.2)
    monkeypatch.setattr(L, "PORT_PREFIXES", (*L.PORT_PREFIXES, str(tmp_path)))
    monkeypatch.setattr(L, "OLD_STATE", tmp_path / "none.sqlite")
    return port


def _until(cond, seconds=10):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_off_by_default_and_a_port_is_needed(app_state):
    svc = L.LoraService(app_state)
    assert svc.config()["enabled"] is False and svc.config()["beacon"] is False
    svc.apply()
    assert not svc.running and svc.thread is None
    with pytest.raises(ValueError):
        svc.save({"enabled": True})
    with pytest.raises(ValueError):
        svc.save({"port": "/etc/passwd"})


def test_on_it_runs_one_gateway_and_a_lost_radio_is_opened_again_on_the_same_gateway(app_state, radio):
    from pubsub import pub
    app_state.port = 8420
    svc = L.LoraService(app_state)
    svc.save({"enabled": True, "port": str(radio), "name": "PBR gateway", "chains": ["regtest", "nonsense"]})
    assert _until(lambda: svc.running), svc.error
    gw = svc.gw
    assert gw.name == "PBR gateway" and gw.networks == ["regtest"] and gw.arcade_url == "http://127.0.0.1:8420"
    first = FakeLink.made[-1]
    assert first.port == str(radio) and first.heard == [gw.heard] and first.frames == [gw.receive]
    s = svc.status()
    assert s["running"] and s["radio"]["node"] == "!433c8154" and s["radio"]["long_name"] == "PBR gateway"
    assert s["radio"]["nodes"] == 2 and s["radio"]["nodes_hour"] == 1 and s["link"] == {"sent": 3, "acked": 2, "failed": 1}
    assert s["devices"] == [] and s["outbox"] == 0 and s["receipts"] == 0
    # The library says the connection was lost: the link is rebuilt, the Gateway kept.
    pub.sendMessage("meshtastic.connection.lost", interface=first.iface)
    assert _until(lambda: len(FakeLink.made) >= 2 and svc.link is FakeLink.made[-1])
    assert first.closed and svc.gw is gw
    assert gw.send_frame(1, b"x") is True and FakeLink.made[-1].sent == [(1, b"x")], "frames go out on the new link"
    # The port goes away: waited for, then opened again.
    radio.unlink()
    assert _until(lambda: svc.link is None)
    assert "no radio" in svc.error or "went away" in svc.error
    radio.write_text("")
    assert _until(lambda: svc.running and svc.gw is gw)
    svc.save({"enabled": False})
    assert not svc.running and FakeLink.made[-1].closed and svc.thread is None


def test_missing_radio_support_is_said_not_crashed(app_state, radio, monkeypatch):
    monkeypatch.setattr(L, "missing_modules", lambda: ["meshtastic"])
    svc = L.LoraService(app_state)
    svc.save({"enabled": True, "port": str(radio)})
    assert _until(lambda: "Install" in svc.error)
    assert not svc.running


def test_the_hand_run_gateways_state_is_taken_over_once(app_state, tmp_path, monkeypatch):
    old = tmp_path / "old.sqlite"
    old.write_bytes(b"sessions")
    monkeypatch.setattr(L, "OLD_STATE", old)
    svc = L.LoraService(app_state)
    path = pathlib.Path(svc.state_path())
    assert path.read_bytes() == b"sessions" and oct(path.stat().st_mode & 0o777) == "0o600"
    old.write_bytes(b"newer")
    assert pathlib.Path(svc.state_path()).read_bytes() == b"sessions", "never copied over a file of its own"


def test_the_firmware_is_served_only_when_it_matches_the_releases_sha256(app_state):
    good = b"\xe9" + b"firmware" * 100
    info = {"version": "arcade-0.1.0", "factory": {"file": "arcade-tdeck-plus-factory.bin", "offset": "0x0",
                                                   "sha256": hashlib.sha256(good).hexdigest(), "size": len(good)}}
    asked = []

    def fetch(url, body={"firmware.json": json.dumps(info).encode(), "arcade-tdeck-plus-factory.bin": good}):
        asked.append(url)
        return body[url.rsplit("/", 1)[1]]
    assert LW.firmware_info(app_state, fetch)["version"] == "arcade-0.1.0"
    assert LW.firmware_file(app_state, "factory", fetch) == good
    n = len(asked)
    assert LW.firmware_file(app_state, "factory", fetch) == good and len(asked) == n, "kept, not fetched again"
    with pytest.raises(ValueError):
        LW.firmware_file(app_state, "app", fetch)
    # A tampered download is refused.
    for f in (pathlib.Path(app_state.home) / "lora-firmware").glob("*.bin"):
        f.unlink()
    with pytest.raises(ValueError, match="sha256"):
        LW.firmware_file(app_state, "factory", lambda url: json.dumps(info).encode() if url.endswith(".json")
                         else b"evil")


def test_admin_routes_need_the_admin_and_save_the_settings(client, radio, monkeypatch):
    app, state = client
    state.port = 8420
    got = app.get("/admin/api/lora", headers=LOCAL).json()
    assert got["config"]["enabled"] is False and got["local"] is True
    assert {c["net"] for c in got["chains"]} >= {"regtest"}
    assert app.post("/admin/api/lora", json={"enabled": True}, headers=LOCAL).status_code == 403, "writes need the header"
    bad = app.post("/admin/api/lora", json={"enabled": True}, headers=WRITE)
    assert bad.status_code == 400 and "serial port" in bad.json()["detail"]
    ok = app.post("/admin/api/lora", json={"enabled": True, "port": str(radio), "name": "Desk"}, headers=WRITE)
    assert ok.status_code == 200 and ok.json()["config"]["name"] == "Desk"
    assert _until(lambda: app.get("/admin/api/lora", headers=LOCAL).json()["status"]["running"])
    outside = app.get("/admin/api/lora", headers={"host": "app.example.com", "cf-ray": "x"})
    assert outside.status_code in (401, 403, 404), "never from outside without the operator"
    assert app.post("/admin/api/lora", json={"enabled": False}, headers=WRITE).status_code == 200
    page = app.get("/admin", headers=LOCAL).text
    assert 'data-tab="meshtastic"' in page and "Flash a T-Deck" in page and "/vendor/esptool.js" in page
    # The flasher (2026-10-24): only Espressif's own USB is offered, so the gateway's Heltec (behind a USB bridge)
    # can never be picked; the port is asked for first, inside the click, before the 3 MB fetch; no byte tracing;
    # and the steps say to switch the T-Deck on (switch down) while holding the trackball.
    flasher = page[page.index("async function flash("):page.index('$("lora-flash-full").onclick')]
    assert "requestPort({filters: [{usbVendorId: 0x303a}]})" in flasher and "requestPort({})" not in flasher
    assert flasher.index("requestPort(") < flasher.index("await fetch(") < flasher.index('import("/vendor/esptool.js")')
    assert flasher.split("await navigator.serial.requestPort(")[0].count("await ") == 0, \
        "nothing is awaited before the picker opens"
    assert "new Transport(port, false)" in flasher and "new Transport(port, true)" not in flasher
    # When the T-Deck can't be reached, a help box says why for the visitor's own system (the operator: "How will the average
    # user be able to figure this out?"): snap Firefox can't reach USB at all, snap Chromium needs raw-usb, a listed
    # port that won't open on Linux needs dialout.
    assert 'id="lora-flash-help"' in page
    for words in ("sudo snap connect chromium:raw-usb", "sudo usermod -aG dialout $USER",
                  "can't reach USB devices at all", "Device Manager", 'flashHelp("noport")', 'flashHelp("noopen")',
                  'flashHelp("nowebserial")'):
        assert words in page, words
    steps = page[page.index("The first flash"):page.index("lora-flash-full")]
    assert steps.index("Hold the trackball down") < steps.index("switch it on (switch down)") < steps.index("wait 2 seconds")


def test_the_lora_badge_travels_in_the_operators_frame():
    import asyncio
    from arcade.mesh.operators import OperatorsShare
    share = OperatorsShare(lambda: {})
    link = type("Link", (), {"peer_id": "p1"})()
    asyncio.run(share.on_frame(link, {"op": "me", "tag": "bigchiefenergy", "lora": True}, b""))
    asyncio.run(share.on_frame(type("Link", (), {"peer_id": "p2"})(), {"op": "me", "lora": "yes"}, b""))
    assert share.peers["p1"]["lora"] is True and share.peers["p2"]["lora"] is False


def test_the_operator_picks_tokens_the_t_decks_see(client, monkeypatch):
    """2026-10-26: "a search bar that auto complete just like in the public token exchange that allows the
    operator to include any token for the T deck to see". Picks are kept as the exchange names a leg; anything else,
    a chain this arcade does not run, or a repeat is dropped; the search finds issued tokens and DRC-20 / PRC-20 ticks."""
    app, state = client
    net = state.messaging.network
    ok = app.post("/admin/api/lora", headers=WRITE, json={"assets": [
        {"key": f"{net}-token-5", "label": "Arcade Coin"}, {"key": f"{net}-token-5", "label": "again"},
        {"key": "nochain-token-1", "label": "x"}, {"key": f"{net}-token20-dogi", "label": "DOGI"},
        {"key": f"{net}-nft-abc", "label": "not a token"}, "junk"]})
    assert ok.status_code == 200
    assert ok.json()["config"]["assets"] == [{"key": f"{net}-token-5", "label": "Arcade Coin"},
                                             {"key": f"{net}-token20-dogi", "label": "DOGI"}]
    assert app.get("/admin/api/lora", headers=LOCAL).json()["config"]["assets"][0]["key"] == f"{net}-token-5"

    class Index:
        def properties(self):
            return [{"property_id": 5, "name": "Arcade Coin"}, {"property_id": 6, "name": "Other"}]
    monkeypatch.setattr(type(state), "token_index", lambda self, chain: Index())
    got = app.get("/admin/api/lora/assets?q=arcade", headers=LOCAL).json()["results"]
    assert [r["key"] for r in got] == [f"{c.network}-token-5" for c in state.token_chains]
    assert app.get("/admin/api/lora/assets?q=", headers=LOCAL).json()["results"] == []
    outside = app.get("/admin/api/lora/assets?q=a", headers={"host": "app.example.com", "cf-ray": "x"})
    assert outside.status_code in (401, 403, 404)
    page = app.get("/admin", headers=LOCAL).text
    assert 'id="lora-asset-pick"' in page and "window.arcadeSearchBox($(\"lora-asset-pick\")" in page

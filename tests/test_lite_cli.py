"""ARCADE Lite in a terminal: every command belongs to a module, the operator installs and removes modules as
packages, `<command> -help` explains any command, and arcade-cli sends lines to the arcade's console."""
import pathlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "installer"))
from test_web import app_state  # noqa: F401,E402
import install  # noqa: E402

from arcade.lite import cli as clilib, commands as cmdlib  # noqa: E402
from arcade.web.app import create_app  # noqa: E402

LITE_LINE = {"x-arcade-lite": "1", "x-arcade-admin": "1"}
OUTSIDE = {"host": "arcade.example.com", "cf-connecting-ip": "198.51.100.9"}


@pytest.fixture
def lite(app_state):
    app_state.set_setting("edition", "lite")
    return create_app(app_state)


def test_modules_install_and_remove_from_the_console_like_packages(lite, app_state):
    client = TestClient(lite)

    def line(text, headers=LITE_LINE):
        return client.post("/lite/api/console", headers=headers, json={"line": text}).json()
    assert line("install feed") == {"ok": True, "result": "on: feed; pages follow now; background work follows at "
                                                          "the next restart"}
    assert client.get("/feed").status_code == 200
    assert line("remove feed")["ok"] and client.get("/feed").status_code == 404
    said = line("remove exchange")                                          # a module with a warning asks first
    assert not said["ok"] and "remove exchange yes" in said["error"]
    assert line("install wallet")["error"] == "Wallet is part of the core and is always on"
    assert line("edition")["result"] == "lite"
    assert not line("edition full extra words")["ok"]
    assert line("edition full")["ok"] and client.get("/exchange").status_code == 200
    assert not line("edition lite")["ok"] and line("edition lite yes")["ok"]
    assert next(m for m in line("listmodules")["result"] if m["id"] == "ords")["on"] is False
    visitor = {"x-arcade-lite": "1", **OUTSIDE}
    app_state.set_setting("public_hosts", ["arcade.example.com"])
    assert "no command 'install'" in line("install feed", headers=visitor)["error"]
    app_state.set_setting("public_hosts", [])


def test_help_flags_and_commands_of_modules_that_are_off(app_state):
    op = cmdlib.Caller(app_state, "operator")
    assert cmdlib.run_line(op, "install -help").startswith("install module\n")
    assert cmdlib.run_line(op, "-help") == cmdlib.run_line(op, "help")
    cmdlib.COMMANDS["_feedread"] = cmdlib.Command("_feedread", "feed", "anyone", lambda caller: "read", "feed")
    try:
        assert cmdlib.run_line(op, "_feedread") == "read"
        app_state.set_setting("edition", "lite")
        with pytest.raises(cmdlib.CommandError, match="Feed module is off on this arcade; `install feed`"):
            cmdlib.run_line(op, "_feedread")
        assert "_feedread" not in cmdlib.run_line(op, "help")
        with pytest.raises(cmdlib.CommandError, match="module is off on this arcade$"):
            cmdlib.run_line(cmdlib.Caller(app_state, "anyone"), "_feedread")
    finally:
        del cmdlib.COMMANDS["_feedread"]


def test_arcade_cli_sends_each_line_to_the_console(monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(clilib, "send", lambda url, line: sent.append((url, line)) or (True, "fine"))
    assert clilib.main(["install", "feed"]) == 0 and sent[-1] == (clilib.DEFAULT_URL, "install feed")
    assert clilib.main(["-url", "http://127.0.0.1:9000", "remove", "exchange", "yes"]) == 0
    assert sent[-1] == ("http://127.0.0.1:9000", "remove exchange yes")
    assert clilib.main(["-help"]) == 0 and sent[-1][1] == "-help" and "arcade-cli -help" in capsys.readouterr().out
    monkeypatch.setattr(clilib, "send", lambda url, line: (False, "no command 'x'"))
    assert clilib.main(["x"]) == 1 and "no command" in capsys.readouterr().err
    said = iter(["help", "exit"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(said))
    assert clilib.main([]) == 0 and "no command" in capsys.readouterr().out


def test_the_installer_puts_arcade_cli_on_the_path(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "info", lambda *a, **k: None)
    venv = tmp_path / "venv"
    install.write_launcher(venv, tmp_path / "bin", "Linux")
    assert f'exec "{venv}/bin/arcade-cli" "$@"' in (tmp_path / "bin" / "arcade-cli").read_text()
    install.write_launcher(venv, tmp_path / "win", "Windows")
    assert "arcade-cli.exe" in (tmp_path / "win" / "arcade-cli.cmd").read_text()

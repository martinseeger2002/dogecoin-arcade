"""The window that greets a node's operator when the machine starts.

2026-09-25: "when the machine that is operating the node starts up,
there should be a window that pops up with the link to the admin panel."

`arcade-admin-window` waits until this machine's node answers, then shows a
small window with the admin panel's address and a button that opens it. On the
node machine the panel needs no password, so the link is the local one.

`arcade-admin-window --install` puts it in the desktop's autostart (Linux
freedesktop autostart, the Windows Startup folder, or a macOS LaunchAgent), so
it appears at every login. `--uninstall` takes it out again.

It uses what the desktop already has -- zenity on Linux, a message box on
Windows, osascript on macOS -- and falls back to opening the browser, so it adds
no dependency.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

DEFAULT_PORT = 8420
TITLE = "DogecoinArcade node"


def admin_url(port: int = DEFAULT_PORT) -> str:
    return f"http://127.0.0.1:{port}/admin"


def wait_for_node(port: int, seconds: float = 600, pause: float = 3.0) -> bool:
    """Until the node answers, or `seconds` pass."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/source.rev", timeout=5):
                return True
        except Exception:                                  # noqa: BLE001 -- not up yet
            time.sleep(pause)
    return False


def message(url: str, running: bool) -> str:
    if running:
        return (f"Your arcade node is running.\n\nAdmin panel (no password on this machine):\n"
                f"{url}\n\nSeats, limits, the node's coins and every switch are there.")
    return (f"The arcade node has not answered yet.\n\nWhen it does, the admin panel is at:\n{url}")


def show(url: str, running: bool) -> None:
    """The window. Returns when it is closed; opens the panel if asked to."""
    text = message(url, running)
    system = platform.system()
    try:
        if system == "Linux" and shutil.which("zenity") and os.environ.get("DISPLAY",
                                                                          os.environ.get("WAYLAND_DISPLAY")):
            answer = subprocess.run(
                ["zenity", "--question", "--title", TITLE, "--width", "420",
                 "--icon-name", "applications-games", "--text", text,
                 "--ok-label", "Open admin panel", "--cancel-label", "Close"])
            if answer.returncode == 0:
                webbrowser.open(url)
            return
        if system == "Windows":
            import ctypes
            MB_OKCANCEL, IDOK = 0x1, 1
            got = ctypes.windll.user32.MessageBoxW(
                None, text + "\n\nOK opens the admin panel.", TITLE, MB_OKCANCEL | 0x40)
            if got == IDOK:
                webbrowser.open(url)
            return
        if system == "Darwin":
            script = (f'display dialog {json_quote(text)} with title {json_quote(TITLE)} '
                      f'buttons {{"Close", "Open admin panel"}} default button 2')
            answer = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
            if "Open admin panel" in answer.stdout:
                webbrowser.open(url)
            return
    except Exception:                                      # noqa: BLE001 -- no desktop to draw on
        pass
    print(text)
    if running:
        webbrowser.open(url)


def json_quote(text: str) -> str:
    import json
    return json.dumps(text)


# --- autostart -----------------------------------------------------------------------

def _command(port: int) -> list[str]:
    here = shutil.which("arcade-admin-window")
    return ([here] if here else [sys.executable, "-m", "arcade.adminwindow"]) + ["--port", str(port)]


def autostart_path() -> Path:
    system = platform.system()
    if system == "Windows":
        return (Path(os.environ.get("APPDATA", Path.home())) / "Microsoft" / "Windows" /
                "Start Menu" / "Programs" / "Startup" / "DogecoinArcade admin.cmd")
    if system == "Darwin":
        return Path.home() / "Library" / "LaunchAgents" / "com.dogecoinarcade.adminwindow.plist"
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "autostart" / "dogecoinarcade-admin.desktop"


def install(port: int = DEFAULT_PORT) -> Path:
    path = autostart_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    command = _command(port)
    system = platform.system()
    if system == "Windows":
        path.write_text("@echo off\r\nstart \"\" " + " ".join(f'"{c}"' for c in command) + "\r\n")
    elif system == "Darwin":
        args = "".join(f"<string>{c}</string>" for c in command)
        path.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>'
            '<key>Label</key><string>com.dogecoinarcade.adminwindow</string>'
            f'<key>ProgramArguments</key><array>{args}</array>'
            '<key>RunAtLoad</key><true/></dict></plist>\n')
    else:
        path.write_text(
            "[Desktop Entry]\nType=Application\nName=DogecoinArcade admin\n"
            "Comment=Shows the link to this node's admin panel at login\n"
            f"Exec={' '.join(command)}\nX-GNOME-Autostart-enabled=true\nTerminal=false\n")
    return path


def uninstall() -> bool:
    path = autostart_path()
    if path.exists():
        path.unlink()
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--install", action="store_true", help="show this window at every login")
    parser.add_argument("--uninstall", action="store_true", help="stop showing it at login")
    parser.add_argument("--wait", type=float, default=600, help="seconds to wait for the node")
    args = parser.parse_args(argv)
    if args.install:
        print(f"installed: {install(args.port)}")
        return 0
    if args.uninstall:
        print("removed" if uninstall() else "was not installed")
        return 0
    url = admin_url(args.port)
    show(url, wait_for_node(args.port, args.wait))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""arcade-cli: the arcade's commands in a terminal, the way a Core's own cli talks to its node.

    arcade-cli -help                 every command this arcade has, by category
    arcade-cli getnodeinfo
    arcade-cli install feed          switch a module on, as a package is installed
    arcade-cli remove feed
    arcade-cli listchains -help      what one command does and takes
    arcade-cli new theme <name>      a theme's folder to start from, here (arcade/packages.py)
    arcade-cli                       an interactive console (type exit to leave)

It runs the same command list as the Lite window's console (arcade/lite/commands.py), as the operator at the node's
own machine. A command whose module is off says so; `install <module>` switches it on. Each argument is read as JSON
when it parses as JSON and as a string otherwise.
"""
from __future__ import annotations

import json
import shlex
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8420"


def send(url: str, line: str) -> tuple[bool, str]:
    """(ok, text) for one console line."""
    request = urllib.request.Request(
        url.rstrip("/") + "/lite/api/console", data=json.dumps({"line": line}).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-Arcade-Lite": "1", "X-Arcade-Admin": "1"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            said = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            said = json.loads(exc.read() or b"{}")
        except ValueError:
            said = {}
        return False, str(said.get("detail") or f"the arcade answered {exc.code}")
    except (urllib.error.URLError, OSError) as exc:
        return False, f"no arcade answered at {url}: {getattr(exc, 'reason', exc)}"
    if not said.get("ok"):
        return False, str(said.get("error") or "refused")
    result = said.get("result")
    return True, result if isinstance(result, str) else json.dumps(result, indent=2)


def new_theme(args: list[str]) -> int:
    """`new theme <name> [folder]`: written here, on this machine, not by the arcade: it is the builder's own folder."""
    from pathlib import Path
    from .. import packages
    if not 1 <= len(args) <= 2:
        print("usage: arcade-cli new theme <name> [folder]", file=sys.stderr)
        return 1
    try:
        made = packages.template(Path(args[1] if len(args) > 1 else "."), args[0])
    except (packages.PackageError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"made {made}: change its manifest.json, then `arcade-cli trytheme {made}` (its README says the rest)")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    url = DEFAULT_URL
    if args[:1] in (["-url"], ["--url"]) and len(args) > 1:
        url, args = args[1], args[2:]
    if args[:1] in (["-help"], ["--help"], ["-h"]) and len(args) == 1:
        print(__doc__.strip() + "\n")
    if args[:2] == ["new", "theme"]:
        return new_theme(args[2:])
    if args:
        ok, text = send(url, shlex.join(args))
        print(text, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    print(f"arcade-cli at {url}: type help, or exit to leave")
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if line in ("exit", "quit"):
            return 0
        if line:
            print(send(url, line)[1])


if __name__ == "__main__":
    raise SystemExit(main())

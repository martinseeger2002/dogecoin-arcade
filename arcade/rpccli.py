"""arcade-rpc: call the bot RPC from a shell, the way bitcoin-cli calls a node.

    arcade-rpc omni_listproperties
    arcade-rpc omni_getallbalancesforid 3
    arcade-rpc -main omni_getinfo

Testnet unless `-main` is given: a bare command that reached mainnet would be
the wrong default for anything that spends. Each argument is read as JSON when
it parses as JSON and as a string otherwise, as bitcoin-cli does, so `3` is a
number and an address is a string. The cookie is read from the running
server's home (`~/.dogecoinarcade/rpc.cookie`).
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _parse(arg: str):
    try:
        return json.loads(arg)
    except ValueError:
        return arg


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arcade-rpc", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-main", action="store_true", help="talk to mainnet (default: testnet)")
    parser.add_argument("-test", action="store_true", help="talk to testnet (the default)")
    parser.add_argument("--home", default=str(Path.home() / ".dogecoinarcade"),
                        help="the server's home, where rpc.cookie is")
    parser.add_argument("--port", type=int, default=8420)
    parser.add_argument("method")
    parser.add_argument("params", nargs="*")
    args = parser.parse_args(argv)
    if args.main and args.test:
        parser.error("-main and -test are exclusive")

    cookie = Path(args.home).expanduser() / "rpc.cookie"
    try:
        user, secret = cookie.read_text().strip().split(":", 1)
    except (OSError, ValueError):
        print(f"arcade-rpc: cannot read {cookie}; is arcade-web running?", file=sys.stderr)
        return 1

    url = f"http://127.0.0.1:{args.port}/rpc/{'main' if args.main else 'test'}"
    body = json.dumps({"jsonrpc": "1.0", "id": "arcade-rpc", "method": args.method,
                       "params": [_parse(p) for p in args.params]}).encode()
    auth = base64.b64encode(f"{user}:{secret}".encode()).decode()
    request = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "Authorization": f"Basic {auth}"})
    try:
        with urllib.request.urlopen(request) as response:
            answer = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            answer = json.loads(exc.read())
        except ValueError:
            print(f"arcade-rpc: HTTP {exc.code} from {url}", file=sys.stderr)
            return 1
    except urllib.error.URLError as exc:
        print(f"arcade-rpc: {url} is not answering ({exc.reason})", file=sys.stderr)
        return 1

    if answer.get("error"):
        err = answer["error"]
        print(f"error code: {err.get('code')}\nerror message:\n{err.get('message')}",
              file=sys.stderr)
        return 1
    result = answer.get("result")
    if isinstance(result, str):
        print(result)
    else:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

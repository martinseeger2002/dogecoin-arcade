#!/usr/bin/env python3
"""Inscribe the showcase: the library first, then the page that uses it.

    python3 examples/showcase/inscribe-showcase.py            # what it will cost
    python3 examples/showcase/inscribe-showcase.py --send     # do it

Two inscriptions, because that is what recursion is: the library goes on the
chain once, and the page refers to it by number. The page cannot be inscribed
until the library has a number, so this does them in order and waits.

Testnet unless --main. Nothing is broadcast without --send.
"""

import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path.home() / ".dogecoinarcade"


def rpc_client(url: str, cookie: Path):
    user, secret = cookie.read_text().strip().split(":", 1)
    auth = base64.b64encode(f"{user}:{secret}".encode()).decode()

    def call(method, *params):
        body = json.dumps({"id": method, "method": method,
                           "params": list(params)}).encode()
        request = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/json", "Authorization": f"Basic {auth}"})
        try:
            with urllib.request.urlopen(request) as response:
                answer = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            answer = json.loads(exc.read())
        if answer.get("error"):
            raise RuntimeError(f"{method}: {answer['error']['message']}")
        return answer["result"]

    return call


def fetch(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=30) as response:
        return json.loads(response.read())


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--library", metavar="ID",
                        help="the number or txid the library was inscribed as; "
                             "writes showcase.built.html ready to inscribe")
    args = parser.parse_args()

    from arcade import inscribe

    library = (HERE / "arcade-lib.js").read_bytes()
    page_source = (HERE / "showcase.html").read_text()

    lib_cost = inscribe.estimate(library, "application/javascript")
    print(f"1. arcade-lib.js   {len(library):>7,} bytes  "
          f"{lib_cost.chunks:>3} tx  {lib_cost.total:>8.2f} "
          f"({lib_cost.net:.2f} net)")
    # The page grows a little when the placeholder becomes a real id.
    guess = page_source.replace("__LIBRARY__", "0" * 64).encode()
    page_cost = inscribe.estimate(guess, "text/html")
    print(f"2. showcase.html   {len(guess):>7,} bytes  "
          f"{page_cost.chunks:>3} tx  {page_cost.total:>8.2f} "
          f"({page_cost.net:.2f} net)")
    print(f"   {'':<17} {'':>7}  {lib_cost.chunks + page_cost.chunks:>3} tx  "
          f"{lib_cost.total + page_cost.total:>8.2f} total")

    if not args.library:
        print()
        print("Recursion is two inscriptions and they have to go in order: the")
        print("page refers to the library by id, so the library must exist first.")
        print()
        print("  1. Inscriptions page -> examples/showcase/arcade-lib.js")
        print('     JSON: {"name": "arcade-lib", "kind": "library", "version": "1.0.0"}')
        print("  2. Note the number it was given.")
        print("  3. Run this again with --library <that number>, which writes")
        print("     showcase.built.html with the id filled in.")
        print("  4. Inscriptions page -> showcase.built.html")
        print('     JSON: {"name": "API showcase", "uses": "arcade-lib"}')
        return 0

    built = page_source.replace("__LIBRARY__", args.library)
    out = HERE / "showcase.built.html"
    out.write_text(built)
    real = inscribe.estimate(built.encode(), "text/html")
    print()
    print(f"Wrote {out}")
    print(f"  it loads /content/{args.library}")
    print(f"  {len(built.encode()):,} bytes, {real.chunks} transaction"
          f"{'' if real.chunks == 1 else 's'}, {real.total:.2f} "
          f"({real.net:.2f} net)")
    print()
    print("Inscribe it from the Inscriptions page with:")
    print('  {"name": "API showcase", "uses": "arcade-lib"}')
    return 0


if __name__ == "__main__":
    sys.exit(main())

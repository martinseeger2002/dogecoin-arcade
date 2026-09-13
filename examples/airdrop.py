#!/usr/bin/env python3
"""Airdrop: send some of one token to every holder of another (or the same) token.

    python3 examples/airdrop.py --from nYourAddress --holders-of 3 --token 3 --amount 5
    python3 examples/airdrop.py ... --send

Testnet, and a dry run, unless told otherwise: without --send it only prints
what it would do. With --send it walks the holders one at a time, preparing
and broadcasting each send through the arcade's bot RPC (docs: bot-rpc.md),
and writes each txid to a done-file so a stopped run picks up where it left
off without paying anyone twice.

Nothing in here is specific to airdrops: `rpc()` is the whole client. Copy
it and build whatever comes next.
"""

import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path.home() / ".dogecoinarcade"


def rpc_client(url: str, cookie: Path):
    """A function that calls one method on the arcade's bot RPC."""
    user, secret = cookie.read_text().strip().split(":", 1)
    auth = base64.b64encode(f"{user}:{secret}".encode()).decode()

    def call(method, *params):
        body = json.dumps({"id": method, "method": method, "params": list(params)}).encode()
        request = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/json", "Authorization": f"Basic {auth}"})
        try:
            with urllib.request.urlopen(request) as response:
                answer = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            answer = json.loads(exc.read())
        if answer.get("error"):
            raise RuntimeError(f"{method}: {answer['error']['message']} "
                               f"(code {answer['error']['code']})")
        return answer["result"]

    return call


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="sender", required=True,
                        help="the address in this node's wallet that pays: holds the tokens and a few coins for fees")
    parser.add_argument("--holders-of", type=int, required=True, help="token whose holders receive")
    parser.add_argument("--token", type=int, required=True, help="token to send")
    parser.add_argument("--amount", required=True, help="amount each holder gets, e.g. 5 or 0.25")
    parser.add_argument("--send", action="store_true", help="actually broadcast (default: dry run)")
    parser.add_argument("--main", action="store_true", help="mainnet (default: testnet)")
    parser.add_argument("--port", type=int, default=8420)
    parser.add_argument("--home", default=str(HOME), help="where the arcade keeps rpc.cookie")
    args = parser.parse_args()

    chain = "main" if args.main else "test"
    rpc = rpc_client(f"http://127.0.0.1:{args.port}/rpc/{chain}",
                     Path(args.home).expanduser() / "rpc.cookie")

    info = rpc("omni_getinfo")
    if info["stopped"]:
        print(f"the index stopped at block {info['stopped']['block']}: {info['stopped']['reason']}")
        return 1
    if info["behind"]:
        print(f"the index is {info['behind']} blocks behind the node; holders may be stale")

    token = rpc("omni_getproperty", args.token)
    holders = [h["address"] for h in rpc("omni_getallbalancesforid", args.holders_of)
               if h["address"] != args.sender]
    done_file = Path(f"airdrop-{chain}-{args.holders_of}-to-{args.token}.done.json")
    done = json.loads(done_file.read_text()) if done_file.exists() else {}
    todo = [h for h in holders if h not in done]

    print(f"{token['name']} (#{args.token}) on {chain}net: {args.amount} each to "
          f"{len(todo)} holder(s) of #{args.holders_of}"
          + (f", {len(done)} already done" if done else ""))
    have = rpc("omni_getbalance", args.sender, args.token)["balance"]
    print(f"{args.sender} holds {have}")
    if not args.send:
        for address in todo:
            print(f"  would send {args.amount} to {address}")
        print("dry run; add --send to do it")
        return 0

    for n, address in enumerate(todo, 1):
        while True:
            prepared = rpc("omni_send", args.sender, address, args.token, args.amount)
            try:
                txid = rpc("omni_broadcast", prepared["txid"])
            except RuntimeError as exc:
                # A node lets one address chain 25 unconfirmed transactions
                # (validation.h: DEFAULT_ANCESTOR_LIMIT); after that, wait for
                # a block and go on. The prepared transaction is discarded --
                # the next prepare picks inputs afresh.
                if "too-long-mempool-chain" not in str(exc):
                    raise
                block = rpc("omni_getinfo")["nodeblock"]
                print(f"  mempool chain is full; waiting for block {block + 1}...")
                while rpc("omni_getinfo")["nodeblock"] == block:
                    time.sleep(10)
                continue
            break
        done[address] = txid
        done_file.write_text(json.dumps(done, indent=2))
        print(f"  [{n}/{len(todo)}] {address}  fee {prepared['fee']}  {txid}")
    print(f"done: {len(done)} sends recorded in {done_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

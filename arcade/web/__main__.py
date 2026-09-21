"""Launch the DogecoinArcade web interface.

    python -m arcade.web --datadir ~/.pepecoin-testnet

Binds 127.0.0.1 by design. The app can trigger signing through the node's
wallet, so exposing it on a network would expose spending authority. Reach it
remotely with an SSH port-forward instead.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import uvicorn

from ..config import MESSAGING_NETWORKS
from .app import create_app
from .state import AppState, ChainContext
from .watcher import BlockWatcher

DEFAULT_HOME = Path.home() / ".dogecoinarcade"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arcade-web", description=__doc__)
    parser.add_argument("--home", default=str(DEFAULT_HOME))
    # Two chains, permanently (D-012): testnet carries messages, mainnet carries
    # tokens, NFTs and PEP.
    parser.add_argument("--msg-network", default="test", choices=sorted(MESSAGING_NETWORKS),
                        help="chain for the Messenger (testnet only)")
    parser.add_argument("--msg-datadir", help="testnet datadir, to read its .cookie")
    parser.add_argument("--msg-conf", help="testnet config with rpcuser/rpcpassword")
    parser.add_argument("--msg-marker", help="Class B marker address on testnet")
    # Tokens are also indexed on the messaging chain, and the Tokens page can
    # switch to it (D-016); the ledger itself is mainnet, always.
    parser.add_argument("--ledger-network", default="main",
                        choices=["main", "doge-main"],
                        help="chain for tokens, NFTs and PEP")
    parser.add_argument("--ledger-datadir", help="mainnet datadir, to read its .cookie")
    parser.add_argument("--ledger-conf", help="mainnet config with rpcuser/rpcpassword")
    parser.add_argument("--port", type=int, default=8420)
    parser.add_argument(
        "--public", action="store_true",
        help="serve this as a public arcade: the feed, the collections and "
             "the chain are open to anyone and every form is refused "
             "(arcade/web/door.py). Without it this is a wallet, and "
             "everything in it belongs to whoever can reach the port.")
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="deliberately defaults to loopback; changing it exposes spending authority",
    )
    parser.add_argument(
        "--log-level", default=os.environ.get("ARCADE_LOG_LEVEL", "info"),
        choices=["debug", "info", "warning", "error"],
        help="how much this writes to the journal; info by default")
    args = parser.parse_args(argv)

    # Nothing configured logging at all, so the root logger sat at its default
    # WARNING and every log.info in the package went nowhere -- two dozen
    # deliberate diagnostics, including the one that says a message store has
    # been rebuilt and the ones that say what the indexer did with a block.
    # Worse than losing them: "check the journal for X" then cannot tell a fix
    # that worked quietly from a fix that never ran, which is exactly the
    # instruction this session had been giving the other machine all day
    # (a test machine, D-110).
    #
    # No timestamp in the format: journald stamps every line, and a terminal
    # user is watching it happen.
    logging.basicConfig(level=getattr(logging, args.log_level.upper()),
                        format="%(levelname)s %(name)s: %(message)s")

    # Startup output is the only feedback before the browser opens; it is
    # worthless if it sits in a buffer until the process exits.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    home = Path(args.home).expanduser()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)

    state = AppState(
        home=home,
        messaging=ChainContext(
            network=args.msg_network,
            role="messaging",
            label="Testnet",
            datadir=Path(args.msg_datadir).expanduser() if args.msg_datadir else None,
            conf=Path(args.msg_conf).expanduser() if args.msg_conf else None,
            marker=args.msg_marker,
            can_spend=True,
        ),
        ledger=ChainContext(
            network=args.ledger_network,
            role="ledger",
            label="Mainnet",
            datadir=Path(args.ledger_datadir).expanduser() if args.ledger_datadir else None,
            conf=Path(args.ledger_conf).expanduser() if args.ledger_conf else None,
            # Determined at runtime from whether the node answers getwalletinfo,
            # rather than assumed here.
            can_spend=True,
        ),
    )

    # Bots authenticate to /rpc/main and /rpc/test with this file's contents.
    print(f"  RPC cookie {state.write_rpc_cookie()}")

    if args.host != "127.0.0.1":
        print(f"WARNING: binding {args.host}, not loopback. This app can spend.")

    # Report what was found, so a silent misconfiguration is visible at startup
    # rather than as an empty panel later.
    from ..discovery import best
    for context in (state.messaging, state.ledger):
        if context.datadir or context.conf:
            where = context.datadir or context.conf
            print(f"  {context.label:8s} {context.network:10s} {where}")
            continue
        found = best(context.network)
        if found:
            print(f"  {context.label:8s} {context.network:10s} {found.datadir}  "
                  f"(found via {found.source.split(' ')[0]}, height {found.height:,})")
        else:
            print(f"  {context.label:8s} {context.network:10s} NOT FOUND -- "
                  f"the {context.role} sections will explain why")

    # There is no era sweep any more (D-151). The floor is set once and
    # does not move again, so the only index that can be from another floor
    # is one built before that height -- and `IndexFromAnotherFloor` still
    # catches that, moves the index aside and rebuilds. What is gone is the
    # machinery that retired rows in every OTHER store, which existed for a
    # floor that moved repeatedly while the rules were being settled.

    # If the passphrase was saved, unlock at startup rather than making the user
    # do it on every launch. That is the entire point of saving it.
    if state.try_auto_unlock():
        print(f"  messaging ready as {state.derived_address}")

    # Read now, while it is still the version that was imported. Left until
    # first use it would report whatever is on disk by then, which after an
    # update is the code that is NOT running.
    state.running_version
    if state.is_stale:
        print(f"  WARNING: running {state.running_version} but "
              f"{state.installed_version} is installed -- restart to use it")

    # Watch for new blocks and scan when one lands, so a message that arrives
    # while somebody is looking at the conversation actually appears.
    BlockWatcher(state).start()

    # A collection that was being inscribed when the process last stopped
    # carries on from the piece after the last one written down.
    try:
        jobs, runner = state.collections
        for job_id in runner.resume_interrupted():
            print(f"  resuming collection {jobs.get(job_id)['name']!r} ({job_id})")
    except Exception as exc:
        print(f"  collections could not be resumed: {exc}")
    print("  watching for new blocks")

    state.port = args.port
    state.public = bool(args.public)
    if state.public:
        # Said at startup, every time. An operator who does not know which
        # of the two things they are running is an operator who will one
        # day put a wallet on a public address.
        print("  PUBLIC: only the feed, the collections and the chain are "
              "served; every form is refused")
    print(f"DogecoinArcade  ->  http://{args.host}:{args.port}")
    uvicorn.run(create_app(state), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

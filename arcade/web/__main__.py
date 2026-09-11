"""Launch the DogecoinArcade web interface.

    python -m arcade.web --datadir ~/.pepecoin-testnet

Binds 127.0.0.1 by design. The app can trigger signing through the node's
wallet, so exposing it on a network would expose spending authority. Reach it
remotely with an SSH port-forward instead.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

from ..config import MESSAGING_NETWORKS
from .app import create_app
from .state import AppState, ChainContext

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
    parser.add_argument("--ledger-network", default="main", choices=["main", "doge-main"],
                        help="chain for tokens, NFTs and PEP")
    parser.add_argument("--ledger-datadir", help="mainnet datadir, to read its .cookie")
    parser.add_argument("--ledger-conf", help="mainnet config with rpcuser/rpcpassword")
    parser.add_argument("--port", type=int, default=8420)
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="deliberately defaults to loopback; changing it exposes spending authority",
    )
    args = parser.parse_args(argv)

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

    # If the passphrase was saved, unlock at startup rather than making the user
    # do it on every launch. That is the entire point of saving it.
    if state.try_auto_unlock():
        print(f"  identity {state.identity.fingerprint} unlocked from saved credentials")

    print(f"DogecoinArcade  ->  http://{args.host}:{args.port}")
    uvicorn.run(create_app(state), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

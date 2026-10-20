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
import shutil
import sys
from pathlib import Path

import uvicorn

from ..config import MESSAGING_NETWORKS, NETWORKS
from . import brand as brandlib
from .app import create_app
from .state import AppState, ChainContext
from .watcher import BlockWatcher

DEFAULT_HOME = Path.home() / ".dogecoinarcade"

#: Seconds a request that is still running is given to finish before a restart
#: cancels it. The number is small on purpose; why it has to be there at all is
#: at the run, at the end of main().
GRACEFUL_STOP = 5.0


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
                        choices=["main"],
                        help="chain for tokens, NFTs and PEP")
    parser.add_argument("--ledger-datadir", help="mainnet datadir, to read its .cookie")
    parser.add_argument("--ledger-conf", help="mainnet config with rpcuser/rpcpassword")
    # The add-on chains (Dogecoin since 2026-10-13, Litecoin and Litecoin testnet since 2026-10-18, any
    # chain whose Params say `addon`): each found by itself at ~/<node_dir>/<node_conf>, which its
    # installer writes; these name another, or leave it out.
    for addon in ADDON_PARAMS:
        parser.add_argument(f"--{addon.name}-conf", help=f"{addon.label} config with rpcuser/rpcpassword")
        parser.add_argument(f"--{addon.name}-datadir", help=f"{addon.label} datadir, to read its .cookie")
        parser.add_argument(f"--no-{addon.name}", action="store_true", help=f"run without {addon.label}")
    parser.add_argument("--brand", choices=sorted(brandlib.BRANDS),
                        help="the face this node shows (name, colours, icons): "
                             "dogecoin (default) or pepecoin; ARCADE_BRAND works too")
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
    # The realtime mesh (arcade/mesh): this node linking to other arcade nodes
    # directly, for game rooms. On by default; it dials out, so it works behind
    # any router, and it listens too, so a node somebody can reach helps others.
    parser.add_argument(
        "--no-mesh", action="store_true",
        help="do not join the realtime mesh: games here play solo")
    parser.add_argument(
        "--mesh-port", type=int, default=8421,
        help="port the mesh listens on for other nodes (0: do not listen, only dial out)")
    parser.add_argument(
        "--mesh-peer", action="append", default=[], metavar="IP:PORT",
        help="another node to link to directly, e.g. one on this network "
             "(the chain cannot carry private addresses); may be repeated")
    parser.add_argument(
        "--mesh-announce", metavar="IP:PORT",
        help="announce THIS address on the chain instead of working it out. By "
             "default a listening node finds its public address from what its "
             "Pepecoin peers see, checks the address really leads back to it, and "
             "announces it (again when it changes, and daily); nothing is announced "
             "if no port is forwarded. Paid for by the fee address")
    parser.add_argument(
        "--mesh-inbound", type=int, default=None, metavar="N",
        help="how many other nodes may link to this one (32 by default; a server "
             "that everyone can reach should take hundreds, e.g. 500)")
    parser.add_argument(
        "--mesh-tunnel", choices=["auto", "off"], default="auto",
        help="when no address of this node can be proved, open a free Cloudflare quick "
             "tunnel (cloudflared, if installed) and announce it, so every node can dial "
             "this one; 'off' never does (auto by default)")
    parser.add_argument(
        "--mesh-core", choices=["on", "off"], default="on",
        help="let this node's Core nodes (Pepecoin, and Dogecoin and Litecoin when it runs them) peer with "
             "other arcades' Core nodes through the mesh, so two nodes behind routers can still sync with "
             "each other (at most 256 KB/s per link; on by default)")
    parser.add_argument(
        "--stun-port", type=int, default=3478, metavar="PORT",
        help="UDP port for this node's STUN server, which tells players' browsers their public address so "
             "they can connect directly for fast games (WebRTC); it is offered to browsers only once another "
             "node has reached it. 0: none (3478 by default)")
    parser.add_argument(
        "--mesh-public-port", type=int, default=None, metavar="PORT",
        help="the port the router forwards to --mesh-port, if it is a different number")
    parser.add_argument(
        "--log-level", default=os.environ.get("ARCADE_LOG_LEVEL", "info"),
        choices=["debug", "info", "warning", "error"],
        help="how much this writes to the journal; info by default")
    args = parser.parse_args(argv)
    if args.brand:   # before create_app, which reads it (brand.py)
        os.environ["ARCADE_BRAND"] = args.brand

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
    # Which Cores run here and which are borrowed from arcades that share theirs (arcade/cores.py).
    from .. import cores as coreslib
    chosen = coreslib.read_settings(home)
    ledger_borrowed = coreslib.mode(chosen, "main") == coreslib.BORROW

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
            borrowed=ledger_borrowed,
        ),
        **_addon_fields({p.name: _addon_context(args, p, coreslib.mode(chosen, p.name, ""))
                         for p in ADDON_PARAMS}),
    )

    # Bots authenticate to /rpc/main and /rpc/test with this file's contents.
    print(f"  RPC cookie {state.write_rpc_cookie()}")

    if args.host != "127.0.0.1":
        print(f"WARNING: binding {args.host}, not loopback. This app can spend.")

    # Report what was found, so a silent misconfiguration is visible at startup
    # rather than as an empty panel later.
    from ..discovery import best
    borrowed = [c for c in (state.ledger, *state.addons) if c.borrowed]
    for context in borrowed:
        print(f"  {context.label:8s} {context.network:10s} borrowed from arcades that share it, through the mesh")
    if borrowed and args.no_mesh:
        print("  WARNING: --no-mesh, so the borrowed chains above cannot be reached")
    for addon in state.addons:
        if not addon.borrowed:
            print(f"  {addon.label:8s} {addon.network:10s} "
                  f"{addon.conf or addon.datadir}  (shown grayed out until it has synced)")
    for context in (state.messaging, state.ledger):
        if context.borrowed:
            continue
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

    if not args.no_mesh:
        try:
            from ..mesh.service import for_state

            def host_port(text: str) -> tuple[str, int]:
                host, _, port = text.rpartition(":")
                return host.strip("[]"), int(port)
            state.mesh = for_state(
                state, listen_port=args.mesh_port or None,
                peers=[host_port(p) for p in args.mesh_peer],
                announce_at=host_port(args.mesh_announce) if args.mesh_announce else None,
                public_port=args.mesh_public_port, max_inbound=args.mesh_inbound,
                tunnel=(shutil.which("cloudflared") or next((p for p in (os.path.expanduser("~/.local/bin/cloudflared"),)
                                                             if os.path.exists(p)), None))
                if args.mesh_tunnel == "auto" and args.mesh_port else None,
                core=args.mesh_core == "on", web_port=args.port, stun_port=args.stun_port or None,
            ).start()
            print(f"  mesh node {state.mesh.node_id[:16]}"
                  + (f" listening on {args.mesh_port}" if args.mesh_port else " (dial-out only)"))
        except Exception as exc:                  # noqa: BLE001 -- games play solo instead
            print(f"  mesh not started: {exc}")

    state.port = args.port
    state.public = bool(args.public)
    if state.public:
        # Said at startup, every time. An operator who does not know which
        # of the two things they are running is an operator who will one
        # day put a wallet on a public address.
        print("  PUBLIC: only the feed, the collections and the chain are "
              "served; every form is refused")
    if not brandlib.FROM_ENV and state.setting("brand"):   # the admin panel's face (create_app applies it too)
        try:
            brandlib.use(str(state.setting("brand")))
        except ValueError:
            pass
    print(f"{brandlib.current().name}  ->  http://{args.host}:{args.port}")
    # Without a timeout here uvicorn waits for every request to finish before it
    # stops, and a game room's event stream is a request that never finishes --
    # so a restart while anyone was playing ran into the stop timeout and the
    # process was killed mid-request rather than shutting down. Whatever was
    # being written was written by a killed process, not by the careful
    # shutdown in the lifespan, which never got its turn.
    uvicorn.run(create_app(state), host=args.host, port=args.port, log_level="warning",
                timeout_graceful_shutdown=GRACEFUL_STOP)
    return 0



#: Every add-on chain, from the chains' own Params.
ADDON_PARAMS = tuple(p for p in NETWORKS.values() if p.addon)


def _addon_context(args, params, mode: str = "") -> "ChainContext | None":
    """An add-on chain (Dogecoin, Litecoin, Litecoin testnet, any chain whose Params say `addon`), as the
    operator chose in Admin -> Chains (arcade/cores.py): "borrow" reads it through an arcade that shares
    its Core over the mesh, "off" leaves it out, and "here" -- or no choice yet -- finds this machine's
    node: named on the command line, or ~/<node_dir>/<node_conf> (its folder too, for the cookie
    beside it: 2026-10-17, an installer's config with no rpcuser read "syncing 0%" forever), or that
    folder with a readable .cookie. None when there is none: the arcade runs without that chain."""
    net = params.name.replace("-", "_")
    if getattr(args, f"no_{net}", False) or mode == "off":
        return None
    if mode == "borrow":
        return ChainContext(network=params.name, role="ledger", label=params.label, can_spend=True,
                            borrowed=True)
    conf = getattr(args, f"{net}_conf", None)
    datadir = getattr(args, f"{net}_datadir", None)
    conf = Path(conf).expanduser() if conf else None
    datadir = Path(datadir).expanduser() if datadir else None
    if conf is None and datadir is None:
        if not params.node_dir:
            return None
        home = Path.home() / params.node_dir
        cookie = home / params.datadir_subdir / ".cookie" if params.datadir_subdir else home / ".cookie"
        if params.node_conf and (home / params.node_conf).exists():
            conf, datadir = home / params.node_conf, home
        elif cookie.exists():
            datadir = home
        elif mode == "":
            # No node of its own here and no choice made: borrowed from an arcade that shares it (2026-10-19:
            # "Using borrowed nodes should be on by default"). Admin -> Chains still turns it off or runs it here.
            return ChainContext(network=params.name, role="ledger", label=params.label, can_spend=True,
                                borrowed=True)
        else:
            return None
    return ChainContext(network=params.name, role="ledger", label=params.label,
                        datadir=datadir, conf=conf, can_spend=True)


def _addon_fields(contexts: dict) -> dict:
    """AppState's fields for the add-on chains: doge, ltc and ltc_test by name, the rest in `more`."""
    named = {"doge": "doge", "ltc": "ltc", "ltctest": "ltc_test"}
    out: dict = {"more": {}}
    for net, context in contexts.items():
        if net in named:
            out[named[net]] = context
        elif context is not None:
            out["more"][net] = context
    return out


if __name__ == "__main__":
    raise SystemExit(main())

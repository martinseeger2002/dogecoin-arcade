"""`arcade-msg` -- the encrypted messaging command line.

Testnet only (D-010). Every command that touches the network resolves its
network through `require_messaging_network()`, so pointing this at mainnet is an
error rather than a configuration choice.

The tool never holds private spending keys: transactions are funded and signed by
the node's wallet. It does hold the X25519 *messaging* key, which is encrypted at
rest with Argon2id and only decrypted in memory when a passphrase is supplied.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import os
import sys
from pathlib import Path

from ..config import (
    MESSAGING_NETWORKS,
    NETWORKS,
    MainnetRefused,
    Params,
    RpcCredentials,
    WrongChain,
    load_rpc_credentials,
    require_messaging_network,
    verify_connected_chain,
)
from ..rpc import RpcClient
from .envelope import build_key_announcement
from .keys import Identity, KeyError_, fingerprint_of, load_identity, save_identity
from .miner import COINBASE_MATURITY, DEFAULT_TARGET_COINS, Miner, MiningError
from .scanner import Scanner
from .sender import MessageSender, SendError, plan_message
from .store import MessageStore

DEFAULT_HOME = Path.home() / ".dogecoinarcade"


# --- plumbing -----------------------------------------------------------------


def _home(args) -> Path:
    home = Path(args.home).expanduser()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    return home


def _key_path(args) -> Path:
    return _home(args) / f"{args.network}.key"


def _store(args) -> MessageStore:
    return MessageStore(_home(args) / f"{args.network}.sqlite")


def _params(args) -> Params:
    params = NETWORKS[args.network]
    require_messaging_network(params)
    if args.marker:
        params = __import__("dataclasses").replace(params, marker_address=args.marker)
    return params


def _rpc(args, params: Params) -> RpcClient:
    datadir = Path(args.datadir).expanduser() if args.datadir else None
    creds = load_rpc_credentials(params, conf_path=Path(args.conf).expanduser()
                                 if args.conf else None, datadir=datadir)
    client = RpcClient(creds)
    # Confirm we reached the node we meant to. require_messaging_network checks
    # intent; this checks reality. Without it, a stray user config can silently
    # point every command at a different chain -- which it did.
    verify_connected_chain(client, params)
    return client


def _passphrase(confirm: bool = False) -> str:
    """Read a passphrase from the terminal, never from a command-line argument.

    Arguments end up in shell history and in `ps` output; a prompt does not.
    """
    env = os.environ.get("ARCADE_PASSPHRASE")
    if env:
        return env
    first = getpass.getpass("Passphrase: ")
    if confirm:
        if first != getpass.getpass("Confirm passphrase: "):
            raise SystemExit("passphrases do not match")
        if len(first) < 8:
            print("warning: a short passphrase gives weak protection to a long-lived key",
                  file=sys.stderr)
    return first


def _identity(args) -> Identity:
    return load_identity(_key_path(args), _passphrase())


def _when(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# --- commands -----------------------------------------------------------------


def cmd_keygen(args) -> int:
    path = _key_path(args)
    if path.exists():
        print(f"a key already exists at {path}", file=sys.stderr)
        print("refusing to overwrite it -- move it aside first if you really mean to",
              file=sys.stderr)
        return 1
    identity = Identity.generate()
    save_identity(path, identity, _passphrase(confirm=True))
    print(f"created {path} (mode 0600)")
    print(f"fingerprint  {identity.fingerprint}")
    print()

    # Funding cannot be done at send time: a coinbase needs 240 blocks to
    # mature, so waiting until coins are needed means waiting four hours with a
    # message already typed. Raise it here, at the one moment it is not urgent.
    if not args.no_fund:
        try:
            params = _params(args)
            with _rpc(args, params) as rpc:
                status = Miner(rpc, params).status()
            if not status.funded and not status.pending:
                print("This wallet has no coins yet, and coins take ~4 hours to mature")
                print("after mining -- so it is worth starting now rather than when you")
                print("first want to send something.")
                print()
                print("  arcade-msg fund")
                print()
            else:
                print(f"wallet: {status.describe()}")
                print()
        except Exception:
            pass          # funding advice is a convenience; never block keygen on it

    print("Publish it with:  arcade-msg publish-key")
    print("Read the fingerprint aloud to your correspondent to verify it out of band;")
    print("an on-chain announcement proves control of an address, not who someone is.")
    return 0


def cmd_fund(args) -> int:
    """One-shot bootstrap: mine a block so this wallet can pay message fees."""
    params = _params(args)
    with _rpc(args, params) as rpc:
        miner = Miner(rpc, params)
        status = miner.status()

        if status.funded:
            print(f"already funded: {status.describe()}")
            return 0

        if status.pending:
            print(status.describe())
            print()
            print("Mining more would not help: maturity is measured in chain height,")
            print("not in blocks you mined. Wait, and the chain will get there on its own.")
            return 0

        address = args.address or rpc.call("getnewaddress")
        print("This wallet has no coins, so it cannot pay transaction fees.")
        print()
        print(f"  One block pays 10,000 PEP. The largest possible message costs about")
        print(f"  0.147 PEP in fees, so a single block covers roughly 68,000 of them.")
        print(f"  This mines ONE block and stops -- it is a bootstrap, not a service.")
        print()
        print(f"  It will use one CPU core, typically for a few minutes.")
        print(f"  Coins then need {COINBASE_MATURITY} blocks (~4 hours) to become spendable.")
        print(f"  Rewards go to {address}")
        print()
        if not args.yes:
            if input("Start mining? [y/N] ").strip().lower() not in ("y", "yes"):
                return 1

        def progress(attempt, message):
            print(f"  [{attempt}] {message}", file=sys.stderr)

        try:
            status = miner.bootstrap(address, on_attempt=progress)
        except MiningError as exc:
            print(f"mining failed: {exc}", file=sys.stderr)
            return 1

        print()
        print(status.describe())
        if status.pending:
            print()
            print("Come back in about four hours, or run `arcade-msg fund` to check.")
    return 0


def cmd_export_key(args) -> int:
    path = _key_path(args)
    if not path.exists():
        print(f"no key at {path}", file=sys.stderr)
        return 1
    blob = path.read_bytes()
    dest = Path(args.out).expanduser() if args.out else None
    if dest:
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, blob)
        finally:
            os.close(fd)
        print(f"exported {len(blob)} encrypted bytes to {dest} (mode 0600)")
    else:
        sys.stdout.write(blob.hex() + "\n")
    print("still encrypted: useless without the passphrase.", file=sys.stderr)
    return 0


def cmd_import_key(args) -> int:
    path = _key_path(args)
    if path.exists():
        print(f"a key already exists at {path}; refusing to overwrite", file=sys.stderr)
        return 1
    source = Path(args.file).expanduser()
    blob = bytes.fromhex(source.read_text().strip()) if args.hex else source.read_bytes()
    identity = __import__("arcade.messaging.keys", fromlist=["decrypt_identity"]).decrypt_identity(
        blob, _passphrase()
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, blob)
    finally:
        os.close(fd)
    print(f"imported {path}")
    print(f"fingerprint  {identity.fingerprint}")
    return 0


def cmd_publish_key(args) -> int:
    params = _params(args)
    identity = _identity(args)
    payload = build_key_announcement(identity.public_bytes)

    with _rpc(args, params) as rpc:
        sender = MessageSender(rpc, params)
        address = args.address or rpc.call("getnewaddress")
        prepared = sender.prepare(address, payload, class_c=True)

        print(f"Key announcement for  {identity.fingerprint}")
        print(f"  from address  {address}")
        print(f"  payload       {len(payload)} bytes (Class C, OP_RETURN)")
        print(prepared.summary())
        if not _confirm(args, prepared):
            return 1
        txid = sender.broadcast(prepared)
        print(f"broadcast {txid}")
    return 0


def cmd_send(args) -> int:
    params = _params(args)
    identity = _identity(args)
    store = _store(args)

    recipient = _resolve_recipient(args, store)
    body = Path(args.file).expanduser().read_bytes() if args.file else args.message.encode()
    if not body:
        print("refusing to send an empty message", file=sys.stderr)
        return 1

    plan = plan_message(identity, recipient, body)
    print(f"Message         {len(body):,} bytes")
    print(f"  encrypted to  {fingerprint_of(recipient)}")
    print(f"  payload       {plan.payload_bytes:,} bytes"
          f"  ({plan.payload_bytes - len(body)} bytes overhead)")
    print(f"  carrier       Class B, {plan.multisig_outputs} multisig output(s)")
    print(f"  transactions  {plan.transactions}{'  (chained)' if plan.chunked else ''}")
    print(f"  estimated     {plan.est_total_coins:.8f} in fees and dust")
    print()

    if plan.chunked and not args.yes:
        print("This message needs more than one transaction. Chunks are chained through")
        print("change outputs, so they cannot be reordered -- but a partial send stays")
        print("on-chain permanently and unreadable.")
        print()

    with _rpc(args, params) as rpc:
        status = Miner(rpc, params).status()
        if not status.funded:
            print(f"cannot send: {status.describe()}", file=sys.stderr)
            if status.pending:
                print("Those coins are mined but still ripening; nothing to do but wait.",
                      file=sys.stderr)
            else:
                print("Run `arcade-msg fund` to mine a block. Note that coins then need",
                      file=sys.stderr)
                print(f"{COINBASE_MATURITY} blocks (~4 hours) to mature.", file=sys.stderr)
            return 1

        sender = MessageSender(rpc, params)
        address = args.address or rpc.call("getnewaddress")
        sent = []
        for index, payload in enumerate(plan.chunk_payloads, 1):
            prepared = sender.prepare(address, payload)
            if plan.transactions > 1:
                print(f"--- transaction {index} of {plan.transactions} ---")
            print(prepared.summary())
            if not _confirm(args, prepared):
                if sent:
                    print(f"\nSTOPPED after {len(sent)} of {plan.transactions}.", file=sys.stderr)
                    print("The message is incomplete on-chain and cannot be read.", file=sys.stderr)
                return 1
            sent.append(sender.broadcast(prepared))
            print(f"broadcast {sent[-1]}\n")
    print(f"sent in {len(sent)} transaction(s)")
    return 0


def _resolve_recipient(args, store: MessageStore) -> bytes:
    if args.pubkey:
        raw = bytes.fromhex(args.pubkey)
        if len(raw) != 32:
            raise SystemExit("--pubkey must be 32 bytes of hex")
        return raw
    row = store.key_for(args.to)
    if row is None:
        raise SystemExit(
            f"no announced key for {args.to}. Run `arcade-msg scan` first, or pass "
            "--pubkey if you have it out of band."
        )
    history = store.key_history(args.to)
    if len(history) > 1:
        print(f"note: {args.to} has announced {len(history)} keys; using the most recent "
              f"(height {row['height']}, {row['fingerprint']})", file=sys.stderr)
    return bytes(row["pubkey"])


def _confirm(args, prepared) -> bool:
    if args.yes:
        return True
    if args.dry_run:
        print("dry run: not broadcasting")
        return False
    answer = input("Broadcast this transaction? [y/N] ").strip().lower()
    return answer in ("y", "yes")


def cmd_scan(args) -> int:
    params = _params(args)
    store = _store(args)
    identity = None
    if _key_path(args).exists() and not args.no_decrypt:
        identity = _identity(args)

    with _rpc(args, params) as rpc:
        scanner = Scanner(rpc, params, store, identity)
        def progress(height, end):
            print(f"  ...{height:,} / {end:,}", end="\r", file=sys.stderr)
        total = None
        while True:
            result = scanner.scan(max_blocks=args.batch, progress=progress)
            print(" " * 40, end="\r", file=sys.stderr)
            if result.blocks == 0:
                break
            print(f"scanned {result}")
            if total is None:
                total = result
            if not args.follow:
                break
    stats = store.stats()
    print(f"store: {stats['messages']} message(s), {stats['unread']} unread, "
          f"{stats['keys']} key(s) known, {stats['unopened']} candidate(s) untried")
    return 0


def cmd_inbox(args) -> int:
    store = _store(args)
    fp = None
    if _key_path(args).exists() and not args.all:
        fp = _identity(args).fingerprint
    messages = store.inbox(recipient_fp=fp, limit=args.limit, unread_only=args.unread)
    if not messages:
        print("no messages")
        return 0
    print(f"{'id':>5}  {'when':16}  {'from':36}  {'size':>7}  status")
    for m in messages:
        status = "unread" if m.read_at is None else "read"
        if not m.complete:
            status = "INCOMPLETE"
        print(f"{m.id:>5}  {_when(m.block_time):16}  {m.sender_addr:36}  "
              f"{len(m.body):>7}  {status}")
    return 0


def cmd_read(args) -> int:
    store = _store(args)
    message = store.get_message(args.id)
    if message is None:
        print(f"no message with id {args.id}", file=sys.stderr)
        return 1
    print(f"from        {message.sender_addr}")
    print(f"sender key  {fingerprint_of(message.sender_pubkey)}")
    print(f"when        {_when(message.block_time)}  (block {message.height:,})")
    print(f"txid        {message.first_txid}")
    print("-" * 60)
    try:
        sys.stdout.write(message.body.decode("utf-8"))
    except UnicodeDecodeError:
        sys.stdout.write(f"<{len(message.body)} bytes of binary content>")
    print()
    store.mark_read(message.id)
    return 0


def cmd_keys(args) -> int:
    store = _store(args)
    rows = store.all_keys()
    if not rows:
        print("no key announcements seen; run `arcade-msg scan`")
        return 0
    print(f"{'address':36}  {'fingerprint':22}  height")
    for r in rows:
        print(f"{r['address']:36}  {r['fingerprint']:22}  {r['height']:,}")
    return 0


# --- argument parsing ---------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="arcade-msg",
        description="Encrypted messaging over Dogecoin-family testnets.",
        epilog="Testnet only. Mainnet is refused in code, not merely discouraged.",
    )
    p.add_argument("--network", default="test", choices=sorted(MESSAGING_NETWORKS),
                   help="which testnet (default: test)")
    p.add_argument("--home", default=str(DEFAULT_HOME), help="key and database directory")
    p.add_argument("--conf", help="path to a node config holding rpcuser/rpcpassword")
    p.add_argument("--datadir", help="node datadir, to read its .cookie")
    p.add_argument("--marker", help="Class B marker address for this network")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("keygen", help="create a messaging identity")
    s.add_argument("--no-fund", action="store_true",
                   help="skip the funding check and advice")
    s.set_defaults(func=cmd_keygen)

    s = sub.add_parser("fund", help="mine one block so this wallet can pay fees")
    s.add_argument("--address", help="mine rewards to this address")
    s.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    s.set_defaults(func=cmd_fund)

    s = sub.add_parser("export-key", help="export the encrypted key file")
    s.add_argument("--out", help="write to this path instead of stdout")
    s.set_defaults(func=cmd_export_key)

    s = sub.add_parser("import-key", help="import an encrypted key file")
    s.add_argument("file")
    s.add_argument("--hex", action="store_true", help="the file holds hex, not raw bytes")
    s.set_defaults(func=cmd_import_key)

    s = sub.add_parser("publish-key", help="announce your public key on chain")
    s.add_argument("--address", help="send from this address")
    s.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_publish_key)

    s = sub.add_parser("send", help="send an encrypted message")
    s.add_argument("--to", help="recipient address with an announced key")
    s.add_argument("--pubkey", help="recipient X25519 public key as hex")
    s.add_argument("--message", help="message text")
    s.add_argument("--file", help="read the message body from a file")
    s.add_argument("--address", help="send from this address")
    s.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_send)

    s = sub.add_parser("scan", help="scan the chain for messages and key announcements")
    s.add_argument("--batch", type=int, default=2000, help="blocks per pass")
    s.add_argument("--follow", action="store_true", help="keep scanning to the tip")
    s.add_argument("--no-decrypt", action="store_true", help="collect only; do not try to open")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("inbox", help="list decrypted messages")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--unread", action="store_true")
    s.add_argument("--all", action="store_true", help="every identity, not just this key")
    s.set_defaults(func=cmd_inbox)

    s = sub.add_parser("read", help="read one message")
    s.add_argument("id", type=int)
    s.set_defaults(func=cmd_read)

    s = sub.add_parser("keys", help="list announced keys seen on chain")
    s.set_defaults(func=cmd_keys)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except MainnetRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except WrongChain as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except (KeyError_, SendError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

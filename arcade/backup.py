"""Backing up and restoring a node wallet.

The premise
-----------
The user should need exactly one thing: their wallet. Not a passphrase, not a
key file, not a fingerprint written on a note. `wallet.dat` holds the coins, and
since the messaging identity is derived from a wallet address
(:mod:`arcade.messaging.derive`), it holds the identity too. Restore the wallet
and everything comes back.

That only holds if backing up and restoring are things a person can actually do,
so they live here and are offered in the interface rather than documented in a
file nobody reads.

Four operations
---------------
``backup``      `backupwallet` -- the node writes a consistent copy while running.
                Never copy a live wallet.dat by hand; BDB does not promise the
                file on disk is coherent mid-write.

``private_keys`` `dumpprivkey` per address, for printing. This is the most
                dangerous thing the application can produce, so it is never
                written to disk here, never logged, and returned only to the
                caller that asked for it.

``import_key``  `importprivkey` with a rescan. The rescan is why this is slow and
                why it runs on a thread: this node family has no
                `rescanblockchain`, so the import call itself does the scanning
                and blocks the whole RPC interface until it finishes.

``restore``     Replacing wallet.dat wholesale. Requires the node to be stopped,
                which is done through the node's own `stop` RPC rather than
                systemd, so it needs no privileges. The unit is configured
                `Restart=always`, so systemd brings the node back by itself.

What is refused
---------------
Restoring over a wallet with a balance, without the existing wallet being backed
up first. Losing someone's coins to a restore is worse than the inconvenience of
refusing one.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .rpc import RpcClient


class BackupError(Exception):
    """Something went wrong that the user has to know about."""


# --- backing up ---------------------------------------------------------------

def default_backup_dir() -> Path:
    return Path.home() / "DogecoinArcade-Backups"


def node_backups_dir(datadir: Path, network: str) -> Path:
    """Where this node family puts `backupwallet` output.

    Not where you asked it to. These nodes are Dogecoin derivatives, and their
    `backupwallet` discards any directory in the argument and writes the basename
    into `<datadir>/<network>/backups/` -- verified against the running node, not
    inferred. The useful consequence is that the node needs no write access
    outside its own datadir, so a mainnet node running as its own user still
    works.
    """
    return wallet_path(datadir, network).parent / "backups"


def backup_wallet(rpc: RpcClient, destination: Path, *, datadir: Path,
                  network: str) -> Path:
    """Write a consistent copy of the wallet to `destination`.

    Two steps, because of the path behaviour above: the node writes into its own
    backups directory, then this copies the result where the user actually asked
    for it. Copying a live wallet.dat directly is never done -- BDB makes no
    promise that the file on disk is coherent between writes.
    """
    destination = Path(destination).expanduser()
    if destination.is_dir() or not destination.suffix:
        destination.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d-%H%M%S")
        destination = destination / f"wallet-{stamp}.dat"
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        rpc.call("backupwallet", destination.name)
    except Exception as exc:
        raise BackupError(f"the node could not write a backup: {exc}") from exc

    written = node_backups_dir(datadir, network) / destination.name
    if not written.exists():
        raise BackupError(
            f"the node reported success but no backup appeared in {written.parent}. "
            f"Check that the node's data directory is where this application "
            f"thinks it is."
        )
    if written == destination:
        return destination
    try:
        shutil.copy2(written, destination)
    except PermissionError as exc:
        raise BackupError(
            f"the backup was written to {written}, but this application cannot "
            f"read it -- the node runs as a different user. The file is there and "
            f"is complete; copy it from that folder yourself."
        ) from exc
    return destination


def wallet_summary(rpc: RpcClient) -> dict[str, Any]:
    """Enough to tell the user what is in the wallet they are about to replace."""
    info: dict[str, Any] = {}
    try:
        info = dict(rpc.call("getwalletinfo") or {})
    except Exception:
        pass
    try:
        info["addresses"] = len(known_addresses(rpc))
    except Exception:
        info["addresses"] = 0
    return info


def known_addresses(rpc: RpcClient) -> list[str]:
    """Every address the wallet knows, deduplicated, in a stable order.

    `listaddressgroupings` only reports addresses that have been used, so the
    accounts list is consulted too -- otherwise a freshly restored wallet would
    look empty.
    """
    found: list[str] = []
    seen: set[str] = set()

    def add(addr: Any) -> None:
        if isinstance(addr, str) and addr and addr not in seen:
            seen.add(addr)
            found.append(addr)

    try:
        for group in rpc.call("listaddressgroupings") or []:
            for entry in group:
                if entry:
                    add(entry[0])
    except Exception:
        pass
    try:
        for account in (rpc.call("listaccounts") or {}):
            for addr in rpc.call("getaddressesbyaccount", account) or []:
                add(addr)
    except Exception:
        pass
    return found


@dataclass
class PrintableKey:
    address: str
    private_key: str
    balance: str = ""


def private_keys(rpc: RpcClient) -> list[PrintableKey]:
    """Every private key in the wallet, for printing onto paper.

    Returned, never stored. The caller renders them once and they exist nowhere
    else -- no file, no log, no database.
    """
    keys: list[PrintableKey] = []
    for address in known_addresses(rpc):
        try:
            wif = rpc.call("dumpprivkey", address)
        except Exception as exc:
            message = str(exc).lower()
            if "encrypted" in message or "locked" in message:
                raise BackupError(
                    "this wallet is encrypted, so the node will not reveal its "
                    "keys until it is unlocked. Unlock it and try again."
                ) from exc
            continue     # watch-only addresses have no key here; skip them
        try:
            received = rpc.call("getreceivedbyaddress", address)
        except Exception:
            received = None
        keys.append(PrintableKey(address, wif, "" if received is None else str(received)))
    if not keys:
        raise BackupError(
            "this wallet has no private keys to show. If the node was started "
            "with disablewallet, there is no wallet to print."
        )
    return keys


# --- importing ----------------------------------------------------------------

@dataclass
class ImportJob:
    """A running `importprivkey`, which can take many minutes.

    The rescan blocks the node's whole RPC interface, so this has to happen off
    the request thread or the interface would appear to hang.
    """

    label: str
    started: float = field(default_factory=time.time)
    done: bool = False
    error: str | None = None
    address: str | None = None

    @property
    def elapsed(self) -> int:
        return int(time.time() - self.started)


def import_private_key(rpc_factory, wif: str, label: str = "imported",
                       rescan: bool = True) -> ImportJob:
    """Import one key, rescanning the chain for its history, on a thread.

    `rpc_factory` rather than a client, because the thread outlives the request
    that started it and needs a connection of its own.
    """
    wif = wif.strip()
    if not wif:
        raise BackupError("paste a private key first.")
    if any(c.isspace() for c in wif):
        raise BackupError(
            "that looks like more than one key, or a phrase. Import one key at a "
            "time -- it starts with a single letter and has no spaces.")

    job = ImportJob(label=label)

    def run() -> None:
        try:
            with rpc_factory() as rpc:
                rpc.call("importprivkey", wif, label, rescan)
                # Report back which address arrived, so the user sees a result
                # rather than a bare "done".
                for address in known_addresses(rpc):
                    try:
                        if rpc.call("dumpprivkey", address) == wif:
                            job.address = address
                            break
                    except Exception:
                        continue
        except Exception as exc:
            message = str(exc)
            if "already" in message.lower():
                job.error = "that key is already in this wallet."
            elif "invalid" in message.lower() or "checksum" in message.lower():
                job.error = ("that is not a valid private key for this chain. "
                             "Check it was copied in full, and that it belongs to "
                             "this network rather than the other one.")
            else:
                job.error = message
        finally:
            job.done = True

    threading.Thread(target=run, name="arcade-importprivkey", daemon=True).start()
    return job


# --- restoring a whole wallet -------------------------------------------------

def wallet_path(datadir: Path, network: str) -> Path:
    """Where wallet.dat lives for a given network."""
    subdir = {"test": "testnet3", "doge-test": "testnet3",
              "regtest": "regtest", "doge-regtest": "regtest"}.get(network, "")
    return Path(datadir) / subdir / "wallet.dat" if subdir else Path(datadir) / "wallet.dat"


def restore_wallet(rpc_factory, source: Path, datadir: Path, network: str,
                   *, keep_backup_in: Path | None = None,
                   timeout: float = 120.0) -> dict[str, Any]:
    """Replace the node's wallet.dat with `source`, safely.

    The existing wallet is always copied aside first. A restore that silently
    destroyed the wallet already in place would be a far worse failure than any
    it prevents, and "I had nothing in that one" is not something to take on
    trust.
    """
    source = Path(source).expanduser()
    if not source.is_file():
        raise BackupError(f"there is no file at {source}.")
    if source.stat().st_size == 0:
        raise BackupError(f"{source.name} is empty.")
    # BDB wallets start with a page header; a text dump or a stray document here
    # would be caught by the node only after it had been put in place.
    with source.open("rb") as handle:
        head = handle.read(16)
    if b"Wallet" in head or head[:4] in (b"# We", b"# Wa"):
        raise BackupError(
            f"{source.name} looks like a text key dump, not a wallet.dat. Those "
            f"are imported differently -- use the private key import instead.")

    target = wallet_path(datadir, network)
    if not target.parent.is_dir():
        raise BackupError(f"{target.parent} does not exist -- is this the right node?")
    if not os.access(target.parent, os.W_OK):
        raise BackupError(
            f"{target.parent} is not writable by this application. That usually "
            f"means the node runs as a different user, and the restore has to be "
            f"done as that user instead.")

    # Save the wallet being replaced, before anything is stopped.
    saved: Path | None = None
    if target.exists():
        backups = Path(keep_backup_in or default_backup_dir()).expanduser()
        backups.mkdir(parents=True, exist_ok=True)
        saved = backups / f"wallet-replaced-{time.strftime('%Y-%m-%d-%H%M%S')}.dat"
        try:
            with rpc_factory() as rpc:
                backup_wallet(rpc, saved, datadir=datadir, network=network)
        except Exception:
            # The node may already be down. A plain copy is second best but far
            # better than replacing the file with no copy at all.
            shutil.copy2(target, saved)

    stopped = _stop_node(rpc_factory, timeout=timeout)

    # os.replace is atomic within a filesystem: the node never sees a half file.
    staged = target.with_name(f"wallet.dat.incoming-{os.getpid()}")
    try:
        shutil.copy2(source, staged)
        os.replace(staged, target)
    except Exception as exc:
        staged.unlink(missing_ok=True)
        raise BackupError(f"could not put the wallet in place: {exc}") from exc
    finally:
        staged.unlink(missing_ok=True)

    return {"restored_from": str(source), "wallet": str(target),
            "previous_saved_to": str(saved) if saved else None,
            "node_was_stopped": stopped}


def _stop_node(rpc_factory, timeout: float) -> bool:
    """Stop the node through its own RPC and wait for it to let go of the wallet.

    Deliberately not systemctl: that needs privileges this application does not
    have and should not want. The unit is `Restart=always`, so systemd treats
    this as an unexpected exit and starts the node again by itself.
    """
    try:
        with rpc_factory() as rpc:
            rpc.call("stop")
    except Exception:
        return False        # already down, which is what we wanted anyway

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.5)
        try:
            with rpc_factory() as rpc:
                rpc.call("getblockcount")
        except Exception:
            return True     # no longer answering: it has gone
    raise BackupError(
        f"the node did not shut down within {int(timeout)} seconds. Nothing has "
        f"been changed. It is probably mid-flush; try again in a minute.")


def wait_for_node(rpc_factory, timeout: float = 180.0) -> bool:
    """Wait for the node to come back after a restore. Best effort."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with rpc_factory() as rpc:
                rpc.call("getblockcount")
            return True
        except Exception:
            time.sleep(1.0)
    return False

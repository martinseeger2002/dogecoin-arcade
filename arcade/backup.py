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

import json
import os
import re
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


# --- several wallets ----------------------------------------------------------
#
# These nodes predate multiwallet RPC: no createwallet, no loadwallet, no
# listwallets. One wallet file per process, chosen at startup with `-wallet=`.
# So "several wallets" means several files with one of them in place, and
# switching means stopping the node, swapping the file, and letting systemd start
# it again -- the same machinery a restore uses.
#
# The active file keeps the default name `wallet.dat` rather than being selected
# with `-wallet=`. That is deliberate: it means switching never has to edit a
# service definition, so it works on installations whose units this application
# did not write and cannot change.

#: Where the inactive wallets live, inside the node's own data directory so they
#: share its permissions and are covered by whatever backs that up.
LIBRARY_DIR = "arcade-wallets"

#: Name of the wallet an installation starts with, before anyone makes a second.
DEFAULT_WALLET = "main"

_NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,40}$")


@dataclass
class WalletEntry:
    name: str
    active: bool
    size: int = 0
    modified: int = 0
    path: Path | None = None


def wallet_library(datadir: Path, network: str) -> Path:
    return wallet_path(datadir, network).parent / LIBRARY_DIR


def _manifest_path(datadir: Path, network: str) -> Path:
    return wallet_library(datadir, network) / "active.json"


def active_wallet_name(datadir: Path, network: str) -> str:
    """Which wallet is in place. Recorded, because the file itself cannot say."""
    try:
        data = json.loads(_manifest_path(datadir, network).read_text())
        name = data.get("active")
        return name if isinstance(name, str) and name else DEFAULT_WALLET
    except Exception:
        return DEFAULT_WALLET


def _set_active(datadir: Path, network: str, name: str) -> None:
    library = wallet_library(datadir, network)
    library.mkdir(parents=True, exist_ok=True)
    _manifest_path(datadir, network).write_text(json.dumps({"active": name}, indent=2))


def check_wallet_name(name: str, datadir: Path, network: str,
                      *, must_be_new: bool = True) -> str:
    name = (name or "").strip()
    if not _NAME_OK.match(name):
        raise BackupError(
            "a wallet name may use letters, numbers, spaces, hyphens and "
            "underscores, must start with a letter or number, and can be at most "
            "41 characters.")
    if must_be_new:
        taken = {entry.name.lower() for entry in list_wallets(datadir, network)}
        if name.lower() in taken:
            raise BackupError(f"there is already a wallet called {name!r}.")
    return name


def list_wallets(datadir: Path, network: str) -> list[WalletEntry]:
    """Every wallet this installation knows about, active one first.

    The active wallet is listed from the manifest even when no file for it sits
    in the library, because while it is in use it lives at wallet.dat instead.
    """
    active = active_wallet_name(datadir, network)
    live = wallet_path(datadir, network)
    entries: list[WalletEntry] = []

    stat = live.stat() if live.is_file() else None
    entries.append(WalletEntry(
        name=active, active=True,
        size=stat.st_size if stat else 0,
        modified=int(stat.st_mtime) if stat else 0,
        path=live if stat else None,
    ))

    library = wallet_library(datadir, network)
    if library.is_dir():
        for candidate in sorted(library.glob("*.dat")):
            name = candidate.stem
            if name.lower() == active.lower():
                continue        # the stale copy of the one currently in use
            info = candidate.stat()
            entries.append(WalletEntry(name=name, active=False, size=info.st_size,
                                       modified=int(info.st_mtime), path=candidate))
    return entries


def _park_active_wallet(datadir: Path, network: str) -> Path | None:
    """Move the wallet currently in place into the library under its own name."""
    live = wallet_path(datadir, network)
    if not live.is_file():
        return None
    library = wallet_library(datadir, network)
    library.mkdir(parents=True, exist_ok=True)
    parked = library / f"{active_wallet_name(datadir, network)}.dat"
    shutil.copy2(live, parked)
    return parked


def create_wallet(rpc_factory, datadir: Path, network: str, name: str) -> dict[str, Any]:
    """Start using a brand new, empty wallet, keeping the current one.

    The node makes the new file itself on startup, which is the only way to get a
    wallet these nodes will accept: there is no createwallet RPC to ask for one.
    """
    name = check_wallet_name(name, datadir, network)
    live = wallet_path(datadir, network)
    if not live.parent.is_dir():
        raise BackupError(f"{live.parent} does not exist -- is this the right node?")
    if not os.access(live.parent, os.W_OK):
        raise BackupError(
            f"{live.parent} is not writable by this application, so wallets "
            f"cannot be switched here. That node runs as a different user.")

    _stop_node(rpc_factory, timeout=120.0)
    parked = _park_active_wallet(datadir, network)
    if live.exists():
        live.unlink()                      # the node creates a fresh one on start
    _set_active(datadir, network, name)
    return {"created": name, "previous_saved_to": str(parked) if parked else None}


def switch_wallet(rpc_factory, datadir: Path, network: str, name: str) -> dict[str, Any]:
    """Put a different wallet in place. The current one is kept, never discarded."""
    library = wallet_library(datadir, network)
    source = library / f"{name}.dat"
    if not source.is_file():
        raise BackupError(f"there is no wallet called {name!r} here.")
    if name.lower() == active_wallet_name(datadir, network).lower():
        raise BackupError(f"{name} is already the wallet in use.")

    live = wallet_path(datadir, network)
    if not os.access(live.parent, os.W_OK):
        raise BackupError(
            f"{live.parent} is not writable by this application, so wallets "
            f"cannot be switched here. That node runs as a different user.")

    _stop_node(rpc_factory, timeout=120.0)
    parked = _park_active_wallet(datadir, network)

    staged = live.with_name(f"wallet.dat.incoming-{os.getpid()}")
    try:
        shutil.copy2(source, staged)
        os.replace(staged, live)           # atomic: the node never sees half a file
    except Exception as exc:
        staged.unlink(missing_ok=True)
        raise BackupError(f"could not put {name} in place: {exc}") from exc
    finally:
        staged.unlink(missing_ok=True)

    _set_active(datadir, network, name)
    return {"now_using": name, "previous_saved_to": str(parked) if parked else None}


def remove_wallet(datadir: Path, network: str, name: str) -> dict[str, Any]:
    """Take a wallet out of the list. Moved aside, never deleted.

    A wallet file may hold coins that nothing else records, and there is no way
    to check without loading it into a node. Unlinking it on a user's say-so
    would make "remove" and "lose everything in it" the same gesture, so this
    moves the file to a dated folder and says where it went. Deleting for real
    stays a decision made with a file manager, deliberately.
    """
    if name.lower() == active_wallet_name(datadir, network).lower():
        raise BackupError(
            f"{name} is the wallet in use. Switch to another one first, then "
            f"remove this.")
    source = wallet_library(datadir, network) / f"{name}.dat"
    if not source.is_file():
        raise BackupError(f"there is no wallet called {name!r} here.")

    removed_dir = wallet_library(datadir, network) / "removed"
    removed_dir.mkdir(parents=True, exist_ok=True)
    destination = removed_dir / f"{name}-{time.strftime('%Y-%m-%d-%H%M%S')}.dat"
    shutil.move(str(source), destination)
    return {"removed": name, "moved_to": str(destination)}

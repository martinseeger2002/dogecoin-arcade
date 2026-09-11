"""Storing passphrases locally, inside the application's own directory.

What this is honestly worth
---------------------------
The application must be able to read a stored passphrase **without asking the
user anything**. That means the key protecting it has to be derivable from the
machine, and anything the application can derive, so can anyone else running as
this user. Calling that "encrypted" would be dishonest: against a local attacker
it is equivalent to plaintext.

What it does buy is narrower but real. The stored blob is bound to this machine
and this user, so a **copied credentials file is useless anywhere else** -- which
is precisely the case the key file's Argon2id encryption exists for: a leaked
backup, a synced folder, a stolen disk image. An attacker who takes the whole
home directory to another machine gets nothing from this file.

So, stated plainly and repeated in the interface:

  * protects against: the credentials file being copied off this machine;
  * does NOT protect against: anyone who can run code as this user here.

Storage layout
--------------
One file per identity under ~/.dogecoinarcade/credentials/, mode 0600, named by
network. Multiple identities therefore coexist without interfering.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import platform
from dataclasses import dataclass
from pathlib import Path

import nacl.secret
import nacl.utils
from nacl.exceptions import CryptoError

log = logging.getLogger(__name__)

MAGIC = b"ARCC"
VERSION = 1


@dataclass(frozen=True)
class VaultStatus:
    available: bool
    location: str
    reason: str = ""


def credentials_dir(home: Path) -> Path:
    return Path(home) / "credentials"


def _machine_secret() -> bytes:
    """Something stable on this machine that another machine will not have.

    Not a secret in any strong sense -- /etc/machine-id is world-readable -- but
    it differs per installation, which is what binds the file to this machine.
    The user id is mixed in so two accounts on one machine cannot read each
    other's stored passphrases by simply copying the file across.
    """
    parts: list[bytes] = []
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            parts.append(Path(path).read_bytes().strip())
            break
        except OSError:
            continue
    if not parts:
        # Windows and macOS: fall back to identifiers that are stable per install.
        parts.append(platform.node().encode())
        parts.append(str(os.environ.get("COMPUTERNAME", "")).encode())
    try:
        parts.append(str(os.getuid()).encode())
    except AttributeError:
        parts.append(str(os.environ.get("USERNAME", "")).encode())
    parts.append(str(Path.home()).encode())
    return hashlib.sha256(b"|".join(parts)).digest()


def _key_for(network: str) -> bytes:
    """A 32-byte key bound to this machine, this user, and this identity."""
    return hmac.new(_machine_secret(), f"arcade-credentials:{network}".encode(),
                    hashlib.sha256).digest()


def status(home: Path) -> VaultStatus:
    directory = credentials_dir(home)
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return VaultStatus(True, str(directory))
    except OSError as exc:
        return VaultStatus(False, str(directory), f"cannot create {directory}: {exc}")


def _path(home: Path, network: str) -> Path:
    return credentials_dir(home) / f"{network}.cred"


def remember(home: Path, network: str, passphrase: str) -> None:
    """Store `passphrase` for `network`, bound to this machine."""
    state = status(home)
    if not state.available:
        raise RuntimeError(state.reason)

    box = nacl.secret.SecretBox(_key_for(network))
    blob = MAGIC + bytes([VERSION]) + box.encrypt(passphrase.encode("utf-8"))

    path = _path(home, network)
    # Written with O_EXCL-style restrictive permissions from the outset rather
    # than chmod-ed afterwards, which would leave a readable window.
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, blob)
    finally:
        os.close(fd)
    tmp.replace(path)
    log.info("passphrase for %s stored at %s", network, path)


def recall(home: Path, network: str) -> str | None:
    """The stored passphrase, or None.

    Returns None rather than raising when the file exists but cannot be
    decrypted: that means it came from a different machine or user, which is the
    binding working as intended, not an error the caller should handle.
    """
    path = _path(home, network)
    try:
        blob = path.read_bytes()
    except OSError:
        return None

    if len(blob) < 5 or blob[:4] != MAGIC:
        log.warning("%s is not a credentials file", path)
        return None
    if blob[4] != VERSION:
        log.warning("%s has an unsupported version", path)
        return None

    try:
        return nacl.secret.SecretBox(_key_for(network)).decrypt(blob[5:]).decode("utf-8")
    except (CryptoError, UnicodeDecodeError):
        log.info("%s was written on a different machine or by a different user", path)
        return None


def forget(home: Path, network: str) -> bool:
    """Delete the stored passphrase. Returns whether there was one."""
    path = _path(home, network)
    if not path.exists():
        return False
    try:
        # Overwrite before unlinking. Of limited value on a journalling
        # filesystem or SSD, but it costs nothing and removes the obvious case.
        size = path.stat().st_size
        with path.open("r+b") as handle:
            handle.write(os.urandom(size))
            handle.flush()
            os.fsync(handle.fileno())
        path.unlink()
        log.info("removed stored passphrase for %s", network)
        return True
    except OSError as exc:
        log.warning("could not remove %s: %s", path, exc)
        return False


def is_remembered(home: Path, network: str) -> bool:
    return _path(home, network).exists()


def list_stored(home: Path) -> list[str]:
    """Which networks have a stored passphrase."""
    directory = credentials_dir(home)
    if not directory.exists():
        return []
    return sorted(p.stem for p in directory.glob("*.cred"))

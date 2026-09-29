#!/usr/bin/env python3
"""DogecoinArcade installer.

Single file, standard library only, so it runs on a machine that has nothing but
Python. It does what we did by hand over the course of this project:

  1. detect the platform and architecture
  2. download the matching Pepecoin Core release
  3. verify its SHA-256 **and** its GPG signature, and stop if either fails
  4. install the binaries
  5. write a mainnet config (ledger) and a testnet config (messenger)
  6. register both as services
  7. install DogecoinArcade itself
  8. start everything and open the browser

Every one of the following cost a debugging cycle during development and is
encoded here so nobody repeats it:

  * `RestrictAddressFamilies` must include AF_NETLINK, or libzmq aborts at
    startup with "Address family not supported by protocol".
  * `-daemonwait` does not exist in this 0.13/0.14-era codebase; use
    Type=simple and run in the foreground.
  * The rpcauth HMAC uses the salt as the **raw ASCII of the hex string**, not
    decoded hex. Getting it wrong gives a silent authentication failure.
  * `.cookie` is always mode 0600, so cookie auth only works when the client
    runs as the same user as the daemon. That dictates the service user.
  * The release signing key is expired and is not on public keyservers, so it
    must come from the source tree.
  * Verify before executing, always.

Usage:
    python3 install.py                 # install everything
    python3 install.py --dry-run       # show what would happen
    python3 install.py --skip-core     # only install the application
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

#: Where an installed copy fetches updates from. The repository is served over
#: plain HTTP from the site, so `git clone` and `git pull` both work with no
#: account, no key and no forge.
REPO_URL = "https://dogecoinarcade.com/repo"

#: The same code as a plain archive, for machines with no git. Windows is why:
#: git is a separate 60 MB download there, and a user who installs it still has
#: a terminal holding the old PATH, so the installer cannot see it even after
#: they have done what it asked. The archive needs nothing but this script.
ARCHIVE_URL = "https://dogecoinarcade.com/source.tar.gz"
ARCHIVE_SUMS_URL = "https://dogecoinarcade.com/source.tar.gz.sha256"
REVISION_URL = "https://dogecoinarcade.com/source.rev"
#: Where a new node gets its index (2026-09-28: "the installer installs
#: the most current version ... with the most recent bootstrap"): the live
#: node's own published copies, remade whenever they go stale (bootstrap.py).
BOOTSTRAP_URL = "https://app.dogecoinarcade.com/bootstrap"
BOOTSTRAP_NETWORKS = ("test", "main")

#: Written into a checkout fetched as an archive, because it has no .git for
#: `git rev-parse` to read and the updater still has to know what is installed.
REVISION_FILE = ".revision"


class Coin:
    """Everything that differs between the two chains.

    The protocol is identical on both -- same OP_RETURN limit, same bare-multisig
    rules, same fees, same dust, down to the same source line numbers, because
    Pepecoin is a Dogecoin fork. Only chain identity differs, so supporting both
    is a matter of data rather than code.
    """

    def __init__(self, key, name, version, repo, binaries, prefix,
                 signer, key_file, datadir_name, app_name, sums):
        self.key = key                    # "pepecoin" / "dogecoin"
        self.name = name                  # shown to the user
        self.version = version
        self.repo = repo                  # GitHub owner/repo
        self.binaries = binaries          # daemon, cli, tx
        self.prefix = prefix              # release asset prefix
        self.signer = signer              # expected signing key fingerprint
        self.key_file = key_file          # its path within the source tree
        self.datadir_name = datadir_name  # platform datadir leaf
        self.app_name = app_name          # macOS .app bundle
        self.sums = sums                  # pinned SHA-256 per asset

    @property
    def base_url(self):
        return f"https://github.com/{self.repo}/releases/download/v{self.version}"

    @property
    def key_url(self):
        return (f"https://raw.githubusercontent.com/{self.repo}/"
                f"v{self.version}/contrib/gitian-keys/{self.key_file}")

    def asset(self, system, machine):
        suffix = {
            ("Linux", "x86_64"):  "x86_64-linux-gnu.tar.gz",
            ("Linux", "aarch64"): "aarch64-linux-gnu.tar.gz",
            ("Linux", "arm64"):   "aarch64-linux-gnu.tar.gz",
            ("Linux", "armv7l"):  "arm-linux-gnueabihf.tar.gz",
            ("Windows", "AMD64"): "win64.zip",
            ("Darwin", "x86_64"): "osx-unsigned.dmg",
            ("Darwin", "arm64"):  "osx-unsigned.dmg",
        }.get((system, machine))
        return f"{self.prefix}-{self.version}-{suffix}" if suffix else None


PEPECOIN = Coin(
    key="pepecoin", name="Pepecoin", version="1.1.0", repo="pepecoinppc/pepecoin",
    binaries=["pepecoind", "pepecoin-cli", "pepecoin-tx"],
    prefix="pepecoin",
    signer="18250EC92E527E9723E49116FD415169D2691927",
    key_file="david2278-key.pgp",
    datadir_name="Pepecoin", app_name="Pepecoin-Qt.app",
    sums={
        "pepecoin-1.1.0-x86_64-linux-gnu.tar.gz":
            "9d7ef948e5726c9941cbc5307b4a0b725edc715bc10ed5515154485faecd710b",
        "pepecoin-1.1.0-aarch64-linux-gnu.tar.gz":
            "c0abd1451e978a171ca5a7d37a57ecb79cc70c6755acf473018acbcd8d31083c",
        "pepecoin-1.1.0-arm-linux-gnueabihf.tar.gz":
            "42394118ecabdb25894a69650f3981fe6ca13eeb23ce7847974437a66a770832",
        "pepecoin-1.1.0-win64.zip":
            "0df90ce84518f1bd827f67fb4900785ce4bfa422304f1a0bc768c0d2489fdf63",
        "pepecoin-1.1.0-osx-unsigned.dmg":
            "9c8cb2c59d96e7db95ca1e6d19ae31de5e81ba326be1b6065882c239a6220c32",
    },
)

DOGECOIN = Coin(
    key="dogecoin", name="Dogecoin", version="1.14.9", repo="dogecoin/dogecoin",
    binaries=["dogecoind", "dogecoin-cli", "dogecoin-tx"],
    prefix="dogecoin",
    # Patrick Lodder. Unlike Pepecoin's signing key, this one has not expired.
    signer="DC6EF4A8BF9F1B1E4DE1EE522D3A345B98D0DC1F",
    key_file="patricklodder-key.pgp",
    datadir_name="Dogecoin", app_name="Dogecoin-Qt.app",
    sums={
        "dogecoin-1.14.9-x86_64-linux-gnu.tar.gz":
            "4f227117b411a7c98622c970986e27bcfc3f547a72bef65e7d9e82989175d4f8",
        "dogecoin-1.14.9-aarch64-linux-gnu.tar.gz":
            "6928c895a20d0bcb6d5c7dcec753d35c884a471aaf8ad4242a89a96acb4f2985",
        "dogecoin-1.14.9-arm-linux-gnueabihf.tar.gz":
            "311fe8aee346d3f9a00c0a8ac594224ca3bfa297fec8a5fae20bb70f28961421",
        "dogecoin-1.14.9-win64.zip":
            "45864cedc210e6d573c7efd5f6694a440147d9773c4a8851a99882a2727ad804",
        "dogecoin-1.14.9-osx-unsigned.dmg":
            "c87c956834a87da8200274a097364c986ccca045d71ce92d0f7d407129d25a83",
    },
)

COINS = {c.key: c for c in (PEPECOIN, DOGECOIN)}

#: How well each platform is actually supported. Stated plainly, because an
#: installer that half-works silently is worse than one that says it cannot.
SUPPORT = {
    ("Linux", "x86_64"):  "tested",
    ("Linux", "aarch64"): "untested",
    ("Linux", "arm64"):   "untested",
    ("Linux", "armv7l"):  "untested",
    ("Windows", "AMD64"): "untested",
    ("Darwin", "x86_64"): "untested",
    ("Darwin", "arm64"):  "untested",
}


class InstallError(Exception):
    """Something went wrong that the user has to know about."""


# --- output -------------------------------------------------------------------

def step(n: int, total: int, text: str) -> None:
    print(f"\n[{n}/{total}] {text}", flush=True)


def info(text: str) -> None:
    print(f"      {text}", flush=True)


def warn(text: str) -> None:
    print(f"  !   {text}", flush=True)


def fail(text: str) -> None:
    raise InstallError(text)


def choose_chain() -> list:
    """Ask which chain to install a node for.

    Asked rather than assumed, because the two chains are entirely separate
    networks and installing the wrong one leaves a user with a node that can
    never see their coins. `--coin` skips this for scripted installs.
    """
    print()
    print("  Which chain do you want to use?")
    print()
    print("    1) Pepecoin   -- the default")
    print("    2) Dogecoin")
    print("    3) Both       -- two nodes, one application")
    print()
    print("  The application is identical on either. You can add the other later")
    print("  by running this installer again.")
    print()
    while True:
        try:
            answer = input("  Choose 1, 2 or 3 [1]: ").strip().lower()
        except EOFError:
            answer = ""
        if answer in ("", "1", "p", "pep", "pepe", "pepecoin"):
            return [PEPECOIN]
        if answer in ("2", "d", "doge", "dogecoin"):
            return [DOGECOIN]
        if answer in ("3", "b", "both"):
            return [PEPECOIN, DOGECOIN]
        print("  Please answer 1, 2 or 3.")


# --- platform -----------------------------------------------------------------

def detect(coin) -> tuple[str, str, str]:
    system = platform.system()
    machine = platform.machine()
    asset = coin.asset(system, machine)
    if asset is None:
        fail(
            f"no {coin.name} Core build for {system}/{machine}. Supported: "
            + ", ".join(f"{s}/{m}" for s, m in SUPPORT)
        )

    support = SUPPORT.get((system, machine), "untested")
    if support == "untested":
        warn(f"{system}/{machine} has not been tested. It should work; tell us if it does not.")
    return system, machine, asset


def datadirs(system: str, coin) -> tuple[Path, Path]:
    """(mainnet datadir, testnet datadir) for this platform and coin."""
    home = Path.home()
    name = coin.datadir_name
    if system == "Windows":
        base = Path(os.environ.get("APPDATA", home / "AppData/Roaming")) / name
        return base, base.with_name(f"{name}-testnet")
    if system == "Darwin":
        base = home / "Library/Application Support" / name
        return base, base.with_name(f"{name}-testnet")
    return home / f".{coin.key}", home / f".{coin.key}-testnet"


def bindir(system: str) -> Path:
    if system == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "DogecoinArcade" / "bin"
    # ~/.local/bin needs no root and is on PATH for most distributions.
    return Path.home() / ".local" / "bin"


# --- download and verify ------------------------------------------------------

#: Cloudflare in front of the site refuses `Python-urllib/3.x` with a 403, so
#: every fetch names itself (D-066).
USER_AGENT = "DogecoinArcade installer (+https://dogecoinarcade.com)"


def _request(url: str):
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT})


def download(url: str, dest: Path, label: str) -> Path:
    info(f"downloading {label}")
    try:
        with urllib.request.urlopen(_request(url), timeout=120) as response, \
                dest.open("wb") as out:
            total = int(response.headers.get("Content-Length", 0))
            read = 0
            while chunk := response.read(64 * 1024):
                out.write(chunk)
                read += len(chunk)
                if total:
                    pct = read * 100 // total
                    print(f"\r      {label}  {pct:3d}%  {read // 1024:,} KB", end="", flush=True)
        if total:
            print(flush=True)
    except urllib.error.URLError as exc:
        fail(f"could not download {url}: {exc}")
    return dest


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify_hash(archive: Path, sums_text: str, coin) -> None:
    """Check the archive against both the published sums and our pinned copy."""
    actual = sha256_of(archive)

    published = None
    for line in sums_text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == archive.name:
            published = parts[0]
            break
    if published is None:
        fail(f"{archive.name} is not listed in SHA256SUMS")
    if actual != published:
        fail(
            f"SHA-256 MISMATCH for {archive.name}\n"
            f"        expected {published}\n"
            f"        got      {actual}\n"
            f"      Refusing to install. Do not run this file."
        )
    info(f"sha256 matches the published sum ({actual[:16]}...)")

    pinned = coin.sums.get(archive.name)
    if pinned and pinned != actual:
        fail(
            f"the published sum matches the download, but BOTH differ from the "
            f"hash pinned in this installer.\n"
            f"        pinned {pinned}\n        got    {actual}\n"
            f"      That means the release itself changed. Stopping."
        )
    if pinned:
        info("sha256 also matches the hash pinned in this installer")


def verify_signature(sums_file: Path, workdir: Path, coin) -> bool:
    """Verify the GPG signature. Returns False if gpg is unavailable.

    The signing key is fetched from the Pepecoin **source tree**, not a
    keyserver: it is not published to one. It is also expired, which is fine --
    a signature made while a key was valid stays valid; expiry only prevents new
    signatures.
    """
    gpg = shutil.which("gpg") or shutil.which("gpg2")
    if not gpg:
        warn("gpg is not installed, so the signature cannot be checked.")
        warn("The SHA-256 check still passed. Install gnupg for the stronger check.")
        return False

    key = workdir / "signer.pgp"
    try:
        download(coin.key_url, key, "signing key")
    except InstallError:
        warn("could not fetch the signing key from the source tree")
        return False

    home = workdir / "gnupg"
    home.mkdir(mode=0o700, exist_ok=True)
    env = {**os.environ, "GNUPGHOME": str(home)}

    subprocess.run([gpg, "--batch", "--import", str(key)],
                   env=env, capture_output=True, check=False)
    result = subprocess.run(
        [gpg, "--batch", "--verify", str(sums_file)],
        env=env, capture_output=True, text=True, check=False,
    )
    output = result.stderr

    if "Good signature" not in output:
        fail(
            "GPG SIGNATURE VERIFICATION FAILED.\n"
            f"{output.strip()}\n"
            "      Refusing to install."
        )
    if coin.signer.replace(" ", "") not in output.replace(" ", ""):
        fail(
            f"the signature is valid but was made by an unexpected key.\n"
            f"      Expected {coin.signer}\n{output.strip()}"
        )
    info("gpg: good signature from the key committed in the Pepecoin source tree")
    if "expired" in output.lower():
        info("(that key has since expired; the signature predates expiry and stands)")
    return True


def fetch_core(workdir: Path, asset: str, coin) -> Path:
    archive = download(f"{coin.base_url}/{asset}", workdir / asset, asset)
    sums = download(f"{coin.base_url}/SHA256SUMS.asc", workdir / "SHA256SUMS.asc", "checksums")
    verify_hash(archive, sums.read_text(errors="replace"), coin)
    verify_signature(sums, workdir, coin)
    return archive


# --- install ------------------------------------------------------------------



def install_core_macos(archive: Path, workdir: Path, coin) -> Path:
    """Mount the .dmg, copy the app to /Applications, unmount.

    The macOS release contains no daemon -- only Pepecoin-Qt.app -- so that is
    what gets installed. Pepecoin-Qt serves the same RPC interface as pepecoind
    when run with -server=1, which is all the application needs.
    """
    hdiutil = shutil.which("hdiutil")
    if not hdiutil:
        fail("hdiutil not found; this does not look like macOS")

    mount = workdir / "mnt"
    mount.mkdir(exist_ok=True)
    result = subprocess.run(
        [hdiutil, "attach", str(archive), "-nobrowse", "-readonly", "-mountpoint", str(mount)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        fail(f"could not mount the disk image: {result.stderr.strip()}")

    try:
        source = mount / coin.app_name
        if not source.exists():
            fail(f"{coin.app_name} is not in the disk image")
        destination = Path("/Applications") / coin.app_name
        if destination.exists():
            info(f"{destination} already exists; replacing it")
            shutil.rmtree(destination)
        shutil.copytree(source, destination, symlinks=True)
        info(f"installed {destination}")
        # Unsigned build: without this, Gatekeeper refuses to launch it and the
        # user sees "damaged and can't be opened", which is misleading.
        subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(destination)], check=False)
        info("cleared the quarantine attribute (the build is unsigned)")
        return destination
    finally:
        subprocess.run([hdiutil, "detach", str(mount), "-quiet"], check=False)


def install_binary(source: Path, destination: Path) -> None:
    """Replace a binary that may currently be running.

    Writing directly over a running executable fails with "Text file busy"
    (ETXTBSY) on Linux, which is exactly what happens when the installer is run a
    second time while the node it installed is up -- the ordinary upgrade case.

    Copying beside it and renaming avoids the problem entirely: rename replaces
    the directory entry, and the running process keeps its own inode until it
    exits. It is also atomic, so an interrupted install cannot leave a truncated
    binary behind.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(destination.name + ".new")
    shutil.copy2(source, staging)
    staging.chmod(0o755)
    try:
        os.replace(staging, destination)
    except OSError as exc:
        staging.unlink(missing_ok=True)
        fail(f"could not replace {destination}: {exc}")


def core_version(binary: Path | str) -> str | None:
    """Ask a daemon what it is. None if it will not answer."""
    try:
        result = subprocess.run([str(binary), "--version"], capture_output=True,
                                text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    first = (result.stdout or result.stderr).strip().splitlines()
    if not first:
        return None
    # "Pepecoin Core Daemon version v1.1.0" -> "v1.1.0"
    words = first[0].split()
    return words[-1] if words else None


def core_search_paths(coin, target: Path, system: str) -> list[Path]:
    """Where a daemon already on this machine is likely to be.

    Someone who has been running a node for years is the person most likely to
    want this application, and the installer used to download 30 MB and write
    over their binary without looking. PATH first, because that is the one they
    actually run.
    """
    daemon = coin.binaries[0] + (".exe" if system == "Windows" else "")
    found = [target / daemon]
    on_path = shutil.which(coin.binaries[0])
    if on_path:
        found.append(Path(on_path))
    if system == "Windows":
        for base in filter(None, (os.environ.get("ProgramFiles"),
                                  os.environ.get("ProgramFiles(x86)"),
                                  os.environ.get("LOCALAPPDATA"))):
            found.append(Path(base) / coin.app_name / "daemon" / daemon)
    elif system == "Darwin":
        found += [Path(f"/Applications/{coin.app_name}.app/Contents/MacOS") / daemon,
                  Path("/usr/local/bin") / daemon,
                  Path("/opt/homebrew/bin") / daemon]
    else:
        found += [Path("/usr/local/bin") / daemon, Path("/usr/bin") / daemon,
                  Path.home() / ".local/bin" / daemon]
    seen, unique = set(), []
    for path in found:
        if str(path) not in seen:
            seen.add(str(path))
            unique.append(path)
    return unique


def existing_core(coin, target: Path, system: str) -> tuple[Path, str | None] | None:
    """A daemon already installed here, if there is one: (path, version)."""
    for candidate in core_search_paths(coin, target, system):
        if candidate.is_file():
            return candidate, core_version(candidate)
    return None


def running_core(coin) -> str | None:
    """A daemon of this coin already running, whoever installed it.

    Reported separately from the file on disk: a node that is up is the reason
    not to touch anything, and it is also the thing an installer is most able to
    break by replacing a binary underneath it.
    """
    daemon = coin.binaries[0]
    try:
        if platform.system() == "Windows":
            result = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {daemon}.exe"],
                                    capture_output=True, text=True, timeout=20)
            return daemon if daemon.lower() in result.stdout.lower() else None
        result = subprocess.run(["pgrep", "-a", daemon], capture_output=True,
                                text=True, timeout=20)
        line = result.stdout.strip().splitlines()
        return line[0] if line else None
    except (OSError, subprocess.SubprocessError):
        return None


def restart_running_nodes(coin) -> list[str]:
    """Restart any node services we installed, so the new binary takes effect.

    Without this an upgrade appears to succeed while the old binary keeps
    running, which is worse than failing: the version on disk and the version in
    memory disagree, silently.
    """
    if not shutil.which("systemctl"):
        return []
    restarted = []
    for label in ("mainnet", "testnet"):
        unit = f"{coin.binaries[0]}-{label}.service"
        active = subprocess.run(["systemctl", "--user", "is-active", unit],
                                capture_output=True, text=True).stdout.strip()
        if active == "active":
            subprocess.run(["systemctl", "--user", "restart", unit], check=False)
            restarted.append(unit)
    return restarted


def install_core(archive: Path, target: Path, system: str, coin) -> None:
    if system == "Darwin":
        install_core_macos(archive, archive.parent, coin)
        return

    target.mkdir(parents=True, exist_ok=True)
    workdir = archive.parent

    if archive.suffix == ".zip":
        import zipfile
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(workdir)
    else:
        with tarfile.open(archive) as tf:
            # Refuse anything that escapes the extraction directory. A malicious
            # archive should not be able to write outside it, even though this
            # one is signed.
            for member in tf.getmembers():
                resolved = (workdir / member.name).resolve()
                if not str(resolved).startswith(str(workdir.resolve())):
                    fail(f"archive contains an unsafe path: {member.name}")
            tf.extractall(workdir)

    extracted = next((p for p in workdir.glob(f"{coin.prefix}-{coin.version}*") if p.is_dir()), None)
    if extracted is None:
        fail(f"could not find the extracted {coin.name} directory")

    suffix = ".exe" if system == "Windows" else ""
    for name in coin.binaries:
        source = extracted / "bin" / f"{name}{suffix}"
        if not source.exists():
            fail(f"{source} is missing from the archive")
        install_binary(source, target / f"{name}{suffix}")
    info(f"installed {', '.join(coin.binaries)} to {target}")


WINDOWS_TASK = (
    'schtasks /Create /F /TN "DogecoinArcade\\pepecoin-{label}" /SC ONLOGON '
    '/TR "\'{binary}\' -datadir=\'{datadir}\'" /RL LIMITED'
)


def windows_startup_dir() -> Path:
    """The per-user Startup folder: what runs at logon without any privilege."""
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData/Roaming")
    return Path(appdata) / "Microsoft/Windows/Start Menu/Programs/Startup"


def install_startup_entry(name: str, command: str) -> Path | None:
    """Start something at logon by putting a .cmd in the user's Startup folder.

    Used when `schtasks` refuses. A Scheduled Task with /SC ONLOGON needs
    administrator rights -- the first version of this said it did not, and a
    Windows user got "ERROR: Access is denied." twice and a working install with
    nothing starting on its own. The Startup folder needs no rights at all: it
    is the user's own folder, and it is what the user can see and delete.
    """
    try:
        folder = windows_startup_dir()
        folder.mkdir(parents=True, exist_ok=True)
        entry = folder / f"{name}.cmd"
        entry.write_text(f'@echo off\r\nstart "" /min {command}\r\n')
        return entry
    except OSError as exc:
        warn(f"could not write a Startup entry for {name}: {exc}")
        return None


def install_services_windows(target: Path, main_dir: Path, test_dir: Path, coin) -> list[str]:
    """Start both nodes at logon.

    Windows has no systemd. A Scheduled Task is the tidier equivalent but
    /SC ONLOGON needs administrator rights, so when it is refused this falls
    back to the user's Startup folder, which never is.
    """
    binary = target / f"{coin.binaries[0]}.exe"
    if not binary.exists():
        warn(f"{binary} not found; skipping service registration")
        return []

    installed = []
    for label, datadir in (("mainnet", main_dir), ("testnet", test_dir)):
        command = WINDOWS_TASK.format(label=f"{coin.key}-{label}", binary=binary, datadir=datadir)
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        if result.returncode == 0:
            installed.append(label)
            info(f"registered logon task for {label}")
            continue
        entry = install_startup_entry(f"DogecoinArcade-{coin.key}-{label}",
                                      f'"{binary}" -datadir="{datadir}"')
        if entry is not None:
            installed.append(label)
            info(f"{label} will start at logon ({entry.name})")
        else:
            warn(f"could not start the {label} node automatically: "
                 f"{result.stdout.strip() or result.stderr.strip()}")
            warn(f"  start it yourself: {binary} -datadir={datadir}")
    return installed





CONF_TEMPLATE = """\
# {coin} Core -- {net}
# Written by the DogecoinArcade installer.
{testnet}
server=1
txindex=1
prune=0

rpcbind=127.0.0.1
rpcallowip=127.0.0.1

# Cookie authentication: the daemon regenerates .cookie at every start, so no
# password is ever written to a file. This works because the node runs as you.

dbcache=512

# ZMQ, for the indexer. Ports are offset per coin and network so that two chains
# can be installed side by side without colliding.
zmqpubhashblock=tcp://127.0.0.1:{z0}
zmqpubrawblock=tcp://127.0.0.1:{z1}
zmqpubrawtx=tcp://127.0.0.1:{z2}
"""

#: Base ZMQ port per coin and network. Pepecoin mainnet 28332, testnet 28432;
#: Dogecoin mainnet 28532, testnet 28632.
ZMQ_BASE = {
    ("pepecoin", "mainnet"): 28332, ("pepecoin", "testnet"): 28432,
    ("dogecoin", "mainnet"): 28532, ("dogecoin", "testnet"): 28632,
}


def write_configs(main_dir: Path, test_dir: Path, coin) -> None:
    for directory, label in ((main_dir, "mainnet"), (test_dir, "testnet")):
        base = ZMQ_BASE[(coin.key, label)]
        content = CONF_TEMPLATE.format(
            coin=coin.name,
            net=("MAINNET (the ledger: tokens, NFTs and coins)" if label == "mainnet"
                 else "TESTNET (the Messenger)"),
            testnet=("" if label == "mainnet" else "\ntestnet=1"),
            z0=base, z1=base + 1, z2=base + 3,
        )
        directory.mkdir(parents=True, exist_ok=True)
        conf = directory / f"{coin.key}.conf"
        if conf.exists():
            info(f"{label} config already exists, leaving it alone: {conf}")
            continue
        conf.write_text(content)
        conf.chmod(0o600)
        info(f"wrote {conf}")


SYSTEMD_UNIT = """\
[Unit]
Description={label} Core for DogecoinArcade
After=network-online.target
Wants=network-online.target

[Service]
# Type=simple and foreground: this codebase has no -daemonwait, so Type=forking
# would race against the PID file.
Type=simple
ExecStart={binary} -datadir={datadir}
# `always`, not `on-failure`: a clean shutdown is how the application swaps a
# restored wallet.dat into place, and the node has to come back afterwards.
# An explicit `systemctl stop` is still honoured -- systemd does not fight that.
Restart=always
RestartSec=5
TimeoutStopSec=600

# Runs as you, so the 0600 .cookie it writes is readable by the application.
# Any other user and cookie authentication cannot work at all.

# AF_NETLINK is required: libzmq opens a netlink socket to enumerate interfaces,
# and without it the process aborts at startup.
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictRealtime=true

[Install]
WantedBy=default.target
"""


LAUNCHD_PLIST = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.dogecoinarcade.pepecoin-{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{binary}</string>
    <string>-datadir={datadir}</string>
    <string>-server=1</string>
    {extra}
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>{datadir}/launchd.err.log</string>
</dict>
</plist>
"""


def install_services_macos(main_dir: Path, test_dir: Path, coin) -> list[str]:
    """Register launchd agents for both nodes.

    macOS has no daemon build, so these run Pepecoin-Qt with -server=1. It is a
    GUI application, so each agent opens a window -- unavoidable until upstream
    ships pepecoind for macOS.
    """
    binary = Path("/Applications") / coin.app_name / f"Contents/MacOS/{coin.app_name[:-4]}"
    if not binary.exists():
        warn(f"{binary} not found; skipping service registration")
        return []

    agents = Path.home() / "Library/LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    installed = []
    for label, datadir, extra in (
        ("mainnet", main_dir, ""),
        ("testnet", test_dir, "<string>-testnet=1</string>"),
    ):
        plist = agents / f"com.dogecoinarcade.{coin.key}-{label}.plist"
        plist.write_text(
            LAUNCHD_PLIST.format(label=f"{coin.key}-{label}", binary=binary, datadir=datadir, extra=extra)
        )
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True, check=False)
        subprocess.run(["launchctl", "load", str(plist)], capture_output=True, check=False)
        installed.append(plist.name)
        info(f"wrote {plist}")
    warn(f"macOS has no headless build, so each node opens a {coin.app_name} window.")
    return installed


#: A machine may already have a SYSTEM unit of the same name. Module-level so a
#: test can point it somewhere else -- checking the real path made the installer
#: tests depend on whether the host happened to have one, which is the same
#: hermeticity trap as the node-discovery tests reaching a real datadir.
SYSTEM_WEB_UNIT = Path("/etc/systemd/system/arcade-web.service")

ARCADE_WEB_UNIT = """\
[Unit]
Description=DogecoinArcade web interface
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={binary}
Restart=always
RestartSec=10

# AF_NETLINK is needed by libraries that enumerate interfaces; omitting it is
# what crash-looped the node units twice during development.
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=default.target
"""


def install_web_service(venv: Path, system: str) -> bool:
    """Register the web interface so it starts at login.

    Omitted from the first version, which meant a completed install left nothing
    listening on :8420 and no indication why -- found on a second machine, where
    the interface simply was not there.
    """
    binary = venv / ("Scripts/arcade-web.exe" if system == "Windows" else "bin/arcade-web")
    if not binary.exists():
        warn(f"{binary} not found; skipping the web service")
        return False

    if system == "Linux" and shutil.which("systemctl"):
        unit_dir = Path.home() / ".config/systemd/user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        (unit_dir / "arcade-web.service").write_text(ARCADE_WEB_UNIT.format(binary=binary))
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "--user", "enable", "--now", "arcade-web.service"],
                       check=False)
        info("registered arcade-web (starts at login)")
        return True

    if system == "Darwin":
        agents = Path.home() / "Library/LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        plist = agents / "com.dogecoinarcade.web.plist"
        plist.write_text(LAUNCHD_PLIST.format(
            label="web", binary=binary, datadir=Path.home() / ".dogecoinarcade", extra=""))
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True, check=False)
        subprocess.run(["launchctl", "load", str(plist)], capture_output=True, check=False)
        info("registered the web interface as a launchd agent")
        return True

    if system == "Windows":
        command = (f'schtasks /Create /F /TN "DogecoinArcade\\web" /SC ONLOGON '
                   f'/TR "\'{binary}\'" /RL LIMITED')
        if subprocess.run(command, shell=True, capture_output=True).returncode == 0:
            info("registered the web interface as a logon task")
            return True
        # Same reason as the nodes: ONLOGON wants administrator rights.
        entry = install_startup_entry("DogecoinArcade", f'"{binary}"')
        if entry is not None:
            info(f"the interface will start at logon ({entry.name})")
            return True

    warn(f"could not register the web interface; start it yourself: {binary}")
    return False


def install_services(system: str, target: Path, main_dir: Path, test_dir: Path, coin) -> list[str]:
    """Register both nodes so they start on login. Returns what was installed."""
    if system == "Darwin":
        return install_services_macos(main_dir, test_dir, coin)

    if system == "Windows":
        return install_services_windows(target, main_dir, test_dir, coin)

    if system != "Linux" or not shutil.which("systemctl"):
        warn("no systemd here; start the nodes yourself:")
        warn(f"  {target}/{coin.binaries[0]} -datadir={main_dir}")
        warn(f"  {target}/{coin.binaries[0]} -datadir={test_dir}")
        return []

    # A *user* unit needs no root at all.
    unit_dir = Path.home() / ".config/systemd/user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    installed = []
    for label, datadir in (("mainnet", main_dir), ("testnet", test_dir)):
        name = f"{coin.binaries[0]}-{label}.service"
        (unit_dir / name).write_text(
            SYSTEMD_UNIT.format(label=f"{coin.name} {label}", binary=target / coin.binaries[0], datadir=datadir)
        )
        installed.append(name)
        info(f"wrote {unit_dir / name}")

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    for name in installed:
        subprocess.run(["systemctl", "--user", "enable", "--now", name], check=False)
    # Without lingering, user services stop at logout -- which would silently
    # stop the node whenever the user logs out.
    subprocess.run(["loginctl", "enable-linger", os.environ.get("USER", "")], check=False)
    return installed


# --- the application ----------------------------------------------------------

def find_source(dry_run: bool) -> Path:
    """Locate the application source, fetching it if this is a standalone run.

    Two situations, and the first implementation only handled the second:

      * **downloaded on its own** -- the usual case. This file sits wherever the
        user put it, with no repository anywhere near it, so the code has to be
        fetched. `Path(__file__).parent.parent` here resolves to something like
        /home, which produced the memorable error "Directory '/home[web]' is not
        installable".
      * **run from inside a checkout** -- the development case, where the repo
        root is two levels up and already has a pyproject.toml.
    """
    local = Path(__file__).resolve().parent.parent
    if (local / "pyproject.toml").exists():
        info(f"using the checkout at {local}")
        return local

    checkout = Path.home() / ".dogecoinarcade" / "src"
    if dry_run:
        info(f"would fetch the application from {REPO_URL} into {checkout}")
        return checkout

    git = find_git()
    if not git:
        info("git is not installed; fetching the source archive instead")
        fetch_source_archive(checkout)
        return checkout

    if (checkout / ".git").exists():
        info(f"updating {checkout}")
        result = subprocess.run([git, "-C", str(checkout), "pull", "--ff-only", "--quiet"],
                                capture_output=True, text=True)
        if result.returncode != 0:
            fail(f"could not update {checkout}: {result.stderr.strip()}")
    else:
        checkout.parent.mkdir(parents=True, exist_ok=True)
        info(f"fetching the application from {REPO_URL}")
        result = subprocess.run([git, "clone", "--quiet", REPO_URL, str(checkout)],
                                capture_output=True, text=True)
        if result.returncode != 0:
            fail(f"could not fetch {REPO_URL}: {result.stderr.strip()}")
    return checkout


def find_git() -> str | None:
    """git, including where Windows put it a moment ago.

    `shutil.which` reads the PATH this process inherited. A user told "install
    git" does so, comes back to the same Command Prompt and is told again that
    git is not installed -- because that window's PATH was fixed when it opened.
    Looking in the standard install locations makes the obvious thing work.
    """
    git = shutil.which("git")
    if git:
        return git
    if os.name != "nt":
        return None
    for base in filter(None, (os.environ.get("ProgramFiles"),
                              os.environ.get("ProgramFiles(x86)"),
                              os.environ.get("LOCALAPPDATA"))):
        candidate = Path(base) / "Git" / "cmd" / "git.exe"
        if candidate.exists():
            return str(candidate)
    return None


def fetch_source_archive(checkout: Path, expect_sha256: str | None = None) -> str | None:
    """Fetch the application as an archive. Returns the revision, if published.

    With no `expect_sha256` the archive is checked against the SHA-256
    published beside it, which catches a truncated or corrupted download and
    nothing else: whoever could replace the archive could replace the sum next
    to it. That is the trust a FIRST install has, and cannot do better -- this
    script runs before the application exists, on whatever Python is lying
    around, with no signature checking available to it.

    An update is a different situation and passes `expect_sha256`: the hash out
    of a manifest it has already checked the signature on (arcade/release.py).
    Then the site cannot serve a different archive than the one that was
    signed, and the sums file beside it does not matter (D-065).
    """
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        archive = download(ARCHIVE_URL, workdir / "source.tar.gz", "source.tar.gz")
        expected = ([str(expect_sha256)] if expect_sha256
                    else read_url(ARCHIVE_SUMS_URL, "checksum").split())
        actual = sha256_of(archive)
        if not expected or expected[0] != actual:
            fail(
                f"SHA-256 MISMATCH for the source archive\n"
                f"        expected {expected[0] if expected else '(none published)'}\n"
                f"        got      {actual}\n"
                f"      Refusing to install."
            )
        info(f"sha256 matches the {'signed manifest' if expect_sha256 else 'published sum'}"
             f" ({actual[:16]}...)")

        unpacked = workdir / "unpacked"
        unpacked.mkdir()
        with tarfile.open(archive) as tf:
            for member in tf.getmembers():
                resolved = (unpacked / member.name).resolve()
                if not str(resolved).startswith(str(unpacked.resolve())):
                    fail(f"archive contains an unsafe path: {member.name}")
            tf.extractall(unpacked, filter="data")

        root = next((p for p in unpacked.iterdir() if (p / "pyproject.toml").is_file()),
                    None)
        if root is None:
            fail("the source archive does not contain the application")

        revision = (read_url(REVISION_URL, "revision", quiet=True) or "").strip() or None
        if revision:
            (root / REVISION_FILE).write_text(revision + "\n")

        # Replaced wholesale rather than merged: a file deleted upstream has to
        # disappear here too, and a half-updated checkout is worse than either
        # version of it. Nothing personal lives here -- the identity, messages
        # and chain data are all in ~/.dogecoinarcade itself, not in src.
        checkout.parent.mkdir(parents=True, exist_ok=True)
        previous = checkout.with_name(checkout.name + ".old")
        shutil.rmtree(previous, ignore_errors=True)
        if checkout.exists():
            checkout.rename(previous)
        try:
            shutil.move(str(root), str(checkout))
        except Exception:
            if previous.exists() and not checkout.exists():
                previous.rename(checkout)          # put back what was working
            raise
        shutil.rmtree(previous, ignore_errors=True)

    info(f"fetched the application into {checkout}"
         + (f" ({revision[:7]})" if revision else ""))
    return revision


def fetch_bootstraps(dry_run: bool) -> None:
    """Start each chain's index from the latest bootstrap instead of block zero.

    Only where there is no index yet: an existing one is this machine's own and
    is never replaced. The copy is checked against its manifest (size and
    sha256) before it is unpacked, and unpacked beside its final name, then
    renamed, so a broken download leaves nothing half-written. A failure is a
    warning, not an error: the node then builds its index itself, slowly."""
    import gzip
    import json as _json
    home = Path.home() / ".dogecoinarcade"
    for net in BOOTSTRAP_NETWORKS:
        target = home / f"{net}-ledger.sqlite"
        if target.exists() and target.stat().st_size > 0:
            info(f"{net}: an index is already here; kept as it is")
            continue
        if dry_run:
            info(f"would fetch {BOOTSTRAP_URL}/{net}.sqlite.gz into {target}")
            continue
        try:
            said = _json.loads(read_url(f"{BOOTSTRAP_URL}/{net}.json", f"{net} bootstrap manifest"))
            with tempfile.TemporaryDirectory(prefix="arcade-bootstrap-") as tmp:
                packed = download(f"{BOOTSTRAP_URL}/{net}.sqlite.gz", Path(tmp) / f"{net}.sqlite.gz",
                                  f"{net} bootstrap")
                if said.get("bytes") and packed.stat().st_size != int(said["bytes"]):
                    raise InstallError("its size is not what its manifest says")
                if sha256_of(packed) != str(said.get("sha256", "")).lower():
                    raise InstallError("its sha256 is not what its manifest says")
                home.mkdir(parents=True, exist_ok=True)
                partial = target.with_suffix(".sqlite.partial")
                with gzip.open(packed, "rb") as src, open(partial, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
                partial.replace(target)
            info(f"{net}: index from block {int(said.get('height') or 0):,} "
                 f"(the node carries on from there)")
        except Exception as exc:                      # noqa: BLE001 -- the node can build its own
            warn(f"{net}: no bootstrap ({exc}); the node will build its index itself")


def read_url(url: str, label: str, quiet: bool = False) -> str:
    """Fetch a small text file. Returns "" when it is not there and quiet."""
    try:
        with urllib.request.urlopen(_request(url), timeout=60) as response:
            return response.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        if quiet:
            return ""
        fail(f"could not download the {label} from {url}: {exc}")
    return ""


def install_app(dry_run: bool) -> Path:
    """Install DogecoinArcade into its own virtual environment.

    A dedicated venv rather than the system Python: the dependencies are pinned,
    and pinned versions in a shared environment is how you break someone's other
    software.
    """
    venv = Path.home() / ".dogecoinarcade" / "venv"
    source = find_source(dry_run)

    if dry_run:
        info(f"would create {venv} and install from {source}")
        return venv

    if not venv.exists():
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        info(f"created {venv}")

    # Before the install rather than after it: an environment with no
    # application in it is exactly the state this repairs, and an install that
    # stops halfway is one way to reach it.
    write_repair(venv)

    pip = venv / ("Scripts/pip.exe" if os.name == "nt" else "bin/pip")
    # pip needs to be current enough to understand modern metadata; the version
    # shipped inside an older venv often is not.
    subprocess.run([str(pip), "install", "-q", "--upgrade", "pip"],
                   capture_output=True, text=True)

    # The extras marker has to attach to the path as a single argument, and the
    # path must be one pip recognises as a project directory.
    result = subprocess.run(
        [str(pip), "install", "-q", "--upgrade", f"{source}[web]"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        fail(f"installing the application failed:\n{result.stderr[-1500:]}"
             + build_failure_hint(result.stderr + result.stdout))

    # pip has reported success and left nothing importable before, on a machine
    # where it had already removed the previous version. Everything downstream
    # -- the launcher, the service, the updater -- then fails with
    # ModuleNotFoundError, a long way from the step that actually went wrong.
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    check = subprocess.run([str(python), "-c", "import arcade"],
                           capture_output=True, text=True)
    if check.returncode != 0:
        fail(f"pip finished, but {python} still cannot import the "
             f"application:\n{check.stderr[-800:]}")
    info("installed DogecoinArcade and its dependencies")
    return venv


def build_failure_hint(output: str) -> str:
    """Explain a pip failure that is really a missing wheel.

    "Failed to build 'pyzmq'" with a line about scikit-build-core tells a user
    nothing they can act on. What it means is that pip found no wheel for this
    Python and tried to compile from source, which needs a compiler that Windows
    does not have. Naming the Python version is the useful part: the fix is
    almost always a Python the package has wheels for.
    """
    if "did not run successfully" not in output and "Failed to build" not in output:
        return ""
    version = ".".join(str(n) for n in sys.version_info[:3])
    return (
        f"\n\n      That is a dependency with no ready-made build for Python "
        f"{version},\n"
        f"      so pip tried to compile it and this machine has no compiler.\n"
        f"      Installing DogecoinArcade with an older Python -- 3.12 or 3.13 "
        f"from\n"
        f"      python.org -- is the quickest way past it. Tell us which "
        f"version you\n"
        f"      were on; a dependency that needs a compiler is a bug on our side."
    )


#: Bumped whenever the text of a shim changes. Every shim carries it in a
#: comment, and an update rewrites the whole set when the machine's copies are
#: older. Without that a fix to a command reached only people installing for the
#: first time, because the updater rewrote shims only when one was missing.
SHIM_VERSION = 3

LAUNCHER = """\
#!/bin/sh
# DogecoinArcade launcher (shim {version}), written by the installer.
if [ ! -x "{venv}/bin/arcade-web" ]; then
    echo "DogecoinArcade is missing from its environment; repairing it first."
    "{venv}/bin/python" "{repair}" || exit 1
fi
exec "{venv}/bin/arcade-web" "$@"
"""

UPDATER = """\
#!/bin/sh
# Update DogecoinArcade to the latest published code (shim {version}).
# Touches only the code: never the identity key, messages or chain data.
# The import test comes first because the updater lives inside the package it
# updates: with the package gone there is nothing to run, and the command died
# with ModuleNotFoundError instead of repairing what was wrong.
if ! "{venv}/bin/python" -c "import arcade.update" 2>/dev/null; then
    echo "DogecoinArcade is missing from its environment; repairing it first."
    "{venv}/bin/python" "{repair}" || exit 1
fi
exec "{venv}/bin/python" -m arcade.update "$@"
"""

BOT_RPC = """\
#!/bin/sh
# Call the running DogecoinArcade's bot RPC from a script (shim {version}).
# Authenticates with the cookie the interface writes; never carries a password.
if [ ! -x "{venv}/bin/arcade-rpc" ]; then
    echo "DogecoinArcade is missing from its environment; repairing it first."
    "{venv}/bin/python" "{repair}" || exit 1
fi
exec "{venv}/bin/arcade-rpc" "$@"
"""

#: Windows has no `exec`, and `||` inside a parenthesised block is a trap, so
#: each of these is the same shape written the long way round.
WIN_LAUNCHER = """\
@echo off
@rem DogecoinArcade launcher (shim {version}), written by the installer.
if exist "{venv}\\Scripts\\arcade-web.exe" goto run
echo DogecoinArcade is missing from its environment; repairing it first.
"{venv}\\Scripts\\python.exe" "{repair}"
if errorlevel 1 exit /b 1
:run
"{venv}\\Scripts\\arcade-web.exe" %*
"""

WIN_UPDATER = """\
@echo off
@rem Update DogecoinArcade to the latest published code (shim {version}).
@rem Touches only the code: never the identity key, messages or chain data.
"{venv}\\Scripts\\python.exe" -c "import arcade.update" >NUL 2>&1
if not errorlevel 1 goto run
echo DogecoinArcade is missing from its environment; repairing it first.
"{venv}\\Scripts\\python.exe" "{repair}"
if errorlevel 1 exit /b 1
:run
"{venv}\\Scripts\\python.exe" -m arcade.update %*
"""

WIN_BOT_RPC = """\
@echo off
@rem Call the running DogecoinArcade's bot RPC from a script (shim {version}).
if exist "{venv}\\Scripts\\arcade-rpc.exe" goto run
echo DogecoinArcade is missing from its environment; repairing it first.
"{venv}\\Scripts\\python.exe" "{repair}"
if errorlevel 1 exit /b 1
:run
"{venv}\\Scripts\\arcade-rpc.exe" %*
"""

#: Written beside the environment rather than inside it, and importing nothing
#: from the application, because it is what runs when the application will not
#: import. Filled in with @@ markers rather than str.format: the body is Python,
#: and Python is full of braces.
REPAIR = '''\
#!/usr/bin/env python3
"""Put DogecoinArcade back into its own environment.

Written by the installer. A virtual environment can end up without the
application in it -- an update that fails between pip removing the old version
and unpacking the new one leaves exactly that -- and from there every command
dies with ModuleNotFoundError, including the updater, which lives inside the
package it would have repaired. This file is outside it, so it still runs.
"""
import subprocess
import sys
from pathlib import Path

VENV = Path(r"@@VENV@@")
PYTHON = Path(r"@@PYTHON@@")
SOURCE = Path(r"@@SOURCE@@")
SITE = "@@SITE@@"


def main() -> int:
    if not PYTHON.exists():
        print(f"The environment at {VENV} has no Python left in it.")
        print(f"Install DogecoinArcade again from {SITE}")
        return 1
    if not (SOURCE / "pyproject.toml").is_file():
        print(f"There is no source to install from at {SOURCE}.")
        print(f"Install DogecoinArcade again from {SITE}")
        return 1

    # flush: pip writes straight to the terminal, so an unflushed line here
    # arrives after everything pip said and reads as a report of its output.
    print(f"Installing DogecoinArcade from {SOURCE}", flush=True)
    # `python -m pip`, not the pip executable: on Windows pip.exe cannot always
    # replace itself, and on a half-removed installation it may not be there.
    if subprocess.run([str(PYTHON), "-m", "pip", "install", "--upgrade",
                       f"{SOURCE}[web]"]).returncode != 0:
        print()
        print("That did not work; the output above says why.")
        print(f"Install DogecoinArcade again from {SITE}")
        return 1

    if subprocess.run([str(PYTHON), "-c", "import arcade"],
                      capture_output=True).returncode != 0:
        print("pip finished, but the application still does not import.")
        print(f"Install DogecoinArcade again from {SITE}")
        return 1

    print("Repaired. Your wallet, messages and chain data were not touched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def repair_source(venv: Path) -> Path:
    """What `repair.py` reinstalls from: the fetched source, or the checkout.

    The installer's own layout is ~/.dogecoinarcade/{venv,src}. A developer
    machine instead has its environment inside the checkout, and there the
    source to reinstall is the checkout itself.
    """
    venv = Path(venv)
    if (venv.parent / "pyproject.toml").is_file():
        return venv.parent
    return venv.parent / "src"


def repair_path(venv: Path) -> Path:
    """Where `write_repair` puts the repair script: beside the environment.

    Except when the environment lives inside a checkout, where "beside" is
    somebody's repository -- the first run of this left an untracked repair.py
    in the working tree, on its way to being committed and published. The
    application's own directory is the one place that is always ours.
    """
    venv = Path(venv)
    if (venv.parent / "pyproject.toml").is_file():
        return Path.home() / ".dogecoinarcade" / "repair.py"
    return venv.parent / "repair.py"


def write_repair(venv: Path, system: str | None = None) -> Path:
    """Write the standalone repair script.

    Written as soon as the environment exists, before anything that can fail:
    the whole point of it is to be there when the install did not finish.
    """
    venv = Path(venv)
    windows = (system or platform.system()) == "Windows"
    python = venv / ("Scripts/python.exe" if windows else "bin/python")
    path = repair_path(venv)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        REPAIR.replace("@@VENV@@", str(venv))
              .replace("@@PYTHON@@", str(python))
              .replace("@@SOURCE@@", str(repair_source(venv)))
              .replace("@@SITE@@", "https://dogecoinarcade.com")
    )
    return path


#: Every command the installer puts on the PATH, in the order it writes them.
#: The updater checks each of these, so a machine installed before one of them
#: existed gets it at its next update rather than `command not found`.
SHIMS = ("dogecoinarcade", "dogecoinarcade-update", "arcade-rpc")


def shim_names(system: str) -> list[str]:
    """The file names `write_launcher` leaves in the bin directory on `system`."""
    suffix = ".cmd" if system == "Windows" else ""
    return [name + suffix for name in SHIMS]


def write_launcher(venv: Path, target: Path, system: str) -> Path:
    """Write every command shim, and the repair script they fall back on."""
    target.mkdir(parents=True, exist_ok=True)
    repair = write_repair(venv, system)
    if system == "Windows":
        shims = [("dogecoinarcade.cmd", WIN_LAUNCHER),
                 ("dogecoinarcade-update.cmd", WIN_UPDATER),
                 ("arcade-rpc.cmd", WIN_BOT_RPC)]
    else:
        shims = [("dogecoinarcade", LAUNCHER),
                 ("dogecoinarcade-update", UPDATER),
                 ("arcade-rpc", BOT_RPC)]
    written = []
    for name, template in shims:
        text = template.format(venv=venv, repair=repair, version=SHIM_VERSION)
        path = target / name
        if system == "Windows":
            # newline="" so the CRs written here are the only ones: text mode on
            # Windows would translate the \n as well and leave \r\r\n.
            path.write_text(text.replace("\n", "\r\n"), newline="")
        else:
            path.write_text(text)
            path.chmod(0o755)
        written.append(path)
    for path in written[1:]:
        info(f"wrote {path}")
    info(f"wrote launcher {written[0]}")
    return written[0]


#: Reads the *user* Path out of the registry and writes it back, rather than
#: `setx`, which truncates a PATH longer than 1024 characters and has eaten
#: people's environments for years.
ADD_TO_PATH = """\
$dir = '{target}'
$cur = [Environment]::GetEnvironmentVariable('Path','User')
if (-not $cur) {{ $cur = '' }}
$parts = @($cur -split ';' | Where-Object {{ $_ -ne '' }})
if ($parts -contains $dir) {{ Write-Output 'already' }}
else {{
  [Environment]::SetEnvironmentVariable('Path', (($parts + $dir) -join ';'), 'User')
  Write-Output 'added'
}}
"""


def add_to_user_path(target: Path, system: str) -> str | None:
    """Put the command directory on the PATH. Windows only; returns what it did.

    Linux and macOS install into ~/.local/bin, which is already on the PATH of
    every shell that matters. Windows has nothing equivalent: the commands went
    into %LOCALAPPDATA%\\DogecoinArcade\\bin, which no terminal looks in, so the
    installer finished by printing a note and every command it had just written
    was "not recognized".
    """
    if system != "Windows":
        return None
    script = ADD_TO_PATH.format(target=str(target).replace("'", "''"))
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        info(f"could not add {target} to your PATH: {exc}")
        return None
    done = result.stdout.strip().splitlines()[-1:] or [""]
    if done[0] not in ("added", "already"):
        info(f"could not add {target} to your PATH: "
             f"{result.stderr.strip()[-200:] or 'no answer from powershell'}")
        return None
    return done[0]


def shims_current(target: Path, system: str) -> bool:
    """Is every shim in `target` present and written by this version?

    The updater asks before rewriting. Existence alone was the old test, and it
    meant a corrected shim never reached a machine that already had one by that
    name -- which is every machine that has ever run an update.
    """
    target = Path(target)
    marker = f"(shim {SHIM_VERSION})"
    for name in shim_names(system):
        path = target / name
        if not path.is_file():
            return False
        try:
            if marker not in path.read_text(errors="replace"):
                return False
        except OSError:
            return False
    return True


# --- update -------------------------------------------------------------------


def ensure_web_service(venv: Path) -> bool:
    """Register the web interface as a service if it is not one already.

    Idempotent, and deliberately runs on update as well as install: an
    installation made before the unit existed has nothing for an update to
    restart, so it keeps serving the old code with no sign but the version in the
    footer. Returns True if it registered one.
    """
    if not shutil.which("systemctl"):
        return False
    unit = Path.home() / ".config/systemd/user" / "arcade-web.service"
    if unit.exists():
        return False
    # A SYSTEM unit counts as already registered. Checking only the user path
    # meant a machine with a perfectly good /etc/systemd/system/arcade-web
    # .service got a second, user-level copy -- which could never bind, because
    # the system one already owned :8420. So it sat in a permanent restart loop
    # while the system service carried on serving code from hours earlier, and
    # every update restarted the user unit and reported success. The interface's
    # own staleness banner was the only thing telling the truth.
    if SYSTEM_WEB_UNIT.exists():
        return False
    return bool(install_web_service(venv, platform.system()))


def migrate_node_units(dry_run: bool = False) -> list[str]:
    """Bring already-installed node units up to `Restart=always`.

    Units written before this change say `Restart=on-failure`, which means a
    clean shutdown is final. That breaks restoring a wallet: the application
    stops the node through its own `stop` RPC -- a clean exit, needing no
    privileges -- and relies on systemd to bring it back.

    Both layouts are checked. The installer writes *user* units under
    ~/.config/systemd/user, but a node set up by hand may be a system unit under
    /etc/systemd/system, and editing that needs privileges this may not have.
    Reported honestly rather than attempted and half-done. Found on a test machine,
    where the published fix named a path that does not exist on a normal install.
    """
    if not shutil.which("systemctl"):
        return []

    changed: list[str] = []
    scopes = [
        (Path.home() / ".config/systemd/user", ["--user"]),
        (Path("/etc/systemd/system"), []),
    ]
    for unit_dir, scope in scopes:
        if not unit_dir.is_dir():
            continue
        reload_needed = False
        for unit in sorted(unit_dir.glob("*coind*.service")):
            try:
                text = unit.read_text()
            except OSError:
                continue
            if "Restart=on-failure" not in text:
                continue
            if dry_run:
                info(f"would set Restart=always in {unit}")
                changed.append(str(unit))
                continue
            updated = text.replace("Restart=on-failure", "Restart=always")
            updated = updated.replace("RestartSec=30", "RestartSec=5")
            try:
                unit.write_text(updated)
            except PermissionError:
                warn(f"{unit} needs Restart=always but is not writable by you.")
                warn(f"  sudo sed -i 's/^Restart=on-failure$/Restart=always/' {unit}")
                warn(f"  sudo systemctl daemon-reload")
                continue
            changed.append(str(unit))
            reload_needed = True
        if reload_needed and not dry_run:
            subprocess.run(["systemctl", *scope, "daemon-reload"], check=False)
    return changed


def do_update(dry_run: bool) -> int:
    """Fetch the latest code and reinstall, leaving data and keys alone.

    Deliberately touches nothing but the code: not the identity key, not the
    message database, not the chain data, not the node configuration. An update
    that could lose someone's key would be worse than no update command.
    """
    home = Path.home() / ".dogecoinarcade"
    venv = home / "venv"
    checkout = home / "src"

    if not venv.exists():
        fail(f"no installation found at {venv}. Run the installer first.")

    git = shutil.which("git")
    if not git:
        fail("git is required to update. Install it, or re-run the installer.")

    step(1, 4, "Fetching the latest code")
    if dry_run:
        info(f"would fetch {REPO_URL} into {checkout}")
    elif checkout.exists():
        before = subprocess.run([git, "-C", str(checkout), "rev-parse", "--short", "HEAD"],
                                capture_output=True, text=True).stdout.strip()
        result = subprocess.run([git, "-C", str(checkout), "pull", "--ff-only", "--quiet"],
                                capture_output=True, text=True)
        if result.returncode != 0:
            fail(f"could not update the checkout: {result.stderr.strip()}")
        after = subprocess.run([git, "-C", str(checkout), "rev-parse", "--short", "HEAD"],
                               capture_output=True, text=True).stdout.strip()
        if before == after:
            info(f"already up to date ({after})")
        else:
            info(f"{before} -> {after}")
    else:
        checkout.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run([git, "clone", "--quiet", REPO_URL, str(checkout)],
                                capture_output=True, text=True)
        if result.returncode != 0:
            fail(f"could not clone {REPO_URL}: {result.stderr.strip()}")
        info(f"cloned into {checkout}")

    step(2, 4, "Reinstalling the application")
    if dry_run:
        info("would pip install the new code into the existing environment")
    else:
        pip = venv / ("Scripts/pip.exe" if os.name == "nt" else "bin/pip")
        result = subprocess.run(
            [str(pip), "install", "-q", "--upgrade", f"{checkout}[web]"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            fail(f"reinstall failed:\n{result.stderr[-1200:]}")
        info("installed")

    step(3, 4, "Bringing services up to date")
    # Register the web interface if it is not a service yet. Without this an
    # existing installation has nothing to restart, so every update leaves the
    # OLD code running and the interface silently serves the previous release --
    # it cost four rounds of confusion on one machine in a day, and produced two
    # false diagnoses: a phantom progress bar and a button that posted into a
    # void, both this one cause wearing different clothes.
    if not dry_run:
        registered = ensure_web_service(venv)
        if registered:
            info("registered arcade-web, so updates can restart it")
    else:
        info("would register arcade-web if it is not a service")

    migrated = migrate_node_units(dry_run)
    if migrated:
        for unit in migrated:
            info(f"Restart=always: {Path(unit).name}")
    else:
        info("node services already correct")

    step(4, 4, "Restarting the interface")
    if dry_run:
        info("would restart arcade-web if it is running as a service")
    elif shutil.which("systemctl"):
        for scope in (["--user"], []):
            result = subprocess.run(["systemctl", *scope, "restart", "arcade-web"],
                                    capture_output=True, text=True)
            if result.returncode == 0:
                info(f"restarted arcade-web ({'user' if scope else 'system'} service)")
                break
        else:
            info("arcade-web is not a service here; restart it yourself")
    else:
        info("restart the interface yourself to pick up the new version")

    print()
    print("Updated. Your identity key, messages and chain data were not touched.")
    return 0


# --- main ---------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install DogecoinArcade.")
    parser.add_argument("--dry-run", action="store_true", help="show what would happen")
    parser.add_argument("--update", action="store_true",
                        help="update an existing installation to the latest code")
    parser.add_argument("--skip-core", action="store_true",
                        help="skip Pepecoin Core; install only the application")
    parser.add_argument("--force-core", action="store_true",
                        help="install our pinned Core even if one is already "
                             "installed or running")
    parser.add_argument("--no-services", action="store_true", help="do not register services")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    parser.add_argument("--no-bootstrap", action="store_true",
                        help="build the index from the chain instead of the latest bootstrap")
    parser.add_argument(
        "--coin", choices=["pepecoin", "dogecoin", "both"], default=None,
        help="which chain to install a node for. Omit it and the installer asks. "
             "The protocol is identical on both, so 'both' installs two nodes.",
    )
    args = parser.parse_args(argv)

    if args.update:
        print("DogecoinArcade updater")
        print("=" * 58)
        try:
            return do_update(args.dry_run)
        except InstallError as exc:
            print(f"\nERROR: {exc}\n", file=sys.stderr)
            return 1

    print("DogecoinArcade installer")
    print("=" * 58)

    # Ask, unless told on the command line or running somewhere nobody can answer
    # (a pipe, a CI job), where the historical default stands.
    if args.coin:
        coins = list(COINS.values()) if args.coin == "both" else [COINS[args.coin]]
    elif args.skip_core or not sys.stdin.isatty():
        coins = [PEPECOIN]
    else:
        coins = choose_chain()
    per_coin = 0 if args.skip_core else 2
    total = 4 + len(coins) * (per_coin + 2)
    n = 0

    try:
        n += 1
        step(n, total, "Checking this machine")
        system = platform.system()
        machine = platform.machine()
        target = bindir(system)
        info(f"{system} / {machine}")
        # Printed because it is the first thing worth knowing when an install
        # fails: a dependency with no wheel for this Python is the difference
        # between "pip installed it" and a page of compiler output.
        info(f"Python {'.'.join(str(n) for n in sys.version_info[:3])}")
        info(f"binaries  -> {target}")
        info(f"chains    -> {', '.join(c.name for c in coins)}")
        # 3.10 is the real floor: the code uses `X | None` annotations and
        # nothing newer. The previous 3.12 requirement was an arbitrary choice
        # that blocked a working machine for no reason.
        if sys.version_info < (3, 10):
            fail(
                f"Python 3.10 or newer is required; this is {sys.version.split()[0]}.\n"
                "      Try a newer interpreter if one is installed, for example:\n"
                "        python3.11 install.py\n"
                "        python3.12 install.py"
            )
        if sys.version_info < (3, 12):
            info(f"Python {sys.version.split()[0]} -- supported, though 3.12 is what "
                 f"gets tested")

        for coin in coins:
            system, machine, asset = detect(coin)
            main_dir, test_dir = datadirs(system, coin)

            # Somebody who has run a node for years is exactly the person this
            # application is for, and the installer used to download 30 MB and
            # write over their binary without looking. Look first.
            present = existing_core(coin, target, system)
            running = running_core(coin)
            keep = bool(present) and not args.force_core

            if not args.skip_core:
                n += 1
                step(n, total, f"Downloading {coin.name} Core {coin.version}")
                archive = None
                if keep:
                    where, version = present
                    info(f"{coin.name} Core is already installed: {where}"
                         + (f" ({version})" if version else ""))
                    if running:
                        info("and it is running, so it is left alone")
                    info("nothing to download. --force-core installs ours anyway.")
                elif args.dry_run:
                    info(f"would download {coin.base_url}/{asset}")
                else:
                    with tempfile.TemporaryDirectory(prefix="arcade-install-") as tmp:
                        archive = fetch_core(Path(tmp), asset, coin)
                        n += 1
                        step(n, total, f"Installing {coin.name} Core")
                        install_core(archive, target, system, coin)
                        for unit in restart_running_nodes(coin):
                            info(f"restarted {unit} to pick up the new binary")
                        archive = "done"

                if archive != "done":
                    n += 1
                    step(n, total, f"Installing {coin.name} Core")
                    if keep:
                        info(f"using the {coin.name} Core already on this machine")
                    else:
                        info(f"would install {', '.join(coin.binaries)} to {target}")

            n += 1
            step(n, total, f"Writing {coin.name} configuration")
            if args.dry_run:
                info(f"would write {main_dir}/{coin.key}.conf and {test_dir}/{coin.key}.conf")
            else:
                write_configs(main_dir, test_dir, coin)

            n += 1
            step(n, total, f"Registering {coin.name} services")
            if args.dry_run or args.no_services:
                info("skipped")
            elif running:
                # Two daemons on one datadir do not share it: the second dies
                # with "Cannot obtain a lock on data directory". Whoever started
                # the one that is up already has a way of starting it.
                info(f"{coin.binaries[0]} is already running; leaving it to "
                     "whatever starts it")
                info("  --force-core registers ours as well, if you want that")
            else:
                install_services(system, target, main_dir, test_dir, coin)

        n += 1
        step(n, total, "Installing the application")
        venv = install_app(args.dry_run)

        n += 1
        step(n, total, "Fetching the latest bootstrap")
        if args.no_bootstrap:
            info("skipped (--no-bootstrap)")
        else:
            fetch_bootstraps(args.dry_run)

        n += 1
        step(n, total, "Finishing up")
        if not args.dry_run and not args.no_services:
            install_web_service(venv, system)
        if not args.dry_run:
            launcher = write_launcher(venv, target, system)
            on_path = add_to_user_path(target, system)
            if on_path == "added":
                info(f"added {target} to your PATH -- open a new terminal for it")
        else:
            launcher = target / "dogecoinarcade"
            on_path = None

        print()
        print("=" * 58)
        print("Done.")
        print()
        print("  Start it with:   dogecoinarcade")
        print("  Then open:       http://127.0.0.1:8420")
        print()
        if str(target) not in os.environ.get("PATH", ""):
            if on_path == "added":
                # It is on the PATH of every terminal opened from now on. This
                # one inherited its copy when it started and cannot see it.
                print(f"  NOTE: {target} was added to your PATH.")
                print("        Open a new terminal before running the commands.")
            else:
                print(f"  NOTE: {target} is not on your PATH. Either add it, or run")
                print(f"        {launcher}")
            print()
        print("  The nodes will take a while to sync before anything works.")
        print("  Testnet is small; mainnet is larger. The application shows progress.")
        print()

        if not args.dry_run and not args.no_browser:
            try:
                import webbrowser
                webbrowser.open("http://127.0.0.1:8420")
            except Exception:
                pass

    except InstallError as exc:
        print(f"\nERROR: {exc}\n", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

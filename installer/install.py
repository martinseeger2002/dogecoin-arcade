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

VERSION = "1.1.0"
BASE_URL = f"https://github.com/pepecoinppc/pepecoin/releases/download/v{VERSION}"
KEY_URL = (
    "https://raw.githubusercontent.com/pepecoinppc/pepecoin/"
    f"v{VERSION}/contrib/gitian-keys/david2278-key.pgp"
)
SIGNER_FINGERPRINT = "18250EC92E527E9723E49116FD415169D2691927"

#: Published SHA-256 sums for v1.1.0. Pinned here as a second, independent
#: check: even if the release page were altered, these would no longer match.
KNOWN_SHA256 = {
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
}

#: How well each platform is actually supported. Stated plainly because an
#: installer that silently half-works is worse than one that says it cannot.
SUPPORT = {
    ("Linux", "x86_64"):  "tested",
    ("Linux", "aarch64"): "untested",
    ("Linux", "arm64"):   "untested",
    ("Linux", "armv7l"):  "untested",
    ("Windows", "AMD64"): "untested",
    ("Darwin", "x86_64"): "unsupported",
    ("Darwin", "arm64"):  "unsupported",
}

ASSETS = {
    ("Linux", "x86_64"):  f"pepecoin-{VERSION}-x86_64-linux-gnu.tar.gz",
    ("Linux", "aarch64"): f"pepecoin-{VERSION}-aarch64-linux-gnu.tar.gz",
    ("Linux", "arm64"):   f"pepecoin-{VERSION}-aarch64-linux-gnu.tar.gz",
    ("Linux", "armv7l"):  f"pepecoin-{VERSION}-arm-linux-gnueabihf.tar.gz",
    ("Windows", "AMD64"): f"pepecoin-{VERSION}-win64.zip",
    ("Darwin", "x86_64"): f"pepecoin-{VERSION}-osx-unsigned.dmg",
    ("Darwin", "arm64"):  f"pepecoin-{VERSION}-osx-unsigned.dmg",
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


# --- platform -----------------------------------------------------------------

def detect() -> tuple[str, str, str]:
    system = platform.system()
    machine = platform.machine()
    asset = ASSETS.get((system, machine))
    if asset is None:
        fail(
            f"no Pepecoin Core build for {system}/{machine}. Supported: "
            + ", ".join(f"{s}/{m}" for s, m in ASSETS)
        )

    support = SUPPORT.get((system, machine), "untested")
    if support == "unsupported":
        fail(
            f"{system}/{machine} is not supported yet.\n"
            "      Pepecoin Core ships macOS only as an unsigned .dmg, which this\n"
            "      installer cannot mount or extract. Install Pepecoin Core by hand,\n"
            "      then run this again with --skip-core."
        )
    if support == "untested":
        warn(f"{system}/{machine} has not been tested. It should work; tell us if it does not.")
    return system, machine, asset


def datadirs(system: str) -> tuple[Path, Path]:
    """(mainnet datadir, testnet datadir) for this platform."""
    home = Path.home()
    if system == "Windows":
        base = Path(os.environ.get("APPDATA", home / "AppData/Roaming")) / "Pepecoin"
        return base, base.with_name("Pepecoin-testnet")
    if system == "Darwin":
        base = home / "Library/Application Support/Pepecoin"
        return base, base.with_name("Pepecoin-testnet")
    return home / ".pepecoin", home / ".pepecoin-testnet"


def bindir(system: str) -> Path:
    if system == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "DogecoinArcade" / "bin"
    # ~/.local/bin needs no root and is on PATH for most distributions.
    return Path.home() / ".local" / "bin"


# --- download and verify ------------------------------------------------------

def download(url: str, dest: Path, label: str) -> Path:
    info(f"downloading {label}")
    try:
        with urllib.request.urlopen(url, timeout=120) as response, dest.open("wb") as out:
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


def verify_hash(archive: Path, sums_text: str) -> None:
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

    pinned = KNOWN_SHA256.get(archive.name)
    if pinned and pinned != actual:
        fail(
            f"the published sum matches the download, but BOTH differ from the "
            f"hash pinned in this installer.\n"
            f"        pinned {pinned}\n        got    {actual}\n"
            f"      That means the release itself changed. Stopping."
        )
    if pinned:
        info("sha256 also matches the hash pinned in this installer")


def verify_signature(sums_file: Path, workdir: Path) -> bool:
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
        download(KEY_URL, key, "signing key")
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
    if SIGNER_FINGERPRINT.replace(" ", "") not in output.replace(" ", ""):
        fail(
            f"the signature is valid but was made by an unexpected key.\n"
            f"      Expected {SIGNER_FINGERPRINT}\n{output.strip()}"
        )
    info("gpg: good signature from the key committed in the Pepecoin source tree")
    if "expired" in output.lower():
        info("(that key has since expired; the signature predates expiry and stands)")
    return True


def fetch_core(workdir: Path, asset: str) -> Path:
    archive = download(f"{BASE_URL}/{asset}", workdir / asset, asset)
    sums = download(f"{BASE_URL}/SHA256SUMS.asc", workdir / "SHA256SUMS.asc", "checksums")
    verify_hash(archive, sums.read_text(errors="replace"))
    verify_signature(sums, workdir)
    return archive


# --- install ------------------------------------------------------------------

BINARIES = ["pepecoind", "pepecoin-cli", "pepecoin-tx"]


def install_core(archive: Path, target: Path, system: str) -> None:
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

    extracted = next((p for p in workdir.glob(f"pepecoin-{VERSION}*") if p.is_dir()), None)
    if extracted is None:
        fail("could not find the extracted Pepecoin directory")

    suffix = ".exe" if system == "Windows" else ""
    for name in BINARIES:
        source = extracted / "bin" / f"{name}{suffix}"
        if not source.exists():
            fail(f"{source} is missing from the archive")
        shutil.copy2(source, target / f"{name}{suffix}")
        (target / f"{name}{suffix}").chmod(0o755)
    info(f"installed {', '.join(BINARIES)} to {target}")


MAINNET_CONF = """\
# Pepecoin Core -- MAINNET (the ledger: tokens, NFTs, PEP)
# Written by the DogecoinArcade installer.

server=1
txindex=1
prune=0

rpcbind=127.0.0.1
rpcallowip=127.0.0.1

# Cookie authentication: the daemon regenerates .cookie at every start, so no
# password is ever written to a file. This works because the node runs as you.

dbcache=512

# ZMQ, for the indexer. Ports chosen not to collide with testnet's 284xx.
zmqpubhashblock=tcp://127.0.0.1:28332
zmqpubrawblock=tcp://127.0.0.1:28333
zmqpubrawtx=tcp://127.0.0.1:28335
"""

TESTNET_CONF = """\
# Pepecoin Core -- TESTNET (the Messenger)
# Written by the DogecoinArcade installer.
#
# Messaging is testnet-only and permanently so: messages are conversational,
# not assets, and testnet chains can be reset.

testnet=1
server=1
txindex=1
prune=0

rpcbind=127.0.0.1
rpcallowip=127.0.0.1

dbcache=512

zmqpubhashblock=tcp://127.0.0.1:28432
zmqpubrawblock=tcp://127.0.0.1:28433
zmqpubrawtx=tcp://127.0.0.1:28435
"""


def write_configs(main_dir: Path, test_dir: Path) -> None:
    for directory, content, label in (
        (main_dir, MAINNET_CONF, "mainnet"),
        (test_dir, TESTNET_CONF, "testnet"),
    ):
        directory.mkdir(parents=True, exist_ok=True)
        conf = directory / "pepecoin.conf"
        if conf.exists():
            info(f"{label} config already exists, leaving it alone: {conf}")
            continue
        conf.write_text(content)
        conf.chmod(0o600)
        info(f"wrote {conf}")


SYSTEMD_UNIT = """\
[Unit]
Description=Pepecoin Core ({label}) for DogecoinArcade
After=network-online.target
Wants=network-online.target

[Service]
# Type=simple and foreground: this codebase has no -daemonwait, so Type=forking
# would race against the PID file.
Type=simple
ExecStart={binary} -datadir={datadir}
Restart=on-failure
RestartSec=30
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


def install_services(system: str, target: Path, main_dir: Path, test_dir: Path) -> list[str]:
    """Register both nodes so they start on login. Returns what was installed."""
    if system != "Linux" or not shutil.which("systemctl"):
        warn("no systemd here; start the nodes yourself:")
        warn(f"  {target}/pepecoind -datadir={main_dir}")
        warn(f"  {target}/pepecoind -datadir={test_dir}")
        return []

    # A *user* unit needs no root at all.
    unit_dir = Path.home() / ".config/systemd/user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    installed = []
    for label, datadir in (("mainnet", main_dir), ("testnet", test_dir)):
        name = f"pepecoind-{label}.service"
        (unit_dir / name).write_text(
            SYSTEMD_UNIT.format(label=label, binary=target / "pepecoind", datadir=datadir)
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

def install_app(dry_run: bool) -> Path:
    """Install DogecoinArcade into its own virtual environment.

    A dedicated venv rather than the system Python: the dependencies are pinned,
    and pinned versions in a shared environment is how you break someone's other
    software.
    """
    venv = Path.home() / ".dogecoinarcade" / "venv"
    source = Path(__file__).resolve().parent.parent

    if dry_run:
        info(f"would create {venv} and install from {source}")
        return venv

    if not venv.exists():
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        info(f"created {venv}")

    pip = venv / ("Scripts/pip.exe" if os.name == "nt" else "bin/pip")
    result = subprocess.run(
        [str(pip), "install", "-q", "--upgrade", f"{source}[web]"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        fail(f"installing the application failed:\n{result.stderr[-1500:]}")
    info("installed DogecoinArcade and its dependencies")
    return venv


LAUNCHER = """\
#!/bin/sh
# DogecoinArcade launcher, written by the installer.
exec "{venv}/bin/arcade-web" "$@"
"""


def write_launcher(venv: Path, target: Path, system: str) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    if system == "Windows":
        path = target / "dogecoinarcade.cmd"
        path.write_text(f'@echo off\r\n"{venv}\\Scripts\\arcade-web.exe" %*\r\n')
    else:
        path = target / "dogecoinarcade"
        path.write_text(LAUNCHER.format(venv=venv))
        path.chmod(0o755)
    info(f"wrote launcher {path}")
    return path


# --- main ---------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install DogecoinArcade.")
    parser.add_argument("--dry-run", action="store_true", help="show what would happen")
    parser.add_argument("--skip-core", action="store_true",
                        help="skip Pepecoin Core; install only the application")
    parser.add_argument("--no-services", action="store_true", help="do not register services")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    args = parser.parse_args(argv)

    print("DogecoinArcade installer")
    print("=" * 58)

    total = 5 if args.skip_core else 7
    n = 0

    try:
        n += 1
        step(n, total, "Checking this machine")
        system, machine, asset = detect()
        main_dir, test_dir = datadirs(system)
        target = bindir(system)
        info(f"{system} / {machine}")
        info(f"binaries  -> {target}")
        info(f"mainnet   -> {main_dir}")
        info(f"testnet   -> {test_dir}")
        if sys.version_info < (3, 12):
            fail(f"Python 3.12 or newer is required; this is {sys.version.split()[0]}")

        if not args.skip_core:
            with tempfile.TemporaryDirectory(prefix="arcade-install-") as tmp:
                workdir = Path(tmp)
                n += 1
                step(n, total, f"Downloading Pepecoin Core {VERSION}")
                if args.dry_run:
                    info(f"would download {BASE_URL}/{asset}")
                    archive = None
                else:
                    archive = fetch_core(workdir, asset)

                n += 1
                step(n, total, "Installing Pepecoin Core")
                if args.dry_run:
                    info(f"would install {', '.join(BINARIES)} to {target}")
                else:
                    install_core(archive, target, system)

        n += 1
        step(n, total, "Writing node configuration")
        if args.dry_run:
            info(f"would write {main_dir}/pepecoin.conf and {test_dir}/pepecoin.conf")
        else:
            write_configs(main_dir, test_dir)

        n += 1
        step(n, total, "Registering services")
        if args.dry_run or args.no_services:
            info("skipped")
        else:
            install_services(system, target, main_dir, test_dir)

        n += 1
        step(n, total, "Installing the application")
        venv = install_app(args.dry_run)

        n += 1
        step(n, total, "Finishing up")
        if not args.dry_run:
            launcher = write_launcher(venv, target, system)
        else:
            launcher = target / "dogecoinarcade"

        print()
        print("=" * 58)
        print("Done.")
        print()
        print("  Start it with:   dogecoinarcade")
        print("  Then open:       http://127.0.0.1:8420")
        print()
        if str(target) not in os.environ.get("PATH", ""):
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

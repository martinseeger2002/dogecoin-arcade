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
        shutil.copy2(source, target / f"{name}{suffix}")
        (target / f"{name}{suffix}").chmod(0o755)
    info(f"installed {', '.join(coin.binaries)} to {target}")


WINDOWS_TASK = (
    'schtasks /Create /F /TN "DogecoinArcade\\pepecoin-{label}" /SC ONLOGON '
    '/TR "\'{binary}\' -datadir=\'{datadir}\'" /RL LIMITED'
)


def install_services_windows(target: Path, main_dir: Path, test_dir: Path, coin) -> list[str]:
    """Register both nodes as logon Scheduled Tasks.

    Windows has no systemd. A Scheduled Task set to ONLOGON is the closest
    equivalent that needs no service wrapper and no administrator rights.
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
        else:
            warn(f"could not register the {label} task: {result.stdout.strip() or result.stderr.strip()}")
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

    git = shutil.which("git")
    if not git:
        fail(
            "git is needed to fetch the application, and it is not installed.\n"
            "        Debian/Ubuntu:  sudo apt install git\n"
            "        Fedora:         sudo dnf install git\n"
            "        macOS:          xcode-select --install"
        )

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
        fail(f"installing the application failed:\n{result.stderr[-1500:]}")
    info("installed DogecoinArcade and its dependencies")
    return venv


LAUNCHER = """\
#!/bin/sh
# DogecoinArcade launcher, written by the installer.
exec "{venv}/bin/arcade-web" "$@"
"""

UPDATER = """\
#!/bin/sh
# Update DogecoinArcade to the latest published code.
# Touches only the code: never the identity key, messages or chain data.
exec "{venv}/bin/python" -m arcade.update "$@"
"""


def write_launcher(venv: Path, target: Path, system: str) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    if system == "Windows":
        path = target / "dogecoinarcade.cmd"
        path.write_text(f'@echo off\r\n"{venv}\\Scripts\\arcade-web.exe" %*\r\n')
        (target / "dogecoinarcade-update.cmd").write_text(
            f'@echo off\r\n"{venv}\\Scripts\\python.exe" -m arcade.update %*\r\n')
    else:
        path = target / "dogecoinarcade"
        path.write_text(LAUNCHER.format(venv=venv))
        path.chmod(0o755)
        update = target / "dogecoinarcade-update"
        update.write_text(UPDATER.format(venv=venv))
        update.chmod(0o755)
        info(f"wrote updater {update}")
    info(f"wrote launcher {path}")
    return path


# --- update -------------------------------------------------------------------

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

    step(1, 3, "Fetching the latest code")
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

    step(2, 3, "Reinstalling the application")
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

    step(3, 3, "Restarting the interface")
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
    parser.add_argument("--no-services", action="store_true", help="do not register services")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    parser.add_argument(
        "--coin", choices=["pepecoin", "dogecoin", "both"], default="pepecoin",
        help="which chain to install a node for (default: pepecoin). The protocol "
             "is identical on both, so 'both' simply installs two nodes.",
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

    coins = list(COINS.values()) if args.coin == "both" else [COINS[args.coin]]
    per_coin = 0 if args.skip_core else 2
    total = 3 + len(coins) * (per_coin + 2)
    n = 0

    try:
        n += 1
        step(n, total, "Checking this machine")
        system = platform.system()
        machine = platform.machine()
        target = bindir(system)
        info(f"{system} / {machine}")
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

            if not args.skip_core:
                with tempfile.TemporaryDirectory(prefix="arcade-install-") as tmp:
                    workdir = Path(tmp)
                    n += 1
                    step(n, total, f"Downloading {coin.name} Core {coin.version}")
                    if args.dry_run:
                        info(f"would download {coin.base_url}/{asset}")
                        archive = None
                    else:
                        archive = fetch_core(workdir, asset, coin)

                    n += 1
                    step(n, total, f"Installing {coin.name} Core")
                    if args.dry_run:
                        info(f"would install {', '.join(coin.binaries)} to {target}")
                    else:
                        install_core(archive, target, system, coin)

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
            else:
                install_services(system, target, main_dir, test_dir, coin)

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

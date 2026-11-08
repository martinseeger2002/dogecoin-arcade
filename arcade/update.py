"""Update an installed DogecoinArcade to the latest published code.

    dogecoinarcade-update            # fetch and reinstall
    dogecoinarcade-update --check    # only report whether an update exists
    dogecoinarcade-update --dry-run  # show what would happen

Touches the code and the service definitions, and nothing else. Not the wallet,
not the message database, not the chain data, not any node's configuration file.
An update that could lose someone's wallet would be worse than having no update
command at all, since the wallet is now the only thing they keep.

Service definitions are included because leaving them stale broke a feature
silently: units written before `Restart=always` cannot bring a node back after a
wallet restore, and nothing tells the user why. The migration itself lives in
`installer/install.py` and is called from the freshly pulled checkout, so there
is exactly one implementation rather than two that can drift -- they already did
drift once, and this command was the half that was missed.

The repository is served over plain HTTP from the site, so this needs no
account, no key and no forge -- just git.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from . import release as releaselib

REPO_URL = "https://dogecoinarcade.com/repo"
#: The archive the installer falls back to when there is no git (see
#: installer/install.py). An installation made that way has no .git, so both
#: "what is installed" and "what is published" have to be read another way.
REVISION_URL = "https://dogecoinarcade.com/source.rev"
ARCHIVE_URL = "https://dogecoinarcade.com/source.tar.gz"
MANIFEST_URL = "https://dogecoinarcade.com/" + releaselib.MANIFEST
REVISION_FILE = ".revision"
#: What was installed last, and when it was published. Kept outside the
#: checkout, because the checkout is replaced wholesale by an update.
INSTALLED_FILE = "installed.json"
HOME = Path.home() / ".dogecoinarcade"

#: The channel an operator picks in Admin -> Overview (2026-10-28, Team HQ: "admin toggle for selecting main live vs
#: main test branch deployment"). "live" is what every machine has always followed. "test" is published on every
#: push to main, and live is promoted from it once a day had no trouble, so a test node runs what live nodes will run
#: tomorrow. Both are signed with the one key pinned in arcade/release.py: a test node trusts the same signature a live
#: one does, only sooner. Kept in settings.json, which this command reads itself, because it runs as its own process.
CHANNEL_SETTING = "update_channel"
CHANNELS = ("live", "test")
#: Where the test channel is published, beside the live files: the published repository's `test` branch, and an
#: archive, its revision and its signed manifest named like live's with "-test".
TEST_BRANCH = "test"
TEST_ARCHIVE_URL = "https://dogecoinarcade.com/source-test.tar.gz"
TEST_REVISION_URL = "https://dogecoinarcade.com/source-test.rev"
TEST_MANIFEST_URL = "https://dogecoinarcade.com/source-test.manifest.json"
#: Why the last check follows another channel than the one chosen (the test channel not published yet), for Admin.
followed_note = ""


#: Cloudflare in front of the site refuses `Python-urllib/3.x` with a 403, so
#: every fetch names itself. Found by fetching the manifest that had just been
#: published: curl got it, the updater got 403, and an automatic update that
#: cannot read the manifest installs nothing -- silently, on every machine
#: (D-066).
USER_AGENT = "DogecoinArcade (+https://dogecoinarcade.com)"


def _request(url: str):
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT})


class UpdateError(Exception):
    """The update could not proceed."""


def _layout() -> tuple[Path, Path, bool]:
    """Find the environment and the checkout, and say whether code can be fetched.

    The installer's layout is ~/.dogecoinarcade/{venv,src}, and this command used
    to accept nothing else: on a machine set up from a source checkout it refused
    outright with "no installation found", while the application it was refusing
    to update was running on that very machine. The user typed the documented
    command on their own mini PC and got that.

    A source checkout has no code to fetch -- the code is already there, and on
    the machine the releases are published from it is AHEAD of what is published.
    Pulling would either fail for want of an upstream or, worse, try to rewind
    work. So it returns `fetchable=False` and the update does only the half that
    still applies: registering the service, migrating unit files and restarting.
    That restart is the part that was actually wanted.
    """
    if (HOME / "venv").exists():
        return HOME / "venv", HOME / "src", True

    checkout = Path(__file__).resolve().parent.parent
    if not (checkout / "pyproject.toml").is_file():
        raise UpdateError(
            f"no installation found at {HOME / 'venv'}, and {checkout} does not\n"
            "  look like a checkout either. This command updates an installation\n"
            "  made by install.py."
        )
    return Path(sys.prefix), checkout, False


def _git() -> str:
    git = _find_git()
    if not git:
        raise UpdateError("git is required to update. Install it and try again.")
    return git


def _find_git() -> str | None:
    """git, including where Windows put it after the terminal's PATH was set."""
    git = shutil.which("git")
    if git or os.name != "nt":
        return git
    for base in filter(None, (os.environ.get("ProgramFiles"),
                              os.environ.get("ProgramFiles(x86)"),
                              os.environ.get("LOCALAPPDATA"))):
        candidate = Path(base) / "Git" / "cmd" / "git.exe"
        if candidate.exists():
            return str(candidate)
    return None


#: What "installed" means after pip (2026-10-31, issue #51): `import arcade` alone passed with 176 of 396 files gone.
#: The modules the interface starts from, its templates, and no "~"-named folder pip left behind mid-replace.
WHOLE_PACKAGE = """
import pathlib, arcade, arcade.config, arcade.update, arcade.web.app
root = pathlib.Path(arcade.__file__).parent
assert (root / "web" / "templates" / "base.html").is_file(), "templates missing"
left = [str(p) for p in root.rglob("~*")]
assert not left, "pip left half-replaced folders: " + ", ".join(left[:5])
"""


def _serving(port: int = 8420) -> bool:
    import socket
    with socket.socket() as probe:
        probe.settimeout(0.4)
        try:
            probe.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True)


def chosen_channel() -> str:
    """The channel picked in Admin -> Overview; live when none was, or when the file cannot be read."""
    try:
        chosen = json.loads((HOME / "settings.json").read_text()).get(CHANNEL_SETTING)
    except Exception:                                    # noqa: BLE001 -- no settings yet: live
        return "live"
    return chosen if chosen in CHANNELS else "live"


def following() -> str:
    """The channel this check follows: the chosen one, or live while test is not published. A machine set to test
    before the publisher makes the test channel stays on live, and says so, rather than updating to nothing."""
    global followed_note
    followed_note = ""
    if chosen_channel() != "test":
        return "live"
    if _published_revision(TEST_REVISION_URL) is None:
        followed_note = "the test channel is not published yet: following live"
        return "live"
    return "test"


def current_revision(checkout: Path) -> str | None:
    if not (checkout / ".git").exists():
        # Fetched as an archive: the installer wrote down what it fetched,
        # because there is no repository here to ask.
        stamp = checkout / REVISION_FILE
        if stamp.is_file():
            return stamp.read_text().strip()[:7] or None
        return None
    result = _run(_git(), "rev-parse", "--short", "HEAD", cwd=checkout)
    return result.stdout.strip() or None


def remote_revision(channel: str | None = None) -> str | None:
    """The published HEAD of the channel followed, without fetching the whole repository."""
    channel = channel or following()
    if _find_git() is not None:
        ref = "HEAD" if channel == "live" else f"refs/heads/{TEST_BRANCH}"
        result = _run(_git(), "ls-remote", REPO_URL, ref)
        if result.returncode == 0:
            line = result.stdout.split()
            if line:
                return line[0][:7]
    return _published_revision(TEST_REVISION_URL if channel == "test" else REVISION_URL)


def _published_revision(url: str = REVISION_URL) -> str | None:
    """The revision named beside the source archive. No git needed."""
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(_request(url), timeout=30) as response:
            return response.read().decode("utf-8", "replace").strip()[:7] or None
    except (urllib.error.URLError, OSError):
        return None


def check() -> tuple[str | None, str | None, bool]:
    """Return (installed, published, update_available).

    The checkout comes from _layout, not from the installer's path: on a source
    install that path does not exist, so this reported "installed: unknown" and
    "an update is available" no matter how current the machine actually was.
    """
    try:
        _, checkout, _ = _layout()
    except UpdateError:
        checkout = HOME / "src"
    installed = current_revision(checkout)
    channel = following()
    published = remote_revision(channel)
    if installed is None or published is None:
        return installed, published, published is not None
    if published.startswith(installed[:7]):
        return installed, published, False
    return installed, published, not _ahead_of(checkout, published, channel)


def _ahead_of(checkout: Path, published: str, channel: str = "live") -> bool:
    """Whether what is installed is already newer than `published`: a machine moved from test back to live, whose
    test release live has not been promoted to yet. Waits for live to catch up instead of reinstalling (and
    restarting, dropping every player) at every check, or going back. A git checkout asks git whether the published
    commit is in its history; an archive install compares the signed release times."""
    if (checkout / ".git").exists():
        git = _find_git()
        if git is None:
            return False
        return _run(git, "merge-base", "--is-ancestor", published, "HEAD", cwd=checkout).returncode == 0
    if _installed_published() is None:
        return False
    try:
        _signed_manifest(channel)
    except UpdateError as exc:
        return "downgrade" in str(exc)     # signed, and older than what is installed: the refusal says so
    return False


def _resync(git: str, checkout: Path, branch: str = "master", to: str = "FETCH_HEAD") -> bool:
    """Follow the published history when it was rewritten rather than extended.

    The published copy is filtered before it goes out (private documents and
    personal details taken out of every commit), and when that filter changes
    the history does too, so a fast-forward is impossible. A checkout nobody
    has edited has nothing to lose by taking the published one as it is; a
    checkout with local edits is left alone and the refusal says why.
    """
    dirty = _run(git, "status", "--porcelain", "--untracked-files=no", cwd=checkout)
    if dirty.returncode != 0 or dirty.stdout.strip():
        return False
    if _run(git, "fetch", "--quiet", "origin", branch, cwd=checkout).returncode != 0:
        return False
    if to != "FETCH_HEAD" and not _has_commit(git, checkout, to):
        return False
    return _run(git, "reset", "--hard", "--quiet", to,
                cwd=checkout).returncode == 0


def _has_commit(git: str, checkout: Path, commit: str) -> bool:
    return _run(git, "cat-file", "-e", f"{commit}^{{commit}}", cwd=checkout).returncode == 0


def _signed_commit(signed: dict) -> str:
    """The commit a signed manifest names: the published HEAD, in full."""
    wanted = str(signed.get("revision") or "").strip().lower()
    if len(wanted) != 40 or any(c not in "0123456789abcdef" for c in wanted):
        raise UpdateError("the signed release names no commit. Nothing was installed.")
    return wanted


def update(dry_run: bool = False) -> int:
    venv, checkout, fetchable = _layout()

    if not fetchable:
        print(f"Source checkout at {checkout}")
        print("  Nothing to fetch: the code here is whatever you have checked out.")
        return _update_services(checkout, venv, dry_run)

    channel = following()
    print(f"Fetching the latest code ({channel} channel{'; ' + followed_note if followed_note else ''})")
    git = None if dry_run else _find_git()
    if dry_run:
        print(f"  would fetch {REPO_URL} ({channel}) into {checkout}")
    elif git is None or (checkout.exists() and not (checkout / ".git").exists()):
        # Installed from the archive, or installed with a git that has since
        # gone. Either way the archive is the way back to current, and it needs
        # nothing but a download.
        before = current_revision(checkout)
        signed = _signed_manifest(channel)
        test = channel == "test"
        args = (checkout, signed["sha256"], *((TEST_ARCHIVE_URL, TEST_REVISION_URL) if test else ()))
        if (checkout / "installer" / "install.py").is_file():
            after = _call_installer(checkout, "fetch_source_archive", *args)
        else:
            # The checkout lost its installer (a src left half-replaced; a ThinkPad, 2026-11-03: "could not fetch the
            # source archive" at every check, because the repair needs the installer and the installer lives in what
            # is being repaired). The signed archive carries it: fetched, held to the signed sha256, and its own
            # installer does the fetch.
            after = _with_archive_installer(TEST_ARCHIVE_URL if test else ARCHIVE_URL, signed["sha256"],
                                            "fetch_source_archive", *args)
        if after is None:
            raise UpdateError(
                f"could not fetch the source archive into {checkout}.\n"
                "  Run the installer again from https://dogecoinarcade.com to "
                "repair this installation.")
        if not after:                                  # in place, its revision file unread: the signed one
            after = str(signed.get("revision") or "")
            if after:
                (checkout / REVISION_FILE).write_text(after + "\n")
        after = str(after)[:7]
        print(f"  already up to date ({after})" if before == after
              else f"  {before or 'unknown'} -> {after}")
        _remember(signed)
    elif (checkout / ".git").exists():
        # Only the signed release's own commit (2026-11-08, security review): a git node took whatever the branch
        # held, unsigned, so whoever could change what the repository serves ran code here. The signed manifest
        # names the commit (`revision`, the published HEAD), as it names the archive's hash.
        signed = _read_manifest(channel)
        wanted = _signed_commit(signed)
        branch = TEST_BRANCH if channel == "test" else "master"
        before = current_revision(checkout)
        if _run(git, "merge-base", "--is-ancestor", wanted, "HEAD", cwd=checkout).returncode == 0:
            print(f"  already up to date ({before})")             # the signed release is here already
        else:
            # The branch, fast-forwarded to the signed commit (test: live's master is always in its history).
            result = _run(git, "fetch", "--quiet", "origin", branch, cwd=checkout)
            if result.returncode == 0 and not _has_commit(git, checkout, wanted):
                raise UpdateError(
                    f"the repository does not have the signed release ({wanted[:7]}) yet.\n"
                    "  Nothing was installed; the next check tries again.")
            if result.returncode == 0:
                result = _run(git, "merge", "--ff-only", "--quiet", wanted, cwd=checkout)
            if result.returncode != 0:
                # A rewritten history: followed to the signed commit only, and never to an older release than the
                # one installed (an old signed release replayed to put a known bug back).
                if not releaselib.is_newer(signed, _installed_published()):
                    raise UpdateError("the published release is older than the one installed here. Nothing was "
                                      "installed: that is what a downgrade attack looks like.")
                if _resync(git, checkout, branch, to=wanted):
                    result = subprocess.CompletedProcess([], 0)   # followed it
            if result.returncode != 0:
                raise UpdateError(
                    f"could not fast-forward the checkout:\n  {result.stderr.strip()}\n"
                    f"  If you have edited files in {checkout}, move them aside first."
                )
            after = current_revision(checkout)
            print(f"  already up to date ({after})" if before == after else f"  {before} -> {after}")
        _remember(signed)
    else:
        signed = _read_manifest(channel)
        wanted = _signed_commit(signed)
        checkout.parent.mkdir(parents=True, exist_ok=True)
        result = _run(git, "clone", "--quiet", REPO_URL, str(checkout))
        if result.returncode != 0:
            raise UpdateError(f"could not clone {REPO_URL}:\n  {result.stderr.strip()}")
        # The signed commit, not whatever the clone's branch held (2026-11-08); nothing is installed without it.
        if _run(git, "checkout", "--quiet", "--detach", wanted, cwd=checkout).returncode != 0:
            shutil.rmtree(checkout, ignore_errors=True)
            raise UpdateError(f"the repository does not have the signed release ({wanted[:7]}) yet. "
                              "Nothing was installed; try again in a few minutes.")
        _remember(signed)
        print(f"  cloned into {checkout} at the signed release {wanted[:7]}")

    print("Reinstalling")
    if dry_run:
        print("  would reinstall into the existing environment")
    else:
        # Windows will not let pip replace files a running arcade has open: it stops halfway and the package is left
        # broken (2026-10-31, issue #51). The code was fetched; it is installed once the arcade is stopped.
        if os.name == "nt" and _serving():
            raise UpdateError(
                "the arcade is still running (something answers on 127.0.0.1:8420).\n"
                "  Windows cannot replace files it has open: stop arcade-web (its window or scheduled task),\n"
                "  run dogecoinarcade-update again, then start the arcade.")
        pip = venv / ("Scripts/pip.exe" if os.name == "nt" else "bin/pip")
        result = _run(str(pip), "install", "-q", "--upgrade", f"{checkout}[web]")
        if result.returncode != 0:
            raise UpdateError(f"reinstall failed:\n{result.stderr[-1200:]}")
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        # pip removes the old version before unpacking the new one. Interrupt it
        # in between -- or have it fail there -- and the environment is left with
        # no application in it, which is how a machine reaches the state where
        # every command, this one included, is a ModuleNotFoundError.
        if subprocess.run([str(python), "-c", WHOLE_PACKAGE], capture_output=True).returncode != 0:
            raise UpdateError(
                "pip finished, but the application no longer imports.\n"
                f"  Repair it with: {python} {venv.parent / 'repair.py'}")
        print("  installed")
        # The rest of the update runs in the code that was just installed, not
        # in this already-imported copy. Otherwise a fix to the updater itself
        # -- a new shim to write, a unit file to migrate -- takes effect one
        # update late: the machine that most needs it runs the old logic once
        # more and gets it next time. Stdout is inherited so the second half
        # prints in line with the first.
        # The new install's own code, never an `arcade/` in the directory this was run from (2026-11-04).
        from .pyrun import module_cmd
        return subprocess.run(module_cmd("arcade.update", "--services-only", python=str(python))).returncode

    return _update_services(checkout, venv, dry_run)


def _read_manifest(channel: str = "live") -> dict:
    """The channel's manifest, checked against the pinned key; whether it is newer is the caller's to ask."""
    url = TEST_MANIFEST_URL if channel == "test" else MANIFEST_URL
    try:
        with urllib.request.urlopen(_request(url), timeout=30) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        raise UpdateError(
            f"could not read the release manifest from {url}: {exc}.\n"
            "  Nothing was installed. The manifest is what says the release is "
            "ours; without it there is nothing to check the download against.")
    try:
        return releaselib.verify(body)
    except releaselib.ReleaseError as exc:
        raise UpdateError(f"{exc}\n  Nothing was installed.")


def _signed_manifest(channel: str = "live") -> dict:
    """The manifest for what is published, checked against the pinned key.

    Everything downstream trusts this: the archive is then required to hash to
    what it says. The refusals are deliberate and total -- no manifest, a
    manifest signed by another key, or one older than what is already
    installed, and nothing is installed. An updater that installs code it
    cannot attribute is a website with a shell on every machine (D-065).
    """
    manifest = _read_manifest(channel)
    if not releaselib.is_newer(manifest, _installed_published()):
        raise UpdateError(
            "the published release is older than the one installed here. That "
            "is what a downgrade attack looks like -- an old signed release, "
            "served to put a known bug back -- so it is refused. If you meant "
            "to go back, install that version deliberately.")
    return manifest


def _installed_published() -> float | None:
    try:
        return float(json.loads((HOME / INSTALLED_FILE).read_text())["published"])
    except Exception:
        return None


def _remember(manifest: dict) -> None:
    """Write down what was installed, so the next update cannot go backwards."""
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        (HOME / INSTALLED_FILE).write_text(json.dumps(manifest, sort_keys=True))
    except OSError as exc:
        print(f"  could not record the installed release: {exc}")


def _update_services(checkout: Path, venv: Path, dry_run: bool) -> int:
    """The half of an update that is about this machine, not about the code.

    Shared by both paths: a source checkout has nothing to fetch but still needs
    its service registered, its unit files current and its interface restarted.
    """
    print("Checking the commands are on your PATH")
    if dry_run:
        print("  would write the launcher and updater if they are missing")
    else:
        written = _ensure_launcher(checkout, venv)
        print(f"  wrote {written}" if written else "  already there")

    print("Updating service definitions")
    if dry_run:
        print("  would register arcade-web if needed, and bring node units "
              "up to Restart=always")
    else:
        if _ensure_web_service(checkout, venv):
            print("  registered arcade-web, so this update can restart it")
        migrated = _migrate_node_units(checkout)
        for unit in migrated:
            print(f"  Restart=always: {Path(unit).name}")
        if not migrated:
            print("  already correct")

    print("Restarting the interface")
    from .web import supervisor as supervisorlib
    if dry_run:
        print("  would restart arcade-web")
    elif supervisorlib.reload(Path.home() / ".dogecoinarcade"):
        # Run by the supervisor (2026-10-20): the new code gets ready beside the old, then takes over -- no downtime.
        print("  reloading: the new code takes over once it is ready, with no downtime")
    elif shutil.which("systemctl"):
        # Restart the scope that ACTUALLY OWNS the port, not the first one that
        # accepts the command. This tried --user first and stopped on success,
        # so on a machine with a system unit serving :8420 it restarted an
        # unrelated user unit, printed "restarted", and left the old code
        # running for hours. The interface's staleness banner was the only thing
        # that knew. `_listener_pid` is the question that matters: whose process
        # is on the port?
        scopes = _scopes_for_port()
        for scope in scopes:
            if _run("systemctl", *scope, "restart", "arcade-web").returncode == 0:
                print(f"  restarted ({'user' if scope else 'system'} service)")
                if _verify_service_owns_port(scope):
                    break
            elif not scope:
                print("  the system service needs root to restart:")
                print("    sudo systemctl restart arcade-web")
        else:
            if not scopes:
                print("  arcade-web is not a service here; restart it yourself")
            _warn_if_still_running()
    else:
        print("  restart the interface yourself to pick up the new version")

    print()
    print("Updated. Your wallet, messages and chain data were not touched.")
    return 0


def _ensure_launcher(checkout: Path, venv: Path) -> Path | None:
    """Write the command shims if any is missing or was written by an older one.

    Same reasoning as registering the service: a machine set up before these
    existed, or set up from a checkout rather than by the installer, has none of
    them on its PATH. The user ran `dogecoinarcade-update` and got `command not
    found` -- on the machine running the application. Later `arcade-rpc` went
    the same way on a machine whose installer predated it.

    Presence was the whole test until a shim's *contents* had to change: the
    updater that repairs a broken environment could never reach a machine that
    already had a file called `dogecoinarcade-update`, which is every machine
    that has ever updated. The installer stamps a version into each shim and
    answers `shims_current`; this rewrites the set whenever that says no.
    Written from the installer's own templates, so there is one definition of
    what they contain.
    """
    import platform
    system = platform.system()
    target = _call_installer(checkout, "bindir", system)
    if target is None:
        return None
    if _call_installer(checkout, "shims_current", Path(target), system):
        return None
    written = _call_installer(checkout, "write_launcher", venv, Path(target), system)
    return Path(written) if written else None


def _listener_pid(port: int) -> int | None:
    """The PID listening on `port`, if it can be determined."""
    if not shutil.which("ss"):
        return None
    result = _run("ss", "-H", "-ltnp", f"sport = :{port}")
    if result.returncode != 0 or not result.stdout.strip():
        return None
    import re
    found = re.search(r"pid=(\d+)", result.stdout)
    return int(found.group(1)) if found else None


def _scopes_for_port(port: int = 8420) -> list[list[str]]:
    """systemctl scopes to try, the one owning `port` first.

    Both a user unit and a system unit can be called arcade-web, and only one of
    them can hold the port. Asking which scope's unit has the listener puts the
    right one first instead of taking whichever accepts a restart.
    """
    pid = _listener_pid(port)
    scopes = []
    if pid is not None:
        for scope in (["--user"], []):
            result = _run("systemctl", *scope, "show", "arcade-web", "-p",
                          "MainPID", "--value")
            if result.returncode == 0 and result.stdout.strip() == str(pid):
                scopes.append(scope)
    for scope in (["--user"], []):
        if scope not in scopes and _run("systemctl", *scope, "cat",
                                        "arcade-web").returncode == 0:
            scopes.append(scope)
    return scopes


def _verify_service_owns_port(scope: list, port: int = 8420) -> bool:
    """Confirm the restarted service is the thing actually serving.

    `systemctl restart` succeeding is not the same as the service running. Every
    existing installation has an `arcade-web` somebody started by hand, and it
    holds the port -- so the new unit starts, fails to bind with "address already
    in use", and enters a restart loop while the OLD code keeps answering. The
    update reports success and the interface is unchanged. a test machine hit exactly that
    on the first update after the unit was registered.
    """
    import time

    for _ in range(10):
        time.sleep(0.5)
        active = _run("systemctl", *scope, "is-active", "arcade-web")
        if active.stdout.strip() == "active":
            break
    else:
        print()
        print("  !  The service did not stay running.")

    main_pid = _run("systemctl", *scope, "show", "arcade-web", "-p", "MainPID",
                    "--value").stdout.strip()
    holder = _listener_pid(port)
    if holder is None or not main_pid.isdigit():
        return True                       # cannot tell; do not cry wolf

    if holder == int(main_pid):
        return True

    print()
    print(f"  !  Something else is serving on 127.0.0.1:{port} (pid {holder}),")
    print(f"     not the service (pid {main_pid or 'none'}). That is almost")
    print( "     certainly an arcade-web started by hand, from before this was")
    print( "     a service. It is holding the port, so the new version cannot")
    print( "     bind and the OLD code is what you are still looking at.")
    print()
    print(f"     Stop it and start the service:   kill {holder}")
    print(f"                                      systemctl {' '.join(scope)} "
           "restart arcade-web")
    return False


def _warn_if_still_running(port: int = 8420) -> bool:
    """Say plainly when an old interface is still serving.

    Without a service unit there is nothing to restart, so `arcade-web` keeps
    running the previous code from memory and every page served is stale. The
    update output said nothing about it and the interface showed no version, so
    the only clue was that nothing had changed. a test machine hit this and caught it; a
    user would not.
    """
    import socket

    with socket.socket() as probe:
        probe.settimeout(0.4)
        try:
            probe.connect(("127.0.0.1", port))
        except OSError:
            return False

    print()
    print("  !  Something is still serving on 127.0.0.1:%d." % port)
    print("     That is the old version, running from memory: the new code is")
    print("     installed but will not be used until it restarts. Stop that")
    print("     process and start it again, or the interface will keep showing")
    print("     you the previous release.")
    return True


def _ensure_web_service(checkout: Path, venv: Path) -> bool:
    """Register the web interface as a service if it is not one already.

    The reason this belongs in the updater and not only the installer: an
    installation made before the unit existed has nothing to restart, so every
    update leaves the old code running. The only visible sign is the commit in
    the footer, and a user would not look.
    """
    return bool(_call_installer(checkout, "ensure_web_service", venv))


def _with_archive_installer(url: str, sha256: str, function: str, *args):
    """Run one installer function from the installer inside the release archive, when the checkout has none.

    The archive is checked against `sha256`, the hash out of a manifest whose signature was already checked, before
    anything in it runs: the site cannot hand over another installer than the one that was signed."""
    import hashlib
    import tarfile
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "source.tar.gz"
        try:
            with urllib.request.urlopen(url, timeout=120) as response, archive.open("wb") as out:
                shutil.copyfileobj(response, out)
        except (urllib.error.URLError, OSError) as exc:
            print(f"  could not download {url}: {exc}")
            return None
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if digest != sha256:
            print(f"  the archive's sha256 is not the signed one ({digest[:16]}...): not run")
            return None
        with tarfile.open(archive) as tf:
            member = next((m for m in tf.getmembers() if m.isfile() and m.name.count("/") == 2
                           and m.name.endswith("/installer/install.py")), None)
            if member is None:
                print("  the archive carries no installer")
                return None
            unpacked = Path(tmp) / "x"
            tf.extract(member, unpacked, filter="data")
        root = unpacked / member.name.split("/", 1)[0]
        return _call_installer(root, function, *args)


def _call_installer(checkout: Path, function: str, *args):
    """Run one function from the freshly pulled installer.

    Loaded from the checkout rather than reimplemented, so there is one copy --
    the installer has to stay standalone and cannot import from this package, so
    the dependency points this way.
    """
    script = checkout / "installer" / "install.py"
    if not script.is_file():
        print(f"  no installer at {script}")
        return None
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_arcade_installer", script)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        target = getattr(module, function, None)
        return target(*args) if target else None
    except Exception as exc:
        print(f"  could not run {function}: {exc}")
        return None


def _migrate_node_units(checkout: Path) -> list[str]:
    """Run the installer's unit migration from the code just pulled.

    Loaded from the checkout rather than reimplemented here. The installer has to
    stay standalone -- it is downloaded and run on its own, before this package
    exists -- so it cannot import from `arcade`, and the dependency has to point
    this way round. Calling into the pulled copy also means the migration is
    always the current one, not whatever shipped with the installed version.
    """
    return list(_call_installer(checkout, "migrate_node_units") or [])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dogecoinarcade-update",
        description="Update DogecoinArcade to the latest published code.",
    )
    parser.add_argument("--check", action="store_true",
                        help="report whether an update is available, and change nothing")
    parser.add_argument("--dry-run", action="store_true", help="show what would happen")
    parser.add_argument("--services-only", action="store_true",
                        help=argparse.SUPPRESS)  # the updater's own second half
    args = parser.parse_args(argv)

    try:
        if args.check:
            installed, published, available = check()
            print(f"installed: {installed or 'unknown'}")
            print(f"published: {published or 'could not reach the server'} ({chosen_channel()} channel"
                  f"{'; ' + followed_note if followed_note else ''})")
            if published is None:
                return 1
            print()
            print("An update is available. Run dogecoinarcade-update." if available
                  else "You are up to date.")
            return 0
        if args.services_only:
            venv, checkout, _fetchable = _layout()
            return _update_services(checkout, venv, args.dry_run)
        return update(args.dry_run)
    except UpdateError as exc:
        print(f"\nERROR: {exc}\n", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

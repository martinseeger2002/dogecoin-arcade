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
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_URL = "https://dogecoinarcade.com/repo"
HOME = Path.home() / ".dogecoinarcade"


class UpdateError(Exception):
    """The update could not proceed."""


def _git() -> str:
    git = shutil.which("git")
    if not git:
        raise UpdateError("git is required to update. Install it and try again.")
    return git


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True)


def current_revision(checkout: Path) -> str | None:
    if not (checkout / ".git").exists():
        return None
    result = _run(_git(), "rev-parse", "--short", "HEAD", cwd=checkout)
    return result.stdout.strip() or None


def remote_revision() -> str | None:
    """The published HEAD, without fetching the whole repository."""
    result = _run(_git(), "ls-remote", REPO_URL, "HEAD")
    if result.returncode != 0:
        return None
    line = result.stdout.split()
    return line[0][:7] if line else None


def check() -> tuple[str | None, str | None, bool]:
    """Return (installed, published, update_available)."""
    installed = current_revision(HOME / "src")
    published = remote_revision()
    if installed is None or published is None:
        return installed, published, published is not None
    return installed, published, not published.startswith(installed[:7])


def update(dry_run: bool = False) -> int:
    venv = HOME / "venv"
    checkout = HOME / "src"

    if not venv.exists():
        raise UpdateError(
            f"no installation found at {venv}.\n"
            "  This command updates an installation made by install.py."
        )

    git = _git()
    print("Fetching the latest code")
    if dry_run:
        print(f"  would fetch {REPO_URL} into {checkout}")
    elif (checkout / ".git").exists():
        before = current_revision(checkout)
        result = _run(git, "pull", "--ff-only", "--quiet", cwd=checkout)
        if result.returncode != 0:
            raise UpdateError(
                f"could not fast-forward the checkout:\n  {result.stderr.strip()}\n"
                f"  If you have edited files in {checkout}, move them aside first."
            )
        after = current_revision(checkout)
        print(f"  already up to date ({after})" if before == after else f"  {before} -> {after}")
    else:
        checkout.parent.mkdir(parents=True, exist_ok=True)
        result = _run(git, "clone", "--quiet", REPO_URL, str(checkout))
        if result.returncode != 0:
            raise UpdateError(f"could not clone {REPO_URL}:\n  {result.stderr.strip()}")
        print(f"  cloned into {checkout}")

    print("Reinstalling")
    if dry_run:
        print("  would reinstall into the existing environment")
    else:
        pip = venv / ("Scripts/pip.exe" if os.name == "nt" else "bin/pip")
        result = _run(str(pip), "install", "-q", "--upgrade", f"{checkout}[web]")
        if result.returncode != 0:
            raise UpdateError(f"reinstall failed:\n{result.stderr[-1200:]}")
        print("  installed")

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
    if dry_run:
        print("  would restart arcade-web")
    elif shutil.which("systemctl"):
        for scope in (["--user"], []):
            if _run("systemctl", *scope, "restart", "arcade-web").returncode == 0:
                print(f"  restarted ({'user' if scope else 'system'} service)")
                _verify_service_owns_port(scope)
                break
        else:
            print("  arcade-web is not a service here; restart it yourself")
            _warn_if_still_running()
    else:
        print("  restart the interface yourself to pick up the new version")

    print()
    print("Updated. Your wallet, messages and chain data were not touched.")
    return 0


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


def _call_installer(checkout: Path, function: str, *args):
    """Run one function from the freshly pulled installer.

    Loaded from the checkout rather than reimplemented, so there is one copy --
    the installer has to stay standalone and cannot import from this package, so
    the dependency points this way.
    """
    script = checkout / "installer" / "install.py"
    if not script.is_file():
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
    args = parser.parse_args(argv)

    try:
        if args.check:
            installed, published, available = check()
            print(f"installed: {installed or 'unknown'}")
            print(f"published: {published or 'could not reach the server'}")
            if published is None:
                return 1
            print()
            print("An update is available. Run dogecoinarcade-update." if available
                  else "You are up to date.")
            return 0
        return update(args.dry_run)
    except UpdateError as exc:
        print(f"\nERROR: {exc}\n", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

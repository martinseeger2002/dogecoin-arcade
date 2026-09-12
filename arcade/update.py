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
        print("  would bring node units up to Restart=always")
    else:
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
                break
        else:
            print("  arcade-web is not a service here; restart it yourself")
            _warn_if_still_running()
    else:
        print("  restart the interface yourself to pick up the new version")

    print()
    print("Updated. Your wallet, messages and chain data were not touched.")
    return 0


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


def _migrate_node_units(checkout: Path) -> list[str]:
    """Run the installer's unit migration from the code just pulled.

    Loaded from the checkout rather than reimplemented here. The installer has to
    stay standalone -- it is downloaded and run on its own, before this package
    exists -- so it cannot import from `arcade`, and the dependency has to point
    this way round. Calling into the pulled copy also means the migration is
    always the current one, not whatever shipped with the installed version.
    """
    script = checkout / "installer" / "install.py"
    if not script.is_file():
        return []
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_arcade_installer", script)
        if spec is None or spec.loader is None:
            return []
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        migrate = getattr(module, "migrate_node_units", None)
        if migrate is None:
            return []          # a checkout from before the migration existed
        return list(migrate())
    except Exception as exc:
        # Never fail an update over this: the code is already installed and
        # working, and a stale unit is a missing improvement, not a breakage.
        print(f"  could not update service definitions: {exc}")
        return []


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

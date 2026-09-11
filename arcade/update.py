"""Update an installed DogecoinArcade to the latest published code.

    dogecoinarcade-update            # fetch and reinstall
    dogecoinarcade-update --check    # only report whether an update exists
    dogecoinarcade-update --dry-run  # show what would happen

Deliberately touches **only the code**. Not the identity key, not the message
database, not the chain data, not the node configuration. An update command that
could destroy someone's key would be worse than having no update command, since
there is no recovery path for that key.

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
    else:
        print("  restart the interface yourself to pick up the new version")

    print()
    print("Updated. Your identity key, messages and chain data were not touched.")
    return 0


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

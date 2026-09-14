"""The update command, which refused to run on the machine it was run from.

`dogecoinarcade-update` assumed the installer's layout -- ~/.dogecoinarcade/venv
and .../src -- and raised "no installation found" on anything else. One of the
two machines running this was set up from a source checkout, so the documented
command failed there with nothing but that message, and the shim was not even on
PATH to produce it: `dogecoinarcade-update: command not found`.

A source checkout has no code to fetch. What it does still need is the half of
an update that is about the machine: the service registered, unit files brought
current, and the interface restarted so it stops serving the old code from
memory. These cover that split.
"""

from pathlib import Path

import pytest

from arcade import update


@pytest.fixture
def checkout(tmp_path):
    """Something shaped enough like a checkout for _layout to accept it."""
    root = tmp_path / "src"
    (root / "arcade").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='dogecoinarcade'\n")
    return root


def test_the_installer_layout_still_wins(tmp_path, monkeypatch):
    home = tmp_path / ".dogecoinarcade"
    (home / "venv").mkdir(parents=True)
    monkeypatch.setattr(update, "HOME", home)

    venv, source, fetchable = update._layout()
    assert venv == home / "venv"
    assert source == home / "src"
    assert fetchable, "an installer install is the one that can be fetched into"


def test_a_source_checkout_is_found_and_is_not_fetchable(tmp_path, monkeypatch, checkout):
    """The fix: it locates itself instead of refusing."""
    monkeypatch.setattr(update, "HOME", tmp_path / "absent")
    monkeypatch.setattr(update, "__file__", str(checkout / "arcade" / "update.py"))
    monkeypatch.setattr(update.sys, "prefix", str(checkout / ".venv"))

    venv, source, fetchable = update._layout()
    assert source == checkout
    assert venv == checkout / ".venv"
    assert not fetchable, (
        "a checkout has nothing to fetch -- on the machine releases are published "
        "from it is ahead of what is published, and pulling would try to rewind it"
    )


def test_neither_layout_is_an_error_that_names_both(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "HOME", tmp_path / "absent")
    monkeypatch.setattr(update, "__file__", str(tmp_path / "nowhere" / "arcade" / "update.py"))

    with pytest.raises(update.UpdateError) as raised:
        update._layout()
    message = str(raised.value)
    assert "no installation found" in message
    assert "checkout" in message


def test_a_source_checkout_never_reaches_git(tmp_path, monkeypatch, checkout, capsys):
    """It used to raise before this point; it must not start pulling either."""
    monkeypatch.setattr(update, "HOME", tmp_path / "absent")
    monkeypatch.setattr(update, "__file__", str(checkout / "arcade" / "update.py"))
    monkeypatch.setattr(update.sys, "prefix", str(checkout / ".venv"))

    def no_git():
        raise AssertionError("a source checkout must not be fetched into")

    monkeypatch.setattr(update, "_git", no_git)
    monkeypatch.setattr(update, "_run", lambda *a, **k: pytest.fail("no subprocesses"))
    monkeypatch.setattr(update.shutil, "which", lambda name: None)
    monkeypatch.setattr(update, "_call_installer", lambda *a: None)

    assert update.update(dry_run=False) == 0
    out = capsys.readouterr().out
    assert "Source checkout" in out
    assert "Nothing to fetch" in out
    assert "wallet, messages and chain data were not touched" in out


def test_the_launcher_is_written_when_it_is_missing(tmp_path, monkeypatch, checkout):
    """The `command not found` half: a machine can be running this and have no shim."""
    bindir = tmp_path / "bin"
    calls = []

    def fake_installer(_checkout, function, *args):
        calls.append(function)
        if function == "bindir":
            return bindir
        if function == "shim_names":
            return ["dogecoinarcade", "dogecoinarcade-update", "arcade-rpc"]
        if function == "write_launcher":
            target = Path(args[1])
            target.mkdir(parents=True, exist_ok=True)
            for name in ("dogecoinarcade", "dogecoinarcade-update", "arcade-rpc"):
                (target / name).write_text("#!/bin/sh\n")
            return target / "dogecoinarcade"
        return None

    monkeypatch.setattr(update, "_call_installer", fake_installer)

    written = update._ensure_launcher(checkout, checkout / ".venv")
    assert written == bindir / "dogecoinarcade"
    assert "write_launcher" in calls

    # Idempotent: a second run must not rewrite a shim that is already there.
    calls.clear()
    assert update._ensure_launcher(checkout, checkout / ".venv") is None
    assert "write_launcher" not in calls

    # A machine installed before `arcade-rpc` existed has the first two and not
    # the third; that is exactly the `command not found` this is for.
    (bindir / "arcade-rpc").unlink()
    calls.clear()
    assert update._ensure_launcher(checkout, checkout / ".venv") == bindir / "dogecoinarcade"
    assert "write_launcher" in calls
    assert (bindir / "arcade-rpc").exists()


def test_check_reads_the_checkout_it_actually_has(tmp_path, monkeypatch, checkout):
    """It reported "installed: unknown, an update is available" on a current machine.

    `check` looked in the installer's ~/.dogecoinarcade/src, which a source
    install does not have, so every check claimed the machine was behind.
    """
    monkeypatch.setattr(update, "HOME", tmp_path / "absent")
    monkeypatch.setattr(update, "__file__", str(checkout / "arcade" / "update.py"))
    monkeypatch.setattr(update.sys, "prefix", str(checkout / ".venv"))
    monkeypatch.setattr(update, "current_revision",
                        lambda path: "abc1234" if path == checkout else None)
    monkeypatch.setattr(update, "remote_revision", lambda: "abc1234")

    installed, published, available = update.check()
    assert installed == "abc1234"
    assert published == "abc1234"
    assert not available, "the machine is on the published commit"


# --- two units can be called arcade-web ---------------------------------------
#
# This machine already had a hardened system unit at
# /etc/systemd/system/arcade-web.service. `ensure_web_service` checked only the
# USER path, so it wrote a second, user-level unit -- which could never bind,
# because the system one owned :8420. The user unit sat in a restart loop
# (NRestarts reached 40) while the system service carried on serving code from
# hours earlier, and every `dogecoinarcade-update` restarted the user unit and
# printed "restarted (user service)". Nothing in the output was false; it was
# just about the wrong unit.
#
# The interface's own staleness banner was the only thing telling the truth, and
# I did not read it -- I grepped the page for the commit hash instead, which
# matched the banner's "<hash> is installed" text. That is the day's lesson
# again: presence of a string is not the property you wanted.


def test_the_owner_of_the_port_is_restarted_first(monkeypatch):
    """Not the first scope that accepts the command."""
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args)
        import subprocess
        # The user unit exists but is NOT the listener; the system unit is.
        if args[:2] == ("systemctl", "--user") and "show" in args:
            return subprocess.CompletedProcess(args, 0, "99999\n", "")
        if args[0] == "systemctl" and "show" in args:
            return subprocess.CompletedProcess(args, 0, "4242\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(update, "_run", fake_run)
    monkeypatch.setattr(update, "_listener_pid", lambda port=8420: 4242)

    scopes = update._scopes_for_port()
    assert scopes[0] == [], (
        "the system unit holds the port, so it must be restarted first"
    )


def test_a_scope_that_owns_nothing_is_still_offered_last(monkeypatch):
    """So a machine with no listener yet can still be restarted."""
    import subprocess

    monkeypatch.setattr(update, "_listener_pid", lambda port=8420: None)
    monkeypatch.setattr(update, "_run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 0, "", ""))

    scopes = update._scopes_for_port()
    assert scopes == [["--user"], []], "both scopes exist, neither owns the port"


def test_the_second_half_runs_in_the_code_just_installed(tmp_path, monkeypatch, capsys):
    """An updater fix took effect one update late: the process that pip had just
    replaced on disk carried on running the copy it had imported, so the shim
    the new code knew to write was written by the *next* run. After the
    reinstall, the rest of the update is handed to the fresh install."""
    home = tmp_path / ".dogecoinarcade"
    venv = home / "venv"
    src = home / "src"
    (venv / "bin").mkdir(parents=True)
    (src / ".git").mkdir(parents=True)
    (src / "arcade").mkdir()
    (src / "pyproject.toml").write_text("[project]\nname='dogecoinarcade'\n")
    monkeypatch.setattr(update, "HOME", home)
    monkeypatch.setattr(update, "_find_git", lambda: "git")
    monkeypatch.setattr(update, "current_revision", lambda _c: "abc1234")

    class Done:
        returncode = 0
        stdout = stderr = ""

    ran = []
    monkeypatch.setattr(update, "_run", lambda *a, **k: ran.append(a) or Done())
    handed_off = []
    monkeypatch.setattr(update.subprocess, "run",
                        lambda argv, **k: handed_off.append(argv) or Done())
    monkeypatch.setattr(update, "_update_services",
                        lambda *a: pytest.fail("the old copy must not run the services half"))

    assert update.update(dry_run=False) == 0
    assert any(a[1:3] == ("install", "-q") for a in ran), ran
    assert handed_off == [[str(venv / "bin" / "python"), "-m", "arcade.update",
                           "--services-only"]]
    assert "installed" in capsys.readouterr().out


def test_services_only_is_the_second_half_and_installs_nothing(tmp_path, monkeypatch):
    home = tmp_path / ".dogecoinarcade"
    venv = home / "venv"
    src = home / "src"
    (venv / "bin").mkdir(parents=True)
    (src / "arcade").mkdir(parents=True)
    (src / "pyproject.toml").write_text("[project]\nname='dogecoinarcade'\n")
    monkeypatch.setattr(update, "HOME", home)
    monkeypatch.setattr(update, "_run", lambda *a, **k: pytest.fail("nothing to fetch or install"))
    monkeypatch.setattr(update.subprocess, "run", lambda *a, **k: pytest.fail("no third half"))
    seen = []
    monkeypatch.setattr(update, "_update_services",
                        lambda checkout, v, dry_run: seen.append((checkout, v, dry_run)) or 0)

    assert update.main(["--services-only"]) == 0
    assert seen == [(src, venv, False)]

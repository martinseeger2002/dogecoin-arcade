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
        if function == "write_launcher":
            target = Path(args[1])
            target.mkdir(parents=True, exist_ok=True)
            (target / "dogecoinarcade").write_text("#!/bin/sh\n")
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

"""Tests for the installer's service migration.

The published fix for `Restart=on-failure` named `/etc/systemd/system/...`, a
path that does not exist on a normal install -- the installer writes *user*
units. a test machine found it. The lesson is that the fix belongs in the updater, where
it can look at what is actually there, rather than in instructions that guess.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "installer"))
import install                                                    # noqa: E402


OLD_UNIT = """\
[Unit]
Description=Pepecoin Core daemon

[Service]
ExecStart=/usr/local/bin/pepecoind -datadir=/home/someone/.pepecoin-testnet
Restart=on-failure
RestartSec=30
"""


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(install.Path, "home", staticmethod(lambda: tmp_path))
    units = tmp_path / ".config/systemd/user"
    units.mkdir(parents=True)
    return tmp_path, units


def test_a_user_unit_is_migrated_without_privileges(fake_home):
    """A user install must need no sudo at all -- that was the whole complaint."""
    _, units = fake_home
    unit = units / "pepecoind-testnet.service"
    unit.write_text(OLD_UNIT)

    changed = install.migrate_node_units()

    assert str(unit) in changed
    text = unit.read_text()
    assert "Restart=always" in text
    assert "Restart=on-failure" not in text
    assert "RestartSec=5" in text


def test_an_already_correct_unit_is_left_alone(fake_home):
    _, units = fake_home
    unit = units / "dogecoind.service"
    unit.write_text(OLD_UNIT.replace("on-failure", "always").replace("RestartSec=30",
                                                                    "RestartSec=5"))
    before = unit.read_text()

    assert str(unit) not in install.migrate_node_units()
    assert unit.read_text() == before


def test_the_rest_of_the_unit_survives_migration(fake_home):
    """Rewriting a unit must not lose the line that says where the data lives."""
    _, units = fake_home
    unit = units / "pepecoind.service"
    unit.write_text(OLD_UNIT)

    install.migrate_node_units()

    assert "datadir=/home/someone/.pepecoin-testnet" in unit.read_text()
    assert "Description=Pepecoin Core daemon" in unit.read_text()


def test_a_dry_run_changes_nothing(fake_home):
    _, units = fake_home
    unit = units / "pepecoind-testnet.service"
    unit.write_text(OLD_UNIT)

    assert str(unit) in install.migrate_node_units(dry_run=True)
    assert unit.read_text() == OLD_UNIT


def test_unrelated_units_are_not_touched(fake_home):
    """Only node units. Somebody else's service is none of our business."""
    _, units = fake_home
    other = units / "syncthing.service"
    other.write_text(OLD_UNIT)

    assert install.migrate_node_units() == []
    assert other.read_text() == OLD_UNIT


# --- the second entry point ---------------------------------------------------
# `dogecoinarcade-update` is arcade.update:main, a different code path from
# `install.py --update`. The unit migration was added to one and not the other,
# so the published command did nothing -- a test machine found it after updating. These
# exist so the two cannot drift apart again unnoticed.


def _checkout_with_installer(tmp_path):
    """A checkout laid out the way a real one is, holding the real installer."""
    import shutil
    checkout = tmp_path / "src"
    (checkout / "installer").mkdir(parents=True)
    shutil.copy2(pathlib.Path(__file__).resolve().parent.parent / "installer/install.py",
                 checkout / "installer" / "install.py")
    return checkout


def test_the_update_command_migrates_units_too(fake_home):
    """The bug: `dogecoinarcade-update` left every unit untouched."""
    from arcade.update import _migrate_node_units

    tmp_path, units = fake_home
    unit = units / "pepecoind-testnet.service"
    unit.write_text(OLD_UNIT)

    changed = _migrate_node_units(_checkout_with_installer(tmp_path))

    assert str(unit) in changed
    assert "Restart=always" in unit.read_text()


def test_an_old_checkout_does_not_break_the_update(fake_home):
    """Updating from before the migration existed must still succeed."""
    from arcade.update import _migrate_node_units

    tmp_path, _ = fake_home
    old = tmp_path / "old"
    (old / "installer").mkdir(parents=True)
    (old / "installer" / "install.py").write_text("VERSION = 1\n")

    assert _migrate_node_units(old) == []


def test_a_missing_installer_does_not_break_the_update(fake_home):
    from arcade.update import _migrate_node_units

    tmp_path, _ = fake_home
    assert _migrate_node_units(tmp_path / "nothing-here") == []


def test_both_update_paths_use_one_implementation(fake_home):
    """Two copies drifted once. There must not be a second one to drift."""
    import arcade.update as update

    source = pathlib.Path(update.__file__).read_text()
    assert "Restart=on-failure" not in source, (
        "arcade/update.py has grown its own copy of the migration; it should "
        "call the installer's instead"
    )


# --- an existing installation has nothing to restart --------------------------
# install.py registered the web interface only on a fresh install, so an
# installation made before that existed had no unit -- and every update left the
# OLD code running, serving the previous release with no sign but the commit in
# the footer. On one machine that cost four rounds in a day and produced two
# false diagnoses: a phantom progress bar, and a button that posted into a void.
# Both were this single cause in different clothes.


@pytest.fixture
def no_system_unit(monkeypatch, tmp_path):
    """Pretend the host has no /etc/systemd/system/arcade-web.service.

    Without this these tests pass or fail according to what the machine running
    them happens to have installed -- and this machine has one, which is how the
    duplicate-unit bug got here in the first place.
    """
    monkeypatch.setattr(install, "SYSTEM_WEB_UNIT", tmp_path / "absent.service")


def test_a_system_unit_is_registration_enough(fake_home, monkeypatch, tmp_path):
    """The bug: only the user path was checked.

    A second unit beside a working system one can never bind, because the system
    one already owns :8420 -- so it restart-loops while the system service goes
    on serving whatever code it started with.
    """
    present = tmp_path / "present.service"
    present.write_text("[Service]\n")
    monkeypatch.setattr(install, "SYSTEM_WEB_UNIT", present)

    called = []
    monkeypatch.setattr(install, "install_web_service",
                        lambda venv, system: called.append(system) or True)
    monkeypatch.setattr(install.shutil, "which", lambda name: "/usr/bin/systemctl")

    assert install.ensure_web_service(tmp_path / "venv") is False
    assert called == [], "a duplicate unit was written"


def test_the_web_service_is_registered_when_missing(fake_home, monkeypatch,
                                                    no_system_unit):
    tmp_path, units = fake_home
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "arcade-web").write_text("#!/bin/sh\n")

    monkeypatch.setattr(install.shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(install.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0})())
    monkeypatch.setattr(install.platform, "system", lambda: "Linux")

    assert install.ensure_web_service(venv) is True
    assert (units / "arcade-web.service").exists()


def test_registering_is_idempotent(fake_home, monkeypatch):
    """An update runs it every time; it must not rewrite a unit each run."""
    tmp_path, units = fake_home
    (units / "arcade-web.service").write_text("[Service]\n")
    monkeypatch.setattr(install.shutil, "which", lambda name: "/usr/bin/systemctl")

    assert install.ensure_web_service(tmp_path / "venv") is False


def test_nothing_is_registered_without_systemd(fake_home, monkeypatch):
    tmp_path, _ = fake_home
    monkeypatch.setattr(install.shutil, "which", lambda name: None)

    assert install.ensure_web_service(tmp_path / "venv") is False


def test_the_updater_registers_it_too(fake_home):
    """The console script is the path a user actually takes."""
    import inspect
    from arcade import update

    source = inspect.getsource(update)
    assert "_ensure_web_service" in source
    assert "nothing to restart" in source


def test_both_installer_calls_go_through_one_loader(fake_home):
    """Two copies of the loading logic would drift, as everything else has."""
    import inspect
    from arcade import update

    source = inspect.getsource(update)
    assert source.count("spec_from_file_location") == 1


# --- restarting a service is not the same as it running -----------------------
# `systemctl restart` succeeding says nothing about whether the service could
# bind. Every existing installation has an arcade-web started by hand holding
# the port, so the new unit starts, fails with "address already in use", and
# enters a restart loop while the OLD code keeps answering -- and the update
# reports success. a test machine hit exactly that on the first update after the unit was
# registered, and got the out-of-date banner for its trouble.


def test_a_foreign_listener_is_reported(monkeypatch, capsys):
    from arcade import update

    def fake_run(*args, **kwargs):
        joined = " ".join(args)
        if "is-active" in joined:
            return type("R", (), {"returncode": 0, "stdout": "active\n"})()
        if "MainPID" in joined:
            return type("R", (), {"returncode": 0, "stdout": "4242\n"})()
        return type("R", (), {"returncode": 0, "stdout": ""})()

    monkeypatch.setattr(update, "_run", fake_run)
    monkeypatch.setattr(update, "_listener_pid", lambda port: 9999)

    assert update._verify_service_owns_port(["--user"]) is False
    printed = capsys.readouterr().out
    assert "Something else is serving" in printed
    assert "9999" in printed and "started by hand" in printed
    assert "kill 9999" in printed, "it should give the exact commands"


def test_the_service_owning_the_port_is_not_reported(monkeypatch, capsys):
    from arcade import update

    def fake_run(*args, **kwargs):
        joined = " ".join(args)
        if "is-active" in joined:
            return type("R", (), {"returncode": 0, "stdout": "active\n"})()
        if "MainPID" in joined:
            return type("R", (), {"returncode": 0, "stdout": "4242\n"})()
        return type("R", (), {"returncode": 0, "stdout": ""})()

    monkeypatch.setattr(update, "_run", fake_run)
    monkeypatch.setattr(update, "_listener_pid", lambda port: 4242)

    assert update._verify_service_owns_port(["--user"]) is True
    assert "Something else is serving" not in capsys.readouterr().out


def test_an_undeterminable_listener_does_not_cry_wolf(monkeypatch, capsys):
    """Without `ss` the PID is unknown; a false alarm is worse than silence."""
    from arcade import update

    monkeypatch.setattr(update, "_run",
                        lambda *a, **k: type("R", (), {"returncode": 0,
                                                       "stdout": "active\n"})())
    monkeypatch.setattr(update, "_listener_pid", lambda port: None)

    assert update._verify_service_owns_port(["--user"]) is True
    assert "Something else" not in capsys.readouterr().out

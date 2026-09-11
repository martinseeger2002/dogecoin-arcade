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

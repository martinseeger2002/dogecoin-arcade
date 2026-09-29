"""Tests for the installer's service migration.

The published fix for `Restart=on-failure` named `/etc/systemd/system/...`, a
path that does not exist on a normal install -- the installer writes *user*
units. BOXA found it. The lesson is that the fix belongs in the updater, where
it can look at what is actually there, rather than in instructions that guess.
"""

import pathlib
from pathlib import Path
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
# so the published command did nothing -- BOXA found it after updating. These
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
# reports success. BOXA hit exactly that on the first update after the unit was
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


# --- fetching the application without git -------------------------------------
# A Windows user got as far as [6/7] and was told "git is needed", with install
# lines for Debian, Fedora and macOS and nothing for the machine they were on.
# They installed Git for Windows and the same Command Prompt still could not see
# it, because its PATH was fixed when the window opened. Neither half of that
# should have been possible: the archive needs no git at all.


@pytest.fixture
def published(tmp_path, monkeypatch):
    """A site serving source.tar.gz, its sum and its revision, from disk."""
    import tarfile
    import urllib.request

    source = tmp_path / "repo" / "dogecoinarcade"
    (source / "arcade").mkdir(parents=True)
    (source / "pyproject.toml").write_text("[project]\nname='dogecoinarcade'\n")
    (source / "arcade" / "__init__.py").write_text("VERSION = 2\n")
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(source, arcname="dogecoinarcade")

    import hashlib
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    served = {
        install.ARCHIVE_URL: archive.read_bytes(),
        install.ARCHIVE_SUMS_URL: (digest + "  source.tar.gz\n").encode(),
        install.REVISION_URL: b"1234567890abcdef1234567890abcdef12345678\n",
    }

    class FakeResponse:
        def __init__(self, body):
            self._body = body
            self.headers = {"Content-Length": str(len(body))}
            self._read = False

        def read(self, size=None):
            if self._read:
                return b""
            self._read = True
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(url, timeout=None):
        # A Request now, not a string: every fetch names itself, because the
        # site's zone refuses Python-urllib's default header with a 403 that
        # no installation could act on (D-066).
        where = getattr(url, "full_url", url)
        assert "DogecoinArcade" in getattr(url, "headers", {}).get("User-agent", ""), \
            "a program that downloads software should say what it is"
        if where not in served:
            raise urllib.error.URLError(f"not served: {where}")
        return FakeResponse(served[where])

    monkeypatch.setattr(install.urllib.request, "urlopen", fake_urlopen)
    return served, digest


def test_the_application_can_be_fetched_without_git(fake_home, published, tmp_path):
    """The archive path has to produce exactly what a clone would: a directory
    with pyproject.toml at its root, ready for `pip install`."""
    checkout = tmp_path / "home" / "src"
    revision = install.fetch_source_archive(checkout)

    assert (checkout / "pyproject.toml").is_file()
    assert (checkout / "arcade" / "__init__.py").read_text() == "VERSION = 2\n"
    assert revision.startswith("1234567")
    assert (checkout / install.REVISION_FILE).read_text().strip() == revision


def test_a_corrupted_archive_is_refused(fake_home, published, tmp_path, monkeypatch):
    """A truncated download must never reach pip. The sum is the only check
    there is here -- it is not a signature -- so it has to be enforced."""
    served, _ = published
    served[install.ARCHIVE_SUMS_URL] = b"0" * 64 + b"  source.tar.gz\n"

    with pytest.raises(install.InstallError, match="MISMATCH"):
        install.fetch_source_archive(tmp_path / "home" / "src")


def test_an_update_that_fails_leaves_the_old_code_in_place(fake_home, published,
                                                           tmp_path, monkeypatch):
    """Replacing the checkout wholesale must not be able to lose it. A machine
    left with no application because a download died mid-way is worse than one
    running last week's."""
    checkout = tmp_path / "home" / "src"
    install.fetch_source_archive(checkout)
    (checkout / "pyproject.toml").write_text("[project]\nname='old'\n")

    monkeypatch.setattr(install.shutil, "move",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        install.fetch_source_archive(checkout)
    assert (checkout / "pyproject.toml").read_text() == "[project]\nname='old'\n"


def test_git_is_found_where_windows_puts_it(monkeypatch, tmp_path):
    """`shutil.which` reads the PATH this process started with, so a git
    installed a minute ago is invisible to the terminal that asked for it."""
    import types

    monkeypatch.setattr(install.shutil, "which", lambda name: None)
    # A shim rather than os.name itself: setting that turns every pathlib.Path
    # on this machine into a WindowsPath, which cannot be instantiated here.
    monkeypatch.setattr(install, "os", types.SimpleNamespace(
        name="nt", environ={"ProgramFiles": str(tmp_path)}))
    assert install.find_git() is None                # not there at all

    git = tmp_path / "Git" / "cmd" / "git.exe"
    git.parent.mkdir(parents=True)
    git.write_text("")
    assert install.find_git() == str(git)


def test_the_updater_knows_what_an_archive_install_holds(fake_home, tmp_path):
    """No .git means `git rev-parse` has nothing to read; the installer wrote
    the revision down for exactly this."""
    import arcade.update as update

    checkout = tmp_path / "src"
    checkout.mkdir()
    assert update.current_revision(checkout) is None
    (checkout / update.REVISION_FILE).write_text("abcdef1234567890\n")
    assert update.current_revision(checkout) == "abcdef1"


def test_the_published_revision_needs_no_git(monkeypatch):
    """--check on a machine with no git reported "unknown" against "unknown"
    and offered an update it could not describe."""
    import urllib.request

    import arcade.update as update

    monkeypatch.setattr(update, "_find_git", lambda: None)

    class FakeResponse:
        def read(self):
            return b"7113d88aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=None: FakeResponse())
    assert update.remote_revision() == "7113d88"


# --- what a plain install has to be able to build -----------------------------
# The same Windows user, one step later: "Failed to build 'pyzmq'". pip found no
# wheel for their Python, tried to compile from source, and Windows has no
# compiler. It was building a C extension for a module the application never
# imports.


def test_nothing_the_application_runs_needs_a_compiler():
    """Every required dependency must install from a wheel on any Python.

    pyzmq is the one with a C extension, and it is an extra for that reason. A
    required dependency that has to be compiled turns a new Python release into
    a broken installer on every machine without build tools.
    """
    import tomllib

    root = pathlib.Path(install.__file__).resolve().parent.parent
    meta = tomllib.loads((root / "pyproject.toml").read_text())
    required = meta["project"]["dependencies"]
    web = meta["project"]["optional-dependencies"]["web"]

    assert not any("zmq" in dep for dep in required + web), (
        "pyzmq belongs in the [zmq] extra: nothing the application runs imports "
        "it, and it is the only dependency that needs a compiler"
    )
    assert any("zmq" in dep
               for dep in meta["project"]["optional-dependencies"]["zmq"])


def test_no_module_the_installer_installs_imports_zmq():
    """The extra may only be needed by the listener itself.

    If anything else grew an `import zmq`, a plain install would import it and
    fail at runtime instead of at install time -- which is worse.
    """
    root = pathlib.Path(install.__file__).resolve().parent.parent
    offenders = [
        path.relative_to(root)
        for path in (root / "arcade").rglob("*.py")
        if path.name != "zmq_listener.py"
        and any(line.startswith(("import zmq", "from zmq"))
                for line in path.read_text().splitlines())
    ]
    assert offenders == [], f"these would fail without the [zmq] extra: {offenders}"


def test_a_build_failure_says_what_it_actually_means():
    """"Use build.targets instead of cmake.targets" is not a sentence anyone
    can act on. The version of Python is the part that matters."""
    hint = install.build_failure_hint(
        "ERROR: Failed to build 'pyzmq' when getting requirements to build wheel")
    assert "no ready-made build for Python" in hint
    assert "no compiler" in hint
    assert install.build_failure_hint("some unrelated pip noise") == ""


# --- installing over an existing node -------------------------------------------


def test_a_daemon_already_installed_is_found(tmp_path, monkeypatch):
    daemon = tmp_path / "pepecoind"
    daemon.write_text("#!/bin/sh\necho 'Pepecoin Core Daemon version v1.1.0'\n")
    daemon.chmod(0o755)
    monkeypatch.setattr(install.shutil, "which", lambda name: None)

    found = install.existing_core(install.PEPECOIN, tmp_path, "Linux")
    assert found is not None
    where, version = found
    assert where == daemon
    assert version == "v1.1.0"


def test_one_on_the_PATH_counts_too(tmp_path, monkeypatch):
    """The copy they actually run is the one the shell finds."""
    daemon = tmp_path / "elsewhere" / "pepecoind"
    daemon.parent.mkdir()
    daemon.write_text("#!/bin/sh\necho 'Pepecoin Core Daemon version v1.1.0'\n")
    daemon.chmod(0o755)
    monkeypatch.setattr(install.shutil, "which",
                        lambda name: str(daemon) if name == "pepecoind" else None)

    found = install.existing_core(install.PEPECOIN, tmp_path / "empty", "Linux")
    assert found is not None and found[0] == daemon


def test_nothing_installed_is_reported_as_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(install.shutil, "which", lambda name: None)
    assert install.existing_core(install.DOGECOIN, tmp_path, "Linux") is None


def test_the_usual_places_are_looked_in(tmp_path):
    for system, expected in (("Linux", "/usr/local/bin/pepecoind"),
                             ("Darwin", "/opt/homebrew/bin/pepecoind"),
                             ("Windows", "pepecoind.exe")):
        paths = [str(p) for p in
                 install.core_search_paths(install.PEPECOIN, tmp_path, system)]
        assert any(expected in p for p in paths), (system, paths)


def test_a_daemon_that_will_not_answer_is_not_a_crash(tmp_path):
    """Version is a nicety; being there is the fact that matters."""
    daemon = tmp_path / "pepecoind"
    daemon.write_text("not executable")
    assert install.core_version(daemon) is None
    found = install.existing_core(install.PEPECOIN, tmp_path, "Linux")
    assert found is not None and found[1] is None


def test_looking_for_a_running_daemon_never_raises(monkeypatch):
    """pgrep is not everywhere, and tasklist is not on Linux."""
    def refuse(*a, **k):
        raise OSError("no such tool")
    monkeypatch.setattr(install.subprocess, "run", refuse)
    assert install.running_core(install.PEPECOIN) is None


def test_an_existing_node_is_not_downloaded_over(tmp_path, monkeypatch, capsys):
    """The whole point: no download, no overwrite, and say why."""
    monkeypatch.setattr(install, "existing_core",
                        lambda coin, target, system: (Path("/usr/local/bin/pepecoind"),
                                                      "v1.1.0"))
    monkeypatch.setattr(install, "running_core", lambda coin: "1234 pepecoind")
    monkeypatch.setattr(install, "fetch_core",
                        lambda *a, **k: pytest.fail("must not download"))
    monkeypatch.setattr(install, "install_core",
                        lambda *a, **k: pytest.fail("must not overwrite"))
    monkeypatch.setattr(install, "install_services",
                        lambda *a, **k: pytest.fail("must not start a second daemon"))
    monkeypatch.setattr(install, "write_configs", lambda *a, **k: None)
    monkeypatch.setattr(install, "install_app", lambda dry: tmp_path / "venv")
    monkeypatch.setattr(install, "install_web_service", lambda *a, **k: True)
    monkeypatch.setattr(install, "write_launcher", lambda *a, **k: tmp_path / "x")

    assert install.main(["--coin", "pepecoin", "--no-browser"]) == 0
    out = capsys.readouterr().out
    assert "already installed" in out
    assert "it is running, so it is left alone" in out
    assert "--force-core" in out, "say how to override it"


def test_force_core_installs_ours_anyway(tmp_path, monkeypatch):
    """An override has to actually override."""
    monkeypatch.setattr(install, "existing_core",
                        lambda coin, target, system: (Path("/usr/local/bin/pepecoind"),
                                                      "v1.0.0"))
    monkeypatch.setattr(install, "running_core", lambda coin: None)
    fetched = []
    monkeypatch.setattr(install, "fetch_core",
                        lambda workdir, asset, coin: fetched.append(asset) or Path("a"))
    monkeypatch.setattr(install, "install_core", lambda *a, **k: None)
    monkeypatch.setattr(install, "restart_running_nodes", lambda coin: [])
    monkeypatch.setattr(install, "write_configs", lambda *a, **k: None)
    monkeypatch.setattr(install, "install_services", lambda *a, **k: [])
    monkeypatch.setattr(install, "install_app", lambda dry: tmp_path / "venv")
    monkeypatch.setattr(install, "install_web_service", lambda *a, **k: True)
    monkeypatch.setattr(install, "write_launcher", lambda *a, **k: tmp_path / "x")

    assert install.main(["--coin", "pepecoin", "--no-browser", "--force-core"]) == 0
    assert fetched, "--force-core must download ours"


def test_every_command_is_written_and_the_updater_knows_their_names(tmp_path, monkeypatch):
    """`arcade-rpc` was on the venv's PATH and nowhere else after an install.

    The bot RPC command is part of what the installer promises, so it is written
    beside the launcher, and the updater asks the installer what it writes rather
    than keeping its own list that would drift the same way.
    """
    monkeypatch.setattr(install, "info", lambda *a, **k: None)
    venv = tmp_path / "venv"
    target = tmp_path / "bin"
    install.write_launcher(venv, target, "Linux")
    assert sorted(f.name for f in target.iterdir()) == sorted(install.shim_names("Linux"))
    rpc = target / "arcade-rpc"
    assert rpc.stat().st_mode & 0o111
    assert f'exec "{venv}/bin/arcade-rpc" "$@"' in rpc.read_text()

    win = tmp_path / "winbin"
    install.write_launcher(venv, win, "Windows")
    assert sorted(f.name for f in win.iterdir()) == sorted(install.shim_names("Windows"))
    assert "arcade-rpc.exe" in (win / "arcade-rpc.cmd").read_text()


def test_a_broken_environment_repairs_itself(tmp_path, monkeypatch):
    """`dogecoinarcade-update` died with ModuleNotFoundError on a real machine.

    The updater lives inside the package it updates, so an environment that has
    lost the package has no way back: every command it owns is a traceback. The
    repair script sits outside the environment for exactly that, and each shim
    runs it before doing its own job.
    """
    monkeypatch.setattr(install, "info", lambda *a, **k: None)
    venv = tmp_path / ".dogecoinarcade" / "venv"
    (venv / "bin").mkdir(parents=True)
    target = tmp_path / "bin"
    install.write_launcher(venv, target, "Linux")

    repair = install.repair_path(venv)
    assert repair.is_file(), "written beside the environment, not inside it"
    updater = (target / "dogecoinarcade-update").read_text()
    assert "import arcade.update" in updater, "test the package before using it"
    assert str(repair) in updater
    for name in ("dogecoinarcade", "arcade-rpc"):
        assert str(repair) in (target / name).read_text()

    # It has to run in an interpreter that knows nothing about the application.
    source = {"__name__": "not_main"}
    exec(compile(repair.read_text(), str(repair), "exec"), source)
    assert source["SOURCE"] == venv.parent / "src"
    assert source["PYTHON"] == venv / "bin" / "python"
    # Nothing to install from: say so in a sentence, not a traceback.
    assert source["main"]() == 1


def test_an_outdated_shim_is_rewritten(tmp_path, monkeypatch):
    """Presence was the only test, so a corrected shim never reached anyone.

    Every machine that has updated already has a file called
    `dogecoinarcade-update`; the fix that repairs a broken environment is in its
    contents. `shims_current` is what the updater asks before rewriting.
    """
    monkeypatch.setattr(install, "info", lambda *a, **k: None)
    venv = tmp_path / ".dogecoinarcade" / "venv"
    target = tmp_path / "bin"
    install.write_launcher(venv, target, "Linux")
    assert install.shims_current(target, "Linux")

    (target / "dogecoinarcade-update").write_text(
        "#!/bin/sh\n# DogecoinArcade updater (shim 1)\n")
    assert not install.shims_current(target, "Linux")

    (target / "arcade-rpc").unlink()
    assert not install.shims_current(target, "Linux")


def test_windows_shims_are_batch_files_with_crlf(tmp_path, monkeypatch):
    """cmd.exe reads these; LF-only line endings and stray CRs both bite."""
    monkeypatch.setattr(install, "info", lambda *a, **k: None)
    venv = tmp_path / ".dogecoinarcade" / "venv"
    target = tmp_path / "winbin"
    install.write_launcher(venv, target, "Windows")
    text = (target / "dogecoinarcade-update.cmd").read_bytes()
    assert text.startswith(b"@echo off\r\n")
    assert b"\r\r\n" not in text
    assert text.count(b"\n") == text.count(b"\r\n"), "every line ends CRLF"
    assert b"exit /b 1" in text


def test_the_repair_script_never_lands_in_a_checkout(tmp_path, monkeypatch):
    """The first run of this left an untracked repair.py in the repository.

    A development machine keeps its environment inside the checkout, so "beside
    the environment" is somebody's working tree -- and the source it would have
    reinstalled from, the sibling `src`, is not there either. Both answers come
    from the checkout instead.
    """
    monkeypatch.setattr(install, "info", lambda *a, **k: None)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    checkout = tmp_path / "dogecoin-arcade"
    (checkout / ".venv" / "bin").mkdir(parents=True)
    (checkout / "pyproject.toml").write_text("[project]\nname='dogecoin-arcade'\n")

    install.write_launcher(checkout / ".venv", tmp_path / "bin", "Linux")
    assert not (checkout / "repair.py").exists(), "not in the working tree"
    repair = tmp_path / "home" / ".dogecoinarcade" / "repair.py"
    assert repair.is_file()
    source = {"__name__": "not_main"}
    exec(compile(repair.read_text(), str(repair), "exec"), source)
    assert source["SOURCE"] == checkout, "reinstall from the checkout itself"


def test_windows_gets_its_command_directory_onto_the_path(tmp_path, monkeypatch):
    """Every command the Windows installer wrote was "not recognized".

    %LOCALAPPDATA%\\DogecoinArcade\\bin is not on anybody's PATH, and the
    installer's answer was a note at the end that told the user to fix it. The
    registry, not setx, because setx truncates a PATH over 1024 characters.
    """
    seen = {}

    class Done:
        returncode = 0
        stdout = "added\n"
        stderr = ""

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return Done()

    monkeypatch.setattr(install.subprocess, "run", fake_run)
    target = tmp_path / "DogecoinArcade" / "bin"

    assert install.add_to_user_path(target, "Linux") is None, "Windows only"
    assert not seen, "nothing to run on a system that already has ~/.local/bin"

    assert install.add_to_user_path(target, "Windows") == "added"
    script = seen["argv"][-1]
    assert seen["argv"][0] == "powershell.exe"
    assert "setx" not in script.lower(), "setx truncates a long PATH"
    assert "SetEnvironmentVariable('Path'" in script
    assert "'User'" in script, "the user's PATH, not the machine's"
    assert str(target) in script

    # An answer that is not one of ours is a failure, not a success.
    Done.stdout = "powershell is very sorry\n"
    assert install.add_to_user_path(target, "Windows") is None

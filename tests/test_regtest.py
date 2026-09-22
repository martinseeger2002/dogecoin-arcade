"""The leftovers of an interrupted run, which the next run should not pay for.

`RegtestNode.stop` removes the datadir, so a directory still sitting in the
temporary folder belongs to a run that never reached its teardown -- a ctrl-C, a
killed pytest, a box that rebooted mid-suite. They are not merely untidy: they
are disk and RAM that the following run is measured against. What the sweep must
never do is stop a node somebody is using, so the two facts it weighs are whether
a process is behind a directory and how long that directory has been there.

`live` is passed in rather than read out of /proc, because what is under test is
what the sweep *does* with each answer /proc is able to give.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

from arcade import regtest as R


def datadir(tmp_path, monkeypatch, name):
    """A datadir-shaped directory, in the folder the sweep actually looks in."""
    monkeypatch.setattr(R.tempfile, "gettempdir", lambda: str(tmp_path))
    path = tmp_path / f"{R.DATADIR_PREFIX}{name}"
    (path / "regtest").mkdir(parents=True)
    return path


def daemon(datadir):
    """A process that looks like a pepecoind to /proc and answers TERM like one.

    The `-datadir=` argument is the entire basis on which a process is allowed to
    be stopped, so it has to be in the argv of something that stays up. Python
    keeps whatever follows `-c` as `sys.argv` instead of trying to read it.
    """
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)",
                             f"-datadir={datadir}"])


def test_a_datadir_with_nothing_behind_it_goes(tmp_path, monkeypatch):
    gone = datadir(tmp_path, monkeypatch, "leftover")
    other = tmp_path / "pytest-of-robin"            # nobody's node, and no prefix
    other.mkdir()
    said = R.reap_leftovers(live={})
    assert not gone.exists(), said
    assert other.exists(), "a directory without the prefix is not this sweep's business"
    assert "1 datadir(s)" in said, said


def test_a_daemon_older_than_a_suite_is_stopped_with_its_data(tmp_path, monkeypatch):
    path = datadir(tmp_path, monkeypatch, "stale")
    node = daemon(path)
    try:
        said = R.reap_leftovers(older_than=0.0, live={str(path): node.pid})
        assert node.poll() is not None, "the stale daemon was told to stop"
        assert not path.exists(), said
        assert "1 stale regtest daemon(s)" in said, said
    finally:
        node.kill()
        node.wait()


def test_a_daemon_younger_than_a_suite_is_left_running(tmp_path, monkeypatch):
    """Two runs can share this box, and one must not stop the other's node.

    The directory is a leftover -- nothing in this run made it -- and it will be
    somebody's stale data in an hour. It is still not worth stopping a daemon
    that has been up for less than a suite to reclaim it.
    """
    path = datadir(tmp_path, monkeypatch, "young")
    node = daemon(path)
    try:
        said = R.reap_leftovers(live={str(path): node.pid})
        assert node.poll() is None, "this sweep stopped a node that is not its own"
        assert path.exists(), said
        assert "0 datadir(s)" in said, said
    finally:
        node.kill()
        node.wait()


def test_a_dead_pid_does_not_hold_a_datadir(tmp_path, monkeypatch):
    """A run that died mid-suite leaves a directory and no process.

    The pid is a number the kernel has never handed out, which is what /proc
    amounts to once a daemon is gone: nothing answers to it, so the directory it
    names can only be debris.
    """
    path = datadir(tmp_path, monkeypatch, "dead")
    never = int(Path("/proc/sys/kernel/pid_max").read_text()) - 1
    said = R.reap_leftovers(older_than=0.0, live={str(path): never})
    assert not path.exists(), said
    assert "0 stale regtest daemon(s)" in said, "nothing was stopped -- it was already gone"


def basetemp(base, name, *, age=0.0):
    """A numbered pytest temp directory, backdated by `age` seconds.

    With a file inside it, because these directories are only interesting when
    they hold something -- sqlite files and wallets are what the 645 MB was.
    """
    path = base / name
    (path / "test_a_thing_0").mkdir(parents=True)
    (path / "test_a_thing_0" / "arcade.sqlite").write_bytes(b"x" * 512)
    when = time.time() - age
    os.utime(path, (when, when))
    return path


def test_a_temp_directory_from_a_run_that_never_ended_goes(tmp_path):
    """pytest prunes these at teardown, so a killed run never gets pruned.

    The other sweep's own directory is in the same folder and is not the same
    thing as a leftover: it is this run's, and deleting it takes the tests with
    it.
    """
    base = tmp_path / "pytest-of-robin"
    stale = basetemp(base, "pytest-100", age=86_400)
    mine = basetemp(base, "pytest-101")
    said = R.reap_stale_basetemps(base=base, mine=mine)
    assert not stale.exists(), said
    assert mine.exists(), "this run's own directory is never a candidate"
    assert "1 stale pytest temp" in said, said


def test_a_temp_directory_younger_than_a_suite_is_left(tmp_path):
    """Two runs can share this box, and one must not delete the other's files.

    A directory written to a minute ago is somebody's suite in progress -- most
    likely a gated commit running alongside this one -- and it is worth less than
    the certainty of never removing a live one.
    """
    base = tmp_path / "pytest-of-robin"
    other = basetemp(base, "pytest-200", age=60)
    mine = basetemp(base, "pytest-201")
    said = R.reap_stale_basetemps(base=base, mine=mine)
    assert other.exists(), said
    assert "1 left young" in said, said


def test_the_pointer_is_not_a_directory_to_delete(tmp_path):
    """`pytest-current` is a symlink at this run's own directory.

    Following it would make every sweep look like it was about to delete the
    suite it is running in, which is the one mistake this function cannot make.
    """
    base = tmp_path / "pytest-of-robin"
    mine = basetemp(base, "pytest-300", age=86_400)
    (base / "pytest-current").symlink_to(mine.name)
    said = R.reap_stale_basetemps(base=base, mine=mine)
    assert mine.exists(), "old, but it is this run's own"
    assert (base / "pytest-current").is_symlink(), said

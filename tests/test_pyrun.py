"""A started arcade module imports this arcade's code, never an `arcade/` in the current directory (2026-11-04, found
on the operator's ThinkPad: an old copy unpacked in $HOME was what the live web worker ran)."""
import os
import pathlib
import subprocess
import sys

import arcade
from arcade import pyrun

ROOT = pathlib.Path(arcade.__file__).resolve().parent.parent


def _decoy(tmp_path):
    fake = tmp_path / "arcade"
    fake.mkdir(parents=True)
    (fake / "__init__.py").write_text("print('THE DECOY')\n")
    (fake / "pyrun.py").write_text("print('THE DECOY')\n")
    return tmp_path


def _run(cmd, cwd):
    env = {**os.environ, "PYTHONPATH": str(ROOT)}         # where this test's arcade is, as a venv install would be
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=60).stdout


def test_a_child_started_from_a_directory_holding_another_arcade_runs_this_one(tmp_path, monkeypatch):
    home = _decoy(tmp_path)
    monkeypatch.chdir(home)
    want = str(pathlib.Path(pyrun.__file__).resolve().parent / "__init__.py")
    for cmd in (pyrun.module_cmd("arcade.pyrun"),                             # this interpreter (-P on 3.11+)
                pyrun.module_cmd("arcade.pyrun", python=sys.executable)):    # another one: the bootstrap
        out = _run(cmd, home)
        assert "THE DECOY" not in out and out.strip() == want, (cmd[:3], out)
    # Unprotected, the decoy wins: what the ThinkPad's worker did.
    assert "THE DECOY" in _run([sys.executable, "-m", "arcade.pyrun"], home)


def test_run_from_a_checkout_it_starts_as_it_always_did(monkeypatch):
    monkeypatch.chdir(ROOT)
    assert pyrun.module_cmd("arcade.web", "--x")[1:] == ["-m", "arcade.web", "--x"]


def test_the_update_shim_starts_in_the_venv_so_a_decoy_in_the_cwd_is_never_run(tmp_path):
    """2026-11-04 (the ThinkPad session): the update shim runs `python -c` and `python -m`, which look in the current
    directory first. It now starts in the venv's own folder. The installer's own template, with its last line pointed
    at the probe instead of a real update, run from a directory holding a decoy."""
    sys.path.insert(0, str(ROOT / "installer"))
    try:
        import install
    finally:
        sys.path.remove(str(ROOT / "installer"))
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    (venv / "bin" / "python").chmod(0o755)
    text = install.UPDATER.format(venv=venv, repair=tmp_path / "repair.py", version=install.SHIM_VERSION)
    assert text.index('cd "') < text.index('-c "import arcade.update"'), "the cd comes before either python"
    shim = tmp_path / "dogecoinarcade-update"
    shim.write_text(text.replace("-m arcade.update", "-m arcade.pyrun"))
    home = _decoy(tmp_path / "home")
    out = _run(["sh", str(shim)], home)
    assert "THE DECOY" not in out and "repairing" not in out, out
    assert out.strip() == str(pathlib.Path(pyrun.__file__).resolve().parent / "__init__.py")
    assert 'cd /d "' in install.WIN_UPDATER and install.SHIM_VERSION >= 4

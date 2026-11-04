"""Starting one of the arcade's own modules in a new Python, with the same `arcade` code this process runs
(2026-11-04, found on the operator's ThinkPad by its session).

`python -m arcade.web` puts the current directory FIRST on sys.path. The web service has no working directory of its
own, so it starts in the operator's home; on the ThinkPad that held an old `arcade/` (unpacked from source.tar.gz),
and the live arcade ran that copy, not the installed package, while the updater -- which compares versions, not
where code was imported from -- could not tell.

So a child is started the way this process found `arcade`: where that was the current directory (a source checkout,
the test suite), exactly as before; otherwise with the current directory kept off sys.path (`-P` from Python 3.11,
and the same done by hand before it).
"""
from __future__ import annotations

import os
import sys

#: Before Python 3.11 (no -P): drop sys.path[0] -- '' for -c, the current directory -- then run the module as -m would.
_BOOT = ("import sys, runpy; del sys.path[0]; sys.argv[0] = {module!r}; "
         "runpy.run_module({module!r}, run_name='__main__', alter_sys=True)")


def _from_cwd() -> bool:
    """Whether this process's `arcade` package is the one in the current directory."""
    import arcade
    try:
        here = os.path.realpath(os.getcwd())
    except OSError:                                       # the directory was removed: nothing there to import
        return False
    return os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(arcade.__file__)))) == here


def module_cmd(module: str, *args: str, python: str | None = None) -> list[str]:
    """The argv that runs `python -m module args...` with this process's `arcade`, never a copy that happens to sit in
    the current directory. `python` is another interpreter (the updater's new venv); the rule is decided here."""
    exe = python or sys.executable
    if _from_cwd():
        return [exe, "-m", module, *args]
    if python is None and sys.version_info >= (3, 11):
        return [exe, "-P", "-m", module, *args]
    return [exe, "-c", _BOOT.format(module=module), *args]


if __name__ == "__main__":                                # for the tests: which `arcade` a started child imported
    import arcade
    print(os.path.realpath(arcade.__file__))

"""Whole-file locks on Windows, where `fcntl` does not exist (2026-11-06).

Why: the operator runs the only Windows arcade, and the supervisor, core_copy and xchain import `fcntl` (Linux and
macOS only), so the arcade could not start there. Those modules still use `fcntl.flock` wherever it exists -- every
Linux and macOS node behaves exactly as before -- and fall back to these only where `import fcntl` fails.
`msvcrt.locking` locks a byte range; locking the first byte of the lock file stands in for flock's whole-file lock.
"""
from __future__ import annotations

import time


def lock(fh, wait: bool = True) -> bool:
    """Take the lock on `fh`; False (never an exception) when another process holds it and `wait` is False."""
    import msvcrt
    fh.seek(0)
    while True:
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            if not wait:
                return False
            time.sleep(0.2)


def unlock(fh) -> None:
    import msvcrt
    try:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass

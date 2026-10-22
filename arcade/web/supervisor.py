"""Updates and restarts with no 502 (2026-10-20: "add zero downtime restarts to Dogecoin arcade so that every
node has redundancy").

`arcade-web` is this supervisor: a small process that holds the web port and runs the arcade as its child (a WORKER,
the same program with --worker-fd). It is what systemd starts and keeps; an update never restarts it.

A reload (SIGHUP: `reload()`, the post-merge hook, arcade/update.py, Admin's restart button):
  1. the supervisor starts a second worker with the new code, on the same listening socket;
  2. the new worker does its slow part -- importing, opening every store -- while the old one serves, then says
     "warm" on its pipe and waits for the run lock (a flock beside the arcade's files);
  3. the supervisor tells the old worker to stop (SIGTERM): it takes no new connections, finishes what it has (at most
     GRACEFUL_STOP) and exits, which lets the lock go;
  4. the new worker takes the lock, starts everything that works in the background (the block watcher, the ord
     indexes, the mesh, ...) and serves.
Connections that arrive between 3 and 4 wait in the socket's queue -- held by the supervisor, so never refused -- and
are answered a few seconds late instead of with a 502. Two workers never serve, write or run background work at the
same time: the lock decides.

A worker that dies on its own (a crash, the stall watchdog) is started again at once, without systemd. Should the
supervisor's own code have changed, a reload first runs the new supervisor in its place (exec: the same process,
the same port, the old worker still its child).
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import signal
import socket
import sys
import time
from pathlib import Path

#: Seconds a new worker has to say it is warm before the reload is given up (the old one keeps serving).
WARM_SECONDS = 600
#: Seconds the old worker has to exit after it is told to stop, before it is killed.
STOP_SECONDS = 60
PIDFILE = "web-supervisor.pid"
LOCK = "web.lock"


def _say(text: str) -> None:
    print(f"arcade-web supervisor: {text}", flush=True)


def _code_hash() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def pidfile(home: Path) -> Path:
    return Path(home) / PIDFILE


def running(home: Path) -> int | None:
    """The supervisor serving this arcade home, if one is alive."""
    try:
        pid = int(pidfile(home).read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def reload(home: Path) -> bool:
    """Ask this home's supervisor for a reload with no downtime. False when none runs (an arcade from before the
    supervisor: restart it the old way, once, and it has one from then on)."""
    pid = running(home)
    if pid is None:
        return False
    os.kill(pid, signal.SIGHUP)
    return True


# --------------------------------------------------------------------------- the worker's side

class RunLock:
    """What only one worker of a home may hold: the right to serve and to work in the background."""

    def __init__(self, home: Path):
        Path(home).mkdir(parents=True, exist_ok=True)
        self._f = open(Path(home) / LOCK, "a+")

    def close(self) -> None:
        self._f.close()

    def take(self, wait: bool = True) -> bool:
        try:
            fcntl.flock(self._f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            return True
        except BlockingIOError:
            return False


def worker_ready(ready_fd: int | None, word: bytes) -> None:
    if ready_fd is None:
        return
    try:
        os.write(ready_fd, word + b"\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- the supervisor

class Supervisor:
    def __init__(self, argv: list[str], host: str, port: int, home: Path, listen_fd: int | None = None,
                 worker_pid: int | None = None):
        self.argv, self.host, self.port, self.home = list(argv), host, port, Path(home)
        self.code = _code_hash()
        if listen_fd is None:
            sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
            sock.listen(2048)
            listen_fd = sock.detach()
        os.set_inheritable(listen_fd, True)
        self.fd = listen_fd
        self.worker: tuple[int, int] | None = None       # (pid, read end of its pipe)
        if worker_pid is not None:
            self.worker = (worker_pid, -1)
        self._hup = False
        self._stop = False
        self._backoff = 1.0

    def _spawn(self) -> tuple[int, int]:
        r, w = os.pipe()
        os.set_inheritable(w, True)
        cmd = [sys.executable, "-m", "arcade.web", *self.argv, "--worker-fd", str(self.fd), "--ready-fd", str(w)]
        env = {**os.environ, "ARCADE_SUPERVISOR_PID": str(os.getpid())}
        pid = os.posix_spawn(sys.executable, cmd, env)
        os.close(w)
        return pid, r

    def _wait_word(self, pid: int, r: int, word: bytes, seconds: float) -> bool:
        """Wait for `word` on a worker's pipe; False if it exits or takes too long."""
        deadline = time.monotonic() + seconds
        got = b""
        os.set_blocking(r, False)
        while time.monotonic() < deadline and not self._stop:
            try:
                chunk = os.read(r, 256)
                if chunk == b"":
                    return False                          # it closed its end: gone
                got += chunk
                if word in got:
                    return True
            except BlockingIOError:
                pass
            done, _status = os.waitpid(pid, os.WNOHANG)
            if done:
                return False
            time.sleep(0.1)
        return False

    def _stop_worker(self, pid: int) -> None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + STOP_SECONDS
        while time.monotonic() < deadline:
            done, _status = os.waitpid(pid, os.WNOHANG)
            if done:
                return
            time.sleep(0.1)
        _say(f"worker {pid} did not stop in {STOP_SECONDS}s: killed")
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass

    def _reload(self) -> None:
        if _code_hash() != self.code:
            # The supervisor itself changed: become the new one first (same process, port and child).
            _say("its own code changed: running the new supervisor in its place")
            old = self.worker[0] if self.worker else ""
            os.execv(sys.executable, [sys.executable, "-m", "arcade.web.supervisor", "--resume", str(self.fd),
                                      str(old), "--hup", "--", *self.argv])
        old = self.worker
        pid, r = self._spawn()
        _say(f"reloading: worker {pid} is getting ready beside {old[0] if old else 'none'}")
        if not self._wait_word(pid, r, b"warm", WARM_SECONDS):
            _say(f"worker {pid} did not get ready: the running one keeps serving")
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except (ProcessLookupError, ChildProcessError):
                pass
            os.close(r)
            return
        if old is not None:
            self._stop_worker(old[0])
            if old[1] >= 0:
                os.close(old[1])
        self.worker = (pid, r)
        _say(f"worker {pid} serving")

    def run(self) -> int:
        pidfile(self.home).write_text(str(os.getpid()))
        signal.signal(signal.SIGHUP, lambda *_: setattr(self, "_hup", True))
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: setattr(self, "_stop", True))
        try:
            if self.worker is None:
                self.worker = self._spawn()
            while not self._stop:
                if self._hup:
                    self._hup = False
                    self._reload()
                    continue
                pid = self.worker[0]
                try:
                    done, status = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    done, status = pid, 0
                if done:
                    code = os.waitstatus_to_exitcode(status) if status else 0
                    _say(f"worker {pid} exited ({code}): starting another in {self._backoff:.0f}s")
                    time.sleep(self._backoff)
                    self._backoff = min(self._backoff * 2, 60.0)
                    self.worker = self._spawn()
                    started = time.monotonic()
                    continue
                if self._backoff > 1.0 and time.monotonic() - locals().get("started", 0) > 300:
                    self._backoff = 1.0
                time.sleep(0.2)
            if self.worker is not None:
                self._stop_worker(self.worker[0])
            return 0
        finally:
            try:
                if running(self.home) == os.getpid():
                    pidfile(self.home).unlink()
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    """`python -m arcade.web.supervisor --resume <fd> <worker pid> [--hup] -- <arcade-web args>`: a supervisor that
    took an older one's place (exec), carrying on with its port and its worker."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] != "--resume":
        print("arcade.web.supervisor runs from arcade-web", file=sys.stderr)
        return 2
    fd = int(argv[1])
    old = int(argv[2]) if argv[2] else None
    hup = "--hup" in argv[3:argv.index("--")]
    rest = argv[argv.index("--") + 1:]
    host, port, home = where(rest)
    sup = Supervisor(rest, host, port, home, listen_fd=fd, worker_pid=old)
    sup._hup = hup
    return sup.run()


def where(argv: list[str]) -> tuple[str, int, Path]:
    """The host, port and home an arcade-web command line names (the defaults __main__ uses), without loading the
    app."""
    import argparse
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8420)
    parser.add_argument("--home", default=str(Path.home() / ".dogecoinarcade"))
    args, _ = parser.parse_known_args(argv)
    return args.host, args.port, Path(args.home).expanduser()


if __name__ == "__main__":
    raise SystemExit(main())

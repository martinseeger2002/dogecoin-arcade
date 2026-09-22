"""A disposable regtest node, for tests and local development.

Spawns `pepecoind -regtest` in a temporary datadir owned by the current user, so
it needs no root and never touches the system service or mainnet data. Cookie
auth is used, so no password exists anywhere.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .config import REGTEST, Params, RpcCredentials
from .rpc import RpcClient, RpcTransportError


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


#: Every datadir this module makes carries this prefix and nothing else on the
#: box uses it, which is what lets the leftovers of an interrupted run be told
#: apart from a node somebody cares about. The real ones keep their data in
#: ~/.pepecoin-testnet and /var/lib/pepecoind, so they cannot match it.
DATADIR_PREFIX = "arcade-regtest-"


def _alive(pid: int) -> bool:
    """Is this process still something that could be serving a datadir?

    A zombie is not: it has closed its files and waits only for a parent to ask
    how it died, and `kill(pid, 0)` cannot see the difference. Unreadable is
    answered alive, because the only cost of being wrong that way is leaving one
    more directory for the next run.
    """
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        after = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1]
    except OSError:
        return True
    return after.split(None, 1)[0].strip() != "Z"


def _live_datadirs() -> dict[str, int]:
    """datadir -> pid, for the regtest daemons the kernel still has.

    Read out of /proc rather than from a formatted table: a datadir path cannot
    be mistaken for part of somebody else's arguments. A process that exits
    between being listed and being read is simply not in the answer.
    """
    found: dict[str, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            args = entry.joinpath("cmdline").read_bytes().split(b"\0")
        except OSError:                     # it exited, or it is not ours to read
            continue
        for arg in args:
            text = arg.decode("utf8", "replace")
            if not text.startswith("-datadir="):
                continue
            datadir = text[len("-datadir="):]
            if datadir.rsplit("/", 1)[-1].startswith(DATADIR_PREFIX):
                found[datadir] = int(entry.name)
    return found


def _serving(pid: int, datadir: str) -> bool:
    """Does this process still name this datadir among its own arguments?

    Asked again at the moment of signalling rather than trusted from the scan a
    few lines above, because a pid is a number the kernel hands out again. Being
    wrong here means sending SIGTERM to whatever took the number after the daemon
    that owned it went away, which is somebody's editor, not a node.
    """
    try:
        args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:                       # gone, or not ours to read
        return False
    return f"-datadir={datadir}".encode() in args


def reap_leftovers(older_than: float = 7200.0,
                   live: dict[str, int] | None = None) -> str:
    """Reclaim what an interrupted run left behind, and say what came away.

    `stop` removes the datadir, so a directory still sitting here belongs to a
    run that never reached its teardown -- ctrl-C, a killed pytest, a machine
    that rebooted mid-suite. A directory with no process of ours behind it is
    leftover, full stop: it is only disk, and six of them is a suite whose
    timing depends on what else is running.

    A *process* is a different thing, so it is only old enough to stop once its
    datadir predates a whole suite. Two runs can share this box and one must not
    stop the other's node, so a young daemon is left alone even when it is known
    to be somebody's leftover in an hour.

    `live` is injectable so a test can drive those decisions without a
    /proc of its own.
    """
    running = _live_datadirs() if live is None else live
    now = time.time()
    targets: list[tuple[Path, int | None]] = []
    for path in sorted(Path(tempfile.gettempdir()).glob(DATADIR_PREFIX + "*")):
        try:
            made = path.stat()
        except OSError:                     # it went away as we looked
            continue
        if made.st_uid != os.getuid() or not path.is_dir():
            continue
        pid = running.get(str(path))
        if pid is None or now - made.st_ctime > older_than:
            targets.append((path, pid))
    stopped = 0
    for path, pid in targets:
        if pid is None or not _serving(pid, str(path)):
            continue                        # debris rather than a daemon: nothing to stop
        subprocess.run(["kill", "-TERM", str(pid)], check=False)
        stopped += 1
        for _ in range(20):                 # a daemon takes a moment to notice
            if not _alive(pid):
                break
            time.sleep(0.5)
    removed = 0
    for path, pid in targets:
        if pid is not None and _serving(pid, str(path)):
            continue                        # still serving; leave its data be
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    left = len(targets) - removed
    return (f"reaped {stopped} stale regtest daemon(s) and {removed} datadir(s)"
            + (f", {left} left running" if left else ""))


class RegtestNode:
    """A throwaway regtest pepecoind.

    Use as a context manager; the datadir is removed on exit.
    """

    def __init__(
        self,
        binary: str = "pepecoind",
        cli: str = "pepecoin-cli",
        require_standard: bool = False,
        extra_args: tuple[str, ...] = (),
        allow_peers: bool = False,
    ):
        # require_standard=True passes -acceptnonstdtxn=0, which makes regtest
        # enforce MAINNET standardness rules. That is the only way to prove a
        # transaction would actually relay on mainnet: testnet and regtest both
        # default to fRequireStandard=false (chainparams.cpp:309,408), so they
        # happily accept transactions mainnet would reject.
        self.require_standard = require_standard
        self.extra_args = tuple(extra_args)
        # Isolated by default: a test node must never reach a real network.
        # allow_peers=True opens listening so two local nodes can be joined with
        # connect_to(), which is how message propagation is tested.
        self.allow_peers = allow_peers
        resolved = shutil.which(binary)
        if resolved is None:
            raise RuntimeError(f"{binary!r} not found on PATH")
        self.binary = resolved
        self.cli = shutil.which(cli) or cli
        self.datadir = Path(tempfile.mkdtemp(prefix="arcade-regtest-"))
        self.rpc_port = _free_port()
        self.p2p_port = _free_port()
        self.zmq_hashblock_port = _free_port()
        self.zmq_rawblock_port = _free_port()
        self.zmq_rawtx_port = _free_port()
        self.process: subprocess.Popen[bytes] | None = None
        self._client: RpcClient | None = None

    @property
    def params(self) -> Params:
        """REGTEST with the ports we actually bound.

        Uses dataclasses.replace rather than constructing a fresh Params, so that
        every other field -- crucially the base58 address version bytes -- is
        carried over. Building one by hand silently inherited the mainnet
        defaults, and the indexer then encoded regtest addresses with Pepecoin's
        mainnet "P" prefix, so nothing ever matched.
        """
        return dataclasses.replace(
            REGTEST, rpc_port=self.rpc_port, p2p_port=self.p2p_port
        )

    def start(self, timeout: float = 60.0) -> "RegtestNode":
        self.process = subprocess.Popen(
            [
                self.binary,
                "-regtest",
                f"-datadir={self.datadir}",
                f"-rpcport={self.rpc_port}",
                f"-port={self.p2p_port}",
                "-server=1",
                *(("-listen=1",) if self.allow_peers else ("-listen=0", "-connect=0")),
                "-dnsseed=0",
                "-fallbackfee=0.01",
                "-txindex=1",
                *(("-acceptnonstdtxn=0",) if self.require_standard else ()),
                *self.extra_args,
                f"-zmqpubhashblock=tcp://127.0.0.1:{self.zmq_hashblock_port}",
                f"-zmqpubrawblock=tcp://127.0.0.1:{self.zmq_rawblock_port}",
                f"-zmqpubrawtx=tcp://127.0.0.1:{self.zmq_rawtx_port}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        cookie = self.datadir / "regtest" / ".cookie"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.process.poll() is not None:
                stderr = self.process.stderr.read().decode() if self.process.stderr else ""
                raise RuntimeError(f"pepecoind exited during startup: {stderr[:500]}")
            if cookie.exists():
                user, _, password = cookie.read_text().partition(":")
                client = RpcClient(
                    RpcCredentials("127.0.0.1", self.rpc_port, user, password), timeout=30
                )
                try:
                    client.get_block_count()
                except (RpcTransportError, Exception):
                    time.sleep(0.3)
                    continue
                self._client = client
                return self
            time.sleep(0.2)
        raise RuntimeError(f"regtest node did not become ready within {timeout}s")

    @property
    def rpc(self) -> RpcClient:
        if self._client is None:
            raise RuntimeError("node not started")
        return self._client

    def generate(self, count: int) -> list[str]:
        """Mine `count` blocks to a fresh address.

        Pepecoin v1.1.0 is Bitcoin 0.13-era, where `generate` still exists and
        mines to the node's own wallet. Newer nodes need generatetoaddress, so try
        both rather than assume.
        """
        try:
            return list(self.rpc.call("generate", count))
        except Exception:
            address = self.rpc.call("getnewaddress")
            return list(self.rpc.call("generatetoaddress", count, address))

    def connect_to(self, other: "RegtestNode", timeout: float = 30.0) -> None:
        """Peer with another local node and wait until the link is up."""
        self.rpc.call("addnode", f"127.0.0.1:{other.p2p_port}", "onetry")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.rpc.call("getconnectioncount") > 0:
                return
            time.sleep(0.3)
        raise RuntimeError("nodes did not connect")

    def sync_with(self, other: "RegtestNode", timeout: float = 60.0) -> None:
        """Block until both nodes agree on the tip."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.rpc.get_block_count() == other.rpc.get_block_count():
                return
            time.sleep(0.3)
        raise RuntimeError(
            f"nodes did not converge: {self.rpc.get_block_count()} vs {other.rpc.get_block_count()}"
        )

    def invalidate(self, block_hash: str) -> None:
        """Force a reorg by marking a block invalid."""
        self.rpc.call("invalidateblock", block_hash)

    def reconsider(self, block_hash: str) -> None:
        self.rpc.call("reconsiderblock", block_hash)

    def stop(self) -> None:
        if self._client is not None:
            try:
                self._client.call("stop")
            except Exception:
                pass
            self._client.close()
            self._client = None
        if self.process is not None:
            try:
                self.process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
            if self.process.stderr:
                self.process.stderr.close()
            self.process = None
        shutil.rmtree(self.datadir, ignore_errors=True)

    def __enter__(self) -> "RegtestNode":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

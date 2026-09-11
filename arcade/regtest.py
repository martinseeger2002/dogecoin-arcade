"""A disposable regtest node, for tests and local development.

Spawns `pepecoind -regtest` in a temporary datadir owned by the current user, so
it needs no root and never touches the system service or mainnet data. Cookie
auth is used, so no password exists anywhere.
"""

from __future__ import annotations

import dataclasses
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


class RegtestNode:
    """A throwaway regtest pepecoind.

    Use as a context manager; the datadir is removed on exit.
    """

    def __init__(self, binary: str = "pepecoind", cli: str = "pepecoin-cli"):
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
                "-listen=0",
                "-connect=0",          # never talk to anyone
                "-dnsseed=0",
                "-fallbackfee=0.01",
                "-txindex=1",
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

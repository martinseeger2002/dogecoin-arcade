"""Minimal JSON-RPC client for pepecoind.

Deliberately small: Arcade only needs to read blocks and, much later, hand a
finished transaction to the node for funding, signing and broadcast.

Batching matters. A naive one-call-per-transaction loop is the difference between
an indexer that keeps up and one that does not, especially on a spinning disk.
"""

from __future__ import annotations

import itertools
from typing import Any

import requests

from .config import RpcCredentials


class RpcError(Exception):
    """A JSON-RPC level error returned by the node."""

    def __init__(self, code: int, message: str, method: str = ""):
        self.code = code
        self.message = message
        self.method = method
        super().__init__(f"{method}: [{code}] {message}" if method else f"[{code}] {message}")


class RpcTransportError(Exception):
    """The node could not be reached, or returned something that is not JSON-RPC."""


class RpcClient:
    """A synchronous JSON-RPC client with batch support.

    One TCP connection is reused across calls via requests.Session, which matters
    a great deal when issuing hundreds of thousands of calls.
    """

    def __init__(self, creds: RpcCredentials, timeout: float = 120.0):
        self._creds = creds
        self._timeout = timeout
        self._ids = itertools.count(1)
        self._session = requests.Session()
        self._session.auth = (creds.user, creds.password)
        self._session.headers["Content-Type"] = "application/json"

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "RpcClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def call(self, method: str, *params: Any) -> Any:
        """Issue a single call and return its result."""
        payload = {"jsonrpc": "1.0", "id": next(self._ids), "method": method, "params": list(params)}
        data = self._post(payload)
        if data.get("error"):
            err = data["error"]
            raise RpcError(err.get("code", -1), err.get("message", ""), method)
        return data.get("result")

    def batch(self, calls: list[tuple[str, list[Any]]]) -> list[Any]:
        """Issue many calls in one round trip.

        Results come back in the order `calls` was given, not the order the node
        chose to answer in -- we re-sort by id rather than trusting the ordering,
        because the JSON-RPC spec does not guarantee it.

        An error in any individual call raises, rather than returning a sentinel:
        a half-applied batch is exactly the kind of thing that produces silent
        state divergence.
        """
        if not calls:
            return []

        payload = []
        order: dict[int, int] = {}
        for index, (method, params) in enumerate(calls):
            call_id = next(self._ids)
            order[call_id] = index
            payload.append({"jsonrpc": "1.0", "id": call_id, "method": method, "params": list(params)})

        data = self._post(payload)
        if not isinstance(data, list):
            raise RpcTransportError(f"expected a batch response, got {type(data).__name__}")

        results: list[Any] = [None] * len(calls)
        for item in data:
            index = order.get(item.get("id"))
            if index is None:
                raise RpcTransportError(f"batch response contained unknown id {item.get('id')!r}")
            if item.get("error"):
                err = item["error"]
                raise RpcError(err.get("code", -1), err.get("message", ""), calls[index][0])
            results[index] = item.get("result")
        return results

    def _post(self, payload: Any) -> Any:
        try:
            response = self._session.post(self._creds.url, json=payload, timeout=self._timeout)
        except requests.RequestException as exc:
            raise RpcTransportError(f"cannot reach pepecoind at {self._creds.url}: {exc}") from exc

        # The node answers 500 with a valid JSON-RPC error body for things like
        # "block not found", so parse before deciding the request failed.
        if response.status_code == 401:
            raise RpcTransportError(
                f"authentication rejected by the node at {self._creds.url}. The "
                f"credentials are probably for a different node -- check --datadir "
                f"and --conf point at the same network."
            )
        try:
            return response.json()
        except ValueError as exc:
            raise RpcTransportError(
                f"non-JSON response from pepecoind (HTTP {response.status_code}): {response.text[:200]!r}"
            ) from exc

    # --- convenience wrappers -------------------------------------------------

    def get_block_count(self) -> int:
        return int(self.call("getblockcount"))

    def get_block_hash(self, height: int) -> str:
        return str(self.call("getblockhash", height))

    def get_block_header(self, block_hash: str) -> dict[str, Any]:
        return self.call("getblockheader", block_hash, True)

    def get_block(self, block_hash: str, verbosity: int = 2) -> dict[str, Any]:
        """Fetch a block.

        verbosity=2 returns fully decoded transactions. Pepecoin v1.1.0 is a
        Bitcoin 0.13-era codebase, where getblock took a boolean rather than an
        integer, so callers must not assume verbosity 2 exists -- see
        `get_block_verbose`, which probes once and remembers.
        """
        return self.call("getblock", block_hash, verbosity)

    def get_block_hex(self, block_hash: str) -> str:
        """Fetch the raw serialized block as hex."""
        return str(self.call("getblock", block_hash, 0))

    def get_blockchain_info(self) -> dict[str, Any]:
        return self.call("getblockchaininfo")

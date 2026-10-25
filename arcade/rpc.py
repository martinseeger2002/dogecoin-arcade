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



#: Bumped by every call that gives a node wallet another address, so a list of a wallet's
#: addresses kept for minutes (web.app._ledger_addresses) is dropped the moment it is short.
ADDRESS_MAKERS = frozenset({"getnewaddress", "getrawchangeaddress", "importaddress",
                            "importprivkey", "importpubkey", "importmulti", "importdescriptors"})
ADDRESS_EPOCH = 0
#: Bumped by every broadcast from this process, so a mempool read kept for a few seconds
#: (web.app._pending_feed) shows what was just sent the moment it is sent.
BROADCAST_EPOCH = 0
#: Called with (raw hex, txid) after every broadcast that went through: the Doginal guard vouches
#: for what the arcade itself sent (doginals.Doginals.vouch_broadcast, 2026-10-15).
AFTER_BROADCAST = None

class RpcError(Exception):
    """A JSON-RPC level error returned by the node."""

    def __init__(self, code: int, message: str, method: str = ""):
        self.code = code
        self.message = message
        self.method = method
        super().__init__(f"{method}: [{code}] {message}" if method else f"[{code}] {message}")


class RpcTransportError(Exception):
    """The node could not be reached, or returned something that is not JSON-RPC."""


def _numbered(params: tuple) -> bool:
    """getblock asked with a numeric verbosity (0, 1, 2), not a boolean: what a Bitcoin-0.14 Core refuses."""
    return len(params) >= 2 and isinstance(params[1], int) and not isinstance(params[1], bool)


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
        if method in ADDRESS_MAKERS:
            global ADDRESS_EPOCH
            ADDRESS_EPOCH += 1
        elif method == "sendrawtransaction":
            global BROADCAST_EPOCH
            BROADCAST_EPOCH += 1
        if method == "signrawtransaction" and getattr(self, "_modern_signing", False) and len(params) == 1:
            method = "signrawtransactionwithwallet"
        if method == "getblock" and _numbered(params) and getattr(self, "_boolean_getblock", False):
            return self._getblock_boolean(params[0], int(params[1]))
        payload = {"jsonrpc": "1.0", "id": next(self._ids), "method": method, "params": list(params)}
        data = self._post(payload)
        if data.get("error"):
            err = data["error"]
            # A Core of Bitcoin 0.14's code (Kotia 1.0.0, 2026-10-25) takes getblock's verbosity as a boolean only:
            # 0, 1 and 2 are refused, and its index never read a block. Learned once per client, then answered
            # from `getblock h false|true` and, for 2, each transaction's getrawtransaction.
            if (method == "getblock" and _numbered(params)
                    and "not a boolean" in str(err.get("message", ""))):
                self._boolean_getblock = True
                return self._getblock_boolean(params[0], int(params[1]))
            # Newer Cores (Litecoin 0.21, 2026-10-17) removed `signrawtransaction`; the wallet-only
            # form takes the same single argument. Learned once per client, then asked directly.
            if (err.get("code") == -32601 and method == "signrawtransaction" and len(params) == 1):
                self._modern_signing = True
                return self.call("signrawtransaction", *params)
            raise RpcError(err.get("code", -1), err.get("message", ""), method)
        if (method == "validateaddress" and isinstance(data.get("result"), dict)
                and data["result"].get("isvalid") and "ismine" not in data["result"] and params):
            # Litecoin 0.21 moved ownership to getaddressinfo; callers read .get("ismine") here.
            info = self.call("getaddressinfo", params[0]) or {}
            data["result"].update({k: info[k] for k in ("ismine", "iswatchonly", "pubkey",
                                                         "iscompressed", "isscript") if k in info})
        if method == "sendrawtransaction" and AFTER_BROADCAST is not None and params:
            try:
                AFTER_BROADCAST(str(params[0]), str(data.get("result") or ""))
            except Exception:                         # noqa: BLE001 -- never fail a send that went
                pass
        return data.get("result")

    def _getblock_boolean(self, block_hash: str, verbosity: int) -> Any:
        """`getblock h <verbosity>` on a Core that takes a boolean (see `call`): 0 the raw hex, 1 the block with
        its txids, 2 the block with each transaction decoded as verbosity 2 has it (txindex=1 is needed there)."""
        if verbosity <= 0:
            return self.call("getblock", block_hash, False)
        block = self.call("getblock", block_hash, True)
        if verbosity >= 2 and block and block.get("tx"):
            txids = list(block["tx"])
            decoded: list[Any] = []
            for at in range(0, len(txids), 500):
                decoded += self.batch([("getrawtransaction", [txid, 1]) for txid in txids[at:at + 500]])
            for tx in decoded:
                for key in ("blockhash", "confirmations", "time", "blocktime"):     # not in a verbosity-2 entry
                    tx.pop(key, None)
            block["tx"] = decoded
        return block

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
        except requests.ConnectionError as exc:
            # The common case by far: no node listening. The urllib3 chain
            # underneath is long and tells a user nothing they can act on.
            raise RpcTransportError(
                f"nothing listening on {self._creds.host}:{self._creds.port} "
                f"-- is the node running?"
            ) from exc
        except requests.Timeout as exc:
            raise RpcTransportError(
                f"{self._creds.host}:{self._creds.port} did not respond in "
                f"{self._timeout:.0f}s -- the node may still be starting"
            ) from exc
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

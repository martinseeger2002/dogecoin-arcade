"""Server-side state for the web UI.

The application is permanently **dual-chain** (docs/DECISIONS.md D-012):

    testnet  -> the Messenger. Messages are conversational, not assets, and
                testnet chains get reset, which is acceptable for chat and
                unacceptable for value.
    mainnet  -> the ledger. Tokens, NFTs and PEP balances are assets and belong
                on a chain nobody resets.

Keeping them apart also means a bug in the messaging code can never touch real
value -- a stronger guarantee than care in the messaging code could ever give.

The messaging identity is held **in memory only** while unlocked. The passphrase
is used once to decrypt the key file and then discarded: never stored, never
logged, never written.
"""

from __future__ import annotations

import dataclasses
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import NETWORKS, Params, load_rpc_credentials, verify_connected_chain
from ..messaging.keys import Identity, load_identity
from ..messaging.store import MessageStore
from ..rpc import RpcClient


@dataclass
class ChainContext:
    """One node connection, and what the UI is allowed to do with it."""

    network: str
    role: str                      # "messaging" or "ledger"
    label: str                     # what the user sees
    datadir: Path | None = None
    conf: Path | None = None
    marker: str | None = None

    #: Whether this chain may sign and broadcast. Mainnet runs disablewallet=1
    #: (Phase 0), so it is read-only until the operator deliberately enables a wallet
    #: that holds real money.
    can_spend: bool = True

    @property
    def params(self) -> Params:
        params = NETWORKS[self.network]
        if self.marker:
            params = dataclasses.replace(params, marker_address=self.marker)
        return params

    @property
    def is_mainnet(self) -> bool:
        return self.network in ("main", "doge-main")

    def credentials(self):
        """Explicit configuration if given, otherwise go and find the node.

        Being told where the node is always wins; discovery is the fallback that
        makes the application start with no arguments at all.
        """
        if self.datadir or self.conf:
            return load_rpc_credentials(self.params, conf_path=self.conf, datadir=self.datadir)

        from ..discovery import best
        found = best(self.network)
        if found is None or found.credentials is None:
            from ..discovery import explain
            raise RuntimeError(explain(self.network))
        # Remember it, so the search happens once per process rather than per request.
        self.datadir = found.datadir
        return found.credentials

    def rpc(self) -> RpcClient:
        creds = self.credentials()
        client = RpcClient(creds)
        # Checks reality, not intent: a stray config must not silently point us
        # at the other chain. With two nodes in play this matters more, not less.
        verify_connected_chain(client, self.params)
        return client

    def status(self) -> dict[str, Any]:
        """Node health, or a plain explanation of why we cannot say."""
        try:
            with self.rpc() as rpc:
                info = rpc.get_blockchain_info()
                result = {
                    "online": True,
                    "chain": info.get("chain"),
                    "blocks": info.get("blocks", 0),
                    "headers": info.get("headers", 0),
                    "synced": info.get("blocks") == info.get("headers"),
                    "peers": rpc.call("getconnectioncount"),
                    "wallet": None,
                }
                try:
                    wallet = rpc.call("getwalletinfo")
                    result["wallet"] = {
                        "balance": float(wallet.get("balance", 0)),
                        "immature": float(wallet.get("immature_balance", 0)),
                    }
                except Exception:
                    # disablewallet=1. Expected on mainnet; reported, not hidden.
                    result["wallet"] = None
                return result
        except Exception as exc:
            return {"online": False, "error": str(exc)}


@dataclass
class AppState:
    """Everything the UI needs, shared across requests."""

    home: Path
    messaging: ChainContext
    ledger: ChainContext

    identity: Identity | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    #: Per-process token required on every state-changing form. A local server is
    #: reachable by any process on this machine, including a stray browser tab.
    csrf_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    notice: str | None = None
    notice_kind: str = "info"

    def flash(self, message: str, kind: str = "info") -> None:
        self.notice, self.notice_kind = message, kind

    def take_notice(self) -> tuple[str | None, str]:
        message, kind = self.notice, self.notice_kind
        self.notice = None
        return message, kind

    # --- identity -------------------------------------------------------------

    @property
    def key_path(self) -> Path:
        return self.home / f"{self.messaging.network}.key"

    @property
    def store_path(self) -> Path:
        return self.home / f"{self.messaging.network}.sqlite"

    def store(self) -> MessageStore:
        return MessageStore(self.store_path)

    def unlock(self, passphrase: str) -> None:
        with self._lock:
            self.identity = load_identity(self.key_path, passphrase)

    def lock(self) -> None:
        with self._lock:
            self.identity = None

    @property
    def unlocked(self) -> bool:
        return self.identity is not None

    @property
    def has_key(self) -> bool:
        return self.key_path.exists()

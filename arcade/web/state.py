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
from ..messaging.derive import DerivationError, derive_identity
from ..messaging.keys import Identity
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

    # --- identity, derived from the wallet ------------------------------------
    #
    # There is no passphrase and no key file. The messaging identity is derived
    # from one wallet address, and that address is filed in the wallet under a
    # fixed account name, so the wallet *is* the backup: restore wallet.dat and
    # the same identity comes back on its own. Nothing else needs keeping.

    #: The account the identity address is filed under. Accounts live inside
    #: wallet.dat, which is what makes a restore self-sufficient.
    IDENTITY_ACCOUNT = "arcade-identity"

    def identity_address(self, rpc: RpcClient) -> str:
        """The address this identity is derived from, creating it if needed.

        Prefers an address already recorded for this installation, so upgrading
        never silently changes identity and orphans existing conversations.
        """
        recorded = self.derived_address
        if recorded:
            # File an older identity under the account too, so a future restore
            # from wallet.dat alone can still find it.
            try:
                rpc.call("setaccount", recorded, self.IDENTITY_ACCOUNT)
            except Exception:
                pass
            return recorded

        # `getaccountaddress` hands back a *new* address as soon as the current
        # one has been used, so calling it every time would silently change
        # identity and orphan every message ever received. Take the account's
        # existing addresses and pick one deterministically instead; only fall
        # through to creating one when the account is genuinely empty.
        try:
            existing = rpc.call("getaddressesbyaccount", self.IDENTITY_ACCOUNT) or []
        except Exception:
            existing = []
        if existing:
            return sorted(existing)[0]
        return rpc.call("getaccountaddress", self.IDENTITY_ACCOUNT)

    def ensure_identity(self) -> Identity:
        """Derive the messaging identity, setting it up on first use.

        Called wherever an identity is needed. Nothing is asked of the user: if
        the node is up and has a wallet, this succeeds.
        """
        if self.identity is not None:
            return self.identity
        with self.messaging.rpc() as rpc:
            return self.use_derived_identity(self.identity_address(rpc))


    @property
    def derived_address(self) -> str | None:
        """The wallet address this identity is derived from, if it is."""
        if not self.store_path.exists():
            return None
        with self.store() as store:
            return store.get_meta(f"identity_address:{self.messaging.network}")

    def use_derived_identity(self, address: str) -> Identity:
        """Adopt the identity that belongs to `address`, and remember which.

        No key file and no passphrase: the identity is reproduced from the wallet
        whenever it is needed, so the wallet's backup is the identity's backup.
        """
        with self.messaging.rpc() as rpc:
            identity = derive_identity(rpc, address)
            try:
                rpc.call("setaccount", address, self.IDENTITY_ACCOUNT)
            except Exception:
                pass
        with self.store() as store:
            store.set_meta(f"identity_address:{self.messaging.network}", address)
            if store.get_meta(f"identity_height:{self.messaging.network}") is None:
                try:
                    with self.messaging.rpc() as rpc:
                        store.set_meta(f"identity_height:{self.messaging.network}",
                                       str(rpc.get_block_count()))
                except Exception:
                    pass
        with self._lock:
            self.identity = identity
        return identity

    def forget_derived_identity(self) -> None:
        if self.store_path.exists():
            with self.store() as store:
                store.set_meta(f"identity_address:{self.messaging.network}", "")
        self.lock()

    @property
    def has_identity(self) -> bool:
        return self.identity is not None or bool(self.derived_address)

    def try_auto_unlock(self) -> bool:
        """Bring the identity up at startup. Requires nothing from the user.

        Failure here is not an error state to show anyone: it just means the node
        is not up yet, and the next attempt will succeed.
        """
        if self.unlocked:
            return False
        try:
            self.ensure_identity()
            return True
        except Exception:
            return False

    def lock(self) -> None:
        with self._lock:
            self.identity = None

    @property
    def unlocked(self) -> bool:
        return self.identity is not None

    @property
    def has_key(self) -> bool:
        return self.key_path.exists()

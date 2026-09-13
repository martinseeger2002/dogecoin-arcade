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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import NETWORKS, Params, load_rpc_credentials, verify_connected_chain
from ..ledger import LedgerIndex
from ..messaging.derive import (
    DerivationError, derive_identity, resolve_identity_address,
)
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
    #: Per-process secret for the bot RPC (web/rpc.py), handed out through
    #: `rpc_cookie_path` the way bitcoind hands out its .cookie.
    rpc_secret: str = field(default_factory=lambda: secrets.token_hex(32))
    notice: str | None = None
    notice_kind: str = "info"
    #: The port this interface is served on. Needed by the remote-access page,
    #: which has to tell cloudflared where to point, and by the guard that
    #: decides whether a request came from this machine.
    port: int = 8420
    #: The open Cloudflare tunnel, if the user has opened one (arcade/remote.py).
    #: One at a time: a second would be a second door nobody is watching.
    tunnel: Any = None
    #: True while one is being opened. Opening takes seconds -- the edge has to
    #: answer -- and until it does, `tunnel` is still None, so a second press of
    #: the button would start a second cloudflared that nothing afterwards knows
    #: about or can close. The claim is what makes the button one press.
    tunnel_opening: bool = False

    #: Latest known tip per network, and when each was last successfully asked.
    #: Kept so the interface can distinguish "nothing new" from "not looking",
    #: which is the failure the block watcher exists to prevent.
    tips: dict = field(default_factory=dict)
    last_checked: dict = field(default_factory=dict)

    #: Each chain's tip as last seen by its token indexer, by network name and
    #: separately from `tips`: testnet is read by the public-board scanner too,
    #: and the one that looks first must not hide a new block from the other.
    ledger_tips: dict = field(default_factory=dict)
    _ledger_indexes: dict = field(default_factory=dict)
    _token_chain: str | None = None
    #: Token transactions broadcast from here and not yet seen in an indexed
    #: block, so the page can say "on its way" instead of showing nothing.
    pending_tokens: list = field(default_factory=list)
    #: Token transactions prepared and shown but not yet confirmed, by txid,
    #: so that the one confirmed is exactly the one that was shown.
    prepared_tokens: dict = field(default_factory=dict)

    #: Incremented whenever a scan finds something a page would show. Open pages
    #: poll this and refresh when it moves; a block with nothing in it for us
    #: must not reload every browser.
    generation: int = 0

    #: Re-read from the checkout, briefly cached. Compared against the version
    #: the code was imported from, because they can differ: Jinja loads
    #: templates from disk on every request while Python is whatever was
    #: imported at startup. After an update without a restart the server renders
    #: the NEW page against the OLD code, so the interface advertises buttons
    #: whose routes do not exist -- a test machine measured a "Start fresh" button posting
    #: into a 404, and the operator reasonably concluded the feature was broken. That
    #: is worse than plain staleness and needs saying out loud.
    _disk_version: tuple = ("", 0.0)
    DISK_CHECK_SECONDS = 20.0

    def _git_head(self) -> str:
        import subprocess
        for root in (Path(__file__).resolve().parent.parent.parent,
                     Path.home() / ".dogecoinarcade" / "src"):
            try:
                result = subprocess.run(
                    ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                    capture_output=True, text=True, timeout=5)
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()
            except Exception:
                continue
        return ""

    @property
    def installed_version(self) -> str:
        """What is on disk right now, as against what is running."""
        version, when = self._disk_version
        if version and (time.monotonic() - when) < self.DISK_CHECK_SECONDS:
            return version
        found = self._git_head()
        object.__setattr__(self, "_disk_version", (found, time.monotonic()))
        return found

    @property
    def is_stale(self) -> bool:
        running, installed = self.running_version, self.installed_version
        return bool(running and installed and running != installed)

    @property
    def running_version(self) -> str:
        """The commit this process is running, if it can be worked out.

        Shown in the interface because a stale process is otherwise invisible:
        after an update, an `arcade-web` with no service unit keeps serving the
        previous code from memory, and nothing says so. a test machine demonstrated exactly
        that -- every page between the update and a manual restart was the old
        version, and a user would not have noticed.
        """
        import subprocess
        cached = getattr(self, "_version", None)
        if cached is not None:
            return cached
        version = ""
        for root in (Path(__file__).resolve().parent.parent.parent,
                     Path.home() / ".dogecoinarcade" / "src"):
            try:
                result = subprocess.run(
                    ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                    capture_output=True, text=True, timeout=5)
                if result.returncode == 0 and result.stdout.strip():
                    version = result.stdout.strip()
                    break
            except Exception:
                continue
        object.__setattr__(self, "_version", version)
        return version

    #: Held for the whole of a send. A long message can take minutes -- the
    #: wallet may be split and that split has to confirm -- and a browser shows
    #: nothing while it waits, so a second click is the natural thing to do. Two
    #: concurrent sends each select their own outputs without seeing the other's
    #: claims, so they can collide and strand a half-written message on the
    #: chain. Only one at a time, and the second is told why.
    _sending: threading.Lock = field(default_factory=threading.Lock)

    #: Digest and time of the last message sent, so an identical one submitted
    #: moments later is recognised as a double click rather than obeyed.
    _last_send: tuple = ("", 0.0)

    #: How long an identical resend is treated as accidental.
    REPEAT_WINDOW = 120.0

    def begin_send(self) -> bool:
        """Claim the right to send. False if another send is already running.

        Refuses once shutdown has begun, so a restart cannot start work it is
        about to abandon.
        """
        if self.shutting_down:
            return False
        return self._sending.acquire(blocking=False)

    #: Set by the shutdown hook. Send threads are daemons, so the interpreter
    #: does not wait for them, and a test machine caught one writing to the store
    #: seventeen seconds AFTER a graceful `systemctl restart` -- the old process
    #: had released the port while its thread carried on, so two processes
    #: briefly shared the database. Nothing was harmed, but a send outliving its
    #: own service is not a property to discover during a wallet operation.
    shutting_down: bool = False

    def begin_shutdown(self, grace: float = 20.0) -> bool:
        """Stop accepting sends and wait for one in flight to finish.

        Returns True if nothing was in flight or it finished within `grace`.
        False means a send is still running and the process is about to go
        anyway: the caller says so rather than leaving it silent, because the
        pending-send record is then the only thing that knows what happened.
        """
        self.shutting_down = True
        if self._sending.acquire(timeout=grace):
            self._sending.release()
            return True
        return False

    def is_repeat_send(self, digest: str) -> bool:
        """True if this exact message was just sent.

        The lock above stops two sends OVERLAPPING, which is not the same thing:
        a slow send shows nothing while it works, so the second click usually
        arrives after the first has finished, and both complete. That is not
        damaging -- each is a valid message -- but it sends the file twice and
        pays for it twice, which is not what the second click meant.
        """
        last, when = self._last_send
        return bool(last) and last == digest and (
            time.monotonic() - when) < self.REPEAT_WINDOW

    def note_send(self, digest: str) -> None:
        self._last_send = (digest, time.monotonic())

    def end_send(self) -> None:
        try:
            self._sending.release()
        except RuntimeError:
            pass                    # not held; nothing to do

    #: What a send in flight is doing, for the conversation to draw. A long send
    #: is minutes of work, so the browser is told to go away and watch rather
    #: than made to hold a request open -- which is what froze the interface and
    #: made a second click look like the thing to do.
    send_progress: dict = field(default_factory=dict)

    def start_progress(self, peer_hex: str, total: int, estimate: str) -> None:
        with self._lock:
            self.send_progress = {
                "peer": peer_hex, "total": total, "done": 0,
                "note": "preparing", "estimate": estimate,
                "started": time.time(), "updated": time.time(),
                "finished": False, "error": "",
            }

    #: A send reporting nothing for this long is treated as gone. In-memory
    #: progress does not survive a restart, but a browser polling for it does --
    #: so a bar could sit at 0%% for ever with no thread behind it. A send that
    #: is genuinely working reports on every chunk and every wait, so silence
    #: this long means the thread is not there any more.
    PROGRESS_STALE_AFTER = 900.0

    def update_progress(self, done: int | None = None, note: str | None = None) -> None:
        with self._lock:
            if not self.send_progress:
                return
            if done is not None:
                self.send_progress["done"] = done
            if note is not None:
                self.send_progress["note"] = note
            self.send_progress["updated"] = time.time()

    def live_progress(self) -> dict:
        """Progress as a browser should see it, with abandoned sends marked.

        Checked on read rather than by a timer: there is no thread left to run
        one, which is the whole problem.
        """
        with self._lock:
            progress = dict(self.send_progress)
        if not progress or progress.get("finished"):
            return progress
        last = progress.get("updated") or progress.get("started", 0)
        if time.time() - last > self.PROGRESS_STALE_AFTER:
            progress["finished"] = True
            progress["error"] = (
                "this send stopped reporting -- the application was probably "
                "restarted while it was working. Anything already on the chain "
                "is listed above and can be finished from there.")
        return progress

    def finish_progress(self, error: str = "") -> None:
        with self._lock:
            if self.send_progress:
                self.send_progress["finished"] = True
                self.send_progress["error"] = error
        self.generation += 1

    def clear_progress(self) -> None:
        with self._lock:
            self.send_progress = {}

    def bump_generation(self) -> None:
        with self._lock:
            self.generation += 1

    def remote_tunnel(self):
        """The tunnel if it is still open, and None the moment it is not.

        Checked rather than remembered: it closes on its own deadline, and
        cloudflared can die on its own too. Anything asking whether the door is
        open must get the truth now, not what was true when it was opened.
        """
        with self._lock:
            tunnel = self.tunnel
            if tunnel is not None and not tunnel.alive():
                self.tunnel = None
                return None
            return tunnel

    def set_tunnel(self, tunnel) -> None:
        with self._lock:
            self.tunnel = tunnel

    def claim_tunnel(self) -> bool:
        """Take the right to open one. False if a tunnel is open or opening."""
        with self._lock:
            if self.tunnel_opening:
                return False
            if self.tunnel is not None and self.tunnel.alive():
                return False
            self.tunnel_opening = True
            return True

    def release_tunnel(self) -> None:
        """Give the claim back, whether the tunnel opened or not."""
        with self._lock:
            self.tunnel_opening = False

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

    # --- the token indexes ----------------------------------------------------
    #
    # Tokens are indexed on BOTH chains, always. Mainnet is the ledger (D-012)
    # and testnet is where anyone can try tokens for nothing (D-016); the
    # Tokens page shows one at a time and the chain tag on it switches. Both
    # indexes run whether or not they are being looked at, so the switch shows
    # a current index rather than one that starts catching up when clicked.

    @property
    def token_chains(self) -> list[ChainContext]:
        """The chains tokens are indexed on: the ledger first, then testnet."""
        chains = [self.ledger]
        if self.messaging.network != self.ledger.network:
            chains.append(self.messaging)
        return chains

    @property
    def rpc_cookie_path(self) -> Path:
        return self.home / "rpc.cookie"

    def write_rpc_cookie(self) -> Path:
        """Write this process's RPC cookie; done once at startup."""
        from .rpc import write_cookie
        write_cookie(self.rpc_cookie_path, self.rpc_secret)
        return self.rpc_cookie_path

    def rpc_chain(self, which: str) -> "ChainContext | None":
        """The chain `/rpc/main` or `/rpc/test` speaks for, if it is indexed."""
        for chain in self.token_chains:
            if which == ("main" if chain.is_mainnet else "test"):
                return chain
        return None

    @property
    def token_chain_path(self) -> Path:
        return self.home / "tokens-chain"

    @property
    def token_chain(self) -> ChainContext:
        """The chain the Tokens page is showing. Mainnet unless switched.

        The choice is kept in a file so that it survives a restart: someone who
        switched to testnet to try things should not find themselves looking
        at mainnet again after an update.
        """
        if self._token_chain is None:
            try:
                self._token_chain = self.token_chain_path.read_text().strip()
            except OSError:
                self._token_chain = self.ledger.network
        for chain in self.token_chains:
            if chain.network == self._token_chain:
                return chain
        return self.ledger

    def switch_token_chain(self, network: str) -> ChainContext:
        for chain in self.token_chains:
            if chain.network == network:
                self._token_chain = network
                try:
                    self.token_chain_path.write_text(network + "\n")
                except OSError:
                    pass                    # remembered for this run only
                return chain
        raise ValueError(f"tokens are not indexed on {network!r}")

    def token_index(self, chain: ChainContext) -> LedgerIndex:
        """The token index for `chain`, built on first use.

        Holds no connection of its own (see arcade.ledger), so sharing one
        across the watcher thread and request threads is safe.
        """
        index = self._ledger_indexes.get(chain.network)
        if index is None:
            index = LedgerIndex(self.home / f"{chain.network}-ledger.sqlite",
                                chain.params, chain.rpc)
            self._ledger_indexes[chain.network] = index
        return index

    # --- identity, derived from the wallet ------------------------------------
    #
    # There is no passphrase and no key file. The messaging identity is derived
    # from one wallet address, and that address is filed in the wallet under a
    # fixed account name, so the wallet *is* the backup: restore wallet.dat and
    # the same identity comes back on its own. Nothing else needs keeping.

    def identity_address(self, rpc: RpcClient) -> str:
        """The address this identity is derived from, choosing one on first use.

        Delegates to `resolve_identity_address`, which the CLI uses too. They had
        separate implementations and diverged: the pin lived here, the CLI never
        read it, and the same wallet answered as two different people depending
        on which half you asked.
        """
        with self.store() as store:
            return resolve_identity_address(rpc, store, self.messaging.network)

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
        """The pinned address this identity is derived from, if one is set."""
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

    # --- how the user introduces themselves ------------------------------------

    @property
    def profile_name(self) -> str:
        """The name sent to a new correspondent. Empty until the user sets one."""
        if not self.store_path.exists():
            return ""
        with self.store() as store:
            return store.get_meta("profile_name") or ""

    def set_profile_name(self, name: str) -> None:
        """Store the name, refusing one that cannot be published whole.

        Checked here as well as in the form, because a browser's maxlength is a
        convenience rather than a guarantee. Refused rather than trimmed: the
        whole reason this exists is that a name was once cut in silence and
        published permanently.
        """
        from ..messaging.envelope import MAX_ANNOUNCE_NAME_CLASS_B

        cleaned = (name or "").strip()
        encoded = cleaned.encode()
        if len(encoded) > MAX_ANNOUNCE_NAME_CLASS_B:
            raise ValueError(
                f"that name is {len(encoded)} bytes and the limit is "
                f"{MAX_ANNOUNCE_NAME_CLASS_B}. Shorten it rather than have it "
                f"cut for you.")
        with self.store() as store:
            store.set_meta("profile_name", cleaned)

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

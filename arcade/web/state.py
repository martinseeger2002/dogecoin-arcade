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
from .. import accounts as accounts_lib
from ..ledger import LedgerIndex
from ..messaging.derive import (
    DerivationError, derive_identity, resolve_identity_address,
)
from ..messaging.keys import Identity
from ..messaging.store import MessageStore
from ..rpc import RpcClient

#: The wallet this node signs with, which is the lane every send in `app.py`
#: claims until it is told otherwise. An account's lane is its own public key.
NODE_LANE = "node"


class SendQueue:
    """One send at a time per wallet, rather than one send at a time per node.

    The single lock this replaces was correct while the node had one wallet and
    wrong the moment it had more than one: what two sends must never do is
    choose from the same pool of coins, and that is a property of a wallet, not
    of a process. A forty-transaction inscription run out of the node's wallet
    has nothing to argue about with an account's post, and blocked it anyway
    (docs/multi-user.md §3).

    Nothing here queues. A claim is refused in the same instant it always was,
    and the caller says so in words, because a send that waits inside a request
    is what froze the interface and made a second click look like the thing to
    do. The order *within* one account is kept elsewhere -- an account's own
    coins are remembered as in-flight by `arcade.web.account`, which is what
    stops a second transaction picking a coin the first already spent.

    A lane is made the first time it is claimed and kept afterwards. Dropping
    one when it is released would allow a transaction to hold a lock that is no
    longer in the dictionary, and a lock nobody can release is a wallet that
    never sends again. A node capped at fifty accounts cannot grow this list
    into anything worth the risk of shrinking it.
    """

    def __init__(self) -> None:
        self._lanes: dict[str, threading.Lock] = {}
        #: Guards the dictionary only, and is never held across an acquire.
        self._guard = threading.Lock()

    def _lane(self, name: str, create: bool) -> threading.Lock | None:
        with self._guard:
            lock = self._lanes.get(name)
            if lock is None and create:
                lock = self._lanes[name] = threading.Lock()
            return lock

    def begin(self, lane: str = NODE_LANE) -> bool:
        """Claim a wallet. False if that wallet is already mid-send."""
        return self._lane(lane, create=True).acquire(blocking=False)

    def end(self, lane: str = NODE_LANE) -> None:
        lock = self._lane(lane, create=False)
        if lock is None:
            return                     # never claimed; nothing to release
        try:
            lock.release()
        except RuntimeError:
            pass                       # not held; nothing to do

    def held(self) -> list[str]:
        """Which wallets are mid-send, so a page can say who is busy."""
        with self._guard:
            return [name for name, lock in self._lanes.items() if lock.locked()]

    def close(self, grace: float = 20.0) -> bool:
        """Take every lane within `grace` seconds, then give them all back.

        True if nothing was in flight or everything finished in time. It re-reads
        the set each pass, because a lane can be claimed by a send that started
        just before shutdown began -- and one wallet missed here is a thread
        outliving its own service, which is how two processes briefly shared a
        database once already.
        """
        deadline = time.monotonic() + grace
        held: list[threading.Lock] = []
        while True:
            pending = [lock for lock in self._lanes.values()
                       if not any(lock is done for done in held)]
            if not pending:
                for lock in held:
                    lock.release()
                return True
            for lock in pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not lock.acquire(timeout=remaining):
                    for done in held:
                        done.release()
                    return False
                held.append(lock)


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
        return self.network == "main"

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
    #: The port this interface is served on.
    port: int = 8420
    #: Whether this instance is served publicly (arcade/web/door.py). False
    #: is a wallet on somebody's own machine, where everything is theirs;
    #: True is a node on a real domain, where only the public surface is
    #: served and every form is refused. Set once at startup, never from a
    #: page: a door that can be opened by a request is not a door.
    public: bool = False
    #: Whether an inscribed page may ask which wallet is looking at it. The
    #: balances themselves are public either way -- anyone with an index can
    #: look one up -- so this is about the one thing the chain does not say.
    inscription_wallet_access: bool = True

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
    #: The seat register, opened lazily by `accounts()`.
    _accounts: Any = None
    #: Token transactions broadcast from here and not yet seen in an indexed
    #: block, so the page can say "on its way" instead of showing nothing.
    pending_tokens: list = field(default_factory=list)
    #: Token transactions prepared and shown but not yet confirmed, by txid,
    #: so that the one confirmed is exactly the one that was shown.
    prepared_tokens: dict = field(default_factory=dict)

    #: The block being mined from the wallet page, while one is: started (time)
    #: and address. Mining is a minute or more of one core's work, and a
    #: button that goes quiet for a minute gets pressed again; this is what
    #: the page shows instead, and what stops a second press from starting
    #: a second miner (D-025).
    mining: dict | None = None

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

    @property
    def checkout(self) -> Path:
        """The source this instance is running from.

        Either the tree it was imported out of -- a development checkout,
        which is what the publishing machine runs -- or the one the
        installer fetched. Whichever it is, it is what `/clone` packs and
        hands to the next person, so a clone is made from what is actually
        running rather than from a website that may have moved on.
        """
        here = Path(__file__).resolve().parent.parent.parent
        if (here / "arcade").is_dir():
            return here
        return Path.home() / ".dogecoinarcade" / "src"

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

    #: Held for the whole of a send, per wallet. A long message can take
    #: minutes -- the wallet may be split and that split has to confirm -- and a
    #: browser shows nothing while it waits, so a second click is the natural
    #: thing to do. Two concurrent sends from the SAME wallet each select their
    #: own outputs without seeing the other's claims, so they can collide and
    #: strand a half-written message on the chain: one at a time per wallet, and
    #: the second is told why. Wallets that share no coins hold nothing up, which
    #: is the whole difference between lanes and a single lock
    #: (docs/multi-user.md §3).
    sends: SendQueue = field(default_factory=SendQueue)

    #: Digest and time of the last message sent, per lane, so an identical one
    #: submitted moments later is recognised as a double click rather than
    #: obeyed. Keyed by lane because the digest is over the peer and the bytes,
    #: not over who is sending: two accounts writing the same words to the same
    #: friend within the window are two people, not one person's second click,
    #: and a shared record would refuse the second one.
    _last_send: dict = field(default_factory=dict)

    #: How long an identical resend is treated as accidental.
    REPEAT_WINDOW = 120.0

    def begin_send(self, lane: str = NODE_LANE) -> bool:
        """Claim the right to send from `lane`. False if that wallet is sending.

        Refuses once shutdown has begun, so a restart cannot start work it is
        about to abandon.
        """
        if self.shutting_down:
            return False
        return self.sends.begin(lane)

    #: Set by the shutdown hook. Send threads are daemons, so the interpreter
    #: does not wait for them, and a test machine caught one writing to the store
    #: seventeen seconds AFTER a graceful `systemctl restart` -- the old process
    #: had released the port while its thread carried on, so two processes
    #: briefly shared the database. Nothing was harmed, but a send outliving its
    #: own service is not a property to discover during a wallet operation.
    shutting_down: bool = False

    def begin_shutdown(self, grace: float = 20.0) -> bool:
        """Stop accepting sends and wait for every one in flight to finish.

        Returns True if nothing was in flight or they all finished within
        `grace`. False means a send is still running and the process is about to
        go anyway: the caller says so rather than leaving it silent, because the
        pending-send record is then the only thing that knows what happened.
        Every lane is waited for, not just the node's own -- a shutdown that
        missed an account's run is the same thread-outliving-its-service bug,
        wearing somebody else's coins.

        A collection run is waited for separately, and before the lanes, because
        waiting for a lane does not wait for a run: between two pieces it holds
        nothing and is merely asleep waiting for the next block, which is up to a
        minute of looking finished. `Runner.quiesce` pauses it awake and joins
        the thread. The two waits share the one `grace`, so a restart is never
        slower than it already promised to be.
        """
        self.shutting_down = True
        started = time.monotonic()
        # Only if the runs were ever opened: shutdown must not be the thing that
        # creates the collection store on a node that never inscribes anything.
        quiet = self.collections[1].quiesce(grace) if self._collections else True
        left = max(0.0, grace - (time.monotonic() - started))
        # Both halves, not the first one that looks clean: a run that did not
        # come back is precisely when the lanes still have to be drained.
        lanes = self.sends.close(left)
        return quiet and lanes

    def is_repeat_send(self, digest: str, lane: str = NODE_LANE) -> bool:
        """True if this exact message was just sent, by this wallet.

        The lane above stops two sends OVERLAPPING, which is not the same thing:
        a slow send shows nothing while it works, so the second click usually
        arrives after the first has finished, and both complete. That is not
        damaging -- each is a valid message -- but it sends the file twice and
        pays for it twice, which is not what the second click meant.

        Compared per lane, and that is not a detail. The digest covers the peer
        and the bytes, never the sender, so one shared record would tell a second
        person that their message was "just sent" because a stranger typed the
        same words to the same friend a minute ago.
        """
        last, when = self._last_send.get(lane, ("", 0.0))
        return bool(last) and last == digest and (
            time.monotonic() - when) < self.REPEAT_WINDOW

    def note_send(self, digest: str, lane: str = NODE_LANE) -> None:
        self._last_send[lane] = (digest, time.monotonic())

    def end_send(self, lane: str = NODE_LANE) -> None:
        self.sends.end(lane)

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

    @property
    def pages_origin(self) -> str:
        """Where an inscribed page is framed from, or "" for right here.

        A second hostname is what gives a page in the sandbox an origin
        that is not the wallet's, so nothing it does can reach the wallet's
        cookies or its routes -- `arcade/web/door.py` serves that name the
        content and the page API and nothing else. Configured by whoever
        runs the node (`pages_host` in settings.json); with none, pages are
        framed from here, which is the same sandbox minus the extra origin
        and is right for one person's own machine.
        """
        host = str(self.setting("pages_host", "") or "").strip()
        if not host:
            return ""
        return host if "://" in host else f"https://{host}"

    def vault(self):
        """Encrypted wallets by @tag. Bytes this node cannot read."""
        from ..accounts import Vault
        return Vault(self.accounts())

    def screen(self):
        """Content screening (arcade/moderation.py): the `moderation` setting names a
        model to ask. Off when unset. Rebuilt only when the setting changes."""
        from ..moderation import Screen
        import json as _json
        config = self.setting("moderation")
        said = _json.dumps(config, sort_keys=True)
        held = getattr(self, "_screen", None)
        if held is None or held[0] != said:
            self._screen = (said, Screen(self.home, config))
        return self._screen[1]

    def faucet(self):
        """The record of what the faucet has given, and its limits.

        The amount and the ceiling are settings, so an operator who would
        rather give less -- or nothing -- changes a number rather than the
        code. `faucet: 0` turns it off.
        """
        from ..faucet import Faucet, DAILY_CEILING, GIFT
        gift = self.setting("faucet")
        ceiling = self.setting("faucet_daily")
        return Faucet(self.accounts(),
                      gift=GIFT if gift is None else int(gift),
                      ceiling=DAILY_CEILING if ceiling is None else int(ceiling),
                      # `faucet_real_coins: true` is the deliberate act that
                      # lets a faucet pay on a chain where the coins are
                      # real. Off by default, and capped far lower when on.
                      real_coins=bool(self.setting("faucet_real_coins", False)))

    def credentials(self):
        """The operator's username and password, if one has been set."""
        from ..accounts import Credentials
        return Credentials(self.accounts())

    def account_for(self, token: str):
        """The account a session cookie belongs to, or None.

        On AppState rather than in the routes because the door needs it too,
        and the door is registered before any route exists.
        """
        try:
            return self.accounts().session(token or "")
        except Exception:
            return None

    @property
    def operator(self) -> str:
        """The account this node belongs to, as a hex public key, or "".

        One key, named in settings.json and settable only from the machine
        itself (`/auth/operator`). It is what lets the person who runs a
        node reach their own wallet from outside it: everybody else who
        signs in gets the public surface, because they are not this key.

        Not a second password and not a way in of its own -- it names an
        account that still has to prove itself the ordinary way, with a
        signature over a nonce this node issued (D-147).
        """
        return str(self.setting("operator", "") or "").strip().lower()

    def claim_operator(self, pubkey: str) -> None:
        self.set_setting("operator", (pubkey or "").strip().lower())

    @property
    def public_hosts(self) -> tuple[str, ...]:
        """The names this node answers to publicly.

        Set by whoever runs it (`public_hosts` in settings.json, one name or
        a list). A request arriving at one of them is served the public
        surface however it got here -- and a request carrying an edge's own
        headers is too, so a name nobody remembered to add still lands on
        the safe side.
        """
        said = self.setting("public_hosts", [])
        if isinstance(said, str):
            said = [said]
        return tuple(str(name).rsplit(":", 1)[0].strip().lower()
                     for name in said if str(name).strip())

    @property
    def pages_hostname(self) -> str:
        """Just the name, for comparing against a Host header.

        The setting may be written either way -- a bare name is the ordinary
        case, a full origin is what a test or a non-standard port needs --
        so the comparison is made on the one part that is in both.
        """
        origin = self.pages_origin
        if not origin:
            return ""
        return origin.split("://", 1)[-1].split("/")[0].rsplit(":", 1)[0].lower()

    def flash(self, message: str, kind: str = "info") -> None:
        self.notice, self.notice_kind = message, kind

    def take_notice(self) -> tuple[str | None, str]:
        message, kind = self.notice, self.notice_kind
        self.notice = None
        return message, kind

    # --- identity -------------------------------------------------------------

    #: What the automatic update last decided, and when. Written by the
    #: watcher, read by the Overview: a thing that runs on its own has to be
    #: able to say when it last ran (D-084).
    #:
    #: On disk as well as in memory, because the success case ENDS THIS
    #: PROCESS: an update restarts arcade-web, so a status held only in
    #: memory is destroyed by the very event it was meant to record. The one
    #: outcome a person most wants to see is the one that could not survive
    #: to be shown (D-088).
    STATUS_FILE = "update-status.json"

    def set_update_status(self, status: dict) -> None:
        import json
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            (self.home / self.STATUS_FILE).write_text(json.dumps(status))
        except OSError:
            pass
        self._update_status = status

    @property
    def update_status(self) -> dict | None:
        import json
        if getattr(self, "_update_status", None):
            return self._update_status
        try:
            return json.loads((self.home / self.STATUS_FILE).read_text())
        except Exception:
            return None

    def my_tag(self) -> str:
        """This wallet's @tag as the chain has it, or "" -- never a guess."""
        try:
            # The chain the MESSAGES are on, which is where a tag is claimed
            # (D-032). It asked the ledger chain, found no tag table entry for
            # this address on mainnet, and answered "" for a wallet that holds
            # one -- which is why this node never announced a release.
            chain = next((c for c in self.token_chains
                          if c.network == self.messaging.network), None)
            if chain is None:
                return ""
            return self.token_index(chain).tag_of(self.derived_address or "") or ""
        except Exception:
            return ""

    def post(self, text: str) -> str:
        """One post on the feed, in one transaction.

        Short by construction: it refuses rather than chunks, because a post
        that does not fit one transaction is a post with a file in it, and a
        file posted to the feed becomes an inscription with the post
        carrying its link (D-138).

        Was `post_to_board(channel, text)` until the board became a feed and
        the release notice stopped being a post (D-147). There are no
        channels to name any more.
        """
        from ..messaging import group
        from ..messaging.sender import MessageSender, funded_address

        chain = self.messaging
        # No nickname: the @tag is the byline, read from the chain, so
        # nobody can type a name they do not hold (D-138).
        plan = group.plan(group.GroupPost(channel="", nickname="", text=text))
        if plan.transactions != 1:
            raise ValueError(
                "that is too long for one transaction -- put a file in it "
                "instead, or say less")
        with chain.rpc() as rpc:
            sender = MessageSender(rpc, chain.params, public_only=True)
            address = funded_address(rpc, mainnet=chain.is_mainnet)
            prepared = sender.prepare(address, plan.payloads[0],
                                      class_c=plan.class_c, change_address=address)
            txid = sender.broadcast(prepared)
        with self.store() as store:
            store.add_group_post(chain.network, "", txid, 0, int(time.time()),
                                 address, "", text, mine=True, txids=[txid])
        self.bump_generation()
        return txid

    def broadcast_release(self, revision: str) -> str:
        """Say that a version exists, as machine talk rather than a post.

        It was a board post in a channel, which put the one message nobody
        reads beside the ones people do -- and once the board became a feed
        there was nowhere honest to put it (D-147). Same short path, same
        lack of authority: the signature on the manifest is what decides
        what installs.
        """
        from .. import release as releaselib
        from ..messaging.sender import MessageSender, funded_address

        chain = self.messaging
        payload = releaselib.build_notice(revision)
        with chain.rpc() as rpc:
            sender = MessageSender(rpc, chain.params, public_only=True)
            address = funded_address(rpc, mainnet=chain.is_mainnet)
            prepared = sender.prepare(address, payload, change_address=address)
            txid = sender.broadcast(prepared)
        self.bump_generation()
        return txid

    def announce_instance(self, domain: str) -> dict:
        """Say on the chain who runs this arcade: its domain and revision, paid
        for by its FEE ADDRESS (arcade/instance.py). The fee address has to pay
        for it itself -- the funding input is the proof -- so an address with no
        coins is refused rather than quietly replaced by another one.
        """
        from .. import instance as instancelib
        from ..messaging.sender import MessageSender, funded_address

        chain = self.messaging
        fee_address = self.derived_address
        if not fee_address:
            raise ValueError("this node has no fee address yet")
        revision = self.running_version or ""
        payload = instancelib.build(domain, revision)
        with chain.rpc() as rpc:
            sender = MessageSender(rpc, chain.params, public_only=True)
            address = funded_address(rpc, prefer=fee_address, mainnet=chain.is_mainnet)
            if address != fee_address:
                raise ValueError(
                    f"the fee address {fee_address} has no coins to pay for this. "
                    "Send it a coin first: the announcement has to come FROM it, "
                    "because that is what proves who made it.")
            prepared = sender.prepare(address, payload, change_address=address)
            txid = sender.broadcast(prepared)
        self.bump_generation()
        return {"txid": txid, "domain": instancelib.clean_domain(domain),
                "revision": revision, "fee_address": fee_address}

    def send_feed_act(self, kind: int, target: str, text: str = "") -> str:
        """Put one like, reply, share, edit or delete on the chain.

        The same short path a board notice takes: one payload, one
        transaction, no chunking and nobody watching a progress bar. A feed
        action is small by construction -- a like is 39 bytes -- and one that
        did not fit would be one that had gone wrong (D-138).

        Written down here at broadcast with height 0, so the page moves the
        moment the button is pressed and the scan upserts the real height when
        the block lands. That is what `add_feed_act` is shaped for.
        """
        from ..messaging import feed
        from ..messaging.sender import MessageSender, funded_address

        chain = self.messaging
        payload = feed.build(kind, target, text)
        with chain.rpc() as rpc:
            sender = MessageSender(rpc, chain.params, public_only=True)
            address = funded_address(rpc, mainnet=chain.is_mainnet)
            prepared = sender.prepare(address, payload, change_address=address)
            txid = sender.broadcast(prepared)
        with self.store() as store:
            store.add_feed_act(chain.network, txid, kind, target, address,
                               text, 0, int(time.time()), mine=True)
        self.bump_generation()
        return txid

    def send_tip(self, network: str, post_txid: str, to_address: str,
                 sats: int) -> str:
        """Pay somebody for a post, in one transaction that says which post.

        The coins move and the same transaction carries the note, so nothing
        has to be reconciled afterwards and nobody pays twice (D-138). On
        either chain: a tip is a payment, and payments are what the ledger
        chains are for -- which is why this takes a network rather than
        assuming the messaging one.
        """
        from ..messaging import feed
        from ..messaging.sender import MessageSender, funded_address

        chain = next((c for c in self.token_chains if c.network == network), None)
        if chain is None:
            raise ValueError(f"nothing is indexed on {network}")
        payload = feed.build(feed.TIP, post_txid)
        with chain.rpc() as rpc:
            sender = MessageSender(rpc, chain.params, public_only=True)
            address = funded_address(rpc, mainnet=chain.is_mainnet)
            prepared = sender.prepare(address, payload, change_address=address,
                                      pay=((int(sats), to_address),))
            txid = sender.broadcast(prepared)
        # Recorded against the chain the transaction itself lives on, which is
        # the key the scan will confirm it under: the row used to be filed
        # under the messaging chain's name wherever the coins had moved, and
        # then the scan filed the same transaction AGAIN under its own chain,
        # and the first row sat there unconfirmed for ever. A tip is gathered
        # across chains by the feed page, and held per chain (feed.py).
        with self.store() as store:
            store.add_feed_act(chain.network, txid, feed.TIP,
                               post_txid, address, f"{chain.network}:{int(sats)}",
                               0, int(time.time()), mine=True,
                               amount=int(sats), paid_on=chain.network)
        self.bump_generation()
        return txid

    # --- settings -------------------------------------------------------------

    #: Preferences that outlive a restart and are nobody's business but this
    #: machine's. A small JSON file rather than a table: there are two of them,
    #: they are read on page renders, and a file can be read by a person who
    #: wants to know what their wallet is doing without opening a database.
    SETTINGS_FILE = "settings.json"

    def settings(self) -> dict:
        try:
            import json
            return json.loads((self.home / self.SETTINGS_FILE).read_text())
        except Exception:
            return {}

    def setting(self, name: str, default: Any = None) -> Any:
        value = self.settings().get(name)
        return default if value is None else value

    def set_setting(self, name: str, value: Any) -> None:
        import json
        data = self.settings()
        data[name] = value
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / self.SETTINGS_FILE).write_text(json.dumps(data, sort_keys=True, indent=1))

    @property
    def key_path(self) -> Path:
        return self.home / f"{self.messaging.network}.key"

    @property
    def store_path(self) -> Path:
        return self.home / f"{self.messaging.network}.sqlite"

    def store(self) -> MessageStore:
        return MessageStore(self.store_path)

    # --- who may use this node ------------------------------------------------

    @property
    def accounts_path(self) -> Path:
        """The seat register (arcade/accounts.py).

        Not per network and not per era. A seat is permission to use this
        machine, and the browser key that holds one has no opinion about
        which chain is being indexed or what height the floor is at -- so a
        floor move leaves this file alone, and everybody keeps their seat
        and loses their tag, which is what a floor move means.
        """
        return self.home / "accounts.sqlite"

    def accounts(self):
        """The register, opened once per process.

        One connection, shared: SQLite serialises writes itself, the rows
        are few, and a connection per request would mean a file handle per
        page draw for a table that is read on every one of them.
        """
        with self._lock:
            if self._accounts is None:
                from ..accounts import Accounts
                # `seats` in settings.json overrides the default. Written
                # this way rather than with `or` so that an operator can
                # set it to 0 and close signups without the fallback
                # quietly reopening them.
                seats = self.setting("seats")
                self._accounts = Accounts(
                    self.accounts_path,
                    seats=accounts_lib.SEATS if seats is None else int(seats))
            return self._accounts

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
        """The chain the Tokens page is showing.

        The choice is kept in a file so that it survives a restart: someone who
        switched to testnet to try things should not find themselves looking
        at mainnet again after an update.

        With no choice made -- a fresh install -- it is the chain this wallet's
        IDENTITY is on, which is the messaging chain. It used to be the ledger
        chain, and the first honest fresh install showed why that was wrong: a
        wallet claims its @tag on testnet, publishes its key there, holds its
        contacts there, and was then shown token and inscription pages for
        mainnet, where nothing of its own exists or can. An inscription made
        two minutes earlier was indexed and invisible, and it reads as a node
        that is not working rather than a page that is not looking (D-134).
        """
        if self._token_chain is None:
            try:
                self._token_chain = self.token_chain_path.read_text().strip()
            except OSError:
                self._token_chain = self.messaging.network
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
            index = LedgerIndex(self.ledger_index_path(chain),
                                chain.params, chain.rpc)
            self._ledger_indexes[chain.network] = index
        return index

    # --- collections ----------------------------------------------------------

    _collections: tuple = ()
    _approvals: Any = None
    _pagestore: Any = None
    _talk: Any = None
    _offers: Any = None
    _listings: Any = None

    def ledger_index_path(self, chain: "ChainContext") -> Path:
        """Where a chain's index file is. One definition, because the
        bootstrap publishes the same file the indexer writes."""
        return self.home / f"{chain.network}-ledger.sqlite"

    def chain_named(self, network: str) -> ChainContext:
        for chain in self.token_chains:
            if chain.network == network:
                return chain
        raise ValueError(f"no chain named {network!r} is indexed")

    @property
    def collections(self) -> tuple:
        """(jobs, runner) for collection runs, built on first use.

        The store is a file under the home, so a run survives the process:
        `runner.resume_interrupted()` at startup picks up whatever was cut off.
        """
        if not self._collections:
            from ..collections import Jobs, Runner
            jobs = Jobs(self.home / "collections.sqlite")
            runner = Runner(
                jobs, chain_for=self.chain_named,
                index_for=lambda network: self.token_index(self.chain_named(network)),
                send_lock=(self.begin_send, self.end_send))
            self._collections = (jobs, runner)
        return self._collections

    @property
    def approvals(self):
        """Sends asked for by pages and bots, waiting for the user's yes."""
        if self._approvals is None:
            from ..approvals import Requests
            self._approvals = Requests(self.home / "approvals.sqlite")
        return self._approvals

    @property
    def pagestore(self):
        """What this wallet remembers for each inscribed page."""
        if self._pagestore is None:
            from ..pagestore import PageStore
            self._pagestore = PageStore(self.home / "pagedata.sqlite")
        return self._pagestore

    @property
    def talk(self):
        """What each inscribed page has said to other nodes (arcade/nodetalk.py)."""
        if self._talk is None:
            from ..nodetalk import Talk
            self._talk = Talk(self.home / "nodetalk.sqlite")
        return self._talk

    @property
    def offers(self):
        """The shops' book: every offer this wallet has made (arcade/swap.py)."""
        if self._offers is None:
            from ..swap import Offers
            self._offers = Offers(self.home / "swaps.sqlite")
        return self._offers

    @property
    def listings(self):
        """The other book, on purpose (arcade/listings.py).

        `offers` is what this wallet signed with its own key, which means this
        node holds that key. `listings` is what this node was handed: a
        signature somebody else made in their own browser, for a piece this
        node cannot move. Two states the multi-user plan exists to keep apart,
        so they are two files and two properties, and nothing that reads one
        should reach the other by accident.
        """
        if self._listings is None:
            from ..listings import Listings
            self._listings = Listings(self.home / "listings.sqlite")
        return self._listings

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

    def home_address(self, chain: Any) -> str:
        """The one address this wallet uses on a chain, for everything.

        One address a chain, holding the coins, the tokens and the NFTs.
        Every awkward thing a wallet of many addresses does came from the
        alternative: a token stranded on a receiving address with no coins to
        move it, an NFT on one address and the published key on another, a
        send that needed three transactions because the balance was in three
        piles (D-046). Address reuse links a wallet's activity together for
        anyone reading the chain, which is the price, and this ledger is
        address-keyed and public anyway.

        On the messaging chain it is the address the identity is derived
        from. On the other chain it is a named account address, which a
        Core wallet keeps for as long as the wallet exists.
        """
        if chain.network == self.messaging.network:
            found = self.derived_address
            if found:
                return found
        with chain.rpc() as rpc:
            return str(rpc.call("getaccountaddress", "arcade-identity"))

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
        """How this wallet introduces itself: its @tag, or nothing at all.

        There used to be a name typed into a box here. Two names for one
        person is one too many, and the typed one was the weaker: nobody
        could check it, it had to be published to be useful, and publishing
        it put an unverifiable claim on the chain for ever. The tag does the
        same job and can be checked, so it is the only name this wallet
        gives out for itself (D-034).
        """
        home = self.derived_address or ""
        if not home:
            return ""
        for chain in self.token_chains:
            if chain.network == self.messaging.network:
                try:
                    found = self.token_index(chain).tag_of(home)
                except Exception:
                    return ""
                return f"@{found}" if found else ""
        return ""

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

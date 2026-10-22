"""A Core that is behind lends out its reads: the chain is borrowed from an arcade that shares it until it catches up.

2026-10-20: "If the block chain is being re-scanned or being synced on any arcade, it should automatically
switch over to borrowing somebody else's core until the re-scan is finished."

For a chain this arcade runs itself (Admin -> Chains "Run here"; a chain set to "borrow" is borrowed anyway and never
comes here), every CHECK_SECONDS this module asks the arcade's own Core how far it is. While it is BEHIND and a linked
arcade shares the chain (arcade/mesh/coreshare.py), ChainContext.rpc() hands out a CatchingUpRpc:

- reads of the chain and broadcasts (`BORROWS`: what a sharer serves) go to the sharer, through BorrowedRpc, so
  they are proved from the raw bytes exactly as on a chain set to "borrow";
- everything else -- above all the node's OWN wallet (getbalance, listunspent, signrawtransactionwithwallet,
  sendtoaddress, ...) -- stays on this arcade's Core, which answers it as it is (or with its "still loading" error);
  a sharer has no wallet of ours and must never be asked for one;
- a broadcast is also handed to this Core, quietly, so its wallet knows the coins it just spent;
- `scantxoutset` is refused: a Core part way through its chain holds part of the coin set, and callers fall back to
  reading the blocks the index holds.

"Behind" is any of:
- the RPC answers error -28 (warming up: loading the block index, verifying blocks, -reindex, a -rescan at start);
- the wallet is rescanning (`getwalletinfo().scanning` is not false);
- initial block download, or `verificationprogress` < PROGRESS_DONE;
- headers more than HEADERS_AHEAD ahead of blocks (catching up after it was off). Six: a Core at the tip is 0-2
  blocks short of its headers while a block propagates and is checked (state.chain_sync allows 2); more than six
  is deeper than that lag and than any ordinary reorg, so it is truly catching up, not just one block late.
The last two are overruled when the sharers' own tip says this Core is already there (within AT_TIP blocks): a
quiet test chain's verificationprogress sits below 0.9999 at its tip, because it is an estimate made from time.

Back to this Core by itself once it is at the tip again, after BACK_AFTER caught-up checks in a row (a minute), so it
does not flap. Never borrowed while no linked arcade shares the chain: then this Core is used as it is, and Admin ->
Chains says why. One log line per switch, never per request. A client already handed out keeps its routing for its
life, so a caller mid-way through a session never changes Core under its feet.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .rpc import RpcClient, RpcError, RpcTransportError

log = logging.getLogger("arcade.cores")

#: How often the arcade's own Core is asked how far it is, per chain.
CHECK_SECONDS = 30.0
#: Headers this far ahead of blocks: catching up (see the module's note).
HEADERS_AHEAD = 6
#: Below this, still verifying the chain (the same mark state.chain_sync uses).
PROGRESS_DONE = 0.9999
#: Caught-up checks in a row before reads come back from the sharer.
BACK_AFTER = 2
#: Within this many blocks of the sharers' tip counts as at the tip, whatever verificationprogress says.
AT_TIP = 2
#: How long a check waits for this Core (it is busy while syncing; a check that times out tells nothing).
CHECK_TIMEOUT = 10.0
#: How long the first look at a chain waits for its first check before answering with what is known.
FIRST_WAIT = 2.0
#: Calls that never go to the sharer and are refused while behind: a part-synced Core's answer is wrong.
REFUSED = frozenset({"scantxoutset"})


def borrows() -> frozenset:
    """What goes to the sharer while this Core is behind: what a sharer serves (reads of the chain and the mempool,
    fee estimates, broadcasting a signed transaction, a test chain's mining work), and decoding a transaction, which
    BorrowedRpc does itself. Everything else stays here."""
    from .mesh import coreshare
    return coreshare.SERVED | coreshare.MINED | {"decoderawtransaction"}


@dataclass
class Reading:
    """What one check found."""
    behind: bool | None              # None: this Core could not be asked (not running, no credentials)
    why: str = ""                    # "syncing", "re-indexing", ... ; or why it could not be asked
    blocks: int | None = None
    headers: int | None = None
    progress: float | None = None


@dataclass
class Catchup:
    """One chain's switch."""
    network: str
    borrowing: bool = False
    reading: Reading | None = None
    caught_up: int = 0                # caught-up checks in a row while borrowing
    sharer: str = ""                  # who reads were borrowed from at the last check
    sharers: int = 0
    tip: int | None = None            # the sharers' best tip at the last check
    checked: float = 0.0              # time.monotonic() of the last check
    lock: threading.Lock = field(default_factory=threading.Lock)
    thread: threading.Thread | None = None


_STATES: dict[str, Catchup] = {}
_STATES_LOCK = threading.Lock()

#: Chains whose Core is rescanning for a wallet import (2026-10-21): network -> why. A 1.14 Core holds its
#: lock for the whole rescan and answers nothing, not even "how far are you?", so a check would only see a Core that
#: does not answer and would not switch: the import says so here instead. Kept by `store` (the arcade's settings),
#: so a restart mid-rescan still borrows. Let go of only when no import is running here (`_running`) and the Core
#: answers a check again.
HOLDS: dict[str, str] = {}
_running: set[str] = set()
#: (load() -> dict, save(dict)), set by the app; None in tests that do not keep them.
store: tuple[Callable[[], dict], Callable[[dict], None]] | None = None
_loaded = False


def _load() -> None:
    global _loaded
    if not _loaded and store is not None:
        try:
            HOLDS.update(store[0]() or {})
        except Exception:                                      # noqa: BLE001 -- kept from now on
            pass
        _loaded = True


def _save() -> None:
    if store is not None:
        try:
            store[1](dict(HOLDS))
        except Exception:                                      # noqa: BLE001
            log.warning("could not keep the rescan holds", exc_info=True)


def hold(chain, why: str) -> bool:
    """`chain`'s Core is about to rescan: borrow its reads from now on (True when a linked arcade shares it)."""
    _load()
    HOLDS[chain.network] = why
    _running.add(chain.network)
    _save()
    peers, tip = _sharers(_share(), chain.network)
    st = _state(chain.network)
    with st.lock:
        st.reading, st.checked, st.caught_up = Reading(True, why), time.monotonic(), 0
        st.sharers, st.tip, st.sharer = len(peers), tip, (peers[0] if peers else "")
        if peers and not st.borrowing:
            st.borrowing = True
            log.warning("%s: this Core is %s; reading the chain from arcade %s until it is done",
                        chain.params.label, why, peers[0][:12])
    return bool(peers)


def finished(network: str) -> None:
    """The import's own call into the Core came back. The hold goes at the next check that the Core answers."""
    _running.discard(network)


def held(network: str) -> str | None:
    _load()
    return HOLDS.get(network)


def can_borrow(chain) -> bool:
    """Whether a linked arcade shares `chain` right now."""
    return bool(_sharers(_share(), chain.network)[0])


def _state(network: str) -> Catchup:
    with _STATES_LOCK:
        return _STATES.setdefault(network, Catchup(network))


def forget() -> None:
    """Every chain back to unknown (tests)."""
    with _STATES_LOCK:
        _STATES.clear()


def _share():
    from .mesh import coreshare
    return coreshare.CURRENT


def _local_client(chain) -> RpcClient:
    """A plain client of this arcade's own Core, with a short timeout (no chain check: it may be loading)."""
    return RpcClient(chain.credentials(), timeout=CHECK_TIMEOUT)


def read_core(rpc) -> Reading:
    """Ask a Core how far it is: getblockchaininfo, then (only when that looks done) getwalletinfo."""
    try:
        info = rpc.call("getblockchaininfo") or {}
    except RpcError as exc:
        if exc.code == -28:
            # "Loading block index...", "Verifying blocks...", "Rescanning...", "Activating best chain...".
            word = (exc.message or "starting").strip().rstrip(".").rstrip("…")
            return Reading(True, f"starting up ({word.lower()})")
        return Reading(None, f"answered {exc}")
    except (RpcTransportError, OSError, RuntimeError, ValueError) as exc:
        return Reading(None, str(exc))
    if not isinstance(info, dict):
        return Reading(None, "getblockchaininfo answered something else")
    blocks, headers = int(info.get("blocks") or 0), int(info.get("headers") or 0)
    progress = float(info.get("verificationprogress") or 0)
    said = Reading(False, "", blocks, headers, progress)
    if headers - blocks > HEADERS_AHEAD:
        said.behind, said.why = True, "catching up" if info.get("initialblockdownload") is False else "syncing"
        return said
    if info.get("initialblockdownload") or progress < PROGRESS_DONE:
        said.behind, said.why = True, "syncing"
        return said
    try:
        wallet = rpc.call("getwalletinfo") or {}
    except Exception:                                          # noqa: BLE001 -- no wallet here (disablewallet)
        wallet = {}
    scanning = wallet.get("scanning", False)
    if scanning not in (False, None):
        said.behind, said.why = True, "rescanning its wallet"
    return said


def _sharers(share, net: str) -> tuple[list[str], int | None]:
    if share is None:
        return [], None
    try:
        peers = list(share.sharers(net))
        tips = [int(t.get("h")) for t in (share.tips(net) or {}).values() if t.get("h") is not None]
    except Exception:                                          # noqa: BLE001
        return [], None
    return peers, (max(tips) if tips else None)


def check(chain, share=None, client: Callable[[Any], Any] | None = None) -> Catchup:
    """One check of `chain`'s own Core, and the switch it implies. Synchronous."""
    share = share if share is not None else _share()
    st = _state(chain.network)
    _load()
    why = HOLDS.get(chain.network)
    if why is not None and chain.network in _running:
        reading = Reading(True, why)                         # rescanning here, now: the Core is not even asked
    else:
        try:
            rpc = (client or _local_client)(chain)
        except Exception as exc:                               # noqa: BLE001 -- no credentials: not asked
            reading = Reading(None, str(exc))
        else:
            try:
                reading = read_core(rpc)
            finally:
                try:
                    rpc.close()
                except Exception:                              # noqa: BLE001
                    pass
        if why is not None:
            if reading.behind is None:
                reading = Reading(True, why)                 # still busy (a rescan from before a restart)
            else:
                HOLDS.pop(chain.network, None)               # it answers again: the rescan is over
                _save()
                log.warning("%s: the Core's rescan is over", chain.params.label)
    peers, tip = _sharers(share, chain.network)
    if (reading.behind and reading.blocks is not None and tip is not None and reading.why != "rescanning its wallet"
            and (reading.headers or 0) - reading.blocks <= HEADERS_AHEAD and reading.blocks >= tip - AT_TIP):
        reading.behind, reading.why = False, ""             # at the sharers' tip: a quiet chain's estimate, not lag
    label = chain.params.label
    with st.lock:
        st.reading, st.checked, st.sharers, st.tip = reading, time.monotonic(), len(peers), tip
        st.sharer = peers[0] if peers else ""
        if reading.behind:
            st.caught_up = 0
            if peers and not st.borrowing:
                st.borrowing = True
                log.warning("%s: this Core is %s%s; reading the chain from arcade %s until it catches up",
                            label, reading.why, _where(reading, tip), peers[0][:12])
            elif not peers and st.borrowing:
                st.borrowing = False
                log.warning("%s: this Core is still %s, but no linked arcade shares the chain any more; "
                            "using this Core as it is", label, reading.why)
        elif reading.behind is False and st.borrowing:
            st.caught_up += 1
            if st.caught_up >= BACK_AFTER:
                st.borrowing, st.caught_up = False, 0
                log.warning("%s: this Core has caught up%s; reading the chain from it again", label,
                            _where(reading, tip))
        # behind None (not answering): stays as it is -- borrowing goes on if it was, and a Core that is away is
        # not a reason to start (its reads fail as they always have).
    return st


def _where(reading: Reading, tip: int | None) -> str:
    if reading.blocks is None:
        return ""
    top = max(reading.headers or 0, tip or 0)
    return f" (block {reading.blocks:,} of {top:,})" if top else f" (block {reading.blocks:,})"


def _due(st: Catchup) -> bool:
    return not st.checked or time.monotonic() - st.checked >= CHECK_SECONDS


def _refresh(chain, share=None, wait: float = 0.0) -> Catchup:
    """Start a check on a thread of its own when one is due (never two at once for a chain); wait up to `wait`
    for it when this chain was never checked."""
    st = _state(chain.network)
    with st.lock:
        running = st.thread is not None and st.thread.is_alive()
        if _due(st) and not running:
            st.thread = threading.Thread(target=_quiet_check, args=(chain, share), daemon=True,
                                         name=f"arcade-catchup-{chain.network}")
            st.thread.start()
        thread = st.thread
    if wait and thread is not None and not st.checked:
        thread.join(wait)
    return st


def _quiet_check(chain, share) -> None:
    try:
        check(chain, share)
    except Exception:                                          # noqa: BLE001 -- a check never takes the arcade down
        log.debug("catch-up check of %s failed", chain.network, exc_info=True)
        _state(chain.network).checked = time.monotonic()


def borrowing(chain) -> bool:
    """Whether `chain`'s reads go to a sharer right now. Cheap: a check runs at most every CHECK_SECONDS, on its own
    thread; nothing is asked of anybody while no linked arcade shares the chain and it is not borrowing already."""
    share = _share()
    st = _STATES.get(chain.network)
    if not (st and st.borrowing):
        peers, _ = _sharers(share, chain.network)
        if not peers:
            return False
    if held(chain.network) and not (st and st.checked):
        check_soon = _refresh(chain, share, wait=FIRST_WAIT)     # a hold from before a restart: decided at once
        return check_soon.borrowing
    st = _refresh(chain, share, wait=FIRST_WAIT)
    return st.borrowing


def known_behind(network: str) -> bool:
    """This Core was behind at its last check (so it is not offered to other arcades)."""
    st = _STATES.get(network)
    return bool(st and st.reading and st.reading.behind)


def say(chain, mesh_on: bool = True) -> str | None:
    """One line for Admin -> Chains, or None when this Core is at the tip (or cannot be asked: the row says so)."""
    st = _refresh(chain, wait=FIRST_WAIT)
    r = st.reading
    if r is None or r.behind is None:
        return None
    where = _where(r, st.tip)
    if st.borrowing and not r.behind:
        left = BACK_AFTER - st.caught_up
        return (f"caught up{where}; reads come back to this Core after {left} more check"
                f"{'s' if left != 1 else ''} (every {CHECK_SECONDS:.0f} s)")
    if not r.behind:
        return None
    if st.borrowing:
        return f"{r.why}: borrowed from arcade {st.sharer[:12]} until it catches up{where}"
    if not mesh_on:
        return f"{r.why}{where}; the mesh is off, so this Core is used as it is"
    return f"{r.why}{where}; no linked arcade shares this chain, so this Core is used as it is"


class CatchingUpRpc(RpcClient):
    """A client for a chain whose own Core is behind: reads and broadcasts to the sharer (`borrowed`, a BorrowedRpc),
    everything else -- the wallet -- to this Core (`local`, made on first need by the given factory)."""

    def __init__(self, local: Callable[[], RpcClient], borrowed: RpcClient):
        import itertools
        self._make_local = local
        self._local_client: RpcClient | None = None
        self._borrowed = borrowed
        self._borrows = borrows()
        self._ids = itertools.count(1)
        self._creds = None
        self._timeout = getattr(borrowed, "_timeout", 60.0)

    def close(self) -> None:
        if self._local_client is not None:
            self._local_client.close()
        self._borrowed.close()

    def _local(self) -> RpcClient:
        if self._local_client is None:
            self._local_client = self._make_local()
        return self._local_client

    def where(self, method: str) -> str:
        """"borrow", "local" or "refuse"."""
        if method in REFUSED:
            return "refuse"
        return "borrow" if method in self._borrows else "local"

    def _post(self, payload: Any) -> Any:
        single = isinstance(payload, dict)
        items = [payload] if single else list(payload)
        out: list[Any] = [None] * len(items)
        far, near = [], []
        for i, item in enumerate(items):
            where = self.where(str(item.get("method")))
            if where == "refuse":
                out[i] = {"id": item.get("id"), "result": None,
                          "error": {"code": -28, "message": "this Core is still catching up, so its coin set is "
                                                            "not complete yet"}}
            else:
                (far if where == "borrow" else near).append(i)
        if far:
            got = self._borrowed._post([items[i] for i in far])
            for i, answer in zip(far, got):
                out[i] = answer
            self._tell_local([items[i] for i in far], got)
        if near:
            if single:
                out[0] = self._local()._post(items[0])
            else:
                got = self._local()._post([items[i] for i in near])
                by_id = {a.get("id"): a for a in (got if isinstance(got, list) else [got])}
                for i in near:
                    out[i] = by_id.get(items[i].get("id")) or {
                        "id": items[i].get("id"), "result": None,
                        "error": {"code": -1, "message": "this Core gave no answer to it"}}
        return out[0] if single else out

    def _tell_local(self, asked: list, got: list) -> None:
        """A transaction the sharer broadcast, handed to this Core too (on a thread: a busy Core never holds up the
        answer), so its wallet sees the coins it just spent and does not offer them again."""
        sent = [dict(item) for item, answer in zip(asked, got)
                if item.get("method") == "sendrawtransaction" and not (answer or {}).get("error")]
        if not sent:
            return
        make = self._make_local

        def hand() -> None:
            try:
                with make() as rpc:
                    for item in sent:
                        try:
                            rpc._post(item)
                        except Exception:                      # noqa: BLE001 -- it learns it from the chain
                            pass
            except Exception:                                  # noqa: BLE001 -- a loading Core
                pass
        threading.Thread(target=hand, daemon=True, name="arcade-catchup-broadcast").start()

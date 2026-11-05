"""Mining a test chain's blocks on this machine's CPU, through its own node, until told to stop (2026-11-04, SadFrog:
"build DigiByte testnet mining into the arcade like L test's ... and start mining to my account").

The node mines (`generatetoaddress`, arcade/messaging/miner.py), in short batches so a stop is heard. A chain whose
Params name `mine_algos` (DigiByte testnet: odo, qubit and skein) is mined with each of them side by side, a thread
each: each algorithm has its own difficulty, so a CPU wins the most blocks on the ones little else mines. Blocks pay
the address given, the operator's own account; their coins can be spent once the chain is 100 blocks further.

One run at a time, in `state.mining`, the same slot the full arcade's Wallet page mines in (web/app.py /fund), so
neither starts while the other runs and that page's progress follows this run too. Test chains only, and only a
chain whose node runs here: Miner refuses a chain of real coins, and a borrowed chain's node is another arcade's."""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)

#: A thread gives up after this many failures in a row (a node that stopped, a chain that is not ready).
MOST_FAILURES = 3


class MineError(ValueError):
    """Mining could not start, said for the operator."""


def start(state, chain, address: str, blocks: int = 0) -> dict:
    """Start mining `chain` to `address` until stopped, or until `blocks` blocks are found (0: no limit)."""
    from .messaging.miner import Miner
    if getattr(chain, "borrowed", False):
        raise MineError(f"{chain.label} is borrowed from another arcade here: only a chain whose node runs on this "
                        f"machine is mined here")
    if getattr(state, "mining", None) is not None:
        raise MineError("this arcade is mining already: `mining` says how it goes, `mine stop` stops it")
    if not address:
        raise MineError(f"your account has no {chain.label} address here")
    with chain.rpc() as rpc:
        Miner(rpc, chain.params)                           # refuses a chain of real coins
        height = rpc.get_block_count()
    algos = tuple(getattr(chain.params, "mine_algos", ()) or ()) or (None,)
    run = {"started": time.time(), "address": address, "tries": 0, "rate": 0.0, "expected": 0, "stop": False,
           "net": chain.network, "chain": chain.label, "want": max(int(blocks), 0), "from_height": height,
           "found": [], "rates": {}, "tries_by": {}, "errors": {}, "threads": len(algos), "lock": threading.Lock()}
    state.mining = run
    for algo in algos:
        threading.Thread(target=_work, args=(state, chain, run, algo), name=f"arcade-mine-{algo or chain.network}",
                         daemon=True).start()
    return status(state)


def _done(run: dict) -> bool:
    return bool(run["stop"]) or bool(run["want"] and len(run["found"]) >= run["want"])


def _work(state, chain, run: dict, algo: str | None) -> None:
    from .messaging.miner import Miner
    name, failures = algo or "default", 0
    try:
        while not _done(run):
            base = run["tries_by"].get(name, 0)

            def progress(tries: int, rate: float) -> None:
                with run["lock"]:
                    run["tries_by"][name] = base + tries
                    run["rates"][name] = int(rate)
                    run["tries"] = sum(run["tries_by"].values())
                    run["rate"] = float(sum(run["rates"].values()))
            try:
                with chain.rpc() as rpc:
                    block = Miner(rpc, chain.params).mine_one(run["address"], on_progress=progress,
                                                              stop=lambda: _done(run), algo=algo)
                failures = 0
            except Exception as exc:                       # noqa: BLE001 -- said in `mining`, tried again
                failures += 1
                run["errors"][name] = str(exc)[:200]
                log.warning("mining %s (%s): %s", chain.network, name, exc)
                if failures >= MOST_FAILURES:
                    return
                time.sleep(5)
                continue
            if block:
                with run["lock"]:
                    run["found"].append({"hash": block, "algo": name, "at": time.time()})
                log.info("mined %s block %s (%s) to %s", chain.label, block, name, run["address"])
    finally:
        with run["lock"]:
            run["threads"] -= 1
            last = run["threads"] == 0
        if last:
            state.last_mined = {"net": chain.network, "chain": chain.label, "ended": time.time(),
                                "outcome": "stopped" if run["stop"] else "found" if run["found"] else "failed",
                                "blocks": [f["hash"] for f in run["found"]]}
            if getattr(state, "mining", None) is run:
                state.mining = None
            bump = getattr(state, "bump_generation", None)
            if bump:
                bump()


def stop(state) -> bool:
    """Ask the run to stop after the batch each thread is on (seconds). False when nothing is mining."""
    run = getattr(state, "mining", None)
    if run is None:
        return False
    run["stop"] = True
    return True


def status(state) -> dict:
    """What the run is doing, or how the last one ended."""
    run = getattr(state, "mining", None)
    if run is None:
        return {"mining": False, **({"last": getattr(state, "last_mined", None)}
                                    if getattr(state, "last_mined", None) else {})}
    with run.get("lock") or threading.Lock():
        found = list(run.get("found", []))
        out = {"mining": True, "chain": run.get("chain"), "to": run.get("address"),
               "minutes": round((time.time() - run["started"]) / 60, 1), "blocks": len(found),
               "hashes_per_second": dict(run.get("rates", {})), "stopping": bool(run.get("stop"))}
    if run.get("want"):
        out["until"] = run["want"]
    if found:
        out["last_block"] = found[-1]["hash"]
        out["by_algo"] = {a: sum(1 for f in found if f["algo"] == a) for a in sorted({f["algo"] for f in found})}
    if run.get("errors"):
        out["errors"] = dict(run["errors"])
    return out

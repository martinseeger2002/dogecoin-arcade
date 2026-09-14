"""One-shot testnet funding by CPU mining.

A user with no coins cannot send anything, and the public faucet's form rejects
valid testnet addresses. Mining is therefore the only self-service route -- but
it should be a **bootstrap, not a background process**:

  * a single block pays 10,000 PEP;
  * the largest possible single-transaction message costs ~0.147 PEP in fees;
  * so one block funds roughly **68,000 maximum-size messages**, or 1.4 million
    short ones.

Continuous mining would peg a CPU core to produce coins nobody will ever spend.
Mine once, stop, and never think about it again.

The awkward part is **coinbase maturity: 240 blocks** (`chainparams.cpp:258`).
Coins are not spendable for roughly four hours of chain time, so funding cannot
be done just-in-time at send. It has to happen early -- which is why `keygen`
offers it, rather than `send` doing it silently at the moment of need.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from ..config import Params, require_messaging_network
from ..rpc import RpcClient, RpcError

log = logging.getLogger(__name__)

COIN = 100_000_000
COINBASE_MATURITY = 240          # chainparams.cpp:258 (testnet, digishield)

# Enough to send thousands of messages; one block is 10,000 PEP, so this is
# reached by a single block and never needs a second.
DEFAULT_TARGET_COINS = 100

# The RPC default of 1,000,000 usually gives up empty: at the difficulty seen on
# testnet a block needs roughly 2.2 million scrypt hashes on average
# (getdifficulty x 2^32; rpc/blockchain.cpp GetDifficulty). This is the most
# one mine_one will try before giving up -- twenty-odd expected blocks' worth,
# so in practice it finds one.
MAX_TRIES = 50_000_000

# `generatetoaddress` hashes in the RPC thread and answers only when it is done,
# and the client stops waiting after 120 s (rpc.py). One machine measured
# 1,600 hashes/s, so a 50-million-try call is hours long and the client hangs
# up while the node hashes on, unheard. So: a short first call to measure the
# rate, then calls sized to about SECONDS_PER_CALL of hashing, which keeps each
# inside the timeout and lets the caller count progress and stop in between.
FIRST_TRIES = 20_000
SECONDS_PER_CALL = 30
TRIES_PER_CALL = (5_000, 5_000_000)

# Difficulty 1 is the 0x1d00ffff target (rpc/blockchain.cpp GetDifficulty);
# a block at difficulty d needs d * 2^32 hashes on average.
HASHES_PER_DIFFICULTY = 2 ** 32


class MiningError(Exception):
    """Mining could not proceed."""


@dataclass
class FundingStatus:
    """What the wallet has, and what is still ripening."""

    spendable: float
    immature: float
    blocks_to_maturity: int
    height: int

    @property
    def funded(self) -> bool:
        return self.spendable > 0

    @property
    def pending(self) -> bool:
        return self.immature > 0

    def describe(self) -> str:
        if self.funded:
            return f"{self.spendable:,.2f} PEP spendable"
        if self.pending:
            hours = self.blocks_to_maturity / 60
            return (
                f"{self.immature:,.2f} PEP mined but not yet spendable -- "
                f"{self.blocks_to_maturity} more blocks (~{hours:.1f} h). "
                f"Coinbase outputs mature after {COINBASE_MATURITY} blocks."
            )
        return "no coins"


class Miner:
    """Mines testnet blocks to fund a messaging address."""

    def __init__(self, rpc: RpcClient, params: Params):
        # Belt and braces over the CLI's own check. Mining on mainnet would be
        # both futile and a waste of someone's electricity.
        require_messaging_network(params)
        self.rpc = rpc
        self.params = params

    def status(self) -> FundingStatus:
        info = self.rpc.call("getwalletinfo")
        height = self.rpc.get_block_count()
        spendable = float(info.get("balance", 0))
        immature = float(info.get("immature_balance", 0))

        remaining = 0
        if immature > 0:
            # Find the youngest immature coinbase; that is what gates us.
            best = 0
            for entry in self.rpc.call("listtransactions", "*", 50, 0):
                if entry.get("category") == "immature":
                    best = max(best, int(entry.get("confirmations", 0)))
            remaining = max(0, COINBASE_MATURITY - best)

        return FundingStatus(
            spendable=spendable, immature=immature,
            blocks_to_maturity=remaining, height=height,
        )

    def expected_hashes(self) -> int:
        """How many hashes a block takes on average at the chain's difficulty now."""
        return int(float(self.rpc.call("getdifficulty")) * HASHES_PER_DIFFICULTY) or 1

    def mine_one(
        self,
        address: str,
        on_progress: Callable[[int, float], None] | None = None,
        stop: Callable[[], bool] | None = None,
        max_tries: int = MAX_TRIES,
    ) -> str | None:
        """Attempt a single block. Returns its hash, or None if the attempt failed.

        `generatetoaddress` mines in the calling thread and returns an empty list
        when it exhausts `maxtries` without finding a block, so an empty result
        is a normal outcome to try again rather than an error. It is asked in
        batches (see FIRST_TRIES); after each, `on_progress(tries, rate)` is
        told how many hashes have been tried and how fast, and `stop()` may
        say to give up.
        """
        tries, batch = 0, FIRST_TRIES
        while tries < max_tries:
            started = time.monotonic()
            try:
                result = self.rpc.call("generatetoaddress", 1, address, batch)
            except RpcError as exc:
                raise MiningError(f"the node refused to mine: {exc}") from exc
            tries += batch
            if result:
                return result[0]
            rate = batch / max(time.monotonic() - started, 0.001)
            low, high = TRIES_PER_CALL
            batch = int(min(max(rate * SECONDS_PER_CALL, low), high))
            if on_progress:
                on_progress(tries, rate)
            if stop and stop():
                return None
        return None

    def bootstrap(
        self,
        address: str,
        target_coins: float = DEFAULT_TARGET_COINS,
        max_blocks: int = 2,
        on_attempt: Callable[[int, str], None] | None = None,
    ) -> FundingStatus:
        """Mine until funded, then stop. Does nothing if already funded.

        `max_blocks` is a hard ceiling, not a goal: it exists so a
        misconfiguration cannot leave a machine mining indefinitely.
        """
        status = self.status()
        if status.spendable >= target_coins:
            return status
        if status.immature > 0:
            # Already mined and waiting. Mining more would not make it mature
            # sooner -- maturity is measured in chain height, not in our blocks.
            return status

        mined = 0
        attempts = 0
        start = time.time()
        while mined < max_blocks:
            attempts += 1
            if on_attempt:
                on_attempt(attempts, f"mining (attempt {attempts})")
            block_hash = self.mine_one(address)
            if block_hash:
                mined += 1
                log.info("mined block %s", block_hash)
                if on_attempt:
                    on_attempt(attempts, f"mined {block_hash}")
                break          # one block is ample; see the module docstring
        elapsed = time.time() - start
        log.info("bootstrap finished: %d block(s) in %.0f s", mined, elapsed)
        return self.status()

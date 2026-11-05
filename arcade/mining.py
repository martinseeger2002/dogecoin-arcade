"""Mining on this node, chain by chain (the Mining module, 2026-11-04): what each chain here can be mined with, and how.

Three ways, each where it can be done:
- **The node's own miner** (arcade/testmine.py): a test chain whose node runs here, mined by that node
  (`generatetoaddress`) with each of its algorithms side by side (DigiByte testnet: odo, qubit, skein). `mine` in the
  console; Admin -> Mining's Mine.
- **This machine's CPU** (arcade/blockminer.py): a test chain's work from its node here or one it borrows, hashed here
  in scrypt or SHA-256. Admin -> Mining's Mine, for a chain of one algorithm.
- **ASICs on this network** (arcade/stratum.py): any chain whose node runs here and is not merge-mined, in scrypt or
  SHA-256, mainnets included: solo mining, every block paying the operator's address.

A chain of real coins is never mined on a CPU: it finds nothing worth the power. A merge-mined chain (Dogecoin,
Pepecoin and the AuxPoW chains) is mined beside its parent's work, which nothing here builds yet."""
from __future__ import annotations

from . import stratum


def ways(chain) -> dict:
    """How `chain` can be mined here: {"cpu": {...} or {"why": ...}, "asic": {...} or {"why": ...}}."""
    p = chain.params
    merged = getattr(p, "auxpow_chain_id", None) is not None
    if getattr(p, "real_coins", True):
        cpu = {"why": "real coins: a CPU finds nothing worth its power"}
    elif getattr(p, "mine_algos", ()):
        cpu = ({"why": "its node is another arcade's: the node's own miner mines a chain whose node runs here"}
               if chain.borrowed else {"by": "node", "algos": list(p.mine_algos)})
    elif merged:
        cpu = {"by": "node", "algos": []} if not chain.borrowed else \
            {"why": "merge-mined, and its node is another arcade's"}
    else:
        cpu = {"by": "cpu", "algos": [getattr(p, "pow_algo", "") or "scrypt"]}
    algos = stratum.asic_algos(p)
    if chain.borrowed:
        asic = {"why": "its node is another arcade's: an ASIC mines a chain whose node runs here"}
    elif merged:
        asic = {"why": "merge-mined: an ASIC mines it beside its parent chain's work, which is not built here yet"}
    elif not algos:
        asic = {"why": "no ASIC here hashes its algorithm"}
    else:
        asic = {"algos": list(algos)}
    return {"cpu": cpu, "asic": asic}


def table(state) -> list[dict]:
    """Every chain this arcade has, and how each can be mined here."""
    return [{"net": c.network, "label": c.label, "here": not c.borrowed,
             "real_coins": bool(getattr(c.params, "real_coins", True)), **ways(c)} for c in state.all_chains]

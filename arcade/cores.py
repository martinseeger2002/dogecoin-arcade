"""Which chains' Cores this arcade runs, which it borrows from arcades that share theirs, and which it shares.

2026-10-17: "It should be required to be running Pepe coin test for everyone along with one more
core." Pepecoin testnet carries the Messenger, the @names and the mesh's own vouching, so it is always
run here; every other chain is run here, borrowed through the mesh (arcade/mesh/coreshare.py), or --
for a chain the arcade can do without -- left off. At least one chain besides testnet must be run here,
so every arcade brings a Core to the network as well as taking. Every chain this arcade runs is shared
unless the operator unticks it (Admin -> Chains, one checkbox each).

Settings (settings.json): `core:<net>` = "here" | "borrow" | "off", `share:<net>` = true | false (true when
not set). Which
chains run here or are borrowed is read when the arcade starts; sharing applies within a minute.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import NETWORKS

#: The chain every arcade runs itself: the messaging chain (regtest in tests).
REQUIRED = "test"
HERE, BORROW, OFF = "here", "borrow", "off"
#: Chains the arcade can be without: every add-on chain (Params.addon -- Dogecoin, Litecoin, Litecoin
#: testnet, any chain added as Params). The ledger, Pepecoin mainnet, cannot be off: only borrowed.
OPTIONAL = frozenset(name for name, p in NETWORKS.items() if p.addon)


class CoreRule(ValueError):
    """A choice of Cores the arcade cannot run with."""


def read_settings(home: Path | str) -> dict:
    try:
        got = json.loads((Path(home) / "settings.json").read_text())
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def mode(settings: dict, net: str, default: str = HERE, required: str = REQUIRED) -> str:
    if net == required:
        return HERE
    got = settings.get(f"core:{net}")
    return got if got in (HERE, BORROW, OFF) else default


#: Every Core that runs here is shared unless the operator unticks it (2026-10-18: "turn on node
#: mesh API by default and then the operator can uncheck it").
SHARE_DEFAULT = True


def sharing(settings: dict, net: str) -> bool:
    return bool(settings.get(f"share:{net}", SHARE_DEFAULT))


def check(modes: dict[str, str], required: str = REQUIRED) -> None:
    """Raise CoreRule unless testnet runs here and at least one other chain does too."""
    if modes.get(required, HERE) != HERE:
        raise CoreRule("Pepecoin test always runs on this machine: the Messenger and the @names live there")
    for net, m in modes.items():
        if m not in (HERE, BORROW, OFF):
            raise CoreRule(f"{net}: run here, borrow, or off")
        if m == OFF and net not in OPTIONAL and net != required:
            raise CoreRule(f"{net} cannot be off: it can be run here or borrowed")
    if not any(m == HERE for net, m in modes.items() if net != required):
        raise CoreRule("run at least one more chain's Core here besides Pepecoin test -- "
                       "every arcade brings a Core as well as borrowing")


def rows(settings: dict, chains: list[Any], status: dict | None = None, required: str = REQUIRED) -> list[dict]:
    """Every chain for the admin panel: how it runs now, how it will run after a restart, whether it is
    shared, and what the mesh says about it (coreshare.CoreShare.status)."""
    status = status or {}
    out = []
    for chain in chains:
        net = chain.network
        now = BORROW if getattr(chain, "borrowed", False) else HERE
        out.append({"net": net, "title": chain.title, "label": chain.params.label, "now": now,
                    "chosen": mode(settings, net, now, required), "required": net == required,
                    "optional": net in OPTIONAL, "share": sharing(settings, net), "mesh": status.get(net, {}),
                    "work": "checked" if chain.params.pow_algo else "the sharer's word"})
    return out

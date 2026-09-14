"""Finding a running node without being told where it is.

The application should start with no arguments and work. That means looking in
the places a Pepecoin node actually puts things, on every platform, and trying
each credential source in turn rather than assuming one.

Order of preference for credentials, and why:

  1. **cookie in the datadir** -- regenerated at every start, never stale, and
     requires no shared secret. Only readable if the node runs as this user.
  2. **rpcuser/rpcpassword in a config** -- works across users, which is how a
     system-service node (running as its own user, cookie mode 0600) is reached.
  3. nothing -- report precisely what was tried, because "cannot connect" with
     no detail is the least useful error a program can give.
"""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass, field
from pathlib import Path

from .config import NETWORKS, Params, RpcCredentials, read_node_conf
from .rpc import RpcClient, RpcTransportError

#: Where Pepecoin Core keeps its datadir, per platform -- BOTH of them.
#:
#: The testnet directory has to be listed for every platform, not only the one
#: the author happened to be on. The installer writes testnet to a sibling of
#: the mainnet directory (`installer/install.py: datadirs`), and this only knew
#: the Linux spelling of that: a Windows user with a synced testnet node at
#: %APPDATA%\Pepecoin-testnet was told "Testnet NOT FOUND" while its RPC port
#: answered perfectly well, and had to pass --msg-datadir by hand. Two places
#: encoding the same convention, and only one of them kept up.
def _platform_datadirs() -> list[Path]:
    system = platform.system()
    home = Path.home()
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) / "Pepecoin" if appdata else home / "AppData/Roaming/Pepecoin"
        return [base, base.with_name("Pepecoin-testnet")]
    if system == "Darwin":
        base = home / "Library/Application Support/Pepecoin"
        return [base, base.with_name("Pepecoin-testnet")]
    return [home / ".pepecoin", home / ".pepecoin-testnet"]


#: Additional places worth looking: a system service, and the Linux convention
#: again for anyone running the app on one platform against a datadir copied
#: from another.
EXTRA_DATADIRS = [
    Path.home() / ".pepecoin-testnet",
    Path("/var/lib/pepecoind"),
    Path("/var/lib/pepecoin"),
]


@dataclass
class Candidate:
    """One place a node might be, and what we found there."""

    datadir: Path
    network: str
    credentials: RpcCredentials | None = None
    source: str = ""                 # "cookie" or "config"
    reachable: bool = False
    chain: str | None = None
    height: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def params(self) -> Params:
        return NETWORKS[self.network]


def _cookie_path(datadir: Path, params: Params) -> Path:
    sub = params.datadir_subdir
    return (datadir / sub / ".cookie") if sub else (datadir / ".cookie")


def _exists(path: Path) -> bool:
    """Path.exists() that survives an untraversable parent.

    A system-service datadir is typically mode 0710, so even stat() on a file
    inside it raises PermissionError for other users. That is a normal condition
    here, not an error worth propagating.
    """
    try:
        return path.exists()
    except (PermissionError, OSError):
        return False


def _conf_paths(datadir: Path) -> list[Path]:
    return [datadir / "pepecoin.conf", Path("/etc/pepecoin/pepecoin.conf")]


def _credentials_for(datadir: Path, params: Params) -> tuple[RpcCredentials | None, str, list[str]]:
    problems: list[str] = []

    cookie = _cookie_path(datadir, params)
    if _exists(cookie):
        try:
            user, _, password = cookie.read_text().partition(":")
            return (
                RpcCredentials("127.0.0.1", params.rpc_port, user, password),
                "cookie",
                problems,
            )
        except PermissionError:
            # Very common: the node runs as its own system user and writes the
            # cookie 0600. Not an error, just a reason to try the config next.
            problems.append(f"{cookie} exists but is not readable by this user")
        except OSError as exc:
            problems.append(f"{cookie}: {exc}")

    for conf_path in _conf_paths(datadir):
        if not _exists(conf_path):
            continue
        try:
            conf = read_node_conf(conf_path)
        except Exception as exc:
            problems.append(f"{conf_path}: {exc}")
            continue
        user = conf.get("rpcuser") or conf.get(f"{params.name}.rpcuser")
        password = conf.get("rpcpassword") or conf.get(f"{params.name}.rpcpassword")
        if user and password:
            return (
                RpcCredentials("127.0.0.1", params.rpc_port, user, password),
                f"config ({conf_path})",
                problems,
            )
    if not problems:
        problems.append(f"no cookie and no rpcuser/rpcpassword found under {datadir}")
    return None, "", problems


def probe(datadir: Path, network: str, timeout: float = 5.0) -> Candidate:
    """Look for a node of `network` in `datadir` and see whether it answers."""
    params = NETWORKS[network]
    candidate = Candidate(datadir=datadir, network=network)
    credentials, source, problems = _credentials_for(datadir, params)
    candidate.problems = problems

    if credentials is None:
        return candidate

    candidate.credentials = credentials
    candidate.source = source
    try:
        with RpcClient(credentials, timeout=timeout) as rpc:
            info = rpc.get_blockchain_info()
            candidate.reachable = True
            candidate.chain = info.get("chain")
            candidate.height = info.get("blocks", 0)
    except RpcTransportError as exc:
        candidate.problems.append(str(exc))
    except Exception as exc:
        candidate.problems.append(f"{type(exc).__name__}: {exc}")
    return candidate


def discover(network: str, extra: Path | None = None) -> list[Candidate]:
    """Every place a node of `network` might be, best first.

    Returns candidates whether or not they worked, so a caller can explain what
    was tried instead of only saying that nothing was found.
    """
    seen: set[Path] = set()
    roots: list[Path] = []
    if extra:
        roots.append(Path(extra).expanduser())
    roots.extend(_platform_datadirs())
    roots.extend(EXTRA_DATADIRS)

    results: list[Candidate] = []
    for root in roots:
        root = root.expanduser()
        if root in seen:
            continue
        seen.add(root)
        if not _exists(root):
            continue
        results.append(probe(root, network))

    results.sort(key=lambda c: (not c.reachable, c.source != "cookie"))
    return results


def best(network: str, extra: Path | None = None) -> Candidate | None:
    """The first candidate that actually answers, or None."""
    for candidate in discover(network, extra):
        if candidate.reachable and candidate.chain == _expected_chain(network):
            return candidate
    return None


def _expected_chain(network: str) -> str:
    from .config import CHAIN_NAMES
    return CHAIN_NAMES[network]


def explain(network: str, extra: Path | None = None) -> str:
    """A human-readable account of what was tried, for when nothing worked."""
    candidates = discover(network, extra)
    if not candidates:
        return (
            f"No Pepecoin datadir found for {network}. Looked in: "
            + ", ".join(str(p) for p in (_platform_datadirs() + EXTRA_DATADIRS))
        )
    lines = [f"Could not reach a {network} node. Tried:"]
    for candidate in candidates:
        state = "reachable" if candidate.reachable else "no"
        lines.append(f"  {candidate.datadir}  [{state}]")
        for problem in candidate.problems[:2]:
            lines.append(f"      {problem}")
    return "\n".join(lines)

"""Configuration and chain parameters.

Chain parameters live here rather than as literals scattered through the code, so
that regtest, testnet and mainnet differ only by which Params instance is used.

Every value carries a source comment. Values marked "verified in source" were read
out of the Pepecoin Core v1.1.0 tree at /home/you/reference/pepecoin.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# The Ribbit Class C marker. Prepended to the payload inside an OP_RETURN, exactly
# as Omni prepends b"omni". Chosen in docs/DECISIONS.md D-005.
#
# CONSENSUS-CRITICAL: changing this after launch splits state.
MARKER = b"rbit"

# Property IDs 1 and 2 are permanently reserved and never assigned, mirroring
# Omni's OMNI_PROPERTY_MSC / OMNI_PROPERTY_TMSC slots (omnicore.h:123-124) which
# Ribbit deliberately leaves empty -- there is no base token (D-007).
RESERVED_PROPERTY_IDS = (1, 2)
FIRST_PROPERTY_ID_MAIN = 3

# Omni's test-ecosystem property IDs start at 0x80000001. Ribbit keeps both
# ecosystems (D-006), and they never mix (tx.cpp:1666).
FIRST_PROPERTY_ID_TEST = 0x80000001


@dataclass(frozen=True)
class Params:
    """Chain parameters for one Pepecoin network."""

    name: str
    rpc_port: int
    p2p_port: int

    # Height at which Ribbit starts interpreting transactions. Blocks below this
    # are never parsed for payloads. None means "not yet chosen" -- mainnet's
    # value is set at launch (D-004), so until then mainnet cannot be indexed.
    activation_height: int | None

    # pepecoind's datadir subdirectory for this network. Mainnet uses the datadir
    # root; other networks nest. Verified in source: chainparamsbase.cpp.
    datadir_subdir: str

    # base58 version bytes. Verified in source: pepecoin/src/chainparams.cpp
    # (mainnet :92-93, testnet, regtest).
    pubkeyhash_version: int = 56
    scripthash_version: int = 22

    # The Class B marker address: every Class B transaction must pay it, exactly
    # as Omni requires an output to Exodus (omnicore.cpp:81). Ribbit gives it NO
    # other meaning -- burn-to-mint was dropped in D-007, so it is purely a
    # marker and never a value sink.
    #
    # None means "not yet chosen"; mainnet's is derived and fixed before launch.
    marker_address: str | None = None


# nRPCPort / nDefaultPort verified in source: pepecoin/src/chainparamsbase.cpp:35,48,62
# and pepecoin/src/chainparams.cpp:153,282,392.
MAINNET = Params(
    name="main",
    rpc_port=33873,
    p2p_port=33874,
    activation_height=None,  # set at launch, ~tip + 1440 (D-004)
    datadir_subdir="",
    pubkeyhash_version=56,   # addresses start with "P"
    scripthash_version=22,
    marker_address=None,     # derived and fixed before launch
)

TESTNET = Params(
    name="test",
    rpc_port=44873,
    p2p_port=44874,
    activation_height=0,
    datadir_subdir="testnet3",
    pubkeyhash_version=113,
    scripthash_version=196,
)

REGTEST = Params(
    name="regtest",
    rpc_port=18332,
    p2p_port=18444,
    activation_height=0,
    datadir_subdir="regtest",
    pubkeyhash_version=111,
    scripthash_version=196,
)

NETWORKS = {p.name: p for p in (MAINNET, TESTNET, REGTEST)}


class ConfigError(Exception):
    """Raised when configuration is missing or unusable."""


def read_node_conf(path: Path) -> dict[str, str]:
    """Parse a pepecoin.conf into a flat dict.

    Only the subset we need is handled: `key=value` lines, `#` comments, and
    blank lines. Network sections (`[main]`) are recorded with their prefix so a
    caller can tell them apart, but we do not need them yet.
    """
    if not path.exists():
        raise ConfigError(f"no config file at {path}")

    values: dict[str, str] = {}
    section = ""
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip() + "."
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[section + key.strip()] = value.strip()
    return values


@dataclass(frozen=True)
class RpcCredentials:
    """How to reach pepecoind's JSON-RPC interface.

    Ribbit never stores a password itself. Credentials are read at run time from
    the user's own pepecoin.conf (mode 0600) or from a cookie file written by the
    daemon. Nothing is ever written back.
    """

    host: str
    port: int
    user: str
    password: str

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def __repr__(self) -> str:  # never leak the password into a traceback or log
        return f"RpcCredentials(host={self.host!r}, port={self.port}, user={self.user!r}, password=***)"


def load_rpc_credentials(
    params: Params = MAINNET,
    conf_path: Path | None = None,
    datadir: Path | None = None,
) -> RpcCredentials:
    """Find RPC credentials, preferring an explicit conf, then a cookie file.

    Order:
      1. `conf_path` (defaults to ~/.pepecoin/pepecoin.conf) with rpcuser/rpcpassword
      2. the daemon's `.cookie` in `datadir`, if readable

    Raises ConfigError with an actionable message if neither works, because a
    silent fallback to the wrong network is far worse than a hard failure.
    """
    conf_path = conf_path or Path.home() / ".pepecoin" / "pepecoin.conf"

    if conf_path.exists():
        conf = read_node_conf(conf_path)
        user = conf.get("rpcuser")
        password = conf.get("rpcpassword")
        if user and password:
            return RpcCredentials(
                host=conf.get("rpcconnect", "127.0.0.1"),
                port=int(conf.get("rpcport", params.rpc_port)),
                user=user,
                password=password,
            )

    if datadir is not None:
        cookie = datadir / params.datadir_subdir / ".cookie" if params.datadir_subdir else datadir / ".cookie"
        try:
            user, _, password = cookie.read_text().partition(":")
        except OSError as exc:
            raise ConfigError(
                f"no rpcuser/rpcpassword in {conf_path} and cookie {cookie} is unreadable: {exc}"
            ) from exc
        return RpcCredentials(host="127.0.0.1", port=params.rpc_port, user=user, password=password)

    raise ConfigError(
        f"no RPC credentials: {conf_path} has no rpcuser/rpcpassword and no datadir was given"
    )


def network_from_env(default: str = "regtest") -> Params:
    """Select a network from RIBBIT_NETWORK, defaulting to regtest.

    Defaulting to regtest is deliberate: a mistake should hit a throwaway chain,
    never mainnet.
    """
    name = os.environ.get("RIBBIT_NETWORK", default)
    if name not in NETWORKS:
        raise ConfigError(f"unknown network {name!r}; expected one of {sorted(NETWORKS)}")
    return NETWORKS[name]

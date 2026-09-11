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

# The DogecoinArcade Class C marker. Prepended to the payload inside an OP_RETURN, exactly
# as Omni prepends b"omni". Chosen in docs/DECISIONS.md D-005.
#
# CONSENSUS-CRITICAL: changing this after launch splits state.
MARKER = b"arcd"

# Property IDs 1 and 2 are permanently reserved and never assigned, mirroring
# Omni's OMNI_PROPERTY_MSC / OMNI_PROPERTY_TMSC slots (omnicore.h:123-124) which
# Arcade deliberately leaves empty -- there is no base token (D-007).
RESERVED_PROPERTY_IDS = (1, 2)
FIRST_PROPERTY_ID_MAIN = 3

# Omni's test-ecosystem property IDs start at 0x80000001. Arcade keeps both
# ecosystems (D-006), and they never mix (tx.cpp:1666).
FIRST_PROPERTY_ID_TEST = 0x80000001


#: The phrase the Class B marker address is derived from.
#:
#: Class B needs a well-known output that identifies a transaction as ours,
#: exactly as Omni requires an output to its Exodus address. Ours must have **no
#: private key**, because it carries no value and nobody should be able to sweep
#: the dust sent to it.
#:
#: Taking hash160 of a PHRASE rather than of a public key guarantees that: to
#: spend from the address one would have to find a public key whose hash160
#: equals this value, which is a preimage attack on RIPEMD160(SHA256(x)).
#:
#: It is a fixed string so that every installation derives the same address
#: independently, with nothing to configure and nothing to get wrong. It is
#: CONSENSUS-CRITICAL: changing it splits the network.
MARKER_SEED = b"DogecoinArcade Class B marker v1"


@dataclass(frozen=True)
class Params:
    """Chain parameters for one Pepecoin network."""

    name: str
    rpc_port: int
    p2p_port: int

    # Height at which Arcade starts interpreting transactions. Blocks below this
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
    # as Omni requires an output to Exodus (omnicore.cpp:81). Arcade gives it NO
    # other meaning -- burn-to-mint was dropped in D-007, so it is purely a
    # marker and never a value sink.
    #
    # Overrides the derived marker. Only for tests and regtest harnesses; real
    # networks use derive_marker_address() so every installation agrees.
    marker_address: str | None = None

    @property
    def marker(self) -> str:
        """The Class B marker address for this network.

        Derived, not configured: two installations that disagree here would not
        recognise each other's transactions at all.
        """
        if self.marker_address:
            return self.marker_address
        return derive_marker_address(self)


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

# --- Dogecoin -----------------------------------------------------------------
#
# Pepecoin is a Dogecoin fork, and every constant that matters to this protocol
# is IDENTICAL between them -- same values, same source line numbers:
#
#   MAX_OP_RETURN_RELAY = 83            script/standard.h:30   (both)
#   DEFAULT_PERMIT_BAREMULTISIG = true  validation.h:143       (both)
#   x-of-3 bare multisig standard       policy/policy.cpp:41   (both)
#   RECOMMENDED_MIN_TX_FEE = COIN/100   policy/policy.h:23     (both)
#   hard dust limit = DUST/10           policy/policy.h:81     (both)
#   scriptSig limit 1650                policy/policy.cpp:86   (both)
#   block spacing 60s                   chainparams.cpp        (both)
#
# So supporting Dogecoin costs exactly these three objects and no code changes.
# Verified in source at /home/you/reference/dogecoin (1.14.99).

DOGE_MAINNET = Params(
    name="doge-main",
    rpc_port=22555,          # chainparamsbase.cpp:35
    p2p_port=22556,          # chainparams.cpp
    activation_height=None,  # chosen at launch, as with Pepecoin
    datadir_subdir="",
    pubkeyhash_version=30,   # addresses start with "D"
    scripthash_version=22,
    marker_address=None,
)

DOGE_TESTNET = Params(
    name="doge-test",
    rpc_port=44555,          # chainparamsbase.cpp:48
    p2p_port=44556,
    activation_height=0,
    datadir_subdir="testnet3",
    pubkeyhash_version=113,
    scripthash_version=196,
)

DOGE_REGTEST = Params(
    name="doge-regtest",
    rpc_port=18332,
    p2p_port=18444,
    activation_height=0,
    datadir_subdir="regtest",
    pubkeyhash_version=111,
    scripthash_version=196,
)

# WARNING: Dogecoin testnet and Pepecoin testnet share PUBKEY_ADDRESS version
# 113 and SCRIPT_ADDRESS version 196, so a testnet address is indistinguishable
# between the two chains. Never infer the chain from an address -- record it
# explicitly alongside any key announcement or message.

NETWORKS = {
    p.name: p
    for p in (MAINNET, TESTNET, REGTEST, DOGE_MAINNET, DOGE_TESTNET, DOGE_REGTEST)
}

#: Networks on which the Messenger may operate. Testnet only, permanently
#: (docs/DECISIONS.md D-010). Enforced in code, not configuration.
MESSAGING_NETWORKS = frozenset({TESTNET.name, REGTEST.name, DOGE_TESTNET.name, DOGE_REGTEST.name})


class MainnetRefused(Exception):
    """Raised when a messaging operation is pointed at a mainnet chain."""


def require_messaging_network(params: "Params") -> None:
    """Refuse to run messaging against mainnet.

    D-010 makes the Messenger testnet-only as a product rule, so this is a hard
    check in code rather than a configuration switch someone can flip.
    """
    if params.name not in MESSAGING_NETWORKS:
        raise MainnetRefused(
            f"the Messenger is testnet-only (D-010); refusing to operate on {params.name!r}. "
            f"Allowed: {sorted(MESSAGING_NETWORKS)}"
        )


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

    Arcade never stores a password itself. Credentials are read at run time from
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
    # An explicitly supplied datadir wins. Otherwise a generic user config --
    # which on this machine points at MAINNET -- silently overrides the network
    # the caller asked for, and every subsequent call goes to the wrong node.
    if datadir is not None:
        sub = params.datadir_subdir
        cookie = (datadir / sub / ".cookie") if sub else (datadir / ".cookie")
        try:
            user, _, password = cookie.read_text().partition(":")
            return RpcCredentials(
                host="127.0.0.1", port=params.rpc_port, user=user, password=password
            )
        except OSError:
            pass      # fall through to the config file

    conf_path = conf_path or Path.home() / ".pepecoin" / "pepecoin.conf"

    if conf_path.exists():
        conf = read_node_conf(conf_path)
        user = conf.get("rpcuser")
        password = conf.get("rpcpassword")
        if user and password:
            # The port comes from the requested network unless the config names
            # one explicitly for THAT network. A bare `rpcport` in a user config
            # is for whichever node that config serves, which need not be ours.
            port = int(conf.get(f"{params.name}.rpcport", params.rpc_port))
            return RpcCredentials(
                host=conf.get("rpcconnect", "127.0.0.1"),
                port=port,
                user=user,
                password=password,
            )

    raise ConfigError(
        f"no RPC credentials: {conf_path} has no rpcuser/rpcpassword and no datadir was given"
    )


#: What each network calls itself in getblockchaininfo's "chain" field.
CHAIN_NAMES = {
    "main": "main", "test": "test", "regtest": "regtest",
    "doge-main": "main", "doge-test": "test", "doge-regtest": "regtest",
}


class WrongChain(Exception):
    """The connected node is not on the chain we asked for."""


def verify_connected_chain(rpc, params: "Params") -> None:
    """Confirm the node we reached is actually on the expected chain.

    `require_messaging_network` checks the params object -- our *intent*. It
    cannot tell that credentials resolved to a different node than intended,
    which is exactly what happens when a stray user config points elsewhere.
    This checks what we are actually talking to, which is the property that
    matters before anything is signed or broadcast.
    """
    actual = rpc.call("getblockchaininfo").get("chain")
    expected = CHAIN_NAMES.get(params.name)
    if actual != expected:
        raise WrongChain(
            f"connected to a node on chain {actual!r}, but {params.name!r} was requested "
            f"(expected chain {expected!r}). Refusing to continue -- check --datadir "
            f"and --conf."
        )


def derive_marker_address(params: "Params") -> str:
    """The marker address for `params`, from MARKER_SEED.

    Same hash160 on every chain; only the version byte differs, so the address
    string looks different on mainnet, testnet and regtest while the underlying
    script is the same shape everywhere.
    """
    from .script import b58check_encode, hash160
    return b58check_encode(params.pubkeyhash_version, hash160(MARKER_SEED))


def network_from_env(default: str = "regtest") -> Params:
    """Select a network from ARCADE_NETWORK, defaulting to regtest.

    Defaulting to regtest is deliberate: a mistake should hit a throwaway chain,
    never mainnet.
    """
    name = os.environ.get("ARCADE_NETWORK", default)
    if name not in NETWORKS:
        raise ConfigError(f"unknown network {name!r}; expected one of {sorted(NETWORKS)}")
    return NETWORKS[name]

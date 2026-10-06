"""Configuration and chain parameters.

Chain parameters live here rather than as literals scattered through the code, so
that regtest, testnet and mainnet differ only by which Params instance is used.

Every value carries a source comment. Values marked "verified in source" were read
out of the Pepecoin Core v1.1.0 tree at /home/you/reference/pepecoin.
"""

from __future__ import annotations

import time

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

    # Height from which a swap (inscriptions.py: KIND_SWAP) is read. A node
    # that predates swaps records one as an invalid inscription and carries
    # on, so it would disagree with a node that reads them about who owns
    # what. A height every node has updated before is how they stay agreed.
    # None means swaps are not read on this chain at all yet.
    swaps_from: int | None = None
    #: Height from which a BUNDLE (inscriptions.KIND_BUNDLE: two named parties,
    #: several legs each way) is read (2026-10-04). Announced before it is set,
    #: for the reason swaps_from gives: a node that predates bundles records one
    #: as an invalid inscription, so the two would disagree about who owns what.
    #: None: not read on this chain yet.
    bundles_from: int | None = None
    #: From this block an order (type 25) may pair two TOKENS, neither side the
    #: coin, and the engine MATCHES it (2026-10-06, the operator: "token to token pairs
    #: on the token exchange so that people can exchange one token for another").
    #: Both sides are balances the ledger holds, so a crossing order settles in
    #: its own block with no second signature: the new order takes the resting
    #: ones at their price, best price first, then the oldest. Before this
    #: height such an order is invalid, so it gets its own height (D-062's
    #: reason). None: not on this chain yet.
    token_pairs_from: int | None = None
    #: From this block a swap may take tokens out of the reserve a standing
    #: order holds, and reduce that order by what it took. Before it, those
    #: tokens are locked and an ask can only be filled after it is cancelled.
    #: Its own height because it makes valid what used to be invalid, so two
    #: nodes on either side of the change would read the same block
    #: differently (D-062).
    fills_from: int | None = None
    #: From this block a holder may put a PRICE on an inscription
    #: (inscriptions.py: KIND_ASK) and every node reads the same one. A node
    #: that predates asks records one as an invalid inscription and shows no
    #: price, so the two would disagree about what is for sale. Nothing moves
    #: on an ask and no balance depends on it -- it is not in the consensus
    #: hash -- but it is still a height, for the same reason the others are:
    #: the rule is agreed before the first one is broadcast, not after.
    asks_from: int | None = None
    #: From this block a swap may also fill the BUYER's standing bid: a bid
    #: reserves nothing, so nothing moves out of it, but the order itself is
    #: reduced by what was bought (D-118). Its own height because it changes
    #: what a block does to the book, and `book_order` is in the consensus
    #: hash -- two nodes either side of it would disagree about what is on
    #: offer, which is the one thing a book may never do.
    bid_fills_from: int | None = None
    #: From this block a swap may NAME the order it fills, and the engine
    #: draws from that order rather than from whatever the seller has loose.
    #: Its own height because it makes a payload legal that was invalid
    #: before -- a swap with 32 bytes after its legs (D-082).
    named_fills_from: int | None = None
    #: From this block a taker may settle a resting ask ALONE -- payload type
    #: 29, the reserve behind somebody else's order taken by the person paying
    #: for it, with no signature from the maker (D-189). Its own height for the
    #: sharpest reason any of these have: a take carries the taker's coins in
    #: its OUTPUTS, so a node that has not read type 29 yet still indexes the
    #: transaction as an unknown message, moves no tokens, and leaves that
    #: person holding a paid-for order nobody filled. Nothing files one until a
    #: route does, and no route goes out before both nodes run this, so the
    #: height is the paperwork and the release order is the safety.
    take_from: int | None = None
    #: From this block, a block's cancels (types 26-28) are applied after
    #: everything else in it. A take pays the maker in plain outputs that the
    #: chain moves whatever this layer decides, so a maker who saw a take in
    #: the mempool could otherwise cancel ahead of it in the same block, keep
    #: the tokens AND the coins, and leave the taker with nothing (Claude's
    #: review of D-189, 2026-09-27). Its own height because it reorders what
    #: every node computes for a block.
    cancels_last_from: int | None = None

    # base58 version bytes. Verified in source: pepecoin/src/chainparams.cpp
    # (mainnet :92-93, testnet, regtest).
    pubkeyhash_version: int = 56
    scripthash_version: int = 22

    # The version byte of THIS coin's other chain: mainnet for a testnet, and
    # the other way round. A key announcement can carry the same person's
    # address on both (D-032), and the 20 bytes on the wire say nothing about
    # which chain they are for -- so the reader supplies that, from here,
    # rather than guessing from the bytes. None where there is no counterpart
    # to name (regtest).
    other_pubkeyhash_version: int | None = None

    # The Class B marker address: every Class B transaction must pay it, exactly
    # as Omni requires an output to Exodus (omnicore.cpp:81). Arcade gives it NO
    # other meaning -- burn-to-mint was dropped in D-007, so it is purely a
    # marker and never a value sink.
    #
    # Overrides the derived marker. Only for tests and regtest harnesses; real
    # networks use derive_marker_address() so every installation agrees.
    #: The block every installation starts reading messages from.
    #:
    #: Shared deliberately. Before this, each machine began at whatever height
    #: its own identity happened to be created, so two people running the same
    #: version saw different histories and neither could tell why -- one would
    #: see an announcement or a public post the other simply never scanned.
    #:
    #: It is a release decision, not a runtime one: bumping it declares a clean
    #: slate for everyone at once, and lowering it asks every installation to
    #: rescan. Set per network because the chains are unrelated.
    messaging_start_height: int = 0

    #: Where @names (tag claims, and the messaging keys that go with them) are
    #: read from, when that is EARLIER than the floor (2026-09-25: names
    #: survive a floor move). Moving the floor raises `activation_height` and the
    #: other starts; this stays where names began, so the index reads the blocks
    #: in between for names and nothing else -- tokens, inscriptions, shops,
    #: posts and messages there are ignored as before -- and every node derives
    #: the same owners from the chain alone. None means "from the floor", as
    #: before. It is a release decision like the floor itself.
    names_from: int | None = None

    marker_address: str | None = None

    @property
    def index_start(self) -> int | None:
        """Where the ledger index begins: the floor, or where names begin if that
        is earlier (see `names_from`)."""
        if self.names_from is None or self.activation_height is None:
            return self.activation_height
        return min(self.activation_height, self.names_from)

    def names_only(self, height: int) -> bool:
        """True for a block read for its names alone: below the floor, at or above
        `names_from`."""
        return (self.names_from is not None and self.activation_height is not None
                and self.names_from <= height < self.activation_height)

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
    # The block the ledger starts reading from. Set 2026-09-12 at tip 1,206,546,
    # about a day ahead (D-004 asked for ~tip + 1440), so no transaction that
    # existed before the token code shipped can be read as a token
    # transaction. Free to move until the first mainnet token exists (D-016);
    # after that, moving it rewrites everybody's balances.
    activation_height=1_208_000,
    datadir_subdir="",
    pubkeyhash_version=56,   # addresses start with "P"
    scripthash_version=22,
    other_pubkeyhash_version=113,   # its own testnet
    marker_address=None,     # derived and fixed before launch
)

TESTNET = Params(
    name="test",
    rpc_port=44873,
    p2p_port=44874,
    # --- one floor, for everything (D-106) -------------------------------------
    #
    # Set 2026-09-17 to the tip ITSELF, 1,493,951, with no margin at all.
    # Every height below is the same number on purpose: tokens, messages,
    # swaps, asks and fills all start together, so there is one date for
    # "before" and one for "after" rather than six.
    #
    # No margin, because a margin was never what made this safe. A floor only
    # changes how a block reads if a transaction is IN one, and the rule is
    # that nothing new goes out until both nodes report the same floor. The
    # blocks between this height and whenever a node takes the release hold
    # nothing of ours, so there is nothing for two nodes to read differently
    # -- and a node that updates late indexes from here anyway, because its
    # index starts at the floor rather than where it happened to be. Three
    # hours bought exactly what no margin buys, an hour and a half later.
    #
    # Why start again at all: what is under it is six weeks of building the
    # thing -- half-formats, a collection inscribed three ways, tokens made to
    # watch a page draw, and an era of swaps from before asks existed. A
    # marketplace whose history is mostly its own construction reads as
    # nothing anybody would trust. Everything below stays on the chain for
    # ever; nothing here will look at it again.
    #
    # Moving a floor does NOT clear what a node has already indexed -- the
    # index resumes from its own height and only skips low blocks going
    # forward -- so an install that keeps its index would see Goofball and the
    # old tokens while a fresh one sees none of them, and the two would
    # disagree about what exists.
    #
    # The index now moves ITSELF aside when it finds the floor above
    # everything it holds, and the wallet's own stores retire their older
    # era's rows the same way (D-123, D-126), so the only file left for a
    # human to decide about is `test.sqlite`, the messaging store: it holds an
    # address book that was typed rather than scanned, and it ALSO holds
    # mainnet's messaging scan position, so moving it aside makes a wallet
    # with mainnet messages rescan them (a test machine found that row).
    #
    # "Nothing else in ~/.dogecoinarcade is per-chain" is what used to be
    # written here, and it was wrong in the way that is hardest to see: every
    # one of those files IS keyed by network, and a reset makes a new era
    # inside one network, so being network-scoped said nothing about a floor.
    # `approvals.sqlite` was the sharp one -- a row naming property 3 by the
    # name of a token from two eras ago, in the file that records what
    # somebody authorised (D-126). `main-ledger.sqlite` must not be touched.
    # Moved again 2026-09-18, third time in two days, and this one is a
    # tidy-up rather than a consensus change: the local stores are retiring an
    # era's worth of rows (D-126), the run list is doing the same (D-125), and
    # starting both machines clean beats reasoning about what survived.
    #
    # Ten blocks above the tip, and set LAST -- after the suite, immediately
    # before the publish. This one is a wipe rather than a reset: both
    # machines delete everything the arcade wrote and start as a fresh
    # install, keeping only their Core datadirs, so the chain below is not
    # merely unread -- there is nothing left that remembers it (D-133). The first two of these floors were chosen before a
    # half-hour test run and were behind the chain by the time the publish
    # finished; the third was an hour ahead and made somebody wait an hour for
    # a number. The floor is one line of config and the suite is testing the
    # code, so the number is the last thing decided rather than the first
    # (the operator).
    activation_height=1_496_133,
    messaging_start_height=1_496_133,
    # Names began at the floor before launch. The launch floor move raises the
    # heights above and leaves this one, so every @name claimed since keeps its
    # owner (docs/multi-user.md, "@names persist across the floor move").
    names_from=1_496_133,
    swaps_from=1_496_133,
    asks_from=1_496_133,
    fills_from=1_496_133,
    named_fills_from=1_496_133,
    bid_fills_from=1_496_133,
    take_from=1_496_133,
    cancels_last_from=1_513_300,
    # Bundles (inscriptions.KIND_BUNDLE) are read from here: switched on 2026-10-05 at
    # The operator's word ("Enable it right now, we are the only node"), the block after the tip.
    bundles_from=1_527_064,
    # Token/token pairs (Params.token_pairs_from), switched on 2026-10-06 at the operator's word
    # ("get token pairs live"), two blocks after the tip: this is the only node.
    token_pairs_from=1_529_092,
    datadir_subdir="testnet3",
    pubkeyhash_version=113,
    scripthash_version=196,
    other_pubkeyhash_version=56,    # Pepecoin mainnet
)

REGTEST = Params(
    name="regtest",
    rpc_port=18332,
    p2p_port=18444,
    activation_height=0,
    swaps_from=0,
    bundles_from=0,
    token_pairs_from=0,
    asks_from=0,
    bid_fills_from=0,
    fills_from=0,
    named_fills_from=0,
    take_from=0,
    cancels_last_from=0,
    datadir_subdir="regtest",
    pubkeyhash_version=111,
    scripthash_version=196,
)

# --- Pepecoin only ------------------------------------------------------------
#
# Dogecoin used to be in here as three more Params objects and no code changes,
# because Pepecoin is a Dogecoin fork and every constant this protocol leans on
# is identical between them (MAX_OP_RETURN_RELAY 83, x-of-3 bare multisig, the
# DUST/10 floor, the 1650-byte scriptSig limit, 60-second blocks -- the table in
# docs/DECISIONS.md that compares the two trees). The operator's call of 2026-09-24:
# what this arcade runs against is Pepecoin testnet and Pepecoin mainnet, and a
# chain with no node behind it is not a feature but an option that breaks
# whoever picks it. So the objects are gone rather than hidden: a page cannot
# offer what the program cannot resolve, and `network_from_env` below answers
# whoever names a chain that is no longer in here.
#
# What is worth keeping is the reason removal is not a licence to get careless
# about a strange address. Dogecoin's testnet shares Pepecoin testnet's
# PUBKEY_ADDRESS version 113 and SCRIPT_ADDRESS 196, and Litecoin's testnet uses
# 111 like this regtest. So a string of base58 never says which chain it belongs
# to: the chain is recorded beside an address, and an address that is merely
# VALID SOMEWHERE ELSE has to be refused rather than accepted as close enough.

NETWORKS = {p.name: p for p in (MAINNET, TESTNET, REGTEST)}

#: Networks on which the Messenger may operate. Testnet only, permanently
#: (docs/DECISIONS.md D-010). Enforced in code, not configuration.
MESSAGING_NETWORKS = frozenset({TESTNET.name, REGTEST.name})


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
#: A Dogecoin node answers with these same three words, which is one more
#: reason this arcade names its chain rather than trusting what it connects to.
CHAIN_NAMES = {
    "main": "main", "test": "test", "regtest": "regtest",
}


class WrongChain(Exception):
    """The connected node is not on the chain we asked for."""


#: (host, port, chain) -> until when a verified match is trusted (verify_connected_chain).
_CHAIN_SEEN: dict = {}


def verify_connected_chain(rpc, params: "Params") -> None:
    """Confirm the node we reached is actually on the expected chain.

    `require_messaging_network` checks the params object -- our *intent*. It
    cannot tell that credentials resolved to a different node than intended,
    which is exactly what happens when a stray user config points elsewhere.
    This checks what we are actually talking to, which is the property that
    matters before anything is signed or broadcast.
    """
    # Once per node and chain for five minutes (2026-10-05): which node a set of
    # credentials reaches does not change between two calls a page makes, and a
    # page made thirteen of them. A wrong chain is still refused at once: only a
    # match is remembered.
    creds = getattr(rpc, "_creds", None)
    key = (getattr(creds, "host", ""), getattr(creds, "port", 0), params.name)
    if creds is not None and _CHAIN_SEEN.get(key, 0) > time.time():
        return
    actual = rpc.call("getblockchaininfo").get("chain")
    expected = CHAIN_NAMES.get(params.name)
    if actual != expected:
        raise WrongChain(
            f"connected to a node on chain {actual!r}, but {params.name!r} was requested "
            f"(expected chain {expected!r}). Refusing to continue -- check --datadir "
            f"and --conf."
        )
    if creds is not None:
        _CHAIN_SEEN[key] = time.time() + 300


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

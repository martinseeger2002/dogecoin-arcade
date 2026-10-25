"""Configuration and chain parameters.

Chain parameters live here rather than as literals scattered through the code, so
that regtest, testnet and mainnet differ only by which Params instance is used.

Every value carries a source comment. Values marked "verified in source" were read
out of the Pepecoin Core v1.1.0 tree at /home/you/reference/pepecoin.
"""

from __future__ import annotations

import time

import os
import dataclasses
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
    #: The fee rate this chain's transactions pay, in base units per kB of virtual size. Pepecoin and
    #: Dogecoin relay at COIN/100 (policy.h RECOMMENDED_MIN_TX_FEE); Litecoin relays at 1/1000 of that,
    #: and 0.01 LTC/kB is over its own -maxtxfee for a Class B transaction (2026-10-17).
    min_fee_per_kb: int = 1_000_000

    #: Whether relay charges the soft-dust fee again for every output under `dust_limit` (Pepecoin
    #: and Dogecoin: GetPepecoinDustFee). Litecoin has no such fee: Bitcoin-style, an output under
    #: its dust threshold is simply not relayed, so nothing is added (2026-10-18).
    soft_dust: bool = True
    #: The least change worth an output of its own; below it the change goes to the miner. 0.01
    #: on the Dogecoin family (the soft-dust line); 0.0001 LTC on Litecoin, above its P2PKH dust
    #: threshold of 5,460 litoshis at the 0.0003 LTC/kB dustrelayfee (2026-10-18).
    dust_limit: int = 1_000_000

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
    #: What its coin is called on a page, and what the chain is called
    #: (2026-10-13, Dogecoin as the third network): "PEP TEST" / "PEP" / "DOGE".
    coin: str = "PEP"
    label: str = "Pepecoin main"
    #: Block 0's hash, where it tells this chain from another that answers to
    #: the same "main" in getblockchaininfo (Dogecoin and Pepecoin both do).
    genesis: str = ""
    #: The chain id merge-mined blocks carry in the top half of their version (AuxPoW), so a block
    #: borrowed from another arcade (arcade/rawchain.py) cannot be another merge-mined coin's. None:
    #: not checked (a chain that is not merge-mined, or regtest).
    auxpow_chain_id: int | None = None
    #: Which bits of the version's top half hold that chain id. Dogecoin's family reads the whole top half
    #: (`nVersion >> 16`). Bellscoin 3 is Bitcoin-Core-based and also signals versionbits up there, so its
    #: header masks the id to 0x1f (bells primitives/pureheader.h MASK_AUXPOW_CHAINID_SHIFTED): a block of
    #: version 0x20100100 is chain 16, not 0x2010.
    auxpow_chain_mask: int = 0xFFFF
    #: A test network's rule that a block found after a long gap may be at the easiest difficulty
    #: (`pow_limit_bits`) whatever the block before it was. rawchain's one-block difficulty check lets
    #: such a block through on these chains instead of refusing it.
    min_difficulty_blocks: bool = False
    pow_limit_bits: int = 0x1E0FFFFF
    #: The proof-of-work hash rawchain checks a borrowed block with: "scrypt" (Pepecoin, Dogecoin,
    #: Litecoin and the scrypt merge-mined coins), "sha256d" (the SHA-256 ones), or "" for a chain whose
    #: work this arcade cannot check (a multi-algorithm chain such as DigiByte): its blocks are still
    #: proved by hash, merkle root and linkage, and the admin panel says the work is the sharer's word.
    pow_algo: str = "scrypt"

    # --- what a chain is, as data (2026-10-18, the operator: "Eventually, we want to add Digi bite and
    # other aux pow chains"): a new chain is a Params entry, not code. -------------------------------
    #: A chain beside the arcade's own two: run here, borrowed through the mesh, or off (arcade/cores.py).
    addon: bool = False
    #: Where its Core keeps its config on a machine that runs it: ~/<node_dir>/<node_conf>, and its
    #: cookie in that folder (or the network's `datadir_subdir` under it).
    node_dir: str = ""
    node_conf: str = ""
    #: The chain's name as people say it, before " - Main" / " - Test" (2026-10-20: "the network name - Main
    #: or test ... Because we could be and will be adding many more blockchains"). Empty: its `label`, less a trailing
    #: "testnet" ("Litecoin testnet" -> "Litecoin"), so a chain copied from another's Params is named by its own label.
    chain_name: str = ""
    #: One capital letter for a badge with no icon, and the coin family it belongs to.
    letter: str = "P"
    family: str = "PEP"
    #: The word a page files the chain under ("dogecoin", "litecoin"); empty: mainnet or testnet.
    net_word: str = ""
    #: Real coins (the second confirmation, no faucet, the words backed up).
    real_coins: bool = False
    #: Its SegWit addresses' human-readable part ("ltc1...", "tltc1..."); empty: no SegWit.
    bech32_hrp: str = ""
    #: Litecoin's MimbleWimble extension blocks: a transaction may carry an MWEB part, a block an MWEB
    #: block after its transactions, and Core prints a few more fields ("ismweb", "nTx", "weight").
    mweb: bool = False
    #: Peercoin-style transactions (Kotia, 2026-10-23): a 4-byte `nTime` right after the version, in the txid and
    #: in the legacy sighash alike (kotiacash primitives/transaction.h, script/interpreter.cpp). A transaction built
    #: without one is unreadable to such a chain's Core, so it is refused there rather than lost.
    tx_time: bool = False
    #: Proof-of-stake blocks: a block signature (varint length + DER) follows the transactions, empty on a
    #: proof-of-work block (kotiacash primitives/block.h `vchBlockSig`).
    block_sig: bool = False
    #: Whether getblockchaininfo's verificationprogress can be trusted as "this node is done". True for a
    #: chain whose chainTxData matches its real traffic. False when that estimate is a stub: Wojakcoin's
    #: is 100 transactions a second, so the figure stays near 0.9 at a live tip and never reaches 0.9999.
    trust_progress: bool = True

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
    auxpow_chain_id=0x3F,
    real_coins=True,
)

TESTNET = Params(
    name="test",
    min_difficulty_blocks=True,
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
    coin="PEP TEST",
    label="Pepecoin test",      # "Pepecoin test" and "Pepecoin main", as the rest of the app says it (2026-10-24)
    scripthash_version=196,
    other_pubkeyhash_version=56,    # Pepecoin mainnet
)

REGTEST = Params(
    name="regtest",
    min_difficulty_blocks=True,
    pow_limit_bits=0x207FFFFF,
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
    coin="PEP TEST",
    label="Pepecoin regtest",
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

# --- Dogecoin, the third network (2026-10-13) ----------------------------
#
# Back, now with a node behind it: "we will be adding it as a third network ...
# Anything they can do on pep. It will be just another option alongside testnet
# pep and mainnet pep." So it is Pepecoin MAINNET's Params with Dogecoin's own
# ports, address bytes and floor, nothing else: the protocol constants are the
# same in both trees (the comparison table in docs/DECISIONS.md). Mainnet only;
# there is no Dogecoin testnet in the arcade (the operator: "We don't want Dogecoin
# test net").
#
# The floor is the network tip of 2026-10-13 17:10 UTC (6,413,728, from a public
# explorer, the node itself still syncing) rounded up to 6,415,000, about a day
# ahead, so nothing that existed before the arcade read Dogecoin can be read as
# an arcade transaction -- the margin D-004 asked for on Pepecoin mainnet. Every
# feature height stays as it is on Pepecoin mainnet: one floor (D-106).
DOGECOIN = dataclasses.replace(
    MAINNET,
    name="doge",
    rpc_port=22555,
    p2p_port=22556,
    activation_height=6_415_000,
    # Its public board (tips, D-014) is read from the same floor, never from block
    # 0: Pepecoin mainnet reads from 0, which on Dogecoin is six million blocks.
    messaging_start_height=6_415_000,
    pubkeyhash_version=30,      # addresses start with "D"
    scripthash_version=22,
    other_pubkeyhash_version=113,
    marker_address=None,
    coin="DOGE",
    label="Dogecoin",
    genesis="1a91e3dace36e2be3bf030a65679fe821aa1d6ef92e7c9902eb318182c355691",
    auxpow_chain_id=0x62,
    addon=True, node_dir=".dogecoin", node_conf="dogecoin.conf",
    letter="D", family="DOGE", net_word="dogecoin", real_coins=True,
)

# Litecoin MAINNET (SadFrog's side, 2026-10-17; LITECOIN-PLAN phase 2). An add-on chain an operator
# runs here, borrows or leaves off (arcade/cores.py). Same shape as Dogecoin: Pepecoin's feature heights, its own ports,
# address versions (L... 48, M... 50) and genesis. Verified on a regtest litecoind 0.21.3: bare
# multisig plus an 80-byte OP_RETURN relays with -acceptnonstdtxn=0, so the Class B layer carries
# over unchanged.
#
# The floor (2026-10-17): the network tip at 16:20 UTC that day was 3,196,698 (a public explorer;
# this node still syncing), rounded up to 3,198,000 -- about two days ahead at 2.5-minute blocks --
# so nothing that existed before the arcade read Litecoin can be read as an arcade transaction
# (the D-004 margin, as Dogecoin's floor was set). Then lowered (2026-10-18, at SadFrog's word) to
# 3,197,300, just under block 3,197,387, where 0.1 LTC was sent to an account's L address: an index
# reads only from its floor up, so coins an account got below it never show in its Wallet. Nothing
# arcade-made existed on Litecoin mainnet below it, and the Litecoin safety fixes (4bfc4d6) were in.
LTC_MAINNET_FLOOR = 3_197_300

LITECOIN = dataclasses.replace(
    MAINNET,
    name="ltc",
    rpc_port=9332,
    p2p_port=9333,
    activation_height=LTC_MAINNET_FLOOR,
    messaging_start_height=LTC_MAINNET_FLOOR,
    pubkeyhash_version=48,      # addresses start with "L"
    scripthash_version=50,      # "M" (the legacy "3", version 5, is read as P2SH too: txbuild)
    other_pubkeyhash_version=113,
    marker_address=None,
    coin="LTC",
    label="Litecoin",
    min_fee_per_kb=10_000,      # 0.0001 LTC/kB: ten times Litecoin 0.21's minimum relay fee
    soft_dust=False, dust_limit=10_000,     # no soft-dust fee; change from 0.0001 LTC
    genesis="12a765e31ffd4059bada1e25190f6e98c99d9714d334efa41a195a7e7e04bfe2",
    auxpow_chain_id=None,       # scrypt, mined on its own (it is the PARENT Dogecoin merge-mines with)
    addon=True, node_dir=".litecoin", node_conf="litecoin.conf",
    letter="L", family="LTC", net_word="litecoin", real_coins=True, bech32_hrp="ltc", mweb=True,
)

# Litecoin TESTNET (SadFrog's side, 2026-10-17; plan B, the arcade's own "hello world" inscription).
# Its own add-on chain, "L test" beside "L main" (the operator's choice), as P test sits beside P main:
# Litecoin mainnet's Params with testnet4's ports, address versions (m/n... 111, Q... 58) and genesis,
# its node's chain in testnet4/. Keys on coin type 1, as every test chain (coins.js COIN_TYPE), so an
# account's L test key is its Pepecoin testnet key.
# The floor: first 4,927,000 (2026-10-18 01:00 UTC, a peer's tip 4,926,272 rounded up), then lowered
# the same day to 4,926,300, just under the tip of 4,926,38x, at SadFrog's word: "lower the floor" so
# the arcade's first L test block, CPU-mined to an account that day, is read. Nothing arcade-made
# existed on Litecoin testnet below it, and this network's blocks come about every 5-6 minutes
# (min-difficulty blocks after each 5-minute gap), so 4,927,000 was two days away.
LTC_TESTNET_FLOOR = 4_926_300

LITECOIN_TESTNET = dataclasses.replace(
    LITECOIN,
    name="ltctest",
    rpc_port=19332,
    p2p_port=19335,
    activation_height=LTC_TESTNET_FLOOR,
    messaging_start_height=LTC_TESTNET_FLOOR,
    datadir_subdir="testnet4",
    pubkeyhash_version=111,     # addresses start with "m" or "n"
    scripthash_version=58,      # "Q" (the older "2", version 196, is read as P2SH too: txbuild)
    other_pubkeyhash_version=48,
    coin="LTC TEST",
    label="Litecoin testnet",
    genesis="4966625a4b2851d9fdee139e56211a0d88575f59ed816ff5e6a63deb4e3e29a0",
    # Testnet's 20-minute rule: a block after a long gap may be at the easiest difficulty.
    min_difficulty_blocks=True,
    node_dir=".litecoin-testnet", real_coins=False, bech32_hrp="tltc",
)

# Bellscoin MAINNET (SadFrog's side, 2026-10-22: "integrate them as fully as Dogecoin"). An add-on chain like
# Dogecoin, as Params alone. Bells Core 3.0.0-rc2 (github.com/BellsChain/bellscoin, kernel/chainparams.cpp) is
# Bitcoin-Core-based, so its relay is Litecoin's shape: no soft-dust fee, a 1,000 sat/kB minimum relay fee and
# Bitcoin's dust threshold (about 546 sat). Merge-mined with chain id 16 from block 144,000, masked to 0x1f
# (`auxpow_chain_mask`). SegWit is buried at 144,000 ("bel1..."); Taproot was a BIP9 deployment from 188,000.
# Policy: an 83-byte OP_RETURN and bare multisig relay, so the Class B layer carries over.
#
# CAUTION, address versions: a Bells P2SH address is version 30, which is Dogecoin's P2PKH "D...". From the
# string alone a Bells script hash and a Dogecoin key hash are the same address, so txbuild cannot read 30 as
# P2SH for every chain. The arcade pays Bells P2PKH ("B...", 25) and refuses a "D..." payee on Bellscoin
# (`unpayable_versions`) rather than guess.
#
# The floor: the network tip on 2026-10-22 was about 1,285,370 (a public explorer whose genesis matches),
# rounded up about two days ahead at one-minute blocks (the D-004 margin).
BEL_MAINNET_FLOOR = 1_289_000

BELLSCOIN = dataclasses.replace(
    DOGECOIN,
    name="bel",
    rpc_port=19918,             # the ecosystem's port; the build's own default is still Bitcoin's 8332
    p2p_port=19919,
    activation_height=BEL_MAINNET_FLOOR,
    messaging_start_height=BEL_MAINNET_FLOOR,
    pubkeyhash_version=25,      # addresses start with "B"
    scripthash_version=30,      # "D" -- see the CAUTION above
    other_pubkeyhash_version=None,
    coin="BEL",
    label="Bellscoin",
    min_fee_per_kb=10_000,      # 0.0001 BEL/kB: ten times Bells 3's minimum relay fee, as Litecoin's
    soft_dust=False, dust_limit=10_000,     # no soft-dust fee; change from 0.0001 BEL
    genesis="e5be24df57c43a82d15c2f06bda961296948f8f8eb48501bed1efb929afe0698",
    auxpow_chain_id=16, auxpow_chain_mask=0x1F,
    addon=True, node_dir=".bells", node_conf="bells.conf",
    letter="B", family="BEL", net_word="bellscoin", real_coins=True, bech32_hrp="bel",
)

# Luckycoin MAINNET (SadFrog's side, 2026-10-22). Luckycoin Core 5.0.1 (github.com/LuckycoinFoundation/Luckycoin,
# chainparams.cpp) is Dogecoin 1.14's code, so Dogecoin's Params fit: 0.01 LKY/kB, the 0.01 soft-dust line,
# one wallet.dat. Merge-mined with chain id 0x2013 from block 176,000; no SegWit. Versions 47 ("K"/"L", not
# Litecoin's 48) and P2SH 5 ("3", which txbuild already reads as P2SH for Litecoin's legacy addresses).
# 5.0.1 is mandatory: it no longer talks to 4.0.x nodes. The floor: the tip on 2026-10-22 was 1,149,640
# (this node's headers, and a public explorer), rounded up about two days ahead at one-minute blocks.
LKY_MAINNET_FLOOR = 1_153_000

LUCKYCOIN = dataclasses.replace(
    DOGECOIN,
    name="lky",
    rpc_port=9918,
    p2p_port=9917,
    activation_height=LKY_MAINNET_FLOOR,
    messaging_start_height=LKY_MAINNET_FLOOR,
    pubkeyhash_version=47,
    scripthash_version=5,
    other_pubkeyhash_version=None,
    coin="LKY",
    label="Luckycoin",
    genesis="9b7bce58999062b63bfb18586813c42491fa32f4591d8d3043cb4fa9e551541b",
    auxpow_chain_id=0x2013,
    addon=True, node_dir=".luckycoin", node_conf="luckycoin.conf",
    # "K": Litecoin already wears the "L" badge.
    letter="K", family="LKY", net_word="luckycoin", real_coins=True,
)

# Kotia MAINNET (tbuuol's side, 2026-10-23: large game assets in OP_RETURN, away from P test's single miner).
# Kotia 1.0.0 (github.com/eleonoraandrea/kotiacash) is a Blackcoin-style proof-of-stake coin on Bitcoin 0.14's code:
# Litecoin's relay shape without SegWit (0.0001 KOT/kB, no soft-dust fee), but Peercoin's transaction `nTime`
# (`tx_time`) and a block signature after the transactions (`block_sig`). Proof of work ended at block 1,000,000, so
# a block's work is not checked here (`pow_algo=""`); its hash is sha256d of the header (version > 6).
# Versions from chainparams.cpp:169-171, each checked against the node: P2PKH 46 and P2SH 45 (both "K..."), WIF 173.
# Neither byte is another chain's, so a Kotia P2SH address is read as one. Policy (policy.cpp, main.cpp): OP_RETURN
# scripts up to 15,000 bytes, several per transaction while at most half the outputs are data; and the mempool refuses
# a transaction with more than 10 outputs under 0.001 KOT (OP_RETURNs count), so change under 0.001 KOT is not made.
# The floor: the tip on 2026-10-23 was 1,528,731, rounded up about two days ahead at ~142-second blocks.
KOT_MAINNET_FLOOR = 1_530_000

KOTIA = dataclasses.replace(
    LITECOIN,
    name="kot",
    rpc_port=46173,
    p2p_port=46174,
    activation_height=KOT_MAINNET_FLOOR,
    messaging_start_height=KOT_MAINNET_FLOOR,
    pubkeyhash_version=46,
    scripthash_version=45,
    other_pubkeyhash_version=None,
    coin="KOT",
    label="Kotia",
    min_fee_per_kb=10_000,      # 0.0001 KOT/kB: Kotia's minimum relay fee (main.h DEFAULT_MIN_RELAY_TX_FEE)
    soft_dust=False, dust_limit=100_000,    # change from 0.001 KOT: the mempool's dust-output count (main.cpp:1246)
    genesis="000008e4586479cd9dc5832b262414a60f152381e371cf7940ffe9f5aa4b80a0",
    auxpow_chain_id=None, pow_algo="",
    addon=True, node_dir=".kotia", node_conf="kotia.conf",
    # "T": Luckycoin already wears the "K" badge.
    letter="T", family="KOT", net_word="kotia", real_coins=True, bech32_hrp="", mweb=False,
    tx_time=True, block_sig=True,
)

# Wojakcoin MAINNET. WojakCoin 1.21.3 (wojakcore chainparams.cpp / chainparamsbase.cpp) is Bitcoin Core's code:
# Litecoin's relay shape without SegWit (0.0001 WJK/kB, ten times the 1,000 sat/kB minimum relay fee; no soft-dust
# fee), sha256d proof of work (CBlockHeader::GetHash is SerializeHash), and no AuxPoW. SegWit and Taproot are
# compiled in but never activate (SegwitHeight and the Taproot deployment are both max/NEVER_ACTIVE), so the
# "wj" bech32 prefix in chainparams is not used here. Versions, each checked against this chain's own node:
# P2PKH 73 ("W..."), P2SH 5 ("3...", the same byte Luckycoin and Litecoin's legacy script addresses use, and not
# any chain's key-hash byte), WIF 201. Policy: an 83-byte OP_RETURN and bare multisig relay (policy.h, policy.cpp).
# The floor: the tip on 2026-10-25 was 226,542, rounded up about two days ahead at two-minute blocks.
WJK_MAINNET_FLOOR = 228_000

WOJAKCOIN = dataclasses.replace(
    LITECOIN,
    name="wjk",
    rpc_port=20760,
    p2p_port=20759,
    activation_height=WJK_MAINNET_FLOOR,
    messaging_start_height=WJK_MAINNET_FLOOR,
    pubkeyhash_version=73,
    scripthash_version=5,
    other_pubkeyhash_version=None,
    coin="WJK",
    label="Wojakcoin",
    min_fee_per_kb=10_000,      # 0.0001 WJK/kB: ten times validation.h DEFAULT_MIN_RELAY_TX_FEE (1,000 sat/kB)
    soft_dust=False, dust_limit=10_000,     # no soft-dust fee; change from 0.0001 WJK (dust relay is 3,000 sat/kB)
    genesis="000000004536a4f8fa9d88f0001ca9f9825f8d9fd3ba6383a2f030c0427bf085",
    auxpow_chain_id=None, pow_algo="sha256d", pow_limit_bits=0x1D00FFFF,
    addon=True, node_dir=".wojakcoin", node_conf="wojakcoin.conf",
    # "W": no other chain wears it.
    letter="W", family="WJK", net_word="wojakcoin", real_coins=True, bech32_hrp="", mweb=False,
    trust_progress=False,
)

NETWORKS = {p.name: p for p in (MAINNET, TESTNET, REGTEST, DOGECOIN, LITECOIN, LITECOIN_TESTNET,
                                BELLSCOIN, LUCKYCOIN, KOTIA, WOJAKCOIN)}

def unpayable_versions(params: "Params") -> frozenset:
    """The version bytes of `params`' own script-hash addresses that another known chain uses for a key hash.

    txbuild reads a version byte the same on every chain, and a byte that is one chain's P2PKH and another's
    P2SH cannot be both: Bellscoin's P2SH 30 is Dogecoin's "D" (2026-10-22). A send on such a chain refuses a
    payee of that byte (web/app.py _check_address) rather than pay a script hash as a key hash."""
    # Duck-typed: a caller may hand over a stand-in with only the version bytes (tests do), which clashes with nothing.
    name, version = getattr(params, "name", None), getattr(params, "scripthash_version", None)
    if name not in NETWORKS or version is None:
        return frozenset()
    key_hashes = {p.pubkeyhash_version for p in NETWORKS.values() if p.name != name}
    return frozenset({version} & key_hashes)


def require_p2sh_payable(params: "Params") -> None:
    """Refuse to make an arcade P2SH address (a swap lock, an escrow, a prize pool) on a chain whose script-hash
    byte is another chain's key-hash byte (`unpayable_versions`, Bellscoin's 30): txbuild would fund it as a key
    hash and the coins would be lost. Lifted once txbuild is told the chain it pays on."""
    if unpayable_versions(params):
        raise ValueError(f"{params.label} script (P2SH) addresses share their version byte with another chain's "
                         f"ordinary addresses, so this arcade does not make one on {params.label} yet")


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
    # A Dogecoin node calls its mainnet "main" too; `genesis` tells them apart.
    "doge": "main",
    "ltc": "main",       # and so does Litecoin's
    "ltctest": "test",
    "bel": "main", "lky": "main",   # Bellscoin and Luckycoin too
    "kot": "main",                  # and Kotia
    "wjk": "main",                  # and Wojakcoin
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
    if params.genesis:
        # Dogecoin and Pepecoin both answer "main": block 0 says which it is.
        found = rpc.call("getblockhash", 0)
        if found != params.genesis:
            raise WrongChain(
                f"connected to a node whose block 0 is {found}, not {params.label}'s "
                f"{params.genesis}. Refusing to continue -- check --datadir and --conf.")
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

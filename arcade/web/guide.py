"""The guide, as data, so the interface and the site cannot drift apart.

The same document lives at /home/you/docs/features.md and on the website. It
is repeated here rather than fetched because a user who is offline, behind the
remote tunnel, or who does not know there is a website still has to be able to
find out what the thing in front of them does -- and a page that needs the
internet to explain an application that deliberately does not is the wrong
shape.

A test compares the section titles here against the headings in that file, so
one of them cannot quietly grow a feature the other has never heard of.
"""

from __future__ import annotations

SECTIONS: list[dict] = [
    {
        "title": "The shape of it",
        "blurb": "A sidecar beside your own node. It never holds your keys and "
                 "never asks for a passphrase.",
        "points": [
            ("Runs beside Pepecoin Core or Dogecoin Core on your machine and "
             "reads the chain the node already has."),
            ("Everything it knows it learned by replaying blocks, so two "
             "installations reading the same chain agree without talking."),
            ("The interface binds 127.0.0.1 because it can spend. Reach it "
             "from a phone with Remote, below."),
            ("Messaging is testnet only, deliberately. Tokens, inscriptions "
             "and @tags work on both."),
        ],
    },
    {
        "title": "Messages",
        "blurb": "Private messages between two people, encrypted end to end "
                 "and carried on the chain.",
        "points": [
            ("Sealed to the recipient's key, with the sender authenticated "
             "inside the envelope: a forged sender is recorded as a forgery "
             "rather than shown."),
            ("Your own copy is the only one you will ever have. A sealed "
             "message cannot be read back off the chain even by you -- that is "
             "the encryption working, not a gap."),
            ("A long message or a file is split across several transactions, "
             "sealed once as a whole. A split wallet sends them all at once."),
            ("Pictures go as Large, Medium or Small, re-encoded in your "
             "browser before anything is uploaded. Each size shows its cost."),
            ("A long send says how many of its transactions are in blocks, and "
             "is confirmed only when all of them are."),
        ],
    },
    {
        "title": "The public board",
        "blurb": "Channels anybody can read. Nothing is encrypted and nothing "
                 "can be deleted -- that is the point of them.",
        "points": [
            ("Posts carry a name, text and optionally a file, unsealed, so any "
             "node can reassemble one with no key and no identity."),
            "A picture is a post: you can post one with nothing typed.",
            "Channels are just names. There is no step to create one.",
            "Loads 40 at a time, with a way back through older posts.",
        ],
    },
    {
        "title": "Tokens",
        "blurb": "An Omni-style ledger, indexed from the chain.",
        "points": [
            ("Create a fixed-supply token, or a managed one you can grant and "
             "revoke against later."),
            "Send, grant, revoke, and hand the issuer role to somebody else.",
            ("A transaction the engine cannot understand stops the index "
             "rather than being skipped: a silently skipped transaction is how "
             "two nodes come to disagree about who owns what."),
        ],
    },
    {
        "title": "Inscriptions",
        "blurb": "A file written onto the chain in full and uncompressed, "
                 "owned by an address.",
        "points": [
            ("Any file, any size. No policy ceiling -- the only limit is "
             "arithmetic, at about 498 MB."),
            ("An immutable JSON field travels with it, covered by its hash, so "
             "it is checked for validity before it is paid for."),
            ("Owned and transferable. Bound to an address rather than to a "
             "satoshi, so spending your coins never moves one by accident."),
            ("Numbered in chain order, so every node replaying the same chain "
             "assigns the same numbers."),
            ("1 MB costs about 198 to send, of which 178 is dust you can sweep "
             "back: about 20 net."),
            ("An inscribed page can call this node: /content/<id> for the "
             "bytes, and /r/... for block height, metadata, balances, @tags "
             "and what your wallet holds."),
        ],
    },
    {
        "title": "Collections",
        "blurb": "A set of inscriptions that belong together -- a HashLips "
                 "build, or anything that names itself the same way.",
        "points": [
            ("Filed from the chain: JSON with a name like 'Doge Punks #12' is "
             "edition 12 of Doge Punks, by its creator. Every node files the "
             "same sets, and a set cannot be edited or hijacked afterwards."),
            ("One set is one creator's, so nobody can slip their own items "
             "into somebody else's."),
            ("Ordered by edition, with traits counted across the set."),
            ("A wizard inscribes a whole HashLips build: point it at the "
             "build folder, see every item priced, and confirm once. Each "
             "item's own JSON goes into its inscription."),
            ("Pause, resume, survive a crash: every transaction is written "
             "down the moment the node takes it, and a run left running "
             "carries on at the next start without paying for a piece twice."),
            ("Messages still go through while a run is on."),
        ],
    },
    {
        "title": "@tags",
        "blurb": "A handle that belongs to an address.",
        "points": [
            "One name to an address, one address to a name.",
            "First claim wins, in chain order.",
            ("Changing your tag frees the old one for anybody else -- holding "
             "names you no longer use is how a namespace fills with nothing."),
            ("Lower case, a-z 0-9 and underscore, 2 to 24 characters. No "
             "hyphens or dots, so two tags can never look alike."),
            "A claim fits an OP_RETURN: naming yourself is one small transaction.",
        ],
    },
    {
        "title": "The address book",
        "blurb": "Who is who, and where to pay them. Stored only on this "
                 "computer -- not published, not on any chain.",
        "points": [
            ("A contact holds a name, a testnet and a mainnet address, a "
             "contact code and your own notes."),
            ("A name you typed always wins over one read off the chain: a "
             "published name is a claim rather than proof."),
            ("Introduce myself sends your name and addresses with a first "
             "message to somebody new. Untick it to stay anonymous."),
            ("Scanning lists people who published a messaging key and are not "
             "already in your book. Publishing is optional."),
        ],
    },
    {
        "title": "Remote",
        "blurb": "Your wallet on your phone, from anywhere, for a while.",
        "points": [
            ("Open the tunnel, choose 4 hours, 12 hours or a day, scan the QR "
             "code. No account and nothing to change on your router."),
            ("The random address is not the secret. The QR carries a key; the "
             "address alone opens a locked page."),
            "It closes itself, and the bot RPC is never reachable through it.",
        ],
    },
    {
        "title": "The bot RPC",
        "blurb": "A JSON-RPC endpoint for programs, in Omni Core's vocabulary.",
        "points": [
            ("omni_getbalance, omni_send, omni_listproperties and the rest, so "
             "anyone who has scripted against Omni already knows it."),
            ("Cookie authentication like bitcoind. A browser tab cannot present "
             "the cookie, so a web page cannot reach it."),
            ("Sending is two calls: one builds and signs, the other broadcasts "
             "exactly those bytes. A bot sees the fee before it pays it."),
        ],
    },
    {
        "title": "Node-to-node messages",
        "blurb": "One arcade talking to another, for marketplaces and bots.",
        "points": [
            ("The same sealed envelope as a private message, with its own type "
             "so machine traffic never lands in a human conversation."),
            ("Its own cursor, so a program can read a queue and resume exactly "
             "where it stopped."),
            ("Every message says what API it speaks, inside the ciphertext "
             "where it cannot be edited in flight."),
        ],
    },
    {
        "title": "Wallets, keys and backup",
        "blurb": "Your wallet is your identity. There is nothing to write down.",
        "points": [
            ("Two-step sends: the decoded transaction and its fee are shown "
             "before anything is broadcast."),
            ("Change comes back to the address that paid, so an ordinary send "
             "cannot quietly empty your messaging identity."),
            ("Split for fast sending cuts the wallet into many small outputs so "
             "the pieces of a long message go at once."),
            ("Back up wallet.dat and everything comes back with it: coins, "
             "messages and your published key."),
        ],
    },
    {
        "title": "What it does not do",
        "blurb": "Worth knowing, and worth saying plainly.",
        "points": [
            ("No custom peer-to-peer protocol: an unknown command is dropped "
             "without being forwarded, so it would not propagate."),
            "No encrypted messaging on mainnet.",
            ("No send-to-owners: the engine does not implement it and such a "
             "transaction would stop the index."),
            ("No NFTs or exchange yet -- both are marked unbuilt rather than "
             "half-present."),
            ("It cannot read your sent messages back off the chain. Nothing "
             "can. That is what sealing to the recipient means."),
        ],
    },
]

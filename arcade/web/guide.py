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
            ("Name an inscription in a post and the board shows a card for "
             "it -- number, name, type and size from this node's own index, "
             "with a button to the viewer. Never the inscription's page: a "
             "post is a stranger's, and the viewer has the sandbox."),
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
        "title": "NFTs",
        "blurb": "A file written onto the chain in full and uncompressed, "
                 "owned by an address -- an inscription in the code, an NFT "
                 "in the interface.",
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
            ("It can sell itself: tick the mintpad at the pricing step, name "
             "a price in coins or in a token, and the run inscribes a mintpad "
             "when the last item is on its way -- last, and only if nothing "
             "failed. Untick it and nothing extra is inscribed."),
            ("Pause, resume, survive a crash: every transaction is written "
             "down the moment the node takes it, and a run left running "
             "carries on at the next start without paying for a piece twice."),
            ("Messages still go through while a run is on."),
        ],
    },
    {
        "title": "Shops and swaps",
        "blurb": "A shop is an inscription. Its JSON says what it sells; the "
                 "page inscribed with it is the storefront.",
        "points": [
            ("The terms are on the chain: what each listing gives and takes -- "
             "coins, a token amount, an inscription, or a random item of the "
             "owner's collection, which is what a minting event is. Both "
             "wallets read them from their own ledgers; no page is trusted "
             "about a price."),
            ("One transaction or nothing: both parties sign the same "
             "transaction, and the engine moves both legs when the block "
             "lands or refuses the whole thing."),
            ("A sale reserves what it sold until its block is indexed: until "
             "then the ledger still calls it the seller's, and a second buyer "
             "would be offered the same piece and pay a fee to be told no."),
            ("The seller is not asked. The shopkeeper answers every order "
             "from the block watcher: offers exactly what the listing says, "
             "checks the buyer's half against exactly that offer, and only "
             "then signs. It cannot sell what the JSON does not list, or "
             "from a shop this wallet did not create and hold."),
            ("It answers orders, not answers: an answer carries the txid it "
             "answers and whether it worked, an order carries neither. Two "
             "shopkeepers that could not tell them apart would refuse each "
             "other's refusals for ever, a message a block from each."),
            ("The buyer is asked once, in the approvals pop-up, with the "
             "transaction as built. Approve signs the buyer's half only."),
            ("A page this wallet created and still holds is your own words: "
             "it may send from this wallet without asking, and the send is "
             "listed with the approvals as one from a page of your own."),
            ("Testnet only, like the messages it travels on."),
        ],
    },
    {
        "title": "The exchange",
        "blurb": "Everything for sale on a chain, read from the chain itself.",
        "points": [
            ("Offers: what somebody has offered for an NFT of yours, and what "
             "you have offered for somebody else's."),
            ("Mintpads: every pad with pieces left. It appears because it is "
             "on the chain, not because anybody listed it, and drops off when "
             "it mints out."),
            ("Tokens: what tokens are being sold for. Shops at prices their "
             "sellers set, not a book of bids and asks."),
            ("Marketplace: single NFTs for sale, collection or not. One that "
             "has moved drops off by itself -- nothing is un-listed, because "
             "nothing was listed anywhere but the chain."),
            ("Charts of what actually traded, for tokens and for NFTs: there "
             "is no book to draw, so what is drawn is what people paid. A day "
             "with no trade is a dot, not a price, and one currency to a "
             "chart."),
            ("An offer can be made on any NFT, listed or not. It is not a "
             "PSBT: the buyer cannot build the transaction, since the "
             "seller's own output must be its first input. It is a message "
             "naming the item and the price, and the holder's yes builds the "
             "rest."),
            ("Your wallet signs its half of an answer automatically only if "
             "the answer names the same item and the same price you offered. "
             "An offer on something of YOURS is never answered without you."),
        ],
    },
    {
        "title": "Approvals",
        "blurb": "A page or a program can ask this wallet to send something. "
                 "It cannot send.",
        "points": [
            ("One queue for coins, tokens, inscriptions and the buyer's half "
             "of a swap, asked for by an inscribed page in its sandbox or by "
             "a program on the bot RPC."),
            ("Nothing is built on the caller's word: when you look, the "
             "transaction is shown with its fee and every output, and goes "
             "out only when you press Approve. Refusing costs nothing."),
            ("Opens in front of the inscription that asked, the moment it "
             "asks; shown on every other page, and on the phone over the "
             "remote tunnel. The page cannot see the pop-up or press it."),
            ("What was never asked is still written down: a page of your own "
             "and a shop of your own send without asking, and both are "
             "written to the same queue as they go, already decided, so one "
             "page shows everything this wallet signed for something that is "
             "not a person."),
            ("A request nobody answers within an hour expires; no more than "
             "twenty wait at once."),
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
            ("Claim or change one from the address book, in two steps like a "
             "send. A name that is malformed, reserved, already yours or "
             "already somebody else's is refused before it costs anything."),
            ("Shown wherever this wallet says who somebody is: a name you "
             "typed wins, then the @tag, then the address. A stale index "
             "shows the address, never a name it cannot vouch for."),
            ("Published with your key, along with your address on this chain "
             "and the same wallet's address on the other one. The tag is the "
             "part a reader can check against their own index; the name you "
             "type is not published at all."),
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
            ("Introduce myself sends your @tag and addresses with a first "
             "message to somebody new. Untick it to stay anonymous. There is "
             "no name to type: the @tag is the only name this wallet gives "
             "out for you, because it is the only one a reader can check."),
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
            ("Inscribed pages get a second, unnamed address of their own, "
             "good for nothing but their content and the page API."),
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
            ("An inscribed page can use it too: it sends as this wallet and "
             "reads only the answers to what it sent. No approval -- a "
             "message moves nothing and lives on testnet -- and thirty an "
             "hour per page."),
        ],
    },
    {
        "title": "Wallets, keys and backup",
        "blurb": "Your wallet is your identity. There is nothing to write down.",
        "points": [
            ("Three tabs -- Wallet, Tokens, NFTs -- answering what you have: "
             "coins per chain, token balances with the form that sends them, "
             "and the inscriptions you hold. The Tokens and NFTs sections "
             "answer what exists on the chain."),
            ("One balance a token, not one an address: a wallet holding a "
             "token on four addresses holds one balance of it. A send comes "
             "out of one address, and the wallet picks one holding enough."),
            ("Two-step sends: the decoded transaction and its fee are shown "
             "before anything is broadcast."),
            ("Change comes back to the address that paid, so an ordinary send "
             "cannot quietly empty your messaging identity."),
            ("Split for fast sending cuts the wallet into many small outputs so "
             "the pieces of a long message go at once."),
            ("Fees are what the miner counts: the node prices a transaction "
             "by its virtual size, twenty bytes per signature operation, and "
             "every funding call raises the rate until a block would take "
             "it. A plain send is unchanged; an inscription pays about "
             "0.004 PEP per data output."),
            ("Mine a testnet block from the wallet page, any time. The page "
             "says how many hashes so far and how long a block takes at "
             "today's difficulty, and Stop mining stops after the batch it "
             "is on."),
            ("Back up wallet.dat and everything comes back with it: coins, "
             "messages and your published key."),
            ("Publishing your key lives on the address book, beside the name "
             "it publishes. There is no page of keys and fingerprints."),
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
            ("No order book: a shop sells at the price its inscription says, "
             "to whoever comes, and an offer is made on one NFT at a time. "
             "Nothing matches bids to asks; the Exchange's Tokens tab is a "
             "list of shops, not a book."),
            ("It cannot read your sent messages back off the chain. Nothing "
             "can. That is what sealing to the recipient means."),
        ],
    },
]

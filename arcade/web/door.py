"""Who may reach what, and how a public instance differs from a wallet.

Until now this application was one person's wallet on one person's machine,
reachable from that machine and — while a Cloudflare tunnel was open — from
a phone holding the token out of a QR code. `remote_guard` was the whole
door: no token, no entry, everything else open.

Going live on a real domain deletes that arrangement. There is no token to
hold, the address is public, and the first stranger to type it in would
otherwise get the operator's wallet: their coins, their messages, their
keys. **So the tunnel's guard is not removed, it is replaced**, and the
replacement is the opposite shape.

**An allowlist, not a blocklist.** A blocklist is wrong the first time
somebody adds a route and forgets — and the route they forget is the one
that spends. So a public instance serves what is named here and refuses
everything else, including every route added after this file was written.
The cost of that choice is that a new public page has to be added here to
appear, which is a visible, one-line kind of mistake; the cost of the other
choice is somebody's coins.

**Almost every POST is refused.** Every FORM in this application spends
the NODE's wallet — posting to the feed, tipping, inscribing, trading —
and none of that is per-account yet, so on a public instance none of it
may be reached.

The exceptions are named one at a time and deliberately, and they are the
routes where an account acts with a key the node never sees: signing in,
and the build-and-sign handshake (`/account/*`). Those are safe to be
public for two reasons that are both load-bearing — each one works only on
the account making the request, and the node broadcasts the transaction it
OFFERED rather than whatever came back signed.

**A session does not open any of this.** Holding a seat says who you are;
it does not make the operator's wallet yours. Nothing here consults the
session at all, which is the point: there is no combination of cookies
that turns a public instance into the operator's wallet, because the
refusal is about the ROUTE and not about the visitor.
"""

from __future__ import annotations

import ipaddress
import re
import urllib.parse

#: Pages and files a stranger may read. Exact paths, matched whole.
PUBLIC_PAGES = frozenset({
    "/",                      # the splash: seats, and how to join
    "/join",
    "/join/keys",
    "/clone",
    "/instances",             # who runs which arcade, as the chain says
    "/.well-known/dogecoinarcade.json",   # this arcade's own claim, to check it
    "/moderation/verdicts",   # what this arcade covers, so a page can label it
    "/source.tar.gz",
    "/source.tar.gz.sha256",
    "/source.rev",
    "/feed",
    "/guide",
    "/docs",
    "/affiliates",            # who the arcade works with (2026-10-15)
    "/affiliates.json",       # the same, as data: the arcades a game may move a player to (2026-10-23)
    "/collections",
    "/tokens",
    "/nfts",
    "/create",
    "/create/chain",          # which chain this browser creates on (a cookie, no spending)
    "/create/ord",            # inscribe an ord from the account's own wallet (signed in the tab)
    "/create/name",           # claim a name ("p":"dns") as an ord, the same way
    "/inscriptions",
    "/exchange",
    "/exchange/token20/search",       # DRC-20 / PRC-20 ticks as they are typed into the exchange's search boxes
    "/exchange/pairs",            # the "open a pair" box: two token ids to a token/token pair's page
    "/apps",                  # every inscribed app (arcade/games.py; the Apps tab since 2026-10-23)
    "/home",                  # the installed app's home screen (home.html, PWA only, 2026-10-24)
    "/apps/store-icon.png",   # the App Store's icon on it
    "/games",                 # the old name: redirects to /apps
    "/account/escrows",       # an account's own escrows (needs a session)
    "/account/doginals",      # this account's own Doginals on Dogecoin, to list (needs a session)
    "/account/segwit/balance",  # the account's registered segwit address (native or P2SH), test chains (needs a session)
    "/account/imported",      # the account's own sealed record of imported wallets, and their addresses (needs a session)
    "/account/imported/busy", # whether an import on a chain must wait for a rescan (needs a session)
    "/account/doge-stray",    # coins at the account's testnet key's Dogecoin address, to move home (2026-10-17)
    "/doginals/listings",     # Doginals listed for DOGE (the Exchange's Doginals tab)
    "/launch",
    "/launches",
    "/mintpad/new",
    "/restore",
    "/listings",              # legs this node holds signatures for. It can
                              # spend none of them: the key is not here.
    "/events",                # what an open page polls to know to refresh
    "/favicon.ico",
    "/icon-32.png",
    "/coin-pep.png",
    "/coin-pep-test.png",
    "/coin-doge.png",
    "/icon-180.png",
    "/icon-192.png",
    "/icon-512.png",
    "/manifest.webmanifest",  # the app, installable (a PWA)
    "/sw.js",                 # its service worker: an offline page and push, no cache of pages
    "/offline",
    "/push/key",              # the key a browser subscribes to this node's pushes with
    "/account/push/news",     # what woke the phone: who wrote, never what (signed in)
    "/account/ring/apps",     # the apps an account lets ring it (arcade/ring.py, signed in)
    "/admin/login",           # the remote admin password page (rate-limited)
    "/signin.js", "/base.css",
    "/coins.js",
    "/wallet.js",
    "/messaging.js",
    "/qrscan.js",
    "/bip39-english.txt",
    # The door itself. Named one by one rather than as a `/auth/` tree: an
    # allowlist that opens a whole subtree opens whatever is put in it
    # later, and this is the subtree a stranger is invited into.
    "/auth/challenge",
    # An account from another arcade, for the sign-in page (2026-10-19). Its handler
    # fetches only from arcades the operator lists (`import_from`) when asked from outside.
    "/signin-import",
    "/auth/who",
    "/auth/door",
    "/me",
    "/me/messages",
    "/me/notifications",
    "/me/contacts",
    "/me/access",             # the games this browser allowed, and turning them off (2026-10-23)
    "/me/backup",
    "/me/nfts",
    "/me/runs",
    "/me/wallet",
    "/me/wallet/tokens",
    "/account",
    "/account/feed",
    "/account/messages",
    "/account/mailbox",
    "/account/find",
    "/account/nfts",
    "/account/nfts/collections",      # what this account holds, by collection
    "/account/nfts/pieces",           # one page of one of those collections
    "/account/tokens",
    "/account/inscribe/unfinished",
    "/account/dust",                  # coins in this account's own payload outputs
    "/account/coins",                 # this account's own coins on one chain (the Doginals check)
    "/account/doginal-index",         # how far the chain's ord index has walked (the Doginals fill in as it goes)
    "/account/token20",               # this account's DRC-20 / PRC-20 balances, from the most complete ord index
    "/account/listed",                # what this account's standing listings hold (the Wallet shows them held)
    "/account/ord/runs",              # this account's ords being inscribed, and the ones made here
    "/account/ord/confirmed",         # how many blocks this account's transaction has (a collection's lanes wait for one)
    "/account/ord/chains",            # the chains an ord is inscribed on here, each with its rules
    "/account/ord/name",              # is a name valid and free on a chain (Creator -> Names, 2026-10-26)
    "/account/mesh/site",             # a .mesh name's holder and pages, for its holder's editor (the .mesh web)
    "/account/arcades",               # the arcades on the mesh, for the Your arcade page's Arcades card
    "/account/favorites",             # the markets this account starred (Exchange, 2026-10-17)
    "/account/homeapps",              # the icons on this account's home screen, in its order (PWA, 2026-10-24)
    "/account/badges",                # the red counts of its tabs, for the home screen (PWA, 2026-10-24)
    "/account/orders/delegated",      # where this account's node-filled orders stand (2026-10-17)
    "/account/talk/inbox",            # sealed swap messages the mesh brought this page (talk_relay)
    "/account/activity",              # this account's own coin history (signed in)
    "/account/chain-stats",           # each chain's node operators and numbers, for Your wallets (2026-10-18)
    "/account/standing/fillable",     # this account's buy orders a sell order meets
    "/account/claimpools",            # this account's own prize pools, to withdraw
    "/account/pagesends",             # what pages have asked this account to send
    "/account/run/item",              # one item of this account's own run, as uploaded
})

#: Prefixes a stranger may read. Everything under them is public, so each
#: one is a promise about a whole subtree: nothing under these may ever
#: learn to spend.
PUBLIC_TREES = (
    "/account/llm/",          # the Exchange's LLM tab: offers, the account's own balance, grant words and keys
    "/exchange/llm/",         # one model's own page on the model exchange (2026-10-23)
    "/v1/models",             # the LLM API's model list, for an API key
    "/signup/",               # is this name free
    "/signin/",               # somebody's encrypted wallet, by name
    "/coins/walk/",           # the shared walk: whether this arcade reads a chain for others, a job's state (2026-10-25)
    "/vendor/",               # the two vendored crypto libraries
    "/docs/",                 # the whole written record, as shipped
    "/account/who/",          # where to write to somebody, from the chain
    "/bootstrap/",            # a copy of the index, and its manifest
    "/u/",                    # somebody's feed, profile and holdings
    "/content/",              # inscribed content, as the chain has it
    "/r/",                    # the page API: what an inscribed page may ask
    "/realtime/",             # game rooms over the mesh: hello and the stream (realtime.py)
    "/collections/",
    "/tokens/",               # a token's own page
    "/exchange/collection/",
    "/mintpad/",              # an account's mintpad: pre-signed listings, read-only here
    "/exchange/pair/",
    "/exchange/pairs/",       # a token/token pair's page (2026-10-06)
    "/exchange/x/",           # a cross-chain market: a testnet asset for mainnet PEPE (2026-10-06)
    "/account/doginal-tx/",   # the Wallet's Doginals check, asked by the node for the account's own coins (2026-10-15)
    "/exchange/swap/",        # every cross-chain market, through lock scripts (2026-10-15)
    "/exchange/trade/",       # one trade: who sold, who bought (2026-10-17)
    "/exchange/history",      # the reader's own trades (2026-10-17)
    "/exchange/swap-trade/",  # one finished lock-script swap (2026-10-17)
    "/exchange/coins/",       # a coin pair: redirects to its swap market
    "/exchange/ord/",         # a Doginals/Pepinals collection described by an inscription (2026-10-17)
)

#: Whole paths with something variable in the middle of them, where a tree
#: would be too wide. This one is the piece page, and it is the only route
#: under `/inscriptions/` that a stranger is sent to rather than sends
#: themselves: it is where a shop lives, and `/exchange/collection/...`
#: already links "Buy it" straight at it. The rest of that prefix is the
#: wizard, the collection build and the send form, and every one of them
#: either spends or names a folder on this machine. The id is matched the way
#: `content._key` reads one -- a 64-character txid or an inscription number --
#: so the door cannot refuse a path the route would happily serve.
#: Nothing is disclosed on the page itself: `inscription_view` reads this
#: node's wallet only when it is the operator's own copy of the page.
PUBLIC_SHAPES = (
    # The .mesh web drawn in a browser (meshweb.py, 2026-10-26): public pages, read-only.
    re.compile(r"^/meshweb(?:/[A-Za-z0-9._/-]{1,200})?$"),
    # The Browser (meshweb.py, 2026-10-26): every name's site, read-only; /site serves a site's page sandboxed.
    re.compile(r"^/browser(?:/[^\s]{1,300})?$"),
    re.compile(r"^/site/[a-z0-9-]{1,24}/[^\s]{1,300}$"),
    re.compile(r"^/account/ring/[0-9a-f]{16}$"),       # the ring a tapped notification opened (signed in)
    # Every chain's coin icon (2026-10-18: Litecoin's two joined PEP's and DOGE's); the route serves only files there.
    re.compile(r"^/coin-[a-z0-9-]{2,24}\.png$"),
    # One game's discussion: read-only, like the launch thread it is.
    re.compile(r"^/games/[0-9a-f]{64}$"),
    re.compile(r"^/games/[0-9a-f]{64}/access$"),   # is a page a game (the wallet gate, base.html arcadeGameGate)
    # A game's own front door: its sign-up, or the game itself once signed in.
    re.compile(r"^/play/[A-Za-z0-9._-]{1,64}$"),
    re.compile(r"^/inscriptions/(?:[0-9a-fA-F]{64}|[0-9]{1,12})/view$"),
    # The bare address of a piece, which only forwards to /view (2026-10-09).
    re.compile(r"^/inscriptions/(?:[0-9a-fA-F]{64}|[0-9]{1,12})$"),
    # The same piece filling the screen (2026-09-29): read-only, and the
    # frame in it is the same sandbox the piece page uses.
    re.compile(r"^/inscriptions/(?:[0-9a-fA-F]{64}|[0-9]{1,12})/full$"),
    # An account's own collection run. A shape rather than a `/me/run/` tree
    # for the reason the tree above gives: a prefix says everything under it is
    # readable, and tomorrow's `/me/run/<id>/export` would arrive already
    # public. Matched to the id `_runs.create` actually mints --
    # `secrets.token_hex` -- and wide enough that the door cannot refuse a path
    # the route would answer. What makes it safe at all is that route: it
    # answers a run that is not the asker's with the same words it answers a
    # run that does not exist, and it has no path under `/me/run/` that spends,
    # names a folder, or reads anybody but the asker's rows.
    re.compile(r"^/me/run/[0-9a-f]{6,32}$"),
    # One launch's whole discussion, readable by anyone (2026-09-27).
    re.compile(r"^/launches/[0-9a-f]{64}$"),
)

#: Prefixes that look public by the rules above and are not. Checked FIRST,
#: because `/tokens/` is a public tree and `/tokens/create` is not, and a
#: rule that depends on the order two prefixes happen to be listed in is a
#: rule that will be got wrong.
NEVER_PUBLIC = (
    # The collection wizard's preview reads a BUILD FOLDER named in the
    # query string -- a path on the operator's own disk. It only serves what
    # the build lists, but a stranger naming directories and being told
    # which ones look like builds is a stranger reading somebody's
    # filesystem. It was in the public list for one afternoon, on the
    # grounds that a preview is read-only; read-only of the wrong machine.
    "/inscriptions/collection",
    "/tokens/launchpad/preview",   # a wizard's preview: no public need
    "/tokens/create",
    "/tokens/send",
    "/tokens/chain",
    "/inscriptions/create",
    "/r/send",                # the page API's "ask the wallet to pay" route
    "/r/wallet",              # which wallet is looking: not a stranger's business
)

#: Public pages must not link somewhere a public instance refuses, or the
#: site is broken for exactly the people it is for. Checked by a test rather
#: than remembered.
LINKED_FROM_PUBLIC = ("/content/", "/collections", "/feed", "/u/", "/docs",
                      "/clone", "/join", "/r/")

#: The only POSTs a public instance accepts, and each one is here because
#: somebody decided it should be. Signing in, and the build-and-sign
#: handshake -- the first routes that are public AND do something. They
#: build an offer and take signatures over it, and what makes that safe is
#: that each works only on the account making the request, and the node
#: broadcasts what it OFFERED rather than what came back.
#: `/auth/challenge` is the one entry here that is not a POST route at all --
#: app.py serves it with `@app.get` alone, so a POST that clears this door is
#: answered 405 by the router and nothing can come of it. Dropping it would be
#: equally correct; it is named so that nobody reads that 405 as the door having
#: opened something. (the door census, 2026-09-27)
PUBLIC_POST = ("/auth/challenge", "/auth/login", "/auth/logout",
               # every session of the signed-in account ends, its own only (issue #13, 2026-10-25)
               "/auth/logout-everywhere",
               # the LLM service (llmweb.py, 2026-10-23): each acts only for the signed-in account, its own grants,
               # top-ups and keys; /v1 answers only an API key the account made
               "/account/llm/grant", "/account/llm/topup", "/account/llm/chat", "/account/llm/keys/new",
               "/account/llm/keys/drop", "/v1/chat/completions", "/account/llm/image", "/v1/images/generations", "/account/llm/helper", "/account/llm/pictures/delete",
               "/v1/images/edits",
               # which chains' wallets an account's Wallet tab shows (2026-10-18)
               "/account/wallets",
               # a tab's own lock-script trades (2026-10-13)
               "/account/trade/spend", "/account/trade/spend/sign", "/account/trade/facts",
               "/account/trade/lock", "/account/trade/watch",
               # game rooms over the mesh (realtime.py): each acts only on the
               # caller's own token, and an account joins only as the address
               # its own browser signed for
               "/realtime/join", "/realtime/send", "/realtime/leave",
               # a trade between two players in a game: the giver signs a leg for
               # one named buyer (its own piece, its own key); the buyer finishes it
               # with /account/fill like any answered leg
               "/account/trade/leg", "/account/trade/leg/sign",
               # a refereed escrow (arcade/escrow.py): the account's own escrow,
               # its own deposits and reclaims; a release is the game's judge's to
               # decide, whoever asks
               "/account/escrow/open", "/account/escrow/deposit",
               "/account/escrow/reclaim", "/account/escrow/reclaim/sign",
               "/r/escrow/release",
               # a .mesh page's bytes, checked, for the name's own holder to inscribe from its tab (the .mesh web)
               "/account/mesh/draft",
               # a transaction the account's own T-Deck built and signed: only its own unspent coins, none inscribed
               "/account/broadcast",
               # a T-Deck on WiFi (tdecklink.py): its LoRa frames over HTTPS; the account proves itself by its own
               # signed LOGIN, and the token it sends is its own secret
               "/r/tdeck/link",
               # a game's state for its pieces: anybody asks, the game's judge decides
               "/r/state/update",
               "/admin/login",
               "/auth/password",
               "/account/address", "/account/announce", "/account/post",
               "/account/react",
               "/account/mainnet",        # own opt-in to real coins, own flag
               # what the account says about itself: a face, a line, a link.
               # The announcement carries the account's own key and its own
               # tag, which the node reads off the chain rather than out of
               # the request, and this node signs nothing of it.
               "/account/profile",
               "/account/write",
               # the account's own sealed message history (accounts.Mailbox)
               "/account/mailbox",
               # one payment to the account's own address, split for a mintpad
               "/account/split",
               "/account/list/cancel",
               "/account/answer/withdraw",
               "/account/answer/withdrawn",
               # pre-signed offers: the buyer's signatures, withdrawing, completing
               "/account/offer/bid", "/account/offer/bid-build", "/account/offer/withdraw",
               "/account/dust/sweep",
               "/account/offer/decline",
               "/account/token-bid/decline",
               # paying the operator for extra room (the account's own coins)
               "/account/boost",
               # buy orders this node holds (standing.py): place, lots, sign, cancel
               "/account/standing", "/account/standing/lots",
               "/account/standing/sign", "/account/standing/cancel",
               "/account/accept/complete",
               # the Restore page's new encrypted wallet, signed in by the words' key
               "/account/vault",
               # how the account's own mintpad page looks (presentation only)
               "/account/mintpad", "/account/mintpad/inscribe", "/account/tokenpad/inscribe", "/account/mintpad/announce", "/account/mintpad/announced",
               "/account/claim", "/account/send", "/account/inscribe", "/account/inscribe/abandon",
               "/account/run/start", "/account/run/plan", "/account/run", "/account/run/piece",
               "/account/run/stop",
               # a folder bigger than one request, in batches; and an item of
               # more than one transaction, sent the one-big-file way, reported
               # done. None of the three broadcasts or spends anything.
               "/account/run/upload", "/account/run/done",
               # the two ends of a run that stopped by itself: the refused
               # pieces put back in line, and a run this node has been keeping
               # pictures for forgotten for good. Neither broadcasts, and both
               # refuse a run that is not the asker's -- which is why the
               # pictures can be deleted: the account that uploaded them is the
               # only one whose word removes them.
               "/account/run/retry", "/account/run/delete",
               "/account/nft/send", "/account/nft/sell",
               # one Doginal on purpose: the only send that spends a coin carrying one
               "/account/doginal/send",
               # an offer on somebody else's piece: one OP_RETURN saying what
               # this account would pay, funded from its own address and
               # signed in its own tab. This node broadcasts it and reserves
               # nothing -- not the piece, which is not this node's to reserve,
               # and not the account's coins, which are the account's to spend
               # on the day somebody says yes.
               "/account/offer",
               # the pair that lists a piece: the first shows a leg, the
               # second files the leg it is sent back with. Neither one
               # broadcasts -- a listing is a signature the node holds, not
               # a transaction it makes.
               "/account/list", "/account/list/sign",
               # a prize pool of token lots, each a leg like the pair above
               # files; it builds legs (or the split they stand on) and
               # broadcasts nothing.
               "/account/claimlots",
               # a prize pool's own address: registered, funded, and closed.
               # Each is built here and signed in the tab; none spends by itself.
               "/account/pools/open", "/account/pools/fund", "/account/pools/close",
               "/account/pools/legs",
               # this account's yes or no to a page that asked it to send
               "/account/pagesends/answer",
               # claiming one lot of a prize pool read off the chain: built here
               # from the pool's own inscription, signed in the claimer's tab.
               "/account/prize", "/account/prize/sign", "/account/prize/take",
               # a bundle: several things each way in one trade (2026-10-04)
               "/account/bundle/build", "/account/bundle/fill", "/account/bundle/fill/sign",
               # a refereed pool (2026-09-29): a seed for the player's next run,
               # and the referee's own two, which other nodes call. A seed is a
               # random number; a signature comes only for a replay the judge
               # passes, over a claim that pays the wallet that played.
               "/account/referee/seed", "/r/referee/seed", "/r/referee/sign",
               # a verified result for an inscription (2026-09-30): the referee's
               # signed verdict on one run, for the address its seed was issued to
               "/account/referee/attest", "/r/referee/attest",
               # an account brought from elsewhere: find the coins it already held
               "/account/coins/backfill", "/account/coins/reconcile", "/account/coins/merge",
               # the pair that fills one of those listings: the first shows a
               # buyer the transaction, the second pastes its signatures onto
               # the seller's leg and broadcasts. Neither reaches
               # `/account/sign`, and neither could: this node signs nothing on
               # this path, and an account that made the listing is refused at
               # both halves.
               "/account/buy", "/account/buy/sign",
               # Doginals for DOGE (arcade/doginal_market.py, [Claude 2026-10-15]): the same build-sign
               # pairs, for a listing whose seller signs one input and a buyer who completes it here.
               "/account/doginals/list", "/account/doginals/list/sign",
               "/account/doginals/buy", "/account/doginals/buy/sign", "/account/doginals/cancel",
               "/account/doge-stray/move", "/account/doge-stray/move/sign",
               "/account/fill", "/account/fill/sign", "/account/fill/check",
               # the pair that answers one of those offers with the piece: the
               # first shows the leg at the buyer's price, the second checks the
               # signatures really say that offer and says where to mail them.
               # Neither broadcasts -- an answer is a signature plus a message,
               # and the message is `/account/talk`'s, sealed in the tab that
               # holds the key this node was never given.
               "/account/accept", "/account/accept/sign",
               # the token book, for an account: an order and a cancel, each one
               # message and a fee, funded from the account's own address and
               # signed in its tab. Nothing here spends the node's wallet, which
               # is the only reason the operator's `/exchange/order` is not in
               # this list. Nor does anything here reserve anything: the engine
               # holds an ask's tokens back when it indexes the order, and coins
               # cannot be reserved at all (D-048).
               "/account/order", "/account/order/cancel", "/account/order/take",
               # and the read that says what those two did. Same queue
               # arithmetic as the take, no transaction, nothing reserved: a
               # program that can put a price on the book and take it off has to
               # be able to ask what is still standing, or it is hoarding its own
               # txids (a tester, 2026-09-27). A stranger gets the door's 403
               # with the reason in it, not a 404 that leaves them guessing
               # whether the route exists (S-work-3).
               "/account/order/list",
               # token/token pairs (2026-10-06): the same as an order and a
               # cancel above, one message and a fee from the account's own
               # address, signed in its tab; the engine matches it in its block.
               "/account/pair-order", "/account/pair-order/cancel",
               # the cross-chain book (2026-10-06): the account's deposit, signed in
               # its tab like any send, and a cancel of its own order
               "/account/x/order", "/account/x/cancel",
               # and the one read that makes the book takeable by a tab: it
               # builds nothing and sends nothing, it only says which order the
               # queue chose, what it prices to, and which key the question has
               # to be sealed to -- a key that lives in this node's address book
               # and nowhere a browser can look. The message it is advice about
               # goes out through `/account/talk`, which is already here.
               "/account/take",
               # and the two halves that turn the answer into a trade. The first
               # builds the account's side out of the maker's offer and signs
               # nothing -- it could not, since the key is in a tab and not here
               # -- and says which inputs that tab's key starts at. The second
               # pastes the signatures back and hands the transaction over, and
               # broadcasts nothing: the maker's half is still unsigned, and it
               # is the maker's node that finishes the trade, exactly as with
               # `/account/shop` below. Nothing on either side of this pair can
               # spend a coin the asking account does not hold, and an offer that
               # is stale, expired, or addressed to somebody else is refused
               # before either one answers.
               "/account/take/build", "/account/take/sign",
               # a shop, bought by an account rather than by this wallet. The
               # door offers three transactions and broadcasts the two messages
               # out of them; the trade itself is not broadcast here at all,
               # because it is the shop's node that finishes it. What is spent
               # is the buyer's, and an account buying from its own shop is
               # refused at every half.
               "/account/shop", "/account/shop/sign",
               # a page talking to another node with an account's key instead
               # of this machine's wallet. What arrives is ciphertext and a
               # destination; the node builds the carrier, never reads it, and
               # offers it for the account's signature -- and the account that
               # lives on mainnet is refused, because sealed messages are
               # testnet's (D-010).
               "/account/talk",
               "/account/push/subscribe", "/account/push/unsubscribe",
               # ringing a player whose app is closed (arcade/ring.py): ring, which apps may, who may
               "/account/ring", "/account/ring/apps", "/account/ring/allow",
               "/account/token/send", "/account/token/create",
               "/account/token/manage",
               "/account/sign",
               # the account's own segwit address (native or P2SH), watched (test chains only)
               "/account/segwit/register",
               # imported wallets (plan item 18): the account's own sealed record, an address registered with its
               # key's signature, its read-back, taking one off, the main wallet's name -- each the caller's own only
               "/account/imported", "/account/imported/address", "/account/imported/rescan",
               "/account/imported/forget", "/account/imported/name",
               # its signed record fetched from the arcade it was brought from (the caller's own account only)
               "/account/imported/bring", "/account/imported/siblings/drop",
               # what sits on the caller's own coins, many transactions at once (the Wallet's Doginals)
               "/account/doginal-txs",
               # a DRC-20 / PRC-20 send of the caller's own tokens: its three offers, one after the other
               "/account/token20/send",
               # an ord from the caller's own wallet: its commit and reveals, one after the other; its list
               "/account/ord/inscribe", "/account/ord/forget", "/account/ord/quote", "/account/ord/split",
               # an ord collection index worked out (nothing spent; the page inscribes it as the caller)
               "/account/ord/collection",
               # a DRC-20 / PRC-20 token into the caller's own trade lock: its four offers, one after the other
               "/account/trade/lock20",
               # starring a market: the caller's own list of favorites, nothing else
               "/account/favorites",
               # arranging the home screen: the caller's own list of icons, nothing else
               "/account/homeapps",
               # the page holding this account's listed swaps is open (order_presence)
               "/account/orders/alive",
               "/account/orders/cancel",
               "/account/orders/delegate",
               "/account/orders/post",
               "/account/orders/taking",
               # swap messages over the mesh (talk_relay): sealed to another page's key
               "/account/talk/relay",
               # a finished lock-script swap, for its market's history (swap_history)
               "/account/trade/done",
               "/signup")

#: POSTs a stranger may make, by shape: another arcade asking this one for an imported address's coins, with a proof
#: that address's key signed (web/app.py signin_imported_coins; nothing without it).
PUBLIC_POST_SHAPES = (
    re.compile(r"^/signin/[^/]{1,64}/imported-coins$"),
    re.compile(r"^/coins/walk/job$"),      # another arcade's part of a shared walk: blocks to read, never a spend
)

#: What an inscribed page's own hostname may reach, and nothing else. A page
#: in the sandbox has an opaque origin and sends no cookie, so this hostname
#: is its whole key -- good for the content and the page API and nothing
#: else, on a public instance exactly as it was through the tunnel.
PAGES_DOOR = ("/content/", "/r/", "/site/")      # /site/: a site's page, sandboxed as /content is (meshweb.py)


def public_path(path: str, method: str = "GET") -> bool:
    """May a stranger reach this on a public instance?"""
    path = (path or "/").rstrip("/") or "/"
    if any(path == deny or path.startswith(deny + "/")
           for deny in NEVER_PUBLIC):
        return False
    if method.upper() not in ("GET", "HEAD"):
        return path in PUBLIC_POST or any(shape.match(path) for shape in PUBLIC_POST_SHAPES)
    if path in PUBLIC_PAGES:
        return True
    if any(shape.match(path) for shape in PUBLIC_SHAPES):
        return True
    return any(path.startswith(tree) for tree in PUBLIC_TREES)


def speaks_for_the_operator(path: str) -> bool:
    """A route that answers as, or asks, the operator's own wallet.

    The inscribed-pages hostname serves all of /r/, and it is reached from the
    internet: without this, pages.<domain>/r/wallet handed every stranger the
    operator's addresses, tag and balances, and any page opened by anybody
    greeted them as the operator (a tester, 2026-09-26).
    """
    path = (path or "/").rstrip("/") or "/"
    return any(path == deny or path.startswith(deny + "/")
               for deny in NEVER_PUBLIC)


def pages_path(path: str) -> bool:
    """May the inscribed-pages hostname serve this?"""
    return (path or "").startswith(PAGES_DOOR)


#: Headers only an edge adds. Cloudflare sets `Cf-Ray` and `Cdn-Loop` on
#: every request that crosses it and overwrites whatever a client sent, so
#: they cannot be forged away by somebody on the outside. `x-forwarded-for`
#: is weaker -- any proxy writes it -- and is here because a request that
#: claims to have been forwarded is not a request from this machine.
EDGE_HEADERS = ("cf-ray", "cdn-loop", "cf-connecting-ip", "x-forwarded-for")


#: Host names that are this machine whatever DNS says: a browser resolves
#: `localhost` and `*.localhost` itself, and an IP literal is not looked up.
#: Anything else -- a name somebody's DNS points at 127.0.0.1 -- is not here.
LOOPBACK_HOSTS = ("localhost",)
#: Peers that are this machine besides the loopback addresses (the test suite
#: adds Starlette's made-up "testclient"; uvicorn only ever reports an IP).
LOOPBACK_PEERS: tuple[str, ...] = ()


def host_name(host: str) -> str:
    """The name in a Host header: lower case, no port, no IPv6 brackets."""
    host = (host or "").strip().lower()
    if host.startswith("["):
        return host[1:host.find("]")] if "]" in host else ""
    if host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host.rstrip(".")


def _loopback_ip(text: str) -> bool:
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    return (mapped or ip).is_loopback


def loopback_host(host: str, local_hosts: tuple[str, ...] = ()) -> bool:
    """Is this Host header one of this machine's own names?"""
    name = host_name(host)
    if not name:
        return False
    return (name in LOOPBACK_HOSTS or name.endswith(".localhost")
            or name in local_hosts or _loopback_ip(name))


def loopback_peer(peer) -> bool:
    """Did the connection itself come from this machine?"""
    peer = str(peer or "")
    return bool(peer) and (peer in LOOPBACK_PEERS or _loopback_ip(peer))


def from_outside(headers, host: str, public_hosts: tuple[str, ...],
                 peer=None, local_hosts: tuple[str, ...] = ()) -> bool:
    """Did this request arrive from the internet rather than from here?

    One machine is both things at once: the operator's wallet, sitting on his
    desk, AND the arcade on dogecoinarcade.com. A process-wide flag cannot
    express that -- it would either lock him out of his own wallet or serve
    it to the world -- and two processes over one state directory is how a
    database gets two writers.

    So publicness is decided per REQUEST, by where it came from. It is from
    outside when any of these holds:

    * the Host is one of the names published for this node;
    * the request carries a header only an edge or a proxy adds;
    * the connection (`peer`, the TCP address) is not a loopback one;
    * the Host is not one of this machine's own names: localhost,
      *.localhost, a loopback IP, or a name listed in `local_hosts`.

    Local is the narrow case -- a loopback connection, naming the machine by
    a loopback name, through nothing -- so anything not foreseen (a proxy
    that adds no headers, a name nobody configured, a web page whose name
    resolves to 127.0.0.1) lands on the public side. The failure that leaves
    is a local request wrongly called public, which shows the operator a
    splash instead of their wallet -- visible, and harmless.
    """
    name = host_name(host)
    if name and name in public_hosts:
        return True
    if any(header in headers for header in EDGE_HEADERS):
        return True
    if not loopback_peer(peer):
        return True
    return not loopback_host(host, local_hosts)


#: Methods that change nothing, so a request from another site may make them.
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
#: Where other sites' and inscribed pages' requests are expected: the page API
#: and content (an opaque-origin frame), and the realtime rooms (open to any
#: origin on purpose, arcade/web/realtime.py).
CROSS_SITE_PATHS = PAGES_DOOR + ("/realtime/",)


def cross_site(headers, host: str) -> bool:
    """Did a browser send this from a page that is not this very origin?

    `Sec-Fetch-Site` when the browser says it (no page can set or remove
    it); otherwise `Origin`, which a
    browser adds to every cross-origin request and to same-origin POSTs.
    Neither present is a program (curl, a script, another node), never a page
    on another site.
    """
    site = (headers.get("sec-fetch-site") or "").strip().lower()
    if site in ("cross-site", "same-site"):
        return True
    if site in ("same-origin", "none"):
        return False
    origin = (headers.get("origin") or "").strip().lower()
    if not origin:
        return False
    if origin == "null":
        return True
    return urllib.parse.urlsplit(origin).netloc != (host or "").strip().lower()

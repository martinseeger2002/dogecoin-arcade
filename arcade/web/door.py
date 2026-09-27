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

import re

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
    "/collections",
    "/tokens",
    "/nfts",
    "/create",
    "/inscriptions",
    "/exchange",
    "/launch",
    "/launches",
    "/mintpad/new",
    "/restore",
    "/listings",              # legs this node holds signatures for. It can
                              # spend none of them: the key is not here.
    "/events",                # what an open page polls to know to refresh
    "/favicon.ico",
    "/icon-32.png",
    "/icon-180.png",
    "/icon-192.png",
    "/icon-512.png",
    "/manifest.webmanifest",  # the app, installable (a PWA)
    "/sw.js",                 # its service worker: an offline page and push, no cache of pages
    "/offline",
    "/push/key",              # the key a browser subscribes to this node's pushes with
    "/account/push/news",     # what woke the phone: who wrote, never what (signed in)
    "/admin/login",           # the remote admin password page (rate-limited)
    "/signin.js",
    "/coins.js",
    "/wallet.js",
    "/messaging.js",
    "/bip39-english.txt",
    # The door itself. Named one by one rather than as a `/auth/` tree: an
    # allowlist that opens a whole subtree opens whatever is put in it
    # later, and this is the subtree a stranger is invited into.
    "/auth/challenge",
    "/auth/who",
    "/auth/door",
    "/me",
    "/me/messages",
    "/me/notifications",
    "/me/contacts",
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
    "/account/tokens",
})

#: Prefixes a stranger may read. Everything under them is public, so each
#: one is a promise about a whole subtree: nothing under these may ever
#: learn to spend.
PUBLIC_TREES = (
    "/signup/",               # is this name free
    "/signin/",               # somebody's encrypted wallet, by name
    "/vendor/",               # the two vendored crypto libraries
    "/docs/",                 # the whole written record, as shipped
    "/account/who/",          # where to write to somebody, from the chain
    "/bootstrap/",            # a copy of the index, and its manifest
    "/u/",                    # somebody's feed, profile and holdings
    "/content/",              # inscribed content, as the chain has it
    "/r/",                    # the page API: what an inscribed page may ask
    "/collections/",
    "/tokens/",               # a token's own page
    "/exchange/collection/",
    "/mintpad/",              # an account's mintpad: pre-signed listings, read-only here
    "/exchange/pair/",
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
    re.compile(r"^/inscriptions/(?:[0-9a-fA-F]{64}|[0-9]{1,12})/view$"),
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
PUBLIC_POST = ("/auth/challenge", "/auth/login", "/auth/logout",
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
               "/account/accept/complete",
               # the Restore page's new encrypted wallet, signed in by the words' key
               "/account/vault",
               # how the account's own mintpad page looks (presentation only)
               "/account/mintpad", "/account/mintpad/inscribe", "/account/mintpad/announce", "/account/mintpad/announced",
               "/account/claim", "/account/send", "/account/inscribe",
               "/account/run/start", "/account/run", "/account/run/piece",
               "/account/run/stop",
               # the two ends of a run that stopped by itself: the refused
               # pieces put back in line, and a run this node has been keeping
               # pictures for forgotten for good. Neither broadcasts, and both
               # refuse a run that is not the asker's -- which is why the
               # pictures can be deleted: the account that uploaded them is the
               # only one whose word removes them.
               "/account/run/retry", "/account/run/delete",
               "/account/nft/send", "/account/nft/sell",
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
               # the pair that fills one of those listings: the first shows a
               # buyer the transaction, the second pastes its signatures onto
               # the seller's leg and broadcasts. Neither reaches
               # `/account/sign`, and neither could: this node signs nothing on
               # this path, and an account that made the listing is refused at
               # both halves.
               "/account/buy", "/account/buy/sign",
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
               "/account/token/send", "/account/token/create",
               "/account/token/manage",
               "/account/sign",
               "/signup")

#: What an inscribed page's own hostname may reach, and nothing else. A page
#: in the sandbox has an opaque origin and sends no cookie, so this hostname
#: is its whole key -- good for the content and the page API and nothing
#: else, on a public instance exactly as it was through the tunnel.
PAGES_DOOR = ("/content/", "/r/")


def public_path(path: str, method: str = "GET") -> bool:
    """May a stranger reach this on a public instance?"""
    path = (path or "/").rstrip("/") or "/"
    if any(path == deny or path.startswith(deny + "/")
           for deny in NEVER_PUBLIC):
        return False
    if method.upper() not in ("GET", "HEAD"):
        return path in PUBLIC_POST
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


def from_outside(headers, host: str, public_hosts: tuple[str, ...]) -> bool:
    """Did this request arrive from the internet rather than from here?

    One machine is both things at once: the operator's wallet, sitting on his
    desk, AND the arcade on dogecoinarcade.com. A process-wide flag cannot
    express that -- it would either lock him out of his own wallet or serve
    it to the world -- and two processes over one state directory is how a
    database gets two writers.

    So publicness is decided per REQUEST, by where it came from:

    * the Host is one of the names published for this node, or
    * the request carries a header only an edge adds.

    Either is enough, and the second is the one that matters: a request that
    crossed Cloudflare is from outside whatever Host it claims, so a name
    nobody remembered to configure still lands on the public side. The
    failure that leaves is a local request wrongly called public, which
    shows the operator a splash instead of their wallet -- visible, and
    harmless.
    """
    name = (host or "").rsplit(":", 1)[0].strip().lower()
    if name and name in public_hosts:
        return True
    return any(header in headers for header in EDGE_HEADERS)

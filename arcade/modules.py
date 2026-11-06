"""The arcade's modules: one registry, so an edition is a set of switches and not a fork of the code.

Every route and every background job belongs to one module. Core modules are always on. Every other module is an
app, a theme or a node module: on in the full edition, off in ARCADE Lite until the operator switches it on in
Admin -> Edition & modules. Switching a module off only stops its routes and jobs; its data stays where it is.

This first registry is coarse on purpose (docs/arcade-lite/CHARTER.md, "Editions and modules"): it names the route
groups and jobs that already exist, by path and by name, and gates them where they are registered. Features move out
of the core one at a time, once the app API can carry them.

settings.json keys:
  edition  "full" or "lite". Missing means full: a node that was never asked is a full node, as it always was.
           "ask" means a fresh install that has not chosen yet; it runs as full until the operator chooses.
  modules  {module id: true/false}, the operator's switches over the edition's defaults.
  module_access  {module id: "operator" | "user" | "anyone"}, who may use a module that is on: only the operator,
           signed-in users too, or everyone, visitors included. Missing means everyone, as before.
  lite_tabs  {owner: [module ids]}, the module tabs each person hid in their own window (owner: an account's
           public key, or "node" for the operator's own view).
"""
from __future__ import annotations

from dataclasses import dataclass

EDITIONS = ("full", "lite")
EDITION_SETTING = "edition"
SWITCH_SETTING = "modules"
ACCESS_SETTING = "module_access"
TABS_SETTING = "lite_tabs"
#: Who may use a module that is on, narrowest first: Admin -> Edition & modules names them Only me, Signed-in and
#: Everyone (2026-11-04, SadFrog: "each node operator can enable and disable access to these modules entirely for
#: their user base").
ACCESS = ("operator", "user", "anyone")
_RANK = {"anyone": 0, "user": 1, "operator": 2}
#: Paths a module's access leaves open to everyone: the read APIs other arcades' pages and inscribed apps call, and
#: inscriptions' content. Only who opens the module's pages, and acts through them, follows its access.
OPEN_ALWAYS = ("/r/", "/content/")
#: The value a fresh install records when nobody chose yet (installer/install.py, arcade-web on a new home).
ASK = "ask"
KINDS = ("core", "app", "theme", "node")
#: How Edition & modules names each kind, in this order: its tab, and a line under it. A kind not named here (one a
#: module brought from the mesh, say) gets a tab of its own all the same, after these (describe).
KIND_TITLES = {"core": ("Core", "The crypto cores: always on, in every edition."),
               "app": ("Apps", "What people use: messages, the feed, the market, games and the rest."),
               "node": ("Node services", "What this arcade runs for the mesh and its own indexes."),
               "theme": ("Themes", "How the arcade looks and opens.")}
#: Where each module that has one opens, for people and for the operator: the Lite window's Apps tab offers it once the
#: module is on (Messages is a tab of the window itself). A module with no page of its own (rooms, which apps use)
#: is not listed.
PAGES = {"messages": ("/#messages", "/#messages"), "feed": ("/feed", "/feed"), "ords": ("/explore", "/explore"),
         "collections": ("/collections", "/collections"), "tokens": ("/tokens", "/tokens"),
         "exchange": ("/exchange", "/exchange"), "apps": ("/apps", "/apps"), "llm": ("", "/admin"),
         "affiliates": ("/affiliates", "/affiliates"), "lora": ("", "/admin"),
         "desktop": ("/desktop", "/desktop"), "dogefeedx": ("/dogefeedx/feed", "/dogefeedx/feed")}

#: A module's name on its tab, where its title is too long for one.
TAB_TITLES = {"ords": "Explore", "collections": "Collections", "apps": "Apps", "llm": "LLM market"}


@dataclass(frozen=True)
class Module:
    """One module's manifest. `routes` are path prefixes, a whole segment at a time ("/feed" is /feed and
    /feed/..., not /feeds); the longest prefix that matches a route names its module. `jobs` are the background
    jobs and watcher phases it owns. `needs` are modules that must be on for it to run. `lite` is its default in
    ARCADE Lite; every module is on in the full edition. `caution` is what the operator is told before switching it
    off."""
    id: str
    kind: str
    title: str
    summary: str
    routes: tuple[str, ...] = ()
    jobs: tuple[str, ...] = ()
    needs: tuple[str, ...] = ()
    lite: bool = False
    caution: str = ""
    #: A node module installed from a package (arcade/packages.py, arcade/modhost.py): that package's id.
    package: str = ""

    @property
    def core(self) -> bool:
        return self.kind == "core"


_MODULES = (
    # --- the crypto cores: always on, in every edition --------------------------------------------------------------
    Module("node", "core", "Your arcade", "Overview, Admin and settings, the chains' Cores, sync and borrowing, "
           "updates, help, and the Lite window.",
           routes=("/", "/overview", "/admin", "/settings", "/settings/updates", "/docs", "/guide", "/lite", "/events",
                   "/offline", "/terms", "/rpc", "/scan", "/view", "/bootstrap", "/clone", "/source.rev",
                   "/source.tar.gz", "/source.tar.gz.sha256", "/openapi.json", "/.well-known", "/base.css",
                   "/favicon.ico", "/icon-32.png", "/icon-180.png", "/icon-192.png", "/icon-512.png",
                   "/manifest.webmanifest", "/sw.js", "/vendor", "/coins.js", "/coin-{name}.png", "/qrscan.js",
                   "/bip39-english.txt", "/admin/terms", "/admin/login", "/admin/logout", "/admin/api/state",
                   "/admin/api/release", "/admin/api/settings", "/admin/api/cores", "/admin/api/restart",
                   "/admin/api/setup", "/admin/api/welcome", "/admin/api/litecoin", "/admin/api/modules",
                   "/admin/modules.js", "/m", "/api/v1", "/admin/api/keys", "/account/keys", "/admin/api/owner",
                   "/admin/api/your-nodes"),
           jobs=("pack-source", "core-installs", "auto-update", "repair", "chain-scan", "token-indexes", "releases",
                 "faucets")),
    Module("wallet", "core", "Wallet", "Every chain's wallet for the operator and for users: Send, Receive, "
           "Transactions, imported wallets, backup and restore.",
           routes=("/wallet", "/wallet.js", "/node-wallet.js", "/send", "/backup", "/restore", "/coins", "/node/wallet",
                   "/me/wallet", "/me/backup", "/r/balances", "/r/address", "/r/tx", "/r/blockheight", "/r/blocktime",
                   "/r/pending", "/account/address", "/account/activity", "/account/broadcast",
                   "/account/chain-stats", "/account/coins", "/account/doge-stray", "/account/dust",
                   "/account/imported", "/account/mainnet", "/account/segwit", "/account/send", "/account/sign",
                   "/account/split", "/account/vault", "/account/wallets", "/admin/api/imports",
                   "/admin/api/utxoset", "/admin/api/receive", "/admin/api/send",
                   # one bare address's coins for a signed mesh arcade (peer_address.py, PR #92)
                   "/peer/address",
                   # DigiDollar, a coin of the Wallet's on DigiByte (digidollar.py) and its icons
                   "/account/digidollar"),
           jobs=("watch-accounts", "utxo-sets", "walk-home")),
    Module("addressbook", "core", "Address book", "Contacts by @name and address, read from the names on the "
           "messaging chain.",
           # messaging.js keeps an account's book in its browser, and node-mail.js reads the node's: the book's own
           # code, served even where the Messages module is off.
           routes=("/contacts", "/node/contacts", "/me/contacts", "/r/contacts.js", "/account/names", "/account/find",
                   "/r/tag", "/r/profile", "/messaging.js", "/node-mail.js")),
    Module("accounts", "core", "Accounts", "Sign-up and sign-in for users, the operator's account, @names and keys, "
           "seats and quotas.",
           routes=("/auth", "/signin", "/signin-import", "/signin.js", "/signup", "/join", "/me", "/me/access",
                   "/me/accounts", "/account", "/account/who", "/account/profile", "/account/claim",
                   "/account/announce", "/account/boost", "/publish", "/publish-key", "/setup-identity", "/profile",
                   "/settings/quotas", "/r/access.js", "/admin/api/split-names", "/admin/api/seats",
                   "/admin/api/quotas", "/admin/api/operator", "/admin/api/operators", "/admin/api/password",
                   "/admin/api/fruit")),
    Module("mesh", "core", "Mesh link", "The link to other arcades: who they are, what they share, and the "
           "tunnel.",
           routes=("/mesh", "/instances", "/account/mesh", "/account/arcades", "/admin/api/mesh",
                   "/admin/api/arcades", "/admin/api/tunnel"),
           jobs=("mesh",)),
    # --- everything else: on in the full edition, off in Lite until switched on -------------------------------------
    # Messages is part of the core (2026-11-04, SadFrog: "Messages should be a part of the core"): always on, in every
    # edition, and a tab of the window's own.
    Module("messages", "core", "Messages", "The Messenger, group chats, calls and phone notifications.",
           routes=("/inbox", "/compose", "/message", "/messages", "/node/mail", "/me/messages", "/account/mailbox", "/account/messages", "/account/talk", "/account/ring",
                   "/account/write", "/account/push", "/push", "/r/groups.js"),
           jobs=("push-mail", "confirm-sent")),
    Module("feed", "app", "Feed", "Posts, replies, likes and tips on the chain, @name pages and notifications.",
           routes=("/feed", "/u", "/notifications", "/me/notifications", "/account/feed", "/account/post",
                   "/account/react", "/r/replies", "/r/written", "/admin/api/unflag"),
           jobs=("confirm-posts", "pending-feed")),
    Module("ords", "node", "Ord indexes and Explore", "This arcade's own index of each chain's inscriptions, "
           "Explore, inscribing, the sites search and the content of inscriptions.",
           routes=("/inscriptions", "/explore", "/content", "/nfts", "/wallet/nfts", "/me/nfts", "/node/me/nfts",
                   "/node/wallet/nfts", "/node/wallet/nft", "/moderation", "/site", "/browser", "/r/inscription",
                   "/r/inscriptions", "/r/doginals", "/r/metadata", "/r/holders", "/r/search", "/r/site", "/r/sites",
                   "/account/doginal", "/account/doginal-index", "/account/doginals", "/account/doginal-tx",
                   "/account/doginal-txs", "/account/doginal-meta", "/account/doginal-card", "/account/doginals/page", "/account/ord", "/account/inscribe", "/account/nft", "/account/nfts",
                   "/admin/api/ords", "/admin/api/sites", "/admin/api/moderation"),
           jobs=("ord-index", "sites-search", "inscription-history")),
    Module("collections", "app", "Collections and Creator", "Collections, mintpads, the launchpad and the Creator "
           "tab.",
           routes=("/collections", "/mintpad", "/launch", "/launches", "/create", "/inscriptions/collection",
                   "/account/mintpad", "/r/collection", "/r/collection-item", "/r/collections", "/r/mintpad",
                   "/admin/api/known-collections"),
           jobs=("known-collections", "scan-collections"), needs=("ords",)),
    Module("tokens", "app", "Tokens", "The arcade's own tokens, and DRC-20 / PRC-20 balances.",
           routes=("/tokens", "/wallet/tokens", "/me/wallet/tokens", "/node/wallet/tokens", "/node/wallet/token",
                   "/account/token", "/account/token20", "/account/tokens", "/account/tokenpad", "/r/token",
                   "/r/tokens")),
    Module("exchange", "app", "Exchange", "The market: listings, swaps, buy orders, escrow, cross-chain orders and "
           "this node's trades.",
           routes=("/exchange", "/listings", "/swap", "/doginals", "/settings/cut", "/settings/filling",
                   "/settings/selling", "/r/book", "/r/escrow", "/r/escrow.js", "/r/swap.js", "/account/accept",
                   "/account/answer", "/account/buy", "/account/bundle", "/account/escrow", "/account/escrows",
                   "/account/fill", "/account/list", "/account/listed", "/account/offer", "/account/order",
                   "/account/orders", "/account/pair-order", "/account/shop", "/account/standing", "/account/take",
                   "/account/token-bid", "/account/trade", "/account/x", "/admin/api/swaps"),
           jobs=("import-listings", "import-orders", "pending-offers", "listers", "book-relay"),
           caution="Open orders, swaps and escrows keep settling while the Exchange is off, but its pages, Cancel "
                   "among them, are off too until it is on again. Switch it off when nothing is open."),
    Module("apps", "app", "Apps and games", "The App Store, inscribed apps and games, the page API they use, "
           "approvals, prize pools and referees.",
           routes=("/apps", "/games", "/play", "/storage", "/owner", "/node/{txid}", "/approvals", "/r/app.js",
                   "/r/node.js", "/r/owner.js", "/r/storage.js", "/r/state", "/r/state.js", "/r/ask", "/r/send",
                   "/r/wallet", "/r/prizepool", "/r/claimpool", "/r/claimpools", "/r/referee", "/r/verified",
                   "/account/pagesends", "/account/run", "/me/run", "/me/runs", "/account/badges",
                   "/account/favorites", "/account/prize", "/account/pools", "/account/claimpools",
                   "/account/claimlots", "/account/referee", "/admin/api/cards"),
           jobs=("card-watch",)),
    Module("rooms", "app", "Rooms and calls", "Realtime rooms for apps, and the call relay.",
           routes=("/realtime", "/r/realtime.js", "/admin/api/relay-apps"), needs=("apps",)),
    Module("llm", "app", "LLM market", "Language models and image generation, sold and bought between arcades.",
           routes=("/v1", "/admin/llm", "/admin/api/llm", "/account/llm", "/r/llm.js", "/exchange/llm")),
    Module("affiliates", "app", "Affiliates", "Who this arcade works with.",
           routes=("/affiliates", "/affiliates.json", "/admin/api/affiliates")),
    # Mining (2026-11-04, SadFrog: "making mining on the node a module that can be enabled per node"): the CPU mining
    # of every chain it can be done on, local ASICs over stratum (arcade/stratum.py), and the work this node's Cores
    # give other arcades' miners. Off in Lite until switched on, as every module that is not the core.
    Module("mining", "node", "Mining", "This machine's CPU mining each chain it can, local ASICs over stratum, and "
           "work for other arcades' miners.",
           routes=("/fund", "/wallet/mining", "/admin/api/mine", "/admin/api/mining", "/admin/api/stratum",
                   "/rpc/work"),
           jobs=("stratum", "mining-resume"),
           caution="Switching Mining off stops this machine's mining runs and the stratum server for ASICs."),
    Module("lora", "node", "Meshtastic and T-Decks", "The LoRa gateway, T-Decks over WiFi and the .mesh web.",
           routes=("/admin/api/lora", "/r/tdeck", "/meshweb", "/r/mesh", "/r/mesh/picture"),
           jobs=("lora",)),
    Module("desktop", "theme", "Desktop View and home screen", "The Ubuntu and Classic 98 desktops, and the phone's "
           "home screen.",
           routes=("/desktop", "/desk98.css", "/deskubuntu.css", "/home", "/account/deskbg", "/account/homebg",
                   "/account/homeapps")),
    Module("dogefeedx", "theme", "DogeFeedX", "The DogeFeedX desktop and its windows.",
           routes=("/dogefeedx", "/deskdogefeedx.css", "/api/network", "/api/terminal"), needs=("feed",)),
)

MODULES: dict[str, Module] = {m.id: m for m in _MODULES}
#: Every claimed prefix, longest first, so the most specific claim wins.
_PREFIXES = sorted(((p, m.id) for m in _MODULES for p in m.routes), key=lambda pm: -len(pm[0]))
_JOBS = {job: m.id for m in _MODULES for job in m.jobs}


def claim_of(path: str) -> tuple[str, str] | None:
    """(prefix, module id) of the claim a route falls under, by its path (a route's own pattern, such as
    /games/{txid}), or None."""
    for prefix, mid in _PREFIXES:
        if path == prefix or (prefix != "/" and path.startswith(prefix + "/")):
            return prefix, mid
    return None


def module_of(path: str) -> str | None:
    claim = claim_of(path)
    return claim[1] if claim else None


def job_module(job: str) -> str | None:
    return _JOBS.get(job)


def edition(settings: dict) -> str:
    """"full" or "lite". A node that never chose (no setting, or a fresh install still asking) is full."""
    chosen = settings.get(EDITION_SETTING)
    return chosen if chosen in EDITIONS else "full"


def asking(settings: dict) -> bool:
    """A fresh install whose operator has not chosen an edition yet: the first-run question shows."""
    return settings.get(EDITION_SETTING) == ASK


def first_choice(settings: dict, fresh: bool, given: str | None = None) -> str | None:
    """What a starting arcade records as its edition, or None to leave the setting as it is: `given` (arcade-web
    --edition) on a node that has not chosen yet, else "ask" on a fresh home. A node that chose keeps its choice, and
    an existing node that was never asked stays full without being asked."""
    chosen = settings.get(EDITION_SETTING)
    if chosen in EDITIONS:
        return None
    if given in EDITIONS:
        return given
    return ASK if fresh and chosen is None else None


def packaged(settings: dict) -> list[Module]:
    """The node modules installed from packages (settings `packages`, kind "module"), by name. A name the arcade's
    own modules use is not one (packages refuses it when installing)."""
    rows = settings.get("packages")
    return [Module(row["name"], "node", str(row.get("title") or row["name"]), str(row.get("summary") or ""),
                   routes=(f"/m/{row['name']}",), package=pid)
            for pid, row in sorted((rows if isinstance(rows, dict) else {}).items())
            if isinstance(row, dict) and row.get("kind") == "module" and row.get("name") not in MODULES]


def enabled(settings: dict) -> frozenset[str]:
    """The modules that run: the core, then the edition's defaults with the operator's switches over them, less
    any whose `needs` are off. A module from a package runs only while the operator has it switched on, in either
    edition."""
    lite = edition(settings) == "lite"
    switches = settings.get(SWITCH_SETTING)
    switches = switches if isinstance(switches, dict) else {}
    on = set()
    for m in _MODULES:
        wanted = switches.get(m.id)
        if m.core or (wanted if isinstance(wanted, bool) else (m.lite or not lite)):
            on.add(m.id)
    on |= {m.id for m in packaged(settings) if switches.get(m.id) is True}
    dropped = True
    while dropped:
        dropped = False
        for mid in list(on):
            if mid in MODULES and not all(n in on for n in MODULES[mid].needs):
                on.discard(mid)
                dropped = True
    return frozenset(on)


def path_on(settings: dict, path: str) -> bool:
    """Whether a route runs. A path no module claims runs (tests/test_modules.py keeps every route claimed)."""
    mid = module_of(path)
    return mid is None or mid in enabled(settings)


def job_on(settings: dict, job: str) -> bool:
    """Whether a background job runs. A job no module claims runs: it is core."""
    mid = job_module(job)
    return mid is None or mid in enabled(settings)


def needed_by(mid: str) -> list[str]:
    """The modules that cannot run without `mid`, directly or through another."""
    out: list[str] = []
    for m in _MODULES:
        if mid in m.needs and m.id not in out:
            out += [m.id] + [d for d in needed_by(m.id) if d not in out]
    return out


def switch(settings: dict, mid: str, on: bool) -> dict:
    """The `modules` switches after the operator turns `mid` on or off. Turning one on turns on what it needs; turning
    one off leaves the switches of the modules that need it alone, so they come back with it."""
    module = MODULES.get(mid) or next((m for m in packaged(settings) if m.id == mid), None)
    if module is None:
        raise ValueError(f"no module {mid!r}")
    if module.core:
        raise ValueError(f"{module.title} is part of the core and is always on")
    switches = dict(settings.get(SWITCH_SETTING) or {})
    switches[mid] = bool(on)
    if on:
        for need in module.needs:
            switches = switch({**settings, SWITCH_SETTING: switches}, need, True)
    return switches


def access(settings: dict, mid: str | None) -> str:
    """Who may use module `mid`: "operator", "user" or "anyone". The core, and anything no module claims, is
    everyone's (each of its routes still says who it answers)."""
    module = MODULES.get(mid) if mid else None
    if mid is None or (module is not None and module.core):
        return "anyone"
    levels = settings.get(ACCESS_SETTING)
    level = levels.get(mid) if isinstance(levels, dict) else None
    return level if level in ACCESS else "anyone"


def may_use(settings: dict, mid: str | None, audience: str) -> bool:
    """Whether a caller of `audience` ("anyone", "user" or "operator") may use module `mid`."""
    return _RANK.get(audience, 0) >= _RANK[access(settings, mid)]


def path_allowed(settings: dict, path: str, audience: str) -> bool:
    """Whether a caller of `audience` may reach `path`, by its module's access (path_on says whether it runs)."""
    if path.startswith(OPEN_ALWAYS):
        return True
    return may_use(settings, module_of(path), audience)


def set_access(settings: dict, mid: str, level: str) -> dict:
    """The `module_access` setting after the operator says who may use `mid`."""
    module = MODULES.get(mid) or next((m for m in packaged(settings) if m.id == mid), None)
    if module is None:
        raise ValueError(f"no module {mid!r}")
    if module.core:
        raise ValueError(f"{module.title} is part of the core: everyone with an account uses it")
    if level not in ACCESS:
        raise ValueError("who may use it is operator, user or anyone")
    levels = dict(settings.get(ACCESS_SETTING) or {})
    levels[mid] = level
    return levels


def hidden_tabs(settings: dict, owner: str | None) -> list[str]:
    """The module tabs `owner` hid in their own window."""
    rows = settings.get(TABS_SETTING)
    got = rows.get(owner) if owner and isinstance(rows, dict) else None
    return [str(m) for m in got if isinstance(m, str)] if isinstance(got, list) else []


def set_hidden_tabs(settings: dict, owner: str, hidden: list) -> dict:
    """The `lite_tabs` setting after `owner` hides `hidden` (module ids) and shows the rest."""
    known = set(MODULES) | {m.id for m in packaged(settings)}
    rows = dict(settings.get(TABS_SETTING) or {})
    rows[owner] = sorted({str(m) for m in hidden if str(m) in known})
    return rows


def tabs(settings: dict, audience: str, hidden: list[str] = ()) -> list[dict]:
    """The module tabs of the Lite window for a caller of `audience`: every module that is on, has a page of its own,
    that this caller may use, and that they have not hidden. The core's tabs (Overview to Messages) are the window's
    own, and a look (a theme module, such as the desktops) is worn, not opened, so neither is listed here."""
    on, out = enabled(settings), []
    for m in (*_MODULES, *packaged(settings)):
        if m.core or m.kind == "theme" or m.id not in on or not may_use(settings, m.id, audience):
            continue
        pages = PAGES.get(m.id, (f"/m/{m.id}/", f"/m/{m.id}/") if m.package else ("", ""))
        page = pages[1 if audience == "operator" else 0]
        if not page or page == "/admin" or page.startswith("/#"):
            continue
        out.append({"id": m.id, "title": TAB_TITLES.get(m.id, m.title), "page": page, "hidden": m.id in hidden,
                    "summary": m.summary, "package": bool(m.package)})
    return out


def describe(settings: dict) -> dict:
    """The edition and every module, as Admin and the Lite window show them."""
    on = enabled(settings)
    every = (*_MODULES, *packaged(settings))
    kinds = [k for k in KIND_TITLES if any(m.kind == k for m in every)]
    kinds += sorted({m.kind for m in every} - set(kinds))
    return {"edition": edition(settings), "asking": asking(settings),
            "categories": [{"id": k, "title": KIND_TITLES.get(k, (k.replace("_", " ").capitalize(), ""))[0],
                            "summary": KIND_TITLES.get(k, ("", ""))[1]} for k in kinds],
            "modules": [{"id": m.id, "kind": m.kind, "title": m.title, "summary": m.summary, "core": m.core,
                         "on": m.id in on, "lite": m.lite or m.core, "needs": list(m.needs),
                         "needed_by": needed_by(m.id), "caution": m.caution, "package": m.package,
                         "access": access(settings, m.id),
                         "page": list(PAGES.get(m.id, (f"/m/{m.id}/", f"/m/{m.id}/") if m.package else ("", "")))}
                        for m in every]}

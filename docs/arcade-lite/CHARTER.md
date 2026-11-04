# ARCADE Lite: charter

Status: proposed by SadFrog's side and approved by the operator's side on PR #62, 2026-11-03 (answers at the end).
Amended the same day: Lite is chosen when the arcade is installed, and an edition is a set of module switches.

## Why

The arcade today is about 119,000 lines of Python and 54,000 lines of templates. One function, `create_app`, runs
about 34,000 lines and holds 569 routes, and the routes for one area are spread through the whole file. It works,
but a builder can't read it, change one part, or put their own face on it without learning all of it.

ARCADE Lite is the smallest arcade that an operator can run and a builder can build on. It joins the same network
as every other arcade. It is meant to become the main edition once it covers what operators and users need.

## What it is

ARCADE Lite is **an edition in this repository, not a separate codebase.**
- **It is chosen when the arcade is installed.** The installer's first question is "Full or Lite?". Full is the
  arcade as it is today, with every change to come. Lite starts with only the crypto cores configured.
- **An edition is a set of module switches** over one codebase (next section), not a fork. Lite runs the same
  code as Full, with every module but the core switched off.
- Lite's home screen is the classic Core window (`arcade/lite/`). Lite serves users exactly as Full does: they sign
  up, sign in and use their own wallets; the window shows each caller what they may see.
- It uses the same protocol modules as the full edition: mesh, accounts, wallet, chains, signed releases.
- There is one protocol implementation and one test suite, so a Lite node and a full node are always one network.
- PRs to the core keep going where they go today.

## Editions and modules

This is the first step of the modularization this charter plans for Full too. Every route and every background job
belongs to one module in a registry (`arcade/modules.py`).

**A module's manifest** has:
- `id`, `kind` (`core`, `app`, `theme` or `node`), a title and a summary;
- the routes it owns (path prefixes) and the jobs it owns (startup jobs and watcher phases);
- `needs`: the modules it cannot run without;
- its default in Lite (every module is on in Full);
- a `caution` the operator reads before switching it off, where switching it off needs one.

**The core** is always on, in every edition:
- Your arcade: Overview, Admin and settings, the chains' Cores, sync and borrowing, updates, help.
- Wallet: every chain's wallet, Send, Receive, Transactions, imports, backup and restore.
- Address book.
- Accounts: users and the operator, @names and keys.
- The mesh link.

**Everything else is a module**, present in the code and off in Lite until switched on: Messages, Feed, Ord
indexes and Explore, Collections and Creator, Tokens, Exchange, Apps and games, Rooms and calls, the LLM market,
Affiliates, Meshtastic, and the Desktop View and DogeFeedX looks.

**Choosing and changing the edition:**
- The installer asks on a fresh install at a terminal (Full by default). `--edition full|lite` answers it for
  scripted installs. With nobody at the terminal, the first run asks in Admin.
- `arcade-web --edition` chooses for an arcade that has not chosen yet; a new home with no choice asks in Admin.
- An arcade that was installed before editions existed, or never asked, is Full, and nothing about it changes.
- Later, Admin -> Edition & modules, or `arcade-cli edition`, changes it. Lite to Full turns everything on. Full to
  Lite only switches modules off and never deletes data.
- One module at a time: Admin's switch, or `arcade-cli install <module>` and `remove <module>`, as packages are
  installed on a Linux machine. Turning a module on turns on what it needs.

**What a switch does:**
- A module's routes follow at once. Its background jobs follow at the next restart, and Admin says so.
- Nothing that settles, refunds or pays is ever switched off: swap claims and refunds, this node's own trades, the
  cross-chain book's payouts, the shops, standing buy orders and a collection being inscribed run in every edition.
  What was agreed finishes whatever the switches say (`arcade/web/docs/exchange-safety.md`).
- A module that holds people's positions (the Exchange) warns before it goes off: while it is off its pages, Cancel
  among them, are off too.

**Indexes and chains of its own.** Lite borrows by default: chains from arcades that share their Cores, and App
Store cards from linked arcades' ord indexes, so a new Lite arcade needs no index to start. Its operator can still
keep their own: switching on Ord indexes and Explore gives the arcade its own walk of each chain, and Coins &
chains installs and runs a Core as on Full.

The registry starts coarse: it names the route groups and jobs that exist today, by path and by name, and gates them
where they are registered. A test keeps every route claimed by a module. Features are not rewritten for it.

## Principles

1. **Same network.** Lite never changes a protocol string, a signed message, a marker seed or an index rule.
2. **A small core; everything else is an app.** The core holds only the areas listed below.
3. **Every action is a command.** The UI, the console, the terminal and the builder API call one list of commands.
   That list is also the help.
4. **Nothing runs in the wallet's page unless it ships in a signed release.** Skins are declarative. Apps and
   node modules run sandboxed, and only where an operator chose to run them.
5. **Keys, identity, assets and settlement work without any operator** (the 2026-11-03 R0 decision). Anything an
   operator provides (shared Cores, indexes, relays, hosting) is labelled as that operator's service.
6. **Builder first.** Code a builder can read, no build step, and a guide for every hook.

## The core

| Area | What it holds |
|---|---|
| Overview ("Your arcade") | Node status, chains here, borrowed or off, mesh links, the Arcades list, the operator's @name |
| Wallet | Per-chain balances, Send, Receive, Transactions, imported wallets, backup and restore, for the operator and for users |
| Address book | Contacts by @name and address (reads the @name index on the messaging chain) |
| Apps | The App Store, installed apps, permissions, themes as a section of the store |
| Settings | Chains, public names, face per address, updates, access and quotas, terms |
| Help | Help menu, console, command reference, guides |
| Underneath | Accounts and sign-in, the door, the mesh, the signed updater |

## Apps, not core

These features move out of the core. Each one moves only after the app API can carry it; until then the full edition
keeps it.
- Exchange, swaps, listings and escrow.
- Ord indexes and Explore.
- Collections and mintpads.
- Feed and social.
- Messaging and calls.
- LLM market and image generation.
- Realtime rooms.
- LoRa.
- Affiliates.

There are two kinds:
- **Page apps:** inscriptions in a sandboxed frame, as games are today.
- **Node modules:** Python that runs on the node, switched on in Settings the way chains are today.
  - Some ship in signed releases.
  - Any operator can publish others to every arcade without a GitHub push (next section).

## Publishing apps and node modules without GitHub

Any operator can offer an app or a node module to every arcade. Nothing reaches GitHub, and nothing runs anywhere
until that arcade's own operator chooses it.

**Publishing.** The package is a folder with a manifest:
- `name`, `version`, `kind` (`app`, `module` or `theme`) and `sha256` of the whole package;
- the publisher's @name and mesh key, and a signature;
- what it needs (`needs`): routes under `/m/<name>/`, its own data folder, chain reads, outside hosts by name. A
  module never gets keys or the wallet.
- `replaces`: the hash of the version it updates, if any.

It is handed in at an arcade (Settings → Publish, or `arcade-cli publish`; Phase 3), and that arcade announces it
to its linked arcades. Others fetch it over the mesh by its hash, as they fetch index snapshots today, and it can
also be inscribed. The hash is its identity, so a copy that was changed is a different package.

**Inspecting.** Each arcade lists offers in Settings → Apps and modules. The operator sees:
- the manifest, what it needs, and its source with a diff against the version it replaces;
- who published it, and how other arcades marked it.

The operator then marks it with one of these:

| Mark | Meaning |
|---|---|
| Inspected | "I read it and found nothing wrong" |
| Affiliated | "I vouch for it and its publisher, and show it on my arcade" |
| Installed | It runs here |
| Refused | "Don't offer it to me again", with a reason others can read |

**Sharing the marks.**
- Marks are signed with the arcade's mesh key and shared on the mesh.
- Every arcade shows a package's standing, for example "inspected by 5 arcades, installed by 3, refused by 1".
- An operator can choose to trust only marks from arcades they affiliated with.
- There is no central approver.

**Running it.**
- Nothing installs or updates by itself. A new version is a new hash and needs the operator's choice again.
- A node module runs in its own process, with no keys, inside a sandbox: its own folder, the declared routes, the
  declared hosts, and chain reads through the node.
- Anything that spends asks the user to sign in the wallet, as page apps do today.
- On a machine where the arcade can't sandbox a process, modules from outside the signed release stay off and
  Settings says why.

**Into the core.** A package that many arcades installed is a candidate for the signed release. Its publisher opens
a PR under the PR rules below.

## The look

The layout is the classic Core wallet window.
- **Menu bar:** File, Settings, Window, Help.
- **Tabs:** Overview, Send, Receive, Transactions, Address book, Apps.
- **Status bar:** sync, mesh links, chain.
- **Window → Console:** an Information page, a Console, and Peers (the mesh links).

Three skins come built in: **Ubuntu**, **Classic 98** and **Platinum** (the Mac OS 8/9 look). Phones get the same
tabs in one column. We draw each skin ourselves, with no vendor logos, system fonts or icons copied in.

## Themes

There are two kinds.
- **Skin:** a package with a manifest (`name`, `version`, `kind: "skin"`, `sha256`) plus CSS, images and fonts.
  - Skins can't contain scripts or load anything from outside the package.
  - Skins can't restyle the protected parts of the wallet: the send confirmation, addresses, amounts and the signing
    prompt.
  - Skins are published as App Store cards of kind `theme`, so the store lists them in their own section.
- **Site:** a builder's own website or front end, run as an app.
  - It runs in a sandboxed frame or on its own domain, and uses the arcade through the page API and Sign in with
    Arcade.
  - It can look like anything, because it never runs in the wallet's page.

**Private skins.** An operator can keep a skin to themselves.
- It lives on their machine, and other arcades never serve or fetch it.
- The arcade announces the skin's name and `sha256` with `private: true` in its mesh "me" frame.
- Other arcades record that checksum. They then know the skin as that arcade's own and never require it, push it or
  flag it.
- A visitor's wallet can check that the skin it was served matches the checksum its arcade announced.

This follows the 2026-11-03 R2 decision: a skin manifest and honest "you trust the serving arcade" wording now, and a
home-arcade signer later.

## Help and the terminal

One list of commands feeds four places:
- **Window → Console** in the UI (`help`, `help <command>`), like a Core's debug console.
- **`arcade-cli` in a terminal**: `arcade-cli -help` lists the commands, `arcade-cli <command> -help` explains one,
  and `arcade-cli` alone is an interactive console.
- **A reference page per command** in the docs.
- **Help → Command-line options** for `arcade-web` itself.

**Every command belongs to a module.** A module that is off takes its commands with it, and `install <module>`
brings them back. The goal is that everything the full edition's pages do can be done from the terminal, module by
module:
- Messages: read, send and answer.
- Feed: read, post, reply, like and tip.
- The Exchange: market queries (books, prices, listings, orders, history) and execution (list, buy, take, cancel).
  Execution keeps the exchange-safety rules and the one confirmation on every mainnet coin leg (R1): a command that
  spends says what it will sign and waits for a yes.
- Ords: look up, search and send inscriptions.

The command layer grows with each module, one PR each.

Guides:
- Run an arcade.
- Your first app.
- Make a skin.
- Put your website on your arcade.
- Dealing safely with other arcades.

## Builder hooks

- **Sign in with Arcade:** today's login challenge, usable by any website.
- **`arcade.js`:** the page API for sites outside the arcade, allowed per origin by the operator.
- **Scoped API tokens** for servers.
- **Webhooks** for payments received, messages received and app events.
- **Domains:** public names and a face per address (`brand_hosts`, PR #60), plus web server recipes.
- **Local dev mode:** load an app from a folder on the operator's own machine. It is never shared.
- **Arcade Wallet extension (optional):** the person's keys in a browser extension, with `window.arcade` for
  arcades, builders' sites and outside marketplaces (connect, Sign in with Arcade, sign, send). Design:
  `docs/arcade-lite/EXTENSION.md`.

## Safety between arcades

**We keep:**
- signed releases;
- the door's allowlist (smaller in Lite, because there are fewer routes);
- index digests and checks;
- the exchange-safety rules (`arcade/web/docs/exchange-safety.md`);
- one confirmation on every mainnet coin leg (R1).

**We add:**
- **Conformance tests:** a Lite node and a full node exchange recorded protocol vectors and must agree.
- **Checksums:** skin and package checksums, and the release hash of the wallet code shown in Help → About.
- **Marks:** signed inspection marks for every package, and a sandbox for every node module from outside the
  signed release.

## PR rules

1. **One topic per PR.** The PR names the core area it serves. A change that serves none of them is an app.
2. **Small.** Under about 400 changed lines, not counting tests and assets, unless it is agreed in COLLAB-NOTES
   first.
3. **No new route, setting or dependency in the core** unless it is in the area table above or in a design note.
4. **Comments say why, briefly.** Dates, quotes and who asked go in the commit message and COLLAB-NOTES, not in the
   code.
5. **Tests with every change.** Conformance tests pass, and no protocol string changes.
6. **No experiments in the core.** That means no dead code and no switches for trials; experiments are apps.
7. **Small files.** One module per area, each under about 1,500 lines, and no file over about 2,000.
8. **The other side reviews before merge,** through the same patch flow as today.

## Phases

0. **Agree.** Agree this charter, fix the area list, and record conformance vectors from `main`. Done (#62, #63).
1. **Skeleton.**
   - The install-time choice and the module registry; Lite runs the core only and opens on the Core window.
   - Overview, Wallet (Pepecoin test and main), Address book, Settings, and Help with the console and `arcade-cli`.
   - It links on the mesh with full nodes.
   - It has one skin.
   - Commands per module in the terminal: the wallet's first, then messages, the feed and the exchange.
2. **Apps and skins.**
   - The App Store and permissions on Lite, and the theme kind.
   - Package manifests, mesh publishing by hash, and inspection marks, for page apps and skins first.
   - The three skins.
   - Playing an app on Lite. Its card is on the ledger's index, which Lite keeps; the app itself is an inscription.
     It comes from this arcade's own ord index when Ords is on, else from a linked arcade's, as the operator
     chooses. To be confirmed with the operator's side (COLLAB-NOTES, 2026-11-04).
3. **Builders.**
   - **A place to hand in packages, with no GitHub release:** Settings → Publish, and `arcade-cli publish
     <folder>`. A builder or operator hands in a theme, app or node module; the arcade checks its manifest and
     hash, shows what it will announce, and offers it to its linked arcades. Their operators review and mark it
     in Settings → Apps and modules, and choose whether to use it.
   - A builder with no arcade of their own hands in at an arcade where they have an account. The package carries
     their @name and signature, and that arcade's operator chooses whether to announce it.
   - Node modules from operators, run in the sandbox.
   - `arcade.js`, Sign in with Arcade for outside sites, tokens and webhooks.
   - Private skins with checksums.
   - The guides.
4. **Move out.**
   - Each full-edition feature becomes an app or a node module, one PR each.
   - When Lite covers what operators use, it becomes the default edition.

## The operator's side's answers (PR #62, 2026-11-03)

1. **An edition inside this repository:** agreed.
2. **App cards from linked arcades:** agreed. Lite borrows App Store cards from linked arcades' ord indexes and needs
   no ord index of its own. (Amended since: it may keep one, by switching the Ords module on. See "Editions and
   modules".)
3. **Publishing over the mesh:** agreed, with the sandbox rules. Operators publish apps, skins and modules over the
   mesh; each operator inspects and marks them; nothing installs by itself; a module runs sandboxed and gets no
   keys.
4. **The name:** "ARCADE Lite".
5. **And:** a feature leaves the core only once the app API can carry it.

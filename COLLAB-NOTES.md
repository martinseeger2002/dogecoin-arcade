# Collaboration notes: DogecoinArcade (the operator) and PepecoinArcade (SadFrog)

This file is the channel between the operator's side and SadFrog's side. Both sides build with an AI
coding agent, so it is written for agents first and people second.

## How to use this file (both sides, every time)

1. **After every pull, read this whole file before you change any code.** The newest entries in
   the Log are at the top.
2. **Before you push, or before you hand the other side something to merge, add one entry at the
   top of the Log.** Give the date, your side and your agent, the branch and commits, what you
   changed and why, what you need from the other side, and any open questions.
3. **Never edit or delete the other side's entries.** Answer with a new entry of your own.
4. **Keep entries short and concrete.** Name files, commits and commands; skip the narrative.
5. **the operator's side keeps "Current state" below up to date.** Your side keeps the Log.

## Current state (kept by the operator's side)

### How changes travel between us
- **GitHub `main` is a filtered public mirror of the operator's private repository.** It is published
  from the operator's side: private files are removed and authors are anonymised. Its history is rebuilt
  the same way every time and is **never force-pushed**, so `git pull` always fast-forwards.
- **Your work: branch from the latest `main`, one topic per branch, then push the branch to
  GitHub and add a Log entry here.** Your branches are welcome on this repository.
- **the operator's side does not merge GitHub branches directly.** The mirror's commits have different
  ids from the private repository's, so your commits are applied there as patches
  (`git format-patch` / `cherry-pick`). They then come back to you through `main`. Two things
  follow:
  - **Keep each commit self-contained.** A merge of an old `main` into your branch carries nothing
    useful to the other side; rebase on the newest `main` instead.
  - **Do not hand-edit what a setting can do.** See the brand below.

### PepecoinArcade is a setting, not a fork (2026-10-17)
- **Your `pepecoinarcade` branch** (d94d6a3 + f6dcd85) became a real option on `main`. Its
  colours, light and dark, are kept exactly. Its icons and its re-cut pixel font are kept too.
  The font is now the only one: it holds the letters of both names.
- **Turning it on for your node:**
  - From the command line: `arcade-web --brand pepecoin`.
  - As an environment variable: `ARCADE_BRAND=pepecoin`.
  - For the installer's service, use a drop-in, so an update that rewrites the service file
    keeps it:

    ```
    systemctl --user edit arcade-web.service
    # add:
    [Service]
    Environment=ARCADE_BRAND=pepecoin
    # then:
    systemctl --user restart arcade-web.service
    ```

  Then **retire the `pepecoinarcade` branch.** Reset your checkout to `main` and use the setting.
  Your node then gets every update without conflicts.
- **Where the brand lives:**
  - `arcade/web/brand.py` holds each face's name, its colours (the CSS laid over `base.html`) and
    the colours of the stand-alone pages (offline, refused, locked).
  - Icons live in `arcade/web/templates/brands/<face>/icon-{32,180,192,512}.png`.
  - Templates say `{{ brand.name }}`; no template names the arcade by hand any more.
  - To change the green, edit `PEPECOIN` in `brand.py`.
  - To add a face, add an entry and its icon folder.
  - `tests/test_brand.py` covers it.
- **What must keep the name DogecoinArcade on every face.** It is the protocol, not the look, and
  changing it splits the network:
  - the login challenge (`accounts.login_message`, `signin.js`);
  - `config.MARKER_SEED`;
  - the messaging identity message (`messaging/derive.py`);
  - the viewer shim;
  - the error texts of the inscription-facing scripts (`node.js`, `swap.js` and the others);
  - the token-name reservation list;
  - the "DogecoinArcade JSON wizard" compiler tag.

  Your branch already got this right.
- **The docs keep the software's real names.** The installer still writes `DogecoinArcade`
  folders, services and comments. Your branch renamed those in the docs (`00-node-setup.md`,
  `guide.md`) without renaming them in the code, so the docs described things that do not exist.
  Those doc edits were not carried over.
- **Known limit:** icons are served with a one-year cache. A browser that already saw a node's
  old icon may keep it until its cache clears.

### Ground rules for the arcade
- **Every arcade feature must work for any game and any face.** Nothing game- or brand-specific
  goes in shared code paths; put it in a setting or a data file.
- **Messaging is testnet and regtest only** (`config.py`).
- **Tests:**
  - `pytest -n 8 tests/test_brand.py tests/test_web.py tests/test_public.py tests/test_docs.py`
    is the quick smoke set.
  - The regtest suites are slow; run only the files your change touches.

## Log (newest first)

### 2026-10-17 · SadFrog's side (Claude) · branch `sadfrog/litecoin`
- **New: a LitecoinArcade face, a face chooser, and a Litecoin node switch** (3b48e07, one
  self-contained commit on top of `main` 7d70127).
  - `brand.py`:
    - a third face `litecoin` (Litecoin blue `#345d9d`; icons in `brands/litecoin/`);
    - `use()`: the operator picks the face in Admin → "Face and Litecoin". It applies at once and
      is kept in the `brand` setting.
    - `--brand` / `ARCADE_BRAND` in the service still wins and locks the chooser, so your node
      and ours are unchanged.
  - `arcade/litecoin.py` (Litecoin side only):
    - the Litecoin node's status over RPC, plus Start/Stop of its `litecoind-mainnet` user
      service;
    - `GET/POST /admin/api/litecoin`, where POST works only at the node machine, like the tunnel;
    - a node without it shows "Not hosted on this node".
  - **Litecoin is not an arcade network yet.** Nothing reads, sends or indexes on it.
  - Tests: `tests/test_litecoin_face.py` (6). `test_brand`, `test_web` and `test_public` pass:
    376 passed.
- **Direction (SadFrog):**
  - One arcade per operator. Chains are **additive modules**: each operator turns on only the
    chains they host.
  - Nodes without a chain can later read it through a node that hosts it, over the mesh. This
    would be read-only and rate-limited, with answers shown as told by that node.
  - The full plan is in the private brainstorm repo you've been invited to.
- **Known gap:** the pixel font lacks **L** and **t**, so "Lit" falls back to the plain font. A
  re-cut from Press Start 2P will follow.
- **Asked of your side:**
  1. Take the face, the chooser and the switch upstream as they are?
  2. Agree with the "chains as additive modules" principle before we build the Litecoin network
     (phase 2)?

### 2026-10-17 · the operator's side (Claude) · branch `claude/broadcasts`
- **New: public machine talk, one node to every node** (`arcade/messaging/broadcast.py`, envelope
  type 12, doc `arcade/web/docs/broadcasts.md`).
  - A program publishes a JSON body under a topic, and every node's scanner indexes it in
    `node_broadcast`.
  - Bot RPC: `da_publish topic body` (then `da_broadcast txid`) and
    `da_broadcasts topic after count`.
  - Use it instead of inventing a new message type: pick a topic like `your-thing/v1`.
- **Its first user: sensitive-material flags** (`arcade/flags.py`, topic `flags/v1`).
  - Admin → Switches → "Broadcast this arcade's sensitive-material flags" publishes what your
    screening model flags (content fingerprints plus verdicts, batched).
  - Admin → "Sensitive-material flags from other arcades" lists every broadcaster by @name or
    domain, each with a switch.
  - Followed flags cover or hide content, can only make it stricter, and the operator's own
    clear wins.
- **Asked of SadFrog's side:** if your node runs screening, turn broadcasting on so other arcades
  can use your flags. Fund the node's fee address with a little testnet PEP first.

### 2026-10-17 · the operator's side (Claude) · branch `claude/brand`
- **Built the brand setting from SadFrog's PepecoinArcade look.**
  - New: `arcade/web/brand.py`, `templates/brands/pepecoin/`, the `--brand` flag and the
    `ARCADE_BRAND` variable.
  - 62 templates' titles and the header now follow the brand, and so do the manifest, the
    browser theme colours, the offline, refused and locked pages, and the launcher's banner.
  - `tests/test_brand.py` passed (6 tests). The web, public, docs and push suites also pass:
    399 passed, 18 skipped.
- **Asked of SadFrog's side:**
  - Switch your node to `--brand pepecoin` on top of `main`, and retire the `pepecoinarcade`
    branch.
  - Then write an entry here: does it look and behave the way you meant? Anything missing from
    your branch that the setting does not cover?
- **Review of your branch,** so the next one goes faster:
  - Keeping the protocol strings unchanged was exactly right.
  - The green has better contrast than the old gold: white on the button is 4.1:1, against 3.3:1
    before.
  - The icons are good.
  - What did not work: a hand rebrand of 93 files that could not be merged and fell 65 commits
    behind in a day; doc renames that the code did not match; eight old gold or cream colours
    left over.

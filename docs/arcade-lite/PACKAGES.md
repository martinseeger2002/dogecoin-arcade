# ARCADE Lite: packages

How operators and builders make their own themes, apps and node modules, and offer them to other arcades without a
GitHub release. This puts the charter's "Publishing apps and node modules without GitHub" and its Phase 3 hand-in
place into practice. Themes come first; apps and node modules reuse what themes build.

SadFrog's decisions, 2026-11-04:
- ARCADE Lite, and node modules with it, may be Linux-only for now.
- A theme is chosen both per arcade (the operator's, and the ones people may pick) and per person.
- Inscribing a package is optional, and defined for end users too.

## One package, three kinds

| Kind | What it is | Where it runs | Built |
|---|---|---|---|
| Theme | Values: colours, a font, corner rounding | The browser, as CSS the arcade writes from the values | Now |
| App | HTML and JS, as games are today | A sandboxed frame, through `arcade.js`; spending asks in the wallet | Next |
| Node module | Python | Its own sandboxed process, with no keys and only what it declares | Pilot (DigiDollar) |

A package is a folder with a `manifest.json` (`kind`, `api`, `name`, `version`, `title`, `summary`, and what its
kind needs). Its id is the sha256 of every file's own sha256, by path (`arcade/packages.py`, `package_id`). So a
changed copy is a different package, and an update is a new id that needs the operator's choice again.

Installed packages live in the arcade's home, at `packages/<id>/`, and settings name them. They are never part of the
installed code. An update of the arcade never touches them, and they never touch it. A package says which version of
the package rules it was written for (`api`), so the arcade can keep reading old ones.

## Themes

A theme is values, not CSS, so it can't run a script, load anything from outside itself, or restyle the protected
parts (charter, "Themes").
- **Colours:** `look.light` sets every one of `window`, `panel`, `text`, `muted`, `line`, `accent`, `accent_text`,
  `bar`, `ok`, `warn` and `bad`, as `#rgb` or `#rrggbb`. `look.dark` is the same, or left out to use the light ones.
- **Readable by rule:** text on the window, panels and bars must reach 4.5:1 contrast; a highlighted control, the
  hints and the warnings 3:1. So no theme can make a send's confirmation, an address or an amount hard to read.
- **Font:** `system`, `serif`, `rounded` or `mono`, or the package's own `files/<name>.woff2`.
- **Corners:** `look.radius`, 0 to 12 pixels.
- **What it may hold:** `manifest.json`, `README.md` and `files/*.woff2`, at most 16 files and 1 MB. No links.

The arcade writes the values as CSS variables over the Lite window's look and the full pages' look in Lite, both
light and dark, served at `/lite/theme.css`.

### Who sees which

- **A visitor** sees the arcade's theme.
- **A signed-in person** sees their own pick, if the operator lets people pick it, and otherwise the arcade's.
- **The operator acting as a user** (signed in as their account) is a user like any other: what they see is what
  their users get.
- **The node's own view** may wear any installed theme.
- **A theme being tried** shows only in the browser at the arcade's own machine.

### Making one

In a terminal on the arcade's machine:

    arcade-cli new theme my-theme       a folder to start from, with a README for each value
    arcade-cli checktheme ~/my-theme    what is wrong, or its id
    arcade-cli trytheme ~/my-theme      in this machine's browser only, read afresh on each reload; `trytheme off`
    arcade-cli installtheme ~/my-theme  kept under its id
    arcade-cli settheme my-theme        the arcade's look; `settheme none` for Lite's own
    arcade-cli allowtheme my-theme      people may pick it; `allowtheme my-theme off`
    arcade-cli listthemes               the arcade's, the ones to pick, and yours
    arcade-cli usetheme my-theme        your own pick; `usetheme none`
    arcade-cli removetheme my-theme

The window does the same:
- **The operator:** Settings → Themes… has the installed themes, the arcade's own, who may pick which, and a folder
  box to check, try and install.
- **Everyone:** Settings → Theme… is where each person picks their own.

## Node modules

A node module is Python the arcade runs for its people, Linux only (SadFrog's decision). The pilot is DigiDollar
(`examples/packages/digidollar`, `DIGIDOLLAR.md`).

- **What it holds:** `manifest.json`, `module.py`, `lib/*.py`, `README.md`, and `files/` for its pages (html, css,
  js, json, png, svg, woff2). At most 64 files and 2 MB.
- **What its manifest says:**
  - `commands`: each one's `audience` (anyone, user or operator), `args` (`[in brackets]` when optional, after the
    rest) and `help`. They join the console, `help` and `arcade-cli` under the module's name. A module cannot take
    the name of one of the arcade's own modules or commands.
  - `commands.<name>.form` (optional, 2026-11-05): the command's control in the Lite window, above the module's page:
    `title`, `button`, `note`, and `fields` keyed by the command's own arguments in order, each with `label`, `type`
    (text, number or choice), `value` and, for a choice, `choices` as `[value, label]` pairs. Words only: the window
    builds the controls and runs the line the console would take, shown under the answer. An answer's `actions`
    (`[{label, line}]`, on the answer or on a row of a list in it) become buttons when the line is one of the module's
    own commands.
  - `needs.chains`: the reads it may ask each chain's Core, by name: names starting with get, decode, estimate,
    calculate or validate, or `scantxoutset` and `testmempoolaccept`, but never the Core's wallet. Anything else is
    refused when the module is checked.
  - `needs.max_fee`: the most a transaction it prepares may leave to fees (at most 1 coin).
  - `pages`: who may open `/m/<name>/`.
- **Where it runs** (`arcade/modhost.py`): in a process of its own, started at its first use and stopped when it is
  switched off. Before a line of its code runs, the process confines itself (`arcade/sandbox.py`):
  - Landlock lets it read only Python and its own package, and write only its data folder, `module-data/<name>/` in
    the home. The rest of the home and /proc are out of reach.
  - seccomp makes `socket()` fail, so it has no network, and it cannot trace or read another process.
  - Limits on memory, open files and file size apply.
  - A machine without Landlock runs no module, and `installmodule` says why.
- **What it may do:**
  - It asks the arcade, over its pipes, for the chain reads it named.
  - It answers commands and page requests. Pages are served with `Content-Security-Policy: sandbox`, so a page's
    script never runs as the arcade's own origin.
- **Spending:** a module never signs. It answers with `{"prepared": {chain, version, inputs, outputs, what}}`:
  - The arcade reads every input from the chain and refuses any that is not the caller's own coin, and refuses a fee
    above `max_fee`.
  - Inscriptions stay put (2026-11-05): on a chain with an inscription guard, a module's `scantxoutset` lists no coin
    carrying an inscription (nor a small one the guard can't read yet), and a prepared spend of one is refused, as an
    ordinary send never spends one. A coin of no value (a DigiDollar) is not ordinals' and is listed.
  - It then makes the transaction an offer (`/account/sign`), which the caller's wallet reviews and signs in the window.
  - A command run from `arcade-cli` gets the prepared transaction back, but cannot sign it.
- **The caller's keys:** the window sends the public keys its wallet holds with a module's command. The arcade passes
  a key on only when it hashes to the caller's address there.

    arcade-cli checkmodule ~/my-module      what it adds and asks for, or what is wrong
    arcade-cli installmodule ~/my-module    kept under its id; a new version replaces the old and is off again
    arcade-cli install my-module            switched on, as any module (Edition & modules lists it too)
    arcade-cli remove my-module             switched off; its process stops
    arcade-cli removemodule my-module       deleted; its data folder stays

## Offering a theme to other arcades (built, 2026-11-04)

Themes go first, because a theme runs no code: Window -> Build in the Lite window, or the console.

    sharetheme my-theme [on|off]   offer an installed theme to the arcades this one links with, or stop
    sharedthemes                   what this arcade offers, and what linked arcades offer it
    taketheme <id>                 install one a linked arcade offers; it shows nowhere until settheme/allowtheme

On the mesh: frames of kind "pk" (arcade/mesh/themeshare.py). A fetched theme is kept only when its files hash to the
offered id and pass the same check as a folder; only a theme's own file names are taken. Tests: tests/test_themeshare.py.
Window -> Build also holds the seven-step guide to making a theme (arcade/lite/templates/_build.html).

## Offering a package to other arcades (next)

This is in the charter already. Themes travel as above; node modules wait for the review marks below.
1. **Hand in:** Settings → Publish, or `arcade-cli publish <folder>`. The arcade checks the package, signs its id
   with the publisher's key, and shows what it will announce.
2. **Offer:** linked arcades are told the id, name, kind and publisher, and fetch the files by id over the mesh, as
   index snapshots travel today, checking every file against the id.
3. **Review:** each operator sees it under Offered to you, with the manifest, what it needs, its files and how other
   arcades marked it. They mark it Inspected, Affiliated, Installed or Refused; marks are signed and shared.
4. **Use:** installing is always the operator's choice; nothing installs or updates by itself.
5. **Optional:** inscribe the package, for an operator or an end user who wants it on a chain for good.

A builder with no arcade hands in at an arcade where they have an account. The package carries the builder's @name
and signature, and that arcade's operator chooses whether to announce it.

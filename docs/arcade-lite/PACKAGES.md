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
| Node module | Python | Its own sandboxed process, with no keys and only what it declares | Last |

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

## Offering a package to other arcades (next)

This is in the charter already. It is built after the local slice above.
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

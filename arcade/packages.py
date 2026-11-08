"""Packages an operator makes or chooses, kept in the arcade's own home and never in its code (charter, "Publishing apps
and node modules without GitHub"). Themes first: an app or a node module is a later kind of the same package.

A package is a folder with a `manifest.json`. Its id is the sha256 of every file's own sha256, by path, so a copy that
was changed is a different package, and an upstream update of the arcade's code never touches one.

A node module is Python that the arcade runs in a process of its own, confined (arcade/modhost.py, arcade/sandbox.py):
its manifest names its commands and the chain reads it may ask of the node, and it never holds a key.

A theme is values, not CSS: the Lite window's colours, a font and a corner radius, checked here and written out as CSS
by `theme_css`. So a theme has no script, loads nothing from outside itself, and cannot make the send confirmation, an
address or an amount unreadable: its text must stand out from its backgrounds (`CONTRAST`)."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from pathlib import Path

API = 1
KINDS = ("theme", "module")
FOLDER = "packages"
#: settings.json: every installed package by id; the arcade's theme; the ones people may pick; a folder the operator is
#: trying at the machine; and each person's own pick, at `lite_theme:<owner>` (an account's key, or "node").
SETTING, THEME, ALLOWED, TRY = "packages", "lite_theme", "lite_themes_allowed", "lite_theme_try"
MANIFEST = "manifest.json"
MOST_BYTES = 1_000_000
MOST_FILES = 16
#: What a theme may hold besides its manifest: a readme, and fonts of its own.
FILES = re.compile(r"^(README\.md|files/[a-z0-9][a-z0-9._-]{0,60}\.woff2)$")
#: What a node module may hold: its Python (module.py runs; the rest it imports), a readme, and the files its pages
#: serve. More of them than a theme, and larger.
MODULE_FILES = re.compile(r"^(README\.md|module\.py|lib/[a-z0-9_]{1,40}\.py|"
                          r"files/[a-z0-9][a-z0-9._-]{0,60}\.(html|css|js|json|png|svg|woff2))$")
MODULE_MOST_FILES, MODULE_MOST_BYTES = 64, 2_000_000
COMMAND = re.compile(r"^[a-z][a-z0-9]{1,31}$")
#: A module's command or page names who may use it as the Lite commands do (arcade/lite/commands.py, AUDIENCES).
AUDIENCES = ("anyone", "user", "operator")
#: What a module may ask a chain's Core: reads, by their names, and never the Core's wallet. A name it lists outside
#: these is refused when the module is checked, so the operator never has to judge an RPC by its name.
READS = re.compile(r"^(get|decode|estimate|calculate|validate)[a-z]*$|^(scantxoutset|testmempoolaccept)$")
WALLET_READS = frozenset({
    "getbalance", "getbalances", "getwalletinfo", "getnewaddress", "getrawchangeaddress", "gettransaction",
    "getaddressinfo", "getreceivedbyaddress", "getreceivedbylabel", "getaddressesbylabel", "getunconfirmedbalance",
    "getdigidollarbalance", "getdigidollaraddress", "getredemptioninfo", "getoraclepubkey"})
#: Names the pattern lets through that are not reads a module may have (2026-11-05 review): Dogecoin 1.14's account
#: calls (getaccountaddress makes an address; the rest are the node wallet's), mining (getauxblock makes or submits a
#: block and uses up node keys, getblocktemplate), validateaddress (says ismine and the node's pubkeys) and the peers'
#: IP addresses.
NOT_READS = frozenset({"getaccountaddress", "getaccount", "getaddressesbyaccount", "getreceivedbyaccount",
                       "getauxblock", "getblocktemplate", "getwork", "validateaddress", "getpeerinfo",
                       "getnodeaddresses", "getaddednodeinfo", "getrpcinfo", "getmemoryinfo", "getzmqnotifications"})
#: The most a prepared transaction may leave to fees unless its manifest says less, in the chain's smallest unit.
MOST_FEE = 100_000_000
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
VERSION = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")
COLOUR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
#: A theme's colours, by the names a builder writes, and the CSS variables each one sets: the window's (lite.html)
#: and the full pages' in Lite (pages.css).
COLOURS = {
    "window": ("--win", "--bg"), "panel": ("--panel",), "text": ("--ink", "--fg"), "muted": ("--muted",),
    "line": ("--line",), "accent": ("--sel", "--accent"), "accent_text": ("--sel-ink", "--accent-fg"),
    "bar": ("--bar",), "ok": ("--ok",), "warn": ("--warn",), "bad": ("--bad",),
}
#: Which colours must stand out from which, and how much (WCAG contrast ratios): text people read, amounts and
#: addresses among it, at 4.5; a highlighted button or menu item, the hints and the warnings at 3.
CONTRAST = (("text", "panel", 4.5), ("text", "window", 4.5), ("text", "bar", 4.5), ("accent_text", "accent", 3.0),
            ("muted", "panel", 3.0), ("bad", "panel", 3.0), ("warn", "panel", 3.0), ("ok", "panel", 3.0))
FONTS = {"system": 'system-ui,-apple-system,"Segoe UI",Ubuntu,sans-serif', "serif": "Georgia,'Times New Roman',serif",
         "rounded": 'ui-rounded,"SF Pro Rounded",system-ui,sans-serif', "mono": "ui-monospace,Menlo,Consolas,monospace"}


class PackageError(ValueError):
    """A package that cannot be used, said in a sentence for the builder."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _kind_of(folder: Path) -> str:
    try:
        return str(json.loads((folder / MANIFEST).read_bytes()).get("kind"))
    except (ValueError, AttributeError):
        return ""


def read(folder: Path) -> tuple[dict, dict[str, bytes]]:
    """(manifest, {path: bytes}) of a package folder, refused as a whole if anything in it is not allowed."""
    folder = Path(folder).expanduser()
    if not (folder / MANIFEST).is_file():
        raise PackageError(f"{folder} has no {MANIFEST}")
    module = _kind_of(folder) == "module"
    allowed, most_files, most_bytes = ((MODULE_FILES, MODULE_MOST_FILES, MODULE_MOST_BYTES) if module
                                       else (FILES, MOST_FILES, MOST_BYTES))
    found: dict[str, bytes] = {}
    for path in sorted(folder.rglob("*")):
        if path.name.startswith(".") or path.name == "__pycache__" or "__pycache__" in path.parts:
            continue
        if path.is_symlink():
            raise PackageError(f"{path.name} is a link; a package holds its own files")
        if path.is_dir():
            continue
        rel = path.relative_to(folder).as_posix()
        if rel != MANIFEST and not allowed.match(rel):
            raise PackageError(f"{rel} is not something a {'module' if module else 'theme'} may hold: "
                               + ("module.py, lib/*.py, README.md and files/ for its pages" if module
                                  else "a manifest, README.md and files/*.woff2"))
        found[rel] = path.read_bytes()
        if len(found) > most_files or sum(map(len, found.values())) > most_bytes:
            raise PackageError(f"a {'module' if module else 'theme'} holds at most {most_files} files and "
                               f"{most_bytes // 1000} kB")
    try:
        manifest = json.loads(found[MANIFEST])
    except ValueError as exc:
        raise PackageError(f"{MANIFEST} is not JSON: {exc}") from None
    check(manifest, found)
    return manifest, found


def package_id(files: dict[str, bytes]) -> str:
    """The package's identity: the sha256 of each file's sha256 by path, in canonical JSON."""
    listing = {path: _sha(data) for path, data in files.items()}
    return _sha(json.dumps(listing, sort_keys=True, separators=(",", ":")).encode())


def _luminance(colour: str) -> float:
    hexes = colour[1:] if len(colour) == 7 else "".join(c * 2 for c in colour[1:])
    def linear(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (linear(int(hexes[i:i + 2], 16) / 255) for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def check(manifest: dict, files: dict[str, bytes]) -> None:
    """Refuse a manifest that is not a theme this arcade can draw, with the first thing wrong."""
    if not isinstance(manifest, dict):
        raise PackageError("the manifest is a JSON object")
    if manifest.get("kind") not in KINDS:
        raise PackageError(f"kind is one of: {', '.join(KINDS)}")
    if manifest.get("api") != API:
        raise PackageError(f"api is {API}: the version of the arcade's package rules this was written for")
    if not NAME.match(str(manifest.get("name") or "")):
        raise PackageError("name is 2-32 lower-case letters, digits and dashes")
    if not VERSION.match(str(manifest.get("version") or "")):
        raise PackageError("version is three numbers, like 1.0.0")
    for field, most in (("title", 40), ("summary", 200)):
        if not isinstance(manifest.get(field, ""), str) or len(manifest.get(field, "")) > most:
            raise PackageError(f"{field} is text of at most {most} characters")
    if manifest["kind"] == "module":
        return check_module(manifest, files)
    look = manifest.get("look")
    if not isinstance(look, dict) or not isinstance(look.get("light"), dict):
        raise PackageError("look.light holds the theme's colours")
    for scheme in ("light", "dark"):
        colours = look.get(scheme)
        if colours is None and scheme == "dark":
            continue
        if not isinstance(colours, dict) or set(colours) != set(COLOURS):
            raise PackageError(f"look.{scheme} sets every colour: {', '.join(COLOURS)}")
        for name, value in colours.items():
            if not COLOUR.match(str(value)):
                raise PackageError(f"look.{scheme}.{name} is a colour like #3874d8")
        for fg, bg, least in CONTRAST:
            if contrast(colours[fg], colours[bg]) < least:
                raise PackageError(f"look.{scheme}: {fg} on {bg} is too hard to read "
                                   f"({contrast(colours[fg], colours[bg]):.1f}:1, at least {least}:1)")
    font = str(look.get("font", "system"))
    if font not in FONTS and font not in files:
        raise PackageError(f"look.font is one of {', '.join(FONTS)} or a files/*.woff2 in the package")
    radius = look.get("radius", 3)
    if not isinstance(radius, int) or not 0 <= radius <= 12:
        raise PackageError("look.radius is a whole number of pixels, 0 to 12")


def check_module(manifest: dict, files: dict[str, bytes]) -> None:
    """Refuse a node module's manifest with the first thing wrong: what it runs, the commands it adds, and what it may
    ask of the node."""
    if "module.py" not in files:
        raise PackageError("a module runs its module.py")
    needs = manifest.get("needs", {})
    if not isinstance(needs, dict) or set(needs) - {"chains", "max_fee"}:
        raise PackageError("needs holds chains (the reads it asks of each chain's Core) and max_fee; a module "
                           "reaches no host of its own yet")
    chains = needs.get("chains", {})
    if not isinstance(chains, dict) or not all(isinstance(m, list) for m in chains.values()):
        raise PackageError('needs.chains names each chain and its reads: {"dgb": ["getblockcount"]}')
    for chain, methods in chains.items():
        for method in methods:
            if (not READS.match(str(method)) or method in WALLET_READS or method in NOT_READS
                    or "wallet" in str(method) or "account" in str(method)):
                raise PackageError(f"needs.chains.{chain}: {method} is not a read a module may ask of a Core")
    fee = needs.get("max_fee", MOST_FEE)
    if not isinstance(fee, int) or not 0 <= fee <= MOST_FEE:
        raise PackageError(f"needs.max_fee is a whole number of the chain's smallest unit, at most {MOST_FEE}")
    commands = manifest.get("commands", {})
    if not isinstance(commands, dict):
        raise PackageError("commands names each command the module adds")
    for name, said in commands.items():
        if not COMMAND.match(name):
            raise PackageError(f"{name}: a command is 2-32 lower-case letters and digits")
        if not isinstance(said, dict) or said.get("audience", "anyone") not in AUDIENCES:
            raise PackageError(f"{name}: audience is one of {', '.join(AUDIENCES)}")
        if not isinstance(said.get("help", ""), str) or not all(
                isinstance(a, str) and re.fullmatch(r"\[?[a-z][a-z_]{0,20}\]?", a) for a in said.get("args", [])):
            raise PackageError(f"{name}: args are names, [in brackets] when they may be left out, and help is text")
        optional = [a.startswith("[") for a in said.get("args", [])]
        if optional != sorted(optional):
            raise PackageError(f"{name}: an argument that may be left out comes after every one that may not")
        if "form" in said:
            _check_form(name, said["form"], [a.strip("[]") for a in said.get("args", [])])
    if manifest.get("pages", "anyone") not in AUDIENCES:
        raise PackageError(f"pages is who may open /m/{manifest['name']}/: one of {', '.join(AUDIENCES)}")


#: A command's form (2026-11-05): how the Lite window draws it as a button and fields above the module's page, so each
#: step has a control as well as its console line. Words only, checked here; the window builds the controls itself and
#: runs the very line a person could type, so a form can do nothing its command can't.
FIELD_TYPES = ("text", "number", "choice")
MOST_WORDS = 80


def _words(where: str, value, most: int = MOST_WORDS) -> str:
    if not isinstance(value, str) or len(value) > most or any(ord(ch) < 32 for ch in value):
        raise PackageError(f"{where} is text of at most {most} characters, on one line")
    return value


def _check_form(name: str, form, args: list[str]) -> None:
    """A command's `form`: {"title", "button", "fields": {arg: {"label", "type", "value", "choices"}}}, every field one
    of the command's own arguments, in their order."""
    if not isinstance(form, dict) or set(form) - {"title", "button", "fields", "note"}:
        raise PackageError(f"{name}: form has a title, a button, a note and fields, and nothing else")
    for key in ("title", "button"):
        _words(f"{name}: form.{key}", form.get(key, ""))
    _words(f"{name}: form.note", form.get("note", ""), 240)
    fields = form.get("fields", {})
    if not isinstance(fields, dict) or [a for a in args if a in fields] != list(fields):
        raise PackageError(f"{name}: form.fields name the command's own arguments, in their order")
    for arg, field in fields.items():
        if not isinstance(field, dict) or set(field) - {"label", "type", "value", "choices"}:
            raise PackageError(f"{name}: form.fields.{arg} has a label, a type, a value and choices, and nothing else")
        _words(f"{name}: form.fields.{arg}.label", field.get("label", arg))
        if field.get("type", "text") not in FIELD_TYPES:
            raise PackageError(f"{name}: form.fields.{arg}.type is one of {', '.join(FIELD_TYPES)}")
        _words(f"{name}: form.fields.{arg}.value", field.get("value", ""))
        if (field.get("type") == "choice") != ("choices" in field):
            raise PackageError(f"{name}: form.fields.{arg} has choices when, and only when, its type is choice")
        choices = field.get("choices", [])
        if not isinstance(choices, list) or len(choices) > 20 or not all(
                isinstance(c, list) and len(c) == 2 and re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", str(c[0]))
                and _words(f"{name}: form.fields.{arg}.choices", c[1]) for c in choices):
            raise PackageError(f"{name}: form.fields.{arg}.choices are at most 20 [value, label] pairs, each value a word")


def theme_css(manifest: dict, files_url: str) -> str:
    """The CSS a checked theme sets: its variables over the window's and the full pages', in both schemes, and its
    font. Only values that `check` allowed reach here, so nothing in it is a builder's own CSS."""
    look = manifest["look"]
    def block(colours: dict) -> str:
        return ";".join(f"{var}:{colours[name]}" for name, names in COLOURS.items() for var in names)
    out = []
    font = look.get("font", "system")
    family = FONTS.get(font)
    if family is None:
        out.append(f"@font-face{{font-family:'arcade-theme';src:url('{files_url}/{font}') format('woff2')}}")
        family = "'arcade-theme'," + FONTS["system"]
    out.append(f"html:root,html body[data-lite]{{{block(look['light'])};--radius:{int(look.get('radius', 3))}px}}")
    dark = look.get("dark") or look["light"]
    out.append("@media (prefers-color-scheme:dark){html:root:not([data-theme=light]),"
               f"html:root:not([data-theme=light]) body[data-lite]{{{block(dark)}}}}}")
    out.append(f"html:root[data-theme=dark],html:root[data-theme=dark] body[data-lite]{{{block(dark)}}}")
    out.append(f"html body,html body[data-lite]{{font-family:{family}}}")
    return "\n".join(out) + "\n"


# --- what this arcade has -----------------------------------------------------------------------------------------

def home_of(home: Path, pid: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", pid or ""):
        raise PackageError("a package id is 64 hex characters")
    return Path(home) / FOLDER / pid


def install(home: Path, folder: Path, source: str = "local") -> tuple[str, dict]:
    """Copy a checked package folder into the arcade's home, under its id. (id, the record kept in settings)."""
    manifest, files = read(folder)
    pid = package_id(files)
    target = home_of(home, pid)
    if not target.exists():
        part = target.with_name(pid + ".part")
        shutil.rmtree(part, ignore_errors=True)
        for rel, data in files.items():
            (part / rel).parent.mkdir(parents=True, exist_ok=True)
            (part / rel).write_bytes(data)
        part.rename(target)
    return pid, {"kind": manifest["kind"], "name": manifest["name"], "version": manifest["version"],
                 "title": manifest.get("title") or manifest["name"], "summary": manifest.get("summary", ""),
                 "from": source, "added": int(time.time())}


#: Where the node modules that come with the arcade's own source are (examples/packages), so Admin can add one with a
#: button rather than only a console line at the machine (2026-11-06, the operator: step 4 "in the ui").
SHIPPED = Path("examples") / "packages"


def shipped(roots) -> list[tuple[Path, dict, str]]:
    """(folder, manifest, id) of each node module under SHIPPED in these source trees, by name; the first tree that
    has a name wins, and a folder that does not read as a module is left out."""
    found: dict[str, tuple[Path, dict, str]] = {}
    for root in roots:
        base = Path(root) / SHIPPED
        for folder in sorted(base.iterdir()) if base.is_dir() else ():
            if not folder.is_dir() or _kind_of(folder) != "module":
                continue
            try:
                manifest, files = read(folder)
            except (PackageError, OSError):
                continue
            found.setdefault(manifest["name"], (folder, manifest, package_id(files)))
    return [found[name] for name in sorted(found)]


def chosen(settings: dict, owner: str | None) -> str:
    """The theme someone sees: their own pick while it is installed and theirs to pick (the node's own view may wear
    any installed one), else the arcade's, else "" for ARCADE Lite's own look."""
    have = settings.get(SETTING) or {}
    mine = settings.get(f"{THEME}:{owner}") if owner else None
    if mine in have and (owner == "node" or mine in (settings.get(ALLOWED) or [])):
        return mine
    arcade = settings.get(THEME)
    return arcade if arcade in have else ""


def manifest_of(home: Path, pid: str) -> dict:
    return json.loads((home_of(home, pid) / MANIFEST).read_text())


def template(folder: Path, name: str) -> Path:
    """A new theme to start from: the Lite window's own look, with every value named, ready to change."""
    if not NAME.match(name):
        raise PackageError("name is 2-32 lower-case letters, digits and dashes")
    folder = Path(folder).expanduser() / name
    if folder.exists():
        raise PackageError(f"{folder} is already there")
    manifest = {
        "kind": "theme", "api": API, "name": name, "version": "1.0.0", "title": name.replace("-", " ").title(),
        "summary": "",
        "look": {
            "light": {"window": "#efefef", "panel": "#ffffff", "text": "#1d1d1d", "muted": "#666666",
                      "line": "#c4c4c4", "accent": "#3874d8", "accent_text": "#ffffff", "bar": "#e4e4e4",
                      "ok": "#2e7d32", "warn": "#b26a00", "bad": "#c62828"},
            "dark": {"window": "#2b2b2b", "panel": "#1f1f1f", "text": "#e6e6e6", "muted": "#a0a0a0",
                     "line": "#4a4a4a", "accent": "#3d7be0", "accent_text": "#ffffff", "bar": "#333333",
                     "ok": "#66bb6a", "warn": "#ffb74d", "bad": "#ef5350"},
            "font": "system", "radius": 3}}
    (folder / "files").mkdir(parents=True)
    (folder / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")
    (folder / "README.md").write_text(README.format(name=name, fonts=", ".join(FONTS)))
    return folder


README = """# {name}

An ArcadeCore theme. Change the colours in `manifest.json`, then on the arcade's own machine:

    arcade-cli checktheme ~/{name}     says what is wrong, or the theme's id
    arcade-cli trytheme ~/{name}       shows it in this machine's browser only; `trytheme off` to stop
    arcade-cli installtheme ~/{name}   keeps it, under its id
    arcade-cli settheme <id>           makes it this arcade's look; `allowtheme <id>` lets people pick it

- `look.light` and `look.dark` set every colour: window, panel, text, muted, line, accent, accent_text, bar, ok,
  warn, bad. Leave `dark` out to use the light colours in both. Text must stand out from the backgrounds: the arcade
  refuses a theme it would be hard to read a send or an address in.
- `look.font` is one of {fonts}, or a font of your own in `files/` (`files/name.woff2`).
- `look.radius` is the corner rounding in pixels, 0 to 12.
- A theme is values only: no CSS of its own, no scripts, nothing loaded from elsewhere.
- A changed theme is a new id: install it again, and choose it again.
"""

"""Which arcade a node looks like: its name, its colours and its icons.

One program, more than one face. DogecoinArcade is the default; a node started
with ``--brand pepecoin`` (or ``ARCADE_BRAND=pepecoin``) shows itself as
PepecoinArcade in Pepe green -- the look SadFrog drew on his pepecoinarcade
branch (2026-10-17), turned into a setting so his node keeps every update
instead of carrying 93 hand-edited files (2026-10-17).

Only the presentation changes. The protocol keeps its own name everywhere it
is part of what is signed or read off the chain -- the login challenge, the
Class B marker seed, the messaging identity message, the viewer shim -- so a
PepecoinArcade node and a DogecoinArcade node stay one network. The docs name
the software and its installer (folders, services), which keep their names
too, so they are not rebranded.

Adding a face: one entry in BRANDS, and its icons (icon-32/180/192/512.png) in
templates/brands/<key>/. ``css`` is laid over base.html's own rules, after them,
so it only names what differs. A new letter in a name needs the pixel font
re-cut (base_css.css, "Arcade Pixel").

A face per web address (SadFrog's side, 2026-11-03): one node published under
more than one name can wear a different face on each -- `brand_hosts` in
settings.json, {"digibyte-arcade.com": "digibyte"}, set in Admin -> Face. The
web app sets it for each request (`by_host`); everything else, the mesh and
the node's own name among them, keeps the node's own face (`own`).
"""
from __future__ import annotations

import contextvars
import os
import sys
from dataclasses import dataclass
from pathlib import Path

ICON_ROOT = Path(__file__).parent / "templates" / "brands"


@dataclass(frozen=True)
class Brand:
    key: str
    name: str
    #: browser chrome (meta theme-color) and the installable app's colours
    light_bg: str
    dark_bg: str
    light_fg: str
    #: the stand-alone pages that carry no base.html (offline, refused, locked)
    accent: str
    dark_accent: str
    muted: str
    dark_muted: str
    dark_fg: str
    #: laid over base.html's styles; empty for the default face
    css: str = ""

    def icon(self, name: str) -> Path | None:
        """This face's own copy of an icon, or None to use the default one."""
        path = ICON_ROOT / self.key / name
        return path if path.is_file() else None


DOGECOIN = Brand(
    key="dogecoin", name="DogecoinArcade",
    light_bg="#faf8f4", dark_bg="#141310", light_fg="#1b1a17",
    accent="#b8860b", dark_accent="#d9a520", muted="#6b665c", dark_muted="#9b948a",
    dark_fg="#ece8e0")

PEPECOIN = Brand(
    key="pepecoin", name="PepecoinArcade",
    light_bg="#f4f8f1", dark_bg="#0d140e", light_fg="#14200f",
    accent="#2f8f3a", dark_accent="#5fcb5a", muted="#5a6a52", dark_muted="#8ea486",
    dark_fg="#e4efe0",
    css="""
:root{
  --bg:#f4f8f1; --panel:#fbfdf9; --fg:#14200f; --muted:#5a6a52; --line:#d6e3cd;
  --accent:#2f8f3a; --accent-fg:#fff; --ok:#1f7a5c; --code:#e9f2e3;
  --shadow:0 1px 2px rgba(12,40,16,.05),0 4px 14px rgba(12,40,16,.06);
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme=light]){
    --bg:#0d140e; --panel:#131c14; --fg:#e4efe0; --muted:#8ea486; --line:#223024;
    --accent:#5fcb5a; --accent-fg:#0d140e; --ok:#4cc29a; --code:#162017;
    --shadow:0 1px 2px rgba(0,0,0,.3),0 4px 14px rgba(0,0,0,.22);
  }
}
:root[data-theme=dark]{
    --bg:#0d140e; --panel:#131c14; --fg:#e4efe0; --muted:#8ea486; --line:#223024;
    --accent:#5fcb5a; --accent-fg:#0d140e; --ok:#4cc29a; --code:#162017;
    --shadow:0 1px 2px rgba(0,0,0,.3),0 4px 14px rgba(0,0,0,.22);
}
.tile:hover{box-shadow:0 2px 4px rgba(12,40,16,.06),0 10px 24px rgba(12,40,16,.1)}
""")

#: SadFrog's LitecoinArcade (2026-10-17): Litecoin's own blue (#345d9d) on a cool,
#: silver-tinted ground; a lighter blue in dark mode so the accent keeps its contrast.
LITECOIN = Brand(
    key="litecoin", name="LitecoinArcade",
    light_bg="#f3f5f9", dark_bg="#0d1118", light_fg="#141a26",
    accent="#345d9d", dark_accent="#7fa3dc", muted="#5b6475", dark_muted="#8b95a8",
    dark_fg="#e3e8f1",
    css="""
:root{
  --bg:#f3f5f9; --panel:#fbfcfe; --fg:#141a26; --muted:#5b6475; --line:#d5dbe6;
  --accent:#345d9d; --accent-fg:#fff; --ok:#1f7a5c; --code:#e8edf5;
  --shadow:0 1px 2px rgba(16,28,52,.05),0 4px 14px rgba(16,28,52,.06);
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme=light]){
    --bg:#0d1118; --panel:#131924; --fg:#e3e8f1; --muted:#8b95a8; --line:#222b3a;
    --accent:#7fa3dc; --accent-fg:#0d1118; --ok:#4cc29a; --code:#161d2a;
    --shadow:0 1px 2px rgba(0,0,0,.3),0 4px 14px rgba(0,0,0,.22);
  }
}
:root[data-theme=dark]{
    --bg:#0d1118; --panel:#131924; --fg:#e3e8f1; --muted:#8b95a8; --line:#222b3a;
    --accent:#7fa3dc; --accent-fg:#0d1118; --ok:#4cc29a; --code:#161d2a;
    --shadow:0 1px 2px rgba(0,0,0,.3),0 4px 14px rgba(0,0,0,.22);
}
.tile:hover{box-shadow:0 2px 4px rgba(16,28,52,.06),0 10px 24px rgba(16,28,52,.1)}
""")

#: SadFrog's DigiByteArcade (2026-11-03): DigiByte's own bright blue (#0066cc) on a clear,
#: blue-white ground, so it reads apart from Litecoin's slate; a lighter blue in dark mode.
DIGIBYTE = Brand(
    key="digibyte", name="DigiByteArcade",
    light_bg="#f2f6fb", dark_bg="#0a1020", light_fg="#0f1a2b",
    accent="#0066cc", dark_accent="#4d9fff", muted="#56647a", dark_muted="#8a9ab3",
    dark_fg="#e2eaf6",
    css="""
:root{
  --bg:#f2f6fb; --panel:#fbfcff; --fg:#0f1a2b; --muted:#56647a; --line:#d3dded;
  --accent:#0066cc; --accent-fg:#fff; --ok:#1f7a5c; --code:#e6eef9;
  --shadow:0 1px 2px rgba(0,40,90,.05),0 4px 14px rgba(0,40,90,.07);
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme=light]){
    --bg:#0a1020; --panel:#111a2e; --fg:#e2eaf6; --muted:#8a9ab3; --line:#1f2b44;
    --accent:#4d9fff; --accent-fg:#0a1020; --ok:#4cc29a; --code:#141f36;
    --shadow:0 1px 2px rgba(0,0,0,.3),0 4px 14px rgba(0,0,0,.22);
  }
}
:root[data-theme=dark]{
    --bg:#0a1020; --panel:#111a2e; --fg:#e2eaf6; --muted:#8a9ab3; --line:#1f2b44;
    --accent:#4d9fff; --accent-fg:#0a1020; --ok:#4cc29a; --code:#141f36;
    --shadow:0 1px 2px rgba(0,0,0,.3),0 4px 14px rgba(0,0,0,.22);
}
.tile:hover{box-shadow:0 2px 4px rgba(0,40,90,.07),0 10px 24px rgba(0,40,90,.11)}
""")

#: The neutral face (2026-11-03, SadFrog: "It should not have any default theme ... it should be og core like if
#: anything by default"; "Full should also be rebranded by default to a neutral theme labeled Arcade"): no coin's name
#: or colours, the classic Core wallet's greys and its blue selection, system fonts, plain icons. A new arcade starts
#: with it, in either edition (`first_face`); ARCADE Lite wears it whenever its operator has picked no face.
CORE = Brand(
    key="core", name="ARCADE",
    light_bg="#efefef", dark_bg="#2b2b2b", light_fg="#1d1d1d",
    accent="#3874d8", dark_accent="#5b93ea", muted="#666666", dark_muted="#a0a0a0",
    dark_fg="#e6e6e6",
    css="""
:root{
  --bg:#efefef; --panel:#ffffff; --fg:#1d1d1d; --muted:#666666; --line:#c4c4c4;
  --accent:#3874d8; --accent-fg:#fff; --ok:#2e7d32; --code:#f3f3f3;
  --shadow:0 1px 2px rgba(0,0,0,.08);
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme=light]){
    --bg:#2b2b2b; --panel:#1f1f1f; --fg:#e6e6e6; --muted:#a0a0a0; --line:#4a4a4a;
    --accent:#5b93ea; --accent-fg:#fff; --ok:#66bb6a; --code:#333333;
    --shadow:0 1px 2px rgba(0,0,0,.3);
  }
}
:root[data-theme=dark]{
    --bg:#2b2b2b; --panel:#1f1f1f; --fg:#e6e6e6; --muted:#a0a0a0; --line:#4a4a4a;
    --accent:#5b93ea; --accent-fg:#fff; --ok:#66bb6a; --code:#333333;
    --shadow:0 1px 2px rgba(0,0,0,.3);
}
body{font-family:system-ui,-apple-system,"Segoe UI",Ubuntu,sans-serif}
.brand{font-family:system-ui,-apple-system,"Segoe UI",Ubuntu,sans-serif;font-size:14px;font-weight:600;letter-spacing:0}
.tile:hover{box-shadow:0 1px 3px rgba(0,0,0,.12)}
""")

BRANDS = {b.key: b for b in (DOGECOIN, PEPECOIN, LITECOIN, DIGIBYTE, CORE)}
DEFAULT = DOGECOIN

#: The face a node with no ARCADE_BRAND wears: DogecoinArcade, or what the web app says for its edition (ARCADE
#: Lite: CORE, until its operator picks a face). Read on every call, so a change of edition shows at once.
fallback = lambda: DEFAULT  # noqa: E731

#: Whether the face was fixed from outside (--brand or ARCADE_BRAND in the service)
#: when the process started. Then the admin panel shows it and cannot change it;
#: otherwise the operator's choice (the `brand` setting) is applied with `use`.
FROM_ENV = bool((os.environ.get("ARCADE_BRAND") or "").strip())


def first_face(settings: dict, fresh: bool) -> str | None:
    """The face a starting arcade records, or None to leave it: the neutral one on a fresh install that was not told
    one. An arcade already running keeps the face it shows, DogecoinArcade included, so an update changes no look."""
    return CORE.key if fresh and not settings.get("brand") and not FROM_ENV else None


def use(key: str) -> Brand:
    """Show `key`'s face from now on, in this process (the admin panel's choice).

    The process environment is what `current()` reads, so every later page,
    icon and manifest follows at once; nothing needs a restart."""
    key = (key or "").strip().lower()
    if key not in BRANDS:
        raise ValueError(f"no such face: {key!r} (one of {sorted(BRANDS)})")
    os.environ["ARCADE_BRAND"] = key
    return BRANDS[key]


def current() -> Brand:
    """The face shown now: the web address's own face during a request that came in by one
    (`brand_hosts`), else the node's own face (`own`)."""
    return _HOST.get() or own()


def own() -> Brand:
    """The node's own face: ARCADE_BRAND (set by --brand or the operator's pick), else `fallback()`.

    An unknown name is said once on stderr and falls back to the default, so a
    typo in a service file costs the colours, never the node."""
    key = (os.environ.get("ARCADE_BRAND") or fallback().key).strip().lower()
    brand = BRANDS.get(key)
    if brand is None:
        if key not in _warned:
            _warned.add(key)
            print(f"ARCADE_BRAND={key!r} is not one of {sorted(BRANDS)}; "
                  f"showing {DEFAULT.name}", file=sys.stderr)
        return DEFAULT
    return brand


_warned: set[str] = set()

#: The face of the web address this request came in by, for one request (the web app's `_HostFace`).
_HOST: "contextvars.ContextVar[Brand | None]" = contextvars.ContextVar("arcade_host_face", default=None)


def hosts(said) -> dict[str, str]:
    """`brand_hosts` as {host: face key}: names lower-cased without a port, unknown faces left out."""
    out: dict[str, str] = {}
    if isinstance(said, dict):
        for host, key in said.items():
            host = str(host).strip().lower().rsplit(":", 1)[0]
            key = str(key).strip().lower()
            if host and key in BRANDS:
                out[host] = key
    return out


def by_host(host: str | None, said) -> Brand | None:
    """The face `brand_hosts` gives the web address `host` (a Host header), or None."""
    if not host:
        return None
    key = hosts(said).get(str(host).strip().lower().rsplit(":", 1)[0])
    return BRANDS.get(key) if key else None


class _Current:
    """The templates' `brand`: whichever face `current()` says when a page reads it."""

    def __getattr__(self, name):
        return getattr(current(), name)


CURRENT = _Current()

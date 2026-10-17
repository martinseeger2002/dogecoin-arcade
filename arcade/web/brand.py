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
so it only names what differs.
"""
from __future__ import annotations

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
  :root{
    --bg:#0d140e; --panel:#131c14; --fg:#e4efe0; --muted:#8ea486; --line:#223024;
    --accent:#5fcb5a; --accent-fg:#0d140e; --ok:#4cc29a; --code:#162017;
    --shadow:0 1px 2px rgba(0,0,0,.3),0 4px 14px rgba(0,0,0,.22);
  }
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
  :root{
    --bg:#0d1118; --panel:#131924; --fg:#e3e8f1; --muted:#8b95a8; --line:#222b3a;
    --accent:#7fa3dc; --accent-fg:#0d1118; --ok:#4cc29a; --code:#161d2a;
    --shadow:0 1px 2px rgba(0,0,0,.3),0 4px 14px rgba(0,0,0,.22);
  }
}
.tile:hover{box-shadow:0 2px 4px rgba(16,28,52,.06),0 10px 24px rgba(16,28,52,.1)}
""")

BRANDS = {b.key: b for b in (DOGECOIN, PEPECOIN, LITECOIN)}
DEFAULT = DOGECOIN

#: Whether the face was fixed from outside (--brand or ARCADE_BRAND in the service)
#: when the process started. Then the admin panel shows it and cannot change it;
#: otherwise the operator's choice (the `brand` setting) is applied with `use`.
FROM_ENV = bool((os.environ.get("ARCADE_BRAND") or "").strip())


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
    """The face this process shows: ARCADE_BRAND (set by --brand), else DogecoinArcade.

    An unknown name is said once on stderr and falls back to the default, so a
    typo in a service file costs the colours, never the node."""
    key = (os.environ.get("ARCADE_BRAND") or DEFAULT.key).strip().lower()
    brand = BRANDS.get(key)
    if brand is None:
        if key not in _warned:
            _warned.add(key)
            print(f"ARCADE_BRAND={key!r} is not one of {sorted(BRANDS)}; "
                  f"showing {DEFAULT.name}", file=sys.stderr)
        return DEFAULT
    return brand


_warned: set[str] = set()

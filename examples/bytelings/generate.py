#!/usr/bin/env python3
"""Bytelings: pixel monsters, the arcade's own DigiByte ordinals collection.

    python3 examples/bytelings/generate.py            # writes examples/bytelings/build/
    python3 examples/bytelings/generate.py --sheet preview.png --scale 6

A thank-you to everyone who tried DigiDollar test on the arcade (2026-11-07, SadFrog). Each Byteling is drawn here
from a seed, never by hand, so anyone can run this and get the same 100 pictures, byte for byte, as the ones on the
chain. They are digital monsters in the virtual-pet tradition: a baby blob, then a rookie, up to armoured giants, seen
side on with claws, tails, spikes, straps and armour. Our own monsters: no other franchise's names, characters or looks.

The first hundred are all babies, the Bit stage (2026-11-07, SadFrog: "100 babys that can eventually become bigger"):
blobs, drops, puffs, beans, tadpoles, wisps and mounds. Every 30 days after the pad opens, the next stage can be
reached by evolving, which is to come: a Bytelings site on the arcade's backbone where an owner signs to burn their
babies for a grown one. The grown bodies below are drawn already, for those later hundreds (`--stage`).

What comes out is a HashLips build (`build/images/<n>.png`, `build/json/<n>.json` and `build/json/_metadata.json`),
the layout the Creator tab already inscribes a whole collection from (arcade/collections.py). A picture is 32 by 32
pixels in an indexed palette, a few hundred bytes, so the fee to inscribe it stays small; pages show it scaled up with
`image-rendering: pixelated`.

Every Byteling has a stage (how far it has grown: Bit, Byte, Kilo, Mega, Giga), an element (its colours), a body
(a Bit's is one of the baby forms; a grown one is a Dino, Beast, Dragon, Avian or Imp), its head, eyes, mouth, marking, gear and background. No two
have the same traits. Pillow is the only thing it needs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path

from PIL import Image

NAME = "Bytelings"
COUNT = 100
SIZE = 32
SEED = "bytelings/2026-11-07"
DESCRIPTION = ("A Byteling: a baby pixel monster, one of the first 100, the Arcade's own collection on DigiByte, "
               "minted with DigiDollar test to thank the people who tried it. Every 30 days it can grow a stage.")
HERE = Path(__file__).resolve().parent
GROUND = 29

#: The stages, youngest first. The first hundred are all Bits (`--stage` draws a hundred of another, for later).
STAGE_NAMES = ("Bit", "Byte", "Kilo", "Mega", "Giga")
STAGES = [("Bit", 100)]
#: How big each stage is drawn, and how much bigger its head is than a grown one's (the young are all head).
SCALE = {"Bit": (1.3, 1.0), "Byte": (.74, 1.25), "Kilo": (.86, 1.1), "Mega": (.95, 1.0), "Giga": (1.0, 1.0)}

#: Each element's colours: background, body, body light, body dark, accent, horn and claw.
ELEMENTS = {
    "Flame": ("#3a1b14", "#e8642e", "#ffa04a", "#a8361f", "#ffd84a", "#fff1c9"),
    "Tide": ("#10263f", "#2f80d6", "#6fb8ff", "#1d4f91", "#7ef0e0", "#eaf6ff"),
    "Volt": ("#2b2710", "#f2c82a", "#fff07a", "#b08a1e", "#5ad0ff", "#fffbe0"),
    "Leaf": ("#14301b", "#4cad4f", "#8fdc66", "#2a6e37", "#f2a7d8", "#f5ffe0"),
    "Frost": ("#1a2c3a", "#a9dcef", "#f2fbff", "#6aa2c4", "#6f8cff", "#ffffff"),
    "Shade": ("#17122a", "#6a46b8", "#9a7ae6", "#3f2878", "#ff5ab4", "#e8dcff"),
    "Bloom": ("#331a2a", "#ee88b4", "#ffc4dc", "#b84f80", "#7ef0b0", "#ffffff"),
    "Steel": ("#1d2226", "#8e9ba6", "#cdd6dd", "#56616b", "#ff4a4a", "#f2f5f7"),
    "Terra": ("#2a1e14", "#a8703f", "#d49d6c", "#6e4527", "#7ed957", "#f7ead8"),
    "Glitch": ("#0e0e16", "#ff3cac", "#3cf6ff", "#7a1f86", "#c6ff3c", "#ffffff"),
}
#: How many of each element there are: Glitch is rare, the rest share what is left.
ELEMENT_COUNTS = {"Glitch": 2, "Steel": 10}
#: How many are gold all over.
SHINIES = 3
INK = "#160f1c"
EYE_WHITE = "#fdfdfd"
GOLD = ("#f2c02a", "#fff09a", "#b8820f")
METAL = ("#b9c3cc", "#e9eef2", "#6c7884")
LEATHER = "#5a3620"
LCD = ("#8fa86a", "#7f9a5c", "#6c8550")

BODIES = ["Dino", "Dino", "Beast", "Beast", "Dragon", "Avian", "Imp"]
HEADS = ["Horns", "Horns", "Unihorn", "Crest", "Frill", "Ears", "Plain"]
EYES = ["Sharp", "Sharp", "Round", "Glow", "Fierce"]
MOUTHS = ["Fangs", "Fangs", "Grin", "Closed"]
MARKINGS = ["None", "Stripes", "Stripes", "Tribal", "Belly", "Spots"]
GEAR = ["None", "None", "Belts", "Belts", "Gauntlet", "Helmet", "Armor", "DigiDollar coin"]
BACKGROUNDS = ["Element", "Element", "Element", "LCD", "Data"]
#: What a baby (a Bit) can be instead.
BABY_FORMS = ["Blob", "Drop", "Puff", "Bean", "Tadpole", "Wisp", "Mound"]
BABY_HEADS = ["Horns", "Unihorn", "Ears", "Crest", "Sprout", "Antenna", "Plain", "Plain"]
BABY_EYES = ["Round", "Round", "Round", "Sharp", "Glow", "Fierce", "Sleepy"]
BABY_MOUTHS = ["Fangs", "Grin", "Grin", "Closed", "Open"]
BABY_MARKINGS = ["None", "None", "Spots", "Stripes", "Tribal", "Blush", "Blush"]
BABY_GEAR = ["None", "None", "None", "None", "Eggshell", "Bow", "DigiDollar coin"]


def hexrgb(h: str) -> tuple[int, int, int]:
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


class Canvas:
    """A 32x32 grid of parts: each pixel is empty or names what it is (body, belly, eye...) and which piece of the
    monster drew it, so a limb in front of the body gets a line of its own. Coloured at the end."""

    def __init__(self, k: float = 1.0, hk: float = 1.0, head: tuple[float, float] = (0, 0)):
        self.px: list[list[str | None]] = [[None] * SIZE for _ in range(SIZE)]
        self.grp: list[list[int]] = [[0] * SIZE for _ in range(SIZE)]
        self.k, self.hk, self.head = k, hk, head
        self.group = 0
        self.scaled_head = False

    # where a point of a full-grown monster lands at this stage: scaled about the ground, the head a little more
    def at(self, x: float, y: float) -> tuple[float, float]:
        if self.scaled_head:
            hx, hy = self.head
            x, y = hx + (x - hx) * self.hk, hy + (y - hy) * self.hk
        return 15 + (x - 15) * self.k, GROUND - (GROUND - y) * self.k

    def r(self, r: float) -> float:
        return r * self.k * (self.hk if self.scaled_head else 1)

    def new(self, scaled_head: bool = False):
        """The next piece: what it draws over an earlier piece gets an edge."""
        self.group += 1
        self.scaled_head = scaled_head

    def get(self, x: int, y: int) -> str | None:
        return self.px[y][x] if 0 <= x < SIZE and 0 <= y < SIZE else None

    def put(self, x: int, y: int, part: str, over: bool = True, only: tuple = ()):
        if not (0 <= x < SIZE and 0 <= y < SIZE):
            return
        if only and self.px[y][x] not in only:
            return
        if over or self.px[y][x] is None:
            self.px[y][x] = part
            self.grp[y][x] = self.group

    def shape(self, inside, part: str, only: tuple = ()):
        for y in range(SIZE):
            for x in range(SIZE):
                if inside(x + .5, y + .5):
                    self.put(x, y, part, only=only)

    def ellipse(self, cx, cy, rx, ry, part, only: tuple = ()):
        (cx, cy), rx, ry = self.at(cx, cy), self.r(rx), self.r(ry)
        self.shape(lambda x, y: ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 <= 1, part, only)

    def capsule(self, x0, y0, r0, x1, y1, r1, part, only: tuple = ()):
        """A limb: a round-ended stroke from one point to another, `r0` thick at the start and `r1` at the end."""
        (x0, y0), (x1, y1), r0, r1 = self.at(x0, y0), self.at(x1, y1), self.r(r0), self.r(r1)
        dx, dy = x1 - x0, y1 - y0
        ll = dx * dx + dy * dy or 1e-9

        def inside(x, y):
            t = max(0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy) / ll))
            px, py = x0 + t * dx, y0 + t * dy
            return (x - px) ** 2 + (y - py) ** 2 <= (r0 + (r1 - r0) * t) ** 2
        self.shape(inside, part, only)

    def tri(self, pts, part, only: tuple = ()):
        (ax, ay), (bx, by), (cx, cy) = [self.at(*p) for p in pts]

        def side(px, py, qx, qy, rx, ry):
            return (px - rx) * (qy - ry) - (qx - rx) * (py - ry)

        def inside(x, y):
            d1, d2, d3 = side(x, y, ax, ay, bx, by), side(x, y, bx, by, cx, cy), side(x, y, cx, cy, ax, ay)
            return not ((d1 < 0 or d2 < 0 or d3 < 0) and (d1 > 0 or d2 > 0 or d3 > 0))
        self.shape(inside, part, only)

    def dot(self, x, y, part, only: tuple = ()):
        px, py = self.at(x, y)
        self.put(int(px), int(py), part, only=only)

    def filled(self, x: int, y: int) -> bool:
        return self.get(x, y) is not None


# --- the bodies ----------------------------------------------------------------------------------------------------
#
# Each is drawn for a full-grown monster facing right, standing on GROUND; the canvas scales it for its stage. Back
# pieces first (the far legs and wings, in the darker colour), then the tail and the body, then the near limbs, then
# the head. A function returns where the head is, for the features that go on it.

def dino(c: Canvas, t: dict) -> dict:
    c.new(); c.capsule(16, 22, 2.6, 17, 27, 1.6, "far"); c.capsule(15, 28, 1.3, 20, 28, 1.2, "far")
    c.new(); c.capsule(10, 21, 3.4, 2.5, 25.5, .9, "body")
    c.ellipse(13, 20, 5.6, 5, "body")
    c.ellipse(15.5, 21.5, 3, 3.6, "belly", only=("body",))
    c.new(); c.ellipse(12, 23, 3.2, 3.2, "body"); c.capsule(12, 24, 2.3, 13, 27.5, 1.7, "body")
    c.capsule(11, 28.3, 1.3, 17, 28.3, 1.3, "body"); c.dot(18, 28.3, "claw"); c.dot(18, 27.3, "claw")
    c.new(True); c.capsule(16, 18, 3, 18.5, 13, 2.8, "body")
    c.ellipse(19.5, 10.5, 4.6, 4, "body"); c.capsule(21, 11.5, 2.9, 26, 12.5, 2.2, "body")
    c.new(); c.capsule(17.5, 18.5, 1.4, 20, 21, 1.1, "body"); c.dot(21, 21, "claw"); c.dot(21, 22, "claw")
    return {"eye": (21, 9), "mouth": (21, 13.3, 26.5), "top": (19.5, 6.6), "back": [(8, 16.5), (11, 15), (14, 14.3)],
            "chest": (16.5, 19.5), "arm": (18.5, 19.5, 21, 21.5), "ear": (17.5, 8)}


def beast(c: Canvas, t: dict) -> dict:
    c.new(); c.capsule(11, 22, 2.2, 11, 28.3, 1.6, "far"); c.capsule(21.5, 22, 2, 21.5, 28.3, 1.6, "far")
    c.new(); c.capsule(7.5, 20, 2, 3.5, 14, 2.6, "body"); c.ellipse(3.5, 13, 2.2, 2.4, "accent")
    c.ellipse(14.5, 21, 8, 4.6, "body")
    c.ellipse(16, 23, 5, 2.4, "belly", only=("body",))
    c.new(); c.capsule(8.5, 22, 2.6, 8.5, 28, 1.9, "body"); c.capsule(7.5, 28.4, 1.2, 11, 28.4, 1.2, "body")
    c.dot(11.5, 28.4, "claw")
    c.new(); c.capsule(19, 22, 2.3, 19, 28, 1.8, "body"); c.capsule(18, 28.4, 1.2, 21.8, 28.4, 1.2, "body")
    c.dot(22.3, 28.4, "claw")
    c.new(True); c.capsule(19, 19, 3.2, 21, 15, 3, "body"); c.ellipse(22, 14, 4.4, 4, "body")
    c.capsule(23, 15, 2.4, 27.5, 16.2, 1.8, "body")
    return {"eye": (23.5, 12.5), "mouth": (24, 17, 28), "top": (21.5, 10.2), "back": [(11, 16.6), (14, 16.3), (17, 16.8)],
            "chest": (20, 19), "arm": None, "ear": (20, 10.5)}


def dragon(c: Canvas, t: dict) -> dict:
    c.new(); c.tri([(12, 17), (1.5, 3), (17, 15)], "dark"); c.tri([(12, 17), (3, 9), (6, 18)], "dark")
    got = dino(c, t)
    c.new(); c.tri([(11, 17), (4, 5), (15, 15)], "accent"); c.tri([(11, 17), (5, 10), (8, 18.5)], "accent")
    c.capsule(11, 17, .5, 4, 5, .5, "dark", only=("accent",))
    return got


def avian(c: Canvas, t: dict) -> dict:
    c.new(); c.capsule(15, 24, .9, 15.5, 28.3, .8, "horn"); c.dot(17, 28.3, "claw")
    c.new(); c.tri([(9, 17), (1.5, 13), (3, 24)], "accent"); c.tri([(9, 19), (2.5, 19), (5, 25)], "dark")
    c.ellipse(13.5, 19.5, 6, 5.4, "body")
    c.ellipse(15.5, 21, 3.2, 3.6, "belly", only=("body",))
    c.new(); c.ellipse(11.5, 19.5, 4.6, 3.3, "dark"); c.tri([(8, 19), (4, 23), (11, 21)], "dark")
    c.new(); c.capsule(12.5, 24, .9, 12.5, 28.3, .8, "horn"); c.capsule(11, 28.4, .7, 15, 28.4, .7, "horn")
    c.new(True); c.ellipse(18.5, 11.5, 4.3, 4, "body")
    c.tri([(21.5, 9.5), (28, 12), (21.5, 14)], "horn"); c.capsule(22, 12.2, .4, 27, 12.2, .4, "ink", only=("horn",))
    return {"eye": (19.5, 10), "mouth": None, "top": (18.5, 7.6), "back": [], "chest": (16, 20), "arm": None,
            "ear": (16.5, 8)}


def imp(c: Canvas, t: dict) -> dict:
    c.new(); c.tri([(12, 16), (3, 8), (7, 19)], "dark"); c.tri([(12, 17), (5, 13), (8, 21)], "dark")
    c.new(); c.capsule(11, 25, 1, 5, 27, .7, "body"); c.tri([(5.5, 25), (2.5, 27), (5.5, 29)], "accent")
    c.ellipse(15, 22, 4.3, 4.8, "body")
    c.ellipse(16.2, 22.5, 2.4, 3, "belly", only=("body",))
    c.new(); c.capsule(13.5, 25, 2, 13.5, 28.2, 1.5, "body"); c.capsule(12.5, 28.4, 1.1, 17, 28.4, 1.1, "body")
    c.capsule(17, 25, 1.8, 17.5, 28.2, 1.5, "far")
    c.new(True); c.ellipse(16.5, 12.5, 6.3, 5.8, "body")
    c.new(); c.capsule(17.5, 19.5, 1.3, 21, 22.5, 1.1, "body"); c.dot(22, 22.5, "claw"); c.dot(21.5, 23.5, "claw")
    return {"eye": (19, 11), "mouth": (17.5, 15.2, 21.5), "top": (16.5, 6.8), "back": [], "chest": (16, 21),
            "arm": (17.5, 19.5, 21, 22.5), "ear": (12.5, 8.5), "two_eyes": True}


# The babies, a Bit's forms: small, round and mostly face, looking at you. Each says where its eyes (the middle of
# the pair), mouth and top are, its `ear` (the left one; the right is its mirror about the top) and `side`, where it
# holds a coin.

def _feet(c: Canvas, y: float = 28.6, x0: float = 10.5, x1: float = 21.5):
    c.capsule(x0 - 1, y, 1.2, x0 + 1, y, 1.2, "far"); c.capsule(x1 - 1, y, 1.2, x1 + 1, y, 1.2, "far")


def _baby(eye, mouth, top, ear, side, **more) -> dict:
    return {"eye": eye, "mouth": mouth, "top": top, "back": [], "chest": None, "arm": None, "ear": ear,
            "side": side, "two_eyes": True, "baby": True, **more}


def blob(c: Canvas, t: dict) -> dict:
    c.new(); _feet(c)
    c.new(); c.ellipse(16, 23.5, 7.5, 6, "body")
    c.ellipse(16, 25.5, 4.5, 3, "belly", only=("body",))
    return _baby((16, 22), (15, 25.3, 17.5), (16, 17.6), (11.5, 19), (22, 25.5))


def drop(c: Canvas, t: dict) -> dict:
    c.new(); _feet(c, x0=11, x1=21)
    c.new(); c.ellipse(16, 24.2, 6.6, 5.2, "body"); c.tri([(9.6, 23.2), (17.5, 12.5), (22.4, 23.2)], "body")
    c.ellipse(16, 26, 4, 2.6, "belly", only=("body",))
    return _baby((16, 22.5), (15, 25.6, 17.5), (17.3, 13.6), (12, 19.5), (21.5, 26))


def puff(c: Canvas, t: dict) -> dict:
    c.new(); _feet(c)
    c.new(); c.ellipse(16, 24, 7.6, 5.4, "body")
    for x, y in ((10.5, 20.5), (14, 19), (18, 19), (21.5, 20.5), (8.7, 23.5), (23.3, 23.5)):
        c.ellipse(x, y, 2.6, 2.4, "body")
    c.ellipse(16, 26, 4.5, 2.6, "belly", only=("body",))
    return _baby((16, 22.6), (15, 25.6, 17.5), (16, 16.8), (11, 19), (22, 26))


def bean(c: Canvas, t: dict) -> dict:
    c.new(); _feet(c, x0=12.5, x1=19.5)
    c.new(); c.ellipse(16, 21.8, 5.6, 6.8, "body")
    c.ellipse(16, 24, 3.4, 3.6, "belly", only=("body",))
    c.new(); c.capsule(10.6, 21.5, 1, 9.3, 24, .9, "body"); c.capsule(21.4, 21.5, 1, 22.7, 24, .9, "body")
    return _baby((16, 19.5), (15, 23, 17.5), (16, 15.2), (12.4, 16.5), (22.5, 25))


def tadpole(c: Canvas, t: dict) -> dict:
    c.new(); c.capsule(12, 24, 3, 5.5, 20.5, .9, "body"); c.ellipse(5.6, 20.4, 1.5, 1.5, "accent")
    c.new(); c.capsule(13.5, 28.4, 1.2, 15.5, 28.4, 1.2, "far"); c.capsule(19, 28.4, 1.2, 21, 28.4, 1.2, "far")
    c.new(); c.ellipse(17.5, 23, 6.6, 5.6, "body")
    c.ellipse(18.5, 25.3, 4, 2.6, "belly", only=("body",))
    return _baby((18.5, 21.6), (17.5, 24.8, 20), (17.5, 17.4), (13.5, 19), (23.5, 25.5))


def wisp(c: Canvas, t: dict) -> dict:
    c.new(); c.ellipse(16, 20.5, 6.6, 6, "body")
    lo, hi = c.at(9.4, 20.5), c.at(22.6, 26)
    c.shape(lambda x, y: lo[0] <= x <= hi[0] and lo[1] <= y <= hi[1], "body")
    for x in (11, 16, 21):
        c.ellipse(x, 26, 1.7, 1.5, "body")
    c.ellipse(16, 23, 3.6, 2.6, "belly", only=("body",))
    return _baby((16, 19), (15, 22.6, 17.5), (16, 14.6), (11.5, 16), (21.5, 23.5))


def mound(c: Canvas, t: dict) -> dict:
    c.new()
    cx, cy = c.at(16, 29.2)
    rx, ry = c.r(9.4), c.r(8.6)
    c.shape(lambda x, y: ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 <= 1 and y < cy, "body")
    for x, y in ((9, 26.5), (23.5, 25.5)):
        c.capsule(x, y, .8, x, y + 2.2, .9, "body")
    c.ellipse(16, 28, 5, 2.4, "belly", only=("body",))
    return _baby((16, 24), (15, 26.8, 17.5), (16, 20.8), (10.8, 23), (22.5, 27))


BABIES = {"Blob": blob, "Drop": drop, "Puff": puff, "Bean": bean, "Tadpole": tadpole, "Wisp": wisp, "Mound": mound}
BODY_DRAW = {"Dino": dino, "Beast": beast, "Dragon": dragon, "Avian": avian, "Imp": imp, **BABIES}


def draw(t: dict) -> Image.Image:
    """One Byteling from its traits: drawn a little smaller until nothing of it (a horn, a crown) leaves the frame."""
    k, hk = SCALE.get(t["Stage"], (1.0, 1.0))
    while True:
        c = compose(t, k, hk)
        if k < .6 or not any(c.filled(x, y) for y in (0, 1) for x in range(SIZE)):
            return paint(c, t)
        k -= .03


def compose(t: dict, k: float, hk: float) -> Canvas:
    """Byteling `t`'s parts, drawn at scale `k` (its head at `hk` of that)."""
    stage = t["Stage"]
    c = Canvas(k, hk)
    # the head is scaled about its own middle, which each body says through its first draw
    c.head = {"Dino": (20, 11), "Dragon": (20, 11), "Beast": (22, 14), "Avian": (18.5, 11.5),
              "Imp": (16.5, 12.5)}.get(t["Body"], (16, 23))
    f = BODY_DRAW[t["Body"]](c, t)
    big = STAGE_NAMES.index(stage)

    # back spikes along the spine, on the grown
    if f["back"] and (big >= 2 or t["Head"] == "Crest"):
        c.new()
        for x, y in f["back"]:
            c.tri([(x - 1.4, y + 1.2), (x - .3, y - 2.6 - big * .3), (x + 1.4, y + 1.2)], "accent")

    # the head's features
    c.new(True)
    tx, ty = f["top"]
    h = t["Head"]
    if f.get("baby"):
        ex, ey = f["ear"]
        if h == "Horns":
            for s in (-1, 1):
                c.tri([(tx + s * 1.5, ty + 2.2), (tx + s * 3.6, ty - 1.8), (tx + s * 4.2, ty + 2.8)], "horn")
        elif h == "Unihorn":
            c.tri([(tx - 1.1, ty + 1), (tx, ty - 3.2), (tx + 1.1, ty + 1)], "horn")
        elif h == "Ears":
            for x0 in (ex, 2 * tx - ex):
                s = 1 if x0 > tx else -1
                c.tri([(x0 - s * 1.5, ey + 2.5), (x0 + s * .5, ey - 3), (x0 + s * 2.5, ey + 1.5)], "body")
                c.tri([(x0 - s * .3, ey + 1.5), (x0 + s * .5, ey - 1.6), (x0 + s * 1.4, ey + 1.2)], "accent",
                      only=("body",))
        elif h == "Crest":
            for d in (-2.2, 0, 2.2):
                c.tri([(tx + d - 1.2, ty + 1.2), (tx + d - .4, ty - 2.2 + abs(d) * .4), (tx + d + 1.2, ty + 1.2)],
                      "accent")
        elif h == "Sprout":
            c.capsule(tx, ty + .8, .45, tx + .4, ty - 2, .45, "dark")
            c.ellipse(tx + 2, ty - 2.4, 1.9, 1.1, "accent"); c.ellipse(tx - 1.3, ty - 1.8, 1.3, .9, "accent")
        elif h == "Antenna":
            c.capsule(tx - .4, ty + .8, .4, tx + 1.6, ty - 3, .4, "dark")
            c.ellipse(tx + 1.7, ty - 3.3, 1, 1, "glow")
    elif h == "Horns":
        c.tri([(tx - 2.5, ty + 1.5), (tx - 6, ty - 3.5), (tx - .5, ty + .5)], "horn")
        c.tri([(tx + .5, ty + 1), (tx - 1.5, ty - 4.5), (tx + 2.2, ty + .8)], "horn")
    elif h == "Unihorn":
        c.tri([(tx + 1, ty + 1.5), (tx + 4.5, ty - 4), (tx + 3, ty + 2)], "horn")
    elif h == "Crest":
        for i in range(3):
            c.tri([(tx - 4 + i * 2, ty + 1.2), (tx - 5.5 + i * 2, ty - 3 + i * .6), (tx - 2 + i * 2, ty + 1.2)], "accent")
    elif h == "Frill":
        ex, ey = f["ear"]
        c.tri([(ex + 1, ey + 2), (ex - 4, ey - 2), (ex - 2.5, ey + 5)], "accent")
        c.tri([(ex + 1, ey + 3), (ex - 4.5, ey + 3), (ex - 1, ey + 7)], "accent")
    elif h == "Ears":
        ex, ey = f["ear"]
        c.tri([(ex - 1, ey + 2), (ex - 1.5, ey - 4.5), (ex + 2.5, ey + 1)], "body")
        c.tri([(ex - .5, ey + 1.2), (ex - 1, ey - 2.8), (ex + 1.5, ey + .8)], "accent", only=("body",))

    # gear on the head
    g = t["Gear"]
    if g == "Helmet" and not f.get("baby"):
        c.new(True)
        ex, ey = f["eye"]
        # the head above the eyes, in metal, with a brow over the eye
        hx, hy = c.at(tx, ty)
        _, top = c.at(ex, ey - .6)
        c.shape(lambda x, y: y < top and abs(x - hx) < c.r(6), "metal", only=("body", "accent", "light", "dark"))
        c.capsule(ex - 2, ey - 1.2, .7, ex + 2.5, ey - 1.2, .7, "metal")
        c.tri([(tx - 1, ty + 1), (tx + .5, ty - 3.5), (tx + 1.5, ty + 1)], "metal")

    # eyes
    c.new(True)
    ex, ey = f["eye"]
    e = t["Eyes"]
    eyes = [(ex - 3, ey), (ex + 2, ey)] if f.get("two_eyes") and f.get("baby") else [(ex, ey)]
    if f.get("two_eyes") and not f.get("baby"):
        eyes = [(ex - 3.5, ey), (ex, ey)]
    for x, y in eyes:
        x, y = c.at(x, y)
        x, y = int(x), int(y)
        if e == "Glow":
            c.put(x, y, "glow"); c.put(x + 1, y, "glow")
            continue
        if e == "Sleepy":
            c.put(x, y + 1, "ink"); c.put(x + 1, y + 1, "ink"); c.put(x - 1, y, "ink")
            continue
        c.put(x, y, "eye"); c.put(x + 1, y, "eye"); c.put(x, y + 1, "eye"); c.put(x + 1, y + 1, "pupil")
        c.put(x + 1, y, "iris" if e == "Fierce" else "pupil")
        if e in ("Sharp", "Fierce"):
            c.put(x - 1, y - 1, "ink"); c.put(x, y - 1, "ink"); c.put(x + 1, y - 1, "ink")
            c.put(x - 1, y, "ink")
        if e == "Round":
            c.put(x, y, "shine")

    # mouth
    if f["mouth"]:
        mx, my, mx1 = f["mouth"]
        (ax, ay), (bx, _) = c.at(mx, my), c.at(mx1, my)
        ax, ay, bx = int(ax), int(ay), int(bx)
        m = t["Mouth"]
        for x in range(ax, bx):
            c.put(x, ay, "ink", only=("body", "belly", "metal"))
        if m == "Fangs":
            for x in range(ax + 1, bx - 1, 2):
                c.put(x, ay + 1, "fang", only=("body", "belly"))
        elif m == "Grin":
            for x in range(ax + 1, bx - 1):
                c.put(x, ay + 1, "ink", only=("body", "belly"))
            c.put(ax + 2, ay + 1, "tongue", only=("ink",))
        elif m == "Open":
            mid = (ax + bx) // 2
            for x in range(ax, bx):
                c.put(x, ay, "body" if abs(x - mid) > 1 else "ink", only=("ink",))
            c.put(mid - 1, ay + 1, "ink", only=("body", "belly")); c.put(mid, ay + 1, "tongue", only=("body", "belly"))
            c.put(mid + 1, ay + 1, "ink", only=("body", "belly")); c.put(mid, ay + 2, "ink", only=("body", "belly"))

    # markings, on the body colour only
    mk = t["Marking"]
    rnd = random.Random(t["dna"])
    for y in range(SIZE):
        for x in range(SIZE):
            if c.get(x, y) != "body":
                continue
            if mk == "Stripes" and x % 3 == 0 and y < 23 and c.get(x, y - 1) is None or \
                    mk == "Stripes" and x % 3 == 0 and c.get(x, y - 2) is None and c.get(x, y - 1) == "body":
                c.put(x, y, "dark")
            elif mk == "Tribal" and (x + 2 * y) % 9 == 0 and y > 14:
                c.put(x, y, "accent")
            elif mk == "Spots" and rnd.random() < .06:
                c.put(x, y, "light")
    if mk == "Belly" and f["chest"]:
        cx, cy = f["chest"]
        c.ellipse(cx, cy + 1, 2.6, 3.4, "light", only=("body", "belly"))
    if mk == "Blush":
        ex, ey = f["eye"]
        for x in (ex - 5, ex + 4):
            x, y = c.at(x, ey + 2)
            c.put(int(x), int(y), "blush", only=("body", "belly")); c.put(int(x) + 1, int(y), "blush", only=("body", "belly"))

    # gear on the body
    c.new()
    if g == "Belts" and f["chest"]:
        cx, cy = f["chest"]
        c.capsule(cx - 6, cy - 3, .55, cx + 2, cy + 4, .55, "strap", only=("body", "belly", "light", "dark", "far"))
        c.capsule(cx - 6, cy + 2.5, .55, cx + 1, cy + 2.5, .55, "strap", only=("body", "belly", "light", "dark"))
        c.dot(cx - 1, cy + 1, "buckle", only=("strap",))
    elif g == "Gauntlet" and f["arm"]:
        x0, y0, x1, y1 = f["arm"]
        c.capsule(x0 + (x1 - x0) * .4, y0 + (y1 - y0) * .4, 1.6, x1, y1, 1.4, "metal")
        c.dot(x1 + .5, y1 - 1.5, "claw")
    elif g == "Armor" and f["chest"]:
        cx, cy = f["chest"]
        c.ellipse(cx - 2, cy - 2.5, 3, 2.4, "metal")
        c.tri([(cx - 4, cy - 3.5), (cx - 3, cy - 7), (cx - 1.5, cy - 4)], "metal")
    elif g == "DigiDollar coin":
        cx, cy = f["chest"] or f["side"]
        c.ellipse(cx, cy + 1, 1.8, 1.8, "coin")
        x, y = c.at(cx, cy + 1)
        c.put(int(x), int(y), "coinmark")
    elif g == "Eggshell":
        # the bottom of the egg it came out of, jagged along its top
        _, y0 = c.at(0, 25.4)
        lo, hi = c.at(9, 0)[0], c.at(23, 0)[0]
        c.shape(lambda x, y: lo <= x <= hi and y >= y0 + abs(int(x) % 4 - 2) * 1.1 and y < GROUND + 1
                and ((x - (lo + hi) / 2) / ((hi - lo) / 2)) ** 2 + ((y - GROUND - .6) / (GROUND + .6 - y0 + .8)) ** 2 <= 1,
                "shell")
    elif g == "Bow":
        ex, ey = f["ear"]
        bx, by = 2 * f["top"][0] - ex + .5, ey - .5
        c.tri([(bx, by), (bx - 2.4, by - 1.5), (bx - 2.4, by + 1.5)], "bow")
        c.tri([(bx, by), (bx + 2.4, by - 1.5), (bx + 2.4, by + 1.5)], "bow")
        c.dot(bx, by, "bowknot")

    # the biggest carry a crown of light
    if stage == "Giga":
        c.new(True)
        c.capsule(tx - 3, ty - 3.6, .6, tx + 3, ty - 3.6, .6, "coin")
        for d in (-3, 0, 3):
            c.dot(tx + d, ty - 4.8, "coin")

    return c


def paint(c: Canvas, t: dict) -> Image.Image:
    bg, base, light, dark, accent, horn = ELEMENTS[t["Element"]]
    if t["Shiny"] == "Yes":
        base, light, dark = GOLD
    colours = {"body": base, "belly": light, "light": light, "dark": dark, "far": dark, "accent": accent,
               "horn": horn, "claw": horn, "eye": EYE_WHITE, "pupil": INK, "ink": INK, "iris": "#e0303a",
               "glow": accent, "shine": EYE_WHITE, "fang": "#ffffff", "tongue": "#e0506a", "metal": METAL[0],
               "strap": LEATHER, "buckle": GOLD[0], "coin": GOLD[0], "coinmark": GOLD[2], "blush": "#ff8fb0",
               "shell": "#f4ecd8", "bow": "#ff5a8a", "bowknot": "#c2185b"}

    back = t["Background"]
    img = Image.new("RGB", (SIZE, SIZE), hexrgb(LCD[0] if back == "LCD" else bg))
    pix = img.load()
    r, g, b = pix[0, 0]
    if back == "LCD":
        for y in range(SIZE):
            for x in range(SIZE):
                if x % 2 == 0 and y % 2 == 0:
                    pix[x, y] = hexrgb(LCD[1])
        for i in range(SIZE):
            for x, y in ((i, 0), (i, SIZE - 1), (0, i), (SIZE - 1, i)):
                pix[x, y] = hexrgb(LCD[2])
    elif back == "Data":
        line = (min(255, r + 18), min(255, g + 18), min(255, b + 18))
        rnd = random.Random(t["dna"] + "bg")
        for y in range(2, SIZE, 4):
            for x in range(SIZE):
                if rnd.random() < .45:
                    pix[x, y] = line
    else:
        dot = (min(255, r + 14), min(255, g + 14), min(255, b + 14))
        for y in range(0, SIZE, 4):
            for x in range(0 if (y // 4) % 2 else 2, SIZE, 4):
                pix[x, y] = dot
    if t["Stage"] == "Giga" or t["Shiny"] == "Yes":
        ring = hexrgb(accent if t["Shiny"] == "No" else GOLD[1])
        for y in range(SIZE):
            for x in range(SIZE):
                d = ((x - 15.5) ** 2 + (y - 15.5) ** 2) ** .5
                if abs(d - 14.5) < .5 and (x + y) % 2 == 0:
                    pix[x, y] = ring
    sr, sg, sb = pix[16, 30]
    shadow = (max(0, sr - 22), max(0, sg - 22), max(0, sb - 22))
    for y in range(29, 32):
        for x in range(SIZE):
            if ((x + .5 - 15) / 10) ** 2 + ((y + .5 - 30) / 1.5) ** 2 <= 1:
                pix[x, y] = shadow

    shaded = ("body", "far", "metal", "shell")
    for y in range(SIZE):
        for x in range(SIZE):
            part = c.get(x, y)
            if part is None:
                if any(c.filled(x + dx, y + dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))):
                    pix[x, y] = hexrgb(INK)
                continue
            colour = colours[part]
            if part in shaded:
                lit, shade = (light, dark) if part == "body" else (METAL[1], METAL[2]) if part == "metal" else \
                    ("#fffaf0", "#c9bb98") if part == "shell" else (base, INK)
                above = c.get(x, y - 1)
                if above is None or (c.grp[y - 1][x] < c.grp[y][x] and above in shaded):
                    colour = lit
                elif c.get(x, y + 1) is None or c.get(x + 1, y + 1) is None:
                    colour = shade
                # a piece drawn over an earlier one gets an edge where it meets it
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    o = c.get(x + dx, y + dy)
                    if o in ("body", "far", "belly", "light") and 0 <= y + dy < SIZE and 0 <= x + dx < SIZE \
                            and c.grp[y + dy][x + dx] < c.grp[y][x] and part == "body":
                        colour = dark
            pix[x, y] = hexrgb(colour)
    if t["Shiny"] == "Yes":
        for x, y in ((4, 5), (27, 8), (6, 24), (26, 22)):
            if c.get(x, y) is None:
                pix[x, y] = hexrgb(GOLD[1])
    return img.quantize(colors=32, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)


def traits(n: int, stage: str, element: str, shiny: bool) -> dict:
    """The traits of Byteling `n`, from its seed, at the stage and element `build` handed it. Rerolled with a counter
    until unique (see build)."""
    rnd = random.Random(hashlib.sha256(f"{SEED}/{n}".encode()).digest())
    if stage == "Bit":
        return {"Stage": stage, "Element": element, "Body": rnd.choice(BABY_FORMS), "Head": rnd.choice(BABY_HEADS),
                "Eyes": rnd.choice(BABY_EYES), "Mouth": rnd.choice(BABY_MOUTHS), "Marking": rnd.choice(BABY_MARKINGS),
                "Gear": rnd.choice(BABY_GEAR), "Background": rnd.choice(BACKGROUNDS), "Shiny": "Yes" if shiny else "No"}
    body = rnd.choice(BODIES)
    t = {"Stage": stage,
         "Element": element,
         "Body": body,
         "Head": rnd.choice(HEADS),
         "Eyes": rnd.choice(EYES),
         "Mouth": rnd.choice(MOUTHS),
         "Marking": rnd.choice(MARKINGS),
         "Gear": rnd.choice(GEAR if stage != "Byte" else GEAR[:4] + GEAR[-1:]),
         "Background": rnd.choice(BACKGROUNDS),
         "Shiny": "Yes" if shiny else "No"}
    if body == "Avian":
        t["Mouth"] = "Beak"
        if t["Gear"] == "Gauntlet":
            t["Gear"] = "Armor"
    if body == "Beast" and t["Gear"] == "Gauntlet":
        t["Gear"] = "Belts"
    return t


def build(stages=None) -> list[dict]:
    """All 100: stages, elements and the gold ones handed out in shuffled orders, so each comes up exactly as often as
    it says, and a number says nothing about how rare one is."""
    stages = stages or STAGES
    seed = SEED if stages == STAGES else f"{SEED}/{stages}"
    rnd = random.Random(seed)
    order = [s for s, k in stages for _ in range(k)]
    rest = [e for e in ELEMENTS if e not in ELEMENT_COUNTS]
    left = COUNT - sum(ELEMENT_COUNTS.values())
    elements = [e for e, k in ELEMENT_COUNTS.items() for _ in range(k)] + [
        e for i, e in enumerate(rest) for _ in range(left // len(rest) + (i < left % len(rest)))]
    shiny = set(rnd.sample(range(1, COUNT + 1), SHINIES))
    rnd.shuffle(order)
    rnd.shuffle(elements)
    seen, out = set(), []
    for n, (stage, element) in enumerate(zip(order, elements), 1):
        k = 0
        while True:
            t = traits(n * 1000 + k, stage, element, n in shiny)
            key = tuple(sorted(t.items()))
            if key not in seen:
                seen.add(key)
                break
            k += 1
        t["dna"] = hashlib.sha256(repr(key).encode()).hexdigest()
        out.append(t)
    return out


TRAITS = ("Stage", "Element", "Body", "Head", "Eyes", "Mouth", "Marking", "Gear", "Background", "Shiny")


def metadata(n: int, t: dict) -> dict:
    """Byteling `n` as a HashLips item."""
    return {"name": f"{NAME} #{n}", "description": DESCRIPTION, "image": f"{n}.png", "dna": t["dna"],
            "edition": n,
            "attributes": [{"trait_type": k, "value": t[k]} for k in TRAITS],
            "compiler": "Bytelings generator (examples/bytelings/generate.py)"}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=HERE / "build")
    ap.add_argument("--sheet", type=Path, help="also write a preview sheet of all 100 here")
    ap.add_argument("--scale", type=int, default=6)
    ap.add_argument("--stage", choices=STAGE_NAMES, help="draw a hundred of this stage instead (the grown, for later)")
    args = ap.parse_args()
    (args.out / "images").mkdir(parents=True, exist_ok=True)
    (args.out / "json").mkdir(parents=True, exist_ok=True)
    every, pictures = [], []
    for n, t in enumerate(build([(args.stage, COUNT)] if args.stage else None), 1):
        img = draw(t)
        img.save(args.out / "images" / f"{n}.png", optimize=True)
        meta = metadata(n, t)
        (args.out / "json" / f"{n}.json").write_text(json.dumps(meta, indent=2) + "\n")
        every.append(meta)
        pictures.append(img)
    (args.out / "json" / "_metadata.json").write_text(json.dumps(every, indent=2) + "\n")
    if args.sheet:
        s, gap = SIZE * args.scale, 4
        sheet = Image.new("RGB", (10 * (s + gap) + gap, 10 * (s + gap) + gap), (20, 18, 24))
        for i, img in enumerate(pictures):
            sheet.paste(img.convert("RGB").resize((s, s), Image.NEAREST),
                        (gap + (i % 10) * (s + gap), gap + (i // 10) * (s + gap)))
        sheet.save(args.sheet)
    sizes = [(args.out / "images" / f"{n}.png").stat().st_size for n in range(1, COUNT + 1)]
    print(f"{COUNT} {NAME} in {args.out}: {sum(sizes):,} bytes of pictures, {max(sizes)} the largest")


if __name__ == "__main__":
    main()

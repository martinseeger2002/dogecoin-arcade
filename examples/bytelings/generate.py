#!/usr/bin/env python3
"""Bytelings: 100 pixel creatures, the arcade's own DigiByte ordinals collection.

    python3 examples/bytelings/generate.py            # writes examples/bytelings/build/
    python3 examples/bytelings/generate.py --sheet preview.png --scale 6

A thank-you to everyone who tried DigiDollar test on the arcade (2026-11-07, SadFrog). Each Byteling is drawn here
from a seed, never by hand, so anyone can run this and get the same 100 pictures, byte for byte, as the ones on the
chain. The creatures are our own: digital monsters in spirit, but no other franchise's names, characters or looks.

What comes out is a HashLips build (`build/images/<n>.png`, `build/json/<n>.json` and `build/json/_metadata.json`),
the layout the Creator tab already inscribes a whole collection from (arcade/collections.py). A picture is 32 by 32
pixels in an indexed palette, a few hundred bytes, so the fee to inscribe it stays small; pages show it scaled up with
`image-rendering: pixelated`.

Every Byteling has a stage (how big it has grown: Bit, Byte, Kilo, Mega, Giga), an element (its colours), a body, and
its head, eyes, mouth, marking and gear. No two have the same traits. Pillow is the only thing it needs.
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
DESCRIPTION = ("A Byteling: one of 100 pixel creatures, the Arcade's own collection on DigiByte, minted with "
               "DigiDollar test to thank the people who tried it.")
HERE = Path(__file__).resolve().parent

#: How many of each stage there are: most are small.
STAGES = [("Bit", 22), ("Byte", 30), ("Kilo", 26), ("Mega", 16), ("Giga", 6)]

#: Each element's colours: background, body, body light, body dark, accent, horn and claw.
ELEMENTS = {
    "Flame": ("#3a1b14", "#f0763a", "#ffb15e", "#b8402a", "#ffd84a", "#fff1c9"),
    "Tide": ("#10263f", "#3a8fe0", "#7cc4ff", "#24599e", "#7ef0e0", "#eaf6ff"),
    "Volt": ("#2b2710", "#f2d23a", "#fff27a", "#b8962a", "#5ad0ff", "#fffbe0"),
    "Leaf": ("#14301b", "#57c25a", "#9be36d", "#2f7d3f", "#f2a7d8", "#f5ffe0"),
    "Frost": ("#1a2c3a", "#bfe8f5", "#ffffff", "#7fb3cf", "#6f8cff", "#e0f7ff"),
    "Shade": ("#17122a", "#7a52c9", "#a988f0", "#4a2f86", "#ff5ab4", "#e8dcff"),
    "Bloom": ("#331a2a", "#f59ac0", "#ffd0e3", "#c25a8a", "#7ef0b0", "#ffffff"),
    "Steel": ("#1d2226", "#9aa7b2", "#d3dce3", "#5f6b75", "#ff4a4a", "#f2f5f7"),
    "Terra": ("#2a1e14", "#b0784a", "#d9a576", "#7a4f2e", "#7ed957", "#f7ead8"),
    "Glitch": ("#0e0e16", "#ff3cac", "#3cf6ff", "#7a1f86", "#c6ff3c", "#ffffff"),
}
#: How many of each element there are: Glitch is rare, the rest share what is left.
ELEMENT_COUNTS = {"Glitch": 2, "Steel": 10}
#: How many are gold all over.
SHINIES = 3
INK = "#1b1320"
EYE_WHITE = "#fdfdfd"
GOLD = ("#ffd23a", "#fff19a", "#c9921a")

BODIES = ["Blob", "Biped", "Beast", "Avian", "Floater"]
HEADS = ["Plain", "Horns", "Unihorn", "Ears", "Long ears", "Crest", "Antennae", "Fins"]
EYES = ["Round", "Round", "Big", "Big", "Fierce", "Fierce", "Visor", "Cyclops", "Sleepy"]
MOUTHS = ["Smile", "Fangs", "Open", "Flat"]
MARKINGS = ["None", "Belly", "Stripes", "Spots", "Circuit"]
GEAR = ["None", "None", "None", "DigiDollar coin", "Headphones", "Scarf", "Goggles"]


def hexrgb(h: str) -> tuple[int, int, int]:
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


class Canvas:
    """A 32x32 grid of parts: each pixel is empty or names what it is (body, belly, eye...), coloured at the end."""

    def __init__(self):
        self.px: list[list[str | None]] = [[None] * SIZE for _ in range(SIZE)]

    def get(self, x: int, y: int) -> str | None:
        return self.px[y][x] if 0 <= x < SIZE and 0 <= y < SIZE else None

    def put(self, x: int, y: int, part: str, over: bool = True):
        if 0 <= x < SIZE and 0 <= y < SIZE and (over or self.px[y][x] is None):
            self.px[y][x] = part

    def ellipse(self, cx: float, cy: float, rx: float, ry: float, part: str, over: bool = True):
        for y in range(SIZE):
            for x in range(SIZE):
                if ((x + .5 - cx) / rx) ** 2 + ((y + .5 - cy) / ry) ** 2 <= 1:
                    self.put(x, y, part, over)

    def rect(self, x0: int, y0: int, x1: int, y1: int, part: str, over: bool = True):
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                self.put(x, y, part, over)

    def tri(self, pts, part: str, over: bool = True):
        (ax, ay), (bx, by), (cx, cy) = pts

        def side(px, py, qx, qy, rx, ry):
            return (px - rx) * (qy - ry) - (qx - rx) * (py - ry)
        for y in range(SIZE):
            for x in range(SIZE):
                p = (x + .5, y + .5)
                d1, d2, d3 = side(*p, ax, ay, bx, by), side(*p, bx, by, cx, cy), side(*p, cx, cy, ax, ay)
                if not ((d1 < 0 or d2 < 0 or d3 < 0) and (d1 > 0 or d2 > 0 or d3 > 0)):
                    self.put(x, y, part, over)

    def mirror(self):
        """The right half is the left half, so every Byteling faces you square on."""
        for y in range(SIZE):
            for x in range(SIZE // 2):
                self.px[y][SIZE - 1 - x] = self.px[y][x]

    def filled(self, x: int, y: int) -> bool:
        return self.get(x, y) not in (None, "shadow")


def frame(stage: str, body: str) -> dict:
    """Where the head and the body sit for a stage: bigger stages stand taller."""
    if stage == "Bit" or body == "Blob":
        size = {"Bit": 6.5, "Byte": 8, "Kilo": 9, "Mega": 10, "Giga": 11}[stage]
        return {"head": (15.5, 29 - size, size + 1, size), "body": None, "eyes_y": 29 - size - 1, "feet": 28}
    return {
        "Byte": {"head": (15.5, 14, 7.5, 6.5), "body": (15.5, 23, 5.5, 4.5), "eyes_y": 14, "feet": 28},
        "Kilo": {"head": (15.5, 11, 6.5, 5.5), "body": (15.5, 20.5, 7, 6), "eyes_y": 11, "feet": 28},
        "Mega": {"head": (15.5, 10, 6, 5), "body": (15.5, 19.5, 8.5, 6.5), "eyes_y": 10, "feet": 28},
        "Giga": {"head": (15.5, 10, 6, 5), "body": (15.5, 19.5, 9.5, 6.5), "eyes_y": 10, "feet": 28},
    }[stage]


def draw(t: dict) -> Image.Image:
    """One Byteling from its traits."""
    c = Canvas()
    f = frame(t["Stage"], t["Body"])
    hx, hy, hrx, hry = f["head"]
    big = {"Bit": 0, "Byte": 1, "Kilo": 2, "Mega": 3, "Giga": 4}[t["Stage"]]
    body, stage = t["Body"], t["Stage"]

    # Wings and the back legs go in first, so the body covers where they join.
    if body == "Avian" and f["body"]:
        bx, by, brx, bry = f["body"]
        span = 6 + big * 1.5
        c.tri([(bx - brx + 1, by - bry), (bx - brx - span + 2, by - bry - 3 - big), (bx - 1, by + 1)], "accent")
        c.tri([(bx - brx + 1, by - bry + 2), (bx - brx - span + 1, by + 1), (bx - 2, by + 2)], "dark")
    if stage == "Giga" or (stage == "Mega" and t["Head"] == "Crest"):
        bx, by, brx, bry = f["body"] or f["head"]
        c.tri([(bx - 3, by - 2), (1, by - 11), (bx - 4, by + 3)], "accent")
        c.tri([(bx - 4, by), (2, by - 4), (bx - 5, by + 4)], "dark")
    if body == "Beast" and f["body"]:
        bx, by, brx, bry = f["body"]
        c.rect(int(bx - brx) - 1, int(by), int(bx - brx) + 1, f["feet"] - 1, "dark")

    # The body, then the head over it.
    if f["body"]:
        bx, by, brx, bry = f["body"]
        if body == "Beast":
            c.ellipse(bx, by + 1, brx + 2, bry - 1, "body")
        elif body == "Floater":
            c.ellipse(bx, by - 1, brx, bry, "body")
            for k in range(4):
                c.rect(int(bx - brx) + 2 + k, int(by + bry) - 1 + k, int(bx) - 1, int(by + bry) - 1 + k, "body")
        else:
            c.ellipse(bx, by, brx, bry, "body")
        if t["Marking"] == "Belly" or body == "Avian":
            c.ellipse(bx, by + 1, brx * .55, bry * .65, "belly")
        # legs and feet
        if body in ("Biped", "Beast", "Avian"):
            lx = int(bx - brx * .55) - (1 if body == "Beast" else 0)
            w = 1 if body == "Avian" else 1 + big // 2
            c.rect(lx - w, int(by + bry * .5), lx + w - (1 if body == "Avian" else 0), f["feet"] - 1, "body")
            c.rect(lx - w - 1, f["feet"] - 1, lx + w + (1 if body != "Avian" else 0), f["feet"], "dark")
            if body == "Avian":
                c.put(lx - w - 2, f["feet"], "horn")
                c.put(lx + w + 1, f["feet"], "horn")
        # arms
        if body in ("Biped", "Floater") or (body == "Beast" and big >= 3):
            ax = int(bx - brx) + 1
            ay = int(by - bry * .4)
            ln = 2 + big
            for k in range(ln):
                c.rect(ax - 1 - k // 2, ay + k, ax - k // 2, ay + k, "body")
            hand = (ax - 1 - (ln - 1) // 2, ay + ln)
            if big >= 2:
                c.put(hand[0] - 1, hand[1], "horn")
                c.put(hand[0], hand[1], "horn")
                c.put(hand[0] - 1, hand[1] + 1, "horn")
            else:
                c.put(hand[0], hand[1], "body")
        # a tail behind on one side shows as a tip by the leg
        if body in ("Biped", "Beast") and big >= 2:
            c.rect(int(bx + brx) - 1, int(by + 2), int(bx + brx) + 1, int(by + 3), "dark", over=False)
    else:
        # a blob's stubby feet
        fx = int(hx - hrx * .5)
        c.rect(fx - 2, f["feet"] - 1, fx, f["feet"], "dark")

    c.ellipse(hx, hy, hrx, hry, "body")
    if f["body"]:
        # a chin line where the head meets the body
        for y in range(SIZE):
            for x in range(SIZE):
                inside = ((x + .5 - hx) / hrx) ** 2 + ((y + .5 - hy) / hry) ** 2 <= 1
                below = ((x + .5 - hx) / hrx) ** 2 + ((y + 1.5 - hy) / hry) ** 2 <= 1
                if inside and not below and c.get(x, y + 1) == "body":
                    c.put(x, y, "chin")

    # the head's features
    top = hy - hry
    # what sticks up from the head stays inside the frame
    up = min(big, int(top) - 5)
    side = hx - hrx
    h = t["Head"]
    if h == "Horns":
        c.tri([(hx - 4, top + 2), (hx - 6 - up * .5, top - 3 - up), (hx - 1.5, top + 1)], "horn")
    elif h == "Unihorn":
        c.tri([(hx - 1.5, top + 1), (hx, top - 4 - up), (hx + 1.5, top + 1)], "horn")
    elif h == "Ears":
        c.tri([(side + 1, top + 4), (side + 1, top - 3), (hx - 2, top + 1)], "body")
        c.tri([(side + 2, top + 3), (side + 2, top - 1), (hx - 3, top + 1)], "accent")
    elif h == "Long ears":
        c.rect(int(side) + 1, int(top) - 5 - up // 2, int(side) + 2, int(top) + 2, "body")
        c.rect(int(side) + 1, int(top) - 4 - up // 2, int(side) + 1, int(top), "accent")
    elif h == "Crest":
        for k in range(3):
            c.tri([(hx - 1 - k * 2, top + 2), (hx - 1.5 - k * 2.2, top - 3 - up + k), (hx + .5 - k * 2, top + 2)],
                  "accent")
    elif h == "Antennae":
        ax = int(hx - 3)
        c.rect(ax, int(top) - 3, ax, int(top) + 1, "dark")
        c.rect(ax - 1, int(top) - 5, ax, int(top) - 4, "accent")
    elif h == "Fins":
        c.tri([(side + 1, hy - 1), (side - 3, hy - 4 - big * .5), (side + 1, hy + 2)], "accent")

    # eyes
    ey = int(f["eyes_y"])
    ex = int(hx - hrx * .45)
    e = t["Eyes"]
    if e == "Round":
        c.rect(ex - 1, ey - 1, ex, ey, "pupil")
    elif e == "Big":
        c.rect(ex - 2, ey - 2, ex, ey, "eye")
        c.rect(ex - 1, ey - 1, ex, ey, "pupil")
    elif e == "Fierce":
        c.rect(ex - 2, ey - 1, ex, ey, "eye")
        c.rect(ex - 1, ey - 1, ex, ey, "pupil")
        c.rect(ex - 2, ey - 2, ex - 2, ey - 2, "ink")
        c.rect(ex - 1, ey - 2, ex, ey - 2, "ink")
        c.put(ex - 2, ey - 2, "body")
        c.put(ex, ey - 3, "ink")
    elif e == "Visor":
        c.rect(int(hx - hrx) + 2, ey - 1, 15, ey, "visor")
    elif e == "Cyclops":
        c.rect(13, ey - 2, 15, ey + 1, "eye")
        c.rect(14, ey - 1, 15, ey, "pupil")
    elif e == "Sleepy":
        c.rect(ex - 2, ey, ex, ey, "pupil")

    # mouth
    my = ey + 2 + (1 if hry >= 6 else 0)
    m = "Beak" if body == "Avian" else t["Mouth"]
    if m == "Beak":
        c.rect(14, my - 1, 15, my, "horn")
        c.put(15, my + 1, "horn")
    elif m == "Smile":
        c.put(14, my, "ink")
        c.put(15, my + 1, "ink")
    elif m == "Fangs":
        c.rect(13, my, 15, my, "ink")
        c.put(13, my + 1, "horn")
    elif m == "Open":
        c.rect(14, my, 15, my + 1, "ink")
        c.put(15, my + 1, "mouth")
    elif m == "Flat":
        c.rect(14, my, 15, my, "ink")

    # markings, only on the body colour
    mk = t["Marking"]
    rnd = random.Random(t["dna"])
    for y in range(SIZE):
        for x in range(SIZE // 2):
            if c.get(x, y) != "body":
                continue
            if mk == "Stripes" and (y % 4 == 0) and x < hx - 3:
                c.put(x, y, "dark")
            elif mk == "Spots" and rnd.random() < .07:
                c.put(x, y, "accent")
            elif mk == "Circuit" and y > top + hry * 1.2 and (x + y) % 7 == 0:
                c.put(x, y, "accent")

    # gear
    g = t["Gear"]
    if g == "Headphones":
        c.rect(int(side) - 1, ey - 1, int(side) + 1, ey + 2, "gear")
        for x in range(int(side), 16):
            yy = int(top) - 1 + (1 if x < hx - 4 else 0)
            c.put(x, yy, "gear")
    elif g == "Goggles":
        c.rect(int(side), ey - 3, 15, ey - 3, "gear")
        c.rect(ex - 2, ey - 4, ex + 1, ey - 2, "gear")
        c.rect(ex - 1, ey - 3, ex, ey - 3, "visor")
    elif g == "Scarf" and f["body"]:
        sy = int(hy + hry) - 1
        c.rect(int(f["body"][0] - f["body"][2]) + 1, sy, 15, sy + 1, "gear")
        c.rect(int(f["body"][0] - f["body"][2]) + 1, sy + 2, int(f["body"][0] - f["body"][2]) + 2, sy + 4, "gear")

    if t["Stage"] == "Giga":
        c.rect(12, int(top) - 2, 15, int(top) - 1, "coin")
        c.put(12, int(top) - 3, "coin")
        c.put(14, int(top) - 3, "coin")

    c.mirror()

    # what is not mirrored: a shine in each eye, the coin's dollar sign
    if e in ("Round", "Big", "Fierce"):
        c.put(ex - 1, ey - 1, "shine")
        c.put(SIZE - 1 - ex, ey - 1, "shine")
    if e == "Cyclops":
        c.put(14, ey - 1, "shine")
    if g == "DigiDollar coin":
        cy = int(f["body"][1] if f["body"] else hy + 2) + (1 if f["body"] else 2)
        c.ellipse(15.5, cy + .5, 2.6, 2.6, "coin")
        for x, y in ((16, cy - 1), (15, cy), (16, cy), (16, cy + 1), (15, cy + 2), (16, cy + 2)):
            c.put(x, y, "coinmark")

    return paint(c, t)


def paint(c: Canvas, t: dict) -> Image.Image:
    bg, base, light, dark, accent, horn = ELEMENTS[t["Element"]]
    if t["Shiny"] == "Yes":
        base, light, dark = GOLD
    gear = {"Headphones": "#2a2d3a", "Goggles": "#5a3b22", "Scarf": accent}.get(t["Gear"], accent)
    colours = {"body": base, "belly": light, "dark": dark, "accent": accent, "horn": horn, "eye": EYE_WHITE,
               "pupil": INK, "ink": INK, "mouth": "#e0506a", "visor": accent, "shine": EYE_WHITE, "chin": dark,
               "gear": gear, "coin": GOLD[0], "coinmark": GOLD[2]}

    img = Image.new("RGB", (SIZE, SIZE), hexrgb(bg))
    pix = img.load()
    r, g, b = hexrgb(bg)
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
    shadow = (max(0, r - 12), max(0, g - 12), max(0, b - 12))
    for y in range(29, 32):
        for x in range(SIZE):
            if ((x + .5 - 16) / 9) ** 2 + ((y + .5 - 30) / 1.6) ** 2 <= 1:
                pix[x, y] = shadow

    for y in range(SIZE):
        for x in range(SIZE):
            part = c.get(x, y)
            if part is None:
                if any(c.filled(x + dx, y + dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))):
                    pix[x, y] = hexrgb(INK)
                continue
            colour = colours[part]
            if part == "body":
                if not c.filled(x, y - 1) or c.get(x, y - 1) in ("horn", "accent", "gear"):
                    colour = light
                elif not c.filled(x, y + 1) or not c.filled(x + (1 if x >= 16 else -1), y + 1):
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
    t = {"Stage": stage,
         "Element": element,
         "Body": "Blob" if stage == "Bit" else rnd.choice(BODIES[1:] if stage in ("Mega", "Giga") else BODIES),
         "Head": rnd.choice(HEADS),
         "Eyes": rnd.choice(EYES),
         "Mouth": rnd.choice(MOUTHS),
         "Marking": rnd.choice(MARKINGS),
         "Gear": rnd.choice(GEAR),
         "Shiny": "Yes" if shiny else "No"}
    if t["Body"] == "Avian":
        t["Mouth"] = "Beak"
    if t["Eyes"] in ("Visor",) and t["Gear"] == "Goggles":
        t["Gear"] = "None"
    return t


def build() -> list[dict]:
    """All 100: stages, elements and the gold ones handed out in shuffled orders, so each comes up exactly as often as
    it says, and a number says nothing about how rare one is."""
    rnd = random.Random(SEED)
    order = [s for s, k in STAGES for _ in range(k)]
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


def metadata(n: int, t: dict) -> dict:
    """Byteling `n` as a HashLips item."""
    return {"name": f"{NAME} #{n}", "description": DESCRIPTION, "image": f"{n}.png", "dna": t["dna"],
            "edition": n,
            "attributes": [{"trait_type": k, "value": t[k]} for k in
                           ("Stage", "Element", "Body", "Head", "Eyes", "Mouth", "Marking", "Gear", "Shiny")],
            "compiler": "Bytelings generator (examples/bytelings/generate.py)"}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=HERE / "build")
    ap.add_argument("--sheet", type=Path, help="also write a preview sheet of all 100 here")
    ap.add_argument("--scale", type=int, default=6)
    args = ap.parse_args()
    (args.out / "images").mkdir(parents=True, exist_ok=True)
    (args.out / "json").mkdir(parents=True, exist_ok=True)
    every, pictures = [], []
    for n, t in enumerate(build(), 1):
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

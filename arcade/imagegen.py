"""Image generation on the exchange, beside the LLM service (arcade/llm.py; 2026-10-23: "I would like to also be
able to share my image generation model"). His choices: priced per image, in any coin or Omni token the exchange trades
(no asks = free, as for the LLM); buyers make pictures from text and edit a picture they bring.

The model server is ComfyUI, over its HTTP API: a workflow graph to POST /prompt, its result read from /history and
/view. The models offered are found by asking ComfyUI what its loaders hold (`ComfyServer.models`); one family is drawn
for now, Qwen-Image (a UNETLoader model with a matching Qwen text encoder and VAE), the model these workflows were
proven on. A ComfyUI that starts on demand is woken by the operator's own `wake` command -- set only at the node machine,
never through the remote admin password, since it runs on this machine.

Credit, top-ups, grants and API keys are the LLM service's (llm.Books, llm.Seller): the same balance pays for words and
pictures, each payment kind priced `per` image.
"""
from __future__ import annotations

import base64
import io
import json
import math
import shlex
import subprocess
import threading
import time
import uuid
from typing import Any, Callable

import requests

from . import llm as llmlib

SETTING = "llm_images"
DEFAULTS = {"on": False, "url": "", "wake": "", "models": [], "max_side": 1024, "steps": 20, "at_once": 1,
            "prices": [], "name": ""}
SIDE_MOST = 1536
SIDE_LEAST = 256
MAKE_SECONDS = 600
WAKE_SECONDS = 300
MAX_UPLOAD = 12 * 1024 * 1024
#: Pictures are priced in credits (2026-10-23: "it should be credits ... an edit should cost two credits and a
#: plain picture should stay at one credit"): the operator asks a price per credit; an edit is two (the buyer's
#: picture goes up and the new one comes down).
PLAIN_CREDITS = 1
EDIT_COUNTS = 2


def config(state: Any) -> dict:
    got = state.setting(SETTING) or {}
    out = dict(DEFAULTS)
    if isinstance(got, dict):
        out.update({k: v for k, v in got.items() if k in DEFAULTS})
    return out


def is_free(cfg: dict) -> bool:
    for p in cfg.get("prices") or []:
        try:
            if llmlib.price(p.get("per", 0)) > 0:
                return False
        except llmlib.LlmError:
            return False
    return True


def clean(said: dict, kept: dict, local: bool) -> dict:
    """The admin panel's image form, checked. `wake` is a command this machine runs: only from the machine itself."""
    out = dict(kept)
    if "on" in said:
        out["on"] = bool(said["on"])
    if "url" in said:
        url = str(said.get("url") or "").strip().rstrip("/")
        if url and not url.startswith(("http://", "https://")):
            raise llmlib.LlmError("ComfyUI's address starts with http:// or https://")
        out["url"] = url
    if "wake" in said and str(said.get("wake") or "") != str(kept.get("wake") or ""):
        if not local:
            raise llmlib.LlmError("the wake command is set at the node machine only (it runs on this machine)")
        out["wake"] = str(said.get("wake") or "").strip()[:300]
    if "name" in said:
        out["name"] = str(said.get("name") or "").strip()[:60]
    if "models" in said:
        out["models"] = list(dict.fromkeys(str(m).strip()[:200] for m in said.get("models") or [] if str(m).strip()))[:10]
    if "max_side" in said:
        out["max_side"] = max(SIDE_LEAST, min(SIDE_MOST, int(said.get("max_side") or 1024))) // 16 * 16
    if "steps" in said:
        out["steps"] = max(4, min(50, int(said.get("steps") or 20)))
    if "at_once" in said:
        out["at_once"] = max(1, min(4, int(said.get("at_once") or 1)))
    if "prices" in said:
        rows, seen = [], set()
        for row in said.get("prices") or []:
            if not isinstance(row, dict):
                continue
            leg = str(row.get("leg") or "").strip()
            llmlib.leg_parts(leg)
            if leg in seen:
                raise llmlib.LlmError("each coin or token once")
            seen.add(leg)
            rows.append({"leg": leg, "label": str(row.get("label") or leg)[:80],
                         "per": format(llmlib.price(row.get("per", 0)), "f")})
        out["prices"] = rows[:40]
    if out.get("on") and not (out.get("url") and out.get("models")):
        raise llmlib.LlmError("to share pictures, name ComfyUI's address and tick at least one model")
    return out


# ------------------------------------------------------------------------------------------------ ComfyUI

class ComfyServer:
    def __init__(self, url: str, wake: str = ""):
        self.url, self.wake = url.rstrip("/"), wake
        self._waking = threading.Lock()

    def up(self) -> bool:
        try:
            return requests.get(f"{self.url}/system_stats", timeout=3).status_code < 400
        except requests.RequestException:
            return False

    def ensure_up(self) -> None:
        """Answering, or woken by the operator's command and waited for; LlmError when it stays down."""
        if self.up():
            return
        if not self.wake:
            raise llmlib.LlmError("the image model is not running right now")
        with self._waking:
            if self.up():
                return
            try:
                subprocess.run(shlex.split(self.wake), capture_output=True, timeout=WAKE_SECONDS)
            except Exception as exc:                            # noqa: BLE001
                raise llmlib.LlmError(f"the image model would not start: {exc}"[:200]) from None
            end = time.time() + WAKE_SECONDS
            while time.time() < end:
                if self.up():
                    return
                time.sleep(3)
        raise llmlib.LlmError("the image model did not start in time; try again in a minute")

    def _choices(self, node: str, field: str) -> list[str]:
        r = requests.get(f"{self.url}/object_info/{node}", timeout=15)
        r.raise_for_status()
        spec = ((r.json().get(node) or {}).get("input") or {}).get("required", {}).get(field) or []
        first = spec[0] if spec else []
        if isinstance(first, list):
            return [str(x) for x in first]
        if isinstance(spec, list) and len(spec) > 1 and isinstance(spec[1], dict):
            return [str(x) for x in spec[1].get("options") or []]
        return []

    def models(self) -> list[dict]:
        """The pictures models ComfyUI holds that these workflows can draw with: each with its text encoder and VAE."""
        unets = self._choices("UNETLoader", "unet_name")
        clips = self._choices("CLIPLoader", "clip_name")
        vaes = self._choices("VAELoader", "vae_name")
        out = []
        for u in unets:
            low = u.lower()
            if "qwen_image" not in low and "qwen-image" not in low:
                continue
            clip = next((c for c in clips if "qwen" in c.lower() and "vl" in c.lower()), None) \
                or next((c for c in clips if "qwen" in c.lower()), None)
            vae = next((v for v in vaes if "qwen_image" in v.lower() and ("2.1" in v) == ("2.1" in u)), None) \
                or next((v for v in vaes if "qwen_image" in v.lower()), None)
            if clip and vae:
                out.append({"id": u, "clip": clip, "vae": vae, "label": label_of(u), "edits": True})
        return out

    def upload(self, png: bytes) -> str:
        name = f"arcade-{uuid.uuid4().hex[:12]}.png"
        r = requests.post(f"{self.url}/upload/image", files={"image": (name, png, "image/png")},
                          data={"overwrite": "true"}, timeout=60)
        r.raise_for_status()
        return r.json().get("name") or name

    def run(self, workflow: dict) -> bytes:
        r = requests.post(f"{self.url}/prompt", json={"prompt": workflow, "client_id": uuid.uuid4().hex}, timeout=60)
        if r.status_code >= 400:
            raise llmlib.LlmError(f"the image model refused the job: {r.text[:200]}")
        pid = r.json()["prompt_id"]
        end = time.time() + MAKE_SECONDS
        while time.time() < end:
            time.sleep(1.5)
            try:
                entry = requests.get(f"{self.url}/history/{pid}", timeout=30).json().get(pid)
            except (requests.RequestException, ValueError):
                continue
            if not entry:
                continue
            status = entry.get("status") or {}
            if status.get("status_str") == "error":
                raise llmlib.LlmError("the picture failed: " + json.dumps(status.get("messages", []))[:200])
            images = [img for out in (entry.get("outputs") or {}).values() for img in out.get("images", [])]
            if not images:
                continue
            img = images[0]
            got = requests.get(f"{self.url}/view", params={"filename": img["filename"],
                                                             "subfolder": img.get("subfolder", ""),
                                                             "type": img.get("type", "output")}, timeout=120)
            got.raise_for_status()
            return got.content
        raise llmlib.LlmError("the picture took too long")


def qwen_text(m: dict, prompt: str, width: int, height: int, steps: int, seed: int) -> dict:
    """Qwen-Image text-to-image (proven in ~/qwen-tools/comfy_mcp.py): the encoder's square empty latent resized."""
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": m["id"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": m["clip"], "type": "qwen_image"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": m["vae"]}},
        "4": {"class_type": "TextEncodeQwenImage21",
              "inputs": {"clip": ["2", 0], "prompt": prompt, "negative_prompt": "", "resolution": 1024}},
        "5": {"class_type": "LatentUpscale",
              "inputs": {"samples": ["4", 2], "upscale_method": "nearest-exact", "width": width // 2,
                         "height": height // 2, "crop": "disabled"}},
        "6": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": steps, "cfg": 2.5, "sampler_name": "euler", "scheduler": "simple",
                         "denoise": 1.0, "model": ["1", 0], "positive": ["4", 0], "negative": ["4", 1],
                         "latent_image": ["5", 0]}},
        "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["3", 0]}},
        "8": {"class_type": "SaveImage", "inputs": {"filename_prefix": "arcade/llm", "images": ["7", 0]}},
    }


def qwen_edit(m: dict, image_name: str, prompt: str, steps: int, seed: int) -> dict:
    """Qwen-Image edit: the picture goes in through the encoder under the FLAT key "images.image_1" (a nested dict is
    silently dropped -- comfy_mcp.py 2026-09-25), and the latent to sample from is the encoder's third output."""
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": m["id"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": m["clip"], "type": "qwen_image"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": m["vae"]}},
        "10": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "4": {"class_type": "TextEncodeQwenImage21",
              "inputs": {"clip": ["2", 0], "vae": ["3", 0], "prompt": prompt,
                         "negative_prompt": "different subject, changed face, blurry, distorted, extra limbs",
                         "resolution": 1024, "images.image_1": ["10", 0]}},
        "7": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": steps, "cfg": 2.5, "sampler_name": "euler", "scheduler": "simple",
                         "denoise": 1.0, "model": ["1", 0], "positive": ["4", 0], "negative": ["4", 1],
                         "latent_image": ["4", 2]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "arcade/llm-edit", "images": ["8", 0]}},
    }


def label_of(name: str) -> str:
    """A picture model's name as people say it, from its file name."""
    low = name.lower()
    if "qwen_image" in low or "qwen-image" in low:
        return "Qwen-Image" + (" 2.1" if "2.1" in low else "")
    return name.rsplit(".", 1)[0]


def size_of(text: Any, most: int) -> tuple[int, int]:
    """"1024x768" (or width/height) cut to the operator's largest side, in steps of 16."""
    try:
        w, h = (int(x) for x in str(text or f"{most}x{most}").lower().split("x", 1))
    except ValueError:
        raise llmlib.LlmError("a size is WIDTHxHEIGHT, e.g. 1024x1024") from None
    if w <= 0 or h <= 0:
        raise llmlib.LlmError("a size is WIDTHxHEIGHT, e.g. 1024x1024")
    k = min(1.0, most / max(w, h))
    w, h = max(SIDE_LEAST, int(w * k)) // 16 * 16, max(SIDE_LEAST, int(h * k)) // 16 * 16
    return w, h


def png_of(data: Any) -> bytes:
    """A picture a buyer brought (base64, or a data: URL), as PNG bytes no larger than the model draws."""
    text = str(data or "")
    if text.startswith("data:"):
        text = text.split(",", 1)[-1]
    try:
        raw = base64.b64decode(text, validate=False)
    except Exception:                                           # noqa: BLE001
        raise llmlib.LlmError("that picture could not be read") from None
    if not raw or len(raw) > MAX_UPLOAD:
        raise llmlib.LlmError("bring a picture of at most 12 MB")
    try:
        from PIL import Image                                   # type: ignore
        img = Image.open(io.BytesIO(raw))
        img.load()
        if max(img.size) > SIDE_MOST:
            img.thumbnail((SIDE_MOST, SIDE_MOST))
        buf = io.BytesIO()
        img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB").save(buf, "PNG")
        return buf.getvalue()
    except ImportError:
        if raw[:8] != b"\x89PNG\r\n\x1a\n" and raw[:3] != b"\xff\xd8\xff":
            raise llmlib.LlmError("bring a PNG or JPEG picture") from None
        return raw
    except Exception:                                           # noqa: BLE001
        raise llmlib.LlmError("that is not a picture this model can read") from None


# ------------------------------------------------------------------------------------------------ the seller

class ImageSeller:
    """This arcade's pictures: its part of the offer, its load, one picture made or edited and charged per image."""

    def __init__(self, config: Callable[[], dict], books: llmlib.Books, pay_to, scale, free, server=None):
        self.config, self.books, self.pay_to, self.scale, self.free = config, books, pay_to, scale, free
        self._server = server
        self._known: dict[str, dict] = {}
        self._slots: threading.BoundedSemaphore | None = None
        self._slots_n = 0
        self._lock = threading.Lock()
        self.active = self.waiting = 0

    def server(self) -> ComfyServer:
        cfg = self.config()
        if self._server is not None:
            return self._server
        return ComfyServer(cfg.get("url") or "", cfg.get("wake") or "")

    def model(self, name: str) -> dict:
        """The loaders a shared model needs (asked of ComfyUI once and kept)."""
        if name not in self._known:
            for m in self.server().models():
                self._known[m["id"]] = m
        if name not in self._known:
            raise llmlib.LlmError("that picture model is not on the server any more")
        return self._known[name]

    def _slot(self) -> threading.BoundedSemaphore:
        n = int(self.config().get("at_once") or 1)
        with self._lock:
            if self._slots is None or n != self._slots_n:
                self._slots, self._slots_n = threading.BoundedSemaphore(n), n
            return self._slots

    def load(self) -> dict:
        now = time.time()
        q15 = self.books.recently(now - 900, kind="image")
        h1 = self.books.recently(now - 3600, kind="image")
        load = {"active": self.active, "waiting": self.waiting, "at_once": int(self.config().get("at_once") or 1),
                "last_15m": q15["n"], "last_hour": h1["n"], "failed_hour": h1["bad"],
                "avg_s": round(h1["ms"] / h1["n"] / 1000, 1) if h1["n"] else 0}
        load["word"] = llmlib.Seller.word(load)
        return load

    def part(self) -> dict | None:
        """What the offer on the book says of pictures, or None when none are shared."""
        cfg = self.config()
        if not (cfg.get("on") and cfg.get("url") and cfg.get("models")):
            return None
        free = is_free(cfg)
        prices = []
        if not free:
            for p in cfg["prices"]:
                try:
                    to = self.pay_to(p["leg"])
                except Exception:                               # noqa: BLE001
                    to = ""
                if to:
                    prices.append({**p, "to": to})
            if not prices:
                return None
        return {"models": [{"id": m, "label": (self._known.get(m) or {}).get("label") or label_of(m)} for m in cfg["models"]],
                "free": free, "prices": prices, "max_side": int(cfg.get("max_side") or 1024), "edits": True,
                "edit_counts": EDIT_COUNTS, "plain_counts": PLAIN_CREDITS,
                "name": cfg.get("name") or "", "load": self.load()}

    def make(self, buyer: str, leg: str, body: dict, via: str = "") -> dict:
        """One picture, from text or from the buyer's own picture, charged per image after it is made."""
        cfg = self.config()
        if not cfg.get("on"):
            raise llmlib.LlmError("this arcade is not sharing pictures now")
        name = str(body.get("model") or "") or (cfg.get("models") or [""])[0]
        if name not in (cfg.get("models") or []):
            raise llmlib.LlmError("this arcade does not share that picture model")
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            raise llmlib.LlmError("say what to draw")
        if len(prompt) > 4000:
            raise llmlib.LlmError("that description is too long")
        free = bool(self.free(buyer)) or is_free(cfg)
        price = {"per": "0"}
        if not free:
            price = next((p for p in cfg.get("prices") or [] if p.get("leg") == leg), None)
            if price is None:
                raise llmlib.LlmError("this arcade does not take that coin or token for its pictures"
                                      if leg != "free" else
                                      "this seller charges for its pictures now; top up and use one of its coins")
        sc = self.scale(leg) if not free else llmlib.COIN
        edit = body.get("image")
        # An edit costs two pictures (2026-10-23: "Because it's a two-way upload and download").
        times = EDIT_COUNTS if edit else PLAIN_CREDITS
        cost = 0 if free else int(math.ceil(llmlib.price(price["per"]) * sc * times))
        if not free and self.books.balance(buyer, leg) < cost:
            raise llmlib.LlmError(f"not enough credit: an edit costs {EDIT_COUNTS} credits; top up first" if edit
                                  else "not enough credit: a picture costs 1 credit; top up first")
        steps = int(cfg.get("steps") or 20)
        seed = int(body.get("seed") or 0) or uuid.uuid4().int % (2 ** 32)
        slot = self._slot()
        with self._lock:
            self.waiting += 1
        try:
            got = slot.acquire(timeout=llmlib.WAIT_FOR_SLOT * 5)
        finally:
            with self._lock:
                self.waiting -= 1
        if not got:
            raise llmlib.LlmError("the picture model is busy; try again in a few minutes")
        with self._lock:
            self.active += 1
        t0 = time.monotonic()
        ok = False
        try:
            srv = self.server()
            srv.ensure_up()
            m = self.model(name)
            if edit:
                png = srv.run(qwen_edit(m, srv.upload(png_of(edit)), prompt, steps, seed))
            else:
                w, h = size_of(body.get("size"), int(cfg.get("max_side") or 1024))
                png = srv.run(qwen_text(m, prompt, w, h, steps, seed))
            ok = True
        finally:
            slot.release()
            with self._lock:
                self.active -= 1
            ms = int((time.monotonic() - t0) * 1000)
            left = self.books.charge(buyer, leg if not free else "free", "image:" + name, 0, 0,
                                     cost if ok else 0, via, ms=ms, first_ms=ms, ok=ok, images=1 if ok else 0)
        return {"png": base64.b64encode(png).decode(), "seed": seed, "model": name, "edit": bool(edit),
                "arcade": {"cost": cost, "cost_text": llmlib.fmt(cost, sc), "left": left,
                           "left_text": llmlib.fmt(left, sc), "free": free, "leg": leg, "ms": ms}}

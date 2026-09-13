"""A temporary Cloudflare tunnel, so a phone can reach this wallet.

The interface binds loopback because it can spend (web/__main__.py). That is
right, and it also means the only way to use it from a phone has been an SSH
port-forward. This puts a door in the wall that the user opens deliberately,
can see is open, and that closes itself.

What makes it safe enough to offer
----------------------------------
A quick tunnel's hostname is random, but a URL is not a secret: it is read over
someone's shoulder, it sits in a phone's history, and Cloudflare's edge sees it.
So the tunnel is not the security. These are:

* **A key.** Every request that did not come from this machine must carry a
  token that only the QR code contains. Without it the page is a locked door,
  whatever the URL.
* **A deadline.** The tunnel stops on its own. A door left open by accident is
  the failure this exists to prevent, and the user cannot be relied on to
  remember -- nobody can.
* **No bot RPC.** `/rpc/*` is refused through the tunnel outright. It has its
  own key, meant for programs on this machine, and it can spend.

Why the token cannot be replaced by "is this request local"
-----------------------------------------------------------
It cannot be told apart by address: cloudflared runs on this machine and
connects to 127.0.0.1, so a request from a phone in another country arrives from
localhost. Measured, not assumed -- a quick tunnel to an echo server reported
`client 127.0.0.1` with `Host: <name>.trycloudflare.com`, `Cf-Ray`, `Cdn-Loop`
and `Cf-Connecting-Ip` set. The headers are what tells them apart.
"""

from __future__ import annotations

import os
import platform
import re
import secrets
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

#: cloudflared prints the address it was given on stderr, in a box.
URL_PATTERN = re.compile(rb"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")

#: How long to wait for the edge to answer before giving up.
START_TIMEOUT = 45.0

#: Offered lengths, in minutes. Not unlimited: see the deadline note above.
DURATIONS = (15, 60, 240)
DEFAULT_MINUTES = 60

#: The name the cookie is stored under. Distinct from the CSRF token: this one
#: says *who* may ask, the other says *this form came from our own page*.
COOKIE_NAME = "arcade_remote"

#: Headers Cloudflare adds on the way in. Any of them means the request did not
#: start on this machine, whatever address it seems to come from.
EDGE_HEADERS = ("cf-ray", "cdn-loop", "cf-connecting-ip", "x-forwarded-for")



class TunnelError(Exception):
    """The tunnel could not be opened, with a reason worth showing."""


def find_cloudflared(home: Path | None = None) -> str | None:
    """The cloudflared the installer put next to the node binaries, or any."""
    name = "cloudflared.exe" if os.name == "nt" else "cloudflared"
    candidates = [
        (home or Path.home() / ".dogecoinarcade") / "bin" / name,
        Path.home() / ".dogecoinarcade" / "bin" / name,
    ]
    if platform.system() == "Windows":
        candidates.append(Path.home() / "AppData/Local/DogecoinArcade/bin" / name)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which("cloudflared")


@dataclass
class Tunnel:
    """One open door: where it is, what opens it, and when it shuts."""

    url: str
    token: str
    opened: float
    closes: float
    process: subprocess.Popen | None = field(default=None, repr=False)

    @property
    def link(self) -> str:
        """The whole of the secret: the address and the key that opens it."""
        return f"{self.url}/remote/unlock?k={self.token}"

    @property
    def seconds_left(self) -> int:
        return max(0, int(self.closes - time.time()))

    @property
    def expired(self) -> bool:
        return time.time() >= self.closes

    def alive(self) -> bool:
        return (self.process is not None and self.process.poll() is None
                and not self.expired)


def open_tunnel(port: int, minutes: int = DEFAULT_MINUTES,
                home: Path | None = None,
                binary: str | None = None) -> Tunnel:
    """Start a quick tunnel to `port` and wait for its address.

    A quick tunnel needs no Cloudflare account and no configuration: the edge
    hands out a random hostname for as long as the process runs. Killing the
    process is what closes it -- there is nothing left registered anywhere.
    """
    cloudflared = binary or find_cloudflared(home)
    if not cloudflared:
        raise TunnelError(
            "cloudflared is not installed. Run the installer again from "
            "https://dogecoinarcade.com, or install it from "
            "https://github.com/cloudflare/cloudflared/releases and put it on "
            "your PATH.")

    process = subprocess.Popen(
        [cloudflared, "tunnel", "--url", f"http://127.0.0.1:{port}",
         "--no-autoupdate"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        # A pipe nobody drains fills and blocks the child. This one is read by
        # the loop below and then by a thread that throws the rest away.
        bufsize=0,
    )

    deadline = time.time() + START_TIMEOUT
    url = None
    collected = bytearray()
    while time.time() < deadline and process.poll() is None:
        line = process.stdout.readline() if process.stdout else b""
        if not line:
            continue
        collected += line
        found = URL_PATTERN.search(line)
        if found:
            url = found.group(0).decode()
            break

    if url is None:
        process.terminate()
        tail = collected.decode("utf-8", "replace").strip().splitlines()[-3:]
        raise TunnelError(
            "cloudflared did not report an address"
            + (f": {' '.join(tail)}" if tail else
               ". Check this machine can reach the internet."))

    _drain(process)
    now = time.time()
    return Tunnel(url=url, token=secrets.token_urlsafe(24), opened=now,
                  closes=now + minutes * 60, process=process)


def _drain(process: subprocess.Popen) -> None:
    """Keep reading what cloudflared says, so its pipe never fills."""
    def work():
        try:
            while process.poll() is None and process.stdout:
                if not process.stdout.readline():
                    break
        except Exception:
            pass

    threading.Thread(target=work, name="cloudflared-drain", daemon=True).start()


def close_tunnel(tunnel: Tunnel | None) -> None:
    """Shut the door. Safe to call on one already shut."""
    if tunnel is None or tunnel.process is None:
        return
    process = tunnel.process
    try:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    except Exception:
        pass
    tunnel.closes = min(tunnel.closes, time.time())


def is_remote(headers, host: str = "", tunnel_url: str | None = None) -> bool:
    """Did this request come from somewhere other than this machine?

    Two independent signals, either of which is enough:

    * a header only a proxy adds -- Cloudflare sets `Cf-Ray` and `Cdn-Loop` on
      every request that crosses its edge and overwrites what a client sends,
      so they cannot be forged away;
    * a Host equal to the tunnel's own hostname, which is exact, since we are
      the ones who were given that name.

    What it deliberately does NOT do is call an unfamiliar Host remote. Someone
    reaching their own wallet at 192.168.1.5:8420 over their own network has not
    gone through Cloudflare and must not be locked out by a feature they never
    turned on.
    """
    if any(header in headers for header in EDGE_HEADERS):
        return True
    if tunnel_url:
        name = (host or "").split(":")[0].strip().lower()
        return name != "" and name == tunnel_url.split("://")[-1].split("/")[0].lower()
    return False


def qr_svg(text: str, scale: int = 5) -> str:
    """The link as a QR code, drawn inline in the page.

    Inline SVG rather than an image file: nothing to serve, nothing to cache,
    and nothing left on disk holding the key after the tunnel is shut.
    """
    import segno

    return segno.make(text, error="m").svg_inline(
        scale=scale, border=2, dark="#111111", light="#ffffff")

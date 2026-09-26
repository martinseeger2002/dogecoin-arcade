"""Putting an arcade on its own domain with a Cloudflare tunnel: the wizard's steps.

2026-09-25: a guide in the admin panel that takes a new operator from
nothing to their arcade on their own domain. Each step is a function here, each
can be run again safely, and each says in plain words what went wrong.

1. `cloudflared` -- found, or (Linux) downloaded into ~/.local/bin, no sudo.
2. Sign in -- `cloudflared tunnel login` prints a Cloudflare link; the person opens
   it, picks their domain, and `cert.pem` lands in ~/.cloudflared.
3. A named tunnel -- `cloudflared tunnel create`.
4. The names -- `cloudflared tunnel route dns` for app.<domain> and pages.<domain>.
5. The config and a user service that runs it (and survives a reboot once the
   operator enables lingering, the one command that needs sudo).
6. The arcade's own settings: `public_hosts` and `pages_host`.
7. A check from outside: the public name answers, and answers with the public
   splash rather than the wallet.

**A machine that already runs a tunnel is left alone.** An existing
~/.cloudflared/config.yml is read (to show which names it serves), never
written; this wizard's own files are `dogecoinarcade.yml` and its own service.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

CONFIG_NAME = "dogecoinarcade.yml"
SERVICE_NAME = "dogecoinarcade-tunnel.service"
RELEASES = "https://github.com/cloudflare/cloudflared/releases/latest/download/"
LOGIN_URL = re.compile(r"https://dash\.cloudflare\.com/\S+")
QUICK_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
CREATED = re.compile(r"Created tunnel (\S+) with id ([0-9a-f-]{36})")


class TunnelError(Exception):
    """A step that did not work, said the way the person needs to hear it."""


def _explain(out: str) -> str:
    """cloudflared's own words, turned into what to do about them."""
    low = out.lower()
    if "already exists" in low and ("record" in low or "dns" in low):
        return ("that name already has a DNS record in Cloudflare. Delete or rename it "
                "in the Cloudflare dashboard (DNS), or choose another name, then try again.")
    if "tunnel with name" in low and "already exists" in low:
        return "a tunnel with that name already exists: pick another name, or use it."
    if "cert.pem" in low or "origin certificate" in low or "login" in low and "not" in low:
        return "this machine is not signed in to Cloudflare yet (step 2)."
    if "zone" in low and ("not found" in low or "no zone" in low):
        return ("that domain is not in the Cloudflare account you signed in with. Add the "
                "domain to Cloudflare first, or sign in with the account that has it.")
    return out.strip()[-400:] or "cloudflared said nothing"


class Wizard:
    def __init__(self, home: Path | None = None, run: Callable = subprocess.run,
                 popen: Callable = subprocess.Popen, which: Callable = shutil.which):
        self.home = Path(home or Path.home())
        self.cf = self.home / ".cloudflared"
        self._run, self._popen, self._which = run, popen, which
        self._login: Any = None
        self._quick: Any = None
        self._quick_url = ""

    # --- what is here --------------------------------------------------------------

    def binary(self) -> str | None:
        found = self._which("cloudflared")
        if found:
            return found
        local = self.home / ".local" / "bin" / "cloudflared"
        return str(local) if local.exists() else None

    def status(self) -> dict:
        exe = self.binary()
        version = ""
        if exe:
            try:
                version = self._run([exe, "--version"], capture_output=True, text=True,
                                    timeout=15).stdout.strip().splitlines()[0]
            except Exception:                                # noqa: BLE001
                version = "?"
        existing = self.cf / "config.yml"
        ours = self.cf / CONFIG_NAME
        return {
            "os": platform.system(), "arch": platform.machine(),
            "cloudflared": exe, "version": version,
            "signed_in": (self.cf / "cert.pem").exists(),
            "login_waiting": bool(self._login and self._login.poll() is None),
            "existing_config": str(existing) if existing.exists() else "",
            "existing_hostnames": hostnames_in(existing) if existing.exists() else [],
            "our_config": str(ours) if ours.exists() else "",
            "our_hostnames": hostnames_in(ours) if ours.exists() else [],
            "quick_url": self._quick_url if (self._quick and self._quick.poll() is None) else "",
        }

    # --- 1. cloudflared --------------------------------------------------------------

    def install(self) -> str:
        """Download cloudflared into ~/.local/bin (Linux). Elsewhere, say where to get it."""
        if self.binary():
            return self.binary()
        system, arch = platform.system(), platform.machine().lower()
        if system != "Linux":
            raise TunnelError(
                "install cloudflared from https://developers.cloudflare.com/cloudflare-one/"
                "connections/connect-networks/downloads/ (on Windows: winget install "
                "--id Cloudflare.cloudflared; on a Mac: brew install cloudflared), then "
                "check again.")
        name = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64",
                "arm64": "arm64", "armv7l": "arm"}.get(arch)
        if not name:
            raise TunnelError(f"there is no cloudflared download for a {arch} machine")
        target = self.home / ".local" / "bin" / "cloudflared"
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_suffix(".part")
        try:
            urllib.request.urlretrieve(RELEASES + f"cloudflared-linux-{name}", part)
        except Exception as exc:                             # noqa: BLE001
            raise TunnelError(f"the download did not work: {exc}") from None
        part.chmod(0o755)
        part.replace(target)
        return str(target)

    # --- 2. sign in --------------------------------------------------------------------

    def login_start(self, wait: float = 25.0) -> str:
        """Start `cloudflared tunnel login` and hand back the link it prints."""
        if (self.cf / "cert.pem").exists():
            return ""                                        # already signed in
        exe = self.binary()
        if not exe:
            raise TunnelError("cloudflared is not installed yet (step 1)")
        if self._login and self._login.poll() is None and getattr(self, "_login_url", ""):
            return self._login_url
        self._login = self._popen([exe, "tunnel", "login"], stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True)
        found: list[str] = []

        def read():
            for line in self._login.stdout:
                hit = LOGIN_URL.search(line)
                if hit and not found:
                    found.append(hit.group(0))
        threading.Thread(target=read, daemon=True).start()
        until = time.time() + wait
        while time.time() < until and not found:
            time.sleep(0.2)
        if not found:
            raise TunnelError("cloudflared did not give a sign-in link; try again")
        self._login_url = found[0]
        return found[0]

    # --- 3. the tunnel -------------------------------------------------------------------

    def _cf(self, *args: str, timeout: int = 60) -> str:
        exe = self.binary()
        if not exe:
            raise TunnelError("cloudflared is not installed yet (step 1)")
        done = self._run([exe, *args], capture_output=True, text=True, timeout=timeout)
        out = (done.stdout or "") + (done.stderr or "")
        if done.returncode != 0:
            raise TunnelError(_explain(out))
        return out

    def create(self, name: str) -> dict:
        name = re.sub(r"[^a-z0-9-]", "-", (name or "dogecoinarcade").lower()).strip("-")
        if not (self.cf / "cert.pem").exists():
            raise TunnelError("sign in to Cloudflare first (step 2)")
        out = self._cf("tunnel", "create", name)
        hit = CREATED.search(out)
        if not hit:
            raise TunnelError(_explain(out))
        return {"name": hit.group(1), "id": hit.group(2),
                "credentials": str(self.cf / f"{hit.group(2)}.json")}

    # --- 4. names -------------------------------------------------------------------------

    def route(self, tunnel: str, hostname: str) -> str:
        hostname = (hostname or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", hostname):
            raise TunnelError(f"{hostname or 'that'} is not a hostname")
        self._cf("tunnel", "route", "dns", tunnel, hostname)
        return hostname

    # --- 5. config + service ------------------------------------------------------------

    def write_config(self, tunnel_id: str, hostnames: list[str], port: int) -> str:
        if not re.fullmatch(r"[0-9a-f-]{36}", tunnel_id or ""):
            raise TunnelError("that is not a tunnel id")
        self.cf.mkdir(parents=True, exist_ok=True)
        lines = [f"tunnel: {tunnel_id}",
                 f"credentials-file: {self.cf / (tunnel_id + '.json')}", "ingress:"]
        for host in hostnames:
            lines += [f"  - hostname: {host}", f"    service: http://127.0.0.1:{int(port)}"]
        lines.append("  - service: http_status:404")
        path = self.cf / CONFIG_NAME
        path.write_text("\n".join(lines) + "\n")
        return str(path)

    def install_service(self) -> dict:
        """A user service running this wizard's config. Never the machine's own tunnel."""
        exe = self.binary()
        config = self.cf / CONFIG_NAME
        if not exe or not config.exists():
            raise TunnelError("steps 1 to 5 first: there is no config to run")
        if platform.system() != "Linux":
            raise TunnelError(f"run it with: {exe} --config {config} tunnel run "
                              "(and add that to the programs that start at login)")
        unit = self.home / ".config" / "systemd" / "user" / SERVICE_NAME
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(
            "[Unit]\nDescription=DogecoinArcade Cloudflare tunnel\nAfter=network-online.target\n\n"
            f"[Service]\nExecStart={exe} --no-autoupdate --config {config} tunnel run\n"
            "Restart=always\nRestartSec=5\n\n[Install]\nWantedBy=default.target\n")
        for args in (["daemon-reload"], ["enable", "--now", SERVICE_NAME]):
            done = self._run(["systemctl", "--user", *args], capture_output=True,
                             text=True, timeout=60)
            if done.returncode != 0:
                raise TunnelError((done.stderr or done.stdout or "systemctl failed").strip())
        return {"unit": str(unit),
                "linger": f"sudo loginctl enable-linger {os.environ.get('USER', 'you')}"}

    # --- the quick way: a temporary name ---------------------------------------------------

    def quick(self, port: int, wait: float = 30.0) -> str:
        exe = self.binary()
        if not exe:
            raise TunnelError("cloudflared is not installed yet (step 1)")
        if self._quick and self._quick.poll() is None and self._quick_url:
            return self._quick_url
        self._quick = self._popen([exe, "tunnel", "--no-autoupdate", "--url",
                                   f"http://127.0.0.1:{int(port)}"],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        found: list[str] = []

        def read():
            for line in self._quick.stdout:
                hit = QUICK_URL.search(line)
                if hit and not found:
                    found.append(hit.group(0))
        threading.Thread(target=read, daemon=True).start()
        until = time.time() + wait
        while time.time() < until and not found:
            time.sleep(0.2)
        if not found:
            raise TunnelError("the temporary tunnel did not start; try again")
        self._quick_url = found[0]
        return found[0]

    def quick_stop(self) -> None:
        if self._quick and self._quick.poll() is None:
            self._quick.terminate()
        self._quick, self._quick_url = None, ""


def hostnames_in(config: Path) -> list[str]:
    try:
        return re.findall(r"hostname:\s*([^\s#]+)", config.read_text())
    except OSError:
        return []


def verify(hostname: str, fetch: Callable = None) -> dict:
    """From outside: does the public name answer, and with the public splash?"""
    fetch = fetch or (lambda url: urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": "dogecoinarcade-wizard"}),
        timeout=20).read().decode("utf-8", "replace"))
    try:
        page = fetch(f"https://{hostname}/join")
    except Exception as exc:                                 # noqa: BLE001
        return {"ok": False, "said": f"https://{hostname} did not answer: {exc}"}
    if "seats" not in page.lower():
        return {"ok": False, "said": "it answered, but not with this arcade's public page"}
    if "Spendable" in page or "Known contacts" in page:
        return {"ok": False, "said": "it answered with the WALLET: the public names are "
                                     "not set, so step 6 first"}
    return {"ok": True, "said": f"https://{hostname} answers with the public page"}

"""Run a service for your own game on your own node: a bank, a referee, a dealer.

2026-10-12 (the operator): "If anybody else wants to make a bank for their own game,
they should be able to do it on their own node." A game's service is a program
that sits in that game's realtime rooms and answers its players -- ASHVALE's
@ashvale Bank is one. Any node's owner can run one for their own game, on their
own machine, with nothing but Python and this module: no browser, no website,
nobody else's node.

**Who it speaks as.** The node's own account: the realtime routes treat a request
from this machine that carries the node's service key as the node's operator, so
the service joins as the node's wallet address, signed by its own Core, exactly
as the operator's own viewer does. Players see that address's @tag, and every
node checks the certificate with its own Core, so nobody can run a service in
your name.

**The key.** `<home>/realtime-service.key`, 32 random bytes made by the node the
first time, readable only by the user the node runs as. It never leaves the
machine: the routes ignore it on any request that came in through the door, so
the internet cannot use it even if it leaked.

**What it can do.** What any player can: join rooms of a game, send messages of
at most 512 bytes at about five a second, hear everyone in the room. Anything
that must last -- payments, items, saves that must survive -- goes on the chain
or into the service's own storage, as with any game.

    from arcade.service import Room
    with Room("mygame", "bank") as room:
        for event in room.events():
            if event["type"] == "message":
                room.send({"t": "ack", "to": event["member"]["id"]})
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Iterator

KEY_FILE = "realtime-service.key"
HEADER = "X-Arcade-Service"


def default_home() -> Path:
    return Path(os.environ.get("ARCADE_HOME", Path.home() / ".dogecoinarcade"))


def service_key(home: Path | str, make: bool = True) -> str | None:
    """The node's service key, made on first use (0600)."""
    path = Path(home) / KEY_FILE
    try:
        return path.read_text().strip() or None
    except FileNotFoundError:
        if not make:
            return None
    key = secrets.token_hex(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:                    # made meanwhile by the node or another service
        return path.read_text().strip() or None
    with os.fdopen(fd, "w") as fh:
        fh.write(key + "\n")
    return key


class ServiceError(Exception):
    pass


class Room:
    """One room of one game, from a program on the node's own machine.

    `events()` yields what happens in the room (join, leave, message) and comes
    back by itself if the node restarts or the stream drops: it joins again and
    carries on. `send()` takes a dict (sent as JSON), a str or bytes."""

    def __init__(self, game: str, room: str, *, node: str = "http://127.0.0.1:8420",
                 home: Path | str | None = None, timeout: float = 15.0):
        import requests
        self.game, self.room, self.node = game, room, node.rstrip("/")
        key = service_key(home or default_home(), make=False)
        if not key:
            raise ServiceError(f"no {KEY_FILE} in the node's home: start the arcade once first")
        self.http = requests.Session()
        self.http.headers[HEADER] = key
        self.timeout = timeout
        self.token: str | None = None
        self.me: dict | None = None
        self.members: list[dict] = []

    def __enter__(self) -> "Room":
        self.join()
        return self

    def __exit__(self, *exc) -> None:
        self.leave()

    def _post(self, path: str, body: dict) -> dict:
        r = self.http.post(self.node + path, json=body, timeout=self.timeout)
        try:
            out = r.json()
        except ValueError:
            raise ServiceError(f"{path}: HTTP {r.status_code}") from None
        if r.status_code != 200 or not out.get("ok"):
            raise ServiceError(f"{path}: {out.get('error') or out.get('detail') or r.status_code}")
        return out

    def join(self) -> dict:
        out = self._post("/realtime/join", {"game": self.game, "room": self.room})
        self.token, self.me, self.members = out["token"], out["me"], out.get("members", [])
        return out

    def send(self, data: Any) -> None:
        if self.token is None:
            self.join()
        if isinstance(data, (dict, list)):
            data = json.dumps(data, separators=(",", ":"))
        body = {"token": self.token}
        if isinstance(data, bytes):
            import base64
            body["b64"] = base64.b64encode(data).decode()
        else:
            body["data"] = str(data)
        self._post("/realtime/send", body)

    def leave(self) -> None:
        if self.token:
            try:
                self._post("/realtime/leave", {"token": self.token})
            except Exception:                  # noqa: BLE001 -- leaving is best effort
                pass
        self.token = None

    def events(self, reconnect: bool = True) -> Iterator[dict]:
        import requests
        wait = 1.0
        while True:
            try:
                if self.token is None:
                    self.join()
                with self.http.get(f"{self.node}/realtime/stream", params={"token": self.token},
                                   stream=True, timeout=(self.timeout, None)) as r:
                    if r.status_code != 200:
                        raise ServiceError(f"stream: HTTP {r.status_code}")
                    wait = 1.0
                    for line in r.iter_lines(decode_unicode=True):
                        if line and line.startswith("data: "):
                            yield json.loads(line[6:])
                        elif line and line.startswith(": "):
                            yield {"type": "heartbeat"}   # the node's "still here": a turn for whatever waits to be sent
            except (requests.RequestException, ServiceError, ValueError):
                if not reconnect:
                    raise
            self.token = None                  # the node forgot us, or restarted: join again
            time.sleep(wait)
            wait = min(30.0, wait * 2)


def _main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m arcade.service",
                                 description="Sit in one room of your game and print what happens (a starting point for a service).")
    ap.add_argument("game")
    ap.add_argument("room")
    ap.add_argument("--node", default="http://127.0.0.1:8420")
    ap.add_argument("--echo", action="store_true", help="answer every message with {'t':'echo', ...}")
    a = ap.parse_args(argv)
    with Room(a.game, a.room, node=a.node) as room:
        print(f"in {a.game}/{a.room} as {room.me}", flush=True)
        for ev in room.events():
            if ev.get("type") == "heartbeat":
                continue
            print(json.dumps(ev), flush=True)
            if a.echo and ev.get("type") == "message" and ev["member"]["id"] != room.me["id"]:
                room.send({"t": "echo", "to": ev["member"]["id"], "data": ev.get("data")})
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


# ---------------------------------------------------------------------------- a room by certificate
#
# A program need not run on the node's machine, nor speak as the node's
# operator. The room routes let anybody be a player: a request through a node's
# public name with no cookie is a guest, and a guest that shows a CERTIFICATE --
# `cert_message(network, node, address, expires)` signed with that address's own
# key in the chain's signed-message format, checked by the node's own Core with
# `verifymessage` -- joins under that address (arcade/web/realtime.py
# realtime_join). So a bank can sit in its game's room as the address that pays,
# with nothing but that address's key: no browser, no password, no sign-in.

MESSAGE_MAGIC = "Pepecoin Signed Message:\n"

_P = 2 ** 256 - 2 ** 32 - 977
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
      0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0] and (a[1] + b[1]) % _P == 0:
        return None
    if a == b:
        lam = 3 * a[0] * a[0] * pow(2 * a[1], -1, _P)
    else:
        lam = (b[1] - a[1]) * pow(b[0] - a[0], -1, _P)
    x = (lam * lam - a[0] - b[0]) % _P
    return x, (lam * (a[0] - x) - a[1]) % _P


def _mul(k: int, point):
    out = None
    while k:
        if k & 1:
            out = _add(out, point)
        point, k = _add(point, point), k >> 1
    return out


def _compressed(point) -> bytes:
    return bytes([2 + (point[1] & 1)]) + point[0].to_bytes(32, "big")


def _varstr(b: bytes) -> bytes:
    return (bytes([len(b)]) if len(b) < 0xFD else b"\xfd" + len(b).to_bytes(2, "little")) + b


def message_hash(message: str, magic: str = MESSAGE_MAGIC) -> bytes:
    import hashlib
    body = _varstr(magic.encode()) + _varstr(message.encode())
    return hashlib.sha256(hashlib.sha256(body).digest()).digest()


def sign_message(secret: int, message: str, magic: str = MESSAGE_MAGIC) -> str:
    """What `signmessage` returns for a compressed key: base64 of the 65-byte
    compact signature, whose first byte says how to recover the key."""
    import base64
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils
    digest = message_hash(message, magic)
    der = ec.derive_private_key(secret, ec.SECP256K1()).sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
    r, s = utils.decode_dss_signature(der)
    if s > _N // 2:
        s = _N - s
    want = _compressed(_mul(secret, _G))
    e = int.from_bytes(digest, "big")
    for recid in range(4):
        x = r + (recid // 2) * _N
        if x >= _P:
            continue
        y = pow((x * x * x + 7) % _P, (_P + 1) // 4, _P)
        if (y & 1) != (recid & 1):
            y = _P - y
        q = _mul(pow(r, -1, _N), _add(_mul(s, (x, y)), _mul((-e) % _N, _G)))
        if q is not None and _compressed(q) == want:
            return base64.b64encode(bytes([27 + 4 + recid]) + r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode()
    raise ServiceError("no recovery id reproduces the key")


class CertRoom(Room):
    """One room of one game, joined through a node's PUBLIC name as `address`,
    proved by a certificate signed with `secret` (that address's key).

    Everything else is `Room`: `events()` joins again by itself when the stream
    drops, `send()` takes a dict, str or bytes. A certificate lasts about a day;
    `events()` joins afresh well before it runs out."""

    REJOIN_S = 6 * 3600

    def __init__(self, secret: int, address: str, game: str, room: str, *, node: str, timeout: float = 15.0):
        import requests
        self.game, self.room, self.node = game, room, node.rstrip("/")
        self._secret, self.address = secret, address
        self.http = requests.Session()
        self.timeout = timeout
        self.token: str | None = None
        self.me: dict | None = None
        self.members: list[dict] = []
        self.joined_at = 0.0

    def _post(self, path: str, body: dict) -> dict:
        self.http.cookies.clear()   # a cookie would make this an account's form, which wants a csrf token
        return super()._post(path, body)

    def join(self) -> dict:
        hello = self.http.get(self.node + "/realtime/hello", timeout=self.timeout).json()
        if not hello.get("online"):
            raise ServiceError(f"the room is offline: {hello.get('why')}")
        cert = hello["cert"]
        expires = int(cert["expires"])
        # Each field held to its shape and the magic pinned here, never the node's (2026-10-26, issue #5, blind
        # signing): a node can have this key sign a realtime certificate and nothing else.
        network, node = str(cert.get("network") or ""), str(cert.get("node") or "")
        if (not re.fullmatch(r"arcade-[a-z0-9]{2,16}", network) or not re.fullmatch(r"[0-9a-f]{64}", node)
                or not time.time() < expires <= time.time() + 86400 + 3600):
            raise ServiceError("the node's certificate is not one this key signs")
        words = f"arcade-realtime\n{network}\n{node}\n{self.address}\n{expires}"
        out = self._post("/realtime/join", {"game": self.game, "room": self.room, "address": self.address,
                                            "expires": expires,
                                            "sig": sign_message(self._secret, words, MESSAGE_MAGIC)})
        self.token, self.me, self.members = out["token"], out["me"], out.get("members", [])
        self.joined_at = time.time()
        if (self.me or {}).get("address") != self.address:
            raise ServiceError(f"joined as {(self.me or {}).get('address')}, not {self.address}")
        return out

    def events(self, reconnect: bool = True) -> Iterator[dict]:
        for event in super().events(reconnect):
            yield event
            if time.time() - self.joined_at > self.REJOIN_S:
                self.leave()                   # the next turn of Room.events joins with a fresh certificate

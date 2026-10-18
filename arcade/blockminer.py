"""Mining a testnet block on this machine's CPU, with work from any Core: one on this machine, or one
another arcade shares through the mesh (2026-10-18: "use my CPU ... to help sad frog mine some
testnet LTC using his shared core").

The Core is asked only for work and for the answer: `getblocktemplate`, then `submitblock`. Everything
between is done here -- the coinbase (its height, the reward to the miner, SegWit's commitment), the
merkle root, the header, and the hashing, scrypt on as many processes as asked. The template is asked
again every TEMPLATE_SECONDS, so a new block elsewhere is noticed, and so is the moment a testnet lets
its next block be mined at the lowest difficulty (no block for twice the spacing: the template's bits
drop by themselves).

When the work comes from another arcade's Core, a tenth of the reward is paid to that arcade's address
in the same coinbase (2026-10-18: "10% of the revenue should go to the operator of the shared
node"): `share_to`, read from its offer.

Testnets only: mining real coins on a CPU is futile, and a mistake there costs real money. Chains
whose blocks are merge-mined (AuxPoW) are refused as well -- a plain block on them is a different
thing, and not one this module builds.

Run on its own, against a Core's RPC or an arcade's `/rpc/work/<chain>`:

    python -m arcade.blockminer --url http://127.0.0.1:8420/rpc/work/ltctest \\
        --cookie ~/.dogecoinarcade/rpc.cookie --chain ltctest --address <yours> --workers 16
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import multiprocessing
import os
import struct
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

TEMPLATE_SECONDS = 20          # a fresh template at least this often
NONCES_PER_TASK = 20_000       # one worker's slice of the nonce space between checks
SHARE_PERCENT = 10             # what the arcade whose Core gave the work is paid of each block
TAG = b"/dogecoinarcade/"


class MiningError(Exception):
    """Mining could not start or go on."""


# --------------------------------------------------------------------------- bytes

def sha256d(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def varint(n: int) -> bytes:
    if n < 0xFD:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\xfd" + struct.pack("<H", n)
    if n <= 0xFFFFFFFF:
        return b"\xfe" + struct.pack("<I", n)
    return b"\xff" + struct.pack("<Q", n)


def push(data: bytes) -> bytes:
    if len(data) < 0x4C:
        return bytes([len(data)]) + data
    if len(data) <= 0xFF:
        return b"\x4c" + bytes([len(data)]) + data
    return b"\x4d" + struct.pack("<H", len(data)) + data


def script_number(n: int) -> bytes:
    """BIP 34: the height as a minimally encoded script number."""
    if n == 0:
        return b""
    out = bytearray()
    while n:
        out.append(n & 0xFF)
        n >>= 8
    if out[-1] & 0x80:
        out.append(0)
    return bytes(out)


def target_of(bits: int) -> int:
    exponent, mantissa = bits >> 24, bits & 0x007FFFFF
    return mantissa << (8 * (exponent - 3)) if exponent > 3 else mantissa >> (8 * (3 - exponent))


def merkle_root(txids: list[bytes]) -> bytes:
    """Of internal-order hashes."""
    level = list(txids)
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [sha256d(level[i] + level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]


# --------------------------------------------------------------------------- addresses

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _b58decode(text: str) -> bytes:
    n = 0
    for ch in text:
        n = n * 58 + _B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    raw = b"\x00" * (len(text) - len(text.lstrip("1"))) + raw
    body, check = raw[:-4], raw[-4:]
    if sha256d(body)[:4] != check:
        raise ValueError("bad checksum")
    return body


def _polymod(values: list[int]) -> int:
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (top >> i) & 1 else 0
    return chk


def _segwit_program(address: str, hrp: str) -> tuple[int, bytes] | None:
    text = address.lower()
    if not hrp or not text.startswith(hrp + "1"):
        return None
    data = [_BECH32.index(c) for c in text[len(hrp) + 1:]]
    expanded = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    const = _polymod(expanded + data)
    version = data[0]
    if const != (1 if version == 0 else 0x2BC830A3):
        raise ValueError("bad bech32 checksum")
    acc, bits, prog = 0, 0, bytearray()
    for v in data[1:-6]:
        acc, bits = (acc << 5) | v, bits + 5
        while bits >= 8:
            bits -= 8
            prog.append((acc >> bits) & 0xFF)
    return version, bytes(prog)


def output_script(address: str, params: Any) -> bytes:
    """The script that pays `address` on the chain `params` describes; refused if it is not that chain's."""
    found = _segwit_program(address, getattr(params, "bech32_hrp", "") or "")
    if found is not None:
        version, program = found
        return bytes([0x50 + version if version else 0]) + push(program)
    body = _b58decode(address)
    version, h160 = body[0], body[1:]
    if len(h160) != 20:
        raise ValueError("not a 20-byte hash")
    if version == params.pubkeyhash_version:
        return b"\x76\xa9\x14" + h160 + b"\x88\xac"
    if version == params.scripthash_version or version in getattr(params, "other_scripthash_versions", ()):
        return b"\xa9\x14" + h160 + b"\x87"
    raise ValueError(f"{address} is not a {params.label} address")


# --------------------------------------------------------------------------- the block

@dataclass
class Work:
    """One template made into a header to hash, and everything needed to send the block it finds."""
    header76: bytes
    target: int
    height: int
    prev: str
    bits: int
    body: bytes                      # varint count + every transaction + any MWEB extension
    reward: int
    paid: dict = field(default_factory=dict)

    def block(self, nonce: int) -> bytes:
        return self.header76 + struct.pack("<I", nonce) + self.body


def build(template: dict, pay_script: bytes, extranonce: int, share: tuple[bytes, int] | None = None) -> Work:
    """A block from `getblocktemplate`: our coinbase first, then the template's transactions as given.

    `share`: (script, percent) paid that share of the reward in the same coinbase. A share under the
    dust a node relays (546) is not paid at all; it would make the block invalid on some chains.
    """
    height = int(template["height"])
    reward = int(template["coinbasevalue"])
    script_sig = push(script_number(height)) + push(struct.pack("<Q", extranonce)) + push(TAG)
    outs: list[tuple[int, bytes]] = []
    paid = {}
    if share is not None:
        cut = reward * int(share[1]) // 100
        if cut >= 546:
            outs.append((cut, share[0]))
            paid["share"] = cut
    outs.insert(0, (reward - sum(v for v, _ in outs), pay_script))
    paid["miner"] = outs[0][0]
    commitment = template.get("default_witness_commitment")
    if commitment:
        outs.append((0, bytes.fromhex(commitment)))
    vin = (b"\x00" * 32 + b"\xff\xff\xff\xff" + varint(len(script_sig)) + script_sig + b"\xff\xff\xff\xff")
    vout = b"".join(struct.pack("<q", v) + varint(len(s)) + s for v, s in outs)
    body = varint(1) + vin + varint(len(outs)) + vout
    stripped = struct.pack("<i", 1) + body + b"\x00\x00\x00\x00"
    if commitment:   # the witness reserved value, which the commitment is made with
        coinbase = struct.pack("<i", 1) + b"\x00\x01" + body + b"\x01\x20" + b"\x00" * 32 + b"\x00\x00\x00\x00"
    else:
        coinbase = stripped
    txs = template.get("transactions") or []
    hashes = [sha256d(stripped)] + [bytes.fromhex(t["txid"])[::-1] for t in txs]
    root = merkle_root(hashes)
    bits = int(template["bits"], 16)
    header76 = (struct.pack("<i", int(template["version"])) + bytes.fromhex(template["previousblockhash"])[::-1]
                + root + struct.pack("<I", int(template["curtime"])) + struct.pack("<I", bits))
    blob = varint(1 + len(txs)) + coinbase + b"".join(bytes.fromhex(t["data"]) for t in txs)
    if template.get("mweb"):       # Litecoin: the block's MWEB part follows its transactions (an optional: 1, then it)
        blob += b"\x01" + bytes.fromhex(template["mweb"])
    target = int(template["target"], 16) if template.get("target") else target_of(bits)
    return Work(header76=header76, target=target, height=height, prev=template["previousblockhash"],
                bits=bits, body=blob, reward=reward, paid=paid)


# --------------------------------------------------------------------------- hashing

def pow_hash(header80: bytes, algo: str = "scrypt") -> int:
    if algo == "sha256d":
        digest = sha256d(header80)
    else:
        digest = hashlib.scrypt(header80, salt=header80, n=1024, r=1, p=1, dklen=32)
    return int.from_bytes(digest, "little")


def search(header76: bytes, target: int, start: int, count: int, algo: str = "scrypt") -> int | None:
    """The first nonce in start..start+count whose hash is at or under `target`, or None."""
    for nonce in range(start, min(start + count, 1 << 32)):
        header = header76 + struct.pack("<I", nonce)
        if pow_hash(header, algo) <= target:
            return nonce
    return None


def _search_task(args: tuple) -> tuple[int | None, int]:
    header76, target, start, count, algo = args
    return search(header76, target, start, count, algo), count


# --------------------------------------------------------------------------- the loop

@dataclass
class Progress:
    tries: int = 0
    rate: float = 0.0
    height: int = 0
    bits: str = ""
    found: list = field(default_factory=list)       # [{hash, height, paid, result}]
    started: float = field(default_factory=time.time)
    note: str = ""


def mine(call: Callable[[str, list], Any], params: Any, address: str, *,
         share_to: Callable[[], str | None] = lambda: None, workers: int = 1,
         stop: Callable[[], bool] = lambda: False, blocks: int = 0,
         progress: Progress | None = None, on_found: Callable[[dict], None] | None = None) -> Progress:
    """Mine until `stop()` says so, or `blocks` blocks are found (0: no limit).

    `call(method, params)` asks the Core. `share_to()` names the address a tenth of each block goes to
    (the arcade whose Core gives the work), or None.
    """
    if getattr(params, "real_coins", True):             # a chain that does not say is treated as real
        raise MiningError(f"{params.label} is a chain of real coins: this miner is for testnets")
    if getattr(params, "auxpow_chain_id", None) is not None:
        raise MiningError(f"{params.label} is merge-mined: its blocks are not built here")
    algo = getattr(params, "pow_algo", "scrypt") or "scrypt"
    pay = output_script(address, params)
    rules = ["segwit"] + (["mweb"] if getattr(params, "mweb", False) else [])
    p = progress or Progress()
    pool = multiprocessing.get_context("spawn").Pool(workers) if workers > 1 else None
    extranonce = int.from_bytes(os.urandom(4), "little") << 16
    try:
        while not stop() and not (blocks and len(p.found) >= blocks):
            template = call("getblocktemplate", [{"rules": rules}])
            share = None
            to = share_to()
            if to and to != address:
                try:
                    share = (output_script(to, params), SHARE_PERCENT)
                except ValueError:
                    share = None
            extranonce += 1
            work = build(template, pay, extranonce, share)
            p.height, p.bits = work.height, f"{work.bits:08x}"
            fresh_at = time.monotonic() + TEMPLATE_SECONDS
            nonce, start = None, 0
            while nonce is None and start < (1 << 32) and time.monotonic() < fresh_at and not stop():
                began = time.monotonic()
                tasks = [(work.header76, work.target, start + i * NONCES_PER_TASK, NONCES_PER_TASK, algo)
                         for i in range(max(1, workers))]
                start += NONCES_PER_TASK * len(tasks)
                results = pool.map(_search_task, tasks) if pool else [_search_task(t) for t in tasks]
                done = sum(n for _, n in results)
                p.tries += done
                p.rate = done / max(time.monotonic() - began, 1e-6)
                nonce = next((n for n, _ in results if n is not None), None)
            if nonce is None:
                continue
            raw = work.block(nonce)
            said = call("submitblock", [raw.hex()])
            header = work.header76 + struct.pack("<I", nonce)
            found = {"hash": sha256d(header)[::-1].hex(), "height": work.height, "paid": work.paid,
                     "share_to": to if share else None, "result": said or "accepted", "at": time.time()}
            p.found.append(found)
            if on_found:
                on_found(found)
    finally:
        if pool is not None:
            pool.terminate()
    return p


# --------------------------------------------------------------------------- run on its own

def http_caller(url: str, user: str, password: str, timeout: float = 120) -> Callable[[str, list], Any]:
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()

    def call(method: str, args: list) -> Any:
        body = json.dumps({"jsonrpc": "1.0", "id": 1, "method": method, "params": args}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json",
                                                              "Authorization": f"Basic {auth}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                said = json.load(r)
        except urllib.error.HTTPError as exc:
            said = json.loads(exc.read() or b"{}")
        if said.get("error"):
            raise MiningError(f"{method}: {said['error'].get('message', said['error'])}")
        return said.get("result")
    return call


def main(argv: list[str] | None = None) -> None:
    from .config import NETWORKS
    ap = argparse.ArgumentParser(description="Mine testnet blocks on this CPU with work from a Core or an arcade.")
    ap.add_argument("--url", required=True, help="a Core's RPC, or an arcade's /rpc/work/<chain>")
    ap.add_argument("--cookie", help="user:password file (an arcade's rpc.cookie, or a Core's .cookie)")
    ap.add_argument("--user", default="")
    ap.add_argument("--password", default="")
    ap.add_argument("--chain", required=True, choices=sorted(NETWORKS))
    ap.add_argument("--address", required=True, help="where the reward goes")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument("--blocks", type=int, default=0, help="stop after this many (0: until stopped)")
    args = ap.parse_args(argv)
    user, password = args.user, args.password
    if args.cookie:
        user, _, password = open(os.path.expanduser(args.cookie)).read().strip().partition(":")
    call = http_caller(args.url, user, password)
    params = NETWORKS[args.chain]

    def share_to() -> str | None:
        try:
            return (call("getworkshare", []) or {}).get("address")
        except MiningError:
            return None                                # a plain Core: nobody to share with

    def said(found: dict) -> None:
        print(f"block {found['height']} {found['hash']}: {found['result']} {found['paid']}", flush=True)

    p = Progress()
    last = [0.0]

    def stop() -> bool:
        if time.time() - last[0] > 30:
            last[0] = time.time()
            print(f"{p.tries:,} hashes, {p.rate:,.0f}/s, height {p.height}, bits {p.bits}", flush=True)
        return False

    try:
        mine(call, params, args.address, share_to=share_to, workers=args.workers, stop=stop,
             blocks=args.blocks, progress=p, on_found=said)
    except KeyboardInterrupt:
        pass
    print(f"found {len(p.found)} block(s) in {p.tries:,} hashes")


if __name__ == "__main__":
    main()

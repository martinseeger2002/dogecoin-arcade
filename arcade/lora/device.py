"""A reference T-Deck: the device side of PROTOCOL.md in Python.

The firmware does exactly this in C. Here it drives tests and lets a PC use a
stock Meshtastic radio as if it were an arcade T-Deck.
"""
from __future__ import annotations

import hashlib
import queue
import time
from dataclasses import dataclass

from . import frames as F
from .device_keys import Account, b58check, b58decode_check, hash160
from . import txcheck


class GatewayError(Exception):
    def __init__(self, code: int, text: str):
        super().__init__(f"[{code}] {text}")
        self.code, self.text = code, text


@dataclass
class Chain:
    network: str
    label: str
    tags: bool
    mainnet: bool
    p2pkh: int = 0
    p2sh: int = 0


# Version bytes per network (arcade/config.py); the device needs them to read
# addresses. The real firmware carries the same table.
VERSIONS = {"main": (56, 22), "test": (113, 196), "regtest": (111, 196),
            "doge": (30, 22), "ltc": (48, 50), "ltctest": (111, 58)}


class DeviceClient:
    def __init__(self, account: Account, send, *, timeout: float = 90.0,
                 fee_cap: int = 5_000_000):
        self.account = account
        self.send_frame = send               # send(frame bytes) -> to the gateway
        self.inbox: queue.Queue = queue.Queue()
        self.joiner = F.Joiner()
        self.timeout = timeout
        self.fee_cap = fee_cap
        self.tag = ""
        self.chains: list[Chain] = []
        self._id = 0
        self.unsolicited: list[F.Message] = []

    # -- plumbing ---------------------------------------------------------------
    def receive(self, frame: bytes):
        msg = self.joiner.add("gw", frame)
        if msg is not None:
            self.inbox.put(msg)

    def request(self, kind: int, body: bytes = b"", want: tuple = ()) -> F.Message:
        self._id = (self._id % 0xFFFF) + 1
        ident = self._id
        for frame in F.split(F.Message(kind, ident, body)):
            self.send_frame(frame)
        deadline = time.monotonic() + self.timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"no answer to {F.NAMES.get(kind)}")
            msg = self.inbox.get(timeout=left)
            if msg.id != ident:
                self.unsolicited.append(msg)
                continue
            if msg.type == F.ERR:
                r = F.Reader(msg.body)
                raise GatewayError(r.u8(), r.str())
            if want and msg.type not in want:
                raise GatewayError(F.E_BAD_REQUEST, f"unexpected {msg.name}")
            return msg

    # -- session ----------------------------------------------------------------
    def login(self, join: bool = False) -> str:
        msg = self.request(F.HELLO, self.account.login_pubkey, want=(F.CHALLENGE, F.WELCOME))
        if msg.type == F.CHALLENGE:
            r = F.Reader(msg.body)
            origin, nonce = r.str(), r.str()
            sig = self.account.sign_login(origin, nonce)
            msg = self.request(F.LOGIN, sig + bytes([1 if join else 0]), want=(F.WELCOME,))
        r = F.Reader(msg.body)
        self.tag = r.str()
        self.chains = []
        for _ in range(r.u8()):
            net, label, flags, p2pkh, p2sh = r.str(), r.str(), r.u8(), r.u8(), r.u8()
            self.chains.append(Chain(net, label, bool(flags & 1), bool(flags & 2), p2pkh, p2sh))
        return self.tag

    # -- wallet -----------------------------------------------------------------
    def balances(self) -> dict[str, tuple[int, int]]:
        r = F.Reader(self.request(F.BAL_REQ, want=(F.BAL,)).body)
        out = {}
        for _ in range(r.u8()):
            i, conf, pend = r.u8(), r.amount(), r.amount()
            out[self.chains[i].network] = (conf, pend)
        return out

    def tokens(self) -> list[tuple[str, str, str]]:
        """(chain, label, amount) of each token the gateway's operator lets T-Decks see."""
        r = F.Reader(self.request(F.TOK_REQ, want=(F.TOKS,)).body)
        return [(r.str(), r.str(), r.str()) for _ in range(r.u8())]

    def page(self, address: str, have: str = "", zlib_ok: bool = False) -> dict:
        """A .mesh page: {"status", "page", "note", "body"} (PROTOCOL.md PAGE). Needs no sign-in."""
        w = F.Writer().str(address).str(have)
        if zlib_ok:
            w.u8(F.PAGE_ACCEPTS_ZLIB)
        r = F.Reader(self.request(F.PAGE_REQ, w.done(), want=(F.PAGE,)).body)
        status, page, note, body = r.u8(), r.str(), r.str(), r.bytes()
        if status & F.PAGE_ZLIB:
            import zlib
            status, body = status & 0x7F, zlib.decompress(body)
        return {"status": status, "page": page, "note": note, "body": body.decode("utf-8", "replace")}

    def picture(self, iid: str) -> dict:
        """A picture on a .mesh page, piece by piece (PROTOCOL.md PIC): {"status", "note", "body"}. Needs no sign-in."""
        body, offset = b"", 0
        while True:
            r = F.Reader(self.request(F.PIC_REQ, F.Writer().str(iid).u16(offset).done(), want=(F.PIC,)).body)
            status, total, at, note, piece = r.u8(), r.u16(), r.u16(), r.str(), r.bytes()
            if status != F.PIC_OK:
                return {"status": status, "note": note, "body": b""}
            body += piece
            offset = at + len(piece)
            if offset >= total or not piece:
                return {"status": status, "note": note, "body": body}

    def coins(self, network: str) -> list[dict]:
        index = self._index(network)
        r = F.Reader(self.request(F.COINS_REQ, bytes([index]), want=(F.COINS,)).body)
        r.u8()
        return [{"txid": r.take(32).hex(), "vout": r.u16(), "value": r.amount(), "height": r.u32()}
                for _ in range(r.u8())]

    def _index(self, network: str) -> int:
        return next(i for i, c in enumerate(self.chains) if c.network == network)

    def _read_offer(self, msg: F.Message) -> dict:
        r = F.Reader(msg.body)
        offer = {"chain": r.u8(), "to_tag": r.str(), "to": r.str(), "fee": r.amount(),
                 "change": r.amount(), "raw": r.bytes()}
        offer["values"] = [r.amount() for _ in range(r.u8())]
        return offer

    def _sign_and_send(self, network: str, offer: dict, checked: txcheck.Checked) -> str:
        sigs = [self.account.sign_digest(network, d) for d in checked.digests]
        w = F.Writer().u8(len(sigs))
        for s in sigs:
            w.u8(len(s)).raw(s)
        w.raw(self.account.coin_pubkey(network))
        return self.request(F.SIGN, w.done(), want=(F.SENT,)).body.hex()

    def send(self, network: str, to: str, amount: int, confirm=lambda offer, checked: True) -> str:
        index = self._index(network)
        body = F.Writer().u8(index).amount(amount).str(to).done()
        offer = self._read_offer(self.request(F.SEND_REQ, body, want=(F.OFFER,)))
        chain = self.chains[index]
        pv, sv = (chain.p2pkh, chain.p2sh) if chain.p2pkh else VERSIONS[network]
        pay_script = txcheck.script_for(offer["to"], pv, sv)
        checked = txcheck.check_offer(offer["raw"], offer["values"], self.account.coin_pubkey(network),
                                      pay_script=pay_script, amount=amount, fee_cap=self.fee_cap)
        if not confirm(offer, checked):
            raise GatewayError(0, "not confirmed")
        return self._sign_and_send(network, offer, checked)

    def register(self, network: str) -> str:
        """Tell the arcade this account's address on one chain (sign-up), with the coin key's proof that it is this
        account's: the arcade files no address without it (2026-11-09; its wallet_imports.proof_message, byte for
        byte)."""
        index = self._index(network)
        chain, pub = self.chains[index], self.account.coin_pubkey(network)
        address = b58check(chain.p2pkh or VERSIONS[network][0], hash160(pub))
        text = (f"DogecoinArcade wallet import\n{self.account.login_pubkey.hex()}\n{network}\n{address}\n"
                f"{pub.hex()}").encode()
        digest = hashlib.sha256(hashlib.sha256(text).digest()).digest()
        sig = self.account.sign_digest(network, digest)[:-1]          # DER, no sighash byte
        body = bytes([index]) + pub + bytes([len(sig)]) + sig
        return F.Reader(self.request(F.ADDR, body, want=(F.ADDR_OK,)).body).str()

    # -- mail -------------------------------------------------------------------
    def take_mail(self, wait: float = 0.0) -> list[dict]:
        """Mail the gateway pushed: keep it, then send the signed receipt.

        The receipt goes only after the whole letter is here, so the gateway's
        outbox keeps it until this device really has it.
        """
        got, deadline = [], time.monotonic() + wait
        while True:
            pending = [m for m in self.unsolicited if m.type in (F.MAIL_IN, F.MAIL_TOO_LONG)]
            self.unsolicited = [m for m in self.unsolicited if m not in pending]
            try:
                while True:
                    m = self.inbox.get_nowait()
                    (pending if m.type in (F.MAIL_IN, F.MAIL_TOO_LONG) else self.unsolicited).append(m)
            except queue.Empty:
                pass
            for m in pending:
                r = F.Reader(m.body)
                txid, when, sender = r.take(32), r.u32(), r.str()
                item = {"txid": txid.hex(), "when": when, "from": sender, "too_long": m.type == F.MAIL_TOO_LONG}
                if m.type == F.MAIL_IN:
                    item["sealed"] = r.bytes()
                else:
                    item["size"] = r.u16()
                got.append(item)
                sig = self.account.login.sign(b"arcade-lora receipt\n" + txid.hex().encode()).signature
                for frame in F.split(F.Message(F.MAIL_ACK, 0, txid + sig)):
                    self.send_frame(frame)
            if got or time.monotonic() >= deadline:
                return got
            time.sleep(0.2)

    # -- contacts ---------------------------------------------------------------
    def who(self, tag: str) -> dict:
        r = F.Reader(self.request(F.WHO_REQ, F.Writer().str(tag).done(), want=(F.WHO,)).body)
        return {"tag": r.str(), "address": r.str(), "key": r.take(32), "name": r.str()}

    def watch(self, addresses: list[str]) -> int:
        w = F.Writer().u8(len(addresses))
        for a in addresses:
            w.raw(b58decode_check(a)[1])
        return F.Reader(self.request(F.WATCH, w.done(), want=(F.WATCH_OK,)).body).u8()

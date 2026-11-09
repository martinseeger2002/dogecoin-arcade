"""The gateway: arcade frames in from the radio, arcade API calls out.

One `Device` per Meshtastic node that has said HELLO. Everything here is
transport-free: `Gateway.receive(node, frame)` takes one frame and returns
nothing; replies go out through the `send(node, frame)` callable it was given,
so the same code runs behind a USB radio, a Bluetooth link, or a test.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from . import frames as F
from .arcade_api import ArcadeError, ArcadeSession
from .device_keys import b58check, b58decode_check, hash160
from .store import Store

import nacl.exceptions
import nacl.signing

log = logging.getLogger("arcade_lora.gateway")

MAIL_MAX = 645                  # sealed bytes; 3 frames of MAIL_IN with its header
RETRY = 180.0                   # seconds between tries while the T-Deck is online
ONLINE_WINDOW = 1200.0          # heard within this long = online; after it, no more tries
RECEIPT = b"arcade-lora receipt\n"
REPLAY_GAP = 10.0               # an answer is replayed at most this often
RESEND_KEEP = 120.0             # seconds the parts of a multi-part answer are kept for RESEND
REPLAY_KEEP = 300.0             # seconds an answer is kept for a T-Deck that asks again
NOTICE_WAIT = 1800.0            # how long a contact's letter may wait for its notice coin to be indexed
NOTICE_VALUE = 1_000_000        # the 0.01 coin a message pays its recipient


def spendable(row: dict) -> int:
    """The web wallet's big number (app.py _spendable): what can be spent now.

    The raw `balance` still counts coins the arcade holds back (inscribed,
    listed, already leaving), so the T-Deck shows this instead.
    """
    if "spendable" in row:
        return int(row["spendable"] or 0)
    return max(0, int(row.get("balance") or 0) - int(row.get("leaving") or 0) - int(row.get("held") or 0)
               - int(row.get("inscribed") or 0) + int(row.get("incoming") or 0))




def shown_units(units: int, decimals: int) -> str:
    """Whole token units as the wallet writes them: 1234500 at 4 decimals -> "123.45"."""
    if decimals <= 0:
        return str(units)
    whole, frac = divmod(units, 10 ** decimals)
    frac_s = f"{frac:0{decimals}d}".rstrip("0")
    return f"{whole}.{frac_s}" if frac_s else str(whole)


@dataclass
class Device:
    node: int
    pubkey: bytes
    arcade: ArcadeSession
    nonce: str = ""
    tag: str = ""
    chains: list[dict] = field(default_factory=list)     # as WELCOME listed them
    watch: set[bytes] = field(default_factory=set)        # hash160s of contacts
    offer: dict | None = None                             # the one open offer
    mail_cursor: int | None = None
    online: bool = False                                  # heard lately; mail only goes to an online T-Deck
    scan_cursor: int | None = None                        # how far this gateway has read (in memory)
    maybe: dict = field(default_factory=dict)             # txid -> (candidate, first seen): a contact's
                                                          # letter whose notice coin is not indexed yet
    last_heard: float = float("-inf")
    flush_at: float | None = None                         # when its waiting mail goes (after its replies)


class Gateway:
    def __init__(self, arcade_url: str, send, *, name: str = "arcade gateway",
                 networks: list[str] | None = None, assets: list[dict] | None = None, mail_max: int = MAIL_MAX,
                 session_factory=ArcadeSession, clock=time.monotonic, store: Store | None = None,
                 flush_delay: float = 6.0):
        self.arcade_url = arcade_url
        self.send_frame = send
        self.name = name
        self.networks = networks            # operator's choice; None = every chain the account has
        # Tokens the operator lets T-Decks see (Admin -> Meshtastic), [{"key": "<net>-token20-<tick>" |
        # "<net>-token-<property id>", "label"}]: their balances answer TOK_REQ.
        self.assets = [a for a in (assets or []) if isinstance(a, dict) and "-token" in str(a.get("key") or "")]
        self.mail_max = mail_max
        self.session_factory = session_factory
        self.clock = clock
        self.devices: dict[int, Device] = {}
        self.joiner = F.Joiner(clock=clock)
        self.lock = threading.RLock()
        self._next_id = 1
        self.answers: dict[tuple, tuple[float, list[bytes]]] = {}   # (node, type, id, body) -> (when, frames)
        self.sent_parts: dict[tuple, tuple[float, list[bytes]]] = {}  # (node, type, id) -> (when, frames): RESEND
        self.replayed: dict[tuple, float] = {}                        # answers key -> when last replayed
        self._capture: list[bytes] | None = None
        self.store = store or Store(":memory:")
        self.flush_delay = flush_delay      # sign-in answers first, then the mail
        for row in self.store.devices():
            arcade = self.session_factory(arcade_url)
            if row["cookie"]:
                arcade.restore(row["cookie"])
            self.devices[row["node"]] = Device(row["node"], row["pubkey"], arcade, tag=row["tag"],
                                               chains=row["chains"], watch=row["watch"],
                                               mail_cursor=row["mail_cursor"])

    def save(self, dev: Device):
        self.store.save_device(dev.node, dev.pubkey, dev.tag, dev.arcade.cookie() or "", dev.chains,
                               dev.watch, dev.mail_cursor)

    # -- out ------------------------------------------------------------------
    def reply(self, node: int, kind: int, ident: int, body: bytes = b""):
        frames = F.split(F.Message(kind, ident, body))
        if len(frames) > 1:
            # kept for RESEND: the T-Deck asks for the parts that did not come, by number (2026-10-26)
            now = self.clock()
            for k in [k for k, (t, _) in self.sent_parts.items() if now - t > RESEND_KEEP]:
                del self.sent_parts[k]
            self.sent_parts[(node, kind, ident)] = (now, frames)
        for frame in frames:
            if self._capture is not None:
                self._capture.append(frame)
            self.send_frame(node, frame)

    def on_resend(self, node, msg):
        """RESEND (PROTOCOL.md): the parts of one of our answers that never came, sent again from what we kept."""
        r = F.Reader(msg.body)
        kind, missing = r.u8(), r.u16()
        kept = self.sent_parts.get((node, kind, msg.id))
        if kept is None:
            return                                       # too old, or never ours: the T-Deck will ask the request again
        frames = kept[1]
        for i, frame in enumerate(frames):
            if missing & (1 << i):
                self.send_frame(node, frame)

    def error(self, node: int, ident: int, code: int, text: str):
        self.reply(node, F.ERR, ident, F.Writer().u8(code).str(text[:200]).done())

    def new_id(self) -> int:
        self._next_id = (self._next_id % 0xFFFF) + 1
        return self._next_id

    def beacon_body(self) -> bytes:
        host = self.arcade_url.split("://", 1)[-1]
        return F.Writer().str(self.name).str(host).u8(1).done()

    def beacon(self) -> bytes:
        """The GW_BEACON frame, for the link to broadcast now and then."""
        return F.split(F.Message(F.GW_BEACON, 0, self.beacon_body()))[0]

    # -- presence -------------------------------------------------------------
    def heard(self, node: int):
        """Any packet at all from `node` (an arcade frame, its node info, a
        position): the T-Deck is on, so whatever waits for it goes now."""
        with self.lock:
            dev = self.devices.get(node)
            if dev is None:
                return
            dev.last_heard = self.clock()
            if not dev.online:
                dev.online = True
                log.info("@%s is back on the mesh; %d letters wait", dev.tag, len(self.store.pending(node)))
                self.flush_soon(dev)

    def flush_soon(self, dev: Device):
        if self.flush_delay <= 0:
            return self.flush(dev, force=True)
        dev.flush_at = self.clock() + self.flush_delay

    def tick(self):
        """Every second from the main loop: mail whose moment has come."""
        with self.lock:
            for dev in list(self.devices.values()):
                if dev.flush_at is not None and self.clock() >= dev.flush_at:
                    dev.flush_at = None
                    self.flush(dev, force=True)

    def flush(self, dev: Device, force: bool = False):
        if not dev.online:
            return
        now = time.time()
        for item in self.store.pending(dev.node):
            if force or item["last_try"] is None or now - item["last_try"] >= RETRY:
                self.store.tried(dev.node, item["txid"])
                self.reply(dev.node, item["type"], self.new_id(), item["body"])

    # -- in -------------------------------------------------------------------
    def receive(self, node: int, frame: bytes):
        try:
            msg = self.joiner.add(node, frame)
        except F.FrameError as exc:
            log.debug("dropped frame from %08x: %s", node, exc)
            return
        if msg is None:
            return
        with self.lock:
            handler = self.HANDLERS.get(msg.type)
            if handler is None:
                return
            # A T-Deck that lost an answer asks again with the same id: it gets
            # the same answer, and the work (a send, a signature) is never done twice.
            key = (node, msg.type, msg.id, msg.body)
            now = self.clock()
            for k in [k for k, (t, _) in self.answers.items() if now - t > REPLAY_KEEP]:
                del self.answers[k]
                self.replayed.pop(k, None)
            if key in self.answers:
                # not again while the last copy may still be queued for the air (2026-10-26: every repeat of a
                # request sent its whole answer again, and the replays drowned the radio)
                last = self.replayed.get(key, self.answers[key][0])
                if now - last < REPLAY_GAP:
                    log.info("%08x asked again (%s %d): answered %.0f s ago, not yet", node, msg.name, msg.id, now - last)
                    return
                self.replayed[key] = now
                log.info("%08x asked again (%s %d): same answer", node, msg.name, msg.id)
                for frame in self.answers[key][1]:
                    self.send_frame(node, frame)
                return
            self._capture = []
            try:
                handler(self, node, msg)
            except ArcadeError as exc:
                self.error(node, msg.id, F.E_ARCADE, str(exc))
            except (F.FrameError, ValueError, KeyError, IndexError) as exc:
                self.error(node, msg.id, F.E_BAD_REQUEST, str(exc))
            finally:
                frames, self._capture = self._capture, None
            if frames and msg.type not in (F.PING, F.HELLO):
                self.answers[key] = (now, frames)

    def _device(self, node: int, msg: F.Message) -> Device | None:
        dev = self.devices.get(node)
        if dev is None or not dev.arcade.signed_in or dev.nonce:
            self.error(node, msg.id, F.E_NOT_LOGGED_IN, "say HELLO first")
            return None
        return dev

    def _chain(self, dev: Device, index: int) -> dict:
        if not 0 <= index < len(dev.chains):
            raise ValueError("no such chain")
        return dev.chains[index]

    # -- session --------------------------------------------------------------
    def on_hello(self, node, msg):
        pubkey = F.Reader(msg.body).take(32)
        old = self.devices.get(node)
        if old is not None and old.pubkey == pubkey and old.arcade.signed_in and not old.nonce:
            return self._welcome(old, msg.id)
        if old is not None:
            old.arcade.close()
        dev = Device(node, pubkey, self.session_factory(self.arcade_url), online=True, last_heard=self.clock())
        if old is not None and old.pubkey == pubkey:       # same account, session lapsed: keep its mail place
            dev.watch, dev.mail_cursor = old.watch, old.mail_cursor
        said = dev.arcade.challenge()
        dev.nonce = said["nonce"]
        self.devices[node] = dev
        self.reply(node, F.CHALLENGE, msg.id, F.Writer().str(said["origin"]).str(said["nonce"]).done())

    def on_login(self, node, msg):
        dev = self.devices.get(node)
        if dev is None or not dev.nonce:
            return self.error(node, msg.id, F.E_NOT_LOGGED_IN, "say HELLO first")
        r = F.Reader(msg.body)
        sig, join = r.take(64), bool(r.u8())
        dev.arcade.login(dev.pubkey, dev.nonce, sig, join)
        dev.nonce = ""
        self._welcome(dev, msg.id)
        self.flush_soon(dev)

    def _welcome(self, dev: Device, ident: int):
        acct = dev.arcade.account()
        dev.tag = acct.get("tag") or ""          # "" = signed in, no @tag claimed yet
        rows = [c for c in acct.get("chains", [])
                if self.networks is None or c["network"] in self.networks or c.get("tags")]
        dev.chains = rows
        acct_mail = acct.get("mail_from")
        if dev.mail_cursor is None and acct_mail is not None:
            dev.mail_cursor = int(acct_mail)
        w = F.Writer().str(dev.tag).u8(len(rows))
        for c in rows:
            w.str(c["network"]).str(c["label"]).u8((1 if c.get("tags") else 0) | (2 if c.get("mainnet") else 0))
            w.u8(int(c.get("version") or 0)).u8(int(c.get("p2sh") or 0))
        self.save(dev)
        self.reply(dev.node, F.WELCOME, ident, w.done())

    def on_ping(self, node, msg):
        # A T-Deck looking for a gateway broadcasts PING; every gateway that
        # hears it answers directly with what it would put in a beacon.
        self.reply(node, F.PONG, msg.id, self.beacon_body())

    # -- wallet ---------------------------------------------------------------
    def on_bal_req(self, node, msg):
        dev = self._device(node, msg)
        if dev is None:
            return
        rows = {c["network"]: c for c in dev.arcade.account().get("chains", [])}
        w = F.Writer().u8(len(dev.chains))
        for i, c in enumerate(dev.chains):
            row = rows.get(c["network"], {})
            w.u8(i).amount(spendable(row)).amount(int(row.get("arriving") or 0))
        self.reply(node, F.BAL, msg.id, w.done())

    #: Tokens in one answer at most (each row ~40 bytes: 24 fill five frames).
    TOKENS_MAX = 24

    def on_tok_req(self, node, msg):
        dev = self._device(node, msg)
        if dev is None:
            return
        rows: list[tuple[str, str, str]] = []
        t20: dict[str, list[dict]] = {}
        held: list[dict] | None = None
        for asset in self.assets[:self.TOKENS_MAX]:
            net, kind, ref = (str(asset["key"]).split("-", 2) + ["", ""])[:3]
            label = str(asset.get("label") or ref)
            amount = "0"
            try:
                if kind == "token20":
                    if net not in t20:
                        t20[net] = dev.arcade.token20(net)
                    b = next((b for b in t20[net] if str(b.get("tick") or "").lower() == ref.lower()), None)
                    if b is not None:
                        units = int(b.get("units") or 0) - int(b.get("pending_units") or 0)
                        amount = shown_units(max(units, 0), int(b.get("decimals") or 0))
                elif kind == "token":
                    if held is None:
                        held = dev.arcade.tokens()
                    for chain in held:
                        if chain.get("network") != net:
                            continue
                        for t in chain.get("tokens") or []:
                            if str(t.get("property_id")) == ref:
                                amount = str(t.get("display") or t.get("balance") or "0")
                else:
                    continue
            except Exception as exc:                     # noqa: BLE001 -- that chain cannot say just now: "?"
                log.info("token %s for @%s: %s", asset["key"], dev.tag, exc)
                amount = "?"
            rows.append((net, label[:40], amount[:24]))
        w = F.Writer().u8(len(rows))
        for net, label, amount in rows:
            w.str(net).str(label).str(amount)
        self.reply(node, F.TOKS, msg.id, w.done())

    # -- the .mesh web (PROTOCOL.md PAGE_REQ / PAGE) --------------------------------------------------------------
    def _public(self):
        """A session with no account in it: pages are public, so anybody's T-Deck may ask, signed in or not."""
        if getattr(self, "_public_session", None) is None:
            self._public_session = self.session_factory(self.arcade_url)
        return self._public_session

    def on_page_req(self, node, msg):
        r = F.Reader(msg.body)
        address, have = r.str()[:200], r.str()[:80]
        flags = r.u8() if r.at < len(r.body) else 0      # a T-Deck from before the flags sends none
        try:
            code, said = self._public().mesh_page(address, have)
        except Exception as exc:                         # noqa: BLE001 -- the arcade did not answer
            log.info("mesh page %s: %s", address, exc)
            code, said = 503, {"detail": "The arcade did not answer"}
        page = str(said.get("page") or "")[:80]
        if code == 200 and said.get("unchanged"):
            status, note, body = F.PAGE_UNCHANGED, str(said.get("title") or ""), b""
        elif code == 200:
            status, note, body = F.PAGE_OK, str(said.get("title") or ""), str(said.get("body") or "").encode()
        else:
            status, note, body = F.PAGE_MISSING, str(said.get("detail") or f"HTTP {code}"), b""
        note = note.encode()[:80].decode("utf-8", "ignore")
        if body and flags & F.PAGE_ACCEPTS_ZLIB:
            import zlib
            packed = zlib.compress(body, 9)
            if len(packed) < len(body):                  # MeshText packs to about half: half the frames on air
                body, status = packed, status | F.PAGE_ZLIB
        head = F.Writer().u8(status).str(page).str(note).done()
        room = F.MAX_PARTS * F.MAX_BODY - len(head) - 2
        if len(body) > room:                             # a site is capped to fit; never more than one message
            body = body[:room]
        self.reply(node, F.PAGE, msg.id, head + len(body).to_bytes(2, "big") + body)

    def on_pic_req(self, node, msg):
        """A piece of a picture on a .mesh page (2026-10-26: "like the old school Internet used to work where the
        image would load from the top to the bottom"): the mesh picture from `offset`, as much as one message holds.
        Its bands are made to fill about one part each, so the T-Deck draws each part as it comes in."""
        r = F.Reader(msg.body)
        iid, offset = r.str()[:80], r.u16()
        pictures = self.__dict__.setdefault("_pictures", {})
        got = pictures.get(iid)
        if got is None:
            try:
                code, got = self._public().mesh_picture(iid)
            except Exception as exc:                     # noqa: BLE001 -- the arcade did not answer
                log.info("mesh picture %s: %s", iid, exc)
                code, got = 503, {"detail": "The arcade did not answer"}
            if code == 200:
                if len(pictures) > 50:
                    pictures.clear()
                pictures[iid] = got
        if not isinstance(got, (bytes, bytearray)):
            note = str((got or {}).get("detail") or "no such picture").encode()[:80].decode("utf-8", "ignore")
            return self.reply(node, F.PIC, msg.id, F.Writer().u8(F.PIC_MISSING).u16(0).u16(offset).str(note).done()
                              + (0).to_bytes(2, "big"))
        head = F.Writer().u8(F.PIC_OK).u16(len(got)).u16(offset).str("").done()
        room = F.MAX_PARTS * F.MAX_BODY - len(head) - 2
        piece = bytes(got[offset:offset + room])
        self.reply(node, F.PIC, msg.id, head + len(piece).to_bytes(2, "big") + piece)

    def on_coins_req(self, node, msg):
        dev = self._device(node, msg)
        if dev is None:
            return
        index = F.Reader(msg.body).u8()
        coins = dev.arcade.coins(self._chain(dev, index)["network"])
        coins.sort(key=lambda c: -int(c["value"]))
        coins = coins[:8]                                # the biggest few: two parts on the air, plenty for a letter
        w = F.Writer().u8(index).u8(len(coins))
        for c in coins:
            w.raw(bytes.fromhex(c["txid"])).u16(int(c["vout"])).amount(int(c["value"])).u32(int(c.get("height") or 0))
        self.reply(node, F.COINS, msg.id, w.done())

    def on_send_req(self, node, msg):
        dev = self._device(node, msg)
        if dev is None:
            return
        r = F.Reader(msg.body)
        index, amount, to = r.u8(), r.amount(), r.str()
        chain = self._chain(dev, index)
        said = dev.arcade.send(chain["network"], to, amount)
        self._offer(dev, msg.id, index, said, "send")

    def _offer(self, dev: Device, ident: int, index: int, said: dict, kind: str):
        raw = bytes.fromhex(said["raw"])
        values = [int(i["value"]) for i in said["inputs"]]
        if any(i.get("segwit") or i.get("script") for i in said["inputs"]):
            raise ArcadeError("this offer spends coins the T-Deck cannot sign yet")
        dev.offer = {"id": said["offer"], "kind": kind, "chain": index, "at": self.clock()}
        w = (F.Writer().u8(index).str(said.get("to_tag") or "").str(said.get("to") or "")
             .amount(int(said.get("fee") or 0)).amount(int(said.get("change") or 0))
             .bytes(raw).u8(len(values)))
        for v in values:
            w.amount(v)
        self.reply(dev.node, F.OFFER, ident, w.done())

    def on_sign(self, node, msg):
        dev = self._device(node, msg)
        if dev is None:
            return
        if dev.offer is None:
            return self.error(node, msg.id, F.E_EXPIRED, "no offer is open")
        r = F.Reader(msg.body)
        sigs = [r.take(r.u8()) for _ in range(r.u8())]
        pubkey = r.take(33)
        offer, dev.offer = dev.offer, None
        said = dev.arcade.sign(offer["id"], sigs, pubkey)
        self.reply(node, F.SENT, msg.id, bytes.fromhex(said["txid"]))

    def on_addr(self, node, msg):
        dev = self._device(node, msg)
        if dev is None:
            return
        r = F.Reader(msg.body)
        chain, pubkey = self._chain(dev, r.u8()), r.take(33)
        # Then the coin key's signature that the address is this account's (2026-11-09): the arcade files no
        # address without it. A device from before sends none, and the arcade says why it refused.
        signature = b"" if r.end() else r.take(r.u8())
        address = b58check(int(chain["version"]), hash160(pubkey))
        # Never overwrite what the arcade already watches for this account: a
        # T-Deck that derived a different address would make the account's
        # real coins vanish from its own view. Same address: nothing to do.
        known = next((c.get("address") for c in dev.arcade.account().get("chains", [])
                      if c["network"] == chain["network"]), None)
        if known and known != address:
            log.warning("@%s: T-Deck address %s for %s differs from the arcade's %s; not registered",
                        dev.tag, address, chain["network"], known)
            return self.error(node, msg.id, F.E_ARCADE,
                              f"This T-Deck's {chain['label']} address is not the one the arcade has; nothing changed")
        if not known:
            dev.arcade.register(chain["network"], address, pubkey, signature)
        self.reply(node, F.ADDR_OK, msg.id, F.Writer().str(address).done())

    def on_broadcast(self, node, msg):
        """A transaction the T-Deck built and signed itself (2026-10-26: a letter as one BROADCAST up, one SENT down);
        the arcade takes only one spending this account's own uninscribed coins."""
        dev = self._device(node, msg)
        if dev is None:
            return
        r = F.Reader(msg.body)
        index, raw = r.u8(), r.bytes()
        said = dev.arcade.broadcast(self._chain(dev, index)["network"], raw)
        self.reply(node, F.SENT, msg.id, bytes.fromhex(said["txid"]))

    # -- contacts -------------------------------------------------------------
    def on_who_req(self, node, msg):
        dev = self._device(node, msg)
        if dev is None:
            return
        name = F.Reader(msg.body).str().strip().lstrip("@").lower()
        try:
            said = dev.arcade.who(name)
        except ArcadeError as exc:
            return self.error(node, msg.id, F.E_UNKNOWN_TAG, str(exc))
        key = bytes.fromhex(said.get("key") or "00" * 32)
        w = F.Writer().str(said.get("tag") or name).str(said["address"]).raw(key).str(said.get("name") or "")
        self.reply(node, F.WHO, msg.id, w.done())

    def on_watch(self, node, msg):
        dev = self._device(node, msg)
        if dev is None:
            return
        r = F.Reader(msg.body)
        watch = {r.take(20) for _ in range(r.u8())}
        if watch - dev.watch and dev.scan_cursor is not None:
            # new contacts: look back over recent letters, which may be theirs
            # (receipts keep anything already delivered from going twice)
            dev.scan_cursor = max(0, int(dev.scan_cursor) - 500)
        dev.watch = watch
        self.save(dev)
        self.reply(node, F.WATCH_OK, msg.id, F.Writer().u8(len(dev.watch)).done())

    # -- mail -----------------------------------------------------------------
    def on_mail_post(self, node, msg):
        dev = self._device(node, msg)
        if dev is None:
            return
        r = F.Reader(msg.body)
        to, sealed = r.str(), r.bytes()
        said = dev.arcade.write(to, sealed)
        tag_chain = next(i for i, c in enumerate(dev.chains) if c.get("tags"))
        said.setdefault("to", to)
        self._offer(dev, msg.id, tag_chain, said, "mail")

    def on_mail_ack(self, node, msg):
        dev = self._device(node, msg)
        if dev is None:
            return
        r = F.Reader(msg.body)
        txid, sig = r.take(32).hex(), r.take(64)
        try:
            nacl.signing.VerifyKey(dev.pubkey).verify(RECEIPT + txid.encode(), sig)
        except nacl.exceptions.BadSignatureError:
            return self.error(node, msg.id, F.E_BAD_REQUEST, "that receipt is not signed by this account")
        self.store.delivered(node, txid, sig)

    def on_mail_poll(self, node, msg):
        dev = self._device(node, msg)
        if dev is None:
            return
        self.check_mail(dev, force=True)

    def check_mail(self, dev: Device, force: bool = False):
        """Queue new mail for one device, and send what waits if it is on."""
        tag_chain = next((c for c in dev.chains if c.get("tags")), None)
        if tag_chain is None:
            return
        # A letter is listed a little before the 0.01 notice coin it pays us is
        # indexed, so a contact's letter without its notice waits and is looked
        # at again; the saved cursor never passes it, so a restart cannot lose it.
        if dev.scan_cursor is None:
            dev.scan_cursor = dev.mail_cursor
        while True:
            said = dev.arcade.messages(dev.scan_cursor)
            for cand in said.get("candidates", []):
                if self._from_contact(dev, cand) and not self.store.known(dev.node, cand["txid"]):
                    dev.maybe.setdefault(cand["txid"], (cand, self.clock()))
            dev.scan_cursor = said.get("cursor", dev.scan_cursor)
            if not said.get("more"):
                break
        if dev.maybe:
            mine = {c["txid"] for c in dev.arcade.coins(tag_chain["network"]) if int(c["value"]) == NOTICE_VALUE}
            for txid, (cand, seen) in list(dev.maybe.items()):
                if txid in mine:
                    self._queue_mail(dev, cand)
                    del dev.maybe[txid]
                elif self.clock() - seen > NOTICE_WAIT:
                    del dev.maybe[txid]          # a contact's letter to somebody else
        waiting = [int(c["cursor"]) for c, _ in dev.maybe.values() if c.get("cursor") is not None]
        dev.mail_cursor = min(waiting) - 1 if waiting else dev.scan_cursor
        self.save(dev)
        self.flush(dev, force=force)

    def _from_contact(self, dev: Device, cand: dict) -> bool:
        try:
            return b58decode_check(cand.get("from_address") or "")[1] in dev.watch
        except Exception:
            return False

    def _queue_mail(self, dev: Device, cand: dict):
        sender = cand.get("from_address") or ""
        try:
            sender_h160 = b58decode_check(sender)[1]
        except Exception:
            return
        if sender_h160 not in dev.watch:
            return                      # only contacts' mail goes on air
        payload = bytes.fromhex(cand["payload"])
        if payload[:4] == b"arcm":
            # The 8-byte on-chain header (magic, version, type, clen); the device
            # wants only the sealed box, so Class B's padding after it goes too.
            clen = int.from_bytes(payload[6:8], "big")
            payload = payload[8:8 + clen] if clen else payload[8:]
        when = int(cand.get("when") or 0)
        txid = bytes.fromhex(cand["txid"])
        tag = dev.arcade.tag_of(sender) if hasattr(dev.arcade, "tag_of") else ""
        if cand.get("type") not in (None, 1) or len(payload) > self.mail_max:
            body = F.Writer().raw(txid).u32(when).str(sender).u16(len(payload)).str(tag).done()
            kind = F.MAIL_TOO_LONG
        else:
            body = F.Writer().raw(txid).u32(when).str(sender).bytes(payload).str(tag).done()
            kind = F.MAIL_IN
        self.store.queue(dev.node, cand["txid"], kind, body)

    def poll(self):
        """Housekeeping, every few seconds from the link's loop."""
        with self.lock:
            self.joiner.expire()
            for dev in list(self.devices.values()):
                if dev.offer and self.clock() - dev.offer["at"] > 300:
                    dev.offer = None
                if dev.online and self.clock() - dev.last_heard > ONLINE_WINDOW:
                    dev.online = False      # switched off or out of range: stop sending into the air
                if dev.tag and dev.arcade.signed_in:
                    try:
                        self.check_mail(dev)
                    except ArcadeError as exc:
                        log.warning("mail check for @%s failed: %s", dev.tag, exc)

    HANDLERS = {
        F.HELLO: on_hello, F.LOGIN: on_login, F.PING: on_ping,
        F.BAL_REQ: on_bal_req, F.TOK_REQ: on_tok_req, F.PAGE_REQ: on_page_req, F.PIC_REQ: on_pic_req, F.RESEND: on_resend, F.COINS_REQ: on_coins_req, F.SEND_REQ: on_send_req,
        F.SIGN: on_sign, F.BROADCAST: on_broadcast, F.ADDR: on_addr,
        F.WHO_REQ: on_who_req, F.WATCH: on_watch,
        F.MAIL_POST: on_mail_post, F.MAIL_ACK: on_mail_ack, F.MAIL_POLL: on_mail_poll,
    }

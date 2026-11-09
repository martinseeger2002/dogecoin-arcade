"""Device <-> gateway over an in-memory 'radio', against a fake arcade."""
import nacl.signing
import pytest
from arcade.lora import frames as F, txcheck as T, device_keys as K
from arcade.lora.arcade_api import ArcadeError
from arcade.lora.device import DeviceClient, GatewayError
from arcade.lora.gateway import Gateway, NOTICE_VALUE

PHRASE = "abandon " * 11 + "about"
ACCT = K.Account(PHRASE)
ROBIN = K.Account("zoo " * 11 + "wrong")
ME = ACCT.address("test", 113)
ROBIN_ADDR = ROBIN.address("test", 113)


class FakeArcade:
    """Just enough of the arcade's account API, and as strict as it is."""
    signed = {}

    def __init__(self, base, *a, **kw):
        self.cookies = {}
        self.nonce = None
        self.offers = {}
        self.sent = []
        FakeArcade.last = self

    @property
    def signed_in(self):
        return bool(self.cookies)

    def cookie(self):
        return self.cookies.get("arcade_session")

    def restore(self, cookie):
        self.cookies["arcade_session"] = cookie

    def close(self): pass

    def challenge(self):
        self.nonce = "n0nce"
        return {"origin": "http://127.0.0.1:8420", "nonce": self.nonce}

    def login(self, pubkey, nonce, signature, join=False):
        msg = b"DogecoinArcade login\nhttp://127.0.0.1:8420\n" + nonce.encode()
        nacl.signing.VerifyKey(pubkey).verify(msg, signature)   # raises if wrong
        self.cookies["arcade_session"] = "x"
        return {"ok": True}

    def account(self):
        return {"tag": "tester", "mail_from": 0, "chains": [
            {"network": "test", "label": "PEP test", "tags": True, "mainnet": False, "balance": 7300, "incoming": 5,
             "leaving": 100, "held": 200, "inscribed": 5, "arriving": 9,
             "version": 113, "p2sh": 196},
            {"network": "main", "label": "PEP", "tags": False, "mainnet": True, "balance": 1, "incoming": 0,
             "version": 56, "p2sh": 22}]}

    def tokens(self):
        return [{"network": "test", "tokens": [{"property_id": 5, "name": "Arcade", "display": "84,000,000"}]}]

    def token20(self, network):
        if network != "doge":
            raise ArcadeError("no ord index of that chain")
        return [{"tick": "DOGI", "units": "1234500", "pending_units": "4500", "decimals": 4}]

    def coins(self, network):
        return [{"txid": "11" * 32, "vout": 0, "value": 4000}, {"txid": "22" * 32, "vout": 3, "value": 3000},
                {"txid": "33" * 32, "vout": 1, "value": NOTICE_VALUE}]

    def _build(self, outs):
        tx = T.Tx(1)
        tx.ins = [T.TxIn(bytes.fromhex("11" * 32), 0, b"", 0xFFFFFFFF), T.TxIn(bytes.fromhex("22" * 32), 3, b"", 0xFFFFFFFF)]
        tx.outs = outs
        return tx.serialize()

    def send(self, network, to, amount):
        sats = int(amount)   # the real session turns sats into coin text; the fake takes sats
        to = ROBIN_ADDR if to == "@robin" else to
        own = T.p2pkh(K.hash160(ACCT.coin_pubkey("test")))
        raw = self._build([T.TxOut(sats, T.script_for(to, 113, 196)), T.TxOut(7000 - sats - 500, own)])
        self.offers["o1"] = raw
        return {"offer": "o1", "to": to, "to_tag": "robin", "raw": raw.hex(), "fee": 500, "change": 7000 - sats - 500,
                "inputs": [{"value": 4000}, {"value": 3000}]}

    def sign(self, offer, sigs, pubkey):
        raw = self.offers.pop(offer)
        signed = T.assemble(raw, sigs, pubkey)
        self.sent.append(signed)
        return {"txid": T.Tx.parse(signed).txid().hex()}

    def who(self, name):
        if name != "robin":
            raise ArcadeError("no such tag")
        return {"tag": "robin", "address": ROBIN_ADDR, "key": ROBIN.mail_pubkey.hex(), "name": "Robin"}

    def messages(self, after, limit=200):
        if after and int(after) >= 2:
            return {"cursor": 2, "more": False, "candidates": []}
        return {"cursor": 2, "more": False, "candidates": [
            {"cursor": 1, "txid": "33" * 32, "when": 1700000000, "from_address": ROBIN_ADDR, "type": 1,
             "payload": (b"arcm\x01\x01\x00\x05" + b"hello" + b"\x00" * 25).hex()},   # Class B pads
            {"cursor": 2, "txid": "44" * 32, "when": 1700000001, "from_address": ROBIN_ADDR, "type": 1,
             "payload": (b"x" * 50).hex()}]}


@pytest.fixture
def pair():
    box = {}
    gw = Gateway("http://127.0.0.1:8420", lambda node, frame: box["dev"].receive(frame),
                 session_factory=FakeArcade, flush_delay=0)
    dev = DeviceClient(ACCT, lambda frame: gw.receive(0x970046A4, frame), timeout=2)
    box["dev"] = dev
    return gw, dev


def test_login_balances_who(pair):
    gw, dev = pair
    assert dev.login() == "tester"
    assert [c.network for c in dev.chains] == ["test", "main"]
    assert dev.balances() == {"test": (7000, 9), "main": (1, 0)}   # spendable, arriving: the web wallet's numbers
    assert dev.who("@robin")["key"] == ROBIN.mail_pubkey
    with pytest.raises(GatewayError) as e:
        dev.who("nobody")
    assert e.value.code == F.E_UNKNOWN_TAG


def test_not_logged_in(pair):
    gw, dev = pair
    with pytest.raises(GatewayError) as e:
        dev.balances()
    assert e.value.code == F.E_NOT_LOGGED_IN


def test_send_signs_after_checking(pair):
    gw, dev = pair
    dev.login()
    txid = dev.send("test", "@robin", 5000 - 1500)
    signed = FakeArcade.last.sent[-1]
    assert T.Tx.parse(signed).txid().hex() == txid


def test_lying_gateway_refused(pair, monkeypatch):
    gw, dev = pair
    dev.login()
    real = FakeArcade.send
    def greedy(self, network, to, amount):
        said = real(self, network, to, amount)
        raw = T.Tx.parse(bytes.fromhex(said["raw"]))
        raw.outs[1].script = T.p2pkh(b"\x66" * 20)     # change stolen
        said["raw"] = raw.serialize().hex()
        return said
    monkeypatch.setattr(FakeArcade, "send", greedy)
    with pytest.raises(T.OfferRefused):
        dev.send("test", "@robin", 3000)
    assert FakeArcade.last.sent == []


def test_mail_only_from_contacts_and_kept_until_receipt(pair):
    gw, dev = pair
    dev.login()
    gw.poll()
    assert dev.inbox.empty()                 # robin not watched yet: nothing on air
    gw.devices[0x970046A4].mail_cursor = 0
    assert dev.watch([ROBIN_ADDR]) == 1
    gw.poll()
    msg = dev.inbox.get(timeout=1)
    assert msg.type == F.MAIL_IN             # 33.. pays us the notice; 44.. does not
    r = F.Reader(msg.body)
    assert r.take(32).hex() == "33" * 32 and r.u32() == 1700000000 and r.str() == ROBIN_ADDR and r.bytes() == b"hello"
    assert dev.inbox.empty()
    assert len(gw.store.pending(0x970046A4)) == 1          # radio delivery alone does not count
    dev.inbox.put(msg)
    assert dev.take_mail()[0]["sealed"] == b"hello"         # the device keeps it and signs a receipt
    assert gw.store.pending(0x970046A4) == []
    assert gw.store.receipt(0x970046A4, "33" * 32) is not None


def test_mail_waits_while_off_and_goes_when_heard(pair):
    gw, dev = pair
    dev.login()
    node = 0x970046A4
    d = gw.devices[node]
    d.mail_cursor = 0
    dev.watch([ROBIN_ADDR])
    d.online = False                          # switched off
    gw.poll()
    assert dev.inbox.empty()                  # nothing sent into the air
    assert len(gw.store.pending(node)) == 1   # but it waits
    gw.heard(node)                            # its node info, a HELLO, anything
    assert dev.inbox.get(timeout=1).type == F.MAIL_IN


def test_restart_keeps_devices_sessions_and_outbox(tmp_path):
    from arcade.lora.store import Store
    path = str(tmp_path / "gw.sqlite")
    box = {}
    gw = Gateway("http://127.0.0.1:8420", lambda n, f: box["dev"].receive(f), session_factory=FakeArcade, store=Store(path), flush_delay=0)
    dev = DeviceClient(ACCT, lambda f: gw.receive(1, f), timeout=2)
    box["dev"] = dev
    dev.login()
    gw.devices[1].mail_cursor = 0
    dev.watch([ROBIN_ADDR])
    gw.devices[1].online = False
    gw.poll()
    gw2 = Gateway("http://127.0.0.1:8420", lambda n, f: box["dev"].receive(f), session_factory=FakeArcade, store=Store(path), flush_delay=0)
    d = gw2.devices[1]
    assert d.tag == "tester" and d.arcade.signed_in and d.watch and len(gw2.store.pending(1)) == 1
    assert __import__("os").stat(path).st_mode & 0o077 == 0


def test_mail_too_long():
    gw = Gateway("http://x", lambda n, f: None, session_factory=FakeArcade, mail_max=3, flush_delay=0)
    from arcade.lora.gateway import Device
    d = Device(1, b"k" * 32, FakeArcade("x"))
    d.watch = {K.b58decode_check(ROBIN_ADDR)[1]}
    gw._queue_mail(d, {"txid": "33" * 32, "when": 1, "from_address": ROBIN_ADDR, "type": 1,
                       "payload": (b"arcm\x01\x01\x00\x05hello").hex()})
    assert gw.store.pending(1)[0]["type"] == F.MAIL_TOO_LONG


def test_register_makes_the_devices_own_address(pair):
    gw, dev = pair
    seen = {}
    FakeArcade.register = lambda self, net, addr, pub, sig=b"": seen.update(net=net, addr=addr, pub=pub, sig=sig) or {}
    orig = FakeArcade.account
    FakeArcade.account = lambda self: {**orig(self), "chains": [dict(c, version=113 if c["network"] == "test" else 56) for c in orig(self)["chains"]]}
    try:
        dev.login()
        assert dev.register("test") == ME == seen["addr"]
        # with the coin key's proof the arcade asks before it files an address (2026-11-09): this account's, here
        from arcade import wallet_imports
        from arcade.config import NETWORKS
        wallet_imports.check_proof(NETWORKS["test"], ACCT.login_pubkey.hex(), "test", ME, seen["pub"].hex(), "p2pkh",
                                   seen["sig"].hex())
        with pytest.raises(wallet_imports.ImportRefused):
            wallet_imports.check_proof(NETWORKS["test"], ROBIN.login_pubkey.hex(), "test", ME, seen["pub"].hex(),
                                       "p2pkh", seen["sig"].hex())
    finally:
        FakeArcade.account = orig


def test_register_never_overwrites_a_different_address(pair):
    gw, dev = pair
    posted = []
    FakeArcade.register = lambda self, net, addr, pub, sig=b"": posted.append(addr) or {}
    orig = FakeArcade.account
    def acct(self):
        a = orig(self)
        a["chains"][0]["address"] = ROBIN_ADDR          # the arcade watches something else
        a["chains"][1]["address"] = None                # nothing yet on main
        return a
    FakeArcade.account = acct
    try:
        dev.login()
        with pytest.raises(GatewayError) as e:
            dev.register("test")
        assert "nothing changed" in e.value.text and posted == []
        dev.register("main")
        assert posted == [ACCT.address("main", 56)]
    finally:
        FakeArcade.account = orig


def test_letter_listed_before_its_notice_coin_still_arrives(pair, monkeypatch):
    """The real miss of 2026-10-23: the 'Hi' was listed before its 0.01 notice was indexed."""
    gw, dev = pair
    dev.login()
    node = 0x970046A4
    gw.devices[node].mail_cursor = 0
    dev.watch([ROBIN_ADDR])
    letter = {"cursor": 7, "txid": "77" * 32, "when": 1700000200, "from_address": ROBIN_ADDR, "type": 1,
              "payload": (b"arcm\x01\x01\x00\x02hi").hex()}
    monkeypatch.setattr(FakeArcade, "messages", lambda self, after, limit=200: {
        "cursor": 7, "more": False, "candidates": [letter] if (after or 0) < 7 else []})
    monkeypatch.setattr(FakeArcade, "coins", lambda self, net: [])            # notice not indexed yet
    gw.poll()
    assert dev.inbox.empty() and gw.devices[node].mail_cursor == 6            # held back, not skipped
    monkeypatch.setattr(FakeArcade, "coins", lambda self, net: [{"txid": "77" * 32, "vout": 1, "value": NOTICE_VALUE}])
    gw.poll()                                                                 # the notice lands
    msg = dev.inbox.get(timeout=1)
    assert msg.type == F.MAIL_IN
    assert gw.devices[node].mail_cursor == 7


def test_a_repeated_sign_gets_the_same_answer_and_sends_once(pair):
    gw, dev = pair
    dev.login()
    out = []
    real = dev.send_frame
    dev.send_frame = lambda f: (out.append(f), real(f))
    txid = dev.send("test", "@robin", 3500)
    sign = [f for f in out if F.parse(f)[0] == F.SIGN]
    assert sign
    before = len(FakeArcade.last.sent)
    for f in sign:                                     # at once: the answer may still be on its way, not sent again
        gw.receive(0x970046A4, f)
    assert dev.inbox.empty(), "no replay within REPLAY_GAP of the answer"
    real_clock = gw.clock
    gw.clock = lambda: real_clock() + 11               # the T-Deck lost SENT and asks again later
    for f in sign:
        gw.receive(0x970046A4, f)
    assert len(FakeArcade.last.sent) == before         # not signed and broadcast twice
    again = dev.inbox.get(timeout=1)
    assert again.type == F.SENT and again.body.hex() == txid


def test_tokens_the_operator_picked_with_this_accounts_balances():
    """Admin -> Meshtastic picks the tokens T-Decks see (2026-10-26: "a search bar that auto complete just like
    in the public token exchange that allows the operator to include any token for the T deck to see")."""
    box = {}
    assets = [{"key": "test-token-5", "label": "Arcade"}, {"key": "doge-token20-dogi", "label": "DOGI \u00b7 DRC-20"},
              {"key": "main-token20-pepe", "label": "PEPE \u00b7 PRC-20"}, {"key": "test-coin", "label": "PEP test"}]
    gw = Gateway("http://127.0.0.1:8420", lambda node, frame: box["dev"].receive(frame),
                 session_factory=FakeArcade, flush_delay=0, assets=assets)
    dev = DeviceClient(ACCT, lambda frame: gw.receive(0x970046A4, frame), timeout=2)
    box["dev"] = dev
    dev.login()
    assert dev.tokens() == [("test", "Arcade", "84,000,000"),
                            ("doge", "DOGI \u00b7 DRC-20", "123"),            # less what is on its way out
                            ("main", "PEPE \u00b7 PRC-20", "?")]              # that chain cannot say: said so


def test_mesh_pages_need_no_sign_in_and_unchanged_ones_are_not_sent_again(pair, monkeypatch):
    """The .mesh web (PROTOCOL.md PAGE_REQ/PAGE, 2026-10-26): any T-Deck asks for a page, signed in or not; a page it
    already holds comes back as "unchanged" with no body; one too big for a message is cut to one message."""
    gw, dev = pair
    asked = []

    def mesh_page(self, address, have=""):
        asked.append((address, have))
        if address.startswith("nobody.mesh"):
            return 404, {"detail": "Nobody has claimed nobody.mesh."}
        if have == "ab" * 32 + "i0":
            return 200, {"page": have, "title": "Hello", "unchanged": True}
        body = "# Hello\n=> /about About" if address != "big.mesh" else "x" * 5000
        return 200, {"page": "ab" * 32 + "i0", "title": "Hello", "body": body}
    monkeypatch.setattr(FakeArcade, "mesh_page", mesh_page, raising=False)
    got = dev.page("me-seeks.mesh")
    assert got == {"status": F.PAGE_OK, "page": "ab" * 32 + "i0", "note": "Hello", "body": "# Hello\n=> /about About"}
    again = dev.page("me-seeks.mesh", have="ab" * 32 + "i0")
    assert again["status"] == F.PAGE_UNCHANGED and again["body"] == ""
    missing = dev.page("nobody.mesh")
    assert missing["status"] == F.PAGE_MISSING and "Nobody has claimed" in missing["note"]
    big = dev.page("big.mesh")
    assert big["status"] == F.PAGE_OK and 2900 < len(big["body"]) < 3120, "cut to one 16-part message"
    assert asked[0] == ("me-seeks.mesh", "")


def test_a_page_goes_compressed_only_to_a_t_deck_that_reads_it(pair, monkeypatch):
    """Item 3 of 2026-10-26's speed work: MeshText packs to about half, so half the frames on air -- but only for a
    T-Deck whose PAGE_REQ says it inflates (a T-Deck from before gets the page as it was)."""
    gw, dev = pair
    body = "# Hello\n" + "\n".join(f"=> /p{i} Page number {i} of the site" for i in range(40))
    monkeypatch.setattr(FakeArcade, "mesh_page", lambda self, a, have="": (200, {"page": "p1", "title": "Hi",
                                                                                "body": body}), raising=False)
    sent = []
    real = gw.send_frame
    gw.send_frame = lambda node, frame: (sent.append(frame), real(node, frame))
    old = dev.page("x.mesh")
    plain = len(sent)
    sent.clear()
    new = dev.page("x.mesh", zlib_ok=True)
    assert old["body"] == new["body"] == body and old["status"] == new["status"] == F.PAGE_OK
    assert len(sent) < plain, (len(sent), plain)


def test_a_picture_comes_in_pieces_and_needs_no_sign_in(pair, monkeypatch):
    """A picture on a .mesh page (PROTOCOL.md PIC_REQ/PIC, 2026-10-26): the arcade's mesh picture, one message's worth
    from each offset the T-Deck asks for, read from the arcade once."""
    gw, dev = pair
    picture = bytes(range(256)) * 30                       # 7,680 bytes: three messages
    asked = []

    def mesh_picture(self, iid):
        asked.append(iid)
        return (200, picture) if iid == "ab" * 32 + "i0" else (404, {"detail": "not a picture"})
    monkeypatch.setattr(FakeArcade, "mesh_picture", mesh_picture, raising=False)
    got = dev.picture("ab" * 32 + "i0")
    assert got["status"] == F.PIC_OK and got["body"] == picture
    assert asked == ["ab" * 32 + "i0"], "the gateway asked the arcade once for every piece"
    missing = dev.picture("cd" * 32 + "i0")
    assert missing["status"] == F.PIC_MISSING and "not a picture" in missing["note"]

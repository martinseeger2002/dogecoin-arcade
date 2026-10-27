"""Welcoming a new operator (arcade/welcome.py, arcade/mesh/welcomeshare.py, arcade/web/welcoming.py; 2026-10-27):
a new arcade's one ask for 1 PEP test, passed across the mesh; one click pays it; it closes on the first block that
pays it, and everybody paid in that block scores."""
import asyncio
import pathlib
import sys
import time
from types import SimpleNamespace

import pytest
from nacl.signing import SigningKey

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_admin import LOCAL, WRITE                              # noqa: E402

from arcade import welcome as W                                  # noqa: E402
from arcade.mesh.welcomeshare import WelcomeShare                # noqa: E402
from arcade.script import b58check_encode                        # noqa: E402
from arcade.web import welcoming                                 # noqa: E402

NOW = int(time.time()) - 3600
TX = ["%064x" % n for n in range(1, 9)]
B1, B2 = "b1" * 32, "b2" * 32


def _id(key):
    return bytes(key.verify_key).hex()


def _ask(key, at=NOW, addr="nPepeTestAddressxxxxxxxxxxxxxxxxx", tag="newbie"):
    return W.sign({"k": "ask", "at": at, "addr": addr, "tag": tag}, key)


def _sent(key, asker, txid, at=NOW + 10, tag=""):
    return W.sign({"k": "sent", "at": at, "ask": _id(asker), "txid": txid, "tag": tag}, key)


def _closed(asker, txids, at=NOW + 100, height=500):
    return W.sign({"k": "closed", "at": at, "block": "ab" * 32, "height": height, "txids": txids}, asker)


# ---------------------------------------------------------------- records


def test_a_record_is_its_authors_and_cannot_be_changed():
    k = SigningKey.generate()
    rec = _ask(k)
    assert W.clean(rec, NOW) == {**rec, "at": NOW}
    assert W.clean({**rec, "addr": "nSomebodyElsexxxxxxxxxxxxxxxxxxxxx"}, NOW) is None, "the address is signed"
    assert W.clean({**rec, "a": _id(SigningKey.generate())}, NOW) is None, "so is who wrote it"
    assert W.clean(_ask(k, at=NOW - W.ASK_DAYS * 86400 - 1), NOW) is None, "an old ask is forgotten"
    assert W.clean(_ask(k, at=NOW + W.SKEW + 5), NOW) is None, "one dated ahead is refused"
    assert W.clean(_ask(k, addr="not an address"), NOW) is None
    assert W.clean(_ask(k, tag="x" * 99), NOW) is None, "a tag not shaped like one: changing it would break the signature"
    assert W.clean(_ask(k, tag="NewBie"), NOW) is None and W.clean(_ask(k, tag=""), NOW)["tag"] == ""
    assert W.clean(W.sign({"k": "sent", "at": NOW, "ask": _id(k), "txid": TX[0]}, k), NOW) is None, \
        "nobody pays its own ask"


def test_one_ask_an_arcade_and_a_closed_one_stays_closed(tmp_path):
    k, payer = SigningKey.generate(), SigningKey.generate()
    book = W.Book(tmp_path / "welcome.json")
    assert book.add(_ask(k), NOW)
    assert book.add(_ask(k), NOW) is None, "known"
    assert book.add(_ask(k, at=NOW + 5, addr="nNewerAddressxxxxxxxxxxxxxxxxxxxxx"), NOW + 5)["addr"].startswith("nNewer")
    assert book.add(_sent(payer, k, TX[0]), NOW + 10)
    assert book.add(_sent(payer, k, TX[1], at=NOW + 11), NOW + 11) is None, "one payment an arcade, an ask"
    assert book.add(_closed(k, [TX[0]]), NOW + 100)
    assert book.open_asks() == []
    assert book.add(_ask(k, at=NOW + 200), NOW + 200) is None, "never opened again"
    again = W.Book(tmp_path / "welcome.json")
    assert len(again.all()) == 3 and again.closed_of(_id(k)), "kept on disk"


# ---------------------------------------------------------------- the board


def test_everybody_paid_in_the_first_block_scores_and_later_ones_do_not():
    new, alice, bob, carol = (SigningKey.generate() for _ in range(4))
    recs = [_ask(new), _sent(alice, new, TX[0], tag="alice"), _sent(bob, new, TX[1], tag="bob"),
            _sent(carol, new, TX[2], tag="carol"), _closed(new, [TX[0], TX[1]])]
    board = W.scores([W.clean(r, NOW + 100) for r in recs])
    assert [(r["tag"], r["points"]) for r in board] == [("alice", 1), ("bob", 1)] or \
        [(r["tag"], r["points"]) for r in board] == [("bob", 1), ("alice", 1)]
    second = SigningKey.generate()
    recs += [_ask(second, at=NOW + 200), _sent(alice, second, TX[3], at=NOW + 210),
             _closed(second, [TX[3]], at=NOW + 300)]
    board = W.scores([W.clean(r, NOW + 300) for r in recs])
    assert board[0]["tag"] == "alice" and board[0]["points"] == 2


def test_a_payment_counts_for_whoever_said_it_first():
    new, alice, mallory = (SigningKey.generate() for _ in range(3))
    recs = [_ask(new), _sent(alice, new, TX[0], at=NOW + 10), _sent(mallory, new, TX[0], at=NOW + 20),
            _closed(new, [TX[0]])]
    board = W.scores([W.clean(r, NOW + 100) for r in recs])
    assert [r["node"] for r in board] == [_id(alice)]


def test_the_first_paid_block_and_all_of_its_payments():
    assert W.paid_block([]) is None
    assert W.paid_block([(TX[0], "", 0)]) is None, "not in a block yet"
    got = W.paid_block([(TX[0], "b2", 12), (TX[1], "b1", 11), (TX[2], "b1", 11)])
    assert got == ("b1", 11, [TX[1], TX[2]])


# ---------------------------------------------------------------- this arcade's side


class Rpc:
    def __init__(self, txs=None, balance=0.0):
        self.txs, self.balance, self.calls = txs or {}, balance, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def call(self, method, *args):
        self.calls.append((method, args))
        if method == "getrawtransaction":
            if args[0] not in self.txs:
                raise RuntimeError("No such mempool or blockchain transaction")
            return self.txs[args[0]]
        if method == "getblockheader":
            return {"height": {B1: 11, B2: 12}[args[0]]}
        if method == "getbalance":
            return self.balance
        if method == "sendtoaddress":
            return TX[7]
        raise AssertionError(method)


def _state(rpc, address="nMyPayoutAddressxxxxxxxxxxxxxxxxxx", tag="newbie"):
    from arcade.config import NETWORKS
    settings = {}
    messaging = SimpleNamespace(network="test", params=NETWORKS["test"], rpc=lambda: rpc)
    return SimpleNamespace(messaging=messaging, payout_address=lambda chain: address, operator_tag=lambda: tag,
                           setting=lambda n, d=None: settings.get(n, d), set_setting=settings.__setitem__,
                           settings=settings)


@pytest.fixture
def coins(monkeypatch):
    held = {"n": 0}
    monkeypatch.setattr(welcoming, "address_coins", lambda state, address: held["n"])
    return held


def test_a_new_arcade_asks_once_when_it_links_and_has_no_coins(coins):
    key, book, st = SigningKey.generate(), W.Book(None), _state(Rpc())
    assert welcoming.tick(st, book, key, linked=0, now=NOW) == [], "nobody to ask yet"
    mine = welcoming.tick(st, book, key, linked=1, now=NOW)
    assert [r["k"] for r in mine] == ["ask"] and mine[0]["addr"] == "nMyPayoutAddressxxxxxxxxxxxxxxxxxx"
    assert mine[0]["tag"] == "newbie" and st.settings[welcoming.ASKED] == NOW
    assert welcoming.tick(st, book, key, linked=1, now=NOW + 60) == [], "open, and nobody paid"
    funded_key, funded_book = SigningKey.generate(), W.Book(None)
    coins["n"] = W.AMOUNT
    assert welcoming.tick(_state(Rpc()), funded_book, funded_key, linked=1, now=NOW) == [], "an arcade with coins"


def test_the_ask_closes_on_the_first_block_that_pays_it(coins):
    key, alice, bob = (SigningKey.generate() for _ in range(3))
    addr = "nMyPayoutAddressxxxxxxxxxxxxxxxxxx"
    pays = {"vout": [{"scriptPubKey": {"addresses": [addr]}}]}
    rpc = Rpc({TX[0]: {**pays, "blockhash": B2}, TX[1]: {**pays, "blockhash": B1},
               TX[2]: {"vout": [{"scriptPubKey": {"address": "nSomebodyElse"}}], "blockhash": B1}})
    book, st = W.Book(None), _state(rpc)
    welcoming.tick(st, book, key, linked=1, now=NOW)
    for payer, txid in ((alice, TX[0]), (bob, TX[1]), (SigningKey.generate(), TX[2]), (SigningKey.generate(), TX[3])):
        assert book.add(_sent(payer, key, txid), NOW + 10)
    closed = welcoming.tick(st, book, key, linked=1, now=NOW + 60)
    assert [(r["k"], r["height"], r["txids"]) for r in closed] == [("closed", 11, [TX[1]])], \
        "block 11 first; a payment to somebody else is no payment"
    assert welcoming.tick(st, book, key, linked=1, now=NOW + 120) == []
    assert [r["node"] for r in W.scores(book.all())] == [_id(bob)]


def test_coins_that_came_another_way_close_it_with_nobody_scoring(coins):
    key, book, st = SigningKey.generate(), W.Book(None), _state(Rpc())
    welcoming.tick(st, book, key, linked=1, now=NOW)
    coins["n"] = W.AMOUNT * 50                                     # it mined a block
    closed = welcoming.tick(st, book, key, linked=1, now=NOW + 60)
    assert closed[0]["k"] == "closed" and closed[0]["txids"] == [] and W.scores(book.all()) == []


def test_one_click_pays_one_pep_test_from_the_node_wallet_once(coins):
    from arcade.config import NETWORKS
    asker, me = SigningKey.generate(), SigningKey.generate()
    addr = b58check_encode(NETWORKS["test"].pubkeyhash_version, bytes(20))
    book, rpc = W.Book(None), Rpc()
    book.add(_ask(asker, at=int(time.time()), addr=addr))
    rec = welcoming.send(_state(rpc, tag="alice"), book, me, _id(asker))
    assert ("sendtoaddress", (addr, 1.0)) in rpc.calls
    assert rec["k"] == "sent" and rec["txid"] == TX[7] and rec["tag"] == "alice"
    with pytest.raises(welcoming.WelcomeError, match="already"):
        welcoming.send(_state(rpc), book, me, _id(asker))
    with pytest.raises(welcoming.WelcomeError, match="own ask"):
        welcoming.send(_state(rpc), book, asker, _id(asker))
    with pytest.raises(welcoming.WelcomeError, match="closed or gone"):
        welcoming.send(_state(rpc), book, me, "ab" * 32)


def test_asking_again_by_hand_only_once_the_first_is_gone(coins):
    key, book, st = SigningKey.generate(), W.Book(None), _state(Rpc())
    welcoming.tick(st, book, key, linked=1, now=time.time())
    with pytest.raises(welcoming.WelcomeError, match="still open"):
        welcoming.ask_again(st, book, key)
    book.records.clear()                                           # forgotten after ASK_DAYS
    assert welcoming.ask_again(st, book, key)["k"] == "ask"


# ---------------------------------------------------------------- over the mesh


class Link:
    def __init__(self, peer):
        self.peer_id, self.sent = peer, []

    async def send(self, header, blob=b""):
        self.sent.append(header)


def test_a_record_crosses_the_mesh_once():
    share = WelcomeShare(W.Book(None))
    a, b, c = Link("a" * 64), Link("b" * 64), Link("c" * 64)
    share.node = SimpleNamespace(links={l.peer_id: l for l in (a, b, c)})
    rec = _ask(SigningKey.generate(), at=int(time.time()))
    asyncio.run(share.on_frame(a, {"t": "wl", "recs": [rec]}, b""))
    assert a.sent == [] and [h["recs"][0]["a"] for h in b.sent + c.sent] == [rec["a"], rec["a"]]
    asyncio.run(share.on_frame(b, {"t": "wl", "recs": [rec]}, b""))
    assert len(c.sent) == 1, "known: not passed on again"
    late = Link("d" * 64)
    asyncio.run(share.link_up(late))
    assert late.sent[0]["recs"][0]["a"] == rec["a"], "an arcade that links later hears the open ask"
    asyncio.run(share.on_frame(a, {"t": "wl", "recs": [{**rec, "addr": "nForgedxxxxxxxxxxxxxxxxxxxxxxxxxx"}]}, b""))
    assert len(c.sent) == 1, "a forged record goes nowhere"


def test_a_link_brings_a_bounded_number_of_new_asks_an_hour():
    share = WelcomeShare(W.Book(None))
    a = Link("a" * 64)
    share.node = SimpleNamespace(links={a.peer_id: a})
    recs = [_ask(SigningKey.generate(), at=int(time.time())) for _ in range(40)]
    asyncio.run(share.on_frame(a, {"t": "wl", "recs": recs}, b""))
    assert len(share.book.open_asks()) == 30


# ---------------------------------------------------------------- Admin and Notifications


@pytest.fixture
def meshed(client, monkeypatch):
    app, state = client
    key = SigningKey.generate()
    share = WelcomeShare(W.Book(None))
    share.node = SimpleNamespace(key=key, links={})

    def run(coro):
        coro.close()
    monkeypatch.setattr(state, "mesh", SimpleNamespace(welcome=share, _call=run), raising=False)
    return app, state, share


def test_admin_shows_the_asks_and_the_board_and_pays_in_one_click(meshed, monkeypatch):
    app, state, share = meshed
    asker = SigningKey.generate()
    addr = b58check_encode(state.messaging.params.pubkeyhash_version, bytes(20))
    share.book.add(_ask(asker, at=int(time.time()), addr=addr, tag="newbie"))
    got = app.get("/admin/api/welcome", headers=LOCAL).json()
    assert got["mesh"] is True and [a["tag"] for a in got["open"]] == ["newbie"] and got["scores"] == []
    assert got["funded"] is False and got["net"] == state.messaging.network
    assert app.post("/admin/api/welcome/send", headers=LOCAL, json={"ask": _id(asker)}).status_code in (401, 403), \
        "a write needs the admin header"
    rpc = Rpc()
    monkeypatch.setattr(state.messaging, "rpc", lambda: rpc)
    said = app.post("/admin/api/welcome/send", headers={**LOCAL, **WRITE}, json={"ask": _id(asker)})
    assert said.status_code == 200 and said.json()["txid"] == TX[7]
    assert app.get("/admin/api/welcome", headers=LOCAL).json()["open"][0]["paid_by_me"] is True
    again = app.post("/admin/api/welcome/send", headers={**LOCAL, **WRITE}, json={"ask": _id(asker)})
    assert again.status_code == 400 and "already" in again.json()["detail"]
    page = app.get("/admin", headers=LOCAL).text
    assert 'id="welcome"' in page and "Mine a block" in page and "Welcomers" in page


def test_an_open_ask_is_in_the_operators_notifications_with_its_send_button(meshed):
    app, state, share = meshed
    asker = SigningKey.generate()
    share.book.add(_ask(asker, at=int(time.time()), tag="newbie"))
    page = app.get("/notifications", headers=LOCAL).text
    assert "asks for 1 PEP test" in page and f'data-ask="{_id(asker)}"' in page and "/admin#welcome" in page


def test_mine_a_block_is_one_block_at_most_a_hundred(meshed):
    app, state, _ = meshed
    bad = app.post("/admin/api/mine", headers={**LOCAL, **WRITE}, json={"net": state.messaging.network, "blocks": 101})
    assert bad.status_code == 400


def test_an_ask_reaches_an_arcade_two_links_away_on_a_real_mesh(monkeypatch):
    """a - b - c: a new arcade linked only to b is heard by c, and c's payment comes back to a."""
    import test_mesh as tm
    for name, value in (("SUB_EVERY", 0.15), ("HERE_EVERY", 0.15), ("IDLE", 1.0), ("LOCAL_GRACE", 1.0),
                        ("DIAL_EVERY", 0.05)):
        monkeypatch.setattr(tm.meshlib, name, value)

    async def go():
        a, b, c = tm.mk(), tm.mk(), tm.mk()
        shares = [WelcomeShare(W.Book(None)) for _ in range(3)]
        for node, share in zip((a, b, c), shares):
            node.add_extension(share)
        await tm.started(a, b, c)
        try:
            a.add_address(*tm.addr(b)); c.add_address(*tm.addr(b))
            await tm.until(lambda: len(b.links) == 2, what="b links to a and c")
            ask = shares[0].book.add(_ask(a.key, at=int(time.time())))
            await shares[0].spread([ask])
            await tm.until(lambda: shares[2].book.ask_of(a.node_id), what="c hears a's ask")
            paid = shares[2].book.add(_sent(c.key, a.key, TX[0], at=int(time.time())))
            await shares[2].spread([paid])
            await tm.until(lambda: shares[0].book.sent_to(a.node_id), what="a hears c paid")
        finally:
            await tm.stop(a, b, c)
    tm.run(go())

"""The DigiDollar-test launchpad (arcade/ddpad.py): held, paid, delivered, and let go, with a chain of its own."""

import json
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from arcade import ddpad  # noqa: E402
from arcade.config import NETWORKS  # noqa: E402
from arcade.ddlib import ddtx  # noqa: E402

T0 = 1_800_000_000.0


def _build(where: Path, n: int = 5) -> Path:
    (where / "images").mkdir(parents=True)
    (where / "json").mkdir()
    for i in range(1, n + 1):
        (where / "images" / f"{i}.png").write_bytes(b"\x89PNG piece %d" % i)
        (where / "json" / f"{i}.json").write_text(json.dumps({
            "name": f"Bytelings #{i}", "attributes": [{"trait_type": "Stage", "value": "Bit"},
                                                      {"trait_type": "Body", "value": "Blob"}]}))
    (where / "json" / "_metadata.json").write_text("[]")
    return where


class Chain:
    """The pad's chains, as it sees them: payments by txid, its own broadcasts, and whether the index caught up."""

    def __init__(self):
        self.payments: dict[str, tuple[str, int]] = {}
        self.inscribed: list[bytes] = []
        self.sent: list[tuple] = []
        self.blocks: set[str] = set()
        self.busy = False

    def payment(self, txid):
        return self.payments.get(txid, ("missing", 0))

    def ready(self):
        return not self.busy

    def inscribe(self, content_type, content):
        self.inscribed.append(content)
        n = len(self.inscribed)
        return {"iid": f"{n:064x}i0", "outpoint": f"{n:064x}:0", "value": 100_000, "commit": f"c{n}"}

    def deliver(self, iid, outpoint, value, to):
        self.sent.append((iid, outpoint, value, to))
        return f"send{len(self.sent)}"

    def settled(self, txid):
        return txid in self.blocks


@pytest.fixture
def pad(tmp_path):
    p = ddpad.Pad(tmp_path / "home")
    assert p.load(_build(tmp_path / "build")) == 5
    return p


def _inscribed(pad, chain):
    pad.set(inscribe=True)
    for _ in range(5):
        pad.tick(chain, now=T0)
    pad.open(now=T0)
    return pad


def test_a_pad_inscribes_one_piece_a_tick_and_opens_only_once_every_piece_is_on_the_chain(pad):
    chain = Chain()
    with pytest.raises(ddpad.PadError, match="5 of 5 pieces are not inscribed"):
        pad.open()
    pad.tick(chain, now=T0)
    assert chain.inscribed == [], "inscribing waits until it is turned on"
    pad.set(inscribe=True)
    chain.busy = True
    pad.tick(chain, now=T0)
    assert chain.inscribed == [], "nothing is broadcast while the last broadcast is not in the index"
    chain.busy = False
    said = pad.tick(chain, now=T0)
    assert chain.inscribed == [b"\x89PNG piece 1"] and said == [f"#1 inscribed as {1:064x}i0"]
    with pytest.raises(ddpad.PadError, match="4 of 5"):
        pad.open()
    for _ in range(6):
        pad.tick(chain, now=T0)
    assert len(chain.inscribed) == 5, "each piece once"
    assert pad.open(now=T0)["opened_at"] == int(T0)
    assert pad.counts() == {"total": 5, "inscribed": 5, "left": 5, "minted": 0, "states": {}}


def test_reloading_a_build_never_redraws_a_piece_that_is_inscribed(pad, tmp_path):
    _inscribed(pad, Chain())
    other = _build(tmp_path / "other")
    (other / "images" / "2.png").write_bytes(b"drawn differently")
    with pytest.raises(ddpad.PadError, match="#2 is inscribed already"):
        pad.load(other)


def test_a_mint_is_held_then_paid_then_delivered_to_the_accounts_own_address(pad):
    chain = Chain()
    _inscribed(pad, chain)
    mint = pad.hold("ACC", "Dme", "TDme", now=T0)
    assert mint["state"] == "held" and 1 <= mint["number"] <= 5
    assert pad.mints_of("acc")[0]["number"] is None, "which piece is a surprise until it is paid for"
    pad.still_held(mint["id"], now=T0 + 10)
    pad.paying(mint["id"], "pay1", now=T0 + 10)
    shown = pad.mints_of("acc")[0]
    assert shown["state"] == "paying" and shown["number"] == mint["number"]

    chain.payments["pay1"] = ("waiting", 100)
    pad.tick(chain, now=T0 + 20)
    assert pad.mints_of("acc")[0]["state"] == "paying", "not until it is in a block"
    chain.payments["pay1"] = ("confirmed", 100)
    chain.busy = True
    pad.tick(chain, now=T0 + 30)
    assert pad.mints_of("acc")[0]["state"] == "paid" and chain.sent == [], "one broadcast at a time"
    chain.busy = False
    pad.tick(chain, now=T0 + 40)
    iid = f"{mint['number']:064x}i0"
    assert chain.sent == [(iid, iid[:-2] + ":0", 100_000, "Dme")]
    assert pad.mints_of("acc")[0]["state"] == "sending"
    assert (iid[:-2], 0) not in pad.held_outpoints(), "a coin on its way is not the pad's any more"
    chain.blocks.add("send1")
    pad.tick(chain, now=T0 + 50)
    assert pad.mints_of("acc")[0]["state"] == "sent"
    assert pad.counts()["minted"] == 1 and pad.counts()["left"] == 4


def test_one_each_by_account_by_digibyte_address_and_by_digidollar_address(pad):
    _inscribed(pad, Chain())
    pad.hold("a1", "D1", "TD1", now=T0)
    for who, to, payer, name in (("a1", "D9", "TD9", "account"), ("a2", "D1", "TD2", "DigiByte address"),
                                 ("a3", "D3", "TD1", "DigiDollar address")):
        with pytest.raises(ddpad.PadError, match=f"this {name} has minted one already"):
            pad.hold(who, to, payer, now=T0)
    pad.set(per_wallet=2)
    pad.hold("a1", "D1", "TD1", now=T0)
    with pytest.raises(ddpad.PadError, match="has minted its 2 already"):
        pad.hold("a1", "D1", "TD1", now=T0)


def test_a_piece_held_but_never_paid_goes_back_and_its_payment_is_refused(pad):
    _inscribed(pad, Chain())
    mint = pad.hold("a1", "D1", "TD1", now=T0)
    pad.tick(Chain(), now=T0 + ddpad.HOLD_SECONDS + 1)
    assert pad.mints_of("a1")[0]["state"] == "let go"
    with pytest.raises(ValueError, match="let go"):
        pad.still_held(mint["id"])
    assert pad.counts()["left"] == 5
    pad.hold("a1", "D1", "TD1", now=T0 + 400), "a wallet whose mint was let go may mint again"


def test_a_payment_that_pays_too_little_or_never_lands_lets_the_piece_go(pad):
    chain = Chain()
    _inscribed(pad, chain)
    short = pad.hold("a1", "D1", "TD1", now=T0)
    pad.paying(short["id"], "short", now=T0)
    lost = pad.hold("a2", "D2", "TD2", now=T0)
    pad.paying(lost["id"], "lost", now=T0)
    chain.payments["short"] = ("confirmed", 99)
    pad.tick(chain, now=T0 + 60)
    assert pad.mints_of("a1")[0]["state"] == "let go"
    assert "paid the pad $0.99, not $1.00" in pad.mints_of("a1")[0]["why"]
    assert pad.mints_of("a2")[0]["state"] == "paying", "a payment not seen yet is waited for"
    pad.tick(chain, now=T0 + ddpad.PAY_WAIT + 1)
    assert pad.mints_of("a2")[0]["state"] == "let go"
    assert chain.sent == []


def test_a_shut_or_sold_out_pad_refuses_a_mint(pad):
    with pytest.raises(ddpad.PadError, match="not open yet"):
        pad.hold("a", "D", "TD")
    _inscribed(pad, Chain())
    for i in range(5):
        pad.hold(f"a{i}", f"D{i}", f"TD{i}", now=T0)
    with pytest.raises(ddpad.PadError, match="every Byteling is taken"):
        pad.hold("a9", "D9", "TD9", now=T0)
    assert {m["number"] for i in range(5) for m in pad.mints_of(f"a{i}")} == {None}, "still a surprise"
    assert not any(p["taken"] for p in pad.pieces()), "nor does the wall say which ones are held"
    for i in range(5):
        pad.paying(pad.mints_of(f"a{i}")[0]["id"], f"pay{i}", now=T0)
    assert all(p["taken"] for p in pad.pieces())


def test_a_new_stage_opens_every_thirty_days_after_the_pad_opens(pad):
    assert pad.evolve()["stages"] == []
    _inscribed(pad, Chain())
    ev = pad.evolve(now=T0 + 31 * ddpad.DAY)
    assert [s["stage"] for s in ev["stages"]] == ["Byte", "Kilo", "Mega", "Giga"]
    assert ev["stages"][0] == {"stage": "Byte", "from": int(T0) + 30 * ddpad.DAY, "open": True}
    assert ev["next"]["stage"] == "Kilo" and ev["next"]["from"] == int(T0) + 60 * ddpad.DAY


def test_the_cli_loads_sets_and_opens(tmp_path, capsys):
    home = tmp_path / "home"
    ddpad.main([str(home), "load", str(_build(tmp_path / "b"))])
    ddpad.main([str(home), "set", "price_cents=250", "inscribe=true"])
    cfg = ddpad.Pad(home).config()
    assert cfg["price_cents"] == 250 and cfg["inscribe"] is True
    with pytest.raises(ddpad.PadError):
        ddpad.main([str(home), "set", "colour=red"])
    assert not (home / ddpad.KEY).exists(), "the key is made when the arcade first uses it"
    assert '"total": 5' in capsys.readouterr().out


def test_the_pads_key_is_its_own_and_kept_private(tmp_path):
    key = ddpad.PadKey(tmp_path)
    assert len(key.pubkey) == 33
    assert (tmp_path / "ddpad.key").stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "trade.key").exists()


# --- a payment, read --------------------------------------------------------------------------------------------


def test_a_payment_is_read_as_the_dollars_it_pays_the_pads_own_address(tmp_path):
    key = ddpad.PadKey(tmp_path)
    rpc = SimpleNamespace(txs={})
    rpc.call = lambda method, txid, verbose: rpc.txs[txid]
    pay = SimpleNamespace(network="dgbtest", rpc=lambda: rpc)
    chain = SimpleNamespace(network="dgb", params=NETWORKS["dgb"], rpc=lambda: rpc)
    from arcade.web.ddpadweb import ArcadeEnv
    env = ArcadeEnv(SimpleNamespace(), ddpad.Pad(tmp_path), key, chain, pay)
    payer = ddpad.PadKey(tmp_path / "payer").pubkey
    prepared = ddtx.send(payer[1:], [{"txid": "aa" * 32, "vout": 0, "cents": 250}],
                         [{"txid": "bb" * 32, "vout": 1, "value": 5 * 10 ** 8}],
                         bytes.fromhex("76a914" + "11" * 20 + "88ac"),
                         ddtx.dd_key("dgbtest", env.pay_address), 100)
    vout = [{"n": n, "value": v / 1e8, "scriptPubKey": {"hex": s}} for n, (v, s) in enumerate(prepared["outputs"])]
    rpc.txs["t1"] = {"vout": vout, "confirmations": 0}
    assert env.payment("t1") == ("waiting", 100)
    rpc.txs["t1"]["confirmations"] = 2
    assert env.payment("t1") == ("confirmed", 100), "its $1.50 change back to the payer is not counted"

    def missing(*_):
        from arcade.rpc import RpcError
        raise RpcError(-5, "No such mempool or blockchain transaction")
    rpc.call = missing
    assert env.payment("t2") == ("missing", 0)


# --- the page and its routes -----------------------------------------------------------------------------------------

from test_account_claim import _sign_in                   # noqa: E402
from test_web import app_state, client                    # noqa: E402,F401


def test_the_pad_page_and_its_state_answer_before_anything_is_loaded(client):
    app, _ = client
    page = app.get("/bytelings")
    assert page.status_code == 200 and "Bytelings" in page.text and "Traceback" not in page.text
    got = app.get("/ddpad/state").json()
    assert got["open"] is False and got["pieces"] == [] and got["mine"] == []
    assert app.get("/ddpad/picture/1").status_code == 404


def test_the_pad_shows_its_pieces_and_refuses_a_mint_on_an_arcade_without_digibyte(client, tmp_path):
    app, state = client
    pad = ddpad.Pad(state.home)
    pad.load(_build(tmp_path / "build"))
    got = app.get("/ddpad/state").json()
    assert [p["number"] for p in got["pieces"]] == [1, 2, 3, 4, 5] and got["counts"]["total"] == 5
    picture = app.get("/ddpad/picture/3")
    assert picture.content == b"\x89PNG piece 3" and picture.headers["content-type"] == "image/png"
    assert app.post("/ddpad/mint", json={}).status_code in (401, 403), "a mint is an account's"
    _sign_in(app)
    refused = app.post("/ddpad/mint", json={"pubkey": "02" + "11" * 32})
    assert refused.status_code == 400 and "dgb" in refused.json()["detail"]

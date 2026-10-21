"""The Wallet's Doginals: the browser checks the account's own Dogecoin coins through this node, and one route sends a
Doginal on purpose (2026-10-14: "identify which UTXOs are doginals to make sure they are not spent unless somebody
is trying to send a doginal to someone else", "We have to have the wallet check its own utxo to see if they are
doginals"). The Doginals source is faked here; nothing in this file asks a real one."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_me_page import _seat                                   # noqa: E402
from test_funding import _pubkey                                 # noqa: E402
from test_dogecoin_network import _with_doge                     # noqa: E402

from arcade import doginals, funding                             # noqa: E402
from arcade.script import b58check_encode, hash160               # noqa: E402

T1, T2, T3 = "11" * 32, "22" * 32, "33" * 32
INS = T1 + "i0"
ME = b58check_encode(30, hash160(_pubkey(int("31" * 32, 16))))
THEM = b58check_encode(30, hash160(_pubkey(int("32" * 32, 16))))
PEP_THEM = b58check_encode(111, hash160(_pubkey(int("33" * 32, 16))))


def _doge_account(app, state, monkeypatch, tmp_path, answers=None):
    """A signed-in account with a Dogecoin address holding a Doginal, another Doginal, and one clean coin."""
    doge = _with_doge(state, monkeypatch, ready=True)
    _seat(app)
    said = app.post("/account/address", json={"address": ME, "chain": "doge"})
    assert said.status_code == 200, said.text
    with state.token_index(doge).open() as db:
        for txid, value in ((T1, 100_000), (T2, 100_000), (T3, 300_000_000)):
            db.conn.execute("INSERT OR REPLACE INTO utxo(txid,vout,address,value,height) VALUES (?,?,?,?,?)",
                            (txid, 0, ME, value, 1))
        db.conn.commit()
    calls = []
    answers = answers if answers is not None else {
        T1: {"txid": T1, "inscriptions": [{"vout": 0, "inscription_id": INS, "sat_offset": 0}]},
        T2: {"txid": T2, "inscriptions": [{"vout": 0, "inscription_id": T2 + "i0", "sat_offset": 0}]},
        T3: {"txid": T3, "inscriptions": []}}

    def fetch(txid):
        calls.append(txid)
        if txid not in answers:
            raise OSError("down")
        return answers[txid]
    monkeypatch.setattr(funding, "GUARD", doginals.Doginals(tmp_path / "dg.sqlite", fetch=fetch))
    return doge, calls


def test_the_account_lists_its_own_dogecoin_coins_and_asks_nobody(client, monkeypatch, tmp_path):
    app, state = client
    _, calls = _doge_account(app, state, monkeypatch, tmp_path)
    got = app.get("/account/coins?chain=doge")
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["doge"] is True and body["address"] == ME
    assert {(c["txid"], c["vout"], c["value"]) for c in body["coins"]} == \
        {(T1, 0, 100_000), (T2, 0, 100_000), (T3, 0, 300_000_000)}
    assert calls == [], "the browser asks about these, not the server"


def test_the_wallet_page_has_the_doginals_section_and_the_configured_base(client, monkeypatch, tmp_path):
    app, state = client
    _doge_account(app, state, monkeypatch, tmp_path)
    page = app.get("/me/wallet").text
    # Read from the chain by this node; nothing outside the arcade is asked (2026-10-15: the outside Doginals API removed).
    assert 'id="doginals"' in page and "dogesoft" not in page.lower()
    # Through this node (2026-10-15): the browser asks nothing outside this arcade.
    assert "/account/doginal-tx/${txid}?chain=" in page and "localStorage.setItem(DG_TX + net" in page
    assert "Inscriptions can't be checked right now" in page and "/content/${encodeURIComponent(g.id)}" in page
    assert '"/account/doginal/send"' in page and "/account/coins?chain=" in page


def _spy(monkeypatch):
    built = []
    real = funding.build_doginal_send

    def spy(*a, **k):
        built.append(real(*a, **k))
        return built[-1]
    monkeypatch.setattr(funding, "build_doginal_send", spy)
    return built


def test_a_doginal_is_sent_first_and_whole_and_fees_never_come_from_another(client, monkeypatch, tmp_path):
    app, state = client
    _doge_account(app, state, monkeypatch, tmp_path)
    built = _spy(monkeypatch)
    got = app.post("/account/doginal/send", json={"chain": "doge", "txid": T1, "vout": 0, "inscription": INS,
                                                  "to": THEM})
    assert got.status_code == 200, got.text
    offer = got.json()
    assert offer["inscription"] == INS and offer["to"] == THEM and offer["chain"] == "doge"
    assert offer["amount"] == 100_000 and offer["offer"]
    ins = [(i["txid"], i["vout"]) for i in offer["inputs"]]
    assert ins[0] == (T1, 0), "the Doginal's coin is input 0"
    assert (T2, 0) not in ins, "the other Doginal never pays the fee"
    assert ins[1:] == [(T3, 0)]
    u = built[0]
    assert u.outputs[0] == (100_000, funding.p2pkh_script(THEM)), "the whole coin to the recipient, output 0"
    assert u.outputs[-1][1] == funding.p2pkh_script(ME), "change last, back home"
    assert sum(c["value"] for c in u.inputs) == sum(v for v, _ in u.outputs) + u.fee
    assert len(offer["sighashes"]) == len(offer["inputs"]), "signed in the tab, like every account send"


def test_a_coin_that_does_not_hold_exactly_that_doginal_is_refused(client, monkeypatch, tmp_path):
    app, state = client
    _doge_account(app, state, monkeypatch, tmp_path)
    for txid, ins in ((T3, T3 + "i0"), (T2, INS), (T1, T1 + "i1")):
        got = app.post("/account/doginal/send", json={"chain": "doge", "txid": txid, "vout": 0, "inscription": ins,
                                                      "to": THEM})
        assert got.status_code == 400 and "exactly that Doginal" in got.json()["detail"], (txid, got.text)


def test_a_coin_that_is_not_the_accounts_is_refused(client, monkeypatch, tmp_path):
    app, state = client
    _doge_account(app, state, monkeypatch, tmp_path)
    got = app.post("/account/doginal/send", json={"chain": "doge", "txid": "44" * 32, "vout": 0,
                                                  "inscription": "44" * 32 + "i0", "to": THEM})
    assert got.status_code == 400 and "not this account's" in got.json()["detail"]


def test_the_index_away_is_said_never_guessed(client, monkeypatch, tmp_path):
    app, state = client
    _doge_account(app, state, monkeypatch, tmp_path, answers={})
    got = app.post("/account/doginal/send", json={"chain": "doge", "txid": T1, "vout": 0, "inscription": INS,
                                                  "to": THEM})
    assert got.status_code == 503 and "can't be checked right now" in got.json()["detail"]


def test_a_doginal_send_is_refused_off_dogecoin(client, monkeypatch, tmp_path):
    app, state = client
    _doge_account(app, state, monkeypatch, tmp_path)
    for chain in ("", state.messaging.network):
        got = app.post("/account/doginal/send", json={"chain": chain, "txid": T1, "vout": 0, "inscription": INS,
                                                      "to": PEP_THEM})
        assert got.status_code == 400 and "Doginals live on Dogecoin" in got.json()["detail"], got.text


def test_a_pepecoin_address_is_refused_as_the_recipient(client, monkeypatch, tmp_path):
    app, state = client
    _doge_account(app, state, monkeypatch, tmp_path)
    got = app.post("/account/doginal/send", json={"chain": "doge", "txid": T1, "vout": 0, "inscription": INS,
                                                  "to": PEP_THEM})
    assert got.status_code == 400


def test_the_public_door_lets_both_routes_through():
    """A route the door does not know 404s on the public site while every local test passes."""
    from arcade.web import door
    assert "/account/coins" in door.PUBLIC_PAGES and "/account/doginal/send" in door.PUBLIC_POST


def test_the_node_reads_the_accounts_own_coins_only(client, monkeypatch, tmp_path):
    """The Wallet's Doginals check goes through this node, read from the chain: one answer shape,
    kept for good, and only for a transaction holding one of this account's own coins."""
    app, state = client
    _, calls = _doge_account(app, state, monkeypatch, tmp_path)
    got = app.get(f"/account/doginal-tx/{T1}")
    assert got.status_code == 200, got.text
    assert got.json() == {"txid": T1, "inscriptions": [{"vout": 0, "inscription_id": INS, "sat_offset": 0,
                                                        "unknown": False}]}
    assert app.get(f"/account/doginal-tx/{T3}").json()["inscriptions"] == []
    app.get(f"/account/doginal-tx/{T1}")
    assert calls.count(T1) == 1, "asked once, kept for good"
    assert app.get(f"/account/doginal-tx/{'99' * 32}").status_code == 403, "not this account's: nobody's free client"
    assert app.get("/account/doginal-tx/nope").status_code == 400


def test_the_index_away_says_so_through_the_node(client, monkeypatch, tmp_path):
    app, state = client
    _doge_account(app, state, monkeypatch, tmp_path, answers={})
    assert app.get(f"/account/doginal-tx/{T1}").status_code == 503


def test_a_coin_carrying_a_doginal_is_held_not_spendable(client, monkeypatch, tmp_path):
    """2026-10-15: @apple's wallet said 0.01 DOGE to spend, and that coin was Anti Doginal Dog #1089. A coin carrying an
    inscription is held: left out of the big number, and said beside it."""
    app, state = client
    _doge_account(app, state, monkeypatch, tmp_path)
    row = next(c for c in app.get("/account").json()["chains"] if c["network"] == "doge")
    assert row["inscribed"] == 200_000, "the two Doginals' coins (T1, T2)"
    assert row["spendable"] == row["balance"] - 200_000 - int(row.get("leaving") or 0) + int(row.get("incoming") or 0)
    assert row["spendable"] == 300_000_000, "the plain coin is still spendable"
    page = app.get("/me/wallet").text
    assert ('held in ${c.network === "doge" ? "Doginals" : (c.network === "ltc" || c.network === "ltctest") ? '
            '"Litecoin inscriptions" : c.network === "main" ? "Pepinals" : "inscriptions"}') in page


def test_someone_elses_wallet_shows_their_doginals_with_an_offer(client, monkeypatch, tmp_path):
    """2026-10-15: "We should be able to see doginals in others wallets. To make offers and all of them." Their
    Dogecoin address (published with their key), its Doginals read from the chain, each with Make an offer."""
    from arcade.db import Database
    from arcade.state import install_schema
    app, state = client
    _doge_account(app, state, monkeypatch, tmp_path)           # ME's Dogecoin coins: a Doginal (T1), another (T2), a plain one
    them = b58check_encode(state.messaging.params.pubkeyhash_version, hash160(_pubkey(int("41" * 32, 16))))
    db = Database(state.home / f"{state.messaging.network}-ledger.sqlite")
    install_schema(db)
    db.conn.execute("INSERT INTO tag(tag,address,claimed_txid,block_height,position) VALUES('them',?,?,100,0)",
                    (them, "t" * 64))
    db.conn.commit()
    db.close()
    with state.store() as store:
        store.add_key_announcement("c1" * 32, them, bytes(32), "fp", 100, 1, stated=True, doge_address=ME)
    page = " ".join(app.get("/u/them/wallet").text.split())
    assert f'data-ord="{INS}"' in page and f'src="/content/{INS}"' in page, "their Doginal, its art from the chain"
    assert f"/exchange/swap/doge-doginal-{INS}/doge-coin?side=buy#x-form" in page and "Make an offer" in page
    assert f'data-ord="{T2}i0"' in page, "every one of them"


def test_moving_a_doginal_needs_dogecoin_besides_it(client, monkeypatch, tmp_path):
    """2026-10-15: "If the user doesn't have any Dogecoin in their wallet, and their only Doge coin is a Doginal
    they can't make a swap. Those buttons should be grayed out unless they have a balance of Dogecoin on top of their
    asset. To pay the miner fee." The node says what a send and a swap's lock need; the pages compare it with what is
    spendable (the Doginals left out)."""
    from arcade import htlc
    app, state = client
    _doge_account(app, state, monkeypatch, tmp_path)
    row = next(c for c in app.get("/account").json()["chains"] if c["network"] == "doge")
    assert 1_000_000 < row["doginal_send_need"] < row["doginal_lock_need"]
    assert row["doginal_lock_need"] > htlc.DOGINAL_RESERVE, "the reserve and the lock's own fee"
    wallet = app.get("/me/wallet").text
    assert "sendShort" in wallet and "swapShort" in wallet and 'swap.setAttribute("aria-disabled", "true")' in wallet
    market = (pathlib.Path(__file__).resolve().parent.parent / "arcade/web/templates/swap_pair.html").read_text()
    assert 'id="dg-need"' in market and "doginal_lock_need" in market, "a visitor's Sell and List sale wait for DOGE too"


def test_machine_records_are_not_anybodys_nfts(tmp_path):
    """2026-10-17: order batches (application/vnd.arcade.orders+json) showed in wallets as NFTs."""
    from arcade.ledger import MACHINE_TYPES
    import fnmatch
    assert fnmatch.fnmatch("application/vnd.arcade.orders+json", MACHINE_TYPES.replace("%", "*"))
    assert not fnmatch.fnmatch("image/png", MACHINE_TYPES.replace("%", "*"))

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
    assert '"/account/doginal-txs"' in page and "localStorage.setItem(DG_TX + net" in page
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
    assert "/account/doginal-txs" in door.PUBLIC_POST


def test_the_node_reads_the_accounts_own_coins_only(client, monkeypatch, tmp_path):
    """The Wallet's Doginals check goes through this node, read from the chain: one answer shape,
    kept for good, and only for a transaction holding one of this account's own coins."""
    app, state = client
    _, calls = _doge_account(app, state, monkeypatch, tmp_path)
    got = app.get(f"/account/doginal-tx/{T1}")
    assert got.status_code == 200, got.text
    assert got.json() == {"txid": T1, "inscriptions": [{"vout": 0, "inscription_id": INS, "sat_offset": 0,
                                                        "unknown": False, "wait": False, "ctype": None}]}
    assert app.get(f"/account/doginal-tx/{T3}").json()["inscriptions"] == []
    app.get(f"/account/doginal-tx/{T1}")
    assert calls.count(T1) == 1, "asked once, kept for good"
    assert app.get(f"/account/doginal-tx/{'99' * 32}").status_code == 403, "not this account's: nobody's free client"
    assert app.get("/account/doginal-tx/nope").status_code == 400


def test_many_transactions_are_read_in_one_call_own_coins_only(client, monkeypatch, tmp_path):
    """2026-10-21, 1,500 Doginals in one wallet, "whatever is lightest weight for the server": up to a hundred
    transactions a call, each read once for good; someone else's never answered, an unreadable one said as null."""
    app, state = client
    _, calls = _doge_account(app, state, monkeypatch, tmp_path, answers={
        T1: {"txid": T1, "inscriptions": [{"vout": 0, "inscription_id": INS, "sat_offset": 0}]},
        T3: {"txid": T3, "inscriptions": []}})
    # Its content type, kept beside its content once read (2026-10-22: the NFTs leave text, JSON, scripts out).
    from arcade import funding
    kept = funding.GUARD.path.parent / "inscription-content"
    kept.mkdir(parents=True, exist_ok=True)
    (kept / (INS + ".type")).write_text("text/plain;charset=utf-8")
    got = app.post("/account/doginal-txs", json={"chain": "doge", "txids": [T1, T2, T3, "99" * 32]})
    assert got.status_code == 200, got.text
    txs = got.json()["txs"]
    assert txs[T1] == [{"vout": 0, "inscription_id": INS, "sat_offset": 0, "unknown": False, "wait": False,
                        "h": None, "ctype": "text/plain;charset=utf-8"}]
    assert txs[T3] == [] and txs[T2] is None and "99" * 32 not in txs
    app.post("/account/doginal-txs", json={"chain": "doge", "txids": [T1]})
    assert calls.count(T1) == 1, "asked once, kept for good"
    assert app.post("/account/doginal-txs", json={"chain": "doge", "txids": ["nope"]}).status_code == 400
    assert app.post("/account/doginal-txs", json={"chain": "doge", "txids": [T1] * 101}).status_code == 400
    assert app.post("/account/doginal-txs", json={"chain": "doge", "txids": [T1],
                                                   "address": THEM}).status_code == 403, "someone else's wallet"


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
    assert ('const inscWord = c.network === "doge" ? "Doginals" : (c.network === "ltc" || c.network === "ltctest") ? '
            '"Litecoin inscriptions" : c.network === "main" ? "Pepinals"') in page
    assert ': c.network === "bel" ? "Bellinals" : c.network === "lky" ? "Luckinals" : c.network === "wjk" ? "Wojakinals" : "inscriptions";' in page
    assert "held in ${inscWord}" in page


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


def test_the_wallet_asks_how_far_the_ord_index_has_walked(client, monkeypatch, tmp_path):
    """2026-10-22: "can the Wallet view be updated in real time as the index is built?" -- the page asks how far
    the index has got, and asks again only about the coins it has walked past."""
    from arcade import funding
    from arcade.web import door
    app, state = client
    _doge_account(app, state, monkeypatch, tmp_path)
    monkeypatch.setattr(funding.GUARD, "index_reach", lambda: 4_700_000)
    monkeypatch.setattr(funding.GUARD, "_tip", lambda: 6_426_000)
    got = app.get("/account/doginal-index?chain=doge")
    assert got.status_code == 200, got.text
    assert got.json() == {"chain": "doge", "reach": 4_700_000, "tip": 6_426_000}
    assert "/account/doginal-index" in door.PUBLIC_PAGES
    page = app.get("/me/wallet").text
    assert "/account/doginal-index?chain=" in page and "shown here as the index gets to each" in page


def test_the_tokens_tab_reads_drc20_balances_from_the_most_complete_index(client, monkeypatch, tmp_path):
    """2026-10-22: "under Wallet tokens tab DRC-20 and PRC-20 balances should be displayed updated as the index
    is updated" -- asked of the index, with how far it has walked; an address not the account's own is refused."""
    from arcade.web import door
    app, state = client
    _doge_account(app, state, monkeypatch, tmp_path)
    asked = []

    class Index:
        tip = 6_426_000

        def lookup(self, q):
            asked.append(q)
            return {"balances": [{"tick": "dogi", "units": "150000", "transferable_units": "0", "decimals": 2}],
                    "walked_to": 4_750_000, "from": "arcade 8c95b1358339"}
    monkeypatch.setattr(type(state), "ord_indexes", lambda self: {"doge": Index()})
    got = app.get("/account/token20?chain=doge")
    assert got.status_code == 200, got.text
    said = got.json()
    assert said["protocol"] == "drc-20" and said["address"] == ME and said["walked_to"] == 4_750_000
    assert said["balances"][0]["tick"] == "dogi" and said["tip"] == 6_426_000
    assert asked == [{"what": "balances", "owner": ME}]
    assert app.get(f"/account/token20?chain=doge&address={'D' * 34}").status_code == 403
    assert "/account/token20" in door.PUBLIC_PAGES
    assert "these balances update as it walks" in app.get("/me/wallet").text


def test_a_drc20_send_is_three_offers_signed_and_broadcast_one_after_the_other(client, monkeypatch, tmp_path):
    """2026-10-22: send a DRC-20 / PRC-20 token "in the same way I can send any other token", one Send press.
    The balance is checked against a complete index; the commit pays the note's P2SH script; once it is out the
    reveal spends it, the note on this account's own address; once that is out the send moves that coin, whole, to
    the recipient, fees from the clean coin; then "done"."""
    from arcade import rawchain, token20_send as t20
    from arcade.txbuild import p2pkh_script
    app, state = client
    doge, _ = _doge_account(app, state, monkeypatch, tmp_path)
    them = b58check_encode(30, hash160(_pubkey(int("41" * 32, 16))))

    class Index:
        tip = 5_000_000

        def lookup(self, q):
            assert q == {"what": "balances", "owner": ME}
            return {"balances": [{"tick": "dogi", "units": "1500", "transferable_units": "200", "decimals": 2}],
                    "walked_to": 4_999_999}
    monkeypatch.setattr(type(state), "ord_indexes", lambda self: {"doge": Index()})
    sent: dict = {}
    mined: dict = {}

    class Rpc:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def call(self, method, *args):
            if method == "decoderawtransaction":
                tx = rawchain.read_tx(bytes.fromhex(args[0]))
                return {"vin": [{"txid": i["txid"], "vout": i["vout"]} for i in tx.vin],
                        "vout": [{"n": n, "value": v / 1e8, "scriptPubKey": {"hex": s.hex()}}
                                 for n, (v, s) in enumerate(tx.vout)]}
            if method == "sendrawtransaction":
                tx = rawchain.read_tx(bytes.fromhex(args[0]))
                sent[tx.txid] = args[0]
                return tx.txid
            if method == "getrawtransaction":
                tx = rawchain.read_tx(bytes.fromhex(sent[args[0]]))
                return {"txid": tx.txid, "confirmations": mined.get(args[0], 0),
                        "vout": [{"n": n, "value": v / 1e8, "scriptPubKey": {"hex": s.hex()}}
                                 for n, (v, s) in enumerate(tx.vout)]}
            raise AssertionError(method)

        def get_block_count(self):
            return 5_000_000
    real_rpc = type(doge).rpc
    monkeypatch.setattr(type(doge), "rpc", lambda self: Rpc() if self.network == "doge" else real_rpc(self))

    def sign(offer):
        done = app.post("/account/sign", json={"offer": offer["offer"], "signatures": ["30" * 71] * len(
            offer["sighashes"]), "pubkey": _pubkey(int("31" * 32, 16)).hex()})
        assert done.status_code == 200, done.text
        return done.json()["txid"]

    key = _pubkey(int("31" * 32, 16)).hex()
    wrong = app.post("/account/token20/send", json={"chain": "doge", "tick": "DOGI", "amount": "1", "to": them,
                                                     "pubkey": _pubkey(int("41" * 32, 16)).hex()})
    assert wrong.status_code == 400 and "not the key of this account's address" in wrong.json()["detail"]
    too_much = app.post("/account/token20/send", json={"chain": "doge", "tick": "DOGI", "amount": "13.01", "to": them,
                                                        "pubkey": key})
    assert too_much.status_code == 400 and "more dogi" in too_much.json()["detail"], "1500 - 200 set aside = 13.00"
    first = app.post("/account/token20/send", json={"chain": "doge", "tick": "DOGI", "amount": "12.5", "to": them,
                                                     "pubkey": key})
    assert first.status_code == 200, first.text
    c = first.json()
    assert c["step"] == "commit" and c["note"] == '{"p":"drc-20","op":"transfer","tick":"dogi","amt":"12.5"}'
    pushes, redeem = t20.envelope(c["note"].encode(), _pubkey(int("31" * 32, 16)))
    commit = sign(c)
    out = rawchain.read_tx(bytes.fromhex(sent[commit])).vout[0]
    assert out[1] == t20.commit_script(redeem) and out[0] == t20.POSTAGE + c["fees"]["reveal"]
    r = app.post("/account/token20/send", json={"send": c["send"]}).json()
    assert r["step"] == "reveal" and r["inputs"][0]["txid"] == commit and r["inputs"][0]["redeem"] == redeem.hex()
    reveal = sign(r)
    raw = rawchain.read_tx(bytes.fromhex(sent[reveal]))
    assert raw.vin[0]["script"].startswith(b"".join(pushes)), "the envelope in front of the signature"
    assert raw.vout == [(t20.POSTAGE, p2pkh_script(ME))], "the note lands on this account's own address"
    s = app.post("/account/token20/send", json={"send": c["send"]}).json()
    assert s["step"] == "send" and (s["inputs"][0]["txid"], s["inputs"][0]["vout"]) == (reveal, 0)
    assert s["inputs"][1]["txid"] == commit, "the fee from the commit's change (it spent the clean coin), never a Doginal's"
    final = sign(s)
    tx = rawchain.read_tx(bytes.fromhex(sent[final]))
    assert tx.vout[0] == (t20.POSTAGE, p2pkh_script(them)), "the note's coin, whole, to them as output 0"
    assert app.post("/account/token20/send", json={"send": c["send"]}).json()["step"] == "done"
    # 2026-10-22: "It should remove the amount sent from the balance total until it can be updated from the
    # chain" -- 12.5 held back (1300 on hand less 1250), and no second send of the same tokens.
    row = app.get("/account/token20?chain=doge").json()["balances"][0]
    assert row["pending_units"] == "1250", row
    again = app.post("/account/token20/send", json={"chain": "doge", "tick": "dogi", "amount": "1", "to": them,
                                                     "pubkey": key})
    assert again.status_code == 400 and "on its way" in again.json()["detail"], again.text
    assert app.post("/account/token20/send", json={"chain": "doge", "tick": "dogi", "amount": "0.5", "to": them,
                                                    "pubkey": key}).status_code == 200, "what is left can still go"
    mined[reveal] = 1                                        # the note in the tip's block: not read by the index yet
    assert app.get("/account/token20?chain=doge").json()["balances"][0]["pending_units"] == "1250"
    mined[reveal] = 3                                        # block 4,999,998: the index (at 4,999,999) walked past it
    Index.lookup = lambda self, q: {"balances": [{"tick": "dogi", "units": "250", "transferable_units": "200",
                                                  "decimals": 2}], "walked_to": 5_000_000}
    assert app.get("/account/token20?chain=doge").json()["balances"][0]["pending_units"] == "0", \
        "read from the chain: the index's own balance says it now"


def test_a_token_lock_is_four_offers_and_ends_with_the_note_on_the_lock(client, monkeypatch, tmp_path):
    """2026-10-22, PRC-20 / DRC-20 on the exchange (token20_swap): note 1 on this account's own address
    (commit, reveal), the funding (note 1 to the lock, note 2's commit), then the lock (note 2 on the lock, the reserve
    beside it), each signed and broadcast before the next; the amount held off the balance from the first."""
    from arcade import htlc as H, rawchain, token20_send as t20, token20_swap as t20swap
    from arcade.txbuild import p2pkh_script
    app, state = client
    doge, _ = _doge_account(app, state, monkeypatch, tmp_path)
    key = _pubkey(int("31" * 32, 16))
    assert app.post("/account/address", json={"address": ME, "chain": "doge", "coin_pubkey": key.hex()}).status_code \
        == 200
    buyer = _pubkey(int("41" * 32, 16))

    class Index:
        tip = 5_000_000

        def lookup(self, q):
            return {"balances": [{"tick": "dogi", "units": "1500", "transferable_units": "0", "decimals": 2}],
                    "walked_to": 4_999_999}
    monkeypatch.setattr(type(state), "ord_indexes", lambda self: {"doge": Index()})
    sent: dict = {}

    class Rpc:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def call(self, method, *args):
            tx = rawchain.read_tx(bytes.fromhex(args[0] if method != "getrawtransaction" else sent[args[0]]))
            vout = [{"n": n, "value": v / 1e8, "scriptPubKey": {"hex": s.hex()}} for n, (v, s) in enumerate(tx.vout)]
            if method == "decoderawtransaction":
                return {"vin": [{"txid": i["txid"], "vout": i["vout"]} for i in tx.vin], "vout": vout}
            if method == "sendrawtransaction":
                sent[tx.txid] = args[0]
                return tx.txid
            if method == "getrawtransaction":
                return {"txid": tx.txid, "confirmations": 0, "vout": vout}
            raise AssertionError(method)

        def get_block_count(self):
            return 5_000_000
    real_rpc = type(doge).rpc
    monkeypatch.setattr(type(doge), "rpc", lambda self: Rpc() if self.network == "doge" else real_rpc(self))

    def sign(offer):
        done = app.post("/account/sign", json={"offer": offer["offer"], "signatures": ["30" * 71] * len(
            offer["sighashes"]), "pubkey": key.hex()})
        assert done.status_code == 200, done.text
        return done.json()["txid"]

    secret, digest = H.make_secret()
    redeem = H.redeem_script(digest, buyer, key, 1_900_000_000)
    leg = {"kind": "token20", "asset": "dogi", "amount": 12}
    too_much = app.post("/account/trade/lock20", json={"chain": "doge", "redeem": redeem.hex(),
                                                       "leg": {**leg, "amount": 16}})
    assert too_much.status_code == 400 and "more dogi" in too_much.json()["detail"], too_much.text
    theirs = H.redeem_script(digest, key, buyer, 1_900_000_000)
    assert "refund somebody else" in app.post("/account/trade/lock20", json={
        "chain": "doge", "redeem": theirs.hex(), "leg": leg}).json()["detail"]
    c = app.post("/account/trade/lock20", json={"chain": "doge", "redeem": redeem.hex(), "leg": leg}).json()
    assert c["step"] == "commit" and c["note"] == '{"p":"drc-20","op":"transfer","tick":"dogi","amt":"12"}', c
    assert c["lock"] == H.address_of(redeem, doge.params)
    sign(c)
    assert app.get("/account/token20?chain=doge").json()["balances"][0]["pending_units"] == "1200", "held from now"
    r = app.post("/account/trade/lock20", json={"lock20": c["lock20"]}).json()
    assert r["step"] == "reveal"
    reveal = sign(r)
    assert rawchain.read_tx(bytes.fromhex(sent[reveal])).vout == [(t20.POSTAGE, p2pkh_script(ME))]
    f = app.post("/account/trade/lock20", json={"lock20": c["lock20"]}).json()
    assert f["step"] == "fund" and (f["inputs"][0]["txid"], f["inputs"][0]["vout"]) == (reveal, 0)
    fund = sign(f)
    outs = rawchain.read_tx(bytes.fromhex(sent[fund])).vout
    assert outs[0] == (t20.POSTAGE, H.p2sh_script(redeem)), "note 1, whole, to the lock: its first move"
    pushes, note_redeem = t20.envelope(c["note"].encode(), key)
    assert outs[1][1] == t20.commit_script(note_redeem), "and note 2's commit"
    k = app.post("/account/trade/lock20", json={"lock20": c["lock20"]}).json()
    assert k["step"] == "lock" and (k["inputs"][0]["txid"], k["inputs"][0]["vout"]) == (fund, 1)
    lock = sign(k)
    tx = rawchain.read_tx(bytes.fromhex(sent[lock]))
    assert tx.vin[0]["script"].startswith(b"".join(pushes)), "note 2's envelope"
    assert [o for o in tx.vout] == t20swap.lock_outputs(redeem, H.DOGINAL_RESERVE), "note 2 on the lock, reserve beside"
    done = app.post("/account/trade/lock20", json={"lock20": c["lock20"]}).json()
    assert done["step"] == "done" and done["lock_txid"] == lock and done["vout"] == 0


def test_a_listing_holds_what_it_lists_until_it_is_unlisted_or_taken(client, monkeypatch, tmp_path):
    """2026-10-22: "I made a listing of frgy from my @apple but it remains in my balance ... It should be locked
    as listed until it is unlisted or purchased" -- and "all tokens and all coins listed", "And NFTs listed". A swap
    order of a DRC-20 / PRC-20 token or a Doginal stays on the account's address until taken, so it is held: off the
    balance, off a send, and a listing past what is free is refused."""
    import json as _json
    app, state = client
    _doge_account(app, state, monkeypatch, tmp_path)
    them = b58check_encode(30, hash160(_pubkey(int("41" * 32, 16))))

    class Index:
        tip = 5_000_000

        def lookup(self, q):
            return {"balances": [{"tick": "dogi", "units": "1500", "transferable_units": "0", "decimals": 2}],
                    "walked_to": 4_999_999}
    monkeypatch.setattr(type(state), "ord_indexes", lambda self: {"doge": Index()})
    held: list = []
    monkeypatch.setattr(type(state.trade_orders), "standing_of", lambda self, listers, now=None: list(held))
    key = _pubkey(int("31" * 32, 16)).hex()
    held.append({"id": "ord-1", "give": {"network": "doge", "kind": "token20", "asset": "dogi", "amount": 10},
                 "get": {"network": "main", "kind": "coin", "amount": 1}})
    row = app.get("/account/token20?chain=doge").json()["balances"][0]
    assert row["listed_units"] == "1000", row
    sent = app.post("/account/token20/send", json={"chain": "doge", "tick": "dogi", "amount": "6", "to": them,
                                                    "pubkey": key})
    assert sent.status_code == 400 and "listed on the exchange" in sent.json()["detail"], sent.text
    assert app.post("/account/token20/send", json={"chain": "doge", "tick": "dogi", "amount": "5", "to": them,
                                                    "pubkey": key}).status_code == 200, "what is not listed can go"
    order = lambda i, n: {"id": i, "give": {"network": "doge", "kind": "token20", "asset": "dogi", "amount": n},  # noqa
                          "get": {"network": "main", "kind": "coin", "amount": 100}}
    acct = state.messaging
    lister = b58check_encode(acct.params.pubkeyhash_version, hash160(_pubkey(int("31" * 32, 16))))
    assert app.post("/account/address", json={"address": lister, "chain": acct.network}).status_code == 200
    over = app.post("/account/orders/post", json={"orders": [order("ord-1", 10), order("ord-2", 6)]})
    # 2026-10-23: the rest of the book is filed, so it answers 200 and says what came off (a 409 told @apple a PEP
    # listing had failed, over an old frgy order of theirs); the tab forgets what came off.
    assert over.status_code == 200 and "more dogi than this wallet has free" in over.json()["notice"], over.text
    assert over.json()["dropped"] == ["ord-2"], "the first that fit stays; the one past what is free comes off"
    assert "ord-2" in over.json()["forget"]
    # 2026-10-22: a 1,000 frgy listing went on as 100,000,000,000 -- a standing order past everything comes off.
    huge = app.post("/account/orders/post", json={"orders": [order("ord-9", 100_000_000_000)]})
    assert huge.status_code == 200 and huge.json()["dropped"] == ["ord-9"]
    # And once cancelled, a tab that still lists it is told to forget it -- it is not dropped again and again.
    monkeypatch.setattr(type(state.trade_orders), "cancelled_of",          # the book holds ord-1 as cancelled
                        lambda self, who, ids: [i for i in ids if i == "ord-1"])
    again = app.post("/account/orders/post", json={"orders": [order("ord-1", 10)]})
    assert again.status_code == 200 and again.json()["forget"] == ["ord-1"] and "notice" not in again.json(), again.text
    # The Doginal on a swap order cannot be sent meanwhile.
    held.append({"id": "ord-3", "give": {"network": "doge", "kind": "doginal", "asset": INS, "amount": 1},
                 "get": {"network": "main", "kind": "coin", "amount": 1}})
    got = app.post("/account/doginal/send", json={"chain": "doge", "txid": T1, "vout": 0, "inscription": INS,
                                                   "to": them})
    assert got.status_code == 400 and "listed for sale" in got.json()["detail"], got.text
    assert app.get("/account/listed").json() == {"doginals": {"doge": [INS]}, "token20": {"doge": {"dogi": 10}}}
    held.clear()
    assert app.get("/account/token20?chain=doge").json()["balances"][0]["listed_units"] == "0", "unlisted: free again"
    _ = _json

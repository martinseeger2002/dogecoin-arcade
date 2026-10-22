"""Every wallet address on the chain, and any wallet hidden or shown (2026-10-18: "the user should be able to
enable or disable any of the chain wallets. If they are disabled, they still should get the Wallet addresses assigned,
but it just won't be visible in their Wallet tab. Make sure that we are now broadcasting all of the Wallet addresses
when publishing to the chain. Instead of only Pepe test Pepe and Doge.")."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                                 # noqa: F401,E402

from arcade.messaging import envelope as E                             # noqa: E402
from arcade.web import door                                            # noqa: E402

KEY = bytes(range(32))
ME, OTHER, DOGE, LTC, LTCT = (bytes([n]) * 20 for n in (0x11, 0x22, 0x33, 0x44, 0x55))


def test_every_chains_address_goes_out_after_the_rest():
    body = E.build_key_announcement(KEY, hash160=ME, other_hash160=OTHER, tag="apple", doge_hash160=DOGE,
                                    chains={"ltc": LTC, "ltctest": LTCT})
    said = E.parse_announced_extras(body)
    assert said["chains"] == {"ltc": LTC, "ltctest": LTCT} and said["doge_hash160"] == DOGE
    assert said["tag"] == "apple"
    # An older reader stops at the first section it does not know, with everything before it read.
    cut = body[:body.index(bytes([E.ANNOUNCE_TAG_CHAIN, 3]) + b"ltc")]
    old = E.parse_announced_extras(cut)
    assert old["doge_hash160"] == DOGE and old["tag"] == "apple" and old["chains"] == {}


def test_the_store_keeps_them_and_a_newer_reader_fills_them_in(client):
    _app, state = client
    with state.store() as store:
        store.add_key_announcement("aa" * 32, "nAddr", KEY, "fp", 10, 1, stated=True)
        store.add_key_announcement("aa" * 32, "nAddr", KEY, "fp", 10, 1, stated=True,
                                   chain_addresses=json.dumps({"ltc": "LTCaddr"}))
        assert json.loads(store.key_for("nAddr")["chain_addresses"]) == {"ltc": "LTCaddr"}


def test_a_wallet_hidden_is_kept_out_of_the_wallet_tab_but_its_address_stays(client):
    from test_me_page import _seat
    app, state = client
    _seat(app)
    net = state.messaging.network
    nets = [c.network for c in state.all_chains]
    for other in nets[:-1]:
        assert app.post("/account/wallets", json={"network": other, "shown": False}).status_code == 200
    last = app.post("/account/wallets", json={"network": nets[-1], "shown": False})
    assert last.status_code == 400 and "at least one" in last.json()["detail"], "the last wallet stays shown"
    assert "/account/wallets" in door.PUBLIC_POST, "a public arcade lets the route through"
    me = app.get("/me").text
    assert 'id="wallet-chains"' in me and f'data-network="{net}"' in me


def test_each_chain_says_its_node_operators_and_numbers(client):
    from test_me_page import _seat
    app, state = client
    state.public = True
    assert app.get("/account/chain-stats").status_code == 403, "signed in only, through the door"
    state.public = False                                     # the operator's own Overview reads them too (2026-10-19)
    assert app.get("/account/chain-stats").status_code == 200
    _seat(app)
    said = app.get("/account/chain-stats").json()
    assert set(said) == {c.network for c in state.all_chains}
    for net, row in said.items():
        assert row["here"] is True and row["operators"] >= 1, "a chain run here counts this arcade"
        assert {"online", "operators", "here"} <= set(row)
    assert "/account/chain-stats" in door.PUBLIC_PAGES, "a public arcade lets the route through"


def test_your_arcade_shows_wallet_coins_lit_or_grey_and_the_arcades_card(client):
    """2026-10-21: "the Wallet selection should just be a row of the icons and have them lit up when enabled
    and grayed out when disabled. Below that should be a copy of the arcades card in the admin overview page"."""
    from test_me_page import _seat
    app, state = client
    _seat(app)
    nets = [c.network for c in state.all_chains]
    assert app.post("/account/wallets", json={"network": nets[0], "shown": False}).status_code == 200
    me = app.get("/me").text
    assert f'data-network="{nets[0]}" aria-pressed="false"' in me, "hidden: greyed"
    assert f'data-network="{nets[-1]}" aria-pressed="true"' in me, "shown: lit"
    assert 'type="checkbox" class="wallet-chain"' not in me and 'class="chain-stats' not in me
    assert 'id="arcades"' in me and "/account/arcades" in me and "window.arcadesPanel" in me
    said = app.get("/account/arcades")
    assert said.status_code == 200 and said.json()["rows"][0]["me"] is True
    assert all("switch" not in c for r in said.json()["rows"] for c in r["cores"]), "the operator's switches stay theirs"
    assert "/account/arcades" in door.PUBLIC_PAGES
    state.public = True
    app.cookies.clear()
    assert app.get("/account/arcades").status_code in (401, 403), "signed in only"
    state.public = False


def test_hiding_one_of_several(client, monkeypatch):
    from test_me_page import _seat
    from test_dogecoin_network import _with_doge
    app, state = client
    _with_doge(state, monkeypatch, ready=True)
    _seat(app)
    assert app.post("/account/wallets", json={"network": "doge", "shown": False}).json()["hidden"] == ["doge"]
    page = app.get("/me/wallet").text
    assert 'const HIDDEN = ["doge"];' in page
    assert 'data-network="doge" ' in app.get("/me").text and 'data-network="doge" checked' not in app.get("/me").text
    assert app.post("/account/wallets", json={"network": "doge", "shown": True}).json()["hidden"] == []
    assert app.post("/account/wallets", json={"network": "nowhere", "shown": False}).status_code == 400


def test_an_announcement_without_litecoin_is_published_again(client, monkeypatch):
    from test_me_page import _seat
    from test_litecoin_network import _with_ltc
    from test_funding import _pubkey
    from arcade.script import b58check_encode, hash160
    app, state = client
    _with_ltc(state, monkeypatch, ready=True)
    _seat(app)
    net = state.messaging.network
    home = b58check_encode(state.messaging.params.pubkeyhash_version, hash160(_pubkey(int("41" * 32, 16))))
    ltc = b58check_encode(48, hash160(_pubkey(int("43" * 32, 16))))
    assert app.post("/account/address", json={"address": home, "chain": net}).status_code == 200
    assert app.post("/account/address", json={"address": ltc, "chain": "ltc"}).status_code == 200
    with state.store() as store:
        store.add_key_announcement("c1" * 32, home, KEY, "fp", 100, 1, stated=True)          # before Litecoin
    said = app.get("/account").json()
    assert said["announced"] is False and said["announcing"] is False, "out of date: published again"
    with state.store() as store:
        store.add_key_announcement("c2" * 32, home, KEY, "fp", 101, 2, stated=True,
                                   chain_addresses=json.dumps({"ltc": ltc}))
    assert app.get("/account").json()["announced"] is True


def test_a_litecoin_arrival_is_named_ltc_and_its_list_starts_where_the_index_does(client, monkeypatch):
    """2026-10-18 (L main: "Arriving: 0.02 PEP", "This list starts at block 0"): a chain's coin is its own,
    and a watch filed before its index held a block starts at the index's first block."""
    from test_me_page import _seat
    from test_litecoin_network import _with_ltc
    from arcade.script import b58check_encode
    from arcade import utxos
    app, state = client
    ltc = _with_ltc(state, monkeypatch, ready=True)
    _seat(app)
    address = b58check_encode(48, bytes(range(20)))
    assert app.post("/account/address", json={"address": address, "chain": "ltc"}).status_code == 200
    chains = app.get("/account").json()["chains"]
    assert next(c for c in chains if c["network"] == "ltc")["coin"] == "LTC"
    page = app.get("/me/wallet").text
    assert "name: coinOf(c)}" in page and "c.coin ? c.coin" in page
    index = state.token_index(ltc)
    import contextlib
    with contextlib.closing(index.open()) as db:
        utxos.watch(db, address, 0, "test")
        db.conn.execute("INSERT INTO block (height, hash, prev_hash, time, tx_count, processed_at) VALUES (?,?,?,?,?,?)",
                        (3197300, "aa" * 32, "bb" * 32, 1, 1, 1))
        db.conn.commit()
    said = app.get("/account/activity?chain=ltc").json()
    assert said["since"] == 3197300, said

"""An @name is paid on another chain at the address its holder PUBLISHED for that chain (2026-11-08).

Before, every chain but the one names live on was worked out from the holder's Pepecoin main address (the "twins":
the same key with that chain's version byte). That is right for a browser account, which holds every real chain on
one key, and wrong for anybody who published another chain's address but no Pepecoin main one -- an ArcadeCore node's
own name published LTC, DGB, BEL and LKY addresses and could be paid by name on none of them. The published address
now comes first; the twin is the fallback; an address published for another chain under this one's name is ignored.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                                 # noqa: F401,E402
from test_profile_pay import EDGE, HASH, _address, _claim, _rows, _sheet, _sign_in, public  # noqa: F401,E402
from test_bells_luckycoin import _with                                 # noqa: E402

import json                                                            # noqa: E402

import pytest                                                          # noqa: E402

from arcade.config import NETWORKS                                     # noqa: E402
from arcade.script import b58check_encode                              # noqa: E402

KEY = bytes(range(32))
OWN = b"\x77" * 20                       # a Bellscoin key of their own, not their Pepecoin one
MAIN = b58check_encode(NETWORKS["main"].pubkeyhash_version, HASH)
BEL_TWIN = b58check_encode(NETWORKS["bel"].pubkeyhash_version, HASH)
BEL_OWN = b58check_encode(NETWORKS["bel"].pubkeyhash_version, OWN)
LKY_OWN = b58check_encode(NETWORKS["lky"].pubkeyhash_version, OWN)


def _publish(state, address, chains=None, **said):
    with state.store() as store:
        store.add_key_announcement("aa" * 32, address, KEY, "fp", 10, 1, stated=True,
                                   chain_addresses=json.dumps(chains) if chains else "", **said)


def _offered(app, sheet_of="them"):
    """{network: address shown} on @them's Pay sheet, which asks `_where_to_pay` exactly as the send does."""
    sheet = _sheet(app.get(f"/u/{sheet_of}", headers=EDGE))
    return sheet, _rows(sheet)


def test_a_published_address_is_paid_rather_than_the_twin(public, monkeypatch):
    app, state = public
    _with(state, monkeypatch, "bel")
    address = _address(state)
    _claim(state, "them", address)
    _publish(state, address, {"bel": BEL_OWN}, other_address=MAIN)
    _sign_in(app)
    sheet, rows = _offered(app)
    assert "bel" in rows and "main" in rows
    assert BEL_OWN[:8] in sheet and BEL_OWN[-6:] in sheet, "where they said they take Bellscoin"
    assert BEL_TWIN[-6:] not in sheet, "not the address worked out from their Pepecoin one"


def test_without_one_the_twin_is_paid_as_before(public, monkeypatch):
    app, state = public
    _with(state, monkeypatch, "bel")
    address = _address(state)
    _claim(state, "them", address)
    _publish(state, address, other_address=MAIN)
    _sign_in(app)
    sheet, rows = _offered(app)
    assert "bel" in rows and BEL_TWIN[:8] in sheet and BEL_TWIN[-6:] in sheet


def test_a_name_with_chain_addresses_but_no_pepecoin_main_one_is_payable_on_them(public, monkeypatch):
    """The ArcadeCore node's case: nothing on Pepecoin main, so main is still not offered, but Bellscoin is."""
    app, state = public
    _with(state, monkeypatch, "bel")
    address = _address(state)
    _claim(state, "them", address)
    _publish(state, address, {"bel": BEL_OWN})
    _sign_in(app)
    sheet, rows = _offered(app)
    assert "main" not in rows
    assert "bel" in rows and BEL_OWN[-6:] in sheet


def test_an_address_published_for_another_chain_is_ignored(public, monkeypatch):
    """A Luckycoin address under Bellscoin's name is no Bellscoin address: the twin is paid, or nothing."""
    app, state = public
    _with(state, monkeypatch, "bel")
    address = _address(state)
    _claim(state, "them", address)
    _publish(state, address, {"bel": LKY_OWN, "lky": "not an address"}, other_address=MAIN)
    _sign_in(app)
    sheet, rows = _offered(app)
    assert "bel" in rows and BEL_TWIN[-6:] in sheet and LKY_OWN[-6:] not in sheet
    _publish(state, address, {"bel": LKY_OWN})
    with state.store() as store:
        store.conn.execute("UPDATE key_announcement SET other_address='' WHERE address=?", (address,))
        store.conn.commit()
    assert "bel" not in _offered(app)[1], "nothing published for Bellscoin and no main address to work one out from"


def test_published_and_twin_by_the_helper_alone():
    from arcade.web.app import _published_on
    from arcade.web.state import ChainContext
    bel = ChainContext(network="bel", role="ledger", label="Bellscoin")
    doge = ChainContext(network="doge", role="ledger", label="Dogecoin")
    row = {"chain_addresses": json.dumps({"bel": BEL_OWN, "doge": "x"}), "doge_address": ""}

    class Row(dict):
        def keys(self):
            return list(super().keys())

    assert _published_on(Row(row), bel) == BEL_OWN
    assert _published_on(Row(row), doge) == "", "not a Dogecoin address"
    doge_own = b58check_encode(NETWORKS["doge"].pubkeyhash_version, OWN)
    assert _published_on(Row({**row, "doge_address": doge_own}), doge) == doge_own, "Dogecoin's own section first"
    assert _published_on(Row({"chain_addresses": "{broken"}), bel) == ""
    assert _published_on(None, bel) == ""


def test_the_profile_wallet_reads_their_published_address(client, monkeypatch):
    app, state = client
    _with(state, monkeypatch, "bel")
    address = _address(state)
    _claim(state, "them", address)
    _publish(state, address, {"bel": BEL_OWN}, other_address=MAIN)
    body = " ".join(app.get("/u/them/wallet").text.split())
    assert BEL_OWN in body and BEL_TWIN not in body


def test_the_nodes_send_still_pays_the_claims_twin_and_falls_back_on_the_published_address(app_state, monkeypatch):
    """The node's send reads a claim on the chain being paid. Found, it wins as before; not found, the name's holder
    on the name chain is paid where they published, instead of the send stopping."""
    from arcade.web import app as applib
    (bel,) = _with(app_state, monkeypatch, "bel")
    monkeypatch.setattr(applib.taglib, "looks_like_a_tag", lambda d: d.startswith("@"))
    monkeypatch.setattr(applib.taglib, "normalise", lambda d: d.lstrip("@"))
    monkeypatch.setattr(applib, "_published_for_tag", lambda state, wanted, chain: BEL_OWN if wanted == "frog" else "")
    real = applib._tag_address

    def claimed(state, d, *, mainnet):
        if mainnet is True:
            return MAIN
        return real(state, d, mainnet=mainnet)
    monkeypatch.setattr(applib, "_tag_address", claimed)
    assert applib._tag_address(app_state, "@frog", mainnet=bel) == BEL_TWIN, "a claim found still wins"

    def unclaimed(state, d, *, mainnet):
        if mainnet is True:
            raise ValueError("nobody holds @frog on mainnet")
        return real(state, d, mainnet=mainnet)
    monkeypatch.setattr(applib, "_tag_address", unclaimed)
    assert applib._tag_address(app_state, "@frog", mainnet=bel) == BEL_OWN
    with pytest.raises(ValueError, match="nobody holds"):
        applib._tag_address(app_state, "@toad", mainnet=bel)

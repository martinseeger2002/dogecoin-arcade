"""Any inscription can call any inscription, cross chain (2026-10-14: "I would like content from all chains to be
available in any game ... Any inscription should be able to call any inscription cross chain"). A page served on one chain
asks /content, /r/inscription, /r/metadata-style routes and the creator/owner lists for a piece or an address that lives on
another chain this node indexes: it gets the piece. A NUMBER stays on the viewer's chain (numbers count per chain)."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

TX_TEST = "aa" * 32       # a piece on PEP testnet
TX_DOGE = "dd" * 32       # a piece on Dogecoin
CREATOR_DOGE = "DDogeCreatorAddressxxxxxxxxxxxxxxx"


def _row(txid, number, creator, body):
    return {"txid": txid, "number": number, "creator": creator, "owner": creator, "block_height": 1,
            "content_type": "text/plain", "content_len": len(body), "sha256": "00" * 32, "chunks": 1,
            "held": 1, "json": "", "collection": None, "edition": None}


class FakeIndex:
    def __init__(self, rows):
        self.rows = {r["txid"]: (r, b) for r, b in rows}
    def inscription(self, k):
        if isinstance(k, int):
            return next((r for r, _ in self.rows.values() if r["number"] == k), None)
        return self.rows.get(k, (None,))[0]
    def inscription_content(self, k):
        r = self.inscription(k)
        return (r["content_type"], self.rows[r["txid"]][1]) if r else None
    def inscriptions(self, limit=100, offset=0, after=-1, creator=None, owner=None):
        return [r for r, _ in self.rows.values() if (not creator or r["creator"] == creator) and (not owner or r["owner"] == owner)]
    def inscription_count(self, owner=None, creator=None):
        return len(self.inscriptions(creator=creator, owner=owner))


def _two_chains(state, monkeypatch):
    from arcade.web.state import ChainContext
    doge = ChainContext(network="doge", role="ledger", label="Dogecoin", conf=pathlib.Path("/nonexistent/dogecoin.conf"))
    here = state.token_chain
    idx = {here.network: FakeIndex([(_row(TX_TEST, 7, "nTestCreator", b"from testnet"), b"from testnet")]),
           "doge": FakeIndex([(_row(TX_DOGE, 7, CREATOR_DOGE, b"from dogecoin"), b"from dogecoin")])}
    monkeypatch.setattr(type(state), "token_chains", property(lambda self: [here, doge]))
    monkeypatch.setattr(state, "token_index", lambda chain: idx[chain.network])
    return here, doge


def test_a_piece_on_another_chain_is_served(client, monkeypatch):
    test_client, state = client
    _two_chains(state, monkeypatch)
    r = test_client.get("/content/" + TX_DOGE)
    assert r.status_code == 200 and r.content == b"from dogecoin"
    assert test_client.get("/content/" + TX_TEST).content == b"from testnet"


def test_its_description_too(client, monkeypatch):
    test_client, state = client
    _two_chains(state, monkeypatch)
    d = test_client.get("/r/inscription/" + TX_DOGE).json()
    assert d["id"] == TX_DOGE and d["creator"] == CREATOR_DOGE


def test_a_number_stays_on_the_viewers_chain(client, monkeypatch):
    test_client, state = client
    _two_chains(state, monkeypatch)
    assert test_client.get("/content/7").content == b"from testnet"   # number 7 here, not Dogecoin's number 7


def test_an_unknown_piece_is_still_missing(client, monkeypatch):
    test_client, state = client
    _two_chains(state, monkeypatch)
    assert test_client.get("/content/" + "ee" * 32).status_code == 404


def test_a_creator_of_another_chain_is_listed(client, monkeypatch):
    test_client, state = client
    _two_chains(state, monkeypatch)
    rows = test_client.get("/r/inscriptions?creator=" + CREATOR_DOGE).json()
    assert [r["id"] for r in rows] == [TX_DOGE]
    assert test_client.get("/r/inscriptions/count?creator=" + CREATOR_DOGE).json() == {"count": 1}


class _Db:
    def __init__(self, path):
        import sqlite3
        self.conn = sqlite3.connect(path); self.conn.row_factory = sqlite3.Row
    def close(self):
        self.conn.close()


def _games_db(tmp_path, name, txid, game_name):
    import json, sqlite3
    path = str(tmp_path / (name + ".sqlite"))
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE inscription(txid TEXT, number INT, creator TEXT, json TEXT, block_height INT, position INT, content_type TEXT)")
    c.execute("CREATE TABLE block(height INT, time INT)")
    c.execute("INSERT INTO inscription VALUES(?,?,?,?,?,?,?)", (txid, 1, "c-" + name, json.dumps({"game": {"name": game_name}}), 5, 0, "text/html"))
    c.execute("INSERT INTO block VALUES(5, 1790000000)"); c.commit(); c.close()
    return path


def test_the_games_tab_shows_every_chains_games(client, monkeypatch, tmp_path):
    test_client, state = client
    here, doge = _two_chains(state, monkeypatch)
    paths = {here.network: _games_db(tmp_path, "here", TX_TEST, "Testnet Quest"), "doge": _games_db(tmp_path, "doge", TX_DOGE, "Doge Dash")}
    real = state.token_index
    for net, ix in ((here.network, real(here)), ("doge", real(doge))):
        ix.open = (lambda p: (lambda: _Db(p)))(paths[net])
    page = test_client.get("/games").text
    assert "Testnet Quest" in page and "Doge Dash" in page, "both chains' games are listed"
    assert "Dogecoin" in page and "on any chain" in page


def test_a_game_on_another_chain_opens_there_for_an_account(client, monkeypatch):
    """2026-10-18: a new account, its browser on another chain, pressed ASHVALE (a PEP testnet game) and was sent away.
    The browser now switches to the piece's chain and comes back to it, full screen or its page."""
    test_client, state = client
    _here, doge = _two_chains(state, monkeypatch)
    state.set_setting("public_hosts", ["node.dogecoinarcade.com"])
    try:
        edge = {"host": "node.dogecoinarcade.com", "cf-ray": "abc-LHR"}
        for end in ("full", "view"):
            went = test_client.get(f"/inscriptions/{TX_DOGE}/{end}", headers=edge, follow_redirects=False)
            assert went.status_code == 303, went.text
            assert went.headers["location"] == f"/create/chain?to=doge&back=/inscriptions/{TX_DOGE}/{end}"
        missing = test_client.get(f"/inscriptions/{'ee' * 32}/full", headers=edge, follow_redirects=False)
        assert "/create/chain" not in missing.headers.get("location", ""), "a piece on no chain is still missing"
    finally:
        state.set_setting("public_hosts", [])

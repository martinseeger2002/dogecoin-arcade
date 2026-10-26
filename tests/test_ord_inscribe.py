"""Inscribing an ord from the account's own wallet (arcade/ord_inscribe.py, 2026-10-23: "Make sure that the
inscriber follows the rules of each chain individually because some of them are different"): each chain's envelope
read back by its own indexer's dialect, its own fees and dust rule, and a whole ord built here walked by our index."""
import os

import pytest

from arcade import config, doginal_index as dx, funding, ord_inscribe as oi, ord_rules, rawchain
from arcade.script import b58check_encode, hash160
from arcade.txbuild import p2pkh_script, push

from test_doginal_index import FUND, Chain, _tx_to  # noqa: E402

PUBKEY = bytes.fromhex("02" + "11" * 32)
PARAMS = {"doge": config.DOGECOIN, "main": config.MAINNET, "bel": config.BELLSCOIN, "lky": config.LUCKYCOIN,
          "wjk": config.WOJAKCOIN, "dgb": config.DIGIBYTE, "test": config.TESTNET}


def _to(net):
    return b58check_encode(PARAMS[net].pubkeyhash_version, hash160(PUBKEY))


@pytest.fixture(autouse=True)
def small(monkeypatch):
    monkeypatch.setattr(dx, "CHUNK", 4)
    monkeypatch.setattr(dx, "SETTLE", 0)
    monkeypatch.setattr(dx, "WALK_SETTLE", 0)


@pytest.mark.parametrize("net", sorted(oi.RULES))
@pytest.mark.parametrize("size", [1, 16, 239, 240, 241, 1499, 1500, 5000, 70_000])
def test_every_size_reads_back_as_one_inscription_under_the_chains_own_rules(net, size):
    data = os.urandom(size)
    p = oi.plan(net, PARAMS[net], "image/png", data, PUBKEY, _to(net))
    scripts = [b"".join(b) + push(b"\x30" * 71) + push(lock) for b, lock in zip(p.batches, p.locks)]
    state, ctype, body, _tags = ord_rules.read(scripts, oi.rules_of(net).dialect)
    assert (state, ctype, body) == ("complete", b"image/png", data)
    for b, lock in zip(p.batches, p.locks):
        assert sum(map(len, b)) <= oi.PAYLOAD + 260
        assert len(b"".join(b)) + 74 + len(push(lock)) <= 1650, "a standard scriptSig"
        assert all(len(x) <= 255 for x in b), "no OP_PUSHDATA2: wonky-ord misreads its length"
        assert lock == push(PUBKEY) + b"\xad" + b"\x75" * len(b) + b"\x51"


def test_the_layout_is_the_anti_doginal_dogs_inscribers():
    """~/doginal-mint/inscribe.py's batches: "ord" alone at the front of the first, then whole (countdown, part) pairs,
    240-byte parts."""
    data = bytes(range(256)) * 4
    p = oi.plan("doge", PARAMS["doge"], "text/html", data, PUBKEY, _to("doge"))
    first = p.batches[0]
    assert first[0] == push(b"ord") and first[1] == oi.number(5) and first[2] == push(b"text/html")
    assert first[3] == oi.number(4) and first[4] == push(data[:240])
    assert oi.number(200) == b"\x02\xc8\x00" and oi.number(100) == b"\x01\x64" and oi.number(16) == b"\x60"


def test_an_empty_file_is_an_inscription_only_on_pepecoin():
    for net in ("doge", "bel", "lky", "wjk", "dgb"):
        with pytest.raises(oi.OrdError):
            oi.plan(net, PARAMS[net], "text/plain", b"", PUBKEY, _to(net))
    p = oi.plan("main", PARAMS["main"], "text/plain", b"", PUBKEY, _to("main"))
    assert p.reveals == 1


def test_fees_follow_each_chain():
    """Dogecoin, Pepecoin and Luckycoin: 0.01 a kB and the soft-dust fee on the 0.001 ord; Bellscoin: 0.0001 a kB and
    none. Every lock holds exactly what the reveals after it pay plus the postage."""
    data = os.urandom(5000)
    got = {}
    for net in ("doge", "main", "bel", "lky", "wjk", "dgb"):
        p = oi.plan(net, PARAMS[net], "image/png", data, PUBKEY, _to(net))
        for i in range(p.reveals):
            assert p.values[i] - p.fees[i] == p.out_value(i)
        assert p.out_value(p.reveals - 1) == oi.POSTAGE
        got[net] = p.fees[-1]
    assert got["doge"] == got["main"] == got["lky"] > 1_000_000, "the soft-dust fee on the ord's coin"
    assert got["bel"] < 100_000 and got["wjk"] < 100_000, "Bellscoin and Wojakcoin: Bitcoin Core's relay, no soft dust"
    assert got["wjk"] < got["dgb"] < got["doge"], "DigiByte: 0.01 DGB a kB, Bitcoin Core's relay, no soft dust"


def test_too_big_is_refused():
    with pytest.raises(oi.OrdError):
        oi.plan("doge", PARAMS["doge"], "image/png", b"x" * (oi.MAX_BYTES + 1), PUBKEY, _to("doge"))


def test_a_whole_ord_built_here_is_found_by_our_index(tmp_path):
    """The commit, then each reveal as funding.assemble puts it together (envelope, signature, lock): our own Dogecoin
    index finds the inscription at the first reveal's txid + i0, on the account's own address, with its content type."""
    holder = _to("doge")
    data = os.urandom(5000)
    p = oi.plan("doge", PARAMS["doge"], "image/png", data, PUBKEY, holder)
    assert p.reveals == 4
    chain = Chain()
    commit, commit_id = _tx_to([(FUND, 3, push(b"\x30" * 71) + push(PUBKEY))], [(p.commit_value, oi.p2sh(p.locks[0]))])
    chain.add([(commit, commit_id)])
    prev, ids = (commit_id, 0), []
    for i in range(p.reveals):
        unsigned = oi.build_reveal(PARAMS["doge"], p, i, {"txid": prev[0], "vout": prev[1]})
        assert unsigned.fee == p.fees[i] and len(unsigned.inputs) == 1 and len(unsigned.outputs) == 1
        raw = bytes.fromhex(funding.assemble(unsigned, ["30" * 71], PUBKEY))
        tx = rawchain.read_tx(raw)
        chain.add([(raw, tx.txid)])
        ids.append(tx.txid)
        prev = (tx.txid, 0)
    assert rawchain.read_tx(raw).vout[0] == (oi.POSTAGE, p2pkh_script(holder))
    index = dx.DoginalIndex(tmp_path / "w.sqlite", lambda: chain, lambda: "walk")
    for _ in range(20):
        index.step()
    found = index.inscription(iid=ids[0] + "i0")["inscription"]
    assert found and found["id"] == ids[0] + "i0", found
    assert found.get("content_type") in ("image/png", b"image/png"), found


# --- the routes: /account/ord/* on an account with Dogecoin -------------------------------------------------------------

import pathlib  # noqa: E402
import sys  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_funding import _pubkey                                 # noqa: E402
from test_doginal_wallet import ME, _doge_account                # noqa: E402


def _fake_doge(monkeypatch, doge):
    sent, mined = {}, {}

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
                return {"txid": args[0], "confirmations": mined.get(args[0], 0)}
            if method == "getmempoolentry":
                if "ancestors" not in mined:
                    raise RuntimeError("Method not found")         # an older node: the run's own count is used
                if mined.get(args[0]):
                    raise RuntimeError("Transaction not in mempool")
                return {"ancestorcount": mined["ancestors"]}
            raise AssertionError(method)
    real_rpc = type(doge).rpc
    monkeypatch.setattr(type(doge), "rpc", lambda self: Rpc() if self.network == "doge" else real_rpc(self))
    return sent, mined


def test_an_ord_is_the_commit_then_every_reveal_and_waits_for_blocks(client, monkeypatch, tmp_path):
    """2026-10-23: inscribe an ord from the Creator tab. The commit pays this account's own lock; each reveal
    spends the coin the last one made and pays the next lock; past MAX_UNCONFIRMED in a row it waits for a block; a
    restart asks for the file again and carries on; the ord lands on this account's own address, its id the first
    reveal's txid + i0."""
    import base64
    app, state = client
    doge, _ = _doge_account(app, state, monkeypatch, tmp_path)
    sent, mined = _fake_doge(monkeypatch, doge)
    monkeypatch.setattr(oi, "MAX_UNCONFIRMED", 3)
    key = _pubkey(int("31" * 32, 16))

    def sign(offer):
        done = app.post("/account/sign", json={"offer": offer["offer"], "signatures": ["30" * 71] * len(
            offer["sighashes"]), "pubkey": key.hex()})
        assert done.status_code == 200, done.text
        return done.json()["txid"]

    data = os.urandom(5000)
    body = {"chain": "doge", "content": base64.b64encode(data).decode(), "content_type": "image/png", "name": "a.png"}
    wrong = app.post("/account/ord/inscribe", json={**body, "pubkey": _pubkey(int("41" * 32, 16)).hex()})
    assert wrong.status_code == 400 and "not the key" in wrong.json()["detail"]
    empty = app.post("/account/ord/inscribe", json={**body, "content": "", "pubkey": key.hex()})
    assert empty.status_code == 400 and "empty" in empty.json()["detail"], "Dogecoin: a 0-part envelope is none"
    first = app.post("/account/ord/inscribe", json={**body, "pubkey": key.hex()})
    assert first.status_code == 200, first.text
    c = first.json()
    p = oi.plan("doge", PARAMS["doge"], "image/png", data, key, ME)
    assert c["of"] == p.reveals + 1 == 5 and c["next_lock"] == p.locks[0].hex()
    assert c["costs"]["reveal_fees"] == sum(p.fees) and c["costs"]["postage"] == oi.POSTAGE
    commit = sign(c)
    outs = rawchain.read_tx(bytes.fromhex(sent[commit])).vout
    assert (p.commit_value, oi.p2sh(p.locks[0])) in outs
    txids, waits = [commit], 0
    for i in range(p.reveals):
        if i == 2:
            app.app.state.ord_held.clear()                 # the arcade restarted: the file is asked for again
            need = app.post("/account/ord/inscribe", json={"run": c["run"]}).json()
            assert need["need_content"] and need["sent"] == 3
            other = app.post("/account/ord/inscribe", json={"run": c["run"], "content": base64.b64encode(b"x").decode()})
            assert other.status_code == 400 and "same file" in other.json()["detail"]
            r = app.post("/account/ord/inscribe", json={"run": c["run"], "content": body["content"]}).json()
        else:
            r = app.post("/account/ord/inscribe", json={"run": c["run"]}).json()
        if r.get("wait"):
            assert len(txids) >= 3, "only past MAX_UNCONFIRMED in a row"
            mined[txids[-3]] = 1
            waits += 1
            r = app.post("/account/ord/inscribe", json={"run": c["run"]}).json()
        assert r["step"] == i + 1 and r["inputs"][0]["txid"] == txids[-1], r
        assert r["inputs"][0]["redeem"] == p.locks[i].hex()
        assert r["last"] == (i + 1 == p.reveals)
        txid = sign(r)
        tx = rawchain.read_tx(bytes.fromhex(sent[txid]))
        assert len(tx.vin) == 1 and tx.vin[0]["script"].startswith(b"".join(p.batches[i]))
        assert tx.vout == [(p.out_value(i), p.out_script(i))]
        txids.append(txid)
    assert waits == 2, "waited for a block at 3 and at 4 unconfirmed"
    assert tx.vout == [(oi.POSTAGE, p2pkh_script(ME))], "the ord on this account's own address"
    assert (txids[-1], 0) in funding.NOTE_COINS, "never taken to pay a fee before the index reads it"
    done = app.post("/account/ord/inscribe", json={"run": c["run"]}).json()
    assert done["done"] and done["inscription"] == txids[1] + "i0"
    listed = app.get("/account/ord/runs").json()["runs"]
    assert listed[0]["inscription"] == txids[1] + "i0" and listed[0]["done"]
    assert app.post("/account/ord/forget", json={"run": c["run"]}).json()["ok"]
    assert app.get("/account/ord/runs").json()["runs"] == []


def test_the_chains_offered_are_the_ones_this_arcade_runs(client, monkeypatch, tmp_path):
    app, state = client
    _doge_account(app, state, monkeypatch, tmp_path)
    chains = {c["chain"]: c for c in app.get("/account/ord/chains").json()["chains"]}
    assert "doge" in chains and chains["doge"]["ord"] == "Doginal" and chains["doge"]["soft_dust"]
    assert "bel" not in chains, "no Bellscoin node here"


def test_a_collection_index_comes_out_in_parts_of_one_transaction(client, monkeypatch, tmp_path):
    """The Doginal Dogs' format (arcade/collection_json.py), each part small enough for one transaction."""
    from arcade import collection_json as C
    app, state = client
    _doge_account(app, state, monkeypatch, tmp_path)
    items = [{"id": f"{n:064x}i0", "number": n, "traits": {"Eyes": ["Red", "Blue"][n % 2], "Hat": "Cap" if n % 3 else ""}}
             for n in range(1, 301)]
    said = app.post("/account/ord/collection", json={"name": "Mine", "chain": "lky", "items": items})
    assert said.status_code == 200, said.text
    got = said.json()
    assert got["content_type"] == C.CONTENT_TYPE and got["items"] == 300 and got["traits"] == ["Eyes", "Hat"]
    assert len(got["parts"]) > 1
    import json as _json
    assert all(len(_json.dumps(x, separators=(",", ":")).encode()) <= 5000 for x in got["parts"])
    root = {**got["root"], "parts": [f"{n:064x}" for n in range(len(got["parts"]))]}
    whole = C.assemble(root, got["parts"]) if hasattr(C, "assemble") else None
    if whole is not None:
        assert len(whole.items) == 300 and whole.chain == "lky"
    bad = app.post("/account/ord/collection", json={"name": "Mine", "chain": "lky", "items": items + [items[0]]})
    assert bad.status_code == 400


def test_the_mempool_says_how_long_the_chain_is_and_a_collection_waits_on_it(client, monkeypatch, tmp_path):
    """A collection's commits stand on one another: the node's ancestorcount, not this run's own length, says when to
    wait for a block."""
    import base64
    app, state = client
    doge, _ = _doge_account(app, state, monkeypatch, tmp_path)
    sent, mined = _fake_doge(monkeypatch, doge)
    key = _pubkey(int("31" * 32, 16))
    c = app.post("/account/ord/inscribe", json={"chain": "doge", "content": base64.b64encode(b"hi").decode(),
                                                 "content_type": "text/plain", "pubkey": key.hex()}).json()
    commit = app.post("/account/sign", json={"offer": c["offer"], "signatures": ["30" * 71], "pubkey": key.hex()})
    assert commit.status_code == 200, commit.text
    mined["ancestors"] = oi.MAX_UNCONFIRMED                 # the commit stands on 19 others
    assert app.post("/account/ord/inscribe", json={"run": c["run"]}).json()["wait"]
    mined["ancestors"] = 3
    assert app.post("/account/ord/inscribe", json={"run": c["run"]}).json()["step"] == 1
    mined[commit.json()["txid"]] = 1                         # in a block: not in the mempool
    mined["ancestors"] = 99
    assert app.post("/account/ord/inscribe", json={"run": c["run"]}).json()["step"] == 1


def test_a_collection_is_quoted_before_it_starts(client, monkeypatch, tmp_path):
    app, state = client
    _doge_account(app, state, monkeypatch, tmp_path)
    q = app.post("/account/ord/quote", json={"chain": "doge", "files": [{"size": 100, "content_type": "image/png"},
                                                                       {"size": 5000, "content_type": "image/png"}]})
    assert q.status_code == 200, q.text
    q = q.json()
    want = [oi.plan("doge", PARAMS["doge"], "image/png", b"\x00" * n, PUBKEY, _to("doge")) for n in (100, 5000)]
    assert [f["txs"] for f in q["files"]] == [p.reveals + 1 for p in want] == [2, 5]
    assert q["fees"] == sum(sum(p.fees) for p in want) + 2 * q["commit_fee"] and q["commit_fee"] > 0


def test_a_collections_lanes_split_pays_only_this_account(client, monkeypatch, tmp_path):
    """2026-10-25: a collection inscribed on lanes, each its own coin. The split is coins of this account's own,
    2 to 8 of them; its block is asked of the node; a commit given a lane's coin is paid from that coin alone."""
    import base64
    app, state = client
    doge, _ = _doge_account(app, state, monkeypatch, tmp_path)
    sent, mined = _fake_doge(monkeypatch, doge)
    assert app.post("/account/ord/split", json={"chain": "doge", "lanes": 1, "each": 10 ** 8}).status_code == 400
    assert app.post("/account/ord/split", json={"chain": "doge", "lanes": 9, "each": 10 ** 8}).status_code == 400
    split = app.post("/account/ord/split", json={"chain": "doge", "lanes": 3, "each": 9 * 10 ** 7})
    assert split.status_code == 200, split.text
    s = split.json()
    assert s["vouts"] == [0, 1, 2]
    assert app.get("/account/ord/confirmed", params={"chain": "doge", "txid": "ab" * 32}).json()["confirmations"] == 0
    mined["ab" * 32] = 2
    assert app.get("/account/ord/confirmed", params={"chain": "doge", "txid": "ab" * 32}).json()["confirmations"] == 2
    key = _pubkey(int("31" * 32, 16))
    body = {"chain": "doge", "content": base64.b64encode(b"x" * 300).decode(), "content_type": "image/png",
            "name": "l.png", "pubkey": key.hex()}
    bad = app.post("/account/ord/inscribe", json={**body, "lane": {"txid": "nope", "vout": 0}})
    assert bad.status_code == 400
    gone = app.post("/account/ord/inscribe", json={**body, "lane": {"txid": "cd" * 32, "vout": 0}})
    assert gone.status_code == 400, "a lane's coin this account does not hold pays nothing"


def test_creator_names_says_whether_a_name_counts_and_is_free(client, monkeypatch, tmp_path):
    """Creator -> Names (2026-10-26: "Build a p:dns inscriber in the creators tab"): /account/ord/name checks a
    name the page would claim as {"p":"dns","op":"reg","name":...} against each reading, and asks the chain's index who
    holds it."""
    app, state = client
    _doge_account(app, state, monkeypatch, tmp_path)

    class Index:
        def lookup(self, q):
            assert q == {"what": "name", "name": q["name"]}
            held = {"wow.doge": {"inscription": "ab" * 32 + "i0", "number": 7, "kind": "json", "output": None}}
            # as DoginalIndex.lookup answers a name: the index's name() under "name", and whose answer it is
            return {"name": {"name": q["name"], "readings": {r: {"winner": held.get(q["name"]), "later_claims": 0}
                                                              for r in ord_rules.NAME_RULES}},
                    "from": "this arcade", "walked_to": 100}
    monkeypatch.setattr(state, "ord_indexes", lambda: {"doge": Index()})
    ask = lambda name, chain="doge": app.get("/account/ord/name", params={"chain": chain, "name": name}).json()
    free = ask("Me-Seeks.doge")
    assert free["valid"] and not free["taken"] and free["name"] == "me-seeks.doge"
    assert all(r["counts"] for r in free["readings"].values()), "a .doge name counts under every reading"
    assert ".doge" in free["endings"]
    mesh = ask("me-seeks.mesh")
    assert mesh["valid"] and not mesh["readings"]["doge2023"]["counts"], "the 2023 guide counts .doge only"
    taken = ask("wow.doge")
    assert taken["taken"] and taken["readings"]["spec"]["winner"]["number"] == 7
    for bad in ("-wow.doge", "wow", "a.b.doge", "w_w.doge"):
        assert not ask(bad)["valid"], bad
    assert ask("1234.pepemap")["why"] == "A block map, not a name"
    away = ask("wow.doge", chain="bel")
    assert away["valid"] and not away["taken"] and "does not read" in away["why"]
    page = app.get("/create/name", headers={"host": "app.example.com", "cf-ray": "x"}).text
    assert 'id="nm-go"' in page and '"p":"dns"' in page
    assert 'href="/create/name"' in app.get("/create", headers={"host": "app.example.com", "cf-ray": "x"}).text


def test_creator_names_route_is_open_to_accounts():
    from arcade.web import door
    assert door.public_path("/create/name") and door.public_path("/account/ord/name")

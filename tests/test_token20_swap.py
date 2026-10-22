"""A DRC-20 / PRC-20 token in a swap's lock (arcade/token20_swap.py, 2026-10-22: "Add prc 20 and drc 20 tokens to
the token/coin exchange"): the four transactions that lock it, walked by our own index as the explorers count them --
the claim hands the tokens to the claimer, a refund back to the seller -- and the lock read as the other side reads it."""
import pytest

from arcade import doginal_index as dx
from arcade import funding, htlc as H, rawchain, token20_send as t20, token20_swap as t20swap
from arcade import trade as T, trade_orders
from arcade.script import b58check_encode, hash160
from arcade.txbuild import p2pkh_script

from test_doginal_index import FUND, Chain, _tx_to  # noqa: E402
from test_ord_rules import TEXT, _drc, _json, _walk  # noqa: E402
from test_web import app_state, client  # noqa: F401,E402

SELLER = bytes.fromhex("02" + "11" * 32)
BUYER = bytes.fromhex("03" + "22" * 32)
PARAMS = type("P", (), {"scripthash_version": 22})()
SIGN = lambda digest: b"\x30" * 71 + b"\x01"           # noqa: E731 -- the walk reads no signature


@pytest.fixture(autouse=True)
def small(monkeypatch):
    monkeypatch.setattr(dx, "CHUNK", 4)
    monkeypatch.setattr(dx, "SETTLE", 0)
    monkeypatch.setattr(dx, "WALK_SETTLE", 0)


def _addr(pub):
    return b58check_encode(30, hash160(pub))


def _envelope_of(op):
    from test_doginal_index import _envelope
    return _envelope(TEXT, parts=((0, _json(op)),))


def _locked(tmp_path):
    """A token minted to the seller, then locked the four-step way; (chain, index, lock redeem, lock txid, secret)."""
    seller = _addr(SELLER)
    chain, txs = Chain(), []
    _drc(txs, chain, {"p": "drc-20", "op": "deploy", "tick": "abcd", "max": "1000", "lim": "1000"}, 0, b"d")
    chain.add(txs)
    chain.add([_tx_to([(FUND, 1, _envelope_of({"p": "drc-20", "op": "mint", "tick": "abcd", "amt": "700"}))],
                      [(100_000, p2pkh_script(seller))])])
    index = _walk(tmp_path, chain)
    secret, digest = H.make_secret()
    redeem = H.redeem_script(digest, BUYER, SELLER, 1_900_000_000)
    pushes, note_redeem = t20.envelope(t20.note("drc-20", "abcd", "300"), SELLER)
    outs = t20swap.lock_outputs(redeem, H.DOGINAL_RESERVE)
    # 1. note 1 on the seller (its commit is a made-up coin here)
    reveal1 = funding.assemble(t20.build_reveal(PARAMS, {"txid": FUND, "vout": 2, "value": 5_000_000}, pushes,
                                                note_redeem, seller), ["30" * 71], SELLER)
    r1 = rawchain.read_tx(bytes.fromhex(reveal1)).txid
    chain.add([(bytes.fromhex(reveal1), r1)])
    # 2. the funding: note 1 to the lock (its first move), and note 2's commit
    from test_doginal_index import SIG
    commit2 = sum(v for v, _ in outs) + 10_000_000
    fund = _tx_to([(r1, 0, SIG), (FUND, 3, SIG)], [(t20.POSTAGE, H.p2sh_script(redeem)),
                                                   (commit2, t20.commit_script(note_redeem))])
    chain.add([fund])
    # 3. the lock: note 2 on the lock, its reserve beside it
    lock_raw = funding.assemble(t20.build_reveal(PARAMS, {"txid": fund[1], "vout": 1, "value": commit2}, pushes,
                                                 note_redeem, outputs=outs), ["30" * 71], SELLER)
    lock = rawchain.read_tx(bytes.fromhex(lock_raw)).txid
    chain.add([(bytes.fromhex(lock_raw), lock)])
    index.step()
    return chain, index, redeem, lock, secret


def _bal(index):
    return {b["owner"]: (b["balance"], b["transferable"]) for b in index.token("abcd")["top"]}


def test_a_locked_token_is_the_claimers_when_claimed(tmp_path):
    chain, index, redeem, lock, secret = _locked(tmp_path)
    lock_address = H.address_of(redeem, PARAMS)
    assert _bal(index)[_addr(SELLER)] == ("400", "0") and _bal(index)[lock_address] == ("300", "300"), \
        "the funding gave the lock the tokens, and its note set them aside on the lock"
    assert index.note(f"{lock}i0") == {"tick": "abcd", "amt": str(300 * 10 ** 18), "owner": lock_address}, \
        "valid, the lock's own (the amount in units: 18 decimal places by default)"
    inputs, outputs = H.doginal_out({"txid": lock, "vout": 0, "value": t20.POSTAGE},
                                    {"txid": lock, "vout": 1, "value": H.DOGINAL_RESERVE}, _addr(BUYER))
    claim = H.claimed(inputs, outputs, redeem, secret, SIGN)
    chain.add([(bytes.fromhex(claim), rawchain.read_tx(bytes.fromhex(claim)).txid)])
    index.step()
    bal = _bal(index)
    assert bal[_addr(BUYER)] == ("300", "0") and bal.get(lock_address, ("0", "0")) == ("0", "0"), bal
    assert index.note(f"{lock}i0") is None, "moved: no longer a note"


def test_a_refunded_lock_gives_the_tokens_back_to_the_seller(tmp_path):
    chain, index, redeem, lock, _secret = _locked(tmp_path)
    inputs, outputs = H.doginal_out({"txid": lock, "vout": 0, "value": t20.POSTAGE},
                                    {"txid": lock, "vout": 1, "value": H.DOGINAL_RESERVE}, _addr(SELLER), refund=True)
    refund = H.refunded(inputs, outputs, redeem, SIGN)
    chain.add([(bytes.fromhex(refund), rawchain.read_tx(bytes.fromhex(refund)).txid)])
    index.step()
    assert _bal(index)[_addr(SELLER)] == ("700", "0")


class _Rpc:
    def __init__(self, txs, tip):
        self.txs, self.tip = txs, tip

    def call(self, method, *args):
        assert method == "getrawtransaction"
        return self.txs[args[0]]

    def get_block_count(self):
        return self.tip


def _decoded(raw_hex):
    tx = rawchain.read_tx(bytes.fromhex(raw_hex))
    return {"txid": tx.txid, "vin": [{"txid": i["txid"], "vout": i["vout"], "scriptSig": {"hex": i["script"].hex()}}
                                     for i in tx.vin],
            "vout": [{"n": n, "value": v / 1e8, "scriptPubKey": {"hex": s.hex()}} for n, (v, s) in enumerate(tx.vout)]}


def test_the_other_side_counts_the_lock_only_once_the_index_has_read_its_note(tmp_path):
    chain, index, redeem, lock, _secret = _locked(tmp_path)
    lock_hex = chain.txs[lock].hex()
    rpc = _Rpc({lock: _decoded(lock_hex)}, tip=1000)
    leg = T.Leg("doge", "token20", "abcd", 300, BUYER.hex(), SELLER.hex())

    class Index:
        def __init__(self, note, walked):
            self.n, self.w = note, walked

        def lookup(self, q):
            assert q == {"what": "note", "id": f"{lock}i0"}
            return {"note": self.n, "walked_to": self.w}

    def seen(conf=3):
        return T.LockSeen(txid=lock, vout=0, value=t20.POSTAGE, script_hex=H.p2sh_script(redeem).hex(),
                          confirmations=conf)
    good = {"tick": "abcd", "amt": "300", "owner": H.address_of(redeem, PARAMS)}
    got = t20swap.token20_seen(seen(), leg, redeem, rpc, Index(good, 1000), PARAMS)
    assert got.asset_ok and got.confirmations == 3 and got.reserve == H.DOGINAL_RESERVE
    got = t20swap.token20_seen(seen(), leg, redeem, rpc, Index(good, 990), PARAMS)
    assert got.confirmations == 0, "the index not past the lock's block yet: unconfirmed, never good"
    got = t20swap.token20_seen(seen(), leg, redeem, rpc, Index(None, 1000), PARAMS)
    assert not got.asset_ok, "read, and not a valid note: wrong"
    got = t20swap.token20_seen(seen(), leg, redeem, rpc, Index({**good, "owner": "Pnobody"}, 1000), PARAMS)
    assert not got.asset_ok, "a note, but not the lock's"
    other = T.Leg("doge", "token20", "abcd", 301, BUYER.hex(), SELLER.hex())
    assert not t20swap.token20_seen(seen(), other, redeem, rpc, Index(good, 1000), PARAMS).asset_ok, "another amount"


def test_a_token_order_names_its_tick_and_whole_tokens_and_waits_for_a_confirmation():
    side = trade_orders._side({"network": "main", "kind": "token20", "asset": "frgy", "amount": 10000})
    assert side == {"network": "main", "kind": "token20", "asset": "frgy", "amount": 10000}
    for bad in ({"network": "test", "kind": "token20", "asset": "frgy", "amount": 1},
                {"network": "main", "kind": "token20", "asset": "FRGY", "amount": 1},
                {"network": "main", "kind": "token20", "asset": "fr gy", "amount": 1}):
        with pytest.raises(trade_orders.OrderError):
            trade_orders._side(bad)
    assert trade_orders.parse_leg_key("main-token20-frgy") == {"network": "main", "kind": "token20", "asset": "frgy"}
    assert trade_orders.leg_key({"network": "main", "kind": "token20", "asset": "frgy"}) == "main-token20-frgy"
    assert trade_orders.one_chain_offer({"network": "main", "kind": "token20"}, {"network": "main", "kind": "coin"})
    give = T.Leg("main", "token20", "frgy", 10000, BUYER.hex(), SELLER.hex())
    get = T.Leg("doge", "coin", "", 5 * 10 ** 8, SELLER.hex(), BUYER.hex())
    assert T.confirmations_for(give, get) == 1 and T.lock_value(give) == 1
    assert T.fund_value(give) == T.doginal_reserve(give)


def test_the_exchange_looks_ticks_up_as_typed_and_never_lists_them_all(client, monkeypatch):
    """2026-10-22: every tick of both chains in the page made it 1.9 MB. The page carries the reader's own; the box asks
    /exchange/token20/search as it is typed into: "tick · PRC-20", a dozen at most."""
    from arcade.web import door
    app, state = client
    asked = []

    class Index:
        tip = 0

        def lookup(self, q):
            asked.append(q)
            if q["what"] == "ticks":
                return {"ticks": {"count": 2, "first": [{"tick": "frgy"}, {"tick": "frog"}]}}
            return {"balances": []}
    monkeypatch.setattr(type(state), "ord_indexes", lambda self: {"main": Index()})
    got = app.get("/exchange/token20/search?q=FR").json()["results"]
    assert [r["label"] for r in got] == ["frgy · PRC-20", "frog · PRC-20"] and got[0]["value"] == "main-token20-frgy"
    assert asked == [{"what": "ticks", "prefix": "fr"}]
    assert app.get("/exchange/token20/search?q=").json()["results"] == []
    assert "/exchange/token20/search" in door.PUBLIC_PAGES
    assert "ticklist" not in str(asked)
    page = app.get("/exchange?tab=swaps").text
    assert "/exchange/token20/search?q=" in page

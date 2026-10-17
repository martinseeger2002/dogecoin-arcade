"""A bundle between two players, end to end on a regtest node (2026-10-04,
The operator: "You should be able to trade multiple items" / "items and Gold" / "how
many of that item" / "two of the same items that are not stackable"). One
transaction both sign, each signing only its own coins; everything moves."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_offer import node, _seated, _settled, _inscribed       # noqa: F401,E402
from test_account_accept import _priced_in                              # noqa: E402
from test_funding import _sign                                          # noqa: E402

COIN = 100_000_000


def test_two_pieces_and_a_stack_for_a_piece_in_one_transaction(node):
    app, state, rpc = node
    alice, bob = _seated(app, state, rpc, 180), _seated(app, state, rpc, 181)
    ac, asecret, apub, aaddr = alice
    bc, bsecret, bpub, baddr = bob
    s1 = _inscribed(ac, state, rpc, asecret, apub, "a bronze sword")
    s2 = _inscribed(ac, state, rpc, asecret, apub, "another bronze sword")
    ring = _inscribed(bc, state, rpc, bsecret, bpub, "a hawk ring")
    gold = _priced_in(state, aaddr, 100 * COIN, 143)

    give = [{"inscription": s1}, {"inscription": s2}, {"token": gold, "amount": "50"}]
    get = [{"inscription": ring}]
    built = ac.post("/account/bundle/build", json={"with": baddr, "give": give, "get": get})
    assert built.status_code == 200, built.text
    offer = built.json()
    assert offer["signed_from"] >= 1, "the other player's coin comes first"
    hand = {"raw": offer["raw"], "a": aaddr, "b": baddr, "give": give, "get": get,
            "a_pubkey": apub.hex(),
            "a_sigs": [_sign(asecret, bytes.fromhex(d)).hex() for d in offer["sighashes"]]}
    assert rpc.call("getrawmempool") == [], "the proposer's half sends nothing"

    wrong = bc.post("/account/bundle/fill", json={"handoff": hand, "expect": {
        "give": get, "get": give[:2]}})
    assert wrong.status_code == 400 and "not the trade you agreed to" in wrong.text, wrong.text
    expect = {"give": get, "get": give}
    asked = bc.post("/account/bundle/fill", json={"handoff": hand, "expect": expect})
    assert asked.status_code == 200, asked.text
    mine = asked.json()
    assert mine["signed_from"] == 0 and mine["signed_to"] == len(mine["sighashes"])
    done = bc.post("/account/bundle/fill/sign", json={
        "handoff": hand, "expect": expect, "pubkey": bpub.hex(),
        "signatures": [_sign(bsecret, bytes.fromhex(d)).hex() for d in mine["sighashes"]]})
    assert done.status_code == 200, done.text

    seen = bc.get(f"/r/pending/{baddr}").json()
    assert {m.get("inscription") for m in seen["incoming"]} >= {s1, s2}, seen

    _settled(state, rpc)
    index = state.token_index(state.messaging)
    verdict = index.open().conn.execute(
        "SELECT valid, invalid_reason FROM arcade_tx WHERE txid=?", (done.json()["txid"],)).fetchone()
    assert verdict["valid"] == 1, verdict["invalid_reason"]
    assert (index.inscription(s1)["owner"], index.inscription(s2)["owner"]) == (baddr, baddr)
    assert index.inscription(ring)["owner"] == aaddr
    assert int(index.balance(baddr, gold)) == 50 * COIN

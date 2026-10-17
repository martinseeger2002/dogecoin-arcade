"""What is on its way, before its block (settle_now_design.md D, 2026-10-04,
The operator: "if somebody leaves the game, they should be able to just look in their
Dogecoin arcade wallet and see what they collected"). Read from the mempool,
labelled arriving or leaving, gone when it lands -- and the ledger is untouched."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_offer import node, _seated, _settled, _inscribed, _signed  # noqa: F401,E402
from test_account_order import _bookcoin, COIN                             # noqa: E402


def test_a_piece_and_a_token_on_their_way_show_as_arriving_until_they_land(node):
    app, state, rpc = node
    book = _bookcoin(node, 170)
    maker = book["client"]
    player = _seated(app, state, rpc, 171)
    pc, paddr = player[0], player[3]
    sword = _inscribed(maker, state, rpc, book["secret"], book["pubkey"], "a sword on its way")

    sent = maker.post("/account/nft/send", json={"piece": sword, "to": paddr})
    assert sent.status_code == 200, sent.text
    assert _signed(maker, book["secret"], book["pubkey"], sent.json()).status_code == 200
    _settled_tokens = maker.post("/account/token/send", json={
        "property_id": book["pid"], "to": paddr, "amount": "7"})
    assert _settled_tokens.status_code == 200, _settled_tokens.text
    assert _signed(maker, book["secret"], book["pubkey"], _settled_tokens.json()).status_code == 200

    # In the mempool: arriving for the player, leaving for the maker.
    seen = pc.get(f"/r/pending/{paddr}").json()
    kinds = {(m["kind"], m.get("inscription") or m.get("property_id")) for m in seen["incoming"]}
    assert ("inscription", sword) in kinds and ("token", book["pid"]) in kinds, seen
    token = next(m for m in seen["incoming"] if m["kind"] == "token")
    assert token["units"] == 7 * COIN and token["from"] == book["address"] and token["why"] == "send"
    gone = pc.get(f"/r/pending/{book['address']}").json()
    assert any(m.get("inscription") == sword for m in gone["outgoing"]), gone

    mine = pc.get(f"/r/inscriptions/{paddr}?pending=1").json()
    arriving = [r for r in mine if r.get("pending") == "arriving"]
    assert [r["id"] for r in arriving] == [sword] and arriving[0]["owner"] == book["address"], \
        "arriving, and the owner is still what the blocks say"
    assert not any(r["id"] == sword for r in pc.get(f"/r/inscriptions/{paddr}").json()), \
        "without pending=1 nothing changes"
    leaving = pc.get(f"/r/inscriptions/{book['address']}?pending=1").json()
    assert next(r for r in leaving if r["id"] == sword)["pending"] == "leaving"
    held = pc.get(f"/r/balances/{paddr}?pending=1").json()
    row = next(r for r in held if r["propertyid"] == book["pid"])
    assert row["arriving"] == 7 * COIN and row["units"] == 0, row

    _settled(state, rpc)
    assert pc.get(f"/r/pending/{paddr}").json() == {"address": paddr, "incoming": [],
                                                     "outgoing": []}
    landed = pc.get(f"/r/inscriptions/{paddr}?pending=1").json()
    assert [r.get("pending") for r in landed if r["id"] == sword] == [None], "it is simply theirs"
    row = next(r for r in pc.get(f"/r/balances/{paddr}?pending=1").json()
               if r["propertyid"] == book["pid"])
    assert row["units"] == 7 * COIN and "arriving" not in row, row

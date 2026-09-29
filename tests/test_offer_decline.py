"""A declined offer leaves both people's offers at once; an ended offer is in
History, newest first (2026-09-28)."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_offer import node, _pair                     # noqa: F401,E402
from test_account_accept import _offered                        # noqa: E402


def test_a_declined_offer_leaves_both_sides_at_once(node):
    app, state, rpc = node
    state.public = True
    try:
        pair = _pair(node, 80, 81)
        holder = pair["holder"][0]
        bidder = pair["bidder"][0]
        offer = _offered(pair, "1")
        assert offer in holder.get("/exchange?tab=offers").text
        assert offer in bidder.get("/exchange?tab=offers").text

        stranger = bidder.post("/account/offer/decline", json={"offer": offer, "piece": pair["piece"]})
        assert stranger.status_code == 400, "only the piece's holder may decline it"
        said = holder.post("/account/offer/decline", json={"offer": offer, "piece": pair["piece"]})
        assert said.status_code == 200 and said.json()["declined"], said.text

        for who in (holder, bidder):
            page = who.get("/exchange?tab=offers").text
            assert f'data-offer="{offer}"' not in page and f'data-answer-for="{offer}"' not in page \
                and f'data-withdraw="{offer}"' not in page, "gone from the active lists"
    finally:
        state.public = False

"""Claims: a listing hidden behind a phrase (2026-09-28: "an agnostic
feature that anybody can use in their own interactive NFT").

The owner signs an ordinary sale and files it with sha256(phrase). It is on no
public page and never announced; `/account/buy` completes it only for whoever
says the phrase, and any inscribed page can hand the phrase over with
{arcade: "claim", listing, secret}.
"""

import hashlib
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_offer import node, _pair, _settled, _sign   # noqa: F401,E402
from arcade import funding                                     # noqa: E402

PHRASE = "the fourth lock opens inward"


def _claim(who, secret, pubkey, piece, amount="0.1", phrase=PHRASE):
    said = who.post("/account/list", json={"piece": piece, "amount": amount}).json()
    return who.post("/account/list/sign", json={
        "raw": said["raw"], "amount": amount, "pubkey": pubkey.hex(),
        "claim_hash": hashlib.sha256(phrase.encode()).hexdigest(),
        "signatures": [_sign(secret, bytes.fromhex(d), funding.SINGLE_ANYONECANPAY).hex()
                       for d in said["sighashes"]]})


def test_a_claim_is_hidden_and_opens_only_with_its_phrase(node):
    pair = _pair(node, 51, 52)
    state, rpc = pair["state"], pair["rpc"]
    holder, hsecret, hpubkey, _hold = pair["holder"]
    bidder, bsecret, bpubkey, bid = pair["bidder"]
    filed = _claim(holder, hsecret, hpubkey, pair["piece"])
    assert filed.status_code == 200 and filed.json()["claim"], filed.text
    claim = filed.json()["listed"]

    net = state.messaging.network
    assert claim not in [r["id"] for r in state.listings.open_listings(net, limit=1000)], \
        "no public reader sees it"
    assert claim in [r["id"] for r in state.listings.open_listings(net, limit=1000, claims=True)]
    view = bidder.get(f"/inscriptions/{pair['piece']}/view").text
    assert claim not in view and "data-buy-listing" not in view, "its page offers no Buy"

    bare = bidder.post("/account/buy", json={"listing": claim})
    assert bare.status_code == 400 and "claim phrase" in bare.json()["detail"]
    wrong = bidder.post("/account/buy", json={"listing": claim, "secret": "open sesame"})
    assert wrong.status_code == 400 and "not this claim" in wrong.json()["detail"]
    assert "raw" not in wrong.json(), "and nothing of the leg comes back"

    asked = bidder.post("/account/buy", json={"listing": claim, "secret": "  " + PHRASE + " "})
    assert asked.status_code == 200, asked.text
    said = asked.json()
    assert said["claim"] and said["price"] == 10_000_000
    assert said["piece"].startswith("#"), said["piece"]
    done = bidder.post("/account/buy/sign", json={
        "raw": said["raw"], "listing": claim, "secret": PHRASE, "pubkey": bpubkey.hex(),
        "signatures": [_sign(bsecret, bytes.fromhex(d)).hex() for d in said["sighashes"]]})
    assert done.status_code == 200, done.text
    _settled(state, rpc)
    index = state.token_index(state.messaging)
    assert index.inscription(pair["piece"])["owner"] == bid, "the solver holds it"


def test_a_claim_hash_must_be_a_sha256(node):
    pair = _pair(node, 53, 54)
    holder, hsecret, hpubkey, _hold = pair["holder"]
    said = holder.post("/account/list", json={"piece": pair["piece"], "amount": "0.1"}).json()
    bad = holder.post("/account/list/sign", json={
        "raw": said["raw"], "amount": "0.1", "pubkey": hpubkey.hex(),
        "claim_hash": "not-a-hash",
        "signatures": [_sign(hsecret, bytes.fromhex(d), funding.SINGLE_ANYONECANPAY).hex()
                       for d in said["sighashes"]]})
    assert bad.status_code == 400 and "sha256" in bad.json()["detail"]


def test_the_bridge_and_the_wallet_carry_the_phrase():
    root = pathlib.Path(__file__).resolve().parents[1] / "arcade/web/templates"
    base = (root / "base.html").read_text()
    assert 'm.arcade === "claim"' in base and "claimFor(" in base
    wallet = (root / "wallet_js.js").read_text()
    assert "export async function claimHash" in wallet
    assert wallet.count("secret: offer.secret") == 1 and "secret: secret" in wallet

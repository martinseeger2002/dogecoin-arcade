"""An account's own Class B payload outputs, counted and swept back (Robin,
2026-09-28, after @tester found 54.50 coins in 5,450 of them that no balance
counted and no button could move)."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_offer import node, _seated, _settled, _inscribed, _signed   # noqa: F401,E402


def test_an_account_sweeps_its_own_payload_outputs_back(node):
    app, state, rpc = node
    who, secret, pubkey, address = _seated(app, state, rpc, 58)
    _inscribed(who, state, rpc, secret, pubkey, "a longer piece " * 60)

    chains = who.get("/account/dust").json()["chains"]
    assert chains, "a Class B inscription leaves payload outputs, and they are counted"
    row = chains[0]
    assert row["count"] > 0 and row["value"] >= row["count"] * 1_000_000 // 2, row

    offer = who.post("/account/dust/sweep", json={"chain": row["chain"]})
    assert offer.status_code == 200, offer.text
    said = offer.json()
    assert said["count"] == row["count"]
    assert all(i.get("script") for i in said["inputs"]), "each input names its multisig"
    done = _signed(who, secret, pubkey, said)
    assert done.status_code == 200, done.text
    txid = done.json()["txid"]
    _settled(state, rpc)
    assert rpc.call("getrawtransaction", txid, 1)["confirmations"] >= 1, "the network took it"
    assert not who.get("/account/dust").json()["chains"], "and nothing is left to sweep"


def test_nothing_to_sweep_is_said_plainly(node):
    app, state, rpc = node
    who, _secret, _pubkey, _address = _seated(app, state, rpc, 59)
    assert who.get("/account/dust").json()["chains"] == []
    tried = who.post("/account/dust/sweep", json={})
    assert tried.status_code == 400 and "nothing to sweep" in tried.json()["detail"]


def test_what_was_read_is_kept_on_disk_and_not_read_again(node, monkeypatch):
    """2026-10-18: an account with 12,000 payload transactions was read again after every restart, twice at once,
    140 s with every other page waiting on the node. What was read is kept on disk and not asked of the node again."""
    app, state, rpc = node
    who, secret, pubkey, address = _seated(app, state, rpc, 60)
    _inscribed(who, state, rpc, secret, pubkey, "a longer piece " * 60)
    first = who.get("/account/dust").json()["chains"]
    assert first and list((pathlib.Path(state.home) / "dust").glob("*.json")), "kept on disk"
    asked = []
    real = rpc.call
    monkeypatch.setattr(rpc, "call", lambda m, *a: (asked.append(m), real(m, *a))[1])
    again = who.get("/account/dust").json()["chains"]
    assert again == first and "getrawtransaction" not in asked, "nothing read twice"

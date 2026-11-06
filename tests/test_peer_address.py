"""Bare-address coins borrowed from a synced mesh arcade (arcade/peer_address.py)."""
import types

import nacl.signing
import pytest

from arcade import peer_address as pa

ADDR = "DCHxodkzaKCLjmnG4LP8uH6NKynmntmCNz"
TX = "ab" * 32


@pytest.fixture(autouse=True)
def _fresh():
    pa._served.clear(); pa._asked.clear()
    yield
    pa._served.clear(); pa._asked.clear()


def test_signed_ask_from_known_mesh_key_passes():
    k = nacl.signing.SigningKey.generate()
    h = pa.sign_ask(k, ADDR, now=1000)
    assert pa.check_ask(h, ADDR, [h["X-Arcade-Mesh-Key"]], now=1010) is None


def test_outside_callers_are_refused():
    k = nacl.signing.SigningKey.generate()
    h = pa.sign_ask(k, ADDR, now=1000)
    assert pa.check_ask({}, ADDR, [h["X-Arcade-Mesh-Key"]], now=1000)                 # unsigned
    assert pa.check_ask(h, ADDR, [], now=1000) == "that mesh key is not a known arcade"  # not in the mesh
    assert pa.check_ask(h, ADDR, [h["X-Arcade-Mesh-Key"]], now=1000 + 500) == "that ask is stale"
    assert pa.check_ask(h, "D" + ADDR[1:-1] + "x", [h["X-Arcade-Mesh-Key"]], now=1000) == "bad signature"


def test_synced():
    assert pa.synced({"blocks": 100, "headers": 101})
    assert not pa.synced({"blocks": 50, "headers": 100})
    assert not pa.synced({"blocks": 100, "headers": 100, "initialblockdownload": True})
    assert not pa.synced(None)


def test_answer_sums_marks_and_caches():
    coins = [{"txid": TX, "vout": 0, "value": 100000, "height": 5}, {"txid": TX, "vout": 1, "value": 5, "height": 5}]
    said = pa.answer(ADDR, 10, coins, {(TX, 0): "ins"}, now=1)
    assert said["balance_koinu"] == 100005 and said["coins"][0]["ins"] == "ins" and "ins" not in said["coins"][1]
    assert pa.answer(ADDR, 10, [], now=2) is said          # kept for the next asker at the same tip


class _R:
    def __init__(self, code, body): self.status_code, self._b = code, body
    def json(self): return self._b


def test_ask_peers_skips_unsynced_and_labels_source():
    k = nacl.signing.SigningKey.generate()
    seen = []

    def ask(url, **kw):
        seen.append((url, kw["headers"]["X-Arcade-Mesh-Key"]))
        if "behind" in url:
            return _R(503, {"synced": False})
        return _R(200, {"address": ADDR, "synced": True, "coins": [{"txid": TX, "vout": 0, "value": 7}]})

    said = pa.ask_peers(ADDR, ["behind.example", "good.example"], ask, k, now=1)
    assert said["from"] == "good.example" and len(seen) == 2
    assert seen[0][1] == k.verify_key.encode().hex()
    assert pa.ask_peers(ADDR, ["good.example"], ask, k, now=2) is said and len(seen) == 2   # cached


def test_wallet_assets_falls_back_to_mesh(monkeypatch):
    from arcade.web.dogefeedx import wallet_assets as wa
    k = nacl.signing.SigningKey.generate()
    monkeypatch.setattr(pa, "PEERS", lambda: (["good.example"], k, lambda url, **kw: _R(
        200, {"address": ADDR, "synced": True, "coins": [{"txid": TX, "vout": 2, "value": 9}]})))
    rows, host = wa._coins_from_mesh(ADDR)
    assert rows == [{"txid": TX, "vout": 2, "value": 9}] and host == "good.example"
    monkeypatch.setattr(pa, "PEERS", None)
    assert wa._coins_from_mesh(ADDR) == (None, "")

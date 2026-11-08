"""Every inscription-guard lookup names its chain (2026-11-08, security review). A DigiByte "D..." address is
Dogecoin's version byte, so funding.guard_for(address) alone gives it Dogecoin's guard: its small coins were judged by
Dogecoin's ord index, and DigiByte's own guard was never asked."""
import ast
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_account_claim import _sign_in                          # noqa: E402
from test_bells_luckycoin import _with                           # noqa: E402

from arcade import funding, utxos                                # noqa: E402
from arcade.config import NETWORKS                               # noqa: E402
from arcade.script import b58check_encode                        # noqa: E402

ARCADE = pathlib.Path(__file__).resolve().parent.parent / "arcade"


def test_every_guard_lookup_in_the_arcade_names_its_chain():
    unnamed = []
    for path in ARCADE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) == "guard_for":
                if len(node.args) + len(node.keywords) < 2:
                    unnamed.append(f"{path.relative_to(ARCADE.parent)}:{node.lineno}")
    assert unnamed == [], f"guard_for without its chain: {unnamed}"


def test_a_digibyte_wallets_spendable_coins_are_judged_by_digibytes_guard(client, monkeypatch):
    app, state = client
    _with(state, monkeypatch, "dgb")
    pubkey = _sign_in(app)
    address = b58check_encode(NETWORKS["dgb"].pubkeyhash_version, b"\x11" * 20)     # "D...", Dogecoin's byte too
    state.set_setting(f"address:dgb:{pubkey}", address)
    coins = [{"txid": "aa" * 32, "vout": 0, "value": 50_000, "address": address},
             {"txid": "bb" * 32, "vout": 1, "value": 9_000_000, "address": address}]
    monkeypatch.setattr(utxos, "unspent", lambda db, a: [dict(c) for c in coins] if a == address else [])

    class Dogecoins:
        def spendable(self, coins):
            raise AssertionError("Dogecoin's guard asked about a DigiByte coin")

        sort_known = held = spendable

    class DigiBytes:
        asked = []

        def spendable(self, coins):
            self.asked.append(len(coins))
            return [c for c in coins if c["txid"] != "aa" * 32]               # the small one carries an inscription

    dgb = DigiBytes()
    monkeypatch.setattr(funding, "GUARD", Dogecoins())
    monkeypatch.setattr(funding, "GUARD_APPLIES", lambda a: str(a).startswith("D"))
    monkeypatch.setattr(funding, "GUARD_BY_NET", {"doge": funding.GUARD, "dgb": dgb})
    said = app.get("/account/coins", params={"chain": "dgb", "spendable": 1})
    assert said.status_code == 200, said.text
    assert [c["txid"] for c in said.json()["coins"]] == ["bb" * 32]
    assert dgb.asked, "DigiByte's own guard judged them"

"""Chains as modules (SadFrog's side 2026-10-17: "everything should work as a module but additive system";
2026-10-18: every coin type listed with its shared nodes, enable as many as you like, and "eventually
... Digi bite and other aux pow chains"). A chain is its Params: the launcher, the panel, the state and the
badges all read it from there, so a new chain needs no code of its own."""
import argparse
import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_web import app_state, client  # noqa: F401,E402
from test_admin import LOCAL, WRITE, public  # noqa: F401,E402

from arcade import config, cores  # noqa: E402
from arcade.config import NETWORKS  # noqa: E402


def test_every_add_on_chain_is_listed_with_its_sharers_and_enabled_as_chosen(public):
    app, state = public
    rows = {r["net"]: r for r in app.get("/admin/api/state", headers=LOCAL).json()["chains"]["rows"]}
    assert {"doge", "ltc", "ltctest"} <= set(rows)
    assert rows["ltc"]["optional"] and rows["ltc"]["now"] == "off", "no Litecoin node here: off until chosen"
    ok = app.post("/admin/api/cores", headers={**LOCAL, **WRITE},
                  json={"modes": {"ltc": "borrow", "ltctest": "borrow"}})
    assert ok.status_code == 200 and ok.json()["restart"] is True
    assert state.setting("core:ltc") == "borrow" and state.setting("core:ltctest") == "borrow"
    page = app.get("/admin", headers=LOCAL).text
    assert 'id="chains"' in page and "Turn on as many as you like" in page


def test_a_chain_added_as_params_alone_runs_like_the_others(monkeypatch, tmp_path):
    """A merge-mined scrypt coin that exists only as Params: the launcher builds it, the state lists it,
    its badge and family come from its Params."""
    from arcade.web import __main__ as launcher
    from arcade.web.state import ChainContext
    coin = dataclasses.replace(NETWORKS["doge"], name="bels", label="Bellscoin", letter="B", family="BEL",
                               net_word="bellscoin", node_dir=".bells", node_conf="bells.conf",
                               auxpow_chain_id=0x10, genesis="")
    monkeypatch.setitem(NETWORKS, "bels", coin)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".bells").mkdir()
    (tmp_path / ".bells" / "bells.conf").write_text("server=1\n")
    args = argparse.Namespace(home=str(tmp_path))
    ctx = launcher._addon_context(args, coin, "")
    assert ctx.network == "bels" and ctx.conf == tmp_path / ".bells" / "bells.conf"
    assert ctx.title == "Bellscoin (BEL) \u2013 Main" and ctx.family == "BEL" and ctx.net == "bellscoin" and ctx.is_addon
    fields = launcher._addon_fields({"doge": None, "bels": ctx})
    assert fields["more"] == {"bels": ctx} and fields["doge"] is None
    borrowed = launcher._addon_context(args, coin, "borrow")
    assert isinstance(borrowed, ChainContext) and borrowed.borrowed


def test_the_rule_counts_any_add_on_chain_as_the_one_more_core():
    cores.check({"test": "here", "main": "borrow", "doge": "off", "ltc": "here", "ltctest": "off"})
    assert cores.mode({}, "ltc", "off") == "off" and cores.mode({"core:ltc": "here"}, "ltc", "off") == "here"


def test_a_chain_whose_work_cannot_be_checked_says_so():
    """DigiByte-like: several mining algorithms. Blocks still proved by hash, merkle root and linkage."""
    multi = dataclasses.replace(NETWORKS["ltc"], name="multi", pow_algo="")
    assert multi.pow_algo == "" and config.Params.pow_algo == "scrypt"

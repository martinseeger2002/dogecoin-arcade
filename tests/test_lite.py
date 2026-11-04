"""ARCADE Lite's first part: the commands, the console, and the Core-style window at /lite.

Everyone who reaches the arcade gets the window. Each command says who may run it: anyone, a signed-in user, or the
operator; help and the overview show each caller only what they may see."""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from nacl.signing import SigningKey

sys.path.insert(0, str(Path(__file__).parent))
from test_web import app_state  # noqa: F401,E402
from test_auth_web import _sign_in  # noqa: E402

from arcade.lite import commands as cmdlib  # noqa: E402
from arcade.web.app import create_app  # noqa: E402

LOCAL = {"host": "127.0.0.1:8420"}
LITE = {"x-arcade-lite": "1", "x-arcade-admin": "1"}
OUTSIDE = {"host": "arcade.example.com", "cf-connecting-ip": "198.51.100.9"}


@pytest.fixture
def app(app_state):
    app_state.set_setting("public_hosts", ["arcade.example.com"])
    yield TestClient(create_app(app_state))
    app_state.set_setting("public_hosts", [])


def caller(state, audience):
    return cmdlib.Caller(state, audience, "02" + "ab" * 32 if audience != "anyone" else None)


def test_help_lists_only_what_the_caller_may_run(app_state):
    anyone = cmdlib.run_line(caller(app_state, "anyone"), "help")
    user = cmdlib.run_line(caller(app_state, "user"), "help")
    operator = cmdlib.run_line(caller(app_state, "operator"), "help")
    assert "getnodeinfo" in anyone and "listchains" in anyone
    assert "whoami" not in anyone and "listarcades" not in anyone and "getmeshinfo" not in anyone
    assert "whoami" in user and "listarcades" in user and "getmeshinfo" not in user
    assert "getmeshinfo" in operator and "== Mesh ==" in operator
    assert cmdlib.run_line(caller(app_state, "operator"), "help listchains").startswith("listchains\n")


def test_a_command_above_the_caller_reads_as_no_command_at_all(app_state):
    for line in ("getmeshinfo", "help getmeshinfo"):
        with pytest.raises(cmdlib.CommandError, match="no command 'getmeshinfo'"):
            cmdlib.run_line(caller(app_state, "user"), line)
    with pytest.raises(cmdlib.CommandError, match="no command 'whoami'"):
        cmdlib.run_line(caller(app_state, "anyone"), "whoami")


def test_every_command_has_help_and_runs_on_a_node_with_nothing_reachable(app_state):
    op = caller(app_state, "operator")
    for cmd in cmdlib.COMMANDS.values():
        assert cmd.summary, f"{cmd.name} has no help"
        if cmd.name != "help" and " " not in cmd.usage and cmd.module not in ("wallet", "messages", "feed", "tokens", "exchange"):
            cmdlib.run(op, cmd.name)                     # no node, no mesh: it still answers


def test_only_the_operator_sees_why_a_chain_is_offline(app_state):
    for audience, sees in (("anyone", False), ("user", False), ("operator", True)):
        chains = cmdlib.run(caller(app_state, audience), "listchains")
        assert chains and all(c["online"] is False for c in chains)
        assert all(("error" in c) is sees for c in chains), audience


def test_the_console_refuses_unknown_commands_and_wrong_arguments(app_state):
    op = caller(app_state, "operator")
    with pytest.raises(cmdlib.CommandError, match="no command"):
        cmdlib.run_line(op, "sendall everything")
    with pytest.raises(cmdlib.CommandError, match="usage: getnodeinfo"):
        cmdlib.run_line(op, "getnodeinfo extra")
    with pytest.raises(cmdlib.CommandError, match="type a command"):
        cmdlib.run_line(op, "   ")


def test_the_operator_at_the_machine_gets_everything(app):
    page = app.get("/lite", headers=LOCAL)
    assert page.status_code == 200 and "data-wtab=\"peers\"" in page.text
    overview = app.get("/lite/api/overview", headers=LOCAL).json()
    assert overview["audience"] == "operator"
    assert {"node", "chains", "mesh", "arcades", "you"} <= set(overview)
    said = app.post("/lite/api/console", headers={**LOCAL, **LITE}, json={"line": "help getmeshinfo"}).json()
    assert said["ok"] and said["result"].startswith("getmeshinfo")


def test_a_visitor_from_outside_sees_the_arcade_and_a_way_to_sign_in(app):
    page = app.get("/lite", headers=OUTSIDE)
    assert page.status_code == 200 and 'href="/join"' in page.text and "data-wtab=\"peers\"" not in page.text
    overview = app.get("/lite/api/overview", headers=OUTSIDE).json()
    assert overview["audience"] == "anyone" and set(overview) == {"audience", "node", "chains"}
    assert all("error" not in c for c in overview["chains"])
    said = app.post("/lite/api/console", headers={**OUTSIDE, **LITE}, json={"line": "help"}).json()
    assert said["ok"] and "getmeshinfo" not in said["result"]
    said = app.post("/lite/api/console", headers={**OUTSIDE, **LITE}, json={"line": "getmeshinfo"}).json()
    assert not said["ok"]
    names = {c["name"] for c in app.get("/lite/api/commands", headers=OUTSIDE).json()}
    assert names == {"help", "getnodeinfo", "listchains", "listmodules", "readfeed", "readposts",
                     "listtokens", "getbook", "getpair", "listdoginals"}


def test_a_signed_in_user_from_outside_sees_their_own_account(app, app_state):
    key = SigningKey.generate()
    assert _sign_in(app, key, headers=OUTSIDE).status_code == 200
    overview = app.get("/lite/api/overview", headers=OUTSIDE).json()
    assert overview["audience"] == "user"
    assert overview["you"]["account"] == key.verify_key.encode().hex()
    assert "mesh" not in overview and "arcades" in overview
    said = app.post("/lite/api/console", headers={**OUTSIDE, **LITE}, json={"line": "whoami"}).json()
    assert said["ok"] and said["result"]["audience"] == "user"
    said = app.post("/lite/api/console", headers={**OUTSIDE, **LITE}, json={"line": "getmeshinfo"}).json()
    assert not said["ok"] and "no command" in said["error"]


def _panel(page, name):
    return page.split(f'<section data-panel="{name}"', 1)[1].split("</section>", 1)[0]


def _wallet_tabs(page):
    return page.split('<section data-panel="send"', 1)[1].split('<section data-panel="apps"')[0]


def test_send_receive_and_transactions_are_lites_own_tabs(app, app_state):
    """Drawn in the window like the Overview, never the full arcade's Wallet page: the node's wallet for the operator,
    the account's own (opened and signed in this browser) for a user, a way to sign in for a visitor."""
    operator = _wallet_tabs(app.get("/lite", headers=LOCAL).text)
    assert 'id="w-form"' in operator and 'from "/node-wallet.js"' in operator
    assert "{reviewed: true}" in operator and 'href="/wallet"' not in operator
    visitor = _wallet_tabs(app.get("/lite", headers=OUTSIDE).text)
    assert 'id="w-form"' not in visitor and 'href="/join"' in visitor
    assert _sign_in(app, SigningKey.generate(), headers=OUTSIDE).status_code == 200
    page = app.get("/lite", headers=OUTSIDE).text
    user = _wallet_tabs(page)
    assert 'from "/wallet.js"' in user and "/node-wallet.js" not in user and 'id="w-unlock"' in user
    assert "/me/wallet" not in page
    assert f'"network": "{app_state.messaging.network}"' in user, "the chain an account's words open on"


def test_the_address_book_is_lites_own_tab(app):
    operator = app.get("/lite", headers=LOCAL).text
    assert 'id="b-add"' in operator and 'from "/node-mail.js"' in operator and 'href="/contacts"' not in operator
    assert 'href="/join"' in _panel(app.get("/lite", headers=OUTSIDE).text, "addresses")
    assert _sign_in(app, SigningKey.generate(), headers=OUTSIDE).status_code == 200
    user = app.get("/lite", headers=OUTSIDE).text
    assert 'from "/messaging.js"' in user and "window.ARCADE_ME" in user and "/me/contacts" not in user


def test_join_is_a_panel_of_the_window_and_lite_serves_it_at_join(app, app_state):
    visitor = _panel(app.get("/lite", headers=OUTSIDE).text, "join")
    assert "seats open" in visitor or "seats are taken" in visitor or "not served over https" in visitor
    tunnel = _panel(app.get("/lite", headers={"host": "think.localhost:8430"}).text, "join")
    assert 'id="j-make"' in tunnel and 'id="j-in"' in tunnel, "*.localhost is a secure context, as browsers say"
    assert 'id="j-make"' not in app.get("/join", headers=LOCAL).text, "the full edition keeps its own Join page"
    app_state.set_setting("edition", "lite")
    try:
        page = app.get("/join", headers=LOCAL)
        assert page.status_code == 200 and 'id="j-make"' in page.text and "'/join': 'join'" in page.text
    finally:
        app_state.set_setting("edition", None)


def test_an_accounts_backup_is_a_panel_of_the_window(app, app_state):
    assert 'id="k-show"' not in app.get("/lite", headers=LOCAL).text, "the node's own backup is its Core wallets"
    assert _sign_in(app, SigningKey.generate(), headers=OUTSIDE).status_code == 200
    page = app.get("/lite", headers=OUTSIDE).text
    assert 'id="k-show"' in _panel(page, "backup") and 'data-go="/me/backup"' in page
    app_state.set_setting("edition", "lite")
    try:
        assert 'id="k-show"' in app.get("/me/backup", headers=OUTSIDE).text
    finally:
        app_state.set_setting("edition", None)
    assert 'id="k-show"' not in app.get("/me/backup", headers=OUTSIDE).text, "the full edition keeps its own page"


def test_options_is_a_window_for_the_operator_only(app):
    operator = app.get("/lite", headers=LOCAL).text
    assert 'id="options"' in operator and 'data-options="modules"' in operator
    assert 'data-go="/admin"' not in operator and "/admin#modules" not in operator
    assert 'id="options"' not in app.get("/lite", headers=OUTSIDE).text


def test_console_lines_come_only_from_the_lite_page(app):
    assert app.post("/lite/api/console", headers=LOCAL, json={"line": "help"}).status_code == 403
    refused = app.post("/lite/api/console", headers={**OUTSIDE, **LITE, "origin": "https://evil.example"},
                       json={"line": "help"})
    assert refused.status_code == 403

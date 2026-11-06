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

from arcade import modules as modulelib  # noqa: E402
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
                     "listtokens", "getbook", "getpair", "listdoginals", "listapps", "listthemes"}


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
    tunnel = _panel(app.get("/lite", headers={"host": "node.localhost:8430"}).text, "join")
    assert 'id="j-make"' in tunnel and 'id="j-in"' in tunnel, "*.localhost is a secure context, as browsers say"
    assert 'id="j-make"' not in app.get("/join", headers=LOCAL).text, "the full edition keeps its own Join page"
    app_state.set_setting("edition", "lite")
    try:
        page = app.get("/join", headers=LOCAL)
        assert page.status_code == 200 and 'id="j-make"' in page.text and "'/join': 'join'" in page.text
    finally:
        app_state.set_setting("edition", None)


def test_lite_serves_join_keys_in_the_window_and_says_a_claim_keeps_the_arcades_name(app, app_state, monkeypatch):
    """2026-11-04: "This node is mine" on the full /join/keys took a Lite test node's @name. Lite draws that page's
    doors in its own Join panel. Since Plan B (2026-11-04) an account made the owner leaves the arcade its name."""
    assert 'id="password-save"' in app.get("/join/keys", headers=LOCAL).text, "the full edition keeps its own page"
    app_state.set_setting("edition", "lite")
    try:
        page = app.get("/join/keys", headers={"host": "node.localhost:8430"}).text
        join = _panel(page, "join")
        for door in ("j-make", "j-in", "j-words-in", "j-ext", "j-op-in", "j-op-set"):
            assert f'id="{door}"' in join, door
        assert 'id="password-save"' not in page and "'/join/keys': 'join'" in page
    finally:
        app_state.set_setting("edition", None)
    monkeypatch.setattr(type(app_state), "operator_tag", lambda self: "pumpkin")
    assert "and its name, @pumpkin" in _panel(app.get("/lite", headers=OUTSIDE).text, "join")
    assert _sign_in(app, SigningKey.generate(), headers=OUTSIDE).status_code == 200
    claim = _panel(app.get("/lite", headers=OUTSIDE).text, "join")
    assert 'id="j-claim"' in claim and "@pumpkin keeps its name" in claim and 'id="j-op-set"' not in claim


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
    assert "/admin#modules" not in operator and operator.count('data-open-tab="admin"') == 1, "only Admin: every control"
    assert 'id="options"' not in app.get("/lite", headers=OUTSIDE).text


def test_messages_is_a_tab_of_the_core_in_every_edition(app, app_state):
    operator = app.get("/lite", headers=LOCAL).text
    assert 'data-tab="messages"' in operator and 'from "/node-mail.js"' in _panel(operator, "messages") + operator
    assert "window.ARCADE_CSRF" in operator and "window.liteLocal = " not in operator, "the node's: run by the node"
    assert _sign_in(app, SigningKey.generate(), headers=OUTSIDE).status_code == 200
    user = app.get("/lite", headers=OUTSIDE).text
    assert 'from "/messaging.js"' in user and "window.liteLocal = " in user and "window.ARCADE_CSRF" not in user
    app_state.set_setting("edition", "lite")
    try:
        lite = app.get("/lite", headers=LOCAL).text
        assert 'data-tab="messages"' in lite and 'data-tab="m-feed"' not in lite, "Messages stays; Feed is off"
        with pytest.raises(ValueError):
            modulelib.switch(app_state.settings(), "messages", False)
    finally:
        app_state.set_setting("edition", None)


def test_each_module_on_is_a_tab_that_opens_its_page_inside_the_window(app, app_state):
    full = app.get("/lite", headers=LOCAL).text
    bar = full.split('<div class="toolbar"', 1)[1].split("</div>", 1)[0]
    for mid, page in (("ords", "/explore"), ("feed", "/feed"), ("exchange", "/exchange")):
        assert f'data-tab="m-{mid}"' in bar and f'data-src="{page}"' in bar, mid
        assert f'<iframe title="' in _panel(full, f"m-{mid}") and f'data-src="{page}"' in _panel(full, f"m-{mid}")
    assert 'data-tab="m-desktop"' not in bar, "a look is worn, not opened"
    assert 'data-tab="admin"' in bar and 'data-src="/admin"' in _panel(full, "admin"), "Admin: a tab of the window"
    visitor = app.get("/lite", headers=OUTSIDE).text
    assert 'data-tab="admin"' not in visitor and 'data-tab="m-feed"' in visitor
    app_state.set_setting("edition", "lite")
    try:
        lite = app.get("/lite", headers=LOCAL).text
        assert 'data-tab="m-' not in lite and 'data-tab="apps"' not in lite, "Lite: no module is on yet"
        app_state.set_setting("modules", {"ords": True})
        assert 'data-tab="m-ords"' in app.get("/lite", headers=LOCAL).text
    finally:
        app_state.set_setting("edition", None)
        app_state.set_setting("modules", None)
    # a page in the window is drawn without its own bar, and its links to another tab open there (base.html)
    assert "window.parent.liteWindow" not in app.get("/wallet", headers=LOCAL).text, "the full edition's pages"
    app_state.set_setting("edition", "lite")
    try:
        assert "window.parent.liteWindow" in app.get("/wallet", headers=LOCAL).text
    finally:
        app_state.set_setting("edition", None)


def test_the_operator_says_who_may_use_each_module(app, app_state):
    def access(mid, level):
        return app.post("/admin/api/modules", headers={**LOCAL, "x-arcade-admin": "1"},
                        json={"module": mid, "access": level})
    assert access("feed", "nobody").status_code == 400 and access("messages", "operator").status_code == 400
    try:
        said = access("feed", "operator").json()
        assert [m["access"] for m in said["modules"] if m["id"] == "feed"] == ["operator"]
        assert 'data-tab="m-feed"' in app.get("/lite", headers=LOCAL).text, "the operator keeps it"
        assert 'data-tab="m-feed"' not in app.get("/lite", headers=OUTSIDE).text
        assert app.get("/feed", headers=OUTSIDE).status_code == 404 and app.get("/feed", headers=LOCAL).status_code == 200
        refused = app.post("/lite/api/console", headers={**OUTSIDE, **LITE}, json={"line": "readfeed"}).json()
        assert not refused["ok"] and "off on this arcade" in refused["error"]
        assert _sign_in(app, SigningKey.generate(), headers=OUTSIDE).status_code == 200
        assert 'data-tab="m-feed"' not in app.get("/lite", headers=OUTSIDE).text, "a user is not the operator"
        access("feed", "user")
        assert 'data-tab="m-feed"' in app.get("/lite", headers=OUTSIDE).text and \
            app.get("/feed", headers=OUTSIDE).status_code == 200, "signed in: open"
        access("ords", "operator")
        assert app.get("/explore", headers=OUTSIDE).status_code == 404
        assert app.get("/r/inscriptions", headers=OUTSIDE).status_code != 404, "its read API stays open to the mesh"
    finally:
        app_state.set_setting(modulelib.ACCESS_SETTING, None)


def test_each_person_hides_the_tabs_they_do_not_want(app, app_state):
    post = lambda body, headers: app.post("/lite/api/tabs", headers={**headers, **LITE}, json=body)
    assert post({"hidden": ["feed"]}, OUTSIDE).status_code == 403, "a visitor's choice stays in their browser"
    assert app.post("/lite/api/tabs", headers=OUTSIDE, json={"hidden": []}).status_code == 403, "from the window only"
    key = SigningKey.generate()
    assert _sign_in(app, key, headers=OUTSIDE).status_code == 200
    try:
        said = post({"hidden": ["feed", "nothing-such"]}, OUTSIDE).json()
        assert [t["id"] for t in said["tabs"] if t["hidden"]] == ["feed"] and said["kept"]
        page = app.get("/lite", headers=OUTSIDE).text
        assert 'data-tab="m-feed"' not in page and 'data-tab="m-exchange"' in page
        assert 'data-tab="m-feed"' in app.get("/lite", headers=LOCAL).text, "the operator's own view is apart"
        assert app.get("/feed", headers=OUTSIDE).status_code == 200, "hidden is not closed"
    finally:
        app_state.set_setting(modulelib.TABS_SETTING, None)


def test_the_apps_tab_is_the_apps_module_and_holds_the_app_store(app, app_state):
    full = app.get("/lite", headers=LOCAL).text
    tab = _panel(full, "apps")
    assert 'data-tab="apps"' in full and 'id="a-list"' in tab and "<legend>Modules</legend>" not in tab
    assert "Playing an app needs" not in tab, "Ord indexes are on in the full edition"
    said = app.post("/lite/api/console", headers={**OUTSIDE, **LITE}, json={"line": "listapps new 5"}).json()
    assert said["ok"] and isinstance(said["result"], list), said
    app_state.set_setting("edition", "lite")
    try:
        lite = app.get("/lite", headers=LOCAL).text
        assert 'data-tab="apps"' not in lite and '<section data-panel="apps"' not in lite, "off: the tab is gone"
        app_state.set_setting("modules", {"apps": True})
        lite = app.get("/lite", headers=LOCAL).text
        assert 'data-tab="apps"' in lite and "Playing an app needs the Ord indexes module" in _panel(lite, "apps")
        assert 'href="/#apps"' in app.get("/wallet", headers=LOCAL).text, "and the bar on every page shows it"
        app_state.set_setting("modules", None)
        assert 'href="/#apps"' not in app.get("/wallet", headers=LOCAL).text
    finally:
        app_state.set_setting("edition", None)
        app_state.set_setting("modules", None)


def test_console_lines_come_only_from_the_lite_page(app):
    assert app.post("/lite/api/console", headers=LOCAL, json={"line": "help"}).status_code == 403
    refused = app.post("/lite/api/console", headers={**OUTSIDE, **LITE, "origin": "https://evil.example"},
                       json={"line": "help"})
    assert refused.status_code == 403


def test_a_visitor_s_console_lines_are_limited_and_the_operator_s_are_not(app):
    """2026-11-03, a test machine's review: /lite/api/console is public on every arcade, so a visitor gets at most
    CONSOLE_PER_MINUTE lines a minute, as the full arcade's terminal; the operator at the node is never held back."""
    from arcade.lite import web as liteweb
    for _ in range(liteweb.CONSOLE_PER_MINUTE):
        assert app.post("/lite/api/console", headers={**OUTSIDE, **LITE}, json={"line": "help"}).status_code == 200
    held = app.post("/lite/api/console", headers={**OUTSIDE, **LITE}, json={"line": "help"})
    assert held.status_code == 429 and "lines a minute" in held.json()["error"]
    for _ in range(liteweb.CONSOLE_PER_MINUTE + 5):
        assert app.post("/lite/api/console", headers={**LOCAL, **LITE}, json={"line": "help"}).status_code == 200


def test_window_build_guides_everyone_and_lets_the_operator_share_themes(app):
    operator = app.get("/lite", headers=LOCAL).text
    assert "data-build>Build…" in operator and 'id="build"' in operator
    assert "arcade-cli new theme my-theme" in operator and "sharetheme my-theme" in operator, "the guide's steps"
    assert 'data-btab="share"' in operator and 'data-btab="others"' in operator
    visitor = app.get("/lite", headers=OUTSIDE).text
    assert "arcade-cli new theme my-theme" in visitor, "anyone may learn to make one"
    assert 'data-btab="share"' not in visitor and "taketheme" not in visitor, "offering and taking are the operator's"
    # API keys: the operator's own and the say over users'; a visitor is told how to get one
    assert 'data-btab="keys"' in operator and 'id="k-users"' in operator and 'data-btab="apiref"' in operator
    assert 'data-btab="keys"' in visitor and 'id="k-make"' not in visitor and "Run an arcade of your own" in visitor


def test_the_operator_from_outside_has_admin_as_a_tab_and_admin_opens_inside_the_window(app, app_state):
    """From outside, the admin password's session is kept for /admin alone, so the window at / never sees it: the
    operator's account shows the Admin tab, and Admin opened on its own goes into the window, under its menus."""
    key = SigningKey.generate()
    assert _sign_in(app, key, headers=OUTSIDE).status_code == 200
    assert app.post("/admin/api/operator", headers={**LOCAL, "x-arcade-admin": "1"},
                    json={"pubkey": key.verify_key.encode().hex()}).status_code == 200
    assert app.post("/admin/api/password", headers={**LOCAL, "x-arcade-admin": "1"},
                    json={"password": "correct horse battery"}).status_code == 200
    assert app.post("/admin/login", headers=OUTSIDE, json={"password": "correct horse battery"}).status_code == 200
    app_state.set_setting("edition", "lite")
    try:
        window = app.get("/lite", headers=OUTSIDE).text
        bar = window.split('<div class="toolbar"', 1)[1].split("</div>", 1)[0]
        assert 'data-tab="admin"' in bar and 'data-src="/admin"' in _panel(window, "admin")
        assert 'data-open-tab="admin"' in window, "Settings -> Admin, as at the machine"
        assert "[hidden]{display:none !important}" in window, "a hidden panel takes no room from the one shown"
        admin = app.get("/admin", headers=OUTSIDE)
        assert admin.status_code == 200 and "location.replace('/#admin')" in admin.text
        assert "location.replace('/#admin')" not in app.get("/feed", headers=OUTSIDE).text, "only a tab's own page"
        app.cookies.clear()
        assert _sign_in(app, SigningKey.generate(), headers=OUTSIDE).status_code == 200
        someone = app.get("/lite", headers=OUTSIDE).text
        assert 'data-tab="admin"' not in someone and 'data-open-tab="admin"' not in someone, "nobody else's"
    finally:
        app_state.set_setting("edition", None)
        app_state.set_setting("operator", None)
    assert "location.replace('/#admin')" not in app.get("/admin", headers=LOCAL).text, "Full has no window to open"

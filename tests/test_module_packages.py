"""Node modules in the arcade (arcade/lite/commands.py, arcade/lite/web.py, arcade/modules.py): installed from a folder
by the operator, switched on like any module, their commands in the console and their pages under /m/<name>/, and
what they prepare to spend made an offer that only the caller's own wallet signs."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_web import app_state  # noqa: F401,E402
from test_module_sandbox import _module, needs_sandbox  # noqa: E402
from test_module_host import Chain, _key  # noqa: E402

from arcade import funding, modhost, modules as modulelib  # noqa: E402
from arcade.lite import commands as cmdlib  # noqa: E402
from arcade.txbuild import p2pkh_script  # noqa: E402


@pytest.fixture
def host(app_state, tmp_path):
    chain = Chain()
    app_state.module_host = modhost.Host(app_state.home, chain)
    yield app_state.module_host, chain
    app_state.module_host.stop()


def _op(state):
    return cmdlib.Caller(state, "operator", local=True)


def test_a_commands_form_reaches_the_window_with_its_arguments_and_the_tab_says_it_is_a_package(app_state, host,
                                                                                                tmp_path):
    """2026-11-05: each step of a module gets a control in the Lite window as well as its console line. The manifest's
    form (words only, packages._check_form) goes out with /lite/api/commands; the window builds the controls and runs
    the line a person could type."""
    form = {"title": "Greet", "button": "Say hello", "fields": {"who": {"label": "Whom", "type": "choice",
                                                                        "choices": [["you", "You"], ["me", "Me"]]}}}
    folder = _module(tmp_path, change=lambda m: m["commands"]["hello"].update(form=form))
    cmdlib.run_line(_op(app_state), f"installmodule {folder}")
    cmdlib.run_line(_op(app_state), "install hello")
    found = {c.name: c for c in cmdlib.available(cmdlib.Caller(app_state))}
    assert found["hello"].form == form and found["hello"].module == "hello" and found["sneak"].form is None
    tab = next(t for t in modulelib.tabs(app_state.settings(), "anyone") if t["id"] == "hello")
    assert tab["package"] is True and tab["page"] == "/m/hello/"
    from fastapi.testclient import TestClient
    from arcade.web.app import create_app
    listed = {c["name"]: c for c in TestClient(create_app(app_state)).get("/lite/api/commands").json()}
    assert listed["hello"]["form"] == form and listed["hello"]["args"] == ["who"] and listed["hello"]["module"] == "hello"
    assert listed["hello"]["order"] == 0, "the manifest's order, the steps' own"
    assert "form" not in listed["sneak"] and listed["sneak"]["module"] == "hello" and "module" not in listed["help"]


@needs_sandbox
def test_a_module_runs_confined_and_asks_the_chain_only_what_it_named(app_state, host, tmp_path):
    the_host, chain = host
    cmdlib.run_line(_op(app_state), f"installmodule {_module(tmp_path)}")
    (app_state.home / "settings.json").write_text(json.dumps({**app_state.settings(), "secret": "the keys"}))
    with pytest.raises(cmdlib.CommandError, match="module is off"):
        cmdlib.run_line(_op(app_state), "hello")
    assert "on: hello" in cmdlib.run_line(_op(app_state), "install hello")
    assert cmdlib.run_line(cmdlib.Caller(app_state), "hello you") == {"hello": "you", "height": 812,
                                                                       "audience": "anyone"}
    for what in ("home", "socket", "proc"):
        with pytest.raises(cmdlib.CommandError, match="PermissionError|Operation not permitted"):
            cmdlib.run_line(_op(app_state), f"sneak {what}")
    with pytest.raises(cmdlib.CommandError, match="may not ask main for dumpprivkey"):
        cmdlib.run_line(_op(app_state), "sneak rpc")
    assert cmdlib.run_line(_op(app_state), "sneak data") == "mine", "it writes its own folder"
    assert (app_state.home / "module-data" / "hello" / "kept.txt").read_text() == "mine"
    assert chain.asked == ["getblockcount"]

    assert "off: hello" in cmdlib.run_line(_op(app_state), "remove hello")
    assert "hello" not in the_host.running, "its process stops with its switch"


def test_the_console_lists_a_modules_commands_and_the_operator_alone_keeps_one(app_state, host, tmp_path):
    folder = _module(tmp_path)
    with pytest.raises(cmdlib.CommandError, match="arcade's own machine"):
        cmdlib.run_line(cmdlib.Caller(app_state, "operator"), f"installmodule {folder}")
    with pytest.raises(cmdlib.CommandError, match="no command"):
        cmdlib.run_line(cmdlib.Caller(app_state, "user", "ab" * 32), f"installmodule {folder}")
    said = cmdlib.run_line(_op(app_state), f"checkmodule {folder}")
    assert said["commands"] == ["hello", "pay", "sneak"] and said["reads"] == {"main": ["getblockcount"]}
    pid = cmdlib.run_line(_op(app_state), f"installmodule {folder}")["id"]
    assert (app_state.home / "packages" / pid / "module.py").is_file()
    cmdlib.run_line(_op(app_state), "install hello")
    assert "hello [who]" in cmdlib.run_line(cmdlib.Caller(app_state), "help")
    assert "pay" not in cmdlib.run_line(cmdlib.Caller(app_state), "help"), "a user's command, for users"
    assert cmdlib.run_line(cmdlib.Caller(app_state), "help hello") == "hello [who]\n\nSay hello."
    row = next(m for m in modulelib.describe(app_state.settings())["modules"] if m["id"] == "hello")
    assert row["on"] and row["kind"] == "node" and row["package"] == pid and row["page"] == ["/m/hello/", "/m/hello/"]

    clash = _module(tmp_path / "x", change=lambda m: m["commands"].update(send={}))
    with pytest.raises(cmdlib.CommandError, match="takes a name the arcade's own"):
        cmdlib.run_line(_op(app_state), f"installmodule {clash}")
    newer = _module(tmp_path / "y", change=lambda m: m.update(version="1.0.1"))
    assert cmdlib.run_line(_op(app_state), f"installmodule {newer}")["id"] != pid
    assert not (app_state.home / "packages" / pid).exists(), "a new version replaces the old"
    assert "hello" not in modulelib.enabled(app_state.settings()), "and is off until switched on again"
    assert cmdlib.run_line(_op(app_state), "removemodule hello") == "removed hello"
    assert not app_state.settings().get("packages")


@needs_sandbox
def test_what_a_module_prepares_to_spend_is_the_callers_own_coins_and_an_offer_to_sign(app_state, host, tmp_path):
    _, chain = host
    pub, chain.address = _key()
    cmdlib.run_line(_op(app_state), f"installmodule {_module(tmp_path)}")
    cmdlib.run_line(_op(app_state), "install hello")
    made = []
    user = cmdlib.Caller(app_state, "user", "cd" * 32, address=lambda c: chain.address,
                         offer=lambda network, unsigned: made.append((network, unsigned)) or {"offer": "o1"})

    said = cmdlib.run_line(user, f"pay {'aa' * 32}")
    assert said == {"said": "here", "sign": {"offer": "o1"}}
    network, unsigned = made[0]
    coin = {"txid": "aa" * 32, "vout": 0, "value": 150_000_000, "address": chain.address}
    assert network == "main" and unsigned.inputs == [coin] and unsigned.fee == 1000
    assert unsigned.sighashes == [funding.sighash([coin], unsigned.outputs, 0, p2pkh_script(chain.address)).hex()]
    assert unsigned.what == "Hello: a test"
    with pytest.raises(cmdlib.CommandError, match="not an unspent coin of yours"):
        cmdlib.run_line(user, f"pay {'bb' * 32}")
    with pytest.raises(cmdlib.CommandError, match="more than it may"):
        cmdlib.run_line(user, f"pay {'aa' * 32} 60000")
    told = cmdlib.run_line(cmdlib.Caller(app_state, "user", "cd" * 32, address=lambda c: chain.address),
                           f"pay {'aa' * 32}")
    assert told["sign"] is None and told["prepared"]["sighashes"] == unsigned.sighashes, "arcade-cli cannot sign"


def test_a_callers_key_reaches_a_module_only_when_it_is_their_addresss(app_state):
    pub, address = _key()
    other, _ = _key()
    def who(key):
        return cmdlib.module_caller(cmdlib.Caller(app_state, "user", "cd" * 32, address=lambda c: address,
                                                  pubkeys={"main": key.hex()}), {"main": []})
    assert who(pub)["pubkey"] == {"main": pub.hex()} and who(pub)["address"] == {"main": address}
    assert who(other)["pubkey"] == {}, "a key that is not this address's is dropped"


@needs_sandbox
def test_a_modules_page_is_served_sandboxed_to_whom_its_manifest_says_and_only_while_it_is_on(app_state, host,
                                                                                             tmp_path):
    """A visitor from outside reaches a page for anyone through the door; a page for users is not theirs."""
    from fastapi.testclient import TestClient
    from arcade.web.app import create_app
    outside = {"host": "arcade.example.com", "cf-connecting-ip": "198.51.100.9"}
    app_state.set_setting("public_hosts", ["arcade.example.com"])
    app = TestClient(create_app(app_state))
    cmdlib.run_line(_op(app_state), f"installmodule {_module(tmp_path)}")
    assert app.get("/m/hello/", headers=outside).status_code == 404, "off"
    cmdlib.run_line(_op(app_state), "install hello")
    got = app.get("/m/hello/x?q=1", headers=outside)
    assert got.status_code == 200 and got.text == "<p>GET x 1</p>"
    assert got.headers["content-security-policy"].startswith("sandbox allow-scripts")
    assert "allow-same-origin" not in got.headers["content-security-policy"]
    assert app.post("/m/hello/x", headers=outside, content="a").status_code in (403, 404), "a visitor only reads"
    assert app.get("/m/nothere/", headers=outside).status_code == 404

    cmdlib.run_line(_op(app_state), f"installmodule {_module(tmp_path / 'u', change=lambda m: m.update(pages='user'))}")
    cmdlib.run_line(_op(app_state), "install hello")
    assert app.get("/m/hello/x", headers=outside).status_code == 404, "a page for users is not a visitor's"
    assert app.get("/m/hello/x").status_code == 200, "the operator, at the machine, may open it"
    app_state.set_setting("public_hosts", [])

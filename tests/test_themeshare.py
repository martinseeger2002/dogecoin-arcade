"""Themes offered between arcades (arcade/mesh/themeshare.py): an operator offers an installed theme, linked arcades see
it, and one of their operators takes it -- checked against its id, refused if it is anything but a theme's own files."""
import asyncio
import json
from pathlib import Path

import pytest

from arcade import packages
from arcade.lite import commands as cmdlib
from arcade.mesh import themeshare
from arcade.mesh.themeshare import ThemeShare


class _State:
    def __init__(self, home: Path):
        self.home, self._s = home, {}

    def settings(self):
        return json.loads(json.dumps(self._s))

    def setting(self, name, default=None):
        return self._s.get(name, default)

    def set_setting(self, name, value):
        if value is None:
            self._s.pop(name, None)
        else:
            self._s[name] = value


class _Node:
    def __init__(self):
        self.links = {}


A_ID, B_ID = "aa" * 32, "bb" * 32


class _Link:
    """A link as one side holds it: `peer_id` is the far side, and what is sent arrives there on its link back."""
    def __init__(self, peer_id, far):
        self.peer_id, self.far, self.back, self.tamper = peer_id, far, None, None

    async def send(self, header, blob=b""):
        if self.tamper:
            header, blob = self.tamper(dict(header), blob)
        await self.far.on_frame(self.back, header, blob)


def _pair(tmp_path):
    """Arcades a and b, linked: (a's state, a, b's state, b)."""
    a_state, b_state = _State(tmp_path / "a"), _State(tmp_path / "b")
    a, b = ThemeShare(a_state), ThemeShare(b_state)
    a.node, b.node = _Node(), _Node()
    a_to_b, b_to_a = _Link(B_ID, b), _Link(A_ID, a)
    a_to_b.back, b_to_a.back = b_to_a, a_to_b
    a.node.links[B_ID], b.node.links[A_ID] = a_to_b, b_to_a
    return a_state, a, b_state, b


def _install_theme(state, tmp_path, name="sunset"):
    folder = packages.template(tmp_path / "src", name)
    pid, row = packages.install(state.home, folder)
    state.set_setting(packages.SETTING, {**(state.setting(packages.SETTING) or {}), pid: row})
    return pid


def _run(coro):
    return asyncio.run(coro)


def test_an_offered_theme_is_seen_and_taken_whole(tmp_path):
    a_state, a, b_state, b = _pair(tmp_path)
    pid = _install_theme(a_state, tmp_path)
    assert a.offers() == [], "nothing is offered until the operator says so"
    a_state.set_setting(themeshare.SHARED, [pid])

    async def go():
        await a.rebalance()                                       # a says what it offers to b
        offered = b.offered()
        assert [o["id"] for o in offered] == [pid] and offered[0]["from"] == [A_ID]
        peer, files = await b.fetch(pid)
        return peer, files
    peer, files = _run(go())
    assert peer == A_ID and packages.package_id(files) == pid and "manifest.json" in files


def test_a_theme_that_does_not_match_its_id_is_not_kept(tmp_path):
    def tamper(header, blob):
        if header.get("op") == "part" and blob:
            got = themeshare.unpack(blob) or {}
            got["README.md"] = b"changed on the way"
            blob = themeshare.pack(got)
        return header, blob
    a_state, a, b_state, b = _pair(tmp_path)
    pid = _install_theme(a_state, tmp_path)
    a_state.set_setting(themeshare.SHARED, [pid])
    a.node.links[B_ID].tamper = tamper                      # what a serves is changed on its way to b

    async def go():
        await a.rebalance()
        with pytest.raises(RuntimeError, match="does not match"):
            await b.fetch(pid)
    _run(go())


def test_only_a_themes_own_file_names_are_taken():
    good = {"manifest.json": b"{}", "README.md": b"x", "files/sans.woff2": b"\0"}
    assert themeshare.unpack(themeshare.pack(good)) == good
    for bad in ("../../.bashrc", "files/../../x.woff2", "module.py", "files/x.js", "/etc/passwd"):
        assert themeshare.unpack(themeshare.pack({**good, bad: b"x"})) is None, bad
    assert themeshare.unpack(b"not zlib") is None


def test_the_operator_offers_and_lists_themes_from_the_console(tmp_path):
    state = _State(tmp_path / "home")
    pid = _install_theme(state, tmp_path)
    state.mesh = type("M", (), {"themeshare": ThemeShare(state)})()
    op = cmdlib.Caller(state, "operator", local=True)
    assert "offered" in cmdlib.run_line(op, "sharetheme sunset")
    assert state.setting(themeshare.SHARED) == [pid]
    said = cmdlib.run_line(op, "sharedthemes")
    assert said["mine"][0]["shared"] and said["offered"] == [] and said["mesh"]
    assert "no longer offered" in cmdlib.run_line(op, "sharetheme sunset off")
    with pytest.raises(cmdlib.CommandError, match="no linked arcade offers"):
        cmdlib.run_line(op, "taketheme " + "ab" * 8)
    with pytest.raises(cmdlib.CommandError, match="no command"):
        cmdlib.run_line(cmdlib.Caller(state, "user", "02" + "ab" * 32), "sharetheme sunset")

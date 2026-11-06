"""Without Linux-only modules (Windows), the startup path still imports (2026-11-06); with them, nothing changes."""
import builtins
import importlib
import sys

import pytest

LINUX_ONLY = {"resource", "fcntl"}


@pytest.fixture
def no_linux_modules(monkeypatch):
    real = builtins.__import__

    def fake(name, *a, **kw):
        if name in LINUX_ONLY:
            raise ImportError(f"No module named {name!r}")
        return real(name, *a, **kw)
    for mod in LINUX_ONLY:
        monkeypatch.delitem(sys.modules, mod, raising=False)
    monkeypatch.setattr(builtins, "__import__", fake)
    yield


@pytest.mark.parametrize("modname", ["arcade.sandbox", "arcade.web.supervisor", "arcade.core_copy"])
def test_imports_without_linux_modules(no_linux_modules, monkeypatch, modname):
    parent_name, _, leaf = modname.rpartition(".")
    parent = importlib.import_module(parent_name)
    saved, saved_attr = sys.modules.pop(modname, None), getattr(parent, leaf, None)
    try:
        mod = importlib.import_module(modname)
        assert getattr(mod, "resource", getattr(mod, "fcntl", "x")) is None
    finally:
        sys.modules.pop(modname, None)
        if saved is not None:
            sys.modules[modname] = saved
            setattr(parent, leaf, saved_attr)
        elif hasattr(parent, leaf):
            delattr(parent, leaf)          # imported here for the first time: the next import starts clean


def test_linux_keeps_its_modules_and_limits():
    if sys.platform != "linux":
        pytest.skip("Linux only")
    import resource
    from arcade import core_copy, sandbox
    from arcade.web import supervisor
    assert sandbox.resource is resource and supervisor.fcntl is not None and core_copy.fcntl is not None


def test_sandbox_refuses_modules_without_resource(monkeypatch):
    from arcade import sandbox
    monkeypatch.setattr(sandbox, "resource", None)
    monkeypatch.setattr(sandbox.sys, "platform", "win32")
    with pytest.raises(sandbox.SandboxError):
        sandbox.confine([], [])

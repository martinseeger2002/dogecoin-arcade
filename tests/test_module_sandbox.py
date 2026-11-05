"""Node modules, the package and the sandbox (arcade/packages.py, arcade/sandbox.py, arcade/modrun.py): what a module's
folder may hold and ask for, and the confinement its process puts itself in before its code runs."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from arcade import packages, sandbox

needs_sandbox = pytest.mark.skipif(bool(sandbox.available()), reason=f"no sandbox here: {sandbox.available()}")

MODULE_PY = '''
import os, socket
def command_hello(ctx, who="world"):
    return {"hello": who, "height": ctx.rpc("main", "getblockcount"), "audience": ctx.caller["audience"]}
def command_sneak(ctx, what):
    if what == "home":
        return open(os.environ.get("HOME_FILE", "") or ctx.data.parent.parent / "settings.json").read()
    if what == "socket":
        return str(socket.socket())
    if what == "proc":
        return open(f"/proc/{os.getppid()}/environ").read()
    if what == "rpc":
        return ctx.rpc("main", "dumpprivkey", "x")
    (ctx.data / "kept.txt").write_text("mine")
    return (ctx.data / "kept.txt").read_text()
def command_pay(ctx, txid, fee=1000):
    return {"said": "here", "prepared": {"chain": "main", "inputs": [{"txid": txid, "vout": 0}],
            "outputs": [[150_000_000 - int(fee), "6a0474657374"]], "what": "a test"}}
def page(ctx, method, path, query, body):
    return {"type": "text/html", "body": f"<p>{method} {path} {query.get('q', '')}</p>"}
'''


def _module(tmp_path, name="hello", change=None, code=MODULE_PY) -> Path:
    folder = tmp_path / name
    folder.mkdir(parents=True)
    manifest = {"kind": "module", "api": 1, "name": name, "version": "1.0.0", "title": name.title(),
                "needs": {"chains": {"main": ["getblockcount"]}, "max_fee": 50_000},
                "commands": {"hello": {"audience": "anyone", "args": ["[who]"], "help": "Say hello."},
                             "sneak": {"audience": "anyone", "args": ["what"]},
                             "pay": {"audience": "user", "args": ["txid", "[fee]"]}}}
    if change:
        change(manifest)
    (folder / "manifest.json").write_text(json.dumps(manifest))
    (folder / "module.py").write_text(code)
    return folder


@pytest.mark.parametrize("change,said", [
    (lambda m: m["needs"]["chains"]["main"].append("dumpprivkey"), "not a read"),
    (lambda m: m["needs"]["chains"]["main"].append("getbalance"), "not a read"),
    (lambda m: m["needs"]["chains"]["main"].append("getwalletinfo"), "not a read"),
    (lambda m: m["needs"].update(hosts=["example.com"]), "no host"),
    (lambda m: m["needs"].update(max_fee=10 ** 9), "max_fee"),
    (lambda m: m["commands"].update(BAD={}), "lower-case"),
    (lambda m: m["commands"]["hello"].update(audience="root"), "audience"),
    (lambda m: m["commands"]["hello"].update(args=["[who]", "what"]), "comes after"),
    (lambda m: m["commands"]["hello"].update(form={"script": "x"}), "nothing else"),
    (lambda m: m["commands"]["hello"].update(form={"fields": {"whom": {}}}), "own arguments"),
    (lambda m: m["commands"]["hello"].update(form={"fields": {"who": {"type": "html"}}}), "type is one of"),
    (lambda m: m["commands"]["hello"].update(form={"fields": {"who": {"type": "choice"}}}), "has choices when"),
    (lambda m: m["commands"]["hello"].update(form={"fields": {"who": {"type": "choice", "choices": [["a b", "x"]]}}}),
     "each value a word"),
    (lambda m: m["commands"]["hello"].update(form={"title": "x" * 81}), "at most 80"),
])
def test_a_module_that_asks_for_the_wallet_or_too_much_is_refused(tmp_path, change, said):
    with pytest.raises(packages.PackageError, match=said):
        packages.read(_module(tmp_path, change=change))


def test_a_module_holds_its_python_and_page_files_only(tmp_path):
    folder = _module(tmp_path)
    (folder / "run.sh").write_text("#!/bin/sh")
    with pytest.raises(packages.PackageError, match="run.sh is not something a module may hold"):
        packages.read(folder)
    (folder / "run.sh").unlink()
    (folder / "module.py").unlink()
    with pytest.raises(packages.PackageError, match="runs its module.py"):
        packages.read(folder)



CONFINED = """
import socket, sys
from arcade import sandbox
package, data, home = sys.argv[1:]
sandbox.confine(read=[package], write=[data])
said = {}
for what, attempt in (("home", lambda: open(home + "/settings.json").read()), ("proc", lambda: open("/proc/1/environ").read()),
                      ("socket", lambda: socket.socket()), ("package", lambda: open(package + "/module.py").read()[:6]),
                      ("data", lambda: open(data + "/kept.txt", "w").write("mine"))):
    try:
        said[what] = str(attempt())
    except OSError as exc:
        said[what] = type(exc).__name__
print(__import__("json").dumps(said))
"""


@needs_sandbox
def test_a_confined_process_reads_its_package_writes_its_folder_and_reaches_nothing_else(tmp_path):
    home = tmp_path / "home"
    (home / "module-data" / "hello").mkdir(parents=True)
    (home / "settings.json").write_text('{"secret": "the keys"}')
    package = _module(tmp_path)
    root = str(Path(__file__).parent.parent)
    out = subprocess.run([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {root!r})\n" + CONFINED,
                          str(package), str(home / "module-data" / "hello"), str(home)],
                         capture_output=True, text=True, timeout=60, check=True).stdout
    assert json.loads(out) == {"home": "PermissionError", "proc": "PermissionError", "socket": "PermissionError",
                               "package": "\nimpor", "data": "4"}
    assert (home / "module-data" / "hello" / "kept.txt").read_text() == "mine"


NO_RING = """
import ctypes, errno, os, sys
from arcade import sandbox
package, data = sys.argv[1:]
libc = ctypes.CDLL(None, use_errno=True)
def call(nr, *args):
    got = libc.syscall(nr, *args)
    return "EPERM" if got == -1 and ctypes.get_errno() == errno.EPERM else ("ok" if got >= 0 else errno.errorcode.get(ctypes.get_errno(), "?"))
KILL = 62 if os.uname().machine == "x86_64" else 129
before = {"pidfd_open": call(434, os.getppid(), 0), "kill": call(KILL, os.getppid(), 0)}
sandbox.confine(read=[package], write=[data])
params = ctypes.create_string_buffer(120)
after = {"io_uring_setup": call(425, 8, params), "pidfd_open": call(434, os.getppid(), 0),
         "pidfd_getfd": call(438, 0, 0, 0), "socket": call(41 if os.uname().machine == "x86_64" else 198, 2, 1, 0),
         "kill": call(KILL, os.getppid(), 0) if sandbox.landlock_abi() >= 6 else "EPERM"}
print(__import__("json").dumps({"before": before, "after": after}))
"""


@needs_sandbox
def test_a_confined_process_reaches_no_socket_by_io_uring_and_no_other_process_s_files(tmp_path):
    """2026-11-05 review: the filter refused socket(), but io_uring makes and connects sockets without it, and a module
    runs as the arcade's own user, so pidfd_getfd could take a copy of the web process's sockets. All refused now."""
    (tmp_path / "data").mkdir()
    package = _module(tmp_path)
    root = str(Path(__file__).parent.parent)
    out = subprocess.run([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {root!r})\n" + NO_RING,
                          str(package), str(tmp_path / "data")], capture_output=True, text=True, timeout=60, check=True).stdout
    said = json.loads(out)
    assert said["before"] == {"pidfd_open": "ok", "kill": "ok"}, "outside the sandbox these work (so the refusals are its)"
    # kill: Landlock's signal scope (ABI 6): the module cannot signal the arcade or a coin daemon, though the same user
    assert said["after"] == {"io_uring_setup": "EPERM", "pidfd_open": "EPERM", "pidfd_getfd": "EPERM", "socket": "EPERM",
                             "kill": "EPERM"}

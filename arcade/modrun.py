"""A node module's own process (arcade/modhost.py starts it): confine it, load the package's module.py, and answer the
arcade over stdin and stdout, one JSON object a line.

    from the arcade   {"id": 1, "call": "command", "name": "ddstats", "args": [], "caller": {...}}
                      {"id": 2, "call": "page", "method": "GET", "path": "", "query": {}, "body": "", "caller": {...}}
    from the module   {"id": 1, "rpc": ["dgb", "getblockcount", []]}     a chain read; the arcade answers
                      {"id": 1, "rpc_result": 812} or {"id": 1, "rpc_error": "..."}
                      {"id": 1, "result": ...} or {"id": 1, "error": "..."}

module.py defines `command_<name>(ctx, *args)` for each command its manifest names, and `page(ctx, method, path,
query, body)` if it has pages. `ctx.rpc(chain, method, *params)` reads a chain through the node, `ctx.data` is its own
folder, and `ctx.caller` who is asking: {audience, account, address: {chain: address}, pubkey: {chain: hex}}.

The sandbox goes on before module.py is read, so nothing in the package runs unconfined."""
from __future__ import annotations

import importlib.util
import json
import sys
import traceback
from pathlib import Path


class ModuleError(Exception):
    """Said back to the person who ran the command, as it is (so is a ValueError a module raises)."""


class Context:
    def __init__(self, data: Path, caller: dict, ask):
        self.data, self.caller, self._ask = data, caller, ask

    def rpc(self, chain: str, method: str, *params):
        return self._ask(chain, method, list(params))


def main(argv: list[str]) -> int:
    from . import sandbox
    package, data = Path(argv[0]), Path(argv[1])
    out, inp = sys.stdout, sys.stdin
    sys.stdout = sys.stderr                                # a module's print() goes to its log, not the arcade
    try:
        sandbox.confine(read=[str(package)], write=[str(data)])
    except sandbox.SandboxError as exc:
        out.write(json.dumps({"id": 0, "error": f"not confined: {exc}"}) + "\n")
        out.flush()
        return 2
    sys.path.insert(0, str(package))
    spec = importlib.util.spec_from_file_location("module", package / "module.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["module"] = mod
    spec.loader.exec_module(mod)
    out.write(json.dumps({"id": 0, "result": "ready"}) + "\n")
    out.flush()
    for line in inp:
        said = json.loads(line)
        rid = said["id"]

        def ask(chain, method, params, rid=rid):
            out.write(json.dumps({"id": rid, "rpc": [chain, method, params]}) + "\n")
            out.flush()
            back = json.loads(inp.readline())
            if "rpc_error" in back:
                raise ModuleError(back["rpc_error"])
            return back["rpc_result"]

        ctx = Context(data, said.get("caller") or {}, ask)
        try:
            if said["call"] == "command":
                fn = getattr(mod, f"command_{said['name']}", None)
                if fn is None:
                    raise ModuleError(f"this module has no command {said['name']}")
                answer = {"id": rid, "result": fn(ctx, *said.get("args", []))}
            else:
                answer = {"id": rid, "result": mod.page(ctx, said["method"], said["path"], said.get("query") or {},
                                                        said.get("body") or "")}
        except (ModuleError, ValueError) as exc:
            answer = {"id": rid, "error": str(exc)}
        except Exception as exc:                           # noqa: BLE001 -- the module's own fault, said briefly
            traceback.print_exc(file=sys.stderr)
            answer = {"id": rid, "error": f"{type(exc).__name__}: {exc}"}
        out.write(json.dumps(answer) + "\n")
        out.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

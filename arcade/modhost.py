"""Node modules installed from packages (arcade/packages.py), run by the arcade in processes of their own.

Each module that is installed and switched on runs `arcade/modrun.py` in its own process, started at its first use.
Before that process reads a line of the module's code it is confined (arcade/sandbox.py): it reads its package and
Python, writes its own data folder, and has no network. It never holds a key. What it may do goes through here:
- **chain reads:** only the reads its manifest names, on the chains it names, asked of this arcade's own Cores;
- **commands and pages:** the arcade asks, with who is asking; the module answers;
- **spending:** a module never spends. It hands back a prepared transaction, `{"prepared": {...}}`. `prepare` checks
  that every coin it spends is the caller's own (read from the chain, not taken from the module) and that it leaves at
  most the manifest's `max_fee` to fees, then makes it an unsigned offer that the caller's own wallet reviews and signs,
  as every account send is.

Linux only: a machine that cannot confine a module runs none, and `why_not` says why."""
from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import funding
from . import packages as packagelib
from . import sandbox
from .txbuild import build_raw_tx

#: Data folders, one per module name, so a new version keeps what the old one wrote.
DATA = "module-data"
#: How long a module may take over one command or page, chain reads included.
TIMEOUT = 30.0
MOST_LINE = 4_000_000
_BOOT = ("import sys; sys.path.insert(0, {root!r}); from arcade.modrun import main; "
         "raise SystemExit(main(sys.argv[1:]))")


class ModuleFailed(RuntimeError):
    """A module refused, failed or took too long, said for the person who asked."""


class _Process:
    def __init__(self, name: str, folder: Path, data: Path, log: Path):
        root = str(Path(__file__).resolve().parent.parent)
        data.mkdir(parents=True, exist_ok=True)
        self.name, self.buffer, self.lock = name, b"", threading.Lock()
        with open(log, "ab") as err:                      # the child keeps its own copy
            self.proc = subprocess.Popen(
                [sys.executable, "-I", "-c", _BOOT.format(root=root), str(folder), str(data)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, cwd=str(data),
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, start_new_session=True)
        ready = self.read(time.monotonic() + TIMEOUT)
        if ready.get("result") != "ready":
            self.stop()
            raise ModuleFailed(f"{name} did not start: {ready.get('error') or 'see its log'}")

    def alive(self) -> bool:
        return self.proc.poll() is None

    def write(self, said: dict) -> None:
        self.proc.stdin.write(json.dumps(said).encode() + b"\n")
        self.proc.stdin.flush()

    def read(self, deadline: float) -> dict:
        fd = self.proc.stdout.fileno()
        while b"\n" not in self.buffer:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([fd], [], [], left)[0]:
                self.stop()
                raise ModuleFailed(f"{self.name} took longer than {TIMEOUT:.0f} seconds and was stopped")
            chunk = os.read(fd, 65536)
            if not chunk or len(self.buffer) > MOST_LINE:
                self.stop()
                raise ModuleFailed(f"{self.name} stopped (its log says why)")
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(5)
        for pipe in (self.proc.stdin, self.proc.stdout):
            pipe.close()


class Host:
    """The node modules of one arcade. `chain_for(name)` is (that chain's Params, call(method, params)) for a chain
    this arcade has, and raises ValueError for one it has not."""

    def __init__(self, home: Path, chain_for: Callable[[str], tuple[Any, Callable]]):
        self.home, self.chain_for = Path(home), chain_for
        self.running: dict[str, tuple[str, _Process]] = {}
        self.lock = threading.Lock()

    @staticmethod
    def why_not() -> str:
        return sandbox.available()

    def installed(self, settings: dict) -> dict[str, tuple[str, dict]]:
        """{name: (package id, manifest)} for every node module installed here."""
        out = {}
        for pid, row in (settings.get(packagelib.SETTING) or {}).items():
            if row.get("kind") == "module":
                try:
                    out[row["name"]] = (pid, packagelib.manifest_of(self.home, pid))
                except (OSError, ValueError, packagelib.PackageError):
                    continue
        return out

    def _process(self, name: str, pid: str) -> _Process:
        with self.lock:
            have = self.running.get(name)
            if have and have[0] == pid and have[1].alive():
                return have[1]
            if have:
                have[1].stop()
            why = self.why_not()
            if why:
                raise ModuleFailed(f"node modules cannot run here: {why}")
            folder = packagelib.home_of(self.home, pid)
            files = packagelib.read(folder)[1]
            if packagelib.package_id(files) != pid:
                raise ModuleFailed(f"the module kept as {pid[:12]} was changed on disk; install it again")
            data = self.home / DATA / name
            proc = _Process(name, folder, data, self.home / DATA / f"{name}.log")
            self.running[name] = (pid, proc)
            return proc

    def stop(self, name: str | None = None) -> None:
        with self.lock:
            for which in ([name] if name else list(self.running)):
                have = self.running.pop(which, None)
                if have:
                    have[1].stop()

    def ask(self, settings: dict, name: str, request: dict, caller: dict) -> Any:
        """Run one command or page call in module `name`, answering its chain reads, and return its result."""
        pid, manifest = self.installed(settings).get(name, (None, None))
        if pid is None:
            raise ModuleFailed(f"no node module {name} is installed")
        reads = manifest.get("needs", {}).get("chains", {})
        proc = self._process(name, pid)
        with proc.lock:
            deadline = time.monotonic() + TIMEOUT
            proc.write({"id": 1, **request, "caller": caller})
            while True:
                said = proc.read(deadline)
                if "rpc" not in said:
                    break
                chain, method, params = (said["rpc"] + [None, None, None])[:3]
                if method not in reads.get(chain, ()):
                    proc.write({"id": 1, "rpc_error": f"{name} may not ask {chain} for {method}: its manifest "
                                                      "does not name it"})
                    continue
                try:
                    proc.write({"id": 1, "rpc_result": self.chain_for(chain)[1](method, list(params or []))})
                except Exception as exc:                   # noqa: BLE001 -- the Core's refusal, for the module
                    proc.write({"id": 1, "rpc_error": str(exc)})
        if "error" in said:
            raise ModuleFailed(str(said["error"]))
        return said.get("result")

    def prepare(self, settings: dict, name: str, prepared: dict, address: str) -> tuple[str, funding.Unsigned]:
        """(chain, an unsigned offer) from a module's prepared transaction, for the caller whose address on that chain
        is `address`. Refused unless every input is a coin of that address, read from the chain itself, and the fee
        is within the manifest's `max_fee`. The wallet reviews the outputs before it signs."""
        _, manifest = self.installed(settings)[name]
        chain = str(prepared.get("chain") or "")
        if chain not in manifest.get("needs", {}).get("chains", {}):
            raise ModuleFailed(f"{name} prepared a transaction on {chain or 'no chain'}, which it does not name")
        if not address:
            raise ModuleFailed(f"you have no address on {chain} here")
        params, call = self.chain_for(chain)
        spend = funding.spend_for(address, params)
        inputs, total = [], 0
        for coin in prepared.get("inputs") or []:
            txid, vout = str(coin.get("txid") or ""), int(coin.get("vout", -1))
            out = call("gettxout", [txid, vout, True])
            if not out or out["scriptPubKey"]["hex"] != spend["change_script"].hex():
                raise ModuleFailed(f"{name} asked to spend {txid[:16]}:{vout}, which is not an unspent coin of yours")
            value = round(float(out["value"]) * 100_000_000)
            total += value
            inputs.append({"txid": txid, "vout": vout, "value": value, "address": address,
                           **({"segwit": spend["kind"]} if spend["kind"] != "p2pkh" else {})})
        outputs = [(int(v), bytes.fromhex(str(s))) for v, s in prepared.get("outputs") or []]
        if not inputs or not outputs or any(v < 0 for v, _ in outputs):
            raise ModuleFailed(f"{name} prepared a transaction with nothing to spend or nothing to pay")
        fee = total - sum(v for v, _ in outputs)
        if not 0 <= fee <= manifest.get("needs", {}).get("max_fee", packagelib.MOST_FEE):
            raise ModuleFailed(f"{name} prepared a transaction that leaves {fee} to fees, more than it may")
        version = int(prepared.get("version", 1))
        if not 1 <= version < 2 ** 31:
            raise ModuleFailed(f"{name} prepared a transaction of version {version}")
        if spend["kind"] == "p2pkh":
            hashes = [funding.sighash(inputs, outputs, n, spend["script_code"], version=version).hex()
                      for n in range(len(inputs))]
        else:
            hashes = [funding.sighash_witness(inputs, outputs, n, spend["script_code"], c["value"],
                                              version=version).hex() for n, c in enumerate(inputs)]
        what = f"{manifest.get('title') or name}: {str(prepared.get('what') or 'a transaction')[:200]}"
        raw = build_raw_tx([(c["txid"], c["vout"]) for c in inputs], outputs, version=version)
        return chain, funding.Unsigned(raw, inputs, outputs, hashes, fee=fee, what=what, version=version)

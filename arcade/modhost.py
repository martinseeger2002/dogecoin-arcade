"""Node modules installed from packages (arcade/packages.py), run by the arcade in processes of their own.

Each module that is installed and switched on runs `arcade/modrun.py` in its own process, started at its first use.
Before that process reads a line of the module's code it is confined (arcade/sandbox.py): it reads its package and
Python, writes its own data folder, and has no network. It never holds a key. What it may do goes through here:
- **chain reads:** only the reads its manifest names, on the chains it names, asked of this arcade's own Cores;
- **commands and pages:** the arcade asks, with who is asking; the module answers;
- **spending:** a module never spends. It hands back a prepared transaction, `{"prepared": {...}}`. `prepare` checks
  that every coin it spends is the caller's own (read from the chain, not taken from the module) and that it leaves at
  most the manifest's `max_fee` to fees, then makes it an unsigned offer that the caller's own wallet reviews and signs,
  as every account send is;
- **inscriptions stay put:** on a chain with an inscription guard (funding.guard_on: the chains with an ord index), a
  module's coin reads (`scantxoutset`) leave out coins carrying an inscription, and `prepare` refuses to spend one, as an
  ordinary send never does (2026-11-05).

Linux only: a machine that cannot confine a module runs none, and `why_not` says why."""
from __future__ import annotations

import contextlib
import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import funding
from . import packages as packagelib
from . import sandbox
from . import taproot

#: Data folders, one per module name, so a new version keeps what the old one wrote.
DATA = "module-data"
#: How long a module may take over one command or page, chain reads included.
TIMEOUT = 30.0
MOST_LINE = 4_000_000
#: A busy module: how long a request waits for it, and how many may wait at once, before "busy" (2026-11-05 review).
BUSY_WAIT = 5.0
MOST_WAITING = 4
#: The coins a module's prepared transaction may spend at most (one chain read each).
MOST_INPUTS = 50
_BOOT = ("import sys; sys.path.insert(0, {root!r}); from arcade.modrun import main; "
         "raise SystemExit(main(sys.argv[1:]))")


class ModuleFailed(RuntimeError):
    """A module refused, failed or took too long, said for the person who asked."""


class _Process:
    def __init__(self, name: str, folder: Path, data: Path, log: Path):
        root = str(Path(__file__).resolve().parent.parent)
        data.mkdir(parents=True, exist_ok=True)
        self.name, self.buffer, self.lock, self.waiting = name, b"", threading.Lock(), 0
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
        try:
            self.proc.stdin.write(json.dumps(said).encode() + b"\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:  # a module that died: said as such, not a 500
            self.stop()
            raise ModuleFailed(f"{self.name} stopped (its log says why)") from exc

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
        # The whole group (2026-11-05 review): a module started in a session of its own, so whatever it forked is in
        # the group, and is stopped with it rather than left running after a stop or removemodule.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.proc.pid, signal.SIGKILL)
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(5)
        for pipe in (self.proc.stdin, self.proc.stdout):
            pipe.close()


def _plain(unspent: dict) -> dict:
    return {"txid": str(unspent.get("txid")), "vout": int(unspent.get("vout", -1)),
            "value": round(float(unspent.get("amount", unspent.get("value", 0)) or 0) * 100_000_000)
            if "amount" in unspent else int(unspent.get("value", 0))}


def without_inscriptions(chain: str, method: str, said):
    """A module's chain read, with what an ordinary send would not spend left out (SadFrog, 2026-11-05: "make module
    transactions inscription-aware"). On a chain with an inscription guard, `scantxoutset`'s coins are put through it:
    a coin carrying an inscription, or a small one the guard cannot read yet, is not listed, so a module building a
    spend never picks it. A coin of no value (a DigiDollar's Taproot output) is not ordinals' and stays. Where the
    guard reads DigiAssets too (DigiByte, 2026-11-09, security review), the read says so, `"digiassets": "read"`: each
    coin listed was read for them, so a module need not keep small coins back on their account (DigiDollar's Gather)."""
    guard = funding.guard_on(chain)
    if guard is None or method != "scantxoutset" or not isinstance(said, dict) \
            or not isinstance(said.get("unspents"), list):
        return said
    valued = [_plain(u) for u in said["unspents"] if float(u.get("amount") or 0) > 0]
    ok = {(c["txid"], c["vout"]) for c in guard.spendable(valued)}
    kept = [u for u in said["unspents"] if not float(u.get("amount") or 0) > 0
            or (str(u.get("txid")), int(u.get("vout", -1))) in ok]
    return {**said, "unspents": kept, "total_amount": round(sum(float(u.get("amount") or 0) for u in kept), 8),
            **({"digiassets": "read"} if getattr(guard, "digiassets", False) else {})}


def _refuse_inscribed(name: str, chain: str, inputs: list[dict]) -> None:
    """Refuse a prepared spend of a coin carrying an inscription on a chain with a guard, whatever the module read:
    the guard's own answer, as `funding.choose` asks it. Taproot inputs (a vault, a DigiDollar) are the module's own
    kind and are checked by their script instead (`_taproot`)."""
    guard = funding.guard_on(chain)
    plain = [c for c in inputs if not c.get("taproot")]
    if guard is None or not plain:
        return
    try:
        ok = {(c["txid"], c["vout"]) for c in guard.spendable([_plain(c) for c in plain])}
    except Exception as exc:                               # noqa: BLE001 -- unread is not clean
        raise ModuleFailed(f"{name}'s spend could not be checked for inscriptions: {exc}") from None
    for c in plain:
        if (c["txid"], c["vout"]) not in ok:
            raise ModuleFailed(f"{name} asked to spend {c['txid'][:16]}:{c['vout']}, which carries an inscription or "
                               f"can't be read for one yet: it stays where it is")


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
        # One request at a time per module, as before, but nobody waits long for a busy one (2026-11-05 review: each
        # waiter held one of the web server's threads for up to TIMEOUT, so one slow module could stall the arcade).
        with self.lock:
            if proc.waiting >= MOST_WAITING:
                raise ModuleFailed(f"{name} is busy; try again in a moment")
            proc.waiting += 1
        try:
            got = proc.lock.acquire(timeout=BUSY_WAIT)
        finally:
            with self.lock:
                proc.waiting -= 1
        if not got:
            raise ModuleFailed(f"{name} is busy; try again in a moment")
        try:
            return self._ask_locked(proc, name, reads, request, caller)
        finally:
            proc.lock.release()

    def _ask_locked(self, proc: "_Process", name: str, reads: dict, request: dict, caller: dict) -> Any:
        deadline = time.monotonic() + TIMEOUT
        proc.write({"id": 1, **request, "caller": caller})
        while True:
            said = proc.read(deadline)
            if "rpc" not in said:
                break
            if not isinstance(said["rpc"], list):
                proc.write({"id": 1, "rpc_error": "a chain read is [chain, method, params]"})
                continue
            chain, method, params = (said["rpc"] + [None, None, None])[:3]
            if not isinstance(params, (list, type(None))):
                proc.write({"id": 1, "rpc_error": "a chain read's params are a list"})
                continue
            if method not in reads.get(chain, ()):
                proc.write({"id": 1, "rpc_error": f"{name} may not ask {chain} for {method}: its manifest "
                                                  "does not name it"})
                continue
            try:
                said_back = self.chain_for(chain)[1](method, list(params or []))
                proc.write({"id": 1, "rpc_result": without_inscriptions(chain, method, said_back)})
            except Exception as exc:                   # noqa: BLE001 -- the Core's refusal, for the module
                proc.write({"id": 1, "rpc_error": str(exc)})
        if "error" in said:
            raise ModuleFailed(str(said["error"]))
        return said.get("result")

    def prepare(self, settings: dict, name: str, prepared: dict, address: str,
                pubkey: bytes | None = None) -> tuple[str, funding.Unsigned]:
        """(chain, an unsigned offer) from a module's prepared transaction, for the caller whose address on that chain
        is `address` and whose key is `pubkey`. Refused unless every input is the caller's, read from the chain
        itself, and the fee is within the manifest's `max_fee`. The wallet reviews the outputs before it signs.

        An input is the caller's when its coin pays the caller's address, or it is a Taproot coin of the caller's key
        (arcade/taproot.py): the key path of that key, or a script path whose leaf asks that key alone to sign. A
        Taproot input may wait for the lock time (`sequence`), and the transaction may carry one (`locktime`)."""
        _, manifest = self.installed(settings)[name]
        chain = str(prepared.get("chain") or "")
        if chain not in manifest.get("needs", {}).get("chains", {}):
            raise ModuleFailed(f"{name} prepared a transaction on {chain or 'no chain'}, which it does not name")
        if not address:
            raise ModuleFailed(f"you have no address on {chain} here")
        params, call = self.chain_for(chain)
        return chain, build_offer(name, prepared, params, call, address, pubkey,
                                  max_fee=manifest.get("needs", {}).get("max_fee", packagelib.MOST_FEE),
                                  title=manifest.get("title") or name)

    @staticmethod
    def _taproot(name: str, tap: dict, script: bytes, pubkey: bytes | None, txid: str, vout: int) -> dict:
        """The Taproot spend a module asked for, if the coin is the caller's to sign that way (arcade/taproot.py)."""
        if not pubkey:
            raise ModuleFailed("open your wallet in this arcade's window first: a Taproot coin is signed by your key")
        theirs = ModuleFailed(f"{name} asked to spend {txid[:16]}:{vout}, which is not a Taproot coin of your key")
        if not isinstance(tap, dict):
            raise theirs
        if tap.get("leaf"):
            leaf, control = bytes.fromhex(str(tap["leaf"])), bytes.fromhex(str(tap.get("control") or ""))
            if not taproot.leaf_mine(script, leaf, control, pubkey):
                raise theirs
            return {"leaf": leaf.hex(), "control": control.hex()}
        if not taproot.key_path_mine(script, pubkey):
            raise theirs
        return {"key": True}


def build_offer(name: str, prepared: dict, params, call: Callable, address: str, pubkey: bytes | None, *,
                max_fee: int, title: str) -> funding.Unsigned:
    """An unsigned offer from a prepared transaction -- a node module's (Host.prepare), or the arcade's own DigiDollar
    send (arcade/digidollar.py) -- for the caller whose address on the chain is `address` and whose key is `pubkey`:
    every input read from the chain (`call`: gettxout) and refused unless it is the caller's, the fee at most `max_fee`.
    `name` is who prepared it, in a refusal's words; `title` heads the offer's description."""
    spend = funding.spend_for(address, params)
    inputs, scripts, total = [], [], 0
    asked = list(prepared.get("inputs") or [])
    if len(asked) > MOST_INPUTS:
        raise ModuleFailed(f"{name} asked to spend {len(asked)} coins, more than {MOST_INPUTS}")
    seen = set()
    for coin in asked:
        txid, vout = str(coin.get("txid") or ""), int(coin.get("vout", -1))
        if (txid, vout) in seen:
            raise ModuleFailed(f"{name} asked to spend {txid[:16]}:{vout} twice")
        seen.add((txid, vout))
        out = call("gettxout", [txid, vout, True])
        script = bytes.fromhex(out["scriptPubKey"]["hex"]) if out else b""
        value = round(float(out["value"]) * 100_000_000) if out else 0
        sequence = int(coin.get("sequence", 0xFFFFFFFF))
        tap = coin.get("taproot")
        if tap is not None:
            tap = Host._taproot(name, tap, script, pubkey, txid, vout)
        elif not out or script != spend["change_script"]:
            raise ModuleFailed(f"{name} asked to spend {txid[:16]}:{vout}, which is not an unspent coin of yours")
        if not 0 <= sequence <= 0xFFFFFFFF or (sequence != 0xFFFFFFFF and tap is None):
            raise ModuleFailed(f"{name} asked for sequence {sequence} on an input that does not wait")
        total += value
        scripts.append(script)
        inputs.append({"txid": txid, "vout": vout, "value": value, "address": address,
                       **({"spent": script.hex()} if tap is not None else {}),
                       **({"sequence": sequence} if sequence != 0xFFFFFFFF else {}),
                       **({"taproot": tap} if tap is not None else
                          {"segwit": spend["kind"]} if spend["kind"] != "p2pkh" else {})})
    outputs = [(int(v), bytes.fromhex(str(s))) for v, s in prepared.get("outputs") or []]
    if not inputs or not outputs or any(v < 0 for v, _ in outputs):
        raise ModuleFailed(f"{name} prepared a transaction with nothing to spend or nothing to pay")
    fee = total - sum(v for v, _ in outputs)
    if not 0 <= fee <= max_fee:
        raise ModuleFailed(f"{name} prepared a transaction that leaves {fee} to fees, more than it may")
    version, locktime = int(prepared.get("version", 1)), int(prepared.get("locktime", 0))
    if not 1 <= version < 2 ** 31:
        raise ModuleFailed(f"{name} prepared a transaction of version {version}")
    if not 0 <= locktime < 500_000_000:
        raise ModuleFailed(f"{name} prepared a lock time of {locktime}, which is not a block height")
    _refuse_inscribed(name, getattr(params, "name", ""), inputs)
    tapped = any(c.get("taproot") for c in inputs)
    if tapped and spend["kind"] != "p2pkh":
        raise ModuleFailed("a Taproot spend here pays its fee from an ordinary (P2PKH) address only")
    hashes = []
    for n, c in enumerate(inputs):
        if c.get("taproot"):
            rows = [{**i, "spent": spent} for i, spent in zip(inputs, scripts)]
            leaf = bytes.fromhex(c["taproot"]["leaf"]) if c["taproot"].get("leaf") else None
            hashes.append(taproot.sighash(rows, outputs, n, version=version, locktime=locktime, leaf=leaf).hex())
        elif spend["kind"] == "p2pkh":
            hashes.append(funding.sighash(inputs, outputs, n, spend["script_code"], version=version,
                                          locktime=locktime).hex())
        else:
            hashes.append(funding.sighash_witness(inputs, outputs, n, spend["script_code"], c["value"],
                                                  version=version, locktime=locktime).hex())
    what = f"{title}: {str(prepared.get('what') or 'a transaction')[:200]}"
    # Unsigned, so with no witness: the plain serialisation, each input's sequence in it.
    raw = funding.serialize_segwit(inputs, outputs, locktime=locktime, version=version)
    return funding.Unsigned(raw, inputs, outputs, hashes, fee=fee, what=what, version=version,
                           locktime=locktime)

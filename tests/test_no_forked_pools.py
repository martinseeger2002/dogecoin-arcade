"""No process pool in the arcade is forked (2026-11-04, app.dogecoinarcade.com down 16:21-16:32): a forked child
inherits every file its worker has open, web.lock among them (web/supervisor.py), and children left waiting when the
worker was reloaded held that run lock, so the next worker never started. Every pool is made from a "spawn" context."""
import pathlib
import re

ARCADE = pathlib.Path(__file__).resolve().parent.parent / "arcade"
POOL = re.compile(r"(ProcessPoolExecutor\(|\.Pool\(|multiprocessing\.Process\(|mp\.Process\()")


def test_every_process_pool_is_spawned_never_forked():
    bad = []
    for path in ARCADE.rglob("*.py"):
        if path.name == "regtest.py":
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if POOL.search(line) and not line.lstrip().startswith("#") and "spawn" not in line:
                bad.append(f"{path.relative_to(ARCADE.parent)}:{n}: {line.strip()}")
    assert not bad, "a process pool that may fork (give it multiprocessing.get_context('spawn')):\n" + "\n".join(bad)


def test_the_block_scan_pool_is_spawned(monkeypatch):
    import concurrent.futures as cf
    from arcade import blkscan
    seen = {}

    class Pool:
        def __init__(self, workers, mp_context=None):
            seen["method"] = mp_context.get_start_method() if mp_context else "fork (default)"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def map(self, fn, args):
            return [[] for _ in args]
    monkeypatch.setattr(cf, "ProcessPoolExecutor", Pool)

    class Files:
        dir, auxpow = "/nonexistent", False
    assert blkscan.scan_parallel(Files(), "/nonexistent/cache", 100, 1, ["x"], None, workers=2) == []
    assert seen["method"] == "spawn"

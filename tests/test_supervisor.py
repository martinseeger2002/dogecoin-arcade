"""Updates with no downtime (arcade/web/supervisor.py, 2026-10-20)."""
import os
import subprocess
import sys
import textwrap

from arcade.web import supervisor as sv


def test_only_one_worker_of_a_home_holds_the_run_lock(tmp_path):
    first = sv.RunLock(tmp_path)
    assert first.take(wait=False)
    code = textwrap.dedent(f"""
        from arcade.web import supervisor as sv
        print(sv.RunLock({str(tmp_path)!r}).take(wait=False))
    """)
    other = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    first.close()
    assert other.stdout.strip() == "False", "a second worker waits while the first one serves"


def test_the_supervisor_reads_where_without_loading_the_app(tmp_path):
    assert sv.where(["--port", "8431", "--home", str(tmp_path), "--no-mesh"]) == ("127.0.0.1", 8431, tmp_path)
    host, port, home = sv.where([])
    assert (host, port) == ("127.0.0.1", 8420) and home.name == ".dogecoinarcade"
    code = "import sys, arcade.web.supervisor; print('arcade.web.app' in sys.modules)"
    loaded = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert loaded.stdout.strip() == "False", "the supervisor stays a small process"


def test_a_reload_with_no_supervisor_running_says_so(tmp_path):
    assert sv.reload(tmp_path) is False
    (tmp_path / sv.PIDFILE).write_text("999999999")
    assert sv.running(tmp_path) is None, "a pid that is gone is no supervisor"
    (tmp_path / sv.PIDFILE).write_text(str(os.getpid()))
    assert sv.running(tmp_path) == os.getpid()


def test_a_worker_says_warm_on_its_pipe():
    r, w = os.pipe()
    sv.worker_ready(w, b"warm")
    sv.worker_ready(None, b"warm")                         # run by hand: nothing to tell
    assert os.read(r, 16) == b"warm\n"

"""The Core's own word on a rescan, read from its debug.log (arcade/core_log.py; 2026-10-22: "include rescan %")."""
import calendar
import time

from arcade import core_log
from arcade.config import NETWORKS


def _line(at, block, progress):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(at))
    return f"{stamp} Still rescanning. At block {block}. Progress={progress:.6f}\n"


def test_the_last_line_says_how_far_and_its_pace_says_how_long(tmp_path):
    now = calendar.timegm((2026, 10, 22, 14, 16, 0, 0, 0, 0))
    log = tmp_path / "debug.log"
    log.write_text("2026-10-22 14:00:00 UpdateTip: new best=...\n"
                   + _line(now - 156, 5460761, 0.588905) + "2026-10-22 14:14:00 socket recv error\n"
                   + _line(now - 96, 5466506, 0.593682) + _line(now - 36, 5471263, 0.597658))
    said = core_log.read_progress(log, now)
    assert said["block"] == 5471263 and abs(said["progress"] - 0.597658) < 1e-9
    rate = (0.597658 - 0.588905) / 120
    assert abs(said["eta"] - ((1 - 0.597658) / rate - 36)) < 2


def test_no_fresh_line_and_no_log_to_read_say_nothing(tmp_path):
    now = time.time()
    log = tmp_path / "debug.log"
    log.write_text(_line(now - 600, 100, 0.5))
    assert core_log.read_progress(log, now) is None, "a rescan that ended ten minutes ago"
    log.write_text(_line(now - 30, 100, 0.5))
    said = core_log.read_progress(log, now)
    assert said["progress"] == 0.5 and said["eta"] is None, "one line: no pace yet"
    assert core_log.read_progress(tmp_path / "missing.log", now) is None
    log.chmod(0)
    try:
        assert core_log.read_progress(log, now) is None or __import__("os").geteuid() == 0
    finally:
        log.chmod(0o600)


def test_the_log_is_looked_for_where_the_core_keeps_it(tmp_path, monkeypatch):
    monkeypatch.setattr(core_log.Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".dogecoin").mkdir()
    (tmp_path / ".dogecoin" / "dogecoin.conf").write_text(f"datadir={tmp_path / 'chain'}\n")
    paths = core_log.log_paths(NETWORKS["doge"])
    assert paths == [tmp_path / "chain" / "debug.log", tmp_path / ".dogecoin" / "debug.log",
                     core_log.Path("/var/lib/dogecoind/debug.log")]
    assert core_log.log_paths(NETWORKS["test"])[0] == tmp_path / ".pepecoin" / "testnet3" / "debug.log"

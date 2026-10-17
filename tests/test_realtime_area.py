"""The page half's helpers for busy places (templates/realtime.js): join(..., {cap}) and area(...).
Runs tests/js/realtime_area.js in node against a stand-in viewer; skipped where node is missing."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_room_caps_and_area_cells_in_the_page_half():
    script = Path(__file__).parent / "js" / "realtime_area.js"
    out = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and "all ok" in out.stdout, out.stdout + out.stderr

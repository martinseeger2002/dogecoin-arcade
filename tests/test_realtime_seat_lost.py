"""A player whose seat the node lost must take it again on every send path (2026-10-20).

The page hears the room through its stream, so a lost seat only shows up when it speaks: the node answers
"join the room first" and the mesh service "not in that room any more -- join again". The page matched only
the second, so a player could see everyone while nobody saw them.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1] / "arcade"
DOORS = (ROOT / "web" / "templates" / "_page_doors.js.html").read_text()


def _raised(path, needle):
    text = (ROOT / path).read_text()
    found = re.findall(r'MeshError\("([^"]*%s[^"]*)"\)' % re.escape(needle), text)
    assert found, f"{path} no longer says {needle!r}: keep the page's seatLost in step"
    return found[0]


def test_the_page_rejoins_on_both_lost_seat_answers():
    pattern = re.search(r"function seatLost\(j\)\{.*?/(.+?)/\.test", DOORS).group(1)
    for said in (_raised("mesh/node.py", "join the room first"), _raised("mesh/service.py", "join again")):
        assert re.search(pattern, said), f"seatLost misses {said!r}"


def test_every_send_path_checks_for_a_lost_seat():
    sends = DOORS.count("realtimePost('/realtime/send'")
    assert sends == 3 and DOORS.count("if (seatLost(j)) realtimeRejoin(") == sends

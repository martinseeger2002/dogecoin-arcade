"""The echo canceller on a room in numbers (tests/echo_canceller_sim.js): 2026-11-02, a shared video's sound coming back through the microphone."""
import json, pathlib, shutil, subprocess

import pytest


@pytest.fixture(scope="module")
def room():
    node = shutil.which("node")
    if not node:
        pytest.skip("no node")
    return json.loads(subprocess.run([node, str(pathlib.Path(__file__).with_name("echo_canceller_sim.js"))], capture_output=True, text=True, timeout=120, check=True).stdout)


def test_the_echo_drops_at_every_delay_a_computer_gives(room):
    for ms in (-30, 0, 20, 60, 150, 300):
        assert room[f"drop_{ms}ms"] > 25, ms


def test_it_learns_in_seconds_and_keeps_up_in_real_time(room):
    assert room["after_2s"] > 15 and room["after_5s"] > 25 and room["realtime_share"] < .3


def test_a_voice_over_the_echo_stays_and_the_echo_goes(room):
    assert room["voice_kept"] > .9 and room["voice_to_echo_out"] > room["voice_to_echo_in"] + 3


def test_headphones_and_silence_leave_the_voice_alone(room):
    assert room["headphones_voice_kept"] > .97 and room["quiet_difference_db"] < -60


def test_a_moved_microphone_is_learned_again(room):
    assert room["moved_after_3s"] > 10


def test_a_voice_there_from_the_start_is_never_what_gets_taken_out(room):
    assert room["talking_from_start_kept"] > .95 and room["talking_from_start_drop"] > -1

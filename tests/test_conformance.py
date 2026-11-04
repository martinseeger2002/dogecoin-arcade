"""The protocol must not drift (ARCADE Lite phase 0): today's code still gives every byte in vectors.json.

A failure here means a change alters what other arcades sign, check or parse. If that is meant, regenerate with
`python tests/conformance/make_vectors.py` in the same commit and say what changed, and why, in COLLAB-NOTES."""
import importlib.util
import json
from pathlib import Path

import pytest

HERE = Path(__file__).parent / "conformance"
_spec = importlib.util.spec_from_file_location("make_vectors", HERE / "make_vectors.py")
make_vectors = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(make_vectors)

RECORDED = json.loads((HERE / "vectors.json").read_text())
NOW = json.loads(json.dumps(make_vectors.compute(), sort_keys=True))


def _differences(a, b, path=""):
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            yield from _differences(a.get(key), b.get(key), f"{path}.{key}")
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b)):
            yield from _differences(x, y, f"{path}[{i}]")
    elif a != b:
        yield f"{path}: recorded {a!r}, now {b!r}"


@pytest.mark.parametrize("section", sorted(RECORDED))
def test_the_protocol_still_gives_the_recorded_bytes(section):
    changed = list(_differences(RECORDED[section], NOW.get(section), section))
    assert not changed, "the protocol changed:\n" + "\n".join(changed[:20])


def test_every_section_is_recorded():
    assert sorted(NOW) == sorted(RECORDED), "a section was added or removed: regenerate vectors.json"

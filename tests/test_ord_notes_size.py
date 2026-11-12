"""A chunk's notes always fit the unpack cap (2026-11-07): 100 Litecoin blocks from the 2023 inscription rush came to
324 MB of notes, past the old 256 MB cap, and the walk dropped them and stood still at block 2,481,929."""
import zlib

import pytest

from arcade import doginal_index, ord_notes


def test_a_chunk_of_full_four_megabyte_blocks_fits_the_cap():
    assert ord_notes.MAX_UNPACKED >= doginal_index.CHUNK * 4_000_000 * 1.25


def test_notes_past_the_cap_are_still_refused():
    blob = zlib.compress(b"\0" * 2048)
    assert ord_notes.unpack(blob) == b"\0" * 2048
    with pytest.raises(ord_notes.NotesError, match="larger than any chunk"):
        ord_notes.unpack(blob, limit=1024)

"""Every instance serves the next one.

A network of nodes that all send people back to one website is one website
away from being no network at all. So the program and a copy of the index
come from whichever node somebody is looking at (D-153).
"""

import gzip
import hashlib
import json
import pathlib
import sqlite3
import sys
import tarfile

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

from arcade import bootstrap                                     # noqa: E402


def an_index(path):
    """A ledger index with something in it, as the engine would leave one."""
    from arcade.db import Database
    from arcade.state import install_schema

    db = Database(path)
    install_schema(db)
    db.conn.execute(
        "INSERT INTO block(height,hash,prev_hash,time,tx_count,processed_at) "
        "VALUES(?,?,?,?,?,?)", (900, "aa" * 32, "bb" * 32, 1, 0, 1))
    db.conn.execute(
        "INSERT INTO inscription(txid,number,creator,owner,block_height,"
        "position,content_type,content_len,sha256,json,chunks,content) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("cc" * 32, 1, "nMe", "nMe", 900, 0, "text/plain", 5, "dd" * 32,
         "{}", 1, b"hello"))
    db.conn.commit()
    db.close()
    return path


# --- the copy of the index ----------------------------------------------------

def test_a_live_index_can_be_copied_while_it_is_being_written(tmp_path):
    """`VACUUM INTO`, not a file copy: the indexer never stops, and a plain
    copy of a database mid-write is one that opens and then lies."""
    index = an_index(tmp_path / "main-ledger.sqlite")
    held = sqlite3.connect(index)          # a reader, as the web UI is
    try:
        snap = bootstrap.make(tmp_path, "main", index, floor=100)
    finally:
        held.close()
    assert snap.height == 900
    assert snap.path.exists() and snap.bytes > 0
    assert snap.sha256 == hashlib.sha256(snap.path.read_bytes()).hexdigest()

    # And what comes out is a database, not a truncated one.
    plain = tmp_path / "out.sqlite"
    plain.write_bytes(gzip.decompress(snap.path.read_bytes()))
    rows = sqlite3.connect(plain).execute(
        "SELECT COUNT(*) FROM inscription").fetchone()[0]
    assert rows == 1


def test_the_manifest_says_what_it_is(tmp_path):
    index = an_index(tmp_path / "main-ledger.sqlite")
    snap = bootstrap.make(tmp_path, "main", index, floor=100)
    said = json.loads(snap.manifest.read_text())
    assert said["height"] == 900 and said["floor"] == 100
    assert len(said["consensus_hash"]) == 64
    assert len(said["index_digest"]) == 64
    assert said["sha256"] == snap.sha256
    assert "convenience" in said["what"] or "prove it" in said["what"]


def test_the_digest_covers_what_the_consensus_hash_does_not(tmp_path):
    """`consensus_hash` mirrors omnicore's sections and so says nothing
    about inscriptions. A doctored bootstrap that moved one would otherwise
    pass every check there is."""
    from arcade.consensushash import consensus_hash
    from arcade.db import Database

    index = an_index(tmp_path / "main-ledger.sqlite")
    db = Database(index)
    before_consensus = consensus_hash(db)
    before_index = bootstrap.index_digest(db)
    db.conn.execute("UPDATE inscription SET owner = 'nThief'")
    db.conn.commit()
    after_consensus = consensus_hash(db)
    after_index = bootstrap.index_digest(db)
    db.close()

    assert after_consensus == before_consensus, (
        "the consensus hash does not see an inscription move -- which is "
        "exactly why the second digest exists")
    assert after_index != before_index, "and the second digest does"


def test_it_is_remade_when_it_goes_stale(tmp_path):
    index = an_index(tmp_path / "main-ledger.sqlite")
    snap = bootstrap.make(tmp_path, "main", index, floor=100, now=1000)
    assert not bootstrap.stale(snap, tip=900, now=1000)
    assert not bootstrap.stale(snap, tip=950, now=1000)
    assert bootstrap.stale(snap, tip=1100, now=1000), "a hundred blocks on"
    assert bootstrap.stale(snap, tip=900, now=1000 + 3601), "an hour on"
    assert bootstrap.stale(None, tip=900), "and there being none at all"


def test_nothing_private_is_ever_in_it(tmp_path):
    """The messaging store holds conversations, an address book somebody
    typed, and the identity key. A bootstrap carrying it would publish all
    of that."""
    index = an_index(tmp_path / "main-ledger.sqlite")
    (tmp_path / "test.sqlite").write_bytes(b"private messages live here")
    snap = bootstrap.make(tmp_path, "main", index, floor=100)
    assert b"private messages" not in gzip.decompress(snap.path.read_bytes())
    assert snap.path.name == "main.sqlite.gz", "one chain's index, named as such"


# --- the copy of the program --------------------------------------------------

def test_the_source_is_packed_without_this_machine_in_it(tmp_path):
    said = bootstrap.source_archive(tmp_path, pathlib.Path("."), "abc1234")
    archive = tmp_path / bootstrap.FOLDER / bootstrap.SOURCE_NAME
    with tarfile.open(archive) as tar:
        names = tar.getnames()
    assert "installer/install.py" in names
    assert any(n.startswith("arcade/web/docs/") for n in names), "the manual too"
    for name in names:
        parts = pathlib.Path(name).parts
        assert ".venv" not in parts, name
        assert "__pycache__" not in parts, name
        assert "bootstrap" not in parts, "a snapshot inside the source of it"
        assert not name.endswith(".pyc"), name
        assert not any(p.endswith(".egg-info") for p in parts), name


def test_two_nodes_at_one_revision_pack_the_same_bytes(tmp_path):
    """Sorted members and a fixed mtime, so somebody comparing two clones'
    archives does not see a difference that is only a timestamp."""
    one = bootstrap.source_archive(tmp_path / "a", pathlib.Path("."), "abc1234")
    two = bootstrap.source_archive(tmp_path / "b", pathlib.Path("."), "abc1234")
    assert one["sha256"] == two["sha256"]


def test_a_directory_that_is_not_a_checkout_is_refused(tmp_path):
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.source_archive(tmp_path, tmp_path, "abc1234")


# --- through the web ----------------------------------------------------------

def test_the_page_offers_the_program_and_the_index(client):
    app, state = client
    an_index(state.home / "main-ledger.sqlite")
    body = app.get("/clone").text
    assert "source.tar.gz" in body
    assert "/bootstrap/main.sqlite.gz" in body
    assert "convenience, not an authority" in body
    assert "not</em> in it" in body or "not in it" in body


def test_the_installer_s_own_three_names_are_served(client):
    """The installer already fetches these, so any node can be the place
    somebody installs from with no new option to point at one."""
    app, _ = client
    archive = app.get("/source.tar.gz")
    assert archive.status_code == 200
    assert archive.content[:2] == b"\x1f\x8b", "gzip"
    checksum = app.get("/source.tar.gz.sha256")
    assert checksum.status_code == 200
    assert hashlib.sha256(archive.content).hexdigest() in checksum.text
    assert app.get("/source.rev").status_code == 200


def test_the_bootstrap_and_its_manifest_are_served(client):
    app, state = client
    an_index(state.home / "main-ledger.sqlite")
    blob = app.get("/bootstrap/main.sqlite.gz")
    assert blob.status_code == 200
    assert blob.headers["x-arcade-height"] == "900"
    said = app.get("/bootstrap/main.json").json()
    assert said["height"] == 900
    assert said["sha256"] == hashlib.sha256(blob.content).hexdigest()


def test_a_chain_with_no_index_says_so_rather_than_erroring(client):
    app, _ = client
    assert app.get("/bootstrap/main.sqlite.gz").status_code == 404
    assert app.get("/bootstrap/nonsense.sqlite.gz").status_code == 404
    assert app.get("/bootstrap/../../etc/passwd").status_code in (404, 400)


def test_a_public_instance_serves_all_of_it(client):
    """This is the point: a clone can clone."""
    app, state = client
    an_index(state.home / "main-ledger.sqlite")
    state.public = True
    try:
        for path in ("/clone", "/source.tar.gz", "/source.tar.gz.sha256",
                     "/source.rev", "/bootstrap/main.sqlite.gz",
                     "/bootstrap/main.json"):
            assert app.get(path).status_code == 200, path
    finally:
        state.public = False


def test_the_page_says_what_the_checksum_cannot_prove(client):
    """A checksum people believe in is worse than none (docs/multi-user.md, "What the
    checksum can and cannot do"): the page must say both halves, not just print it."""
    app, _state = client
    page = app.get("/clone").text
    assert "What this number proves, and what it does not" in page
    assert "cannot</b> prove that this site is honest" in page
    assert "typed in yourself rather than followed from a link" in page


def test_an_operator_is_told_what_to_do_and_what_not_to(client):
    """Their own domain, never a numbered look-alike; public_hosts; the twelve-words rule."""
    app, _state = client
    page = app.get("/clone").text
    assert "domain of your own" in page and "numbered" in page
    assert "public_hosts" in page
    assert "typed once" in page and "is stealing them" in page

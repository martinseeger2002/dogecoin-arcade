"""Collections in the index, on the pages, and through the API."""

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_collections import hashlips                             # noqa: E402


def index_with_a_collection(home, count=5):
    """A ledger index holding a HashLips set, as the engine would have filed
    it -- plus one lone inscription that is in no set."""
    from arcade.db import Database
    from arcade.state import install_schema

    path = home / "main-ledger.sqlite"
    db = Database(path)
    install_schema(db)
    rows = []
    for edition in range(1, count + 1):
        item = {"name": f"Doge Punks #{edition}", "edition": edition,
                "attributes": [{"trait_type": "Background",
                                "value": "Blue" if edition % 2 else "Red"}]}
        rows.append((f"{edition:064x}", count - edition, "nMe", "nMe", 100 + edition, 0,
                     "image/png", 10, "ab" * 32, json.dumps(item), 1, b"\x89PNG"))
    rows.append(("f" * 64, count, "nYou", "nYou", 200, 0, "text/plain", 5,
                 "cd" * 32, '{"name": "Hours"}', 1, b"hello"))
    for row in rows:
        db.conn.execute(
            "INSERT INTO inscription(txid,number,creator,owner,block_height,"
            "position,content_type,content_len,sha256,json,chunks,content) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", row)
    db.conn.commit()
    # An index built before collections existed is brought level on open.
    install_schema(db)
    db.close()
    return path


def test_an_older_index_is_filed_into_collections_on_open(tmp_path):
    from arcade.config import NETWORKS
    from arcade.ledger import LedgerIndex

    path = index_with_a_collection(tmp_path)
    index = LedgerIndex(path, NETWORKS["main"], rpc_factory=lambda: None)
    sets = index.collections()
    assert len(sets) == 1
    assert sets[0]["collection"] == "Doge Punks" and sets[0]["count"] == 5
    assert sets[0]["creator"] == "nMe"
    assert sets[0]["cover_txid"] == f"{1:064x}", "the cover is edition 1"
    assert index.collection_count() == 1
    assert index.inscription("f" * 64)["collection"] is None, "Hours is in no set"

    items = index.collection_items("nMe", "Doge Punks")
    assert [i["edition"] for i in items] == [1, 2, 3, 4, 5], "edition order, not number order"
    assert items[0]["number"] == 4
    assert index.collection_traits("nMe", "Doge Punks") == {
        "Background": {"Blue": 3, "Red": 2}}
    assert index.collection("nMe", "Nope") is None


def test_the_pages_and_the_api_show_the_set(client):
    app, state = client
    index_with_a_collection(state.home)

    page = app.get("/collections")
    assert page.status_code == 200
    assert "Doge Punks" in page.text and "5 items" in page.text

    page = app.get("/collections/nMe/Doge%20Punks")
    assert page.status_code == 200
    assert "Doge Punks #1" in page.text and "Background" in page.text
    assert "60.0%" in page.text, "Blue is three of five"

    assert app.get("/collections/nMe/Nope").status_code in (200, 303)

    listing = app.get("/r/collections")
    assert listing.headers["access-control-allow-origin"] == "*"
    assert listing.json()[0] == {
        "creator": "nMe", "name": "Doge Punks", "count": 5,
        "firstnumber": 0, "lastnumber": 4, "firstedition": 1, "lastedition": 5,
        "cover": f"{1:064x}", "covertype": "image/png"}
    assert app.get("/r/collections/count").json() == {"count": 1}

    one = app.get("/r/collection/nMe/Doge%20Punks?traits=1&limit=2").json()
    assert one["count"] == 5
    assert [i["edition"] for i in one["items"]] == [1, 2]
    assert one["items"][0]["json"]["name"] == "Doge Punks #1"
    assert one["items"][0]["collection"] == "Doge Punks"
    assert one["traits"] == {"Background": {"Blue": 3, "Red": 2}}
    assert app.get("/r/collection/nMe/Nope").status_code == 404

    row = app.get("/r/inscription/4").json()
    assert row["collection"] == "Doge Punks" and row["edition"] == 1


def test_the_wizard_renders_and_explains_a_missing_node(client, tmp_path):
    app, state = client
    assert app.get("/inscriptions/collection").status_code == 200
    build = hashlips(tmp_path)
    response = app.post("/inscriptions/collection/review",
                        data={"csrf_token": state.csrf_token, "folder": str(build)})
    assert response.status_code == 200
    assert "Traceback" not in response.text
    # It read the build before asking the node, so the folder is echoed back
    # and the node's absence is the complaint, not the folder.
    assert "does not look like" not in response.text


def test_the_wizard_refuses_a_folder_that_is_not_a_build(client, tmp_path):
    app, state = client
    response = app.post("/inscriptions/collection/review",
                        data={"csrf_token": state.csrf_token, "folder": str(tmp_path)})
    assert response.status_code == 200
    assert "does not look like a HashLips build" in response.text


def test_an_uploaded_folder_is_laid_out_like_a_build(client, tmp_path):
    from arcade import collections as C

    app, state = client
    build = hashlips(tmp_path, count=2)
    files = []
    for path in sorted(build.rglob("*")):
        if path.is_file():
            files.append(("files", (path.name, path.read_bytes())))
    files.append(("files", ("README.txt", b"not part of it")))
    app.post("/inscriptions/collection/review",
             data={"csrf_token": state.csrf_token}, files=files)
    saved = list((state.home / "collections").iterdir())
    assert len(saved) == 1
    read = C.read_build(saved[0])
    assert [i.edition for i in read.items] == [1, 2]
    assert not (saved[0] / "README.txt").exists()


def test_a_run_page_follows_the_job(client, tmp_path):
    app, state = client
    build = hashlips(tmp_path, count=3)
    from arcade import collections as C
    jobs, runner = state.collections
    job_id = jobs.create("main", "nMe", C.read_build(build))
    jobs.record_piece(job_id, 1, 0, "a" * 64, manifest=True)

    page = app.get(f"/inscriptions/collection/{job_id}")
    assert page.status_code == 200
    assert "Doge Punks" in page.text and "1 of 3 transactions" in page.text
    status = app.get(f"/inscriptions/collection/{job_id}/status").json()
    assert status["sent_chunks"] == 1 and status["running"] is False

    assert app.get("/inscriptions/collection/nope").status_code in (200, 303)
    assert app.get("/inscriptions/collection/nope/status").status_code == 404

    # Pausing a job that is not running just files it as paused.
    jobs.set_status(job_id, "running")
    app.post(f"/inscriptions/collection/{job_id}/pause",
             data={"csrf_token": state.csrf_token})
    assert jobs.get(job_id)["status"] == "paused"

    app.post(f"/inscriptions/collection/{job_id}/delete",
             data={"csrf_token": state.csrf_token})
    assert jobs.get(job_id) is None


def test_the_mintpad_can_be_seen_before_it_is_paid_for(client, tmp_path):
    """The page that will be inscribed, drawn from the build on disk: nothing
    on the chain, nothing spent (D-104)."""
    app, state = client
    build = hashlips(tmp_path, count=3)

    page = app.get("/inscriptions/collection/preview", params={"folder": str(build)})
    assert page.status_code == 200
    body = page.text
    assert "DOGE PUNKS MINTPAD" in body.upper()
    assert "/r/collection/" not in body, "it reads the build, not the chain"
    assert "/inscriptions/collection/preview/set" in body
    assert "'/content/' + id" not in body, "and the wall is the build's pictures"
    assert "nothing is on the chain yet" in body, "and it says so, on the button"

    listing = app.get("/inscriptions/collection/preview/set",
                      params={"folder": str(build)}).json()
    assert listing["count"] == 3
    assert [i["edition"] for i in listing["items"]] == [1, 2, 3]
    assert listing["items"][0]["json"]["name"] == "Doge Punks #1"

    piece = app.get("/inscriptions/collection/preview/piece",
                    params={"folder": str(build), "n": 2})
    assert piece.status_code == 200 and piece.content[:4] == b"\x89PNG"
    assert app.get("/inscriptions/collection/preview/piece",
                   params={"folder": str(build), "n": 99}).status_code == 404


def test_a_preview_serves_only_what_the_build_lists(client, tmp_path):
    """A route that reads a folder the user named must still not hand out
    anything that folder does not contain."""
    app, state = client
    build = hashlips(tmp_path, count=2)
    (tmp_path / "secret.txt").write_text("not yours")

    for n in ("../secret.txt", "0", "-1"):
        answer = app.get("/inscriptions/collection/preview/piece",
                         params={"folder": str(build), "n": n})
        assert answer.status_code in (404, 422), n


def test_the_review_screen_leads_with_the_name_not_the_path(client, tmp_path):
    """Two build folders one letter apart in the collection name are
    indistinguishable when the name is a table row and the path is what
    catches the eye. That is how a raw HashLips build went on the chain in
    place of a prepared one, reviewed by two people and noticed by neither
    (a test machine, D-114)."""
    app, state = client
    build = hashlips(tmp_path, count=3)

    body = app.post("/inscriptions/collection/review",
                    data={"csrf_token": state.csrf_token, "folder": str(build)}).text
    # The node is unreachable in this fixture, so the review panel does not
    # draw at all -- it is priced against a wallet. What can be asserted here
    # is the template itself, which is where the change lives.
    from pathlib import Path as _Path

    wizard = _Path("arcade/web/templates/collection_wizard.html").read_text()
    lead = wizard[wizard.index("{% if build %}"):wizard.index("<table>")]
    assert "font-size:1.35rem" in lead, "the set's name is the biggest thing here"
    assert "build.collection" in lead and "build.items[0].name" in lead, \
        "and the first piece's own name is beside it, before any path"
    assert lead.index("build.collection") < lead.index("build.folder"), \
        "the name comes before the path, which is the whole point"
    assert "First piece is called" in wizard
    assert body.count("Traceback") == 0

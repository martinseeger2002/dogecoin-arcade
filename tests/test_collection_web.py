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


def test_a_mintpad_is_not_up_until_a_block_carries_it(client, tmp_path):
    """A broadcast is not a page. "The mintpad is up. Open it" leading to "no
    such inscription" is the wallet lying about its own work (D-115)."""
    from arcade import collections as C

    app, state = client
    build = hashlips(tmp_path, count=2)
    jobs, _ = state.collections
    job_id = jobs.create("main", "nMe", C.read_build(build))
    jobs.set_pad(job_id, txid="d" * 64)

    page = app.get(f"/inscriptions/collection/{job_id}").text
    assert "The mintpad is on its way" in page
    assert "The mintpad is up" not in page, "not until the chain has it"
    assert "d" * 64 in page, "and it says which transaction to wait for"

    # Once it is indexed, it is a page, and the wallet says so.
    index_with_a_collection(state.home)
    from arcade.db import Database
    db = Database(state.home / "main-ledger.sqlite")
    db.conn.execute(
        "INSERT INTO inscription(txid,number,creator,owner,block_height,position,"
        "content_type,content_len,sha256,json,chunks,content) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)",
        ("d" * 64, 99, "nMe", "nMe", 500, 0, "text/html", 10, "ab" * 32, "{}", 1))
    db.conn.commit()
    db.close()

    page = app.get(f"/inscriptions/collection/{job_id}").text
    assert "The mintpad is up" in page


def test_a_set_already_on_the_chain_cannot_be_inscribed_again(client, tmp_path,
                                                              monkeypatch):
    """The wizard refuses before the node is asked anything, because the
    answer is already in the index: inscribing a build twice buys a second
    copy of every piece that no node will file into the set (D-120)."""
    import contextlib

    from arcade.web import app as webapp

    app, state = client
    index_with_a_collection(state.home, count=5)
    build = hashlips(tmp_path)          # the same "Doge Punks", same address

    # The node answers, so the refusal is the wizard's own and not the
    # absence of a node standing in for it.
    monkeypatch.setattr(type(state.token_chain), "rpc",
                        lambda self: contextlib.nullcontext(object()), raising=False)
    monkeypatch.setattr(webapp, "_ledger_addresses", lambda rpc: ["nMe"])

    response = app.post("/inscriptions/collection/start",
                        data={"csrf_token": state.csrf_token, "folder": str(build),
                              "fromaddress": "nMe"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/inscriptions/collection"
    page = app.get("/inscriptions/collection").text
    assert "already on this chain from this address" in page
    assert "5 pieces" in page

    jobs, _ = state.collections
    assert jobs.list() == [], "and nothing was written down"


def test_a_run_already_under_way_is_not_started_twice(client, tmp_path):
    """The other half: while a set is being inscribed there is nothing on
    the chain yet to refuse it, so the run itself is what stands in the way."""
    from arcade import collections as C

    app, state = client
    build = C.read_build(hashlips(tmp_path, count=2))
    jobs, _ = state.collections
    chain = state.token_chain
    jobs.create(chain.network, "nMe", build, name="Doge Punks",
                floor=chain.params.activation_height)

    response = app.post("/inscriptions/collection/start",
                        data={"csrf_token": state.csrf_token,
                              "folder": str(build.folder), "fromaddress": "nMe"},
                        follow_redirects=False)
    assert response.status_code == 303
    assert "is already being inscribed" in app.get("/inscriptions/collection").text
    assert len(jobs.list()) == 1

    # Somebody else's set of the same name is their own, and is not in the way.
    response = app.post("/inscriptions/collection/start",
                        data={"csrf_token": state.csrf_token,
                              "folder": str(build.folder), "fromaddress": "nSomebodyElse"},
                        follow_redirects=False)
    assert "is already being inscribed" not in app.get("/inscriptions/collection").text


def test_a_half_finished_set_is_finished_rather_than_paid_for_twice(
        client, tmp_path, monkeypatch):
    """The other shape of the same mistake. Five of the set are up, the run
    that sent them is gone, and the creator points the wizard at the build
    again: the five that are there are left alone and the rest go up (D-120)."""
    import contextlib

    from arcade import collections as C
    from arcade.web import app as webapp

    app, state = client
    index_with_a_collection(state.home, count=5)     # Doge Punks #1..#5 from nMe
    build = C.read_build(hashlips(tmp_path, count=8))

    # Far enough for the route to write the job down: the address is the
    # creator's, and the runner is never started here.
    chain = state.token_chain
    monkeypatch.setattr(type(chain), "rpc",
                        lambda self: contextlib.nullcontext(object()), raising=False)
    monkeypatch.setattr(webapp, "_ledger_addresses", lambda rpc: ["nMe"])
    jobs, runner = state.collections
    monkeypatch.setattr(type(runner), "start", lambda self, job_id: True)

    response = app.post("/inscriptions/collection/start",
                        data={"csrf_token": state.csrf_token,
                              "folder": str(build.folder), "fromaddress": "nMe"},
                        follow_redirects=False)
    assert response.status_code == 303
    runs = jobs.list()
    assert len(runs) == 1
    editions = [i["edition"] for i in jobs.items(runs[0]["id"])]
    assert editions == [6, 7, 8], "only what is not on the chain is paid for"


def test_a_run_from_an_older_floor_does_not_block_a_new_one(client, tmp_path,
                                                            monkeypatch):
    """A run belongs to a chain era.

    `collections.sqlite` was never named in the reset instruction, so after a
    floor moved it still held runs against a chain nobody reads -- and they
    went on refusing a set that no longer existed anywhere. The operator hit this
    on the first collection test after the second reset (D-125).
    """
    import contextlib

    from arcade import collections as C
    from arcade.web import app as webapp

    app, state = client
    build = C.read_build(hashlips(tmp_path, count=2))
    jobs, runner = state.collections
    chain = state.token_chain
    floor = chain.params.activation_height

    old = jobs.create(chain.network, "nMe", build, name="Doge Punks",
                      floor=(floor or 0) - 1000)
    assert jobs.get(old)["status"] != "failed", "and it is not a failed run"

    monkeypatch.setattr(type(chain), "rpc",
                        lambda self: contextlib.nullcontext(object()), raising=False)
    monkeypatch.setattr(webapp, "_ledger_addresses", lambda rpc: ["nMe"])
    monkeypatch.setattr(type(runner), "start", lambda self, job_id: True)

    response = app.post("/inscriptions/collection/start",
                        data={"csrf_token": state.csrf_token,
                              "folder": str(build.folder), "fromaddress": "nMe"},
                        follow_redirects=False)
    assert response.status_code == 303
    assert "is already being inscribed" not in app.get("/inscriptions/collection").text
    assert len(jobs.list()) == 2, "the new run was written down"

    # The old one is still there and still refuses to be resumed into this
    # chain: the half it sent is below the floor, so finishing it would make
    # a set with holes and no #1.
    app.post(f"/inscriptions/collection/{old}/resume",
             data={"csrf_token": state.csrf_token}, follow_redirects=False)
    page = app.get(f"/inscriptions/collection/{old}").text
    assert "below the floor" in page and "holes in it" in page


def test_a_run_from_this_floor_still_blocks(client, tmp_path):
    """The guard that matters is not weakened: two runs of one set on one
    chain is the thing it was written for."""
    from arcade import collections as C

    app, state = client
    build = C.read_build(hashlips(tmp_path, count=2))
    jobs, _ = state.collections
    chain = state.token_chain
    jobs.create(chain.network, "nMe", build, name="Doge Punks",
                floor=chain.params.activation_height)

    app.post("/inscriptions/collection/start",
             data={"csrf_token": state.csrf_token,
                   "folder": str(build.folder), "fromaddress": "nMe"},
             follow_redirects=False)
    assert "is already being inscribed" in app.get("/inscriptions/collection").text
    assert len(jobs.list()) == 1


def test_a_finished_run_does_not_say_go_and_resume_it(client, tmp_path):
    """"done" is not "failed", so a finished run blocked for ever -- and did
    it with "Open that run and resume it", which was wrong twice over:
    nothing was being inscribed and there was nothing to resume. Whether the
    SET exists is a question for the chain; what this file knows is whether a
    run is still going (a test machine, D-125)."""
    from arcade import collections as C

    app, state = client
    build = C.read_build(hashlips(tmp_path, count=2))
    jobs, _ = state.collections
    chain = state.token_chain
    job_id = jobs.create(chain.network, "nMe", build, name="Doge Punks",
                         floor=chain.params.activation_height)
    jobs.set_status(job_id, "done", note="every item is on its way")

    app.post("/inscriptions/collection/start",
             data={"csrf_token": state.csrf_token,
                   "folder": str(build.folder), "fromaddress": "nMe"},
             follow_redirects=False)
    page = app.get("/inscriptions/collection").text
    assert "resume it" not in page, "there is nothing to resume"
    assert "already inscribed Doge Punks on this chain" in page
    assert "None of it is indexed yet" in page, \
        "which is the only reason a finished run still stands in the way"

    # And once the chain shows the set, the chain is what answers.
    index_with_a_collection(state.home, count=5)
    app.post("/inscriptions/collection/start",
             data={"csrf_token": state.csrf_token,
                   "folder": str(hashlips(tmp_path / "again")), "fromaddress": "nMe"},
             follow_redirects=False)
    assert "already on this chain from this address" in \
        app.get("/inscriptions/collection").text


def test_the_review_says_what_number_one_will_say_about_the_set(client, tmp_path,
                                                                monkeypatch):
    """The text that becomes permanent, shown before the press.

    A build wrote a real name into the `collection` object's `artist` field —
    the object a marketplace reads for the whole set — and the only reason it
    was not inscribed for ever was an unrelated defect stopping the run. The
    screen showed the cost of that text and never the text (a test machine).
    """
    import contextlib
    import json as jsonlib

    from arcade.web import app as webapp

    app, state = client
    folder = hashlips(tmp_path, count=2)
    # Through _metadata.json, which is what a build is read from when it has
    # one -- the same file an Art Engine writes and the same place a
    # generator would put an author.
    listing = folder / "json" / "_metadata.json"
    items = jsonlib.loads(listing.read_text())
    items[0]["collection"] = {"name": "Doge Punks", "artist": "A Real Name",
                              "description": "five punks"}
    listing.write_text(jsonlib.dumps(items))

    monkeypatch.setattr(type(state.token_chain), "rpc",
                        lambda self: contextlib.nullcontext(object()), raising=False)
    monkeypatch.setattr(webapp, "_ledger_addresses", lambda rpc: ["nMe"])

    page = app.post("/inscriptions/collection/review",
                    data={"csrf_token": state.csrf_token, "folder": str(folder),
                          "fromaddress": "nMe"}).text
    assert "What piece #1 will say about the collection" in page
    assert "A Real Name" in page, "the field that would be published is on the page"
    assert "five punks" in page
    assert "names them permanently" in page

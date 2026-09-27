"""Collections in the index, on the pages, and through the API."""

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_collections import hashlips                             # noqa: E402


@pytest.fixture(autouse=True)
def on_mainnet(app_state):
    """`index_with_a_collection` writes the MAINNET ledger, so the pages are
    asked about that chain explicitly. A wallet with no choice recorded opens
    on the chain its identity is on now (D-134); before that it opened on
    mainnet and these tests inherited it."""
    (app_state.home / "tokens-chain").write_text("main\n")
    app_state._token_chain = None
    yield


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
    assert "Doge Punks" in page.text and "5 of ∞" in page.text, "N of M, or of ∞ with no cap (2026-09-25)"

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
        "cover": f"{1:064x}", "covertype": "image/png",
        "supply": None}  # no cap on its #1: unlimited (2026-09-25)
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
    for opened in (state.ledger, state.messaging):
        monkeypatch.setattr(opened, "rpc",
                            lambda: contextlib.nullcontext(object()))
    monkeypatch.setattr(webapp, "_ledger_addresses", lambda rpc: ["nMe"])

    response = app.post("/inscriptions/collection/start",
                        data={"csrf_token": state.csrf_token, "folder": str(build),
                              "fromaddress": "nMe"}, follow_redirects=False)
    # The answer lands on the page it was asked from, not two pages back --
    # which, since the POSTs stopped answering with a page, means a redirect
    # and the refusal held for the GET that follows it. (2026-09-22: this still
    # asserted the older direct render, so it read the 303 as a start.)
    assert response.status_code == 303
    page = app.get(response.headers["location"]).text
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
    assert "is already being inscribed" in app.get(response.headers["location"]).text
    assert len(jobs.list()) == 1

    # Somebody else's set of the same name is their own, and is not in the way.
    response = app.post("/inscriptions/collection/start",
                        data={"csrf_token": state.csrf_token,
                              "folder": str(build.folder), "fromaddress": "nSomebodyElse"},
                        follow_redirects=False)
    assert "is already being inscribed" not in app.get(response.headers["location"]).text


def test_a_run_under_way_is_refused_even_with_the_index_out_of_the_way(
        client, tmp_path, monkeypatch):
    """The refusal cannot be switched off by a fault in something else.

    Both of the run-list guards sat inside the same `try` as the index read, so an
    index that raised -- a node restarting, a chain set to one this node is not
    indexing -- returned before either of them ran, and the press started a second
    run of a set that is already being inscribed and paid for every piece twice.
    What that guard reads is this node's own job book, which asks nothing of a
    node, so it is outside the `try` now: an out-of-the-way index can make the
    answer say less, not make it say nothing.
    """
    from arcade import collections as C

    app, state = client
    build = C.read_build(hashlips(tmp_path, count=2))
    jobs, _ = state.collections
    chain = state.token_chain
    jobs.create(chain.network, "nMe", build, name="Doge Punks",
                floor=chain.params.activation_height)

    real = state.token_index

    class IndexThatCannotAnswer:
        """There is an index and it answers everything except the one read that
        goes through to the node -- which is the failure an index actually has.
        `LedgerIndex` holds no connection and its `__init__` only assigns
        attributes, so the object itself is never what is missing; a query is."""

        def __init__(self, index):
            self._index = index

        def collection_editions(self, *args, **kwargs):
            raise RuntimeError("the index is not answering")

        def __getattr__(self, name):
            return getattr(self._index, name)

    monkeypatch.setattr(state, "token_index",
                        lambda chain: IndexThatCannotAnswer(real(chain)))
    response = app.post("/inscriptions/collection/start",
                        data={"csrf_token": state.csrf_token,
                              "folder": str(build.folder), "fromaddress": "nMe"},
                        follow_redirects=False)
    assert "is already being inscribed" in app.get(response.headers["location"]).text
    assert len(jobs.list()) == 1, "and no second run was written down"


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
    for opened in (state.ledger, state.messaging):
        monkeypatch.setattr(opened, "rpc", lambda: contextlib.nullcontext(object()))
    monkeypatch.setattr(webapp, "_ledger_addresses", lambda rpc: ["nMe"])
    jobs, runner = state.collections
    monkeypatch.setattr(runner, "start", lambda job_id: True)

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

    for opened in (state.ledger, state.messaging):
        monkeypatch.setattr(opened, "rpc", lambda: contextlib.nullcontext(object()))
    monkeypatch.setattr(webapp, "_ledger_addresses", lambda rpc: ["nMe"])
    monkeypatch.setattr(runner, "start", lambda job_id: True)

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

    response = app.post("/inscriptions/collection/start",
                        data={"csrf_token": state.csrf_token,
                              "folder": str(build.folder), "fromaddress": "nMe"},
                        follow_redirects=False)
    assert "is already being inscribed" in app.get(response.headers["location"]).text
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

    response = app.post("/inscriptions/collection/start",
                        data={"csrf_token": state.csrf_token,
                              "folder": str(build.folder), "fromaddress": "nMe"},
                        follow_redirects=False)
    page = app.get(response.headers["location"]).text
    assert "resume it" not in page, "there is nothing to resume"
    assert "already inscribed Doge Punks on this chain" in page
    assert "None of it is indexed yet" in page, \
        "which is the only reason a finished run still stands in the way"

    # And once the chain shows the set, the chain is what answers.
    index_with_a_collection(state.home, count=5)
    response = app.post("/inscriptions/collection/start",
                        data={"csrf_token": state.csrf_token,
                              "folder": str(hashlips(tmp_path / "again")),
                              "fromaddress": "nMe"}, follow_redirects=False)
    assert "already on this chain from this address" in app.get(
        response.headers["location"]).text


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

    for opened in (state.ledger, state.messaging):
        monkeypatch.setattr(opened, "rpc",
                            lambda: contextlib.nullcontext(object()))
    monkeypatch.setattr(webapp, "_ledger_addresses", lambda rpc: ["nMe"])

    page = app.post("/inscriptions/collection/review",
                    data={"csrf_token": state.csrf_token, "folder": str(folder),
                          "fromaddress": "nMe"}).text
    assert "What piece #1 will say about the collection" in page
    assert "A Real Name" in page, "the field that would be published is on the page"
    assert "five punks" in page
    assert "names them permanently" in page


def test_a_set_entirely_on_the_chain_says_so_instead_of_crashing(client, tmp_path,
                                                                 monkeypatch):
    """The refusal has to survive its own page.

    Pricing "what this run would actually send" empties the build when every
    piece is already up, and the page then died on `build.items[0]` --
    "list object has no element 0" -- with the explanation sitting in a
    variable it never reached. A crash in front of a message is worse than no
    message: it says nothing and looks like a fault in the wizard (D-128).
    """
    import contextlib

    from arcade.web import app as webapp

    app, state = client
    index_with_a_collection(state.home, count=5)      # Doge Punks #1..#5 from nMe
    build = hashlips(tmp_path, count=5)               # the same five

    for opened in (state.ledger, state.messaging):
        monkeypatch.setattr(opened, "rpc",
                            lambda: contextlib.nullcontext(object()))
    monkeypatch.setattr(webapp, "_ledger_addresses", lambda rpc: ["nMe"])

    page = app.post("/inscriptions/collection/review",
                    data={"csrf_token": state.csrf_token, "folder": str(build),
                          "fromaddress": "nMe"})
    assert page.status_code == 200
    assert "no element 0" not in page.text and "Traceback" not in page.text
    assert "already on this chain from this address, all 5 pieces of it" in page.text
    assert "Doge Punks #1" in page.text, "and the build is still described"


def test_a_mintpad_with_no_price_keeps_the_review(client, tmp_path, monkeypatch):
    """One missing field should not cost the whole review.

    Ticking the mintpad and leaving the price empty threw on the server and
    bounced back to step one: the build, the costing, the addresses and
    everything typed, gone, to say one word about one box. The browser now
    refuses to submit at all (the field is `required` while the pad is on),
    and if it arrives anyway the answer lands on the page it came from
    (D-129).
    """
    import contextlib

    from arcade.web import app as webapp

    app, state = client
    build = hashlips(tmp_path, count=2)
    for opened in (state.ledger, state.messaging):
        monkeypatch.setattr(opened, "rpc",
                            lambda: contextlib.nullcontext(object()))
    monkeypatch.setattr(webapp, "_ledger_addresses", lambda rpc: ["nMe"])

    response = app.post("/inscriptions/collection/start",
                        data={"csrf_token": state.csrf_token, "folder": str(build),
                              "fromaddress": "nMe", "launchpad": "yes",
                              "pad_amount": "", "pad_kind": "coins"},
                        follow_redirects=False)
    assert response.status_code == 303
    page = app.get(response.headers["location"])
    assert page.status_code == 200, "the review, not a redirect to the top"
    assert "say what one costs" in page.text
    assert "Doge Punks" in page.text and "Inscribe" in page.text, \
        "with the build and the button still there"
    jobs, _ = state.collections
    assert jobs.list() == [], "and nothing was written down"


def test_the_price_is_required_while_the_pad_is_on(client, tmp_path, monkeypatch):
    """Belt to the server's braces: the browser will not send it at all."""
    import contextlib

    from arcade.web import app as webapp

    app, state = client
    for opened in (state.ledger, state.messaging):
        monkeypatch.setattr(opened, "rpc",
                            lambda: contextlib.nullcontext(object()))
    monkeypatch.setattr(webapp, "_ledger_addresses", lambda rpc: ["nMe"])
    page = app.post("/inscriptions/collection/review",
                    data={"csrf_token": state.csrf_token,
                          "folder": str(hashlips(tmp_path, count=2)),
                          "fromaddress": "nMe"}).text
    assert 'id="pad-amount"' in page
    assert "amount.required = box.checked" in page, \
        "and it follows the tick, because a required field inside a hidden " \
        "panel cannot be filled in"
    # The pad starts OFF, so the price starts not required: a `required`
    # field inside a panel nobody opened is a form that will not submit and
    # will not say why.
    box = page[page.index('id="pad-amount"'):]
    assert "required" not in box[:box.index(">")]


# --- the mintpad is a separate decision (D-149) -------------------------------

def _review(app, state, folder, monkeypatch):
    import contextlib

    from arcade.web import app as webapp
    for opened in (state.ledger, state.messaging):
        monkeypatch.setattr(opened, "rpc",
                            lambda: contextlib.nullcontext(object()))
    monkeypatch.setattr(webapp, "_ledger_addresses", lambda rpc: ["nMe"])
    return app.post("/inscriptions/collection/review",
                    data={"csrf_token": state.csrf_token, "folder": str(folder),
                          "fromaddress": "nMe"}).text


def test_the_mintpad_is_offered_rather_than_assumed(client, tmp_path, monkeypatch):
    """A collection and a shop are two decisions. Inscribing a shop front
    nobody asked for spends their coins on a page they did not want."""
    app, state = client
    page = _review(app, state, hashlips(tmp_path, count=2), monkeypatch)
    tick = page[page.index('id="launchpad"') - 200:page.index('id="launchpad"') + 60]
    assert "checked" not in tick, "off unless asked for"
    assert 'id="pad-fields" hidden' in page, "and everything about it is folded away"
    assert "optional" in page


def test_a_run_with_no_mintpad_writes_no_pad(client, tmp_path, monkeypatch):
    import contextlib

    from arcade.web import app as webapp
    app, state = client
    build = hashlips(tmp_path, count=2)
    for opened in (state.ledger, state.messaging):
        monkeypatch.setattr(opened, "rpc",
                            lambda: contextlib.nullcontext(object()))
    monkeypatch.setattr(webapp, "_ledger_addresses", lambda rpc: ["nMe"])
    app.post("/inscriptions/collection/start",
             data={"csrf_token": state.csrf_token, "folder": str(build),
                   "fromaddress": "nMe"}, follow_redirects=False)
    jobs, _ = state.collections
    job = jobs.get(jobs.list()[0]["id"])
    assert job["pad_json"] == "" and job["pad_html"] == ""


def test_the_html_box_holds_the_page_as_it_will_be_inscribed(client, tmp_path,
                                                             monkeypatch):
    """Not a placeholder version of it. What the editor shows is what goes on
    the chain, so the creator address in the box is the real one -- the
    preview's own `creator=preview` is a rewrite for drawing only."""
    app, state = client
    page = _review(app, state, hashlips(tmp_path, count=2), monkeypatch)
    box = page[page.index('id="pad-html"'):]
    box = box[box.index(">") + 1:box.index("</textarea>")]
    assert "CREATOR" in box and "nMe" in box
    assert "DOGE PUNKS MINTPAD" in box.upper()
    assert "preview" not in box.lower(), "the real page, not the preview's"


def test_the_live_preview_uses_the_servers_own_substitutions(client, tmp_path,
                                                             monkeypatch):
    """One definition of what turns the page into a preview. Two copies is a
    preview that stops matching the page the moment either is touched."""
    app, state = client
    page = _review(app, state, hashlips(tmp_path, count=2), monkeypatch)
    assert "var PAD_SWAPS = [" in page
    assert "/inscriptions/collection/preview/set" in page
    assert "/inscriptions/collection/preview/piece" in page
    assert "frame.srcdoc = html + PAD_NOTE" in page, "drawn in the browser"
    assert "nothing is on the chain yet" in page, "and it still says so"


def test_an_unedited_page_is_not_carried_on_the_job(client, tmp_path, monkeypatch):
    """A textarea posts CRLF whatever it was handed, so a page nobody touched
    comes back different on every line. Comparing after normalising is what
    keeps "I changed nothing" from freezing today's template onto the run."""
    import contextlib

    from arcade.messaging.keys import Identity
    from arcade.web import app as webapp
    app, state = client
    build = hashlips(tmp_path, count=2)
    # A pad carries the node's messaging identity in its shop JSON, so one
    # has to exist before a pad can be asked for at all.
    state.identity = Identity.generate()
    for opened in (state.ledger, state.messaging):
        monkeypatch.setattr(opened, "rpc",
                            lambda: contextlib.nullcontext(object()))
    monkeypatch.setattr(webapp, "_ledger_addresses", lambda rpc: ["nMe"])

    from arcade import mintpad as M
    standard = M.page("nMe", "Doge Punks").decode("utf-8")
    app.post("/inscriptions/collection/start",
             data={"csrf_token": state.csrf_token, "folder": str(build),
                   "fromaddress": "nMe", "launchpad": "yes", "pad_amount": "5",
                   "pad_kind": "coins",
                   "pad_html": standard.replace("\n", "\r\n")},
             follow_redirects=False)
    jobs, _ = state.collections
    job = jobs.get(jobs.list()[0]["id"])
    assert job["pad_json"], "the pad was asked for"
    assert job["pad_html"] == "", "but the page was left alone"


def test_an_edited_page_is_carried_on_the_job(client, tmp_path, monkeypatch):
    import contextlib

    from arcade.messaging.keys import Identity
    from arcade.web import app as webapp
    app, state = client
    build = hashlips(tmp_path, count=2)
    state.identity = Identity.generate()
    for opened in (state.ledger, state.messaging):
        monkeypatch.setattr(opened, "rpc",
                            lambda: contextlib.nullcontext(object()))
    monkeypatch.setattr(webapp, "_ledger_addresses", lambda rpc: ["nMe"])

    mine = "<!doctype html><h1>mine</h1><script>const CREATOR='nMe';</script>"
    app.post("/inscriptions/collection/start",
             data={"csrf_token": state.csrf_token, "folder": str(build),
                   "fromaddress": "nMe", "launchpad": "yes", "pad_amount": "5",
                   "pad_kind": "coins", "pad_html": mine},
             follow_redirects=False)
    jobs, _ = state.collections
    job = jobs.get(jobs.list()[0]["id"])
    assert job["pad_html"] == mine


def test_the_page_script_survives_the_closing_tag_inside_it(client, tmp_path,
                                                            monkeypatch):
    """The preview note contains a literal `</script>`.

    An HTML parser ends a script block at that sequence wherever it appears
    -- inside a JavaScript string literal included -- so embedding it raw
    killed the whole inline script and every control on the mintpad panel
    did nothing at all. The bytes were correct server-side, which is why
    only a browser found it; this is the cheap guard against it coming back.
    """
    app, state = client
    page = _review(app, state, hashlips(tmp_path, count=2), monkeypatch)
    script = page[page.index("var PAD_SWAPS"):]
    script = script[:script.index("</script>")]
    assert "</script>" not in script
    assert "<\\/script>" in script, "the closing tag has to be escaped, not removed"


def test_a_public_collection_page_never_asks_the_node_s_wallet(client, monkeypatch):
    """Plan item 4, D-184's shape on the collection page: on a public copy the
    offer form is the looking account's, so the node's wallet -- a stranger's
    wallet there -- is not asked what it holds. Drawing its tokens and coins
    into a visitor's form told the visitor what the operator holds."""
    app, state = client
    index_with_a_collection(state.home)
    state.public = True
    try:
        # Only the wallet's own questions count: reading the chain or the
        # mempool says nothing about whose coins these are.
        WALLET = {"getbalance", "listunspent", "getaddressesbylabel",
                  "getaddressesbyaccount", "listreceivedbyaddress",
                  "listaddressgroupings", "getwalletinfo", "listlabels"}
        asked = []

        class Recorder:
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
            def call(self, method, *a):
                if method in WALLET:
                    asked.append(method)
                raise RuntimeError("no node in this test")

        monkeypatch.setattr(type(state.ledger), "rpc", lambda self, *a, **k: Recorder(),
                            raising=False)
        page = app.get("/exchange/collection/nMe/Doge%20Punks")
        assert page.status_code == 200, page.text[:500]
        assert "Doge Punks" in page.text
        assert asked == [], f"the node's wallet was asked on a public page: {asked}"
    finally:
        state.public = False


def test_a_sealed_set_says_so_on_its_own_page_and_api(client):
    """#1's details reach the single-set page and API, not just the list
    (filming, 2026-09-26: "Bone Brigade" sealed at 8 said supply null)."""
    import sqlite3
    app, state = client
    path = index_with_a_collection(state.home)
    one = {"name": "Doge Punks #1", "edition": 1,
           "collection": {"name": "Doge Punks", "description": "Five punks.",
                          "artist": "@nMe", "url": "https://example.com", "supply": 8}}
    conn = sqlite3.connect(path)
    conn.execute("UPDATE inscription SET json = ? WHERE txid = ?",
                 (json.dumps(one), f"{1:064x}"))
    conn.commit()
    conn.close()
    assert app.get("/r/collection/nMe/Doge%20Punks").json()["supply"] == 8
    page = app.get("/collections/nMe/Doge%20Punks").text
    assert "5 of 8" in page and "Five punks." in page and "Art by @nMe" in page

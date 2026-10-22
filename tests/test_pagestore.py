"""What the wallet remembers for an inscribed page."""

import pytest

from arcade import pagestore as P


def test_a_page_keeps_and_forgets_things(tmp_path):
    store = P.PageStore(tmp_path / "pagedata.sqlite")
    assert store.items("a" * 64) == {}
    store.set("a" * 64, "best", "42")
    store.set("a" * 64, "name", "Sunrise")
    store.set("a" * 64, "best", "43")
    assert store.items("a" * 64) == {"best": "43", "name": "Sunrise"}
    assert store.items("b" * 64) == {}, "another page sees nothing of it"
    store.remove("a" * 64, "name")
    assert store.items("a" * 64) == {"best": "43"}
    store.clear("a" * 64)
    assert store.items("a" * 64) == {}


def test_a_page_cannot_fill_the_disk(tmp_path):
    store = P.PageStore(tmp_path / "pagedata.sqlite")
    with pytest.raises(P.StoreError, match="characters"):
        store.set("a" * 64, "k", "v" * (P.MAX_VALUE + 1))
    with pytest.raises(P.StoreError, match="characters"):
        store.set("a" * 64, "", "v")
    with pytest.raises(P.StoreError, match="characters"):
        store.set("a" * 64, "k" * (P.MAX_KEY + 1), "v")
    # Up to the total, then no more -- but replacing a value is not adding.
    piece = "v" * P.MAX_VALUE
    for i in range(P.MAX_TOTAL // P.MAX_VALUE):
        store.set("a" * 64, f"k{i}", piece[:-4])
    with pytest.raises(P.StoreError, match="in all"):
        store.set("a" * 64, "one more", piece)
    store.set("a" * 64, "k0", "small")
    assert store.items("a" * 64)["k0"] == "small"
    assert store.items("b" * 64) == {}
    store.set("b" * 64, "k", "v"), "another page's room is its own"


def test_the_wallet_serves_the_shim_and_guards_the_store():
    import tempfile
    from pathlib import Path

    from fastapi.testclient import TestClient

    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    home = Path(tempfile.mkdtemp())
    state = AppState(home=home,
                     messaging=ChainContext(network="regtest", role="messaging", label="T",
                                            datadir=Path("/nonexistent")),
                     ledger=ChainContext(network="main", role="ledger", label="M",
                                         datadir=Path("/nonexistent")))
    app = TestClient(create_app(state))
    shim = app.get("/r/storage.js")
    assert shim.status_code == 200 and "javascript" in shim.headers["content-type"]
    assert shim.headers["access-control-allow-origin"] == "*"
    assert "arcade.storage" in shim.text and "localStorage" in shim.text
    assert "history: function" in shim.text, "a page can read back what a key held before"
    # The store itself is the viewer's, same origin, CSRF-guarded, and only
    # for inscriptions this wallet knows.
    assert app.get("/storage/" + "a" * 64).status_code == 404
    assert "access-control-allow-origin" not in app.get("/storage/" + "a" * 64).headers
    assert app.post("/storage/" + "a" * 64, json={"op": "set", "key": "k", "value": "v"}).status_code == 400
    assert app.post("/storage/" + "a" * 64, json={"csrf_token": state.csrf_token, "op": "set",
                                                  "key": "k", "value": "v"}).status_code == 404
    assert state.pagestore.items("a" * 64) == {}


def test_a_key_keeps_its_last_values(tmp_path):
    """2026-10-20: a game saved a new player over the old one and nothing could bring it back."""
    store, page = P.PageStore(tmp_path / "pagedata.sqlite"), "a" * 64
    for n in range(P.HISTORY + 3):
        store.set(page, "save", f"v{n}")
    store.set(page, "save", f"v{P.HISTORY + 2}")         # the same value again is not a change
    store.set(page, "best", "1")
    kept = store.history(page)["save"]
    assert [h["value"] for h in kept] == [f"v{n}" for n in range(P.HISTORY + 1, 1, -1)]
    assert "best" not in store.history(page), "a first value replaced nothing"
    store.remove(page, "save")
    assert store.history(page, "save")["save"][0]["value"] == f"v{P.HISTORY + 2}"
    store.clear(page)
    assert store.history(page, "best")["best"][0]["value"] == "1"
    assert store.history("b" * 64) == {}, "another page sees nothing of it"


def test_history_never_holds_more_than_a_page_may_keep(tmp_path):
    store, page = P.PageStore(tmp_path / "pagedata.sqlite"), "a" * 64
    for n in range(40):
        store.set(page, f"k{n % 4}", str(n) * (P.MAX_VALUE // 2 // len(str(n))))
    total = sum(len(k) + len(h["value"]) for k, hs in store.history(page).items() for h in hs)
    assert 0 < total <= P.MAX_TOTAL

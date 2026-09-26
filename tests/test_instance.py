"""Identity by key: an arcade says on the chain who runs it (arcade/instance.py).

Order item "Identity by key instead of by name" (docs/multi-user.md): an
instance announces its domain and revision in a transaction paid for by its
fee address; the directory lists them and a visitor's browser checks each
domain's /.well-known/dogecoinarcade.json against the chain.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

from arcade import instance                                      # noqa: E402
from arcade.messaging.envelope import Header, TYPE_INSTANCE      # noqa: E402
from arcade.messaging.store import MessageStore                  # noqa: E402

EDGE = {"host": "node.dogecoinarcade.com", "cf-ray": "abc123-LHR"}


# --- the payload -------------------------------------------------------------------

def test_an_announcement_says_a_domain_and_a_revision_and_reads_back():
    payload = instance.build("App.DogecoinArcade.com", "72D6533")
    assert Header.decode(payload).type == TYPE_INSTANCE
    assert instance.parse(payload) == {"domain": "app.dogecoinarcade.com",
                                       "revision": "72d6533"}
    # Class B pads with NULs, and one transaction's output is enough.
    assert instance.parse(payload + b"\x00" * 9)["domain"] == "app.dogecoinarcade.com"
    assert len(payload) <= 72, "fits one OP_RETURN"


@pytest.mark.parametrize("bad", ["localhost", "1.2.3.4", "no spaces.com x", "",
                                 "a" * 300 + ".com", "arcade.example.com:8420"])
def test_a_domain_is_a_hostname_and_nothing_else(bad):
    with pytest.raises(ValueError):
        instance.build(bad, "abcdef1")


def test_a_scheme_or_a_path_is_trimmed_not_trusted():
    assert instance.clean_domain("https://arcade.example.com/join") == "arcade.example.com"


def test_anything_else_is_not_an_announcement():
    from arcade.release import build_notice
    assert instance.parse(build_notice("abcdef1")) is None
    assert instance.parse(b"arcm\x01\x09hello there world") is None
    assert instance.parse(b"not arcade at all") is None


# --- the index ---------------------------------------------------------------------

def test_the_directory_keeps_the_latest_per_address_and_domain(tmp_path):
    store = MessageStore(tmp_path / "m.sqlite")
    store.add_instance_announcement("aa" * 32, "test", "nFee1", "a.example.com", "1111111", 100, 1000)
    store.add_instance_announcement("bb" * 32, "test", "nFee1", "a.example.com", "2222222", 110, 1100)
    store.add_instance_announcement("cc" * 32, "test", "nFee2", "a.example.com", "3333333", 105, 1050)
    store.add_instance_announcement("dd" * 32, "test", "nFee3", "b.example.com", "4444444", 0, 0)
    rows = [(r["address"], r["revision"]) for r in store.instances("test")]
    # nFee1's newer announcement replaces its older one; nFee2 is a separate claim
    # to the same domain (the browser check tells them apart); a pool row waits.
    assert rows == [("nFee1", "2222222"), ("nFee2", "3333333")]
    store.add_instance_announcement("dd" * 32, "test", "nFee3", "b.example.com", "4444444", 120, 1200)
    assert ("nFee3", "4444444") in [(r["address"], r["revision"]) for r in store.instances("test")]


# --- the pages ---------------------------------------------------------------------

def test_the_claim_file_is_public_and_readable_from_any_site(client):
    app, state = client
    answer = app.get("/.well-known/dogecoinarcade.json", headers=EDGE)
    assert answer.status_code == 200
    assert answer.headers["access-control-allow-origin"] == "*"
    said = answer.json()
    assert set(said) >= {"fee_address", "revision", "domains", "announced"}


def test_the_directory_is_public_and_lists_what_the_chain_says(client):
    app, state = client
    with state.store() as store:
        store.add_instance_announcement("ee" * 32, state.messaging.network, "nSomeFeeAddr",
                                        "arcade.example.com", "abcdef1", 200, 2000)
    page = app.get("/instances", headers=EDGE)
    assert page.status_code == 200
    assert "arcade.example.com" in page.text and "nSomeFeeAddr" in page.text
    assert "/.well-known/dogecoinarcade.json" in page.text      # the browser check
    assert "Announce this arcade" not in page.text               # not for strangers


def test_only_the_operator_may_announce(client):
    app, state = client
    refused = app.post("/instances/announce", data={"domain": "x.example.com"}, headers=EDGE)
    assert refused.status_code in (403, 404)
    assert "Announce this arcade" in app.get("/instances").text  # the operator's own view

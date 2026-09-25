"""The installable app, and push notifications for the Messenger.

2026-09-25. What is pinned here: a push carries nothing and wakes only
the account whose address a message transaction paid; the node never learns
more than the chain already says; a device that is gone is forgotten; and the
app shell is public while the news is the signed-in account's alone.
"""

import base64
import json
import pathlib
import sys

import pytest
from nacl.signing import SigningKey

pytest.importorskip("cryptography", reason="push needs cryptography: pip install .[push]")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_auth_web import LOCAL, _sign_in                        # noqa: E402

from arcade import push as pushlib                               # noqa: E402
from arcade.web import door                                      # noqa: E402

ENDPOINT = "https://push.example.com/send/abc"
ALICE, BOB = "aa" * 32, "bb" * 32


class Service:
    """A push service: records what it was sent, answers with `status`."""

    def __init__(self, status=201):
        self.status, self.sent = status, []

    def __call__(self, url, data=b"", timeout=None, headers=None):
        self.sent.append({"url": url, "data": data, "headers": headers})
        return type("Answer", (), {"status_code": self.status})()


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_the_push_is_empty_and_signed_by_this_node(tmp_path):
    push = pushlib.Push(tmp_path)
    service = Service()
    push.subscribe(ALICE, ENDPOINT)
    assert push.tell(ALICE, "11" * 32, "nSender", post=service) == 1
    sent = service.sent[0]
    assert sent["data"] == b"", "a push carries nothing: the phone asks the node"
    auth = sent["headers"]["Authorization"]
    assert auth.startswith("vapid t=") and f"k={push.public_key()}" in auth

    # The JWT is a real ES256 signature by the key the browser subscribed with.
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    jwt = auth.split("t=")[1].split(",")[0]
    head, body, sig = jwt.split(".")
    claims = json.loads(_unb64(body))
    assert claims["aud"] == "https://push.example.com"
    raw = _unb64(sig)
    public = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), _unb64(push.public_key()))
    public.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"),
                                       int.from_bytes(raw[32:], "big")),
                  f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))


def test_the_nodes_key_is_kept(tmp_path):
    assert pushlib.Push(tmp_path).public_key() == pushlib.Push(tmp_path).public_key()


def test_a_device_the_service_says_is_gone_is_forgotten(tmp_path):
    push = pushlib.Push(tmp_path)
    push.subscribe(ALICE, ENDPOINT)
    assert push.tell(ALICE, "11" * 32, "nSender", post=Service(410)) == 0
    assert push.devices(ALICE) == []


def test_one_message_is_told_once(tmp_path):
    push = pushlib.Push(tmp_path)
    service = Service()
    push.subscribe(ALICE, ENDPOINT)
    push.tell(ALICE, "11" * 32, "nSender", post=service)
    push.tell(ALICE, "11" * 32, "nSender", post=service)
    assert len(service.sent) == 1
    news = push.news(ALICE)
    assert [n["sender"] for n in news] == ["nSender"]
    assert push.news(ALICE) == [], "shown once"


def _watch(push, rows, paid, owners, service):
    return push.watch(lambda after: [r for r in rows if r["cursor"] > after],
                      lambda: max([r["cursor"] for r in rows] or [0]),
                      lambda txid: paid[txid], owners.get, post=service)


def test_only_the_account_a_message_paid_is_woken(tmp_path):
    push = pushlib.Push(tmp_path)
    service = Service()
    push.subscribe(ALICE, ENDPOINT)
    push.subscribe(BOB, ENDPOINT + "-bob")
    owners = {"nAlice": ALICE, "nBob": BOB, "nCarol": "cc" * 32}
    rows = []
    assert _watch(push, rows, {}, owners, service) == 0     # first pass: notes where it is
    rows.append({"cursor": 1, "txid": "11" * 32, "sender_addr": "nCarol"})
    # the message pays Alice; the change goes back to Carol, the sender
    paid = {"11" * 32: ["nAlice", "nCarol"]}
    assert _watch(push, rows, paid, owners, service) == 1
    assert [s["url"] for s in service.sent] == [ENDPOINT]
    assert push.news(BOB) == [] and push.news("cc" * 32) == []


def test_turning_it_on_does_not_wake_anybody_for_old_mail(tmp_path):
    push = pushlib.Push(tmp_path)
    service = Service()
    push.subscribe(ALICE, ENDPOINT)
    rows = [{"cursor": n, "txid": f"{n:064x}", "sender_addr": "nX"} for n in range(1, 6)]
    paid = {r["txid"]: ["nAlice"] for r in rows}
    assert _watch(push, rows, paid, {"nAlice": ALICE}, service) == 0
    assert service.sent == []


# --- the app ------------------------------------------------------------------------


def test_the_app_shell_is_public(client):
    for path in ("/manifest.webmanifest", "/sw.js", "/offline", "/icon-192.png",
                 "/icon-512.png", "/push/key", "/account/push/news"):
        assert door.public_path(path), path
    for path in ("/account/push/subscribe", "/account/push/unsubscribe"):
        assert door.public_path(path, "POST"), path


def test_the_manifest_makes_it_installable(client):
    app, state = client
    manifest = app.get("/manifest.webmanifest").json()
    assert manifest["display"] == "standalone" and manifest["start_url"] == "/"
    assert {i["sizes"] for i in manifest["icons"]} >= {"192x192", "512x512"}
    for icon in manifest["icons"]:
        assert app.get(icon["src"]).headers["content-type"] == "image/png"
    assert 'rel="manifest"' in app.get("/join", headers=LOCAL).text


def test_the_service_worker_never_caches_a_page(client):
    """Pages, balances and messages always come live; one offline page is kept."""
    app, state = client
    sw = app.get("/sw.js")
    assert sw.headers["cache-control"] == "no-cache"
    assert "KEEP = ['/offline', '/icon-192.png']" in sw.text
    assert "cache.put" not in sw.text and ".put(" not in sw.text
    assert app.get("/offline").status_code == 200


def test_news_is_for_the_signed_in_account_only(client):
    app, state = client
    assert app.get("/account/push/news", headers=LOCAL).status_code == 403
    assert app.post("/account/push/subscribe", headers=LOCAL,
                    json={"endpoint": ENDPOINT}).status_code == 403

    key = SigningKey.generate()
    assert _sign_in(app, key).status_code == 200
    me = key.verify_key.encode().hex()
    assert app.post("/account/push/subscribe", headers=LOCAL, json={
        "endpoint": ENDPOINT, "keys": {"p256dh": "x", "auth": "y"}}).json() == {"ok": True}
    assert state.push().devices(me) == [ENDPOINT]
    state.push().tell(me, "22" * 32, "nSomebody", post=Service())
    state.push().tell(ALICE, "33" * 32, "nSomebodyElse", post=Service())
    news = app.get("/account/push/news", headers=LOCAL).json()["news"]
    assert [n["txid"] for n in news] == ["22" * 32], "only this account's"
    assert "text" not in news[0] and "body" not in news[0]


def test_the_worker_is_registered_by_a_script_not_printed_on_the_page(client):
    """Merging two blocks into base.html once left this code outside its <script>
    tag, where every page would have shown it as text."""
    app, state = client
    page = app.get("/join", headers=LOCAL).text
    at = page.index("serviceWorker.register")
    assert page.rfind("<script", 0, at) > page.rfind("</script>", 0, at)

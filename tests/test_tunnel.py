"""The Cloudflare tunnel wizard (arcade/tunnel.py), against a fake cloudflared.

2026-09-25: guide a new operator from nothing to their arcade on their own
domain. What is pinned: a machine's existing tunnel is read, never written; each
step says what went wrong in words a person can act on; and the check from
outside tells the public splash from the wallet.
"""

import pathlib
import sys
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

from arcade import tunnel                                        # noqa: E402

ID = "63bf0487-635a-4792-9cbb-b9b777cb73d7"


def fake_run(answers):
    """subprocess.run that answers by the cloudflared subcommand."""
    calls = []

    def run(args, **kw):
        calls.append(args)
        key = " ".join(args[1:3])
        rc, out = answers.get(key, (0, ""))
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr="")
    run.calls = calls
    return run


def wizard(tmp_path, answers=None):
    return tunnel.Wizard(home=tmp_path, run=fake_run(answers or {}),
                         which=lambda name: "/usr/bin/cloudflared")


def test_an_existing_tunnel_is_read_and_left_alone(tmp_path):
    cf = tmp_path / ".cloudflared"
    cf.mkdir()
    (cf / "config.yml").write_text("tunnel: x\ningress:\n  - hostname: app.mine.com\n"
                                   "    service: http://127.0.0.1:8420\n  - service: http_status:404\n")
    (cf / "cert.pem").write_text("x")
    w = wizard(tmp_path)
    got = w.status()
    assert got["existing_hostnames"] == ["app.mine.com"] and got["signed_in"]
    w.write_config(ID, ["app.new.com"], 8420)
    assert "app.mine.com" in (cf / "config.yml").read_text(), "its own config is untouched"
    assert (cf / tunnel.CONFIG_NAME).exists()


def test_create_reads_the_tunnel_id_out_of_cloudflared(tmp_path):
    (tmp_path / ".cloudflared").mkdir()
    (tmp_path / ".cloudflared" / "cert.pem").write_text("x")
    w = wizard(tmp_path, {"tunnel create": (0, f"Created tunnel my-arcade with id {ID}\n")})
    got = w.create("My Arcade!")
    assert got["id"] == ID and got["name"] == "my-arcade"


def test_create_before_signing_in_says_so(tmp_path):
    with pytest.raises(tunnel.TunnelError, match="sign in"):
        wizard(tmp_path).create("x")


def test_a_name_that_already_has_a_dns_record_is_explained(tmp_path):
    w = wizard(tmp_path, {"tunnel route": (1, "Failed to add route: code: 1003, reason: "
                                              "An A, AAAA, or CNAME record with that host already exists.")})
    with pytest.raises(tunnel.TunnelError, match="already has a DNS record"):
        w.route("t", "app.example.com")


def test_the_config_points_both_names_at_this_node(tmp_path):
    path = pathlib.Path(wizard(tmp_path).write_config(ID, ["app.a.com", "pages.a.com"], 8420))
    text = path.read_text()
    assert f"tunnel: {ID}" in text and text.count("service: http://127.0.0.1:8420") == 2
    assert text.rstrip().endswith("service: http_status:404")


def test_the_check_tells_the_public_page_from_the_wallet():
    ok = tunnel.verify("app.a.com", fetch=lambda url: "<p>12 of 100 seats open</p>")
    assert ok["ok"]
    wallet = tunnel.verify("app.a.com", fetch=lambda url: "seats ... Spendable 12")
    assert not wallet["ok"] and "WALLET" in wallet["said"]

    def down(url):
        raise OSError("no route")
    assert not tunnel.verify("app.a.com", fetch=down)["ok"]


def test_the_wizard_answers_only_at_the_node_machine(client):
    app, state = client
    state.set_setting("public_hosts", ["node.dogecoinarcade.com"])
    try:
        edge = {"host": "node.dogecoinarcade.com", "cf-ray": "x"}
        assert app.get("/admin/api/tunnel", headers=edge).status_code in (401, 403, 404)
        local = app.get("/admin/api/tunnel", headers={"host": "127.0.0.1:8420"})
        assert local.status_code == 200 and "cloudflared" in local.json()
    finally:
        state.set_setting("public_hosts", [])

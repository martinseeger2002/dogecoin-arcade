"""The script policy, phase 1 (2026-11-09, security review): the unlocked words live in sessionStorage, so any script
running on this origin could read them. Every page is measured against `script-src 'self' 'nonce-...'`, report-only:
every inline <script> a template writes carries the request's nonce, the browser reports what would still be stopped
to /csp-report, and the count of inline on...= handlers (phase 2 moves them) may only go down.

Phase 2 (2026-11-09): the same policy can be enforced (Admin -> Safety -> "Enforce the script policy", or
ARCADE_ENFORCE_SCRIPT_POLICY=1 / --enforce-script-policy for a whole process): ONE Content-Security-Policy header with
the page policy and the script policy together, still reported."""
import json
import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_safe_headers import LOCAL, _html_pages                # noqa: E402

from arcade.web import door                                      # noqa: E402
from arcade.web import app as webapp                             # noqa: E402
from arcade.web.app import (PAGE_CSP, SCRIPT_POLICY_ENV, SCRIPT_POLICY_SETTING, CspReports,  # noqa: E402
                            enforced_page_policy)

ROOT = pathlib.Path(__file__).resolve().parent.parent / "arcade"
TEMPLATE_DIRS = (ROOT / "web" / "templates", ROOT / "lite" / "templates")

#: Read as plain files, never through Jinja, so a `{{ csp_nonce }}` in them would be sent as it is written: the
#: mintpad and launchpad are bytes that go on a chain, and the offline recovery page carries its own hash policy.
NOT_JINJA = {"mintpad.html", "tokenpad.html", "recover.html"}

#: Inline on...= handlers in the templates on 2026-11-09 (68 in 32 files). Phase 2 moves them into the pages' scripts;
#: until then this number may only go down. Lower it when you move one; never raise it -- add a listener instead.
INLINE_HANDLERS_AT_MOST = 68

#: Pages whose inline scripts carry no nonce yet (phase 2): inscription previews drawn as the page that will be
#: inscribed, the nodes globe (the same file GitHub Pages serves) and the mesh browser are files, not templates; the
#: DogeFeedX Network page is sent as its template's unrendered text (dogefeedx/network.py: `render` is no module name).
UNNONCED_PAGES = {"/globe", "/tokens/launchpad/preview", "/inscriptions/collection/preview", "/browser",
                  "/dogefeedx/network"}

SCRIPT_TAG = re.compile(r"<script\b[^>]*>", re.I)
HANDLER = re.compile(r"\son[a-z]+=")
JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.S)


def _templates():
    for folder in TEMPLATE_DIRS:
        for path in sorted(folder.rglob("*.html")):
            if path.name not in NOT_JINJA:
                yield path, JINJA_COMMENT.sub("", path.read_text(encoding="utf-8"))


def test_every_inline_script_a_template_writes_carries_the_nonce():
    bare = []
    for path, text in _templates():
        for tag in SCRIPT_TAG.findall(text):
            if "src=" not in tag and 'nonce="{{ csp_nonce }}"' not in tag:
                bare.append(f"{path.relative_to(ROOT)}: {tag}")
    assert not bare, "write nonce=\"{{ csp_nonce }}\" into each inline <script>:\n" + "\n".join(bare)


def test_inline_handlers_may_only_go_down():
    found = {str(p.relative_to(ROOT)): len(HANDLER.findall(t)) for p, t in _templates() if HANDLER.search(t)}
    total = sum(found.values())
    assert total <= INLINE_HANDLERS_AT_MOST, (
        f"{total} inline on...= handlers, {INLINE_HANDLERS_AT_MOST} allowed: attach new ones with addEventListener "
        f"in a nonced script instead ({found})")


def _policy(got) -> str:
    """The script policy a page was sent: report-only beside PAGE_CSP, or enforced in its one policy (phase 2)."""
    enforced = got.headers.get("content-security-policy", "")
    return got.headers.get("content-security-policy-report-only", "") or (enforced if "'nonce-" in enforced else "")


@pytest.fixture
def report_only(monkeypatch):
    """Phase 1's behaviour, also when the whole run is enforced (ARCADE_ENFORCE_SCRIPT_POLICY=1)."""
    monkeypatch.delenv(SCRIPT_POLICY_ENV, raising=False)


def test_every_page_s_inline_scripts_carry_the_nonce_its_header_names(client):
    app, _ = client
    seen, wrong = [], []
    for path, got in _html_pages(app):
        policy = _policy(got)
        if not policy:
            continue
        seen.append(path)
        nonce = re.search(r"'nonce-([A-Za-z0-9_-]+)'", policy).group(1)
        for tag in SCRIPT_TAG.findall(got.text):
            if "src=" not in tag and f'nonce="{nonce}"' not in tag and path not in UNNONCED_PAGES:
                wrong.append(f"{path}: {tag}")
    assert {"/", "/lite", "/join", "/terms", "/offline"} <= set(seen), seen
    assert not wrong, "inline scripts a page's policy would stop:\n" + "\n".join(wrong)


def test_the_script_policy_is_report_only_beside_the_page_policy_and_new_each_request(client, report_only):
    app, _ = client
    first, second = (app.get("/join", headers=LOCAL) for _ in range(2))
    assert first.headers["content-security-policy"] == PAGE_CSP, "the enforced policy is unchanged"
    policy = _policy(first)
    assert policy.startswith("script-src 'self' 'nonce-") and "report-uri /csp-report" in policy, policy
    assert "object-src 'none'" in policy and "base-uri 'self'" in policy
    assert policy != _policy(second), "a nonce is never reused"
    nonce = re.search(r"'nonce-([^']+)'", policy).group(1)
    assert len(nonce) >= 16 and f'nonce="{nonce}"' in first.text


def test_a_page_with_its_own_policy_gets_no_script_policy_beside_it(client):
    """The offline recovery page holds its own hash policy; answers that are not pages get none."""
    app, _ = client
    assert not _policy(app.get("/recover", headers=LOCAL))
    assert not _policy(app.get("/coins.js", headers=LOCAL))


def _report(page="https://wallet.example/me?v=secret-ticket", blocked="inline", rule="script-src-elem"):
    return json.dumps({"csp-report": {"document-uri": page, "effective-directive": rule, "blocked-uri": blocked,
                                      "violated-directive": rule, "source-file": page, "line-number": 12,
                                      "original-policy": "x" * 500}}).encode()


def test_a_report_is_summarised_without_its_query_string():
    said = CspReports.summary(_report(blocked="https://evil.example/x.js?token=abc"))
    assert said == ("/me", "script-src-elem", "https://evil.example/x.js", "https://wallet.example/me:12"), said
    assert "secret" not in repr(said) and "token" not in repr(said)
    api = json.dumps([{"type": "csp-violation", "body": {"documentURL": "https://a/b?q", "effectiveDirective":
                                                         "script-src-attr", "blockedURL": "inline"}}]).encode()
    assert CspReports.summary(api) == ("/b", "script-src-attr", "inline", ""), CspReports.summary(api)
    assert CspReports.summary(b"not json") is None and CspReports.summary(b"{}") is None
    forged = CspReports.summary(_report(page="https://a/x\nWARNING forged line", blocked="inline\r\nx"))
    assert not any(c in "".join(forged) for c in "\r\n "), forged


def test_reports_are_logged_once_per_kind_and_rate_limited(caplog):
    reports = CspReports()
    with caplog.at_level("WARNING", logger="arcade.web.app"):
        assert reports.note(_report(), now=60) and not reports.note(_report(), now=61), "one line per kind"
        logged = sum(reports.note(_report(page=f"https://a/p{i}"), now=62) for i in range(100))
    assert logged == CspReports.PER_MINUTE - 1 and reports.dropped == 100 - logged
    assert caplog.text.count("would stop") == CspReports.PER_MINUTE and "secret" not in caplog.text
    assert reports.note(_report(page="https://a/next"), now=125), "a new minute logs again"
    enforced = json.loads(_report(page="https://a/enforced"))
    enforced["csp-report"]["disposition"] = "enforce"
    with caplog.at_level("WARNING", logger="arcade.web.app"):
        assert reports.note(json.dumps(enforced).encode(), now=125)
    assert "script policy stopped: page /enforced" in caplog.text, "an enforced policy's report says it stopped it"
    reports.kinds = {("p", str(i), "", ""): 1 for i in range(CspReports.KINDS)}
    assert not reports.note(_report(page="https://a/full"), now=126) and len(reports.kinds) == CspReports.KINDS


def test_the_report_endpoint_is_public_small_and_says_nothing_back(client):
    app, _ = client
    assert door.public_path("/csp-report", "POST"), "every visitor's browser sends them"
    sent = app.post("/csp-report", content=_report(), headers={**LOCAL, "content-type": "application/csp-report"})
    assert sent.status_code == 204 and not sent.content
    big = app.post("/csp-report", content=b"{" + b" " * (CspReports.MOST_BYTES + 10) + b"}",
                   headers={**LOCAL, "content-type": "application/csp-report"})
    assert big.status_code == 413
    public = app.post("/csp-report", content=_report(), headers={"host": "node.example.com", "cf-ray": "abc",
                                                                 "content-type": "application/csp-report"})
    assert public.status_code == 204, public.text


# --- phase 2: enforced (2026-11-09, security review) -----------------------------------------------------------------

def _nonce(policy: str) -> str:
    return re.search(r"'nonce-([A-Za-z0-9_-]+)'", policy).group(1)


def test_switched_on_the_policy_is_one_enforced_header_still_reported(client, report_only):
    app, state = client
    state.set_setting(SCRIPT_POLICY_SETTING, True)
    got = app.get("/join", headers=LOCAL)
    policies = [v for k, v in got.headers.items() if k.lower() == "content-security-policy"]
    assert len(policies) == 1, policies
    nonce = _nonce(policies[0])
    assert policies[0] == enforced_page_policy(nonce) == (
        f"frame-ancestors 'self'; object-src 'none'; base-uri 'self'; script-src 'self' 'nonce-{nonce}'; "
        f"worker-src 'self' blob:; report-uri /csp-report"), policies[0]
    assert "content-security-policy-report-only" not in got.headers, "enforced instead of reported, not beside it"
    assert f'nonce="{nonce}"' in got.text
    for path in ("/lite", "/", "/terms", "/offline"):          # drawn outside render() too
        assert app.get(path, headers=LOCAL, follow_redirects=False).headers[
            "content-security-policy"].startswith(PAGE_CSP + "; script-src 'self' 'nonce-"), path
    state.set_setting(SCRIPT_POLICY_SETTING, False)
    off = app.get("/join", headers=LOCAL)
    assert off.headers["content-security-policy"] == PAGE_CSP and _policy(off).startswith("script-src"), "phase 1 again"


def test_the_environment_turns_it_on_for_a_whole_run(client, monkeypatch):
    app, state = client
    monkeypatch.setenv(SCRIPT_POLICY_ENV, "1")
    assert not state.setting(SCRIPT_POLICY_SETTING)
    got = app.get("/join", headers=LOCAL)
    assert got.headers["content-security-policy"] == enforced_page_policy(_nonce(got.headers["content-security-policy"]))
    monkeypatch.setenv(SCRIPT_POLICY_ENV, "0")
    assert app.get("/join", headers=LOCAL).headers["content-security-policy"] == PAGE_CSP


def test_the_command_line_flag_sets_the_environment(monkeypatch):
    """`arcade-web --enforce-script-policy`: the service's way to hold it on (the supervisor's worker parses it too)."""
    from arcade.web import __main__ as cli

    class Applied(Exception):
        pass

    def stop(**_):                                     # the first step after the flags are applied
        raise Applied

    monkeypatch.setenv(SCRIPT_POLICY_ENV, "0")         # recorded, so the run's own value comes back afterwards
    monkeypatch.setenv("ARCADE_NO_SUPERVISOR", "1")
    monkeypatch.setattr(cli.logging, "basicConfig", stop)
    with pytest.raises(Applied):
        cli.main(["--enforce-script-policy"])
    assert webapp.script_policy_forced()


def test_pages_with_their_own_policy_keep_it_when_enforced(client, monkeypatch):
    app, _ = client
    monkeypatch.setenv(SCRIPT_POLICY_ENV, "1")
    recover = app.get("/recover", headers=LOCAL)
    policies = [v for k, v in recover.headers.items() if k.lower() == "content-security-policy"]
    assert len(policies) == 1 and "frame-ancestors 'none'" in policies[0] and "'nonce-" not in policies[0], policies
    assert "content-security-policy" not in app.get("/coins.js", headers=LOCAL).headers


def _admin(app):
    return app.get("/admin", headers=LOCAL).text


def test_the_switch_is_in_admin_safety_and_persists(client, report_only):
    app, state = client
    page = _admin(app)
    safety = page[page.index('data-tab="safety"'):page.index('data-tab="network" aria-label')]
    box = re.search(r'<input type="checkbox" id="script_policy_enforce"[^>]*>', safety)
    assert box and "checked" not in box.group(0) and "Enforce the script policy" in safety
    said = app.post("/admin/api/settings", headers={**LOCAL, "x-arcade-admin": "1"},
                    json={SCRIPT_POLICY_SETTING: True})
    assert said.status_code == 200, said.text
    assert state.setting(SCRIPT_POLICY_SETTING) is True
    assert re.search(r'id="script_policy_enforce"\s+checked', _admin(app)), "drawn on after a reload"
    assert app.get("/admin/api/state", headers=LOCAL).json()["script_policy"] == {"enforce": True, "forced": False}
    assert "'nonce-" in app.get("/join", headers=LOCAL).headers["content-security-policy"], "and in force at once"
    app.post("/admin/api/settings", headers={**LOCAL, "x-arcade-admin": "1"}, json={SCRIPT_POLICY_SETTING: False})
    assert state.setting(SCRIPT_POLICY_SETTING) is False
    assert app.get("/join", headers=LOCAL).headers["content-security-policy"] == PAGE_CSP


def test_the_switch_says_when_the_service_holds_it_on(client, monkeypatch):
    app, _ = client
    monkeypatch.setenv(SCRIPT_POLICY_ENV, "1")
    box = re.search(r'<input type="checkbox" id="script_policy_enforce"[^>]*>', _admin(app)).group(0)
    assert "checked" in box and "disabled" in box
    assert app.get("/admin/api/state", headers=LOCAL).json()["script_policy"]["forced"] is True

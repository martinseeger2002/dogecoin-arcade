"""The script policy's report endpoint (2026-11-09, security review): what a browser says the report-only script
policy would have stopped arrives at POST /csp-report, public, read short, summarised and rate limited."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_safe_headers import LOCAL                              # noqa: E402

from arcade.web import door                                      # noqa: E402
from arcade.web.app import CspReports                            # noqa: E402


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

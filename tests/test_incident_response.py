import importlib.util
import json
import sys
from pathlib import Path

import pytest

RESPONDER_PATH = Path(__file__).parent.parent / "incident-response" / "responder.py"
spec = importlib.util.spec_from_file_location("responder", RESPONDER_PATH)
responder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(responder)

ROUTE = "/api/orders/{order_id}"
TRACE_ID = "70e56134748f21019ead332f6287381f"


def alert_payload(status="firing", starts_at="2026-09-30T19:20:00Z"):
    return {
        "status": status,
        "alerts": [{
            "status": status,
            "fingerprint": "abc123",
            "startsAt": starts_at,
            "labels": {"alertname": "Order lookup 5xx responses", "route": ROUTE},
            "annotations": {
                "endpoint": f"GET {ROUTE}",
                "evaluation_window": "5m (rule evaluated every 1m)",
                "dashboard_url": "http://127.0.0.1:3000/d/order-lookups/order-lookups",
            },
        }],
    }


def fake_backends(url, params=None, timeout=10):
    if url.endswith("/loki/api/v1/query_range"):
        return {"data": {"result": [{
            "stream": {
                "order_id": "express-1002",
                "http_response_status_code": "500",
                "exception_type": "ValueError",
                "exception_message": "day is out of range for month",
                "exception_stacktrace": "Traceback ...\nValueError: day is out of range for month",
                "trace_id": TRACE_ID,
                "duration_ms": "4.2",
            },
            "values": [["1790796000000000000", "Order lookup"]],
        }]}}
    if url.endswith("/api/v1/query"):
        return {"data": {"result": [{
            "metric": {"http_response_status_code": "500", "error_type": "ValueError"},
            "value": [0, "3"],
        }]}}
    if url.endswith("/api/v1/query_range"):
        return {"data": {"result": [{"metric": {}, "values": [[1790796000, "3"]]}]}}
    if url.endswith("/api/search"):
        return {"traces": []}
    if url.endswith(f"/api/v2/traces/{TRACE_ID}"):
        return {"trace": {"resourceSpans": [{"scopeSpans": [{"spans": [{
            "name": f"GET {ROUTE}",
            "startTimeUnixNano": "1000000",
            "endTimeUnixNano": "5000000",
            "status": {"code": "STATUS_CODE_ERROR"},
            "attributes": [{"key": "order.id", "value": {"stringValue": "express-1002"}}],
        }]}]}]}}
    raise AssertionError(f"unexpected URL {url}")


@pytest.fixture
def incidents(tmp_path, monkeypatch):
    monkeypatch.setattr(responder, "INCIDENTS_DIR", tmp_path)
    return tmp_path


def test_firing_alert_is_queued_once(incidents):
    service = responder.Responder(investigate=lambda *args: None)

    status, body = service.accept(alert_payload())
    assert status == 202
    incident_dir = incidents / body["incident"]
    assert json.loads((incident_dir / "alert.json").read_text())["status"] == "firing"
    assert service.jobs.qsize() == 1

    # Grafana repeats firing notifications; the same alert instance is investigated once.
    assert service.accept(alert_payload())[0] == 200
    assert service.jobs.qsize() == 1
    # A new firing episode (new startsAt) is a new incident.
    assert service.accept(alert_payload(starts_at="2026-09-30T21:00:00Z"))[0] == 202


def test_resolved_notification_is_ignored(incidents):
    service = responder.Responder(investigate=lambda *args: None)
    status, _ = service.accept(alert_payload(status="resolved"))
    assert status == 200
    assert service.jobs.qsize() == 0
    assert list(incidents.iterdir()) == []


def test_collect_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(responder, "get_json", fake_backends)

    evidence = responder.collect_evidence(tmp_path, alert_payload())

    assert f"Affected endpoint: GET {ROUTE}" in evidence
    assert "Evaluation window: 5m" in evidence
    assert "ValueError: day is out of range for month (1 occurrence(s))" in evidence
    assert "`express-1002`" in evidence
    assert f"Trace `{TRACE_ID}`" in evidence
    assert "Collection errors" not in evidence
    for name in ("evidence.md", "metrics.json", "logs.json", f"traces/{TRACE_ID}.json"):
        assert (tmp_path / name).exists()


def test_collect_evidence_survives_backend_outage(tmp_path, monkeypatch):
    def unavailable(url, params=None, timeout=10):
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(responder, "get_json", unavailable)
    evidence = responder.collect_evidence(tmp_path, alert_payload())
    assert "Prometheus: ConnectionRefusedError" in evidence
    assert "Loki: ConnectionRefusedError" in evidence
    assert "Tempo: ConnectionRefusedError" in evidence
    assert evidence.count(responder.UNAVAILABLE) == 3


def test_claude_command_is_read_only(monkeypatch):
    monkeypatch.setattr(responder.shutil, "which", lambda name: "/usr/bin/claude")
    command = responder.claude_command()
    assert command[:2] == ["/usr/bin/claude", "-p"]
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    assert "--restricted" in command
    assert command[command.index("--tools") + 1] == "Read,Grep,Glob"
    assert command[command.index("--allowedTools") + 1] == "Read,Grep,Glob"


def test_run_claude_saves_response(tmp_path, monkeypatch):
    # Stand-in for `claude -p --output-format json` that echoes the prompt length.
    fake_claude = tmp_path / "fake_claude.py"
    fake_claude.write_text(
        "import json, sys\n"
        "prompt = sys.stdin.read()\n"
        "print(json.dumps({'result': f'## Summary\\nread {len(prompt)} chars',"
        " 'session_id': 'sess-1', 'total_cost_usd': 0.01, 'is_error': False}))\n"
    )
    monkeypatch.setattr(responder, "REPO_ROOT", tmp_path)
    incident_dir = tmp_path / "incident"
    incident_dir.mkdir()

    prompt = responder.build_prompt(incident_dir, "evidence with \"quotes\" & | pipes")
    result = responder.run_claude(incident_dir, prompt, [sys.executable, str(fake_claude)])

    assert result == {"exit_code": 0, "is_error": False, "session_id": "sess-1", "cost_usd": 0.01}
    response = (incident_dir / "response.md").read_text()
    assert f"read {len(prompt)} chars" in response
    assert "session_id: sess-1" in response
    assert "<evidence>\nevidence with \"quotes\" & | pipes\n</evidence>" in prompt
    assert "`incident/`" in prompt


def test_run_claude_drops_parent_session_variables(tmp_path, monkeypatch):
    fake_claude = tmp_path / "fake_claude.py"
    fake_claude.write_text(
        "import json, os, sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'result': json.dumps(sorted(os.environ))}))\n"
    )
    for name in responder.PARENT_SESSION_VARS:
        monkeypatch.setenv(name, "from-parent-session")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setattr(responder, "REPO_ROOT", tmp_path)

    responder.run_claude(tmp_path, "prompt", [sys.executable, str(fake_claude)])

    output = json.loads((tmp_path / "claude_output.json").read_text())
    child_env = {name.upper() for name in json.loads(output["result"])}
    assert child_env.isdisjoint(responder.PARENT_SESSION_VARS)
    assert "CLAUDE_CODE_USE_BEDROCK" in child_env

import importlib.util
import json
import subprocess
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
    assert f"`{incident_dir.as_posix()}/`" in prompt
    assert responder.READ_ONLY_CAPABILITIES in prompt


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


def test_remediation_command_allows_edits_only_under_app_and_tests(monkeypatch):
    monkeypatch.setattr(responder.shutil, "which", lambda name: "/usr/bin/claude")
    command = responder.claude_command(
        remediate=True, budget_usd=1.5, resume="sess-1", add_dirs=["/incident"]
    )
    tools = command[command.index("--tools") + 1]
    allowed = command[command.index("--allowedTools") + 1]
    assert tools == "Read,Grep,Glob,Edit,Write"
    assert allowed == "Read,Grep,Glob,Edit(app/**),Edit(tests/**)"
    assert "Bash" not in tools + allowed and "PowerShell" not in tools + allowed
    assert "--restricted" in command and "--strict-mcp-config" in command
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    assert command[command.index("--max-budget-usd") + 1] == "1.50"
    assert command[command.index("--resume") + 1] == "sess-1"
    assert command[command.index("--add-dir") + 1] == "/incident"


def test_outside_editable():
    assert responder.outside_editable(
        ["app/main.py", "tests/test_api.py", "compose.yaml", "Dockerfile", "appx/a.py", "app"]
    ) == ["compose.yaml", "Dockerfile", "appx/a.py", "app"]


# --- Remediation flow against a real temporary git repository ---------------------------


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def remediation(tmp_path, monkeypatch):
    """A temp repo, plus fakes for Claude, tests, deploy, verify, and rollback."""
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "app" / "main.py").write_text("BUG = True\n")
    (repo / "tests" / "test_api.py").write_text("def test_api(): pass\n")
    (repo / "compose.yaml").write_text("services: {}\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")

    incident_dir = tmp_path / "incidents" / "20260930T000000Z-order-lookup-5xx"
    incident_dir.mkdir(parents=True)
    responder.write_json(incident_dir / "logs.json", {"errors": {"entries": [
        {"order_id": "express-1002"}, {"order_id": "express-1002"},
    ]}})

    monkeypatch.setattr(responder, "REPO_ROOT", repo)
    monkeypatch.setattr(responder, "WORKTREES_DIR", tmp_path / "worktrees")
    monkeypatch.setattr(responder, "CLAUDE_MAX_BUDGET_USD", 2.0)
    monkeypatch.setattr(responder.shutil, "which", lambda name: "/usr/bin/claude")

    harness = type("Harness", (), {})()
    harness.repo, harness.incident_dir = repo, incident_dir
    harness.calls = calls = {"claude": [], "tests": 0, "deploy": 0, "verify": [], "rollback": []}
    harness.edits = edits = []  # one callable per Claude round: edit(worktree)
    harness.test_results = test_results = [(True, "1 passed")]
    harness.verify_result = verify_result = [(True, [{"status": 200}])]

    def fake_run_claude(incident_dir, prompt, command=None, cwd=None, suffix=""):
        calls["claude"].append({"prompt": prompt, "command": command, "cwd": cwd})
        edits[len(calls["claude"]) - 1](Path(cwd))
        return {"exit_code": 0, "is_error": False, "session_id": "sess-1", "cost_usd": 0.4}

    def fake_run_tests(worktree):
        calls["tests"] += 1
        return test_results[calls["tests"] - 1]

    def fake_deploy(worktree):
        calls["deploy"] += 1
        calls["deployed_code"] = (worktree / "app" / "main.py").read_text()
        return True, "sha256:previous", "deployed"

    def fake_verify(order_ids):
        calls["verify"].append(order_ids)
        return verify_result[0]

    def fake_rollback(worktree, previous_image):
        calls["rollback"].append(previous_image)
        return True, "rolled back"

    monkeypatch.setattr(responder, "run_claude", fake_run_claude)
    monkeypatch.setattr(responder, "run_tests", fake_run_tests)
    monkeypatch.setattr(responder, "deploy", fake_deploy)
    monkeypatch.setattr(responder, "verify", fake_verify)
    monkeypatch.setattr(responder, "rollback", fake_rollback)
    harness.run = lambda: responder.Responder()._remediate(
        incident_dir, alert_payload(), "evidence"
    )
    return harness


def fix_bug(worktree):
    (worktree / "app" / "main.py").write_text("BUG = False\n")
    (worktree / "tests" / "test_regression.py").write_text("def test_fixed(): pass\n")


def test_remediation_commits_deploys_and_verifies(remediation):
    remediation.edits.append(fix_bug)

    state, details = remediation.run()

    assert state == "fixed"
    branch = "incident/20260930T000000Z-order-lookup-5xx"
    assert details["branch"] == branch
    assert git(remediation.repo, "rev-parse", branch) == details["commit"]
    assert git(remediation.repo, "show", f"{branch}:app/main.py") == "BUG = False"
    assert "Co-Authored-By: Claude" in git(remediation.repo, "log", "-1", "--format=%B", branch)
    # The main tree and branch are untouched; the deployed code is the fix.
    assert (remediation.repo / "app" / "main.py").read_text() == "BUG = True\n"
    assert git(remediation.repo, "rev-parse", "main") != details["commit"]
    assert remediation.calls["deployed_code"] == "BUG = False\n"
    assert remediation.calls["verify"] == [["express-1002"]]
    assert remediation.calls["rollback"] == []
    # Claude ran in the worktree with the edit allowlist and could read the evidence.
    first = remediation.calls["claude"][0]
    assert first["cwd"].name == "20260930T000000Z-order-lookup-5xx"
    assert "Edit(app/**)" in first["command"][first["command"].index("--allowedTools") + 1]
    assert first["command"][first["command"].index("--add-dir") + 1] == str(
        remediation.incident_dir
    )
    assert responder.REMEDIATE_CAPABILITIES in first["prompt"]
    report = json.loads((remediation.incident_dir / "remediation.json").read_text())
    assert report["changed_files"] == ["app/main.py", "tests/test_regression.py"]
    assert not Path(report["worktree"]).exists()


def test_failed_tests_go_back_to_the_same_session(remediation):
    remediation.edits += [
        lambda wt: (wt / "app" / "main.py").write_text("BUG = 'half fixed'\n"),
        fix_bug,
    ]
    remediation.test_results[:] = [(False, "FAILED tests/test_api.py::test_x"), (True, "ok")]

    state, details = remediation.run()

    assert state == "fixed"
    assert details["rounds"] == 2 and details["cost_usd"] == 0.8
    second = remediation.calls["claude"][1]
    assert "FAILED tests/test_api.py::test_x" in second["prompt"]
    assert second["command"][second["command"].index("--resume") + 1] == "sess-1"
    # The second round only gets what is left of the per-incident budget.
    assert second["command"][second["command"].index("--max-budget-usd") + 1] == "1.60"


def test_changes_outside_app_and_tests_are_rejected(remediation):
    def edit_compose(worktree):
        fix_bug(worktree)
        (worktree / "compose.yaml").write_text("services: {evil: {}}\n")

    remediation.edits.append(edit_compose)

    state, details = remediation.run()

    assert state == "fix_rejected"
    assert remediation.calls["tests"] == 0 and remediation.calls["deploy"] == 0
    assert details["commit"] is None
    report = json.loads((remediation.incident_dir / "remediation.json").read_text())
    assert report["rejected_files"] == ["compose.yaml"]


def test_no_changes_means_no_fix(remediation):
    remediation.edits.append(lambda worktree: None)

    state, details = remediation.run()

    assert state == "no_fix"
    assert remediation.calls["tests"] == 0 and remediation.calls["deploy"] == 0
    assert details["branch"] is None
    assert "incident/" not in git(remediation.repo, "branch", "--list")


def test_failed_verification_rolls_back(remediation):
    remediation.edits.append(fix_bug)
    remediation.verify_result[0] = (False, [{"status": 500}])

    state, _ = remediation.run()

    assert state == "rolled_back"
    assert remediation.calls["rollback"] == ["sha256:previous"]


def test_tests_that_keep_failing_are_not_deployed(remediation):
    remediation.edits += [fix_bug, fix_bug]
    remediation.test_results[:] = [(False, "FAILED"), (False, "FAILED again")]

    state, details = remediation.run()

    assert state == "tests_failed"
    assert remediation.calls["deploy"] == 0 and details["commit"] is None

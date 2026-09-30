"""Grafana alert webhook -> evidence bundle -> headless Claude Code investigation.

Listens on 127.0.0.1:8001 for Grafana webhook notifications at POST /alerts. For each new
firing alert it saves the alert, metrics (Prometheus), logs (Loki), and traces (Tempo) to
incidents/<id>/, then runs `claude -p` in the repository with that evidence and saves the
response to incidents/<id>/response.md.

Uses only the standard library. Run it on the host (not in Docker) so it can start the
local `claude` CLI with your login: `uv run python incident-response/responder.py`.
"""

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import urlopen

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
INCIDENTS_DIR = Path(os.getenv("INCIDENTS_DIR", HERE / "incidents"))
PROMPT_TEMPLATE = HERE / "prompt.md"

HOST = os.getenv("RESPONDER_HOST", "127.0.0.1")
PORT = int(os.getenv("RESPONDER_PORT", "8001"))
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://127.0.0.1:9090")
LOKI_URL = os.getenv("LOKI_URL", "http://127.0.0.1:3100")
TEMPO_URL = os.getenv("TEMPO_URL", "http://127.0.0.1:3200")

SERVICE_NAME = "order-tracker"
DEFAULT_ROUTE = "/api/orders/{order_id}"
REQUEST_METRIC = "http_server_request_duration_seconds"
LOOKBACK = timedelta(minutes=int(os.getenv("EVIDENCE_LOOKBACK_MINUTES", "30")))
MAX_TRACES = 5
MAX_BODY_BYTES = 1_000_000
MAX_EVIDENCE_CHARS = 60_000

CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL")
CLAUDE_MAX_BUDGET_USD = os.getenv("CLAUDE_MAX_BUDGET_USD", "2")
CLAUDE_TIMEOUT_SECONDS = int(os.getenv("CLAUDE_TIMEOUT_SECONDS", "900"))
# Read-only: the evidence contains user-controlled strings (e.g. order IDs from URLs),
# so the headless session may read the repo but not run commands, edit, or fetch.
# `--tools` removes every other built-in tool; `--allowedTools` alone would not, because
# allow rules in user/project settings still apply.
CLAUDE_TOOLS = "Read,Grep,Glob"
# Set by a Claude Code session for the processes it starts. If the responder itself was
# started from Claude Code, drop them so each investigation is a standalone session, as
# when started from a terminal. Auth/config variables (e.g. CLAUDE_CODE_USE_BEDROCK,
# ANTHROPIC_API_KEY) pass through.
PARENT_SESSION_VARS = {
    "CLAUDECODE",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_EFFORT",
    "CLAUDE_PID",
}

log = logging.getLogger("incident-responder")


# --- Backend queries -------------------------------------------------------------------


def get_json(url, params=None, timeout=10):
    if params:
        url = f"{url}?{urlencode(params)}"
    with urlopen(url, timeout=timeout) as response:
        return json.load(response)


def quoted(value):
    """A double-quoted string literal, valid in PromQL, LogQL, and TraceQL."""
    return json.dumps(value)


def request_selector(route, status_regex=None):
    matchers = [
        f'job={quoted(SERVICE_NAME)}',
        'http_request_method="GET"',
        f"http_route={quoted(route)}",
    ]
    if status_regex:
        matchers.append(f"http_response_status_code=~{quoted(status_regex)}")
    return "{" + ", ".join(matchers) + "}"


def collect_metrics(route, start, end):
    window = f"{max(int((end - start).total_seconds()), 60)}s"
    queries = {
        "requests_by_status_and_error": (
            "instant",
            f"sum by (http_response_status_code, error_type) "
            f"(increase({REQUEST_METRIC}_count{request_selector(route)}[{window}]))",
        ),
        "server_errors_per_minute": (
            "range",
            f"sum(increase({REQUEST_METRIC}_count{request_selector(route, '5..')}[1m]))",
        ),
        "requests_per_minute": (
            "range",
            f"sum(increase({REQUEST_METRIC}_count{request_selector(route)}[1m]))",
        ),
        "p95_latency_seconds": (
            "range",
            f"histogram_quantile(0.95, sum by (le) "
            f"(rate({REQUEST_METRIC}_bucket{request_selector(route)}[5m])))",
        ),
    }
    results = {}
    for name, (kind, expr) in queries.items():
        if kind == "instant":
            params = {"query": expr, "time": end.timestamp()}
            url = f"{PROMETHEUS_URL}/api/v1/query"
        else:
            params = {"query": expr, "start": start.timestamp(), "end": end.timestamp(), "step": 60}
            url = f"{PROMETHEUS_URL}/api/v1/query_range"
        results[name] = {"query": expr, "result": get_json(url, params)["data"]["result"]}
    return results


def collect_logs(route, start, end):
    base = f"{{service_name={quoted(SERVICE_NAME)}}} | http_route={quoted(route)}"
    queries = {
        "errors": f'{base} | severity_text="ERROR"',
        "recent_lookups": base,
    }
    results = {}
    for name, expr in queries.items():
        data = get_json(f"{LOKI_URL}/loki/api/v1/query_range", {
            "query": expr,
            "start": int(start.timestamp() * 1e9),
            "end": int(end.timestamp() * 1e9),
            "limit": 100 if name == "errors" else 30,
            "direction": "backward",
        })["data"]["result"]
        entries = [
            {"timestamp": ts, "line": line, **stream["stream"]}
            for stream in data
            for ts, line in stream["values"]
        ]
        entries.sort(key=lambda entry: entry["timestamp"], reverse=True)
        results[name] = {"query": expr, "entries": entries}
    return results


def collect_traces(route, start, end, trace_ids):
    """Fetch traces referenced by error logs, topped up with a TraceQL search for errors."""
    ids = list(dict.fromkeys(trace_ids))[:MAX_TRACES]
    traceql = (
        f"{{ resource.service.name = {quoted(SERVICE_NAME)} && "
        f"span.http.route = {quoted(route)} && status = error }}"
    )
    if len(ids) < MAX_TRACES:
        found = get_json(f"{TEMPO_URL}/api/search", {
            "q": traceql,
            "start": int(start.timestamp()),
            "end": int(end.timestamp()) + 1,
            "limit": MAX_TRACES,
        }).get("traces", [])
        for trace in found:
            if trace["traceID"] not in ids and len(ids) < MAX_TRACES:
                ids.append(trace["traceID"])
    traces = {}
    for trace_id in ids:
        try:
            traces[trace_id] = get_json(f"{TEMPO_URL}/api/v2/traces/{quote(trace_id)}")
        except HTTPError as exc:
            traces[trace_id] = {"error": f"HTTP {exc.code}"}
    return {"search_query": traceql, "traces": traces}


# --- Evidence ----------------------------------------------------------------------------


def parse_time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    # Grafana sends 0001-01-01T00:00:00Z for "not set".
    return parsed if parsed.year > 1 else None


def attribute_values(attributes):
    values = {}
    for attribute in attributes or []:
        value = attribute.get("value", {})
        values[attribute["key"]] = next(iter(value.values()), None) if value else None
    return values


def summarize_trace(trace):
    spans = []
    for resource_spans in trace.get("trace", {}).get("resourceSpans", []):
        for scope_spans in resource_spans.get("scopeSpans", []):
            for span in scope_spans.get("spans", []):
                duration_ms = (
                    int(span.get("endTimeUnixNano", 0)) - int(span.get("startTimeUnixNano", 0))
                ) / 1e6
                spans.append({
                    "name": span.get("name"),
                    "duration_ms": round(duration_ms, 2),
                    "status": span.get("status", {}),
                    "attributes": attribute_values(span.get("attributes")),
                    "events": [
                        {"name": event.get("name"), **attribute_values(event.get("attributes"))}
                        for event in span.get("events", [])
                    ],
                })
    return spans


UNAVAILABLE = "Unavailable (see Collection errors)."


def fence(text):
    return f"```\n{text.rstrip()}\n```"


def render_evidence(alert, route, start, end, metrics, logs, traces, errors):
    labels, annotations = alert.get("labels", {}), alert.get("annotations", {})
    lines = [
        f"# Incident evidence: {labels.get('alertname', 'unknown alert')}",
        "",
        "## Alert",
        "",
        f"- Status: {alert.get('status')}",
        f"- Started: {alert.get('startsAt')}",
        f"- Affected endpoint: {annotations.get('endpoint', f'GET {route}')}",
        f"- Evaluation window: {annotations.get('evaluation_window', 'unknown')}",
        f"- Summary: {annotations.get('summary', '')}",
        f"- Description: {annotations.get('description', '')}",
        f"- Value: {alert.get('valueString', '')}",
        f"- Dashboard: {annotations.get('dashboard_url') or alert.get('dashboardURL', '')}",
        f"- Alert rule: {alert.get('generatorURL', '')}",
        f"- Labels: {json.dumps(labels, sort_keys=True)}",
        "",
        f"Evidence window: {start.isoformat()} to {end.isoformat()} (UTC).",
        "",
    ]

    if errors:
        lines += ["## Collection errors", ""]
        lines += [f"- {error}" for error in errors]
        lines.append("")

    lines += ["## Metrics (Prometheus)", ""]
    if metrics:
        lines += ["Requests in the evidence window, by status code and error type:", ""]
        lines += ["| Status | Error type | Requests |", "| --- | --- | --- |"]
        for series in metrics["requests_by_status_and_error"]["result"]:
            metric = series["metric"]
            lines.append(
                f"| {metric.get('http_response_status_code', '?')} "
                f"| {metric.get('error_type', '')} | {float(series['value'][1]):.0f} |"
            )
        lines += ["", "Minutes with 5xx responses (UTC):", ""]
        points = [
            (datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M"), float(value))
            for series in metrics["server_errors_per_minute"]["result"]
            for ts, value in series["values"]
            if float(value) > 0
        ]
        lines += [f"- {minute}: ~{value:.0f}" for minute, value in points] or ["- none"]
        lines += ["", "Queries and full series are in `metrics.json`.", ""]
    else:
        lines += [UNAVAILABLE, ""]

    lines += ["## Logs (Loki)", ""]
    if logs:
        error_entries = logs["errors"]["entries"]
        lines.append(f"{len(error_entries)} ERROR log record(s) for this endpoint in the window.")
        signatures = {}
        for entry in error_entries:
            key = (entry.get("exception_type", ""), entry.get("exception_message", ""))
            signatures.setdefault(key, []).append(entry)
        for (exc_type, exc_message), entries in signatures.items():
            sample = entries[0]
            lines += [
                "",
                f"### {exc_type or 'Error'}: {exc_message} ({len(entries)} occurrence(s))",
                "",
                "Affected order IDs: "
                + ", ".join(sorted({f"`{e.get('order_id', '?')}`" for e in entries})),
                "",
                "Most recent occurrence:",
                "",
                fence(json.dumps(
                    {k: v for k, v in sample.items() if k != "exception_stacktrace"}, indent=2
                )),
            ]
            if sample.get("exception_stacktrace"):
                lines += ["", "Stack trace:", "", fence(sample["exception_stacktrace"][-4000:])]
        lines += ["", "Recent lookups for this endpoint (all statuses, newest first):", ""]
        lines += ["| Time (UTC) | Status | Order ID | Duration (ms) |", "| --- | --- | --- | --- |"]
        for entry in logs["recent_lookups"]["entries"][:20]:
            timestamp = datetime.fromtimestamp(int(entry["timestamp"]) / 1e9, timezone.utc)
            lines.append(
                f"| {timestamp:%H:%M:%S} | {entry.get('http_response_status_code', '?')} "
                f"| `{entry.get('order_id', '?')}` "
                f"| {float(entry.get('duration_ms', 0)):.1f} |"
            )
        lines += ["", "Raw entries are in `logs.json`.", ""]
    else:
        lines += [UNAVAILABLE, ""]

    lines += ["## Traces (Tempo)", ""]
    if traces and traces["traces"]:
        for trace_id, trace in traces["traces"].items():
            lines += [f"### Trace `{trace_id}`", ""]
            if "error" in trace:
                lines += [f"Could not fetch: {trace['error']}", ""]
                continue
            lines += [fence(json.dumps(summarize_trace(trace), indent=2)), ""]
        lines += ["Full traces are in `traces/`.", ""]
    elif traces is None:
        lines += [UNAVAILABLE, ""]
    else:
        lines += ["No error traces found in the window.", ""]

    return "\n".join(lines)


def collect_evidence(incident_dir, payload):
    alert = next(a for a in payload["alerts"] if a.get("status") == "firing")
    route = alert.get("labels", {}).get("route") or DEFAULT_ROUTE
    end = datetime.now(timezone.utc)
    start = end - LOOKBACK
    started = parse_time(alert.get("startsAt"))
    if started and started - timedelta(minutes=10) < start:
        start = started - timedelta(minutes=10)

    errors, metrics, logs, traces = [], None, None, None
    try:
        metrics = collect_metrics(route, start, end)
        write_json(incident_dir / "metrics.json", metrics)
    except Exception as exc:
        errors.append(f"Prometheus: {exc!r}")
    try:
        logs = collect_logs(route, start, end)
        write_json(incident_dir / "logs.json", logs)
    except Exception as exc:
        errors.append(f"Loki: {exc!r}")
    try:
        trace_ids = [e["trace_id"] for e in (logs or {}).get("errors", {}).get("entries", [])
                     if e.get("trace_id")]
        traces = collect_traces(route, start, end, trace_ids)
        (incident_dir / "traces").mkdir(exist_ok=True)
        for trace_id, trace in traces["traces"].items():
            write_json(incident_dir / "traces" / f"{trace_id}.json", trace)
    except Exception as exc:
        errors.append(f"Tempo: {exc!r}")

    evidence = render_evidence(alert, route, start, end, metrics, logs, traces, errors)
    (incident_dir / "evidence.md").write_text(evidence, encoding="utf-8")
    return evidence


# --- Claude Code -------------------------------------------------------------------------


def claude_command():
    executable = shutil.which(CLAUDE_BIN)
    if not executable:
        raise FileNotFoundError(f"Claude Code CLI not found: {CLAUDE_BIN!r}")
    command = [
        executable, "-p",
        "--output-format", "json",
        # Ignores user/project/local settings (and their allow rules), keeps file tools
        # inside the repo, and refuses bypassPermissions.
        "--restricted",
        "--tools", CLAUDE_TOOLS,
        "--allowedTools", CLAUDE_TOOLS,
        "--permission-mode", "dontAsk",
        "--strict-mcp-config",
        "--max-budget-usd", CLAUDE_MAX_BUDGET_USD,
    ]
    if CLAUDE_MODEL:
        command += ["--model", CLAUDE_MODEL]
    return command


def build_prompt(incident_dir, evidence):
    if len(evidence) > MAX_EVIDENCE_CHARS:
        evidence = evidence[:MAX_EVIDENCE_CHARS] + "\n\n[evidence truncated; see files]"
    template = PROMPT_TEMPLATE.read_text(encoding="utf-8")
    return (
        template
        .replace("{{incident_dir}}", incident_dir.relative_to(REPO_ROOT).as_posix())
        .replace("{{evidence}}", evidence)
    )


def kill_tree(process):
    if os.name == "nt":
        # .cmd shims start node as a child; terminate the whole tree.
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True)
    else:
        process.kill()


def run_claude(incident_dir, prompt, command=None):
    """Run headless Claude Code with the prompt on stdin and save its response."""
    (incident_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in PARENT_SESSION_VARS}
    process = subprocess.Popen(
        command or claude_command(),
        cwd=REPO_ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        # Prompt goes over stdin: it is large and holds untrusted text, so it must not
        # pass through a Windows command line.
        stdout, stderr = process.communicate(prompt, timeout=CLAUDE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        kill_tree(process)
        stdout, stderr = process.communicate()
        stderr += f"\nTimed out after {CLAUDE_TIMEOUT_SECONDS}s."
    (incident_dir / "claude_output.json").write_text(stdout, encoding="utf-8")
    if stderr.strip():
        (incident_dir / "claude_stderr.log").write_text(stderr, encoding="utf-8")

    try:
        output = json.loads(stdout)
    except json.JSONDecodeError:
        output = {"is_error": True, "result": stdout or stderr}
    result = output.get("result") or "(no response text)"
    header = [
        "<!-- Generated by headless Claude Code. -->",
        f"<!-- session_id: {output.get('session_id', 'n/a')} | "
        f"cost_usd: {output.get('total_cost_usd', 'n/a')} | "
        f"exit_code: {process.returncode} -->",
        "",
    ]
    (incident_dir / "response.md").write_text("\n".join(header) + result + "\n", encoding="utf-8")
    return {
        "exit_code": process.returncode,
        "is_error": bool(output.get("is_error")) or process.returncode != 0,
        "session_id": output.get("session_id"),
        "cost_usd": output.get("total_cost_usd"),
    }


# --- Incidents ---------------------------------------------------------------------------


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "alert"


class Responder:
    """Accepts webhook payloads and investigates new incidents one at a time."""

    def __init__(self, investigate=None):
        self.investigate = investigate or self._investigate
        self.jobs = queue.Queue()
        self.seen = set()
        self.lock = threading.Lock()

    def accept(self, payload):
        """Returns (http_status, response_body)."""
        firing = [a for a in payload.get("alerts", []) if a.get("status") == "firing"]
        if not firing:
            return 200, {"ignored": f"no firing alerts (status={payload.get('status')})"}
        # Grafana re-sends firing alerts every repeat_interval; investigate each once.
        key = tuple(sorted(f"{a.get('fingerprint')}@{a.get('startsAt')}" for a in firing))
        with self.lock:
            if key in self.seen:
                return 200, {"ignored": "already investigating or investigated"}
            self.seen.add(key)

        name = firing[0].get("labels", {}).get("alertname", "alert")
        incident_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{slug(name)}"
        incident_dir = INCIDENTS_DIR / incident_id
        incident_dir.mkdir(parents=True, exist_ok=True)
        write_json(incident_dir / "alert.json", payload)
        self.set_status(incident_dir, "queued")
        self.jobs.put((incident_dir, payload))
        return 202, {"incident": incident_id, "path": str(incident_dir)}

    def set_status(self, incident_dir, state, **details):
        write_json(incident_dir / "status.json", {
            "state": state,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            **details,
        })

    def _investigate(self, incident_dir, payload):
        self.set_status(incident_dir, "collecting_evidence")
        evidence = collect_evidence(incident_dir, payload)
        self.set_status(incident_dir, "investigating")
        started = time.monotonic()
        result = run_claude(incident_dir, build_prompt(incident_dir, evidence))
        state = "failed" if result["is_error"] else "done"
        self.set_status(incident_dir, state, duration_s=round(time.monotonic() - started), **result)
        log.info("Incident %s %s: %s", incident_dir.name, state, incident_dir / "response.md")

    def work(self):
        while True:
            incident_dir, payload = self.jobs.get()
            try:
                self.investigate(incident_dir, payload)
            except Exception as exc:
                log.exception("Incident %s failed", incident_dir.name)
                self.set_status(incident_dir, "failed", error=repr(exc))
            finally:
                self.jobs.task_done()


def make_handler(responder):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/healthz":
                self.reply(200, {"status": "ok", "queued": responder.jobs.qsize()})
            else:
                self.reply(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/alerts":
                return self.reply(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                return self.reply(413, {"error": "payload too large"})
            try:
                payload = json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                return self.reply(400, {"error": "invalid JSON"})
            if not isinstance(payload, dict):
                return self.reply(400, {"error": "expected a JSON object"})
            status, body = responder.accept(payload)
            log.info("POST /alerts status=%s -> %s %s", payload.get("status"), status, body)
            self.reply(status, body)

        def reply(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):
            pass

    return Handler


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        claude_command()
    except FileNotFoundError as exc:
        sys.exit(f"{exc}. Install Claude Code or set CLAUDE_BIN.")
    responder = Responder()
    threading.Thread(target=responder.work, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), make_handler(responder))
    log.info("Listening on http://%s:%s/alerts; incidents go to %s", HOST, PORT, INCIDENTS_DIR)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

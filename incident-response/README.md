# Incident responder

A small webhook service that turns a Grafana alert into an evidence bundle and a first
investigation by Claude Code, running headless.

```text
Grafana alert ──POST /alerts──▶ responder.py ──▶ incidents/<id>/
                                    │              alert.json, metrics.json, logs.json,
                                    │              traces/, evidence.md
                                    └──▶ claude -p (read-only) ──▶ incidents/<id>/response.md
```

## Run it

Start the Compose stack first (see the main README), then run the responder on the host.
It runs on the host, not in Docker, so it can use your local `claude` CLI and login.

```bash
uv run python incident-response/responder.py
```

It listens on `127.0.0.1:8001`. Grafana reaches it at `http://host.docker.internal:8001/alerts`
through the `incident-responder` contact point in
`observability/grafana/provisioning/alerting/incident-responder.yaml`. All alerts route
there.

## What happens on an alert

1. **Accept.** Grafana POSTs a firing notification and the responder replies `202` right away.
   - Resolved notifications are ignored.
   - Grafana repeats a firing notification every 4 hours; a repeat of the same alert is ignored.
   - Incidents are processed one at a time.
2. **Collect evidence** for the alert's `route` label over the last 30 minutes, or from
   10 minutes before the alert started if that is earlier:
   - **Prometheus:** requests by status code and error type, 5xx and total requests per
     minute, and p95 latency.
   - **Loki:** ERROR logs for the endpoint, with exception, stack trace and order ID, plus
     the 30 most recent lookups.
   - **Tempo:** up to 5 error traces. It uses the trace IDs from those logs, then a TraceQL
     search for more.

   If a backend is down, the failure is recorded under "Collection errors" and the
   investigation continues with what was collected.
3. **Investigate.** The responder runs `claude -p` from the repository root, with
   `prompt.md` and `evidence.md` on stdin, and saves:
   - `response.md`: the report.
   - `claude_output.json`: the raw result, including cost and `session_id`.
   - `status.json`: progress (`queued` → `collecting_evidence` → `investigating` →
     `done`/`failed`).

To continue an investigation interactively, run `claude --resume <session_id>` from the
repository root.

## Safety

The evidence includes strings that clients control, such as order IDs in URLs. The
headless session is limited accordingly:

- **Restricted mode with read-only tools** (`--restricted --tools Read,Grep,Glob`,
  `--permission-mode dontAsk`). It can read the repository but cannot run commands, edit
  files, or fetch URLs. `--allowedTools` alone is not enough, because allow rules in your
  user and project settings would still apply.
- **No MCP servers** (`--strict-mcp-config`).
- **Limits:** a $2 budget and a 15-minute timeout per incident.
- **Evidence goes in on stdin**, not the command line, and the prompt tells Claude to treat
  it as data.
- **The webhook binds to localhost only.** Containers on this Docker host can reach it;
  nothing else can.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `RESPONDER_HOST` / `RESPONDER_PORT` | `127.0.0.1` / `8001` | Listen address |
| `PROMETHEUS_URL`, `LOKI_URL`, `TEMPO_URL` | `http://127.0.0.1:9090`, `:3100`, `:3200` | Backends (ports published by Compose) |
| `EVIDENCE_LOOKBACK_MINUTES` | `30` | Evidence window |
| `CLAUDE_BIN` | `claude` | Claude Code CLI |
| `CLAUDE_MODEL` | CLI default | Model override |
| `CLAUDE_MAX_BUDGET_USD` | `2` | Spend cap per investigation |
| `CLAUDE_TIMEOUT_SECONDS` | `900` | Time limit per investigation |
| `INCIDENTS_DIR` | `incident-response/incidents` | Output folder (git-ignored) |

Edit `prompt.md` to change what the report covers.

## Test without waiting for an alert

Replay a saved notification:

```bash
curl -X POST -H 'Content-Type: application/json' \
  --data-binary @incident-response/incidents/<id>/alert.json http://127.0.0.1:8001/alerts
```

The responder only remembers repeats while it is running, so a replayed alert is treated
as new after a restart.

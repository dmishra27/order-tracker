# Incident responder

A small webhook service that turns a Grafana alert into an evidence bundle, an
investigation by headless Claude Code, and (by default) a tested, deployed fix.

```text
Grafana alert ──POST /alerts──▶ responder.py ──▶ incidents/<id>/  (evidence)
                                    │
                                    ├─▶ git worktree on branch incident/<id>
                                    ├─▶ claude -p: edits app/ and tests/ only, no shell
                                    ├─▶ tests in a container with no network
                                    │     (on failure: back to the same Claude session, once)
                                    ├─▶ commit on incident/<id>
                                    ├─▶ docker compose up --build app  (from the worktree)
                                    └─▶ verify the failing requests recover, else roll back
```

## Run it

Start the Compose stack first (see the main README), then run the responder on the host.
It runs on the host, not in Docker, so it can use your local `claude` CLI and login, plus
`git` and `docker`.

```bash
uv run python incident-response/responder.py
```

It listens on `127.0.0.1:8001`. Grafana reaches it at `http://host.docker.internal:8001/alerts`
through the `incident-responder` contact point in
`observability/grafana/provisioning/alerting/incident-responder.yaml`. All alerts route
there.

Set `AUTO_REMEDIATE=0` for read-only investigations (a report, no changes).

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
3. **Fix.** The responder creates a git worktree of `HEAD` at
   `incident-response/worktrees/<id>` on a new branch, `incident/<id>`, and runs `claude -p`
   there with `prompt.md` and the evidence on stdin. Claude can read the code and the raw
   evidence files, and can edit files under `app/` and `tests/` only. It writes the report
   to `response.md`.
4. **Check the change.** The responder stages what changed:
   - Nothing changed (for example a test alert or an infrastructure problem): `no_fix`. The
     worktree and branch are deleted.
   - Anything outside `app/` or `tests/` changed: `fix_rejected`. Nothing runs or deploys.
5. **Test.** The suite runs in a container built from `Dockerfile.test`, with no network and
   the worktree mounted read-only. If it fails, the output goes back to the same Claude
   session for another round, within the same budget (2 rounds by default). If it still
   fails: `tests_failed`, and nothing is committed or deployed.
6. **Commit and deploy.** The responder commits on `incident/<id>`, then rebuilds and
   restarts only the `app` service from the worktree:
   `docker compose up --build --detach --wait --no-deps app`.
7. **Verify.** It checks that `/healthz` returns 200 and that every order ID from the
   incident's error logs now returns a non-5xx status. If so: `fixed`. If not, it retags
   the previous image and restarts the app with it: `rolled_back` (or `deploy_failed` if
   the new container never became healthy).

`main` and your working tree are never touched. The fix is live, but it only reaches
`main` when you review and merge the `incident/<id>` branch. Until then, rebuilding the app
from `main` (`docker compose up --build -d app`) brings the old code back.

Each incident folder holds:

| File | Contents |
| --- | --- |
| `alert.json`, `metrics.json`, `logs.json`, `traces/`, `evidence.md` | Evidence |
| `prompt.md`, `response.md`, `claude_output.json` | Claude's input, report, and raw result (`_round2` files for a second round) |
| `tests_round<N>.log`, `deploy.log`, `rollback.log`, `commit_message.txt` | Remediation steps |
| `remediation.json` | Changed files, test results, commit, previous image, verification checks |
| `status.json` | Progress: `queued` → `collecting_evidence` → `investigating` → `testing` → `deploying` → `verifying` → final state |

To continue an investigation interactively, run `claude --resume <session_id>` from the
worktree, or from the repository root if the worktree was removed.

## Safety

The evidence includes strings that clients control, such as order IDs in URLs, so a
crafted request could try to steer the model. The design assumes that can happen:

- **No shell, web access, or MCP servers.** The session runs with `--restricted`,
  `--tools Read,Grep,Glob,Edit,Write` (or `Read,Grep,Glob` with `AUTO_REMEDIATE=0`),
  `--permission-mode dontAsk`, and `--strict-mcp-config`. Only an explicit list of tools
  exists, and anything else is denied rather than prompted for. `--allowedTools` alone is
  not enough, because allow rules in your user and project settings would still apply.
- **Edits are fenced twice.** Permission rules allow only `Edit(app/**)` and
  `Edit(tests/**)`, which also covers Write. `--restricted` confines file tools to the
  worktree and the incident folder. Then the responder itself rejects any change outside
  `app/` and `tests/`.
- **Model-written code runs only in containers.** Editing code and running tests together
  amounts to running arbitrary code, since pytest executes `tests/` and `conftest.py`. The
  test container has no network and a read-only mount. The deployed app runs in its usual
  container.
- **Privileged steps run in the responder, not the model.** git, test and docker commands
  are fixed argument lists run without a shell. Claude's output never becomes a command.
- **The fix is reviewable and reversible.** It lives on its own branch, the previous image
  is kept for rollback, and verification failures roll back automatically.
- **Limits:** a $2 budget across all rounds and a 15-minute timeout per Claude run.
- **Environment:** variables from a parent Claude Code session are removed, so each run is
  a standalone session.
- **The webhook binds to localhost only.** Containers on this Docker host can reach it;
  nothing else can.

The remaining risk is inherent to auto-deploying: code a model wrote, steered in part by
untrusted input, goes live after passing the tests. It can only change `app/` and
`tests/`, and it runs where the app already runs. Review the `incident/<id>` branch before
merging, or run with `AUTO_REMEDIATE=0` if you want a human to approve every change.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `RESPONDER_HOST` / `RESPONDER_PORT` | `127.0.0.1` / `8001` | Listen address |
| `PROMETHEUS_URL`, `LOKI_URL`, `TEMPO_URL` | `http://127.0.0.1:9090`, `:3100`, `:3200` | Backends (ports published by Compose) |
| `APP_URL` | `http://127.0.0.1:8000` | App to verify after deploying |
| `EVIDENCE_LOOKBACK_MINUTES` | `30` | Evidence window |
| `AUTO_REMEDIATE` | `1` | `0` for read-only investigations |
| `MAX_FIX_ROUNDS` | `2` | Claude rounds when tests fail |
| `CLAUDE_BIN` | `claude` | Claude Code CLI |
| `CLAUDE_MODEL` | CLI default | Model override |
| `CLAUDE_MAX_BUDGET_USD` | `2` | Spend cap per incident, across rounds |
| `CLAUDE_TIMEOUT_SECONDS` | `900` | Time limit per Claude run |
| `INCIDENTS_DIR` | `incident-response/incidents` | Output folder (git-ignored) |
| `WORKTREES_DIR` | `incident-response/worktrees` | Fix worktrees (git-ignored) |

Edit `prompt.md` to change what the report covers.

## Test without waiting for an alert

Replay a saved notification:

```bash
curl -X POST -H 'Content-Type: application/json' \
  --data-binary @incident-response/incidents/<id>/alert.json http://127.0.0.1:8001/alerts
```

The responder only remembers repeats while it is running, so a replayed alert is treated
as new after a restart. With remediation on, a replay can change and redeploy the app.

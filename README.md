# Order Tracker

A small order tracking app for the AI Dev Tools Zoomcamp observability homework. It includes a web page, API, tests, and a Docker Compose setup. You add telemetry, alerts, and an incident responder in Homework 4.

The main user flow is creating an order and checking its status. Three sample orders are created on first startup.

## Run it

You need Docker with Compose. To run the tests, you also need Python 3.11+ and `uv`.

```bash
docker compose up --build -d --wait
```

Open <http://127.0.0.1:8000>. The API is at `/api/orders`, and the health check is at `/healthz`. Data is stored in a Docker volume and survives container recreation.

If port 8000 is occupied, set `ORDER_TRACKER_PORT`, for example:

```bash
ORDER_TRACKER_PORT=18080 docker compose up --build -d --wait
```

## Observability

Compose also starts an OpenTelemetry Collector, Prometheus, Loki, Tempo, and Grafana. The app sends metrics, logs, and traces for order lookups (`GET /api/orders/{id}`) over OTLP to the Collector, which forwards them to Prometheus, Loki, and Tempo respectively.

Open Grafana at <http://127.0.0.1:3000> (no login). The home dashboard, **Order lookups**, shows request counts, 4xx and 5xx errors, the error rate, and recent failed lookups; log lines link to their traces in Tempo. Prometheus is at <http://127.0.0.1:9090>. Use `GRAFANA_PORT` and `PROMETHEUS_PORT` to change the ports.

All configuration lives in `observability/`:

| Path | Purpose |
| --- | --- |
| `otel-collector.yaml` | OTLP receiver and one pipeline per signal |
| `prometheus.yaml`, `loki.yaml`, `tempo.yaml` | Backend configuration |
| `grafana/provisioning/` | Data sources and dashboard provider |
| `grafana/dashboards/order-lookups.json` | The provisioned dashboard |

Outside Compose, the app prints telemetry to the console unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set.

## Tests

Run tests with `uv run --frozen pytest -q`. Stop the app with `docker compose down`. Add `-v` only if you also want to delete the order data.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Web page |
| GET | `/healthz` | Database health check |
| GET | `/api/orders` | List orders |
| POST | `/api/orders` | Create an order |
| GET | `/api/orders/{id}` | Check an order |
| PATCH | `/api/orders/{id}` | Change an order status |

The app uses SQLite to keep setup small. Run one app container at a time. The course exercise is about detecting and handling an incident, not scaling the database.

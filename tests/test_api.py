import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "orders.db")
    with TestClient(main.app) as test_client:
        yield test_client


def test_health_and_seeded_orders(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    orders = client.get("/api/orders").json()
    assert len(orders) == 3
    assert {order["priority"] for order in orders} == {"standard", "express"}


def test_create_and_update_order(client):
    response = client.post(
        "/api/orders",
        json={"customer": "Taylor", "item": "Mug", "priority": "standard"},
    )
    assert response.status_code == 201
    order_id = response.json()["id"]
    assert client.get(f"/api/orders/{order_id}").json()["status"] == "received"
    updated = client.patch(f"/api/orders/{order_id}", json={"status": "shipped"})
    assert updated.status_code == 200
    assert updated.json()["status"] == "shipped"


def test_missing_order(client):
    assert client.get("/api/orders/missing").status_code == 404


def test_express_order_placed_at_month_end(client):
    # The seeded express order is placed on the last day of the previous month.
    response = client.get("/api/orders/express-1002")
    assert response.status_code == 200
    assert "estimated_delivery" in response.json()


@pytest.mark.parametrize(
    ("created_at", "expected"),
    [
        ("2026-08-31T12:00:00+00:00", "2026-09-02"),
        ("2026-09-30T12:00:00+00:00", "2026-10-02"),
        ("2026-12-31T12:00:00+00:00", "2027-01-02"),
        ("2028-02-28T12:00:00+00:00", "2028-03-01"),
        ("2026-09-10T12:00:00+00:00", "2026-09-12"),
    ],
)
def test_express_estimated_delivery_crosses_month_boundary(created_at, expected):
    row = {"id": "express-x", "priority": "express", "created_at": created_at}
    assert main.order_detail(row)["estimated_delivery"] == expected


ORDER_ROUTE = "/api/orders/{order_id}"


def test_order_lookup_telemetry(client, telemetry):
    spans, logs, request_counts = telemetry
    before = request_counts()

    assert client.get("/api/orders/standard-1001").status_code == 200
    assert client.get("/api/orders/missing").status_code == 404

    after = request_counts()
    assert after[("GET", ORDER_ROUTE, 200)] - before.get(("GET", ORDER_ROUTE, 200), 0) == 1
    assert after[("GET", ORDER_ROUTE, 404)] - before.get(("GET", ORDER_ROUTE, 404), 0) == 1

    finished = spans.get_finished_spans()
    assert [span.name for span in finished] == [f"GET {ORDER_ROUTE}"] * 2
    assert [span.attributes["http.response.status_code"] for span in finished] == [200, 404]
    assert finished[0].attributes["order.id"] == "standard-1001"

    records = [data.log_record for data in logs.get_finished_logs()]
    assert [record.attributes["http.response.status_code"] for record in records] == [200, 404]
    assert [record.severity_text for record in records] == ["INFO", "WARN"]
    assert records[0].trace_id == finished[0].context.trace_id


def test_order_lookup_server_error_telemetry(client, telemetry, monkeypatch):
    spans, logs, request_counts = telemetry
    monkeypatch.setattr(main, "order_detail", lambda row: 1 / 0)
    with TestClient(main.app, raise_server_exceptions=False) as failing_client:
        assert failing_client.get("/api/orders/standard-1001").status_code == 500

    assert request_counts()[("GET", ORDER_ROUTE, 500)] >= 1
    (span,) = spans.get_finished_spans()
    assert span.attributes["http.response.status_code"] == 500
    assert span.status.is_ok is False
    (record,) = [data.log_record for data in logs.get_finished_logs()]
    assert record.severity_text == "ERROR"
    assert record.attributes["error.type"] == "ZeroDivisionError"


def test_other_endpoints_are_not_instrumented(client, telemetry):
    spans, logs, request_counts = telemetry
    client.get("/healthz")
    client.get("/api/orders")
    client.post("/api/orders", json={"customer": "Taylor", "item": "Mug"})
    client.patch("/api/orders/standard-1001", json={"status": "shipped"})

    assert spans.get_finished_spans() == ()
    assert logs.get_finished_logs() == ()
    assert {route for _, route, _ in request_counts()} <= {ORDER_ROUTE}

import os
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from opentelemetry import _logs, metrics, propagate, trace
from opentelemetry._logs import SeverityNumber
from opentelemetry.trace import SpanKind, Status, StatusCode
from pydantic import BaseModel, Field
from starlette.routing import Match

from app.telemetry import setup_telemetry


DB_PATH = Path(os.getenv("ORDER_DB_PATH", "data/orders.db"))
STATUSES = {"received", "preparing", "shipped", "delivered"}


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with connect() as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                customer TEXT NOT NULL,
                item TEXT NOT NULL,
                priority TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        if db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0:
            now = datetime.now(timezone.utc)
            previous_month_end = now.replace(day=1) - timedelta(days=1)
            for order in (
                ("standard-1001", "Avery", "Notebook", "standard", "received", now),
                ("express-1002", "Sam", "Headphones", "express", "preparing", previous_month_end),
                ("standard-1003", "Riley", "Water bottle", "standard", "shipped", now),
            ):
                db.execute(
                    "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)",
                    (*order[:5], order[5].isoformat()),
                )


def as_dict(row):
    return dict(row) if row else None


def order_detail(row):
    order = as_dict(row)
    if order["priority"] == "express":
        placed_at = datetime.fromisoformat(order["created_at"])
        estimated_at = placed_at.replace(day=placed_at.day + 2)
        order["estimated_delivery"] = estimated_at.date().isoformat()
    return order


class NewOrder(BaseModel):
    customer: str = Field(min_length=1, max_length=80)
    item: str = Field(min_length=1, max_length=120)
    priority: str = "standard"


class StatusUpdate(BaseModel):
    status: str


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Order Tracker", lifespan=lifespan)

setup_telemetry()
tracer = trace.get_tracer("order_tracker")
logger = _logs.get_logger("order_tracker")
request_duration = metrics.get_meter("order_tracker").create_histogram(
    "http.server.request.duration",
    unit="s",
    description="Duration of HTTP server requests",
    explicit_bucket_boundaries_advisory=[
        0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1, 2.5, 5, 7.5, 10,
    ],
)


@app.middleware("http")
async def instrument_order_lookup(request: Request, call_next):
    """Record a span, a duration metric and a log record for order lookups only."""
    for route in app.routes:
        match, child_scope = route.matches(request.scope)
        if match is Match.FULL and getattr(route, "endpoint", None) is get_order:
            break
    else:
        return await call_next(request)

    order_id = child_scope["path_params"]["order_id"]
    status_code, error = 500, None
    start = time.perf_counter()
    with tracer.start_as_current_span(
        f"{request.method} {route.path}",
        context=propagate.extract(request.headers),
        kind=SpanKind.SERVER,
        attributes={
            "http.request.method": request.method,
            "http.route": route.path,
            "url.path": request.url.path,
            "order.id": order_id,
        },
    ) as span:
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception as exc:
            error = exc
            raise
        finally:
            duration = time.perf_counter() - start
            attributes = {
                "http.request.method": request.method,
                "http.route": route.path,
                "http.response.status_code": status_code,
            }
            if status_code >= 500:
                attributes["error.type"] = type(error).__qualname__ if error else str(status_code)
                span.set_status(Status(StatusCode.ERROR))
            span.set_attribute("http.response.status_code", status_code)
            request_duration.record(duration, attributes)

            if status_code >= 500:
                severity = SeverityNumber.ERROR
            elif status_code >= 400:
                severity = SeverityNumber.WARN
            else:
                severity = SeverityNumber.INFO
            logger.emit(
                timestamp=time.time_ns(),
                severity_number=severity,
                severity_text=severity.name,
                body="Order lookup",
                attributes={**attributes, "order.id": order_id, "duration_ms": duration * 1000},
                exception=error,
            )


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent.parent / "static" / "index.html")


@app.get("/healthz")
def health():
    with connect() as db:
        db.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/api/orders")
def list_orders():
    with connect() as db:
        rows = db.execute("SELECT * FROM orders ORDER BY created_at DESC").fetchall()
    return [as_dict(row) for row in rows]


@app.get("/api/orders/{order_id}")
def get_order(order_id: str):
    with connect() as db:
        row = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Order not found")
    return order_detail(row)


@app.post("/api/orders", status_code=201)
def create_order(order: NewOrder):
    if order.priority not in {"standard", "express"}:
        raise HTTPException(422, "Priority must be standard or express")
    order_id = str(uuid4())
    with connect() as db:
        db.execute(
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)",
            (order_id, order.customer, order.item, order.priority, "received",
             datetime.now(timezone.utc).isoformat()),
        )
    return get_order(order_id)


@app.patch("/api/orders/{order_id}")
def update_status(order_id: str, update: StatusUpdate):
    if update.status not in STATUSES:
        raise HTTPException(422, "Invalid status")
    with connect() as db:
        cursor = db.execute(
            "UPDATE orders SET status = ? WHERE id = ?",
            (update.status, order_id),
        )
    if cursor.rowcount == 0:
        raise HTTPException(404, "Order not found")
    return get_order(order_id)

# Incident evidence: Order lookup 5xx responses

## Alert

- Status: firing
- Started: 2026-09-30T20:36:00Z
- Affected endpoint: GET /api/orders/{order_id}
- Evaluation window: 5m (rule evaluated every 1m)
- Summary: Order lookups are returning 5xx errors
- Description: GET /api/orders/{order_id} returned about 1 5xx response(s) in the last 5 minutes.
- Value: [ var='A' labels={} type='query' value=1.0228853550094106 ], [ var='C' labels={} type='threshold' value=1 ]
- Dashboard: http://127.0.0.1:3000/d/order-lookups/order-lookups
- Alert rule: http://127.0.0.1:3000/alerting/grafana/order-lookup-5xx/view?orgId=1
- Labels: {"alertname": "Order lookup 5xx responses", "grafana_folder": "Order Tracker", "route": "/api/orders/{order_id}", "service": "order-tracker", "severity": "critical"}

Evidence window: 2026-09-30T20:06:10.961709+00:00 to 2026-09-30T20:36:10.961709+00:00 (UTC).

## Metrics (Prometheus)

Requests in the evidence window, by status code and error type:

| Status | Error type | Requests |
| --- | --- | --- |
| 200 |  | 0 |
| 404 |  | 0 |
| 500 | ValueError | 3 |

Minutes with 5xx responses (UTC):

- 20:18: ~1
- 20:22: ~1
- 20:36: ~1

Queries and full series are in `metrics.json`.

## Logs (Loki)

3 ERROR log record(s) for this endpoint in the window.

### ValueError: day is out of range for month (3 occurrence(s))

Affected order IDs: `express-1002`

Most recent occurrence:

```
{
  "timestamp": "1790800528060253882",
  "line": "Order lookup",
  "detected_level": "error",
  "duration_ms": "28.11273200040887",
  "error_type": "ValueError",
  "exception_message": "day is out of range for month",
  "exception_type": "ValueError",
  "flags": "3",
  "http_request_method": "GET",
  "http_response_status_code": "500",
  "http_route": "/api/orders/{order_id}",
  "observed_timestamp": "1790800528086177506",
  "order_id": "express-1002",
  "scope_name": "order_tracker",
  "service_instance_id": "06f03ea7-ca35-4931-a2f0-4237145e88a6",
  "service_name": "order-tracker",
  "severity_number": "17",
  "severity_text": "ERROR",
  "span_id": "23e6961d5acb4c4d",
  "telemetry_sdk_language": "python",
  "telemetry_sdk_name": "opentelemetry",
  "telemetry_sdk_version": "1.44.0",
  "trace_id": "4ef909674d53c0abe6dbc70263f807b3"
}
```

Stack trace:

```
Traceback (most recent call last):
  File "/app/app/main.py", line 126, in instrument_order_lookup
    response = await call_next(request)
               ^^^^^^^^^^^^^^^^^^^^^^^^
  File "/app/.venv/lib/python3.12/site-packages/starlette/middleware/base.py", line 168, in call_next
    raise app_exc from app_exc.__cause__ or app_exc.__context__
  File "/app/.venv/lib/python3.12/site-packages/starlette/middleware/base.py", line 144, in coro
    await self.app(scope, receive_or_disconnect, send_no_error)
  File "/app/.venv/lib/python3.12/site-packages/starlette/middleware/exceptions.py", line 63, in __call__
    await wrap_app_handling_exceptions(self.app, conn)(scope, receive, send)
  File "/app/.venv/lib/python3.12/site-packages/starlette/_exception_handler.py", line 53, in wrapped_app
    raise exc
  File "/app/.venv/lib/python3.12/site-packages/starlette/_exception_handler.py", line 42, in wrapped_app
    await app(scope, receive, sender)
  File "/app/.venv/lib/python3.12/site-packages/fastapi/middleware/asyncexitstack.py", line 18, in __call__
    await self.app(scope, receive, send)
  File "/app/.venv/lib/python3.12/site-packages/starlette/routing.py", line 670, in __call__
    await self.middleware_stack(scope, receive, send)
  File "/app/.venv/lib/python3.12/site-packages/fastapi/routing.py", line 2734, in app
    await route.handle(scope, receive, send)
  File "/app/.venv/lib/python3.12/site-packages/fastapi/routing.py", line 1281, in handle
    await super().handle(scope, receive, send)
  File "/app/.venv/lib/python3.12/site-packages/starlette/routing.py", line 280, in handle
    await self.app(scope, receive, send)
  File "/app/.venv/lib/python3.12/site-packages/fastapi/routing.py", line 158, in app
    await wrap_app_handling_exceptions(app, request)(scope, receive, send)
  File "/app/.venv/lib/python3.12/site-packages/starlette/_exception_handler.py", line 53, in wrapped_app
    raise exc
  File "/app/.venv/lib/python3.12/site-packages/starlette/_exception_handler.py", line 42, in wrapped_app
    await app(scope, receive, sender)
  File "/app/.venv/lib/python3.12/site-packages/fastapi/routing.py", line 144, in app
    response = await f(request)
               ^^^^^^^^^^^^^^^^
  File "/app/.venv/lib/python3.12/site-packages/fastapi/routing.py", line 706, in app
    raw_response = await run_endpoint_function(
                   ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/app/.venv/lib/python3.12/site-packages/fastapi/routing.py", line 354, in run_endpoint_function
    return await run_in_threadpool(dependant.call, **values)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/app/.venv/lib/python3.12/site-packages/starlette/concurrency.py", line 34, in run_in_threadpool
    return await anyio.to_thread.run_sync(func)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/app/.venv/lib/python3.12/site-packages/anyio/to_thread.py", line 65, in run_sync
    return await get_async_backend().run_sync_in_worker_thread(
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/app/.venv/lib/python3.12/site-packages/anyio/_backends/_asyncio.py", line 2706, in run_sync_in_worker_thread
    return await future
           ^^^^^^^^^^^^
  File "/app/.venv/lib/python3.12/site-packages/anyio/_backends/_asyncio.py", line 1100, in run
    result = context.run(func, *args)
             ^^^^^^^^^^^^^^^^^^^^^^^^
  File "/app/app/main.py", line 186, in get_order
    return order_detail(row)
           ^^^^^^^^^^^^^^^^^
  File "/app/app/main.py", line 65, in order_detail
    estimated_at = placed_at.replace(day=placed_at.day + 2)
                   ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
ValueError: day is out of range for month
```

Recent lookups for this endpoint (all statuses, newest first):

| Time (UTC) | Status | Order ID | Duration (ms) |
| --- | --- | --- | --- |
| 20:35:28 | 500 | `express-1002` | 28.1 |
| 20:21:40 | 500 | `express-1002` | 26.9 |
| 20:17:36 | 500 | `express-1002` | 69.5 |

Raw entries are in `logs.json`.

## Traces (Tempo)

### Trace `4ef909674d53c0abe6dbc70263f807b3`

```
[
  {
    "name": "GET /api/orders/{order_id}",
    "duration_ms": 48.6,
    "status": {
      "message": "ValueError: day is out of range for month",
      "code": "STATUS_CODE_ERROR"
    },
    "attributes": {
      "order.id": "express-1002",
      "http.request.method": "GET",
      "http.response.status_code": "500",
      "url.path": "/api/orders/express-1002",
      "http.route": "/api/orders/{order_id}"
    },
    "events": [
      {
        "name": "exception",
        "exception.type": "ValueError",
        "exception.message": "day is out of range for month",
        "exception.stacktrace": "Traceback (most recent call last):\n  File \"/app/.venv/lib/python3.12/site-packages/opentelemetry/trace/__init__.py\", line 608, in use_span\n    yield span\n  File \"/app/.venv/lib/python3.12/site-packages/opentelemetry/sdk/trace/__init__.py\", line 1183, in start_as_current_span\n    yield span\n  File \"/app/app/main.py\", line 126, in instrument_order_lookup\n    response = await call_next(request)\n               ^^^^^^^^^^^^^^^^^^^^^^^^\n  File \"/app/.venv/lib/python3.12/site-packages/starlette/middleware/base.py\", line 168, in call_next\n    raise app_exc from app_exc.__cause__ or app_exc.__context__\n  File \"/app/.venv/lib/python3.12/site-packages/starlette/middleware/base.py\", line 144, in coro\n    await self.app(scope, receive_or_disconnect, send_no_error)\n  File \"/app/.venv/lib/python3.12/site-packages/starlette/middleware/exceptions.py\", line 63, in __call__\n    await wrap_app_handling_exceptions(self.app, conn)(scope, receive, send)\n  File \"/app/.venv/lib/python3.12/site-packages/starlette/_exception_handler.py\", line 53, in wrapped_app\n    raise exc\n  File \"/app/.venv/lib/python3.12/site-packages/starlette/_exception_handler.py\", line 42, in wrapped_app\n    await app(scope, receive, sender)\n  File \"/app/.venv/lib/python3.12/site-packages/fastapi/middleware/asyncexitstack.py\", line 18, in __call__\n    await self.app(scope, receive, send)\n  File \"/app/.venv/lib/python3.12/site-packages/starlette/routing.py\", line 670, in __call__\n    await self.middleware_stack(scope, receive, send)\n  File \"/app/.venv/lib/python3.12/site-packages/fastapi/routing.py\", line 2734, in app\n    await route.handle(scope, receive, send)\n  File \"/app/.venv/lib/python3.12/site-packages/fastapi/routing.py\", line 1281, in handle\n    await super().handle(scope, receive, send)\n  File \"/app/.venv/lib/python3.12/site-packages/starlette/routing.py\", line 280, in handle\n    await self.app(scope, receive, send)\n  File \"/app/.venv/lib/python3.12/site-packages/fastapi/routing.py\", line 158, in app\n    await wrap_app_handling_exceptions(app, request)(sco",
        "exception.escaped": "False"
      }
    ]
  }
]
```

### Trace `bfa64b7f080408552dab8817ddb0ac35`

```
[
  {
    "name": "GET /api/orders/{order_id}",
    "duration_ms": 36.45,
    "status": {
      "message": "ValueError: day is out of range for month",
      "code": "STATUS_CODE_ERROR"
    },
    "attributes": {
      "order.id": "express-1002",
      "http.request.method": "GET",
      "http.response.status_code": "500",
      "url.path": "/api/orders/express-1002",
      "http.route": "/api/orders/{order_id}"
    },
    "events": [
      {
        "name": "exception",
        "exception.type": "ValueError",
        "exception.message": "day is out of range for month",
        "exception.stacktrace": "Traceback (most recent call last):\n  File \"/app/.venv/lib/python3.12/site-packages/opentelemetry/trace/__init__.py\", line 608, in use_span\n    yield span\n  File \"/app/.venv/lib/python3.12/site-packages/opentelemetry/sdk/trace/__init__.py\", line 1183, in start_as_current_span\n    yield span\n  File \"/app/app/main.py\", line 126, in instrument_order_lookup\n    response = await call_next(request)\n               ^^^^^^^^^^^^^^^^^^^^^^^^\n  File \"/app/.venv/lib/python3.12/site-packages/starlette/middleware/base.py\", line 168, in call_next\n    raise app_exc from app_exc.__cause__ or app_exc.__context__\n  File \"/app/.venv/lib/python3.12/site-packages/starlette/middleware/base.py\", line 144, in coro\n    await self.app(scope, receive_or_disconnect, send_no_error)\n  File \"/app/.venv/lib/python3.12/site-packages/starlette/middleware/exceptions.py\", line 63, in __call__\n    await wrap_app_handling_exceptions(self.app, conn)(scope, receive, send)\n  File \"/app/.venv/lib/python3.12/site-packages/starlette/_exception_handler.py\", line 53, in wrapped_app\n    raise exc\n  File \"/app/.venv/lib/python3.12/site-packages/starlette/_exception_handler.py\", line 42, in wrapped_app\n    await app(scope, receive, sender)\n  File \"/app/.venv/lib/python3.12/site-packages/fastapi/middleware/asyncexitstack.py\", line 18, in __call__\n    await self.app(scope, receive, send)\n  File \"/app/.venv/lib/python3.12/site-packages/starlette/routing.py\", line 670, in __call__\n    await self.middleware_stack(scope, receive, send)\n  File \"/app/.venv/lib/python3.12/site-packages/fastapi/routing.py\", line 2734, in app\n    await route.handle(scope, receive, send)\n  File \"/app/.venv/lib/python3.12/site-packages/fastapi/routing.py\", line 1281, in handle\n    await super().handle(scope, receive, send)\n  File \"/app/.venv/lib/python3.12/site-packages/starlette/routing.py\", line 280, in handle\n    await self.app(scope, receive, send)\n  File \"/app/.venv/lib/python3.12/site-packages/fastapi/routing.py\", line 158, in app\n    await wrap_app_handling_exceptions(app, request)(sco",
        "exception.escaped": "False"
      }
    ]
  }
]
```

### Trace `cff2c7dab785de46ceef2b3e59ff00fe`

```
[
  {
    "name": "GET /api/orders/{order_id}",
    "duration_ms": 71.51,
    "status": {
      "message": "ValueError: day is out of range for month",
      "code": "STATUS_CODE_ERROR"
    },
    "attributes": {
      "order.id": "express-1002",
      "http.request.method": "GET",
      "http.response.status_code": "500",
      "url.path": "/api/orders/express-1002",
      "http.route": "/api/orders/{order_id}"
    },
    "events": [
      {
        "name": "exception",
        "exception.type": "ValueError",
        "exception.message": "day is out of range for month",
        "exception.stacktrace": "Traceback (most recent call last):\n  File \"/app/.venv/lib/python3.12/site-packages/opentelemetry/trace/__init__.py\", line 608, in use_span\n    yield span\n  File \"/app/.venv/lib/python3.12/site-packages/opentelemetry/sdk/trace/__init__.py\", line 1183, in start_as_current_span\n    yield span\n  File \"/app/app/main.py\", line 126, in instrument_order_lookup\n    response = await call_next(request)\n               ^^^^^^^^^^^^^^^^^^^^^^^^\n  File \"/app/.venv/lib/python3.12/site-packages/starlette/middleware/base.py\", line 168, in call_next\n    raise app_exc from app_exc.__cause__ or app_exc.__context__\n  File \"/app/.venv/lib/python3.12/site-packages/starlette/middleware/base.py\", line 144, in coro\n    await self.app(scope, receive_or_disconnect, send_no_error)\n  File \"/app/.venv/lib/python3.12/site-packages/starlette/middleware/exceptions.py\", line 63, in __call__\n    await wrap_app_handling_exceptions(self.app, conn)(scope, receive, send)\n  File \"/app/.venv/lib/python3.12/site-packages/starlette/_exception_handler.py\", line 53, in wrapped_app\n    raise exc\n  File \"/app/.venv/lib/python3.12/site-packages/starlette/_exception_handler.py\", line 42, in wrapped_app\n    await app(scope, receive, sender)\n  File \"/app/.venv/lib/python3.12/site-packages/fastapi/middleware/asyncexitstack.py\", line 18, in __call__\n    await self.app(scope, receive, send)\n  File \"/app/.venv/lib/python3.12/site-packages/starlette/routing.py\", line 670, in __call__\n    await self.middleware_stack(scope, receive, send)\n  File \"/app/.venv/lib/python3.12/site-packages/fastapi/routing.py\", line 2734, in app\n    await route.handle(scope, receive, send)\n  File \"/app/.venv/lib/python3.12/site-packages/fastapi/routing.py\", line 1281, in handle\n    await super().handle(scope, receive, send)\n  File \"/app/.venv/lib/python3.12/site-packages/starlette/routing.py\", line 280, in handle\n    await self.app(scope, receive, send)\n  File \"/app/.venv/lib/python3.12/site-packages/fastapi/routing.py\", line 158, in app\n    await wrap_app_handling_exceptions(app, request)(sco",
        "exception.escaped": "False"
      }
    ]
  }
]
```

Full traces are in `traces/`.

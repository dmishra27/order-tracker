"""OpenTelemetry setup.

Signals go over OTLP/HTTP when OTEL_EXPORTER_OTLP_ENDPOINT is set (Compose points it at
the Collector) and to the console otherwise, e.g. for a bare `uvicorn` run.
"""

import os

from opentelemetry import _logs, metrics, trace
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, ConsoleLogRecordExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter


def setup_telemetry():
    resource = Resource.create(
        {"service.name": os.getenv("OTEL_SERVICE_NAME", "order-tracker")}
    )
    # The OTLP exporters read the endpoint and append /v1/traces, /v1/metrics, /v1/logs.
    use_otlp = bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))

    # Keep providers that are already installed, such as the in-memory ones used by tests.
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        tracer_provider = TracerProvider(resource=resource)
        span_exporter = OTLPSpanExporter() if use_otlp else ConsoleSpanExporter()
        tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
        trace.set_tracer_provider(tracer_provider)

    if not isinstance(metrics.get_meter_provider(), MeterProvider):
        # Exports every 60s by default; override with OTEL_METRIC_EXPORT_INTERVAL (ms).
        metric_exporter = OTLPMetricExporter() if use_otlp else ConsoleMetricExporter()
        reader = PeriodicExportingMetricReader(metric_exporter)
        metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))

    if not isinstance(_logs.get_logger_provider(), LoggerProvider):
        logger_provider = LoggerProvider(resource=resource)
        log_exporter = OTLPLogExporter() if use_otlp else ConsoleLogRecordExporter()
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
        _logs.set_logger_provider(logger_provider)

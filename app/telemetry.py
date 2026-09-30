"""OpenTelemetry setup. All three signals are exported to the console for now."""

import os

from opentelemetry import _logs, metrics, trace
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

    # Keep providers that are already installed, such as the in-memory ones used by tests.
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        trace.set_tracer_provider(tracer_provider)

    if not isinstance(metrics.get_meter_provider(), MeterProvider):
        # Exports every 60s by default; override with OTEL_METRIC_EXPORT_INTERVAL (ms).
        reader = PeriodicExportingMetricReader(ConsoleMetricExporter())
        metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))

    if not isinstance(_logs.get_logger_provider(), LoggerProvider):
        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(ConsoleLogRecordExporter())
        )
        _logs.set_logger_provider(logger_provider)

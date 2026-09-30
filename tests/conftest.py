import pytest
from opentelemetry import _logs, metrics, trace
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# Installed before the app is imported, so setup_telemetry() keeps these instead of
# the console exporters.
span_exporter = InMemorySpanExporter()
metric_reader = InMemoryMetricReader()
log_exporter = InMemoryLogRecordExporter()

_tracer_provider = TracerProvider()
_tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
trace.set_tracer_provider(_tracer_provider)
metrics.set_meter_provider(MeterProvider(metric_readers=[metric_reader]))
_logger_provider = LoggerProvider()
_logger_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
_logs.set_logger_provider(_logger_provider)


def request_counts():
    """Cumulative request counts keyed by (method, route, status code)."""
    counts = {}
    data = metric_reader.get_metrics_data()
    for resource_metrics in data.resource_metrics if data else []:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name != "http.server.request.duration":
                    continue
                for point in metric.data.data_points:
                    key = (
                        point.attributes["http.request.method"],
                        point.attributes["http.route"],
                        point.attributes["http.response.status_code"],
                    )
                    counts[key] = point.count
    return counts


@pytest.fixture
def telemetry():
    span_exporter.clear()
    log_exporter.clear()
    return span_exporter, log_exporter, request_counts

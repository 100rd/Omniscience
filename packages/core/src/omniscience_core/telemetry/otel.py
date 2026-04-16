"""OpenTelemetry initialisation for tracing and metrics.

``init_telemetry`` should be called once at application startup, after
``configure_logging``.  When ``settings.otlp_endpoint`` is not set the
function installs no-op providers so all instrumentation calls are safe
to make without a running collector.
"""

from __future__ import annotations

import structlog
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from omniscience_core.config import Settings

log = structlog.get_logger(__name__)


def _build_resource(settings: Settings) -> Resource:
    return Resource.create(
        {
            "service.name": settings.app_name,
            "service.version": settings.app_version,
            "deployment.environment": settings.environment,
        }
    )


def init_telemetry(settings: Settings) -> trace.Tracer:
    """Initialise TracerProvider and MeterProvider.

    When ``settings.otlp_endpoint`` is ``None`` the SDK providers are still
    registered (so spans/metrics are collected) but no exporter is attached —
    they are discarded in-process.  This keeps all instrumentation call-sites
    identical between dev and prod.

    Args:
        settings: Application settings instance.

    Returns:
        A named tracer for the application.  Callers may use it directly or
        obtain fresh tracers via ``opentelemetry.trace.get_tracer()``.
    """
    resource = _build_resource(settings)

    _init_tracing(settings, resource)
    _init_metrics(settings, resource)

    return trace.get_tracer(settings.app_name, settings.app_version)


def _init_tracing(settings: Settings, resource: Resource) -> None:
    provider = TracerProvider(resource=resource)

    if settings.otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter(endpoint=settings.otlp_endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        log.info("otel_tracing_enabled", endpoint=settings.otlp_endpoint)
    else:
        log.info("otel_tracing_disabled", reason="OTLP_ENDPOINT not set")

    trace.set_tracer_provider(provider)


def _init_metrics(settings: Settings, resource: Resource) -> None:
    readers = []

    if settings.otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter

        exporter = OTLPMetricExporter(endpoint=settings.otlp_endpoint)
        readers.append(PeriodicExportingMetricReader(exporter))
        log.info("otel_metrics_enabled", endpoint=settings.otlp_endpoint)
    else:
        log.info("otel_metrics_disabled", reason="OTLP_ENDPOINT not set")

    provider = MeterProvider(resource=resource, metric_readers=readers)
    metrics.set_meter_provider(provider)

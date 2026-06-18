# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""OpenTelemetry bootstrap and structured logging for Précis.

Single responsibility: wire up traces, metrics, and structured logging
*at process start* so the rest of the codebase can emit signal without
ceremony.  See operations/observability.md for the why.

Design constraints:
- **Fail open.**  If any OTel package is missing, any exporter is
  unreachable, or any instrumentor errors, the app must still start.
  Telemetry never blocks the product.
- **Off by default in tests.**  PRECIS_TELEMETRY_ENABLED is a hard
  kill switch; absence or "false" disables everything.
- **No PII by default.**  PRECIS_TELEMETRY_CAPTURE_CONTENT controls
  whether prompts and tool inputs/outputs ride on spans.  Default is
  off; collector-side scrubbing is defence-in-depth.

Public entry points:
    init_observability(app)     — call once from FastAPI lifespan
    get_tracer(name)            — module-level tracer accessor
    get_logger(name)            — structlog logger with trace context
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public state
# ---------------------------------------------------------------------------

_initialised: bool = False
_enabled: bool = False


def telemetry_enabled() -> bool:
    return _enabled


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def init_observability(app: Any) -> None:
    """Bootstrap OTel + structlog.  Idempotent; safe to call twice.

    Reads:
        PRECIS_TELEMETRY_ENABLED      — "true" / "false" (default false)
        PRECIS_TELEMETRY_CAPTURE_CONTENT — "true" / "false" (default false)
        OTEL_SERVICE_NAME             — default "precis-mcp"
        OTEL_EXPORTER_OTLP_ENDPOINT   — default "http://otel-collector:4318"
        OTEL_RESOURCE_ATTRIBUTES      — passed through to the SDK
        OTEL_SEMCONV_STABILITY_OPT_IN — recommend "gen_ai_latest_experimental"
    """
    global _initialised, _enabled
    if _initialised:
        return
    _initialised = True

    if os.getenv("PRECIS_TELEMETRY_ENABLED", "false").lower() != "true":
        logger.info("Telemetry disabled (PRECIS_TELEMETRY_ENABLED!=true)")
        _setup_structlog_minimal()
        return

    # Set the OTel-recommended semconv opt-in if not already set.  Without
    # this, gen_ai.* attributes use the deprecated names.
    os.environ.setdefault(
        "OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental",
    )

    try:
        _init_tracer_provider()
        _init_instrumentors(app)
        _setup_structlog_with_otel()
        _enabled = True
        logger.info(
            "Telemetry enabled — exporter=%s service=%s",
            os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318"),
            os.getenv("OTEL_SERVICE_NAME", "precis-mcp"),
        )
    except Exception:
        # Never let observability bootstrap break app startup.
        logger.exception("Telemetry init failed — running without instrumentation")
        _enabled = False
        _setup_structlog_minimal()


def _init_tracer_provider() -> None:
    """Configure the global TracerProvider with an OTLP HTTP exporter."""
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    service_name = os.getenv("OTEL_SERVICE_NAME", "precis-mcp")
    resource = Resource.create({"service.name": service_name})

    # If a provider is already set (e.g. by tests), respect it.
    existing = trace.get_tracer_provider()
    if isinstance(existing, TracerProvider):
        provider = existing
    else:
        provider = TracerProvider(resource=resource)
        trace.set_tracer_provider(provider)

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318")
    exporter = OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")
    provider.add_span_processor(BatchSpanProcessor(exporter))


def _init_instrumentors(app: Any) -> None:
    """Attach auto-instrumentors.  Each one is best-effort; failures are logged."""
    capture_content = (
        os.getenv("PRECIS_TELEMETRY_CAPTURE_CONTENT", "false").lower() == "true"
    )

    # Suppress feedback loop: don't trace the OTLP exporter's own outbound POSTs
    # to the trace backend. Without this, every batch export creates a span
    # which creates more exports — flooding the backend with empty `POST` spans.
    # MUST be set BEFORE instrumentors are constructed — they read the env var
    # at .instrument() time and cache the excluded URL list.
    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if otlp_endpoint:
        target = otlp_endpoint.split("://")[-1].rstrip("/")
        excl = os.getenv("OTEL_PYTHON_EXCLUDED_URLS", "")
        if target and target not in excl:
            os.environ["OTEL_PYTHON_EXCLUDED_URLS"] = (
                f"{excl},{target}" if excl else target
            )

    # FastAPI — request spans for every HTTP route.
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor().instrument_app(app)
    except Exception:
        logger.exception("FastAPI instrumentor failed")

    # Outbound HTTP (Anthropic SDK uses httpx; ClickHouse via clickhouse-connect uses urllib3).
    # asyncpg deliberately omitted — Précis uses psycopg, not asyncpg, and importing the
    # asyncpg instrumentor crashes with ModuleNotFoundError when asyncpg isn't installed.
    # (mod_path, cls_name, instrumented_lib) — skipped quietly when the
    # instrumented library itself isn't installed (e.g. redis is absent from
    # the open package since the lock move to Postgres).
    import importlib.util
    for mod_path, cls_name, lib in [
        ("opentelemetry.instrumentation.httpx", "HTTPXClientInstrumentor", "httpx"),
        ("opentelemetry.instrumentation.urllib3", "URLLib3Instrumentor", "urllib3"),
        ("opentelemetry.instrumentation.redis", "RedisInstrumentor", "redis"),
    ]:
        if importlib.util.find_spec(lib) is None:
            continue
        try:
            mod = __import__(mod_path, fromlist=[cls_name])
            getattr(mod, cls_name)().instrument()
        except Exception:
            logger.exception("Instrumentor %s failed", cls_name)

    # LLM and agent framework — OpenLLMetry / Traceloop.
    # capture_content controls whether prompts and tool I/O ride on spans.
    try:
        from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor
        AnthropicInstrumentor().instrument()
    except Exception:
        logger.exception("AnthropicInstrumentor failed")

    try:
        from opentelemetry.instrumentation.langchain import LangchainInstrumentor
        LangchainInstrumentor().instrument()
    except Exception:
        logger.exception("LangchainInstrumentor failed")

    if not capture_content:
        # Best-effort suppression: Traceloop honours TRACELOOP_TRACE_CONTENT.
        # Setting at instrumentation time is the supported path.
        os.environ.setdefault("TRACELOOP_TRACE_CONTENT", "false")


# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------


def _setup_structlog_minimal() -> None:
    """Configure structlog to emit JSON without OTel context.

    Used when telemetry is disabled — keeps log shape consistent so
    downstream parsing isn't conditional on env.
    """
    try:
        import structlog
    except ImportError:
        return  # structlog not installed — fall back to stdlib logger
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )


def _setup_structlog_with_otel() -> None:
    """Configure structlog to inject trace_id / span_id from OTel context."""
    try:
        import structlog
    except ImportError:
        return

    def _add_otel_context(logger, method_name, event_dict):
        try:
            from opentelemetry import trace
            span = trace.get_current_span()
            ctx = span.get_span_context() if span else None
            if ctx and ctx.is_valid:
                event_dict["trace_id"] = format(ctx.trace_id, "032x")
                event_dict["span_id"] = format(ctx.span_id, "016x")
        except Exception:
            pass
        return event_dict

    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _add_otel_context,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )


def get_logger(name: str | None = None):
    """Return a structlog logger with OTel context injection (when enabled).

    Falls back to a kwarg-tolerant stdlib wrapper when structlog is missing
    (e.g. test envs without the obs deps installed).  Never raises.
    """
    try:
        import structlog
        return structlog.get_logger(name) if name else structlog.get_logger()
    except ImportError:
        return _StdlibKwargLogger(logging.getLogger(name or __name__))


class _StdlibKwargLogger:
    """Stdlib logger wrapper that accepts (and stringifies) kwargs.

    structlog's API is `log.info("event.name", key=value, ...)`. The stdlib
    logger raises on unknown kwargs. This shim flattens the kwargs into the
    formatted message so call sites don't need to branch.
    """
    def __init__(self, base):
        self._base = base

    def _format(self, event, kwargs):
        if not kwargs:
            return event
        attrs = " ".join(f"{k}={v!r}" for k, v in kwargs.items())
        return f"{event} {attrs}"

    def info(self, event, **kwargs):
        self._base.info(self._format(event, kwargs))

    def warning(self, event, **kwargs):
        self._base.warning(self._format(event, kwargs))

    def error(self, event, **kwargs):
        self._base.error(self._format(event, kwargs))

    def exception(self, event, **kwargs):
        self._base.exception(self._format(event, kwargs))

    def debug(self, event, **kwargs):
        self._base.debug(self._format(event, kwargs))


def get_tracer(name: str):
    """Return an OTel tracer.  No-op tracer when telemetry is disabled."""
    try:
        from opentelemetry import trace
        return trace.get_tracer(name)
    except ImportError:
        return _NoopTracer()


def get_meter(name: str):
    """Return an OTel meter.  No-op meter when telemetry is disabled.

    Mirrors `get_tracer` — call sites stay branch-free whether or not the
    OTel SDK is installed.
    """
    try:
        from opentelemetry import metrics
        return metrics.get_meter(name)
    except ImportError:
        return _NoopMeter()


class _NoopMeter:
    """Minimal meter stub for when OTel is unavailable."""
    def create_counter(self, *args, **kwargs):
        return _NoopInstrument()

    def create_histogram(self, *args, **kwargs):
        return _NoopInstrument()

    def create_up_down_counter(self, *args, **kwargs):
        return _NoopInstrument()


class _NoopInstrument:
    def add(self, *args, **kwargs):
        pass

    def record(self, *args, **kwargs):
        pass


class _NoopTracer:
    """Minimal tracer stub for when OTel is unavailable."""
    def start_as_current_span(self, *args, **kwargs):
        return _NoopSpan()


class _NoopSpan:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def set_attribute(self, *args, **kwargs):
        pass

    def add_event(self, *args, **kwargs):
        pass

    def record_exception(self, *args, **kwargs):
        pass

    def set_status(self, *args, **kwargs):
        pass
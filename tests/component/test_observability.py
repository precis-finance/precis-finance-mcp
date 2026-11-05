# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Opt-in tracing against an in-memory collector, with no external services."""
from __future__ import annotations

import builtins
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import runpy
import sys

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from precis_mcp import auth, observability


@pytest.fixture(autouse=True)
def isolated_bootstrap(monkeypatch):
    import structlog

    config = structlog.get_config()
    auth_token = auth._auth_context.set(None)
    monkeypatch.setattr(observability, "_initialised", False)
    monkeypatch.setattr(observability, "_enabled", False)
    monkeypatch.setenv("PRECIS_TELEMETRY_ENABLED", "false")
    for name in (
        "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_PYTHON_EXCLUDED_URLS",
        "OTEL_PYTHON_FASTAPI_EXCLUDED_URLS", "OTEL_SEMCONV_STABILITY_OPT_IN",
    ):
        # Track absence too: initialization may populate a default itself.
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name, raising=False)
    yield
    auth._auth_context.reset(auth_token)
    structlog.configure(**config)


@pytest.fixture
def collector(monkeypatch):
    trace = pytest.importorskip("opentelemetry.trace")
    sdk = pytest.importorskip("opentelemetry.sdk.trace")
    export = pytest.importorskip("opentelemetry.sdk.trace.export.in_memory_span_exporter")
    otlp = pytest.importorskip("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    exporter = export.InMemorySpanExporter()
    provider = sdk.TracerProvider(shutdown_on_exit=False)
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", provider)
    monkeypatch.setenv("PRECIS_TELEMETRY_ENABLED", "true")
    endpoints = []

    def make_exporter(*, endpoint):
        endpoints.append(endpoint)
        return exporter

    monkeypatch.setattr(otlp, "OTLPSpanExporter", make_exporter)
    # Keep process-global client instrumentation isolated from other tests.
    for module, cls in (
        ("httpx", "HTTPXClientInstrumentor"),
        ("urllib3", "URLLib3Instrumentor"),
        ("redis", "RedisInstrumentor"),
    ):
        instrumentor = pytest.importorskip("opentelemetry.instrumentation." + module)
        monkeypatch.setattr(getattr(instrumentor, cls), "instrument", lambda self: None)

    original_import = builtins.__import__
    llm_imports = []

    def track_import(name, *args, **kwargs):
        if name in {
            "opentelemetry.instrumentation.anthropic",
            "opentelemetry.instrumentation.langchain",
        }:
            llm_imports.append(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", track_import)
    yield provider, exporter, endpoints
    provider.shutdown()
    assert llm_imports == []


def test_disabled_bootstrap_needs_no_otel_and_keeps_json_logging(monkeypatch, capsys):
    original_import = builtins.__import__

    def without_otel(name, *args, **kwargs):
        if name.startswith("opentelemetry"):
            pytest.fail("Disabled telemetry tried to import OpenTelemetry")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_otel)
    observability.init_observability(FastAPI(), include_llm_instrumentors=False)
    assert not observability.telemetry_enabled()
    observability.get_logger("test").info("server.started")
    assert json.loads(capsys.readouterr().out)["event"] == "server.started"


def test_missing_telemetry_dependencies_do_not_prevent_requests(monkeypatch):
    def unavailable():
        raise ModuleNotFoundError("telemetry extra not installed")

    monkeypatch.setenv("PRECIS_TELEMETRY_ENABLED", "true")
    monkeypatch.setattr(observability, "_init_tracer_provider", unavailable)
    app = FastAPI()
    observability.init_observability(app, include_llm_instrumentors=False)
    assert not observability.telemetry_enabled()
    with TestClient(app) as client:
        assert client.get("/missing").status_code == 404


@pytest.mark.parametrize("framework", ["fastapi", "starlette"])
def test_http_traces_exclude_signed_transfers_and_exporter(framework, collector, monkeypatch):
    provider, exporter, endpoints = collector

    async def ok(request):
        return JSONResponse({"ok": True})

    routes = [Route(path, ok) for path in (
        "/probe", "/user-skip", "/api/artifacts/download", "/api/artifacts/upload",
    )]
    app = FastAPI(routes=routes) if framework == "fastapi" else Starlette(routes=routes)
    monkeypatch.setenv("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS", "user-skip")
    observability.init_observability(app, include_llm_instrumentors=False)
    # Repeated initialization must not attach a second exporter.
    observability.init_observability(app, include_llm_instrumentors=False)
    with TestClient(app) as client:
        for path in ("/probe", "/user-skip", "/api/artifacts/download?t=private-test-token",
                     "/api/artifacts/upload?t=private-test-token"):
            assert client.get(path).status_code == 200
    assert provider.force_flush()
    spans = exporter.get_finished_spans()
    servers = [span for span in spans if span.kind.name == "SERVER"]
    assert len(servers) == 1
    assert "/probe" in servers[0].name
    assert "private-test-token" not in repr([dict(span.attributes) for span in spans])
    assert endpoints == ["http://otel-collector:4318/v1/traces"]
    assert "otel-collector:4318" in os.environ["OTEL_PYTHON_EXCLUDED_URLS"]


@pytest.mark.parametrize("entrypoint", ["app_open", "server"])
def test_mcp_entrypoints_initialize_tracing_before_serving(entrypoint, collector, monkeypatch):
    provider, exporter, _ = collector
    from precis_mcp.artifacts import lifecycle
    from precis_mcp.engine import ibis_registry
    from tests.fakes.fake_redis import FakeRedis

    redis = FakeRedis()
    monkeypatch.setattr(ibis_registry._DEFAULT, "_registry", ibis_registry._DEFAULT._registry)
    monkeypatch.setattr(ibis_registry._DEFAULT, "_cache", ibis_registry._DEFAULT._cache.copy())
    monkeypatch.setattr("precis_mcp.db.open_platform_pool", lambda: None)
    monkeypatch.setattr("precis_mcp.db.close_platform_pool", lambda: None)
    monkeypatch.setattr("precis_mcp.db.close_redis_client", lambda: None)
    monkeypatch.setattr("precis_mcp.db.get_redis_client", lambda: redis)
    monkeypatch.setattr(lifecycle, "get_redis_client", lambda: redis)
    monkeypatch.setattr(lifecycle, "check_artifacts", lambda: None)
    monkeypatch.setattr("precis_mcp.analytical.runtime.validate_runtime_inputs", lambda catalogue: None)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.delenv("PRECIS_AUTH_MODE", raising=False)
    monkeypatch.delenv("PRECIS_AUTH_PREFLIGHT", raising=False)
    monkeypatch.delenv("PRECIS_BASE_URL", raising=False)
    root = Path(__file__).resolve().parents[2]
    if entrypoint == "app_open":
        namespace = runpy.run_path(str(root / "precis_mcp/app_open.py"))
        app = namespace["app"]
    else:
        # server.py uses setdefault; register the change with pytest so the
        # next application's startup does not inherit the dev-only mode.
        monkeypatch.setenv("PRECIS_AUTH_MODE", "devkey")
        monkeypatch.setenv("ENABLE_MCP_DEV_SERVER", "1")
        monkeypatch.setenv("MCP_DEV_KEY", "test-dev-key-" + "x" * 32)
        captured = []
        monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: captured.append(app))
        runpy.run_path(str(root / "precis_mcp/server.py"), run_name="__main__")
        app = captured[0]

    async def probe(request):
        return JSONResponse({"ok": True})

    app.router.routes.append(Route("/telemetry-probe", probe))
    with TestClient(app) as client:
        response = client.get("/telemetry-probe", headers={
            "Authorization": "Bearer test-dev-key-" + "x" * 32,
        })
    assert response.status_code == 200
    assert observability.telemetry_enabled()
    assert provider.force_flush()
    assert any(span.kind.name == "SERVER" and "/telemetry-probe" in span.name
               for span in exporter.get_finished_spans())


def test_embedding_lifespan_initialization_traces_http_without_restarting_lifespan(collector):
    provider, exporter, _ = collector
    events = []

    @asynccontextmanager
    async def lifespan(app):
        events.append("startup")
        observability.init_observability(app, include_llm_instrumentors=False)
        yield
        events.append("shutdown")

    app = FastAPI(lifespan=lifespan)
    with TestClient(app) as client:
        assert client.get("/missing").status_code == 404
    assert provider.force_flush()
    assert any(span.kind.name == "SERVER" for span in exporter.get_finished_spans())
    assert events == ["startup", "shutdown"]


@pytest.mark.parametrize("filename", ["docker-compose.yml", "docker-compose.local.yml"])
def test_services_sharing_an_application_image_use_identical_builds(filename):
    root = Path(__file__).resolve().parents[2]
    services = yaml.safe_load((root / "deploy" / filename).read_text())["services"]
    builds = {}
    for name, service in services.items():
        image = service.get("image", "")
        if "precis-finance-mcp:" not in image or "build" not in service:
            continue
        if image in builds:
            assert service["build"] == builds[image], name
        else:
            builds[image] = service["build"]
    assert builds

# Observability

What the server emits, how to point it at your collector, and what to alert
on. Everything here is optional and off by default — the baseline you always
get is structured JSON logs and health endpoints.

## The baseline (no setup)

- **Structured logs.** The server logs JSON (structlog: ISO timestamps,
  event keys) on stdout — ready for your log shipper as-is.
- **Liveness.** `GET /health` on the multi-user server returns
  `{"status": "ok"}`. Use it for a process-liveness probe; it checks no
  dependencies.
- **Readiness.** Use `GET /readyz` for load-balancer routing or a readiness
  probe. It returns HTTP 200 with `status: ready` when PostgreSQL, Redis,
  artifact schema/signing/storage and the selected analytical backend check
  pass; otherwise it returns HTTP 503. ClickHouse is checked with a query.
  For Databricks, the check validates configuration only, not warehouse
  connectivity.

For explicit operator diagnostics, use `clickhouse_init --scope open --check`
for ClickHouse and `admin_cli check-auth` for identity.

## Enabling telemetry

Tracing is optional. First install its dependencies:

```bash
pip install ".[telemetry]"
```

Then set `PRECIS_TELEMETRY_ENABLED=true` and point
`OTEL_EXPORTER_OTLP_ENDPOINT` at your collector before starting the server.
Both the OAuth server and the single-user dev-key server initialize tracing
before serving requests. Installing the extra alone leaves export disabled.

The default Docker image excludes telemetry dependencies. For either supplied
Compose bundle, build from source with `PRECIS_EXTRAS=telemetry` (or append
`,telemetry` to your existing extras), then recreate the application with the
enable switch and collector endpoint set. The Compose files pass these settings
to `precis-mcp`. The collector must be reachable from the container; the bundle
does not start one for you.

The deployment helper also accepts `--extras telemetry`; this installs the
dependencies but leaves the enable switch under your control.

| Variable | Default | Purpose |
|---|---|---|
| `PRECIS_TELEMETRY_ENABLED` | `false` | Master switch. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://otel-collector:4318` | OTLP **HTTP** receiver. |
| `OTEL_SERVICE_NAME` | `precis-mcp` | `service.name` on emitted spans. |
| `OTEL_RESOURCE_ATTRIBUTES` | unset | Extra `key=value` resource attributes. |

Bootstrap is deliberately non-fatal: missing dependencies or an initialization
failure are logged and the server keeps running. Individual instrumentation
failures do not prevent the remaining tracing from starting. An unreachable
collector causes export failures without preventing requests from being served.

Any OTLP-HTTP-capable backend works: an OpenTelemetry Collector, Grafana
Tempo/Alloy, Jaeger, or a vendor endpoint.

## What gets emitted

**Spans.** HTTP server spans, plus client spans for outbound httpx/urllib3 and
Redis calls. Signed artifact download/upload routes are excluded to keep
capability URLs out of traces. The remote OAuth transport additionally records
`mcp.tool_call` spans. The shared ingestion code defines the `ingest.*` spans
below; a separately launched ingestion worker needs its own tracing setup.

| Span | Attributes | Emitted when |
|---|---|---|
| `mcp.tool_call` | `precis.transport=mcp`, `precis.user_id`, `precis.tool_name`, `precis.out_mode`, `precis.is_error` | A tool call through the remote OAuth transport. |
| `ingest.attempt` → `ingest.extract` / `ingest.validate` / `ingest.swap` | `ingest.load_id`, `ingest.binding_id`, `ingest.source_id`, `ingest.source.kind` | A load running in a process with tracing configured. |

**Metric instruments** (ingestion):

These counters and histograms exist in the ingestion code, but the package does
not configure a metric provider or metric exporter. The tracing switch does not
export them; an embedding deployment must supply that separate metrics setup.

| Instrument | Type | Labels |
|---|---|---|
| `precis.ingest.attempts_total` | counter | `binding`, `status` |
| `precis.ingest.duration_seconds` | histogram | `binding`, `stage` |
| `precis.ingest.rows_landed` | histogram | `binding` |

**Log–trace correlation.** With telemetry on, JSON logs emitted within an active
span carry its `trace_id`/`span_id` — a log line found in your aggregator pivots
straight to its trace.

## Alerting on failed loads

The question that matters in production is "did last night's load fail, and
who finds out first?" Three tiers, by what you run:

1. **With a separately configured metrics pipeline.** Alert on
   `precis.ingest.attempts_total{status=~"failed_.*"}` increasing — one rule
   covers every binding, labelled by which one failed and at which stage
   (`duration_seconds{stage=…}` tells you where time went).
2. **No collector?** Poll the audit table from your existing monitoring —
   the `load_history` query in
   [Verifying a load](../configuration/ingestion.md#verifying-a-load)
   wrapped in a cron/exporter, alerting on any `failed_*` row newer than
   the last check.
3. **Ad hoc.** Any provisioned MCP user can ask — the
   [ingestion status tools](../reference/mcp-tools.md#ingestion-status)
   answer "did April land?" from the client. Useful, but not a substitute
   for an alert.

The same pattern covers **failed backups**: the backup runner POSTs any
non-success outcome to `BACKUP_ALERT_WEBHOOK_URL`, and every run, restore,
and drill lands an outcome row in `backup_history`
([backups & restore](backups.md)).

## Privacy

Précis MCP does not attach raw tool arguments or returned financial data as span
attributes. `PRECIS_TELEMETRY_CAPTURE_CONTENT` does not enable MCP payload
capture. Trace metadata includes tool names, user IDs, timing and status;
recorded exceptions can also contain error details. Use a collector within the
appropriate trust boundary. Signed transfer routes are excluded independently
of content-capture settings.

## Related

- [Environment variable reference](../configuration/environment-variables.md)
  — the telemetry variables in the full table.
- [Ingestion & data sources](../configuration/ingestion.md) — the pipeline
  the `ingest.*` spans and metrics describe.
- [Troubleshooting](troubleshooting.md) — symptom-indexed failures these
  signals point you at.

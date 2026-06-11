# Observability

What the server emits, how to point it at your collector, and what to alert
on. Everything here is optional and off by default — the baseline you always
get is structured JSON logs and a liveness endpoint.

## The baseline (no setup)

- **Structured logs.** The server logs JSON (structlog: ISO timestamps,
  event keys) on stdout — ready for your log shipper as-is.
- **Liveness.** `GET /health` on the multi-user server returns
  `{"status": "ok"}` — point your load balancer or container healthcheck at
  it. It is *liveness only* (process up), not readiness: it checks no
  dependencies. The deep checks are explicit commands —
  `clickhouse_init --scope open --check` for the data side,
  `admin_cli check-auth` for identity.

## Enabling telemetry

Tracing and metrics are OpenTelemetry, behind an extra and one switch:

```bash
pip install ".[telemetry]"          # bundled in the Docker image
```

| Variable | Default | Purpose |
|---|---|---|
| `PRECIS_TELEMETRY_ENABLED` | `false` | Master switch. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://otel-collector:4318` | OTLP **HTTP** receiver. |
| `OTEL_SERVICE_NAME` | `precis-agent` | `service.name` on emitted spans. |
| `OTEL_RESOURCE_ATTRIBUTES` | unset | Extra `key=value` resource attributes. |
| `PRECIS_TELEMETRY_CAPTURE_CONTENT` | `false` | Also put request/response *content* on spans — see [privacy](#privacy). |

Bootstrap is deliberately non-fatal: if the exporter is unreachable or an
instrumentor fails, the server logs the problem and runs uninstrumented —
telemetry can never take the service down.

Any OTLP-HTTP-capable backend works: an OpenTelemetry Collector, Grafana
Tempo/Alloy, Jaeger, or a vendor endpoint.

## What gets emitted

**Spans.** HTTP server spans for every request (FastAPI
auto-instrumentation), plus client spans for outbound httpx/urllib3
calls, plus two Précis-specific families:

| Span | Attributes | Emitted when |
|---|---|---|
| `mcp.tool_call` | `precis.transport=mcp`, `precis.user_id`, `precis.tool_name`, `precis.out_mode`, `precis.is_error` | Every `/mcp` tool call — who called what, with what outcome. |
| `ingest.attempt` → `ingest.extract` / `ingest.validate` / `ingest.swap` | `ingest.load_id`, `ingest.binding_id`, `ingest.source_id`, `ingest.source.kind` | Every load — one parent per attempt, one child per stage, so a slow or failing stage is visible directly in the trace. |

**Metrics** (ingestion):

| Instrument | Type | Labels |
|---|---|---|
| `precis.ingest.attempts_total` | counter | `binding`, `status` |
| `precis.ingest.duration_seconds` | histogram | `binding`, `stage` |
| `precis.ingest.rows_landed` | histogram | `binding` |

**Log–trace correlation.** With telemetry on, every JSON log line carries
the active `trace_id`/`span_id` — a log line found in your aggregator pivots
straight to its trace.

## Alerting on failed loads

The question that matters in production is "did last night's load fail, and
who finds out first?" Three tiers, by what you run:

1. **Metrics (preferred).** Alert on
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

!!! warning "Content capture sends financial figures to your collector"
    `PRECIS_TELEMETRY_CAPTURE_CONTENT` is a second, separate switch for a
    reason: with it on, tool inputs and outputs — financial figures — ride
    on spans into your collector. Leave it off unless the collector is
    inside the same trust boundary as the data.

Everything else emitted (span names, tool names, user ids, durations,
statuses) is operational metadata.

## Related

- [Environment variable reference](../configuration/environment-variables.md)
  — the telemetry variables in the full table.
- [Ingestion & data sources](../configuration/ingestion.md) — the pipeline
  the `ingest.*` spans and metrics describe.
- [Troubleshooting](troubleshooting.md) — symptom-indexed failures these
  signals point you at.

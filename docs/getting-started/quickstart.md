---
description: Run Précis Finance MCP locally with Docker, load the sample finance model, and connect an MCP client using a development key.
---

# Quickstart — local, single-user

The fastest way to see Précis Finance MCP working: a server on your own machine,
authenticated with a single static key. Use it for a trial, a local demo, or to
check your catalogue before exposing the server to other people.

**You need:** Docker with the compose plugin, the single-user compose file (one
download — no full checkout required, because the published image bundles the
demo model), and an MCP client to point at it (Claude Code, Claude Desktop, or
any MCP-capable agent). **Time:** about 15 minutes, most of it the demo-data
generation in step 2.

For a multi-user server that real users sign in to, see
[Remote access](../deployment/oauth-keycloak.md).

## 1. Start the stack

Download the single-user compose file and start the stack with **Docker Compose
2.17 or later**. It pulls the published application image, which contains the
demo model, and automatically builds a small PostgreSQL security patch layer.
The build instructions are embedded in the Compose file; no checkout or
instance is needed:

```bash
curl -O https://raw.githubusercontent.com/precis-finance/precis-finance-mcp/main/deploy/docker-compose.local.yml
export MCP_DEV_KEY=$(openssl rand -hex 32)
export ARTIFACT_SIGNING_KEY=$(openssl rand -hex 32)
docker compose -f docker-compose.local.yml up -d
```

(From a repository checkout the file is `deploy/docker-compose.local.yml` —
adjust the `-f` path. `up --build` builds the image from a checkout instead of
pulling; pin a release with `PRECIS_MCP_TAG`.)

That starts four long-running containers: Valkey for snapshots, a bundled ClickHouse, a bundled Postgres (the
ingestion bookkeeping database, doubling as the demo's mock data source), and
the single-user MCP server, published on `127.0.0.1` only. The server is
guarded by three deliberate gates — an explicit enable flag, the 32+
character key you just minted, and the localhost bind — so it accepts only
you, on this machine.

Keep hold of the key (`echo $MCP_DEV_KEY`); your client sends it on every
request.

**You should see:** `docker compose -f docker-compose.local.yml ps`
lists `clickhouse`, `postgres` and `redis` (healthy), plus `precis-mcp`
(`Up`). The `migrate` one-shot must complete successfully before the server starts.

## 2. Populate the demo model

The bundled ClickHouse starts empty. Generate the synthetic demo dataset —
monthly actuals from January 2023 through the installation month, plus the
example 2026 budget and forecast for a fictional IT consultancy — **inside the
server container** (it has the package installed and the connections configured):

```bash
docker compose -f docker-compose.local.yml exec precis-mcp \
  python -m precis_mcp.sample_data
```

This is more than a seed script: it writes the dataset to the mock Postgres
source and then pulls it into ClickHouse through the same ingestion pipeline
a real deployment runs — bindings, validation, atomic swap, semantic views —
so what you're evaluating is the pipeline itself, not a shortcut around it.
Takes a few minutes; it ends with a validation report.

Then confirm the deployment is coherent:

```bash
docker compose -f docker-compose.local.yml exec precis-mcp \
  python -m precis_mcp.clickhouse_init --scope open --check
```

**You should see:** the generator's validation report (revenue, margin,
utilisation, reconciliation, and monthly delivery cost-centre coverage all
`[OK]`), and the `--check` run printing one `ok` line per check (catalogue,
each semantic view, the scenario registry) before exiting `0`. A `FAIL` line
names exactly what's missing —
[troubleshooting](../operations/troubleshooting.md#clickhouse-and-the-model).

!!! note "Where commands run"
    The same pattern applies to every `python -m precis_mcp.*` command in
    these docs: `docker compose exec precis-mcp …` against the running
    stack, or a plain shell in a [source checkout](#running-without-docker)
    with the connection env vars exported.

Evaluating with your own model and data instead of the demo? Skip the
generator and provision the schema only — see
[Bring your own model](#5-bring-your-own-model) below,
[ClickHouse data modes](../deployment/clickhouse-data-modes.md) for the
provisioning presets, and [Ingestion](../configuration/ingestion.md) for
loading your data.

## 3. Connect your client

Point your MCP client at the local server, sending the key as a bearer token:

```jsonc
{
  "mcpServers": {
    "precis": {
      "url": "http://127.0.0.1:8768/mcp",
      "headers": { "Authorization": "Bearer <MCP_DEV_KEY>" }
    }
  }
}
```

## 4. Verify

Ask your client the kind of finance questions the catalogue is built for —
each exercises a different part of the server:

- **"Show the P&L for 2025 with comparatives."** — a statement with the right
  lines, signs, and subtotals (`run_statement`), not a flat column dump.
- **"Drill revenue down by cost centre."** — the same governed metric returned
  per cost centre, rolled up through the catalogue's dimensional hierarchy
  (`run_metric`).
- **"Show utilisation by month."** — a ratio whose denominator is averaged
  across periods, defined once in the catalogue rather than reconstructed in
  SQL per query (`run_metric`).
- **"What metrics can I report on?"** — the governed KPI catalogue
  (`list_kpis`), a curated metric vocabulary rather than a list of raw tables.

**You should see:** the client's tool list shows the Précis tools
(`precis_orientation`, `list_scenarios`, `list_kpis`, `run_metric`, …);
`list_kpis` returns the catalogue's governed metrics and `list_scenarios` the
demo's actuals / budget / forecast set; and the statement and metric answers
come back as figures aggregated from the semantic views and traceable to
source — never a value guessed over raw rows.

## 5. Bring your own model

Your model — `catalogue/`, `semantic/`, `integrations/`, `scenarios.yml` —
lives in an `instance/` directory; the image bundles a complete demo instance
showing the shape (it is what serves by default). To serve **your own** instead,
download the instance overlay alongside the compose file, set
`PRECIS_INSTANCE_DIR` to your directory, and bring the stack up with both files —
the overlay bind-mounts your model over the bundled one:

```bash
curl -O https://raw.githubusercontent.com/precis-finance/precis-finance-mcp/main/deploy/docker-compose.instance.yml
PRECIS_INSTANCE_DIR=/path/to/your/instance docker compose \
  -f docker-compose.local.yml -f docker-compose.instance.yml up -d
```

Then provision the *schema only* (instead of running the demo generator):

```bash
docker compose -f docker-compose.local.yml exec precis-mcp \
  python -m precis_mcp.clickhouse_init --scope open
```

Metric values stay empty until your data is loaded —
[Ingestion](../configuration/ingestion.md) covers pulling from your sources.

See [Catalogue & semantic model](../configuration/catalogue-and-semantic.md)
for how to describe your metrics and statements.

## Running without Docker

The server is plain Python (3.12+). Before starting it, configure PostgreSQL
(`PG*`) and Redis/Valkey, apply the open PostgreSQL migrations, and set
`PRECIS_BASE_URL` plus a writable `USER_DATA_DIR`. See
[direct Python setup](../operations/upgrades.md#direct-python-installations).

From a source checkout:

```bash
pip install -e ".[dev]"

export CHHOST=127.0.0.1 CHPORT=8123 CHUSER=default CHPASSWORD=...
export ENABLE_MCP_DEV_SERVER=1
export MCP_DEV_KEY=$(openssl rand -hex 32)
export ARTIFACT_SIGNING_KEY=$(openssl rand -hex 32)

python -m precis_mcp.server
```

It binds `127.0.0.1:8768` by default (`MCP_BIND_HOST` / `MCP_BIND_PORT` to
override). In this mode the instance directory is the checkout's `instance/`
— replace its contents (or the directory itself) with your model.

To populate the demo model in this mode (`python -m precis_mcp.sample_data`),
also export `CUSTOMER_PG_HOST` / `CUSTOMER_PG_USER` / `CUSTOMER_PG_PASSWORD` /
`CUSTOMER_PG_DATABASE=fpa_actuals` pointing at the same PostgreSQL server before
running the generator. The compose stack wires all of this for you — going
Docker-less is for serving a model you populate by other means.

## Next steps

- Understand the moving parts → [How Précis Finance MCP works](concepts.md)
- Describe your own metrics → [Catalogue & semantic model](../configuration/catalogue-and-semantic.md)
- Load your own data → [Ingestion & data sources](../configuration/ingestion.md)
- Open it to other users → [Remote access](../deployment/oauth-keycloak.md)
- Every knob → [Environment variable reference](../configuration/environment-variables.md)

## Charts and files

The bundle also starts Valkey for bounded snapshots and applies the open artifact migration before serving. It mounts persistent user-data storage. Query first, use `eval_chart_transform(data_refs=[data_ref], ...)` to build a chart, or `export_dataset(data_ref, format="xlsx")` to download the snapshot. File upload uses reserve → raw PUT → complete. See [Files and external agent hosts](../reference/files-and-hosts.md).

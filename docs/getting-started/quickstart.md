# Quickstart — local, single-user

The fastest way to see Précis-MCP working: a server on your own machine,
authenticated with a single static key. Use it for a trial, a local demo, or to
check your catalogue before exposing the server to other people.

**You need:** Docker with the compose plugin, a checkout of this repository,
and an MCP client to point at it (Claude Code, Claude Desktop, or any
MCP-capable agent). **Time:** about 10 minutes.

For a multi-user server that real users sign in to, see
[Remote access](../deployment/oauth-keycloak.md).

## 1. Start the stack

From the repository root:

```bash
MCP_DEV_KEY=$(openssl rand -hex 32) \
  docker compose -f deploy/docker-compose.local.yml up -d --build
```

That runs two containers: a bundled ClickHouse and the single-user MCP server,
published on `127.0.0.1` only. The server is guarded by three deliberate
gates — an explicit enable flag, the 32+ character key you just minted, and
the localhost bind — so it accepts only you, on this machine.

Keep hold of the key (`echo $MCP_DEV_KEY`); your client sends it on every
request.

**You should see:** `docker compose -f deploy/docker-compose.local.yml ps`
lists two services, `clickhouse` (healthy) and `precis-mcp`, both `Up`.

## 2. Provision the schema

The bundled ClickHouse starts empty. Create the read-layer schema for the
bundled demo model (or your own — see step 5). Run the provisioner **inside
the server container** — it already has the package installed and the
ClickHouse connection configured:

```bash
docker compose -f deploy/docker-compose.local.yml exec precis-mcp \
  python -m precis_mcp.clickhouse_init --scope open
docker compose -f deploy/docker-compose.local.yml exec precis-mcp \
  python -m precis_mcp.clickhouse_init --scope open --check   # validates catalogue + views
```

**You should see:** the `--check` run prints one `ok` line per check
(catalogue, each semantic view, the scenario registry) and exits `0`. A
`FAIL` line names exactly what's missing —
[troubleshooting](../operations/troubleshooting.md#clickhouse-and-the-model).

!!! note "Where commands run"
    The same pattern applies to every `python -m precis_mcp.*` command in
    these docs: `docker compose exec precis-mcp …` against the running
    stack, or a plain shell in a [source checkout](#running-without-docker)
    with the connection env vars exported.

See [ClickHouse data modes](../deployment/clickhouse-data-modes.md) for the
provisioning presets, including sample data, and
[Ingestion](../configuration/ingestion.md) for loading your own.

## 3. Connect your client

Point your MCP client at the local server, sending the key as a bearer token:

```jsonc
{
  "mcpServers": {
    "precis": {
      "url": "http://127.0.0.1:8768/sse",
      "headers": { "Authorization": "Bearer <MCP_DEV_KEY>" }
    }
  }
}
```

## 4. Verify

Ask your client to:

- list the available scenarios and metrics,
- run a metric or statement and read back the table,
- inspect the rows behind one of the figures.

**You should see:** the client's tool list shows the Précis tools
(`precis_orientation`, `list_scenarios`, `run_metric`, …), and the scenario
and metric listings return the entries your catalogue defines. If discovery
works, your server and model are wired up correctly. Note that
step 2 provisioned the *schema only* — metric values stay empty until data is
loaded. To query real figures, load your own data
([Ingestion](../configuration/ingestion.md)) or, for a populated demo without
bringing data, deploy the multi-user bundle with synthetic sample data
(`--data-mode bundle-sample` — see
[ClickHouse data modes](../deployment/clickhouse-data-modes.md)).

## 5. Bring your own model

Your model — `catalogue/`, `semantic/`, `integrations/`, `scenarios.yml` —
lives in an `instance/` directory; the repository ships a complete demo
instance showing the shape. Point the stack at yours by setting
`PRECIS_INSTANCE_DIR` to its host path before `docker compose up` (it is
mounted read-only over the bundled one), then re-run step 2.

See [Catalogue & semantic model](../configuration/catalogue-and-semantic.md)
for how to describe your metrics and statements.

## Running without Docker

The server is plain Python (3.12+). From a source checkout:

```bash
pip install -e ".[dev]"

export CHHOST=127.0.0.1 CHPORT=8123 CHUSER=default CHPASSWORD=...
export ENABLE_MCP_DEV_SERVER=1
export MCP_DEV_KEY=$(openssl rand -hex 32)

python -m precis_mcp.server
```

It binds `127.0.0.1:8768` by default (`MCP_BIND_HOST` / `MCP_BIND_PORT` to
override). In this mode the instance directory is the checkout's `instance/`
— replace its contents (or the directory itself) with your model.

## Next steps

- Understand the moving parts → [How Précis-MCP works](concepts.md)
- Describe your own metrics → [Catalogue & semantic model](../configuration/catalogue-and-semantic.md)
- Load your own data → [Ingestion & data sources](../configuration/ingestion.md)
- Open it to other users → [Remote access](../deployment/oauth-keycloak.md)
- Every knob → [Environment variable reference](../configuration/environment-variables.md)

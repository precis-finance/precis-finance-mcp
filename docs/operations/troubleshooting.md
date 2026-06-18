# Troubleshooting

Symptom-indexed. Error messages are quoted verbatim where the server prints
them — search this page for the text you're seeing. Each entry: what it
means, then the fix.

## First run

### "set MCP_DEV_KEY to a 32+ char random string"

Every compose command against the local stack — `up`, but also later `ps` /
`exec` — needs `MCP_DEV_KEY` in the shell. Export it (a one-shot `VAR=… up`
prefix doesn't survive to the next command):

```bash
export MCP_DEV_KEY=$(openssl rand -hex 32)
docker compose -f deploy/docker-compose.local.yml up -d --build
```

### "MCP dev server disabled. Set ENABLE_MCP_DEV_SERVER=1 to enable."

The single-user server never starts implicitly — the flag must be exactly
`1`. The bundled local compose sets it for you; you'll see this when running
`python -m precis_mcp.server` from a checkout without it.

### "MCP_DEV_KEY must be at least 32 characters of entropy."

Deliberate floor — a short key on a no-per-user-auth server is a guessable
password. `openssl rand -hex 32` produces 64 chars.

### "PRECIS_AUTH_MODE=… is invalid" — or the dev server refuses to start

Two different cases:

- *Invalid value*: the selector takes `devkey`, `keycloak`, or `oidc`
  ([identity modes](../deployment/oauth-keycloak.md)).
- *The dev server refuses while the value is valid*: `keycloak`/`oidc` mean
  this host is configured multi-user, and the dev entrypoint
  (`precis_mcp.server`) deliberately won't run on it — the multi-user server
  is `precis_mcp.app_open`. This is the cross-refusal working as designed.

### `ModuleNotFoundError: No module named 'precis_mcp'`

You ran a `python -m precis_mcp.*` command on the host. Run it **inside the
server container** — `docker compose -f deploy/docker-compose.local.yml exec
precis-mcp python -m …` (multi-user: `-f deploy/docker-compose.yml`) — or
from a source checkout with the package installed
([quickstart](../getting-started/quickstart.md#2-populate-the-demo-model)).

### `PermissionError` reading `/app/instance/…`

The container runs as a non-root user (uid 10001). A bind-mounted instance
directory (or known_hosts file) must be readable by that uid on the host:
`chmod -R o+rX <your-instance-dir>`.

### Discovery works, but every metric returns no data

Schema provisioning (`clickhouse_init`) creates the *schema only*.
`list_scenarios` and `list_kpis` answer from the catalogue and registry;
figures need rows. Generate the demo dataset
([quickstart step 2](../getting-started/quickstart.md#2-populate-the-demo-model)),
load your own data ([ingestion](../configuration/ingestion.md)), or deploy
the multi-user bundle with sample data (`--data-mode bundle-sample`,
[data modes](../deployment/clickhouse-data-modes.md)).

## ClickHouse and the model

### Connection refused / authentication failed on ClickHouse

Check which side of the network boundary you're on: **inside** the compose
network the host is the service name (`CHHOST=clickhouse`); **from the host**
it's `127.0.0.1`. Credentials: the bundled service creates its user and
password from `CHUSER`/`CHPASSWORD` **at first start only** — the data
volume persists them, so changing the variables later doesn't change the
database. Either reset the volume (destroys data) or alter the user inside
ClickHouse.

### `FAIL view:… — domain '…': semantic.v_… not found in ClickHouse`

The preflight (`clickhouse_init --scope open --check`) found a catalogue
domain whose `source_view` doesn't exist as a view. Usually: you edited or
added SQL under `instance/semantic/` and didn't re-run the provisioner
(`python -m precis_mcp.clickhouse_init --scope open`) — or the container is
mounting a different `instance/` than you edited (`PRECIS_INSTANCE_DIR`).

### "semantic.scenarios is empty"

The scenario registry has no rows, so the engine can't resolve any scenario.
Declare at least the scenario your actuals live under in
`instance/scenarios.yml` and re-run the provisioner
([schema contract](../configuration/clickhouse-schema-contract.md#semanticscenarios-the-scenario-registry)).

### `CatalogueError: … 'source_filter' is no longer supported`

Intentional: raw-SQL filter strings are rejected at load. Rewrite as a
structured `where:` predicate list — same semantics, portable across
backends ([contract](../configuration/adding-metrics-and-dimensions.md#invariants)).

### The catalogue loads, but a query fails with an unknown column

`load_catalogue()` validates references *inside* YAML; it cannot prove a
`source_column` or `where.column` exists on the physical view — that
surfaces at query time. Compare the metric's columns against the view's
actual output. If the missing column is `commit_id`: domains default to
`versioned: true`, which requires it — actuals-only domains must declare
`versioned: false`
([trap list](../configuration/adding-metrics-and-dimensions.md#failure-modes-and-traps)).

## Sign-in and permissions (`/mcp`)

### 401 "Missing Authorization header" / "Invalid token"

No bearer token, or a token the verifier rejects (wrong issuer, expired,
signature). Run `python -m precis_mcp.admin_cli check-auth` to confirm the
issuer/JWKS/audience configuration, and set `PRECIS_AUTH_PREFLIGHT=1` so a
misconfigured deployment fails at boot instead of per request.

### 403 "Token identity claim missing or unmatched"

The token verified, but the claim named by `PRECIS_IDENTITY_CLAIM` (default
`precis_user_id`) is absent — or its value matched no row in the column
named by `PRECIS_IDENTITY_COLUMN`. Mode B: the user's Keycloak record lacks
the `precis_user_id` attribute (brokered users:
[step 3 of the walkthrough](../deployment/keycloak-brokering.md#step-3-carry-a-stable-identity-into-the-token)).
Mode C: the IdP isn't emitting the claim — per-IdP setup in the
[recipes](../deployment/external-idp-recipes.md).

### 403 "User '…' not provisioned"

Sign-in succeeded; the user doesn't exist in Précis-MCP. This is the
provisioning gate working — existence in the IdP grants nothing. Create the
user and assign a profile
([Remote access](../deployment/oauth-keycloak.md#create-the-first-admin-and-provision-users)).

### Connector shows only master-data and ops tools — no `run_metric` / `run_statement`

The client connected and authenticated, but its tool list is missing every
scenario-scoped tool (`run_metric*`, `run_statement*`, `inspect_rows`,
charting). `tools/list` deliberately hides scenario-scoped tools from a user
with **no scenario grants** — almost always a user that was created but never
[assigned a profile](../configuration/user-profiles.md):

```bash
python -m precis_mcp.admin_cli show-user --id <id>     # assignment present?
python -m precis_mcp.admin_cli assign --user <id> --profile <profile>
```

The client caches the tool list per session — reconnect (or start a new
conversation) after assigning.

### "No access to scenario '…'" — or `list_scenarios` returns nothing

The user authenticates but their profile grants nothing (no profile, or no
pattern matches the scenario). An empty `list_scenarios` almost always means
**no profile assigned**. See
[User profiles & permissions](../configuration/user-profiles.md) — and mind
the trap that a typo'd `allow:` list locks out everything it meant to grant.

### claude.ai / ChatGPT can't connect (mode C)

Their connectors self-register via DCR, which Okta / Entra / PingOne don't
offer — this is an IdP limitation, not a configuration error. Route those
deployments through the bundled Keycloak
([mode B brokering](../deployment/keycloak-brokering.md)).

### "Tool not exposed over MCP: …"

The tool exists but its catalogue entry doesn't opt into the `/mcp` surface
(`mcp_read`) — publishing is deliberate, per tool. The advertised set is the
[MCP tool reference](../reference/mcp-tools.md); the opt-in mechanics are in
[Adding read tools](../development/adding-read-tools.md).

## Ingestion

### A load failed — which bucket, and how to recover?

`load_history.status` tells you where in extract → validate → swap it
stopped; `control_total_result` carries any data-quality detail. Read both
with `get_load_status`. Each bucket and its fix:

- **`failed_extract`** — the source query failed (connectivity, SQL,
  credentials), or a `reconcile` check's `source_query` couldn't run. Fix the
  source / query / credentials and re-run.
- **`failed_validation`** — **zero rows** extracted; refused on purpose so an
  empty slice never replaces live data. Check the `:period` filter and that
  the source has data for that period, then re-run.
- **`failed_recon`** — staging/live **column-shape** mismatch (the structural
  gate), or a check that couldn't be evaluated. Fix the extract query's
  projection or the live DDL (re-apply it), or the broken check SQL; re-run.
- **`failed_checks`** — a data-quality **error** check tripped: the data is
  wrong and **nothing landed** (no swap). `control_total_result` names the
  failing check and its failing count / reconcile gap. Fix the source data
  (or the check rule, if that's what's wrong) and re-run. Warnings never land
  here — a warning load *succeeds* (`status='success'`) with
  `verdict: succeeded_with_warnings` recorded for review.
- **`failed_swap`** — the atomic swap failed mid-promote (a ClickHouse error).
  Staging holds the rows; live is untouched. Investigate ClickHouse (disk, or
  an engine-spec mismatch between `live.<x>` and `staging.<x>`) and re-run.
- **`failed_other`** — usually the per-target lock (another load for the same
  binding was in flight). Wait for it, or find it with `list_load_history`.

Every failure is recoverable by **re-running the same `(binding, period)`** —
the swap is idempotent (REPLACE PARTITION / EXCHANGE replaces in place), so a
re-run never double-loads. Nothing reached `live.*` unless the status is
`success`. The
[status table](../configuration/ingestion.md#verifying-a-load) is the full
reference.

### A load fails with "needs the optional '…' driver"

The source's `kind` (Snowflake, BigQuery, Databricks, MSSQL) needs a driver
that ships as an optional extra. Install it where the load runs — `pip install
'precis-mcp[snowflake]'` (or `[bigquery]` / `[databricks]` / `[mssql]`) — then
restart the process (server, scheduler, or watcher). In a compose deployment,
add the extra to the image and rebuild. Postgres and file-drop sources need no
extra.

### Startup warns `secret_ref_missing` for a source

The `<SECRET_REF>_*` env vars aren't visible to **that process**. The
server, the scheduler daemon, and the watcher daemon each read their own
environment — set the variables wherever each runs
([credentials](../configuration/ingestion.md#credentials)). For a file-drop
source the warning is expected and harmless (no credentials to resolve).

### Edits under `instance/integrations/` don't take effect

The daemons read the registry at process start and have no reload hook —
restart them after any change. Restart the server too. A failed reload keeps
the previous registry active (atomic), so nothing is broken while you fix
the YAML.

### "SFTP host-key verification is not configured"

The SFTP drop store refuses to connect until it can verify the server's
host key. Set `PRECIS_SFTP_KNOWN_HOSTS` to a known_hosts file path
(bind-mounted into the container) or `PRECIS_SFTP_HOST_KEY` to the server's
public key inline — capture either with
`ssh-keyscan -p <port> <host>`. A mismatch at connect time
(`paramiko.ssh_exception.SSHException`) means the server's key changed or
you are not talking to the server you pinned — re-keyscan only after
confirming the change is legitimate.

### The watcher never picks up a file

Three usual causes: the filename doesn't match the binding's `file_glob`;
the `filename_regex` doesn't extract a period (for `period_from:
filename_regex`); or you didn't wait a tick
(`PRECIS_WATCHER_INTERVAL_SECONDS`, default 30).

## Clients and widgets

### The table renders as raw JSON instead of a widget

Widgets need a host that supports MCP Apps **and** a built bundle — a widget
is only advertised when its bundle exists, and hosts without the extension
get the same figures as structured JSON. Nothing is lost but the rendering.

### A structured argument is rejected with a shape error

Some models double-encode lists/dicts as JSON strings. The standard
parameter names (`metrics`, `dimensions`, `scenarios`, `filters`, …) are
un-mangled server-side and return corrective errors the model can act on;
custom tools with non-standard parameter names must cope themselves
([coercion](../development/adding-read-tools.md#the-catalogue-entry)).

## Related

- [Ingestion & data sources](../configuration/ingestion.md) — the full
  status reference and pipeline detail.
- [Adding metrics & dimensions](../configuration/adding-metrics-and-dimensions.md)
  — the model-change failure modes and traps.
- [Remote access](../deployment/oauth-keycloak.md) /
  [External IdP recipes](../deployment/external-idp-recipes.md) — the
  identity configuration these symptoms trace back to.

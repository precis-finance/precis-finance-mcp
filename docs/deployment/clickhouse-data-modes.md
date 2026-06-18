# ClickHouse: bundled or your own (data modes)

Précis-MCP reads your financial model from ClickHouse. You make two decisions
about that ClickHouse: **where it runs** (a bundled one this stack starts for you,
or your own cluster) and **what's in it** (an empty schema you'll feed with your
own ingestion, or the synthetic demo data for a trial). Those two decisions are
packaged as one setting, `PRECIS_DATA_MODE`, with three values:

| Mode | ClickHouse | Contents | Use it when |
|---|---|---|---|
| `bundle-sample` | bundled (this stack runs it) | synthetic demo data | you want a multi-user server with data to explore (for a local single-user trial, the [quickstart](../getting-started/quickstart.md) is the shorter path) |
| `bundle-empty` | bundled (this stack runs it) | empty schema, ready for your ingestion | you don't run a ClickHouse, but you have your own data to load |
| `byo` | **your own** cluster | empty schema, ready for your ingestion | you already run ClickHouse |

The fourth combination — your own cluster + synthetic data — isn't offered as a
mode (you wouldn't load demo data into a production cluster), but it's reachable
by setting the underlying switches directly (see [Advanced](#advanced-the-two-switches-underneath)).

`PRECIS_DATA_MODE` is independent of how users sign in
([identity modes](oauth-keycloak.md)) — pick one of each.

---

## Choosing a mode at deploy

`deploy-mcp.sh` runs from your workstation and deploys to a **remote host**:
it rsyncs the working tree to the box (default SSH alias `precis-mcp`,
override with `--server <host>`; provisioned by
`scripts/install-precis-mcp.sh`) and drives `docker compose` there against
`deploy/docker-compose.yml`. It takes the mode as a flag:

```bash
# Try it with demo data (bundled ClickHouse, synthetic):
bash scripts/deploy-mcp.sh --data-mode bundle-sample

# Bundled ClickHouse, your own model, empty and ready to ingest into:
bash scripts/deploy-mcp.sh --data-mode bundle-empty

# Your own ClickHouse cluster:
bash scripts/deploy-mcp.sh --data-mode byo
```

(Equivalently, set `PRECIS_DATA_MODE` in the environment. Running `deploy-mcp.sh`
with no mode just redeploys the code without re-provisioning — useful once a box
is already set up.)

What each mode does, under the hood:

- It sets `COMPOSE_PROFILES` (whether the bundled `clickhouse` service starts) and
  `CHHOST` (where the app connects) in `deploy/.env`.
- It runs the right provisioner **before** the server comes up:
  `bundle-sample` runs the synthetic generator
  (`python -m precis_mcp.sample_data` — the same command the
  [quickstart](../getting-started/quickstart.md) runs locally); `bundle-empty`
  and `byo` run the schema provisioner, `clickhouse_init` (see below).

---

## Re-running from scratch (`--teardown`)

The database and Keycloak passwords are generated **once** (into `deploy/.env`)
and then baked into the postgres / clickhouse **data volumes** at first init.
Those volumes live outside the deploy directory, so wiping the code dir — or
deleting `deploy/.env` and re-deploying — mints *fresh* secrets that no longer
match the surviving volumes. Nothing breaks immediately (the running containers
keep the original secrets), but the next container recreate fails to
authenticate. To re-initialise cleanly, drop the data volumes too:

```bash
bash scripts/deploy-mcp.sh --teardown                  # stop the stack + remove its data volumes
bash scripts/deploy-mcp.sh --data-mode bundle-sample   # fresh install
```

`--teardown` keeps `deploy/.env` (the next deploy reuses the same secrets
against fresh volumes — consistent by construction); delete it as well if you
want entirely new secrets. As a safety net, a normal deploy **refuses to mint
new secrets when the data volumes already exist but `deploy/.env` is missing**,
pointing you at `--teardown` instead of silently creating a mismatch.

> The single-user local trial (`deploy/docker-compose.local.yml`) is a separate
> Compose project — tear it down with
> `docker compose -f deploy/docker-compose.local.yml down -v`.

---

## Bring your own ClickHouse (`byo`)

Two things you provide:

1. **Connection** — set `CHHOST` (and `CHPORT`/`CHUSER`/`CHPASSWORD` as needed) in
   `deploy/.env` to a ClickHouse reachable from the Précis-MCP container. The
   bundled `clickhouse` service is **not** started in this mode. `deploy-mcp.sh`
   refuses to provision `byo` until `CHHOST` is set. **TLS:** if your cluster is
   remote, managed, or **ClickHouse Cloud**, set `CHSECURE=true` (the default
   port becomes 8443; `CHCACERT=/path/ca.pem` to pin a CA, `CHVERIFY=false` only
   for a self-signed dev cert). ClickHouse holds the full plan+actuals model, so
   a non-co-located link must not be plaintext.
2. **Schema** — your cluster starts empty. The provisioner creates the databases,
   tables, views, and scenario registry the engine needs, from your instance
   config. What exactly it must contain is the
   [ClickHouse schema contract](../configuration/clickhouse-schema-contract.md).

Loading your actual figures into those tables is a separate step —
[ingestion](../configuration/ingestion.md).

---

## The schema provisioner (`clickhouse_init`)

`bundle-empty` and `byo` provision the schema with:

```bash
python -m precis_mcp.clickhouse_init --scope open
```

It is the ClickHouse counterpart of a database migration: it reads your
`instance/` (the live-table DDL, the scenario registry, the semantic views) and
applies them to ClickHouse, in the order they depend on each other. It is
**idempotent** — re-running it against an already-provisioned cluster reconciles
rather than clobbering, so it's safe to run again after you edit your model.

It is **schema-only**: it creates the structures; it does not load data. Your
ingestion fills them.

Two flags worth knowing:

```bash
# Show the plan without touching ClickHouse:
python -m precis_mcp.clickhouse_init --scope open --dry-run

# Preflight: confirm the deployment is coherent and ready to serve:
python -m precis_mcp.clickhouse_init --scope open --check
```

`--check` validates **without applying** — your catalogue parses, the semantic
views it names exist in ClickHouse, and the scenario registry is populated. Run
it before go-live (or in CI) to catch a misconfiguration before a client hits it,
rather than after. It exits non-zero if anything is off.

---

## Advanced: the two switches underneath

`PRECIS_DATA_MODE` is a convenience over two independent settings in
`deploy/.env`, which you can set yourself:

- `COMPOSE_PROFILES=bundled-clickhouse` (bundled) or empty (your own).
- `CHHOST` — the host the app connects to.
- `CHSECURE=true` — TLS to a remote/BYO-cloud cluster (default port → 8443; `CHCACERT` / `CHVERIFY` optional).

The provisioner you run is likewise your choice (`clickhouse_init` for a
schema, `python -m precis_mcp.sample_data` for demo data). So the un-named
combination — your own cluster *with* demo data, e.g. a populated demo on
infrastructure you already run — is just "empty `COMPOSE_PROFILES` +
`CHHOST=<yours>`, then run the synthetic generator." The mode preset is the
front door, not the only door.

---

## Related

- [ClickHouse schema contract](../configuration/clickhouse-schema-contract.md) —
  what your ClickHouse must contain.
- [Ingestion & data sources](../configuration/ingestion.md) — getting your data
  into it.
- [Catalogue & semantic model](../configuration/catalogue-and-semantic.md) — the
  `instance/` config the provisioner reads.
- [Remote access — sign-in & identity modes](oauth-keycloak.md) — the orthogonal
  identity axis.

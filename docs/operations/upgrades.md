# Upgrading

## Artifact rename in 0.2.4

Release 0.2.4 moves globally published artifacts to the collision-free
`precis-finance-mcp` name. Compose files in the release already use
`ghcr.io/precis-finance/precis-finance-mcp`; `PRECIS_MCP_TAG`, Compose service
names, and the `precis_mcp` Python import stay unchanged.

If you installed the Python distribution directly, remove the old
Précis-built distribution before installing the new one:

```sh
pip uninstall precis-mcp
pip install precis-finance-mcp
```

The installed admin commands are now `precis-finance-mcp-admin` and
`precis-finance-mcp-clickhouse-init`.

The public repository advances through sync commits. Read `CHANGELOG.md` for
new functionality, deployment requirements and client-integration changes.
Deploy a published release or build from your chosen source revision.

## Multi-user bundle

### Upgrade to 0.4.0: Databricks and live Excel formulas

Update to the matching release files and application image. Apply pending open
PostgreSQL migrations before starting the server, pull updated bundled service
images and rebuild PostgreSQL and bundled Caddy. Compose 2.17+ builds the small
PostgreSQL APT patch layer embedded in the release Compose file; this preserves
PostgreSQL 16 and its data volume. Preserve your `instance/`, secrets and volumes.
See `CHANGELOG.md` for the full release requirements.

When reusing a PostgreSQL volume across an image OS, glibc or ICU change, apply
the [collation compatibility check](#postgresql-collation-compatibility) before
accepting the upgrade. A fresh volume initialized by the current image does
not need this repair.

- **Hierarchies:** to adopt materialized mappings, run
  `precis-finance-mcp-admin hierarchy refresh --provision` with operator
  credentials. Customer-managed pipelines must refresh after hierarchy changes.
  Follow the [hierarchy provisioning and refresh contract](../configuration/analytical-client.md#explicit-hierarchy-provisioning-and-refresh)
  for permissions and coordinated publication.
- **Excel:** update the bundle and hosted manifest (`1.0.0.9`) together.
  Regenerate older exports/live markers lacking region and context-footer
  metadata. Remove the former tenth decimal-mode argument from financial
  formulas; use `decimals` for display precision. Selected conversion requires
  ExcelApi 1.13+. See [Excel setup and conversion](../excel/index.md) and the
  [function reference](../excel/functions.md).
- **Databricks:** build with `PRECIS_EXTRAS=databricks-primary` and follow
  [backend setup](../configuration/analytical-client.md#docker-deployment-with-databricks).
  Include `-f deploy/docker-compose.databricks.yml` alongside the multi-user
  base file in Compose upgrade commands; the deployment helper requires
  `--data-mode databricks` on subsequent deployments too. Provision warehouse
  inputs before startup; keep the serving identity read-only.
- **Bundled Keycloak:** reconcile the scoped user-provisioning client as
  described in [sign-in and identity modes](../deployment/oauth-keycloak.md).

### Upgrade to 0.3.0: charts and file exports

Version 0.3.0 adds chart creation and downloadable JSON/Excel extracts to the
standalone MCP server. It needs **Valkey**, PostgreSQL artifact tables and a
persistent file volume. The supplied Compose files configure these dependencies;
the application image includes the new Python packages (`redis` and `quickjs-ng`)
and built widgets. An application tag change or restart alone is insufficient.

The commands below use the published 0.3.0 image. For source builds, use the
alternative below instead.

1. Back up using [Backups & restore](backups.md). Keep your existing Compose
   project name and data volumes.
2. Update your **open-repository** checkout to the selected release or source
   revision, including `deploy/`, Dockerfile, locks and migration scripts.
   Preserve your existing `instance/` and environment files. For a published tag:

   ```sh
   git fetch origin --tags
   git checkout v0.3.0
   ```

3. Add these settings to your existing `deploy/.env`, retaining its database,
   identity-provider, domain and `COMPOSE_PROFILES` settings:

   ```dotenv
   PRECIS_MCP_TAG=0.3.0
   ARTIFACT_SIGNING_KEY=<paste-a-new-random-key-here>
   PRECIS_BASE_URL=https://your-mcp.example.com
   PRECIS_MCP_UPLOADS_ENABLED=false
   ```

   Generate the artifact key once with `openssl rand -hex 32`, then save it in
   that file. Use a distinct key from OAuth/database credentials and keep it
   stable across restarts. Retain an existing artifact key during routine
   upgrades. Use your actual public HTTPS origin for `PRECIS_BASE_URL`.
4. With your own ingress, update artifact routes from
   `deploy/nginx/mcp.precis.finance.conf` or `deploy/caddy/Caddyfile`: forward
   `/api/artifacts/` to MCP and suppress transfer-URL logging. Retain your
   hostnames, certificates and OAuth routes. Reload the ingress after upgrading;
   restarting MCP does not reload an external proxy.

Standard 0.2.x open installations have no previous artifact registry. Migration
`008_artifacts.sql` adds it without resetting users, profiles or analytical
data. Current source also includes `009_upgrade_legacy_artifacts.sql` to add
missing version, provenance and access metadata to the known legacy registry.
It preserves file IDs and blob paths. Legacy generated files without recorded
read grants remain stored but unavailable; regenerate them under current
permissions. Untouched user uploads retain owner-only access. Existing expiry
and deletion rules continue to apply. Already-current artifact metadata is
preserved. Review unrelated custom schemas separately; do not drop a table
merely because it exists. A previously published image containing only `008`
does not include this forward fix; select a source revision or release that
contains `009` when upgrading a legacy registry.

From the open checkout on the deployment host, install the new dependency and
explicitly apply and verify migrations:

```sh
mcp_compose() { docker compose -f deploy/docker-compose.yml --env-file deploy/.env "$@"; }
mcp_compose stop precis-mcp
mcp_compose pull precis-mcp migrate redis
mcp_compose build postgres
mcp_compose up -d --wait postgres redis
mcp_compose run --rm --no-deps migrate
mcp_compose run --rm --no-deps migrate python scripts/migrate.py --scope open --status
```

The `redis` service runs the pinned Valkey image. `migrate` runs
`python scripts/migrate.py --scope open` against the platform database; only
pending migrations apply. The status command must show `008_artifacts.sql` as
`applied`, along with `009_upgrade_legacy_artifacts.sql` when deploying the
forward-fix revision. Stop if migration execution fails.

The release also refreshes PostgreSQL 16's Debian packages and the application
base image. Build PostgreSQL explicitly as above; an application-only update
leaves the database image's packages unchanged. This remains a same-major
PostgreSQL upgrade using your existing data volume. For the bundled proxy,
rebuild Caddy from the updated Dockerfile before starting the stack:

```sh
mcp_compose build caddy
```

Skip this build for external ingress. Current source pins Go 1.26.10 because
the builder image's bundled toolchain alone does not include the required
security patches. Native Python installations need their host OS packages
and external ingress patched separately; installing a Python wheel does not
update them.

For **bundled Keycloak** (`bundled-keycloak` in `COMPOSE_PROFILES`), explicitly
reconcile existing clients' audience scope before starting the app:

```sh
mcp_compose run --rm keycloak-realm-apply
```

Skip that command for external OIDC. Then start the configured stack:

```sh
mcp_compose up -d
mcp_compose ps
```

Compose creates the new `user_data` file volume and starts Valkey. The server
waits for its migration and Valkey health. No host-side `pip install` is needed
with the published image. With **bundled Caddy**, reload its updated routes:

```sh
mcp_compose exec caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
```

For your own ingress, validate and reload it through your normal deployment
procedure instead.

**Source builds / optional warehouse drivers:** replace the `pull` command above
with the following, then run the same migration/startup sequence:

```sh
mcp_compose pull redis
mcp_compose build postgres
mcp_compose build precis-mcp migrate
```

The image build installs `requirements.lock`, including Redis/QuickJS, installs
the open package and builds widgets from the checked-out source revision.
For warehouse drivers, retain `PRECIS_EXTRAS` in `deploy/.env` (for example,
`snowflake` or `bigquery,snowflake`) and build from source: the standard image
excludes optional drivers. Keep this release's `constraints-extras.txt`, which
fixes the Snowflake advisory and aligns shared dependencies with the core lock.

**Verify:** check readiness from the deployment host:

```sh
curl --fail --silent --show-error http://127.0.0.1:8769/readyz
```

PostgreSQL, ClickHouse, Redis and artifact checks must report `ok`. Reconnect
your client if it cached old tools or authorization scopes. Run a query, export
its `data_ref` through `export_dataset` to both `json` and `xlsx`, and open the
links. Renew through `get_file_download`; test a chart through
`eval_chart_transform` with `data_refs=[data_ref]`. With OAuth, also verify a
tool call after access-token expiry. Receiving tools should be absent while
uploads are disabled; exports/downloads remain enabled. See
[Files and agent hosts](../reference/files-and-hosts.md) for client limitations.

### Upgrade procedure

1. **Back up first.**

    !!! warning "There are no reverse migrations"
        Rolling back an upgrade means restoring the pre-upgrade bundle. You
        have a one-command chain — [`backup run`](backups.md). An upgrade
        you can't roll back from is a bet, not a procedure.
2. **Take the new version.** Two paths, matching how you deploy:
   - *Pinned release (the default):* set `PRECIS_MCP_TAG` to the new version and
     re-pull (see [Pinned-release mode](#pinned-release-mode-pull-instead-of-build)
     below), or `scripts/deploy-mcp.sh --tag <version>` from your workstation —
     it pulls the published application image and builds the PostgreSQL patch.
   - *Rolling `main` / fork:* `git pull` on the deployment checkout, or
     `scripts/deploy-mcp.sh --build` from your workstation (it rsyncs the tree
     to the box and builds there).
3. **Bring the stack up** (the rolling-`main` / build path; the pinned-release
   path uses `up -d` without `--build`):

   ```bash
   docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build
   ```

   This does the upgrade in the right order on its own: the `migrate`
   service applies any new numbered Postgres migrations and must complete
   before the server (and the daemon/backup sidecars) start; services
   whose image changed are recreated, which is also how the
   scheduler/watcher/backup sidecars pick up new code.
4. **If the changelog entry touches `instance/` shapes** (new DDL, semantic
   conventions), re-run the idempotent provisioner:

   ```bash
   docker compose -f deploy/docker-compose.yml --env-file deploy/.env exec precis-mcp \
     python -m precis_mcp.clickhouse_init --scope open
   ```

5. **Verify:** `GET /health` returns ok; `python -m precis_mcp.admin_cli
   check-auth` passes; `clickhouse_init --scope open --check` passes; one
   known metric returns the expected number through a client.

## Single-user (quickstart) stack

For 0.3.0, update to the matching release files and back up as above. Keep your
existing `MCP_DEV_KEY` and database settings in the environment file used by the
local stack. Add a distinct `ARTIFACT_SIGNING_KEY`, set `PRECIS_MCP_TAG=0.3.0`,
and use `PRECIS_BASE_URL=http://127.0.0.1:8768` for a client on the same machine.

```sh
local_mcp_compose() { docker compose -f deploy/docker-compose.local.yml --env-file deploy/.env "$@"; }
local_mcp_compose stop precis-mcp
local_mcp_compose pull precis-mcp migrate redis
local_mcp_compose build postgres
local_mcp_compose up -d --wait postgres redis
local_mcp_compose run --rm --no-deps migrate
local_mcp_compose run --rm --no-deps migrate python scripts/migrate.py --scope open --status
local_mcp_compose up -d
```

For a source candidate, replace the application pull with
`local_mcp_compose pull redis`, `local_mcp_compose build postgres`, followed by
`local_mcp_compose build precis-mcp migrate`. If you use your own instance,
include `-f deploy/docker-compose.instance.yml` in the function and preserve
`PRECIS_INSTANCE_DIR`. The local stack remains host-local; a remote agent host
cannot download a URL pointing to your machine's loopback address.

Re-run the provisioner only if the changelog says the release changes
`instance/` shapes; the new chart/file features alone do not require it.

## Direct Python installations

Stop your MCP process and update the open checkout to the release revision.
In its Python 3.12 environment, install the complete dependency lock and package:

```sh
python -m pip install --upgrade --require-hashes -r requirements.lock
python -m pip install --upgrade --no-deps .
```

This installs the new Redis client and QuickJS runtime; a file sync or process
restart does not install them. Use an existing Redis-compatible service or
start the same pinned Valkey used by Compose, bound to loopback:

```sh
docker run -d --name precis-mcp-valkey --restart unless-stopped \
  -p 127.0.0.1:6379:6379 \
  valkey/valkey:7.2.15-alpine@sha256:f8c1a646f4b31ae4dc3980ff06723a20b533ea1123b9796d450d78ed53ad6d3e \
  valkey-server --maxmemory 256mb --maxmemory-policy noeviction --save "" --appendonly no
```

Configure the MCP process with `REDIS_HOST=127.0.0.1` for that local service,
the distinct `ARTIFACT_SIGNING_KEY`, the appropriate `PRECIS_BASE_URL`, and a
persistent `USER_DATA_DIR` writable by the service account. Retain your normal
PostgreSQL/ClickHouse and identity settings. Export your platform database
connection settings (`PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD` or its file-backed
equivalent, and `PLATFORM_DB_NAME`) in the migration shell, then run:

```sh
python scripts/migrate.py --scope open
python scripts/migrate.py --scope open --status
```

Check that `008_artifacts.sql` and, for a revision including the forward fix,
`009_upgrade_legacy_artifacts.sql` are applied, then restart through your normal
process manager and perform the readiness/export/chart checks above. Update
and reload your ingress as well if its artifact routes were absent.

## Pinned-release mode (pull instead of build)

PostgreSQL is an APT-only layer over a digest-pinned upstream PostgreSQL 16
Bookworm image. Compose 2.17+ builds it from the embedded Dockerfile, preserving
the single-file quickstart and existing database volume. The layer changes only
APT packages; PostgreSQL binaries and libc remain unchanged. Build it even when
pulling the published application. The first APT commands disable retries to
prevent the package-verification vulnerability while upgrading APT itself.

The steps above build the app image from the synced source — the right choice
when you track rolling `main`. To run a **pinned, pre-built release** instead,
set `PRECIS_MCP_TAG` to a published image version and bring the stack up
*without* `--build`:

```bash
PRECIS_MCP_TAG=0.4.0 docker compose -f deploy/docker-compose.yml --env-file deploy/.env pull precis-mcp migrate
docker compose -f deploy/docker-compose.yml --env-file deploy/.env build postgres
PRECIS_MCP_TAG=0.4.0 docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d
```

This explicitly pulls `ghcr.io/precis-finance/precis-finance-mcp:<tag>`.
Upgrading is then bumping
`PRECIS_MCP_TAG` to a newer release and re-running `up -d`; the application
does not need a source build. The
bundled ClickHouse / Keycloak images are unaffected either way; they
stay digest-pinned in the compose file. Still read `CHANGELOG.md` first, and
back up (step 1) — a tag bump is an upgrade like any other.

For 0.2.7, sync the matching Compose manifest as well as changing the app tag:
the new manifest updates bundled Keycloak and switches ClickHouse to the
official 26.3 Alpine variant. A tag bump alone leaves both old images running.
Back up the ClickHouse volume before recreating that container and verify the
data and health check after it starts.

## PostgreSQL collation compatibility

Keeping PostgreSQL's major version unchanged does not guarantee compatible
text ordering. Reusing a data volume across a Debian release, glibc or ICU
change can leave indexes ordered under different rules from the running
server. This applies to library downgrades as well as upgrades. Retain explicit
OS and image digest pins, and check compatibility when changing them. The
small APT-only patch layer does not itself replace libc or ICU.
For native PostgreSQL installations, apply the same check after OS or
collation-library updates.

A fresh installation with an empty volume initializes its collation versions
from the current image, so it needs no historical repair. Starting a new
container with an old volume is an upgrade, not a fresh installation.

After starting the selected PostgreSQL image, connect as the database operator
and check database defaults, including `template1`:

```sql
SELECT datname, datcollversion AS recorded,
       pg_database_collation_actual_version(oid) AS actual
FROM pg_database
WHERE datallowconn
  AND datcollversion IS NOT NULL
  AND datcollversion IS DISTINCT FROM pg_database_collation_actual_version(oid);
```

Also run this query in each connectable database; named-collation catalogues
are local to each database:

```sql
SELECT n.nspname, c.collname, c.collversion AS recorded,
       pg_collation_actual_version(c.oid) AS actual
FROM pg_collation c
JOIN pg_namespace n ON n.oid = c.collnamespace
WHERE c.collversion IS NOT NULL
  AND c.collversion IS DISTINCT FROM pg_collation_actual_version(c.oid);
```

Both queries should return no rows. A null actual version needs investigation,
not an automatic metadata refresh. These are operator checks; the deployment
helper does not repair collations automatically.

If versions mismatch, hold upgrade acceptance and schedule maintenance:

1. Pause every writer to the affected cluster, including the application,
   bundled Keycloak and any ingestion workers. Verify that no unexpected
   client sessions remain, then take the final fresh database backup.
2. Inventory objects using the database default and mismatched named collations.
   Rebuild affected indexes using `REINDEX` in each affected database. Review
   other stored dependencies, such as partition boundaries, materialized views
   and generated values; reindexing alone does not rebuild these.
3. Verify rebuilt indexes, for example with PostgreSQL's `amcheck`, and verify
   preserved table contents. Only then run `ALTER COLLATION ... REFRESH VERSION`
   for repaired named-collation dependencies, or unused stale collations, and
   `ALTER DATABASE ... REFRESH COLLATION VERSION` for each repaired database
   default. Recheck versions and invalid indexes before resuming writers and
   verifying readiness and sign-in.

Do not refresh versions merely to silence warnings: refresh changes metadata
without repairing or validating indexes. `REINDEX DATABASE` excludes system
catalogues, so inspect their dependencies too. Repair `template1` where needed
because its mismatch can block new database creation; leave `template0`
unchanged. Repair is a bounded maintenance action after relevant library
changes, not a step on every deployment. See PostgreSQL's
[collation guidance](https://www.postgresql.org/docs/16/sql-altercollation.html),
[REINDEX](https://www.postgresql.org/docs/16/sql-reindex.html) and
[amcheck](https://www.postgresql.org/docs/16/amcheck.html).

## Rolling back

There are no reverse migrations. Rolling back is a **restore**: check out
the previous tag, rebuild, then restore the pre-upgrade bundle —
[Backups & restore](backups.md). This is the reason step 1 isn't optional.

## Temporary Keycloak risk acceptance

The reviewed upstream Keycloak 26.8.1 image retains Guava CVE-2026-102554.
Maintainers accepted this availability risk until **December 5, 2026, 00:00 UTC**,
independent of the next release date. The narrowly scoped CI exception applies
only to the reviewed image digest and Guava JAR/PURL. CI rejects a changed image,
a broader rule or an expired exception. Reachability of attacker-controlled Java
serialization in this deployment has not been disproved. External OIDC does not
run this container. Apply a fixed upstream image once available.

## Dependency and image pinning

What a sync ships is exactly what runs: Python dependencies install from a
hashed lockfile (`requirements.lock`), and the upstream ClickHouse / Valkey /
Keycloak images are digest-pinned (`tag@sha256:…`) in the compose files,
so an upstream re-tag can't change your stack silently. The precis-mcp
application image follows the same principle — published to
`ghcr.io/precis-finance/precis-finance-mcp` and selected by `PRECIS_MCP_TAG` (a release
version by default, pinnable to a `@sha256:` digest), or built from the synced
source when you run `up --build`. The flip side is
that base-image and dependency security fixes do **not** arrive by
re-pulling — they arrive as pin bumps in sync commits. CI gates both Python
lock sets with `pip-audit`, audits both frontend release-builder dependency sets
including build tools (High/Critical gate), scans the application image, scans the exact
ClickHouse / Valkey / Keycloak pins, and builds and scans patched PostgreSQL and Caddy
proxy. If you maintain a fork, regenerate the lockfile with `make lock`, refresh
image digests with `docker buildx imagetools inspect <tag>`, and rerun the same
image scans on your own cadence.

The Excel release builder and its npm gate both use `--omit=peer`, excluding the
Office developer login/sideload CLI that webpack does not use. Ordinary local
`npm ci` installs retain that tooling and its known Microsoft CLI MSAL/ZIP
advisories; CI reports the full developer-install audit separately. No Node
toolchain or Office CLI is copied into the application runtime image.

## Not an upgrade

Editing your own `instance/` files isn't an upgrade and needs no rebuild:
catalogue/semantic changes need the provisioner + a server restart
([modelling contract](../configuration/adding-metrics-and-dimensions.md)),
and `instance/integrations/` changes need the daemons restarted
([ingestion](../configuration/ingestion.md#scheduling)).

## Related

- [Backups & restore](backups.md) — the rollback mechanism.
- [Troubleshooting](troubleshooting.md) — if the post-upgrade verify fails.

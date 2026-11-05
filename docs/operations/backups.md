# Backups & restore

Précis Finance MCP ships a backup subsystem driven by one declarative file,
`instance/backup.yml`: one scheduled run produces a bundle of configured managed
stores — a PostgreSQL dump, a ClickHouse backup when selected/managed, and a tarball of your
`instance/` config — at a local volume or an S3-compatible bucket, with
retention pruning, a checksum manifest, and a restore/drill command. The
shipped tier is **dump** (nightly full backups by default), so your recovery
point equals the backup cadence; a `pitr` tier (continuous WAL archiving) is
reserved in the config and rejected until it ships.

## What a bundle contains

| Store | Artifact | Mechanism |
|---|---|---|
| Platform PostgreSQL (users, profiles, `load_history`, audit tables) | `pg/` | `pg_dump --format=custom` |
| ClickHouse (`live`, `staging`, `semantic`, plus `planning` when present) | `ch/` | server-side `BACKUP DATABASE … TO Disk('precis_backups')` |
| Your model (`instance/` — catalogue, semantic SQL, integrations, scenarios) | `instance/` | tarball, git SHA recorded |
| Manifest (checksums, sizes, package version, per-table row counts) | `manifest/` | written **last** — its presence means the bundle is complete |

Deliberately not in the bundle: **secrets** (your
secret manager is their durability boundary; backup config holds credential
*references* only, and validation rejects inline values). Your `instance/`
git remote remains the source of truth for the model — the bundled tarball
makes a restore self-contained and records exactly which model state was
live.

A store failure degrades the run to `partial` rather than aborting the
bundle, and any non-success outcome can POST to a webhook.

CH backups detect optional `planning` once and record the exact database list
in the existing manifest; row-count baselines and restore/drill operations use
the same list. The backup identity must be able to see and back up that database.
Inventory-query failure fails the CH store rather than silently omitting data.
Open deployments without planning still back up only the original three
databases. Legacy manifests without the list restore only those three and
leave existing planning untouched, including with `--force`; they cannot recover
planning data never captured. Take a new backup after upgrading for that
coverage. There is no new operator-maintained configuration file.

## Configuring

### Databricks and customer-managed warehouse recovery

`PRECIS_ANALYTICAL_BACKEND=databricks` makes warehouse recovery external.
The customer/operator owns recovery of all Databricks data, including Précis
scenario metadata and any application-owned tables stored there. Warehouse
durability alone does not establish a configured customer recovery process.
No Databricks backup/export or warehouse restore is performed by these tools.

Précis continues backing up PostgreSQL and instance configuration, plus files
when `scope.files: managed`. The existing `backup.yml` format is unchanged;
any legacy managed-CH scope is excluded automatically in Databricks mode.
The manifest records `analytical_backend: databricks` and a skipped
`databricks` store with external ownership. No CH client, disk XML or warehouse
credential is needed. `backup init` probes the platform backup destination and
reports the external warehouse boundary instead of rendering CH disk settings.
`backup list --json` includes the recorded backend. Success means the configured
platform stores succeeded; it does not mean warehouse data was backed up.

Use the [Databricks overlay](../configuration/analytical-client.md#docker-deployment-with-databricks)
for all Compose commands in this mode, including backup commands. Its optional
backup sidecar runs as UID 10001, matching the owner of private managed files;
the image initializes the shared `/backups` volume with write access. Existing
volumes retain their existing permissions. The CH sidecar's separate file-reader
limitation remains; this does not change ownership of CH backup directories.

Restore/drill accepts only successful artifacts actually present. It rejects
CH artifacts for a Databricks deployment or bundle **before** artifact fetch or
database operations, including with `--force`. Legacy manifests without the
backend field are treated as CH bundles. Explicit platform-only recovery from
an older bundle is supported with `--stores postgres,instance,files` when all
three have successful artifacts; select only the available stores. A database
drill can use `--stores postgres --drill` and explicitly reports Databricks
recovery as unexercised. Coordinate the recovered platform/config/files point
with the customer-managed warehouse recovery point; these commands do not
orchestrate consistency across them. Recover secrets through the secret manager.

Run the CLI inside the server container
(`docker compose -f deploy/docker-compose.yml exec precis-mcp
precis-finance-mcp-admin backup …`).

**Prerequisites.** The multi-user bundle deployed; for an S3 destination, the
`s3` extra in the image (`pip install ".[s3]"` when building from source) and
two credential pairs — a **writer** that cannot delete objects and a
**reader** for restore — plus, for the ransomware posture, object-lock
(WORM) and a lifecycle policy on the bucket (the bucket policy is yours;
Précis Finance MCP verifies it). Bring-your-own ClickHouse needs the `BACKUP` grant
on the configured user (`GRANT BACKUP ON *.* TO <chuser>`); the bundled
service's default user has it implicitly.

1. **Write `instance/backup.yml`.** The bundled demo instance ships the
   local-volume default. An S3 variant:

   ```yaml
   mode: dump
   destination:
     type: s3
     endpoint: https://s3.eu-central-1.amazonaws.com   # omit for AWS; required for MinIO/R2
     bucket: acme-precis-backups
     prefix: prod
     region: eu-central-1
   credentials:
     writer: BACKUP_WRITER     # → BACKUP_WRITER_ACCESS_KEY_ID(_FILE) + _SECRET_ACCESS_KEY(_FILE)
     reader: BACKUP_READER
   schedule:
     cron: "30 2 * * *"
   retention:
     postgres:   { keep: 14, days: 90 }
     clickhouse: { keep: 14, days: 90 }
     instance:   { keep: 30 }
   scope:
     postgres: managed
     clickhouse: managed
     files: external           # back up artifact blobs separately; see Shared artifacts below
   encryption:
     kms_key_id: null          # SSE-KMS key for process-side puts; null = bucket default
   expect_worm: true
   alert:
     webhook_url: https://hooks.example.com/precis-backup
   ```

   The file holds credential references, never values — safe to commit to
   your instance repository.

   With `files: external`, this bundle excludes artifact blobs. Back up
   `USER_DATA_DIR` separately and restore it together with the matching platform
   database backup; see [Shared artifacts](#shared-artifacts) for the sidecar's
   file-permission limitation.

2. **Set the credentials** in `deploy/.env` (or as `_FILE` secrets):
   `BACKUP_WRITER_ACCESS_KEY_ID`, `BACKUP_WRITER_SECRET_ACCESS_KEY`,
   `BACKUP_READER_ACCESS_KEY_ID`, `BACKUP_READER_SECRET_ACCESS_KEY`. A local
   destination needs none.

3. **Validate:** `precis-finance-mcp-admin backup validate` (static checks).

4. **Render and check:** `precis-finance-mcp-admin backup init`. Writes the
   ClickHouse backup-disk config to `deploy/secrets/precis_backup_disk.xml`
   (for S3 it embeds the resolved writer credential and is written mode
   0600, which is why it lives in the secrets area and is regenerated,
   never hand-edited; a local-destination render holds no secret), probes
   the destination for writability, and checks whether ClickHouse sees the
   `precis_backups` disk — on first run that check warns, expected.

5. **Mount and restart ClickHouse:** set
   `PRECIS_BACKUP_CH_CONFIG=./secrets/precis_backup_disk.xml` in
   `deploy/.env`, `docker compose up -d clickhouse`, re-run
   `backup init` — the disk check now passes. **S3 only:** the 0600 render
   is owned by the CLI's uid, but the clickhouse service reads the mount as
   its own user — transfer ownership first
   (`chown 101 deploy/secrets/precis_backup_disk.xml` for the stock image),
   and re-do that after every credential rotation re-render.

6. **First manual run:** `precis-finance-mcp-admin backup run`, then
   `precis-finance-mcp-admin backup list` to confirm the bundle.

7. **Enable the schedule:** append `backup` to `COMPOSE_PROFILES` and
   `docker compose up -d` — the `backup-scheduler` sidecar fires the same
   code path on the configured cron.

**Credential rotation:** update the secret values → re-run
`backup init` (re-renders the XML) → restart the clickhouse service.

## Restore and drills

!!! warning "A backup that has not been restored is not a backup"
    Run a drill before trusting the chain with production data, and
    quarterly after.

```bash
precis-finance-mcp-admin backup restore --id <run_id> --drill
```

The drill restores into side databases (`precis_platform_drill`,
`live_drill` / `staging_drill` / `semantic_drill`), verifies per-table row
counts against the manifest, records the outcome in `backup_history`, and
drops the drill databases — it never touches live data. (Current limitation:
the drill verifies tables and checksums; restored `semantic_drill` views
still reference `live.*`, so metric-level verification means pointing an app
instance at the restored stores.)

The drill requires a successful PostgreSQL or ClickHouse artifact.
Instance/files artifacts are fetched and checksum-checked where checksums
exist, but reported as skipped because database drills do not exercise
extraction. Selecting only instance/files for a drill is rejected.

A **real restore**:

```bash
precis-finance-mcp-admin backup restore --id <run_id> [--stores postgres,clickhouse,instance] [--force]
```

Checksums are verified before anything is touched; a non-empty target is
refused without `--force`. The `instance` artifact is always extracted
*beside* your live config, never over it — review and swap it in yourself.
Every explicitly requested store must have a successful, keyed artifact in
the bundle; absent, failed, or skipped stores reject the whole selection
before restoring anything. Empty selections are rejected too. Without
`--stores`, available successful stores are selected, even from a partial
bundle: inspect `backup list` to establish which stores were backed up.
After a full host loss, the order is: images + `docker compose up` →
`backup init` (ClickHouse disk) → restore postgres → restore clickhouse →
swap instance config in → restart → drill or a known-scenario metric
spot-check.

## Verifying the chain

- [ ] `backup run` exits 0; `backup list` shows the bundle with every store `success`.
- [ ] The destination holds `pg/`, `ch/`, `instance/`, `manifest/` objects for the run id.
- [ ] `backup restore --id <run> --drill` exits 0 with all row counts matching.
- [ ] `backup_history` (platform PostgreSQL) shows the run and the drill, both `outcome='success'`.
- [ ] The sidecar fires at the next cron tick (`docker compose logs backup-scheduler`).
- [ ] With `expect_worm: true`: deleting a backup object with the **writer** credential fails, and `backup init` verifies object-lock without warning.
- [ ] Webhook configured: stop Postgres, run `backup run`, confirm the POST arrives and the run reports `partial`.

## Disabling

Backups are additive: remove `backup` from `COMPOSE_PROFILES` and
`docker compose up -d`. The ClickHouse disk config can stay mounted (an
unused disk definition is inert). Existing bundles are untouched.

## Related

- [Environment variable reference](../configuration/environment-variables.md)
  — the `BACKUP_*` / `PRECIS_BACKUP_*` variables.
- [Security model](../deployment/security-model.md) — where the
  writer/reader split and the WORM chain sit in the wider posture.
- [Observability](observability.md) — alerting; `backup_history` is the
  outcome record.
- [Troubleshooting](troubleshooting.md)

## Shared artifacts

Artifact identities, upload reservations and retired-blob cleanup metadata live in the platform PostgreSQL database. Managed bytes live in `USER_DATA_DIR`, mounted read-only by the open backup sidecar. Back up and restore these together. Redis snapshots are disposable and excluded: rerun queries after losing or restoring the cache. Renew transfer capabilities for restored files under current grants; expired URLs are not durable recovery identifiers.

**Current limitation:** the shipped sidecar runs as ClickHouse UID 101,
while managed artifact blobs are private to application UID 10001
(mode 0600). The read-only mount does not grant access to these blobs;
`scope.files: managed` is not reliable in this sidecar configuration.
Managed-file coverage requires a reader identity and backup-volume layout
that also preserve ClickHouse checksum reads and retention deletion. Track
and validate that deployment work before relying on the bundle for recovery
of managed artifacts; do not make private blobs world-readable.

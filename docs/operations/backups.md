# Backups & restore

Précis Finance MCP ships a backup subsystem driven by one declarative file,
`instance/backup.yml`: one scheduled run produces a complete, self-contained
bundle — a PostgreSQL dump, a ClickHouse backup, and a tarball of your
`instance/` config — at a local volume or an S3-compatible bucket, with
retention pruning, a checksum manifest, and a restore/drill command. The
shipped tier is **dump** (nightly full backups by default), so your recovery
point equals the backup cadence; a `pitr` tier (continuous WAL archiving) is
reserved in the config and rejected until it ships.

## What a bundle contains

| Store | Artifact | Mechanism |
|---|---|---|
| Platform PostgreSQL (users, profiles, `load_history`, audit tables) | `pg/` | `pg_dump --format=custom` |
| ClickHouse (`live`, `staging`, `semantic`) | `ch/` | server-side `BACKUP DATABASE … TO Disk('precis_backups')` |
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

## Configuring

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
     files: external           # the files store belongs to the Précis platform; keep external here
   encryption:
     kms_key_id: null          # SSE-KMS key for process-side puts; null = bucket default
   expect_worm: true
   alert:
     webhook_url: https://hooks.example.com/precis-backup
   ```

   The file holds credential references, never values — safe to commit to
   your instance repository.

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

A **real restore**:

```bash
precis-finance-mcp-admin backup restore --id <run_id> [--stores postgres,clickhouse,instance] [--force]
```

Checksums are verified before anything is touched; a non-empty target is
refused without `--force`. The `instance` artifact is always extracted
*beside* your live config, never over it — review and swap it in yourself.
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

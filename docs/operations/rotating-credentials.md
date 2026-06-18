# Rotating credentials

## When to run this

- On a routine schedule (your secret-rotation policy).
- Immediately on suspected compromise of any secret below.
- When an operator with access to `deploy/.env` or the host leaves.

## The secrets, and who owns them

| Secret | Used for | Owner |
|---|---|---|
| `KC_BOOTSTRAP_ADMIN_PASSWORD` | Bundled Keycloak admin — the credential the admin CLI and realm-apply use to provision users (mode B only) | Précis (bundled Keycloak) |
| `OIDC_CLIENT_SECRET` | Confidential-client secret for an external IdP (mode C) | Your IdP |
| `PGPASSWORD` / `CHPASSWORD` | Platform DB and ClickHouse connections | Your databases |
| `INGEST_BINDING_JWT_SECRET` | HS256 signing key for binding-scoped ingestion tokens (push API) | Précis |
| `MCP_DEV_KEY` | Single-user local trial only | Local only — not present in a multi-user deploy |

**End-user passwords are never a Précis secret.** In mode B they live in
Keycloak; in mode C they live in your IdP. Précis stores no user credential —
`admin_cli reset-password --no-keycloak` confirms this for a mode-C user.

## Rotate the Keycloak bootstrap admin (mode B)

The bundled Keycloak seeds its admin from `KC_BOOTSTRAP_ADMIN_*` at first
start; after that the password lives in Keycloak's own database, so changing
the env var alone does **not** rotate it. Three steps, in order:

1. **Change the password in Keycloak.** Sign in to the Keycloak admin console
   (reach it over an SSH tunnel; it is not publicly exposed) and reset the
   bootstrap admin user's password. The exact path is per your Keycloak
   version — see Keycloak's own admin docs.
2. **Sync the Précis side.** Update `KC_BOOTSTRAP_ADMIN_PASSWORD` in
   `deploy/.env` (or its `*_FILE` secret) to the new value, so the admin CLI
   and realm-apply authenticate with it.
3. **Redeploy so the new value is in the container env:**
   ```bash
   bash scripts/deploy-mcp.sh --server YOUR_HOST --no-build
   ```

The serving path (JWT verification via JWKS) does not use this secret, so
signed-in users are unaffected during rotation.

## Mode C (external IdP)

Précis holds no admin credential for your IdP — rotate at the IdP:

- **User passwords / MFA:** managed entirely in your IdP. Direct users there.
- **Client secret:** rotate `OIDC_CLIENT_SECRET` at the IdP, update
  `deploy/.env`, and redeploy (`deploy-mcp.sh --no-build`). Verify with
  `admin_cli check-auth`.

## Database and signing secrets

- **`PGPASSWORD` / `CHPASSWORD`:** rotate at the database first, then update
  `deploy/.env` and redeploy. ClickHouse and Postgres connections re-establish
  on the next request (the platform pool opens lazily).
- **`INGEST_BINDING_JWT_SECRET`:** rotating it invalidates outstanding
  binding-scoped ingestion tokens — re-issue them to any external orchestrator
  after rotation.

## Verification

- `admin_cli check-auth` reports `auth conformance: OK`.
- `curl -fsS https://YOUR_DOMAIN/readyz` returns `200` (DB connections
  re-established with the new credentials).
- A signed-in user can still run a tool; provisioning a test user via the
  admin CLI succeeds (mode B), confirming the new Keycloak admin password.

## Rollback or recovery

Rotation is forward-only at the source (you cannot un-change a Keycloak
password). If a redeploy left a stale secret in `deploy/.env`, correct the
value and redeploy again — the source-of-truth is the IdP/database, and
`deploy/.env` is brought back into agreement with it.

## Related runbooks

- [Production deployment — first-run checklist](../deployment/production-checklist.md)
- [Remote access — sign-in & identity modes](../deployment/oauth-keycloak.md)
- [Security model](../deployment/security-model.md)

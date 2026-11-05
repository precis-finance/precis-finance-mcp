---
description: Review the Précis Finance MCP trust model, read-only enforcement, authentication, authorisation, data flows, and deployment responsibilities.
---

# Security model

The trust model in one place, for a security review: what this server can
never do, who is authenticated and authorised by what, where data flows, and
which surfaces need hardening attention. Précis Finance MCP serves **financial data
to AI clients**, so the review questions are predictable — this page answers
them in order.

## Read-only by construction

"Read-only" is enforced at specific points, not promised:

- **No finance-write tools ship in this package.** Finance tools expose
  metrics, statements, row inspection, discovery and ingestion status. Charts
  transform bounded read snapshots. Artifact tools create owned files and
  upload reservations; they do not write financial data.
- **The `/mcp` transport denies finance-write authority.** Reads require the
  catalogue's `mcp_read` flag and a `read`/`general` access class. Artifact
  mutations require `mcp_artifact`, the `general` access class and membership
  in the explicit artifact-tool allow-list. External uploads and attachment
  imports additionally require `PRECIS_MCP_UPLOADS_ENABLED=true`; they are
  hidden and denied by default, including the signed PUT endpoint. Exports and
  downloads remain enabled.
- **File tools use general access.** They need no separate file role or scenario
  scope. Owner, quota and grant-stamp checks still apply. `general` alone does
  not publish a tool; `mcp_artifact` remains a transport flag.
- **Tool publication fails closed.** Every loaded tool must have a catalogue
  entry (startup fails otherwise), and appearing on `/mcp` is a per-tool
  opt-in flag — a new tool is never published by default
  ([Adding read tools](../development/adding-read-tools.md)).
- **The engine reads one surface**: the `semantic.*` views. No raw-table
  access from the query path, no DDL, no writes.

Scope the claim correctly: **read-only describes financial access**, while the AI-facing
artifact API writes file metadata and managed bytes. The package also contains operator-side subsystems
that write by design — ingestion loads into ClickHouse, migrations evolve
the platform database, backups write to your destination. Those run under
*your* control (CLI, daemons, compose services), never reachable through an
MCP client. Treat the deployment as a data platform with a governed
read-only API, not a read-only appliance.

## Authentication

Sign-in posture is one variable (`PRECIS_AUTH_MODE`) with three modes —
detailed in [Remote access](oauth-keycloak.md):

- **Multi-user (`/mcp`)**: OAuth 2.1 + PKCE; clients discover the issuer via
  RFC 9728 protected-resource metadata; tokens must carry the `/mcp`
  audience (RFC 8707 — stamped by the bundled Keycloak, or your IdP's
  equivalent). The issuer is the bundled Keycloak (optionally
  [brokered to your corporate IdP](keycloak-brokering.md), inheriting your
  SSO/MFA/session policies) or your own OIDC IdP trusted directly.
- **Single-user dev server**: a static bearer key behind three deliberate
  gates (explicit enable flag, 32+ character key, localhost bind). It is a
  separate entrypoint, and the two refuse each other's modes: a
  `keycloak`/`oidc`-configured host cannot accidentally start the no-auth
  dev server, and the multi-user entrypoint refuses `devkey`. The dev server
  is never a production surface.
- Set `PRECIS_AUTH_PREFLIGHT=1` to fail at boot on issuer/JWKS/audience
  misconfiguration instead of per request.

## Authorisation

Authentication only identifies. Three further layers decide what a request
may read:

1. **The provisioning gate.** Existence in the IdP grants nothing: a valid
   token whose identity doesn't resolve to a provisioned user is refused
   (`403`). Creating the platform user and assigning a profile is the
   access-granting act, and it stays with your admin.
2. **Profiles** ([full schema](../configuration/user-profiles.md)): scenario
   patterns → role → domain and dimension scope, deny-wins. Scope is applied
   **inside the query** — out-of-scope rows are absent from results,
   hierarchy search, and scenario listings, not filtered client-side.
3. **Gate ordering.** The permission check runs *before* request validation,
   so a denied caller can't probe dimension members through validation
   error messages.

Permissions load **per request** — revoking a profile takes effect on the
user's next call, with nothing to restart. And identity is injected, never
accepted: the parameters carrying user identity and scope are stripped from
every advertised tool schema, so a client cannot supply them.

## The AI client is untrusted input

Treat the connected model as an adversarial caller — the design already
does. A prompt-injected or misbehaving client can only call the advertised
read tools, inside the calling *user's* profile scope; its blast radius is
"read what that user may read". It cannot write (no tools), cannot escalate
(identity injected server-side), and cannot mine internals from errors —
tool failures return generic messages while detail goes to the server log.

## Data in transit, credentials at rest

- **Ingress**: the app is proxy-agnostic. The default is the bundled Caddy
  proxy (`bundled-proxy` profile) — automatic Let's Encrypt TLS for
  `PRECIS_DOMAIN`; drop the profile and TLS terminates at your own ingress.
- **ClickHouse**: `CHSECURE=true` (+ `CHCACERT` to pin) for any non-co-located
  cluster — it holds the full model.
- **Source warehouses** (ingestion and federated reads): set
  `<REF>_SSLMODE=verify-full`, not `require` — `require` encrypts but
  authenticates nothing, and this link carries financial data *and* the
  warehouse credential.
- **Platform PostgreSQL**: `PGSSLMODE`/`PGSSLROOTCERT` for a remote DB.
- **Credentials** resolve from environment variables, every one of them with
  `<NAME>_FILE` indirection for Docker secrets / Kubernetes volumes / Vault
  sidecars. Configuration YAML never holds a secret — a `Source` names a
  `secret_ref` (an env-var prefix), not a credential, and `backup.yml`
  validation rejects inline secrets outright. Reading a binding's
  configuration through any surface exposes no secret material. (One scoped
  exception: the rendered ClickHouse backup-disk config embeds the resolved
  S3 writer credential — ClickHouse server config can't read `*_FILE` — so
  it lives in the secrets-permissioned area at mode 0600 and is regenerated
  on rotation, never edited.)
- **Backups** support a ransomware-resistant chain: separate **writer**
  (no delete permission) and **reader** credentials, WORM object-lock
  expected and *verified* by `backup init` when `expect_worm: true`, and
  optional SSE-KMS under your key —
  [Backups & restore](../operations/backups.md).

## Container posture

The `precis-finance-mcp` image runs as a non-root user (uid 10001), and both compose
bundles drop all Linux capabilities and set `no-new-privileges` on every
service built from it. Two consequences to know about:

- Host paths you bind-mount in (an `instance/` directory, a known_hosts
  file) must be readable by that uid — typically `chmod -R o+rX`.
- The bundled databases keep their stock-image users. ClickHouse runs under
  Docker's default seccomp profile (verified against startup, queries,
  partition swaps, and backup/restore). If it fails to start on an older
  kernel, `security_opt: [seccomp:unconfined]` on the clickhouse service is
  the escape hatch — treat it as a downgrade to report, not a default.

## Exposure surfaces to harden

The honest list — each deliberate, each with a posture:

- **Anonymous client registration on the bundled Keycloak.** Open by design
  so claude.ai/ChatGPT can self-register. Registering a client grants no
  data access (the user behind it still authenticates and must be
  provisioned), but it is an unauthenticated write surface for client
  records. Both the bundled Caddy proxy and the reference nginx config
  rate-limit it per IP (5/min); close it entirely if you don't use the
  public connectors —
  [hardening detail](keycloak-brokering.md#the-dcr-and-admin-surfaces).
  **Unresolved consent risk:** the shipped policy removes Consent Required;
  an existing SSO session may authorize a newly registered, untrusted client
  without explicit approval. Provisioning and profile restrictions limit the
  token's access but do not establish consent to that client.
- **The Keycloak admin console.** Both the bundled Caddy proxy and the
  reference nginx config block `/auth/admin/` at the edge; reach it over an
  SSH tunnel, or relax the block to an IP allowlist. A custom ingress that
  proxies all of `/auth/` publicly leaves the console guarded only by the
  bootstrap admin password — reproduce the block.
- **The dev server** is localhost-bound and key-gated; it has no per-user
  auth and must never be exposed beyond the machine it runs on.

## Audit trail

- **`security_audit_log`** (platform PostgreSQL): one row per MCP session
  start and per tool call — actor, tool, outcome, scenario, truncated error.
  Best-effort by design: an audit-write failure is logged and never blocks
  the response.
- **`inspection_audit`**: row-level drill-through reads are recorded
  separately — who inspected which source, when.
- **`load_history`**: every ingestion attempt, success or failure, is the
  durable record of what landed when ([ingestion](../configuration/ingestion.md)).
- **`backup_history`**: every backup run, restore, and drill, with its
  outcome ([backups & restore](../operations/backups.md)). Written
  best-effort *after* the bundle lands — PostgreSQL is itself one of the
  stores being backed up, so a backup of a degraded platform must still
  succeed; the manifest at the destination, not this table, is the
  bundle-complete signal.
- **Telemetry is off by default**; enabling it emits infrastructure spans
  only. Request/response *content* capture is a second, separate switch
  (`PRECIS_TELEMETRY_CAPTURE_CONTENT`) — leave it off unless your collector
  is trusted with financial data.

## Reporting a vulnerability

Report suspected vulnerabilities privately via the repository's **Security**
tab ("Report a vulnerability" — GitHub private vulnerability reporting), not
in a public issue. The policy is the repository's `SECURITY.md`.

## Related

- [Remote access — sign-in & identity modes](oauth-keycloak.md)
- [Sign in with your corporate IdP](keycloak-brokering.md) — including the
  DCR and admin-surface hardening detail.
- [User profiles & permissions](../configuration/user-profiles.md)
- [MCP tool reference](../reference/mcp-tools.md) — the complete advertised
  surface this model governs.

## Owned artifact transfer

Core artifact storage retains the creating service's filesystem ownership;
new per-user storage directories are private (`0700`). The open package does
not transfer ownership to an execution worker or assume a worker UID. Optional
deployment integrations must provision their own narrowly scoped filesystem
handoff without weakening registry owner/grant checks.

Finance access remains read-only. Explicit artifact tools create owned files and reservations; they do not acquire plan-write or ingestion authority. Signed GET/PUT capabilities have purpose-specific derived keys/audiences, bounded lifetime, live account checks, and owner checks. Generated artifacts, tool-rewritten file versions and datasets also bind current read grants; any change requires a fresh query. Download capabilities bind file version/hash and cannot authorize replacement bytes. Registry reads enforce expiry before GC.

The optional host attachment importer requires exact trusted HTTPS hostnames and public DNS addresses, pins the connection while preserving TLS hostname verification, rejects redirects and credential forwarding, and caps size/time. Uploaded workbook metadata is untrusted input and never grants access. Configure ingress to omit capability query strings from access/error logs; transfer routes are excluded from HTTP telemetry. See [Files and external agent hosts](../reference/files-and-hosts.md).

### Domain allowances in security profiles

Dimension allowances use the same absent-versus-empty distinction: an omitted
axis is unrestricted; an explicit empty member list denies all members on that
axis. Leaf, derived and ragged-hierarchy resolution preserves empty constraints
through query and write-scope enforcement. Allowed dimension clauses intersect,
including different levels resolving to the same leaf; exclusions union and
subtract. Discovery unions permitted sets across readable scenarios only after
each scope has been resolved.

Profile JSON and YAML distinguish an omitted/null `domains.allow` (all domains, subject to `deny`) from `domains.allow: []` (no domains). A non-empty allowance permits only its listed domains, and exclusions take precedence. The shared admin parser preserves this distinction; providing only a deny list does not create an empty allowance. Existing explicit empty allowances remain deny-all and require an intentional administrator edit to change access. Dimension restrictions remain independent.

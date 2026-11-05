# Remote access — sign-in & identity modes

When more than one person uses your server, you need real sign-in. MCP clients
authenticate to a remote server with **OAuth 2.1 + PKCE** and discover the
sign-in endpoint via [RFC 9728](https://www.rfc-editor.org/rfc/rfc9728), so you
need an OIDC authorization server in front of Précis Finance MCP.

Your ingress must serve `/.well-known/oauth-protected-resource` and forward
the authorization-server discovery and endpoint URLs it advertises. In bundled
Keycloak mode, Précis also serves origin-relative compatibility routes:
`/.well-known/oauth-authorization-server`, `/authorize`, `/register`, `/token`.
Their metadata names the same canonical Keycloak realm issuer as protected-resource
discovery and advertises authorization-response issuer identification (`iss`).
The aliases forward to Keycloak and do not create a second token issuer.
The bundled Caddy and reference nginx configurations expose these routes;
a custom proxy must route them explicitly if its fallback serves another app.
External-OIDC mode uses the external issuer's endpoints and disables these
Keycloak-specific aliases.

For remote OAuth, publish the issuer under a resolvable hostname with a trusted
TLS certificate. Some clients reject private-IP authorization-server identifiers
even when the MCP resource itself is reachable on the LAN: Codex 0.161.0's
[pinned SDK policy](https://github.com/modelcontextprotocol/rust-sdk/blob/3e636cab26c013eca5131103c03d20237f12c4df/crates/rmcp/src/transport/auth.rs)
does this during protected-resource discovery. Its subsequent origin fallback
can report an issuer mismatch for a realm-based issuer. Origin aliases do not
remove that client restriction; retain the actual issuer consistently in
Keycloak, Précis metadata and token validation. The issuer hostname need not
match the MCP resource hostname, but clients must resolve and trust it.

You pick the sign-in posture at install with **one variable, `PRECIS_AUTH_MODE`**:

| Mode | `PRECIS_AUTH_MODE` | Sign-in | When |
|---|---|---|---|
| **A** | `devkey` | One static key, localhost only | Trial / demo / your own machine — see the [Quickstart](../getting-started/quickstart.md). Single-user. |
| **B** | `keycloak` | The **bundled Keycloak** (optionally federated to your IdP) | You want a ready-to-run sign-in provider. The default multi-user path. |
| **C** | `oidc` | **Your own OIDC IdP** (Auth0, Okta, Entra, Ping, …), trusted directly | You already run an IdP and want no extra moving parts. |

The modes differ only in *which issuer signs the token*. The token check, the
per-user permission model, and provisioning are identical across all three.

!!! note "The dev-key server is a separate entrypoint — not part of B/C"
    Mode A is a distinct, localhost-only process (`python -m precis_mcp.server`),
    enabled by `ENABLE_MCP_DEV_SERVER=1` and **off by default**. The multi-user
    modes B/C run a different entrypoint (`precis_mcp.app_open`, the OAuth-gated
    `/mcp` server) and the production bundle doesn't include the dev server.
    `PRECIS_AUTH_MODE` is enforced on **both** entrypoints: if you set
    `PRECIS_AUTH_MODE=keycloak` or `oidc`, the dev server **refuses to start even
    if `ENABLE_MCP_DEV_SERVER=1`** — so a B/C-configured host can't accidentally
    run a no-auth server. (And `app_open` likewise refuses `devkey`.)

## Mode B — bundled Keycloak (`PRECIS_AUTH_MODE=keycloak`)

Précis Finance MCP ships a ready-to-run Keycloak in the bundled `docker-compose`, with a
committed realm (sign-in client, the `precis_user_id` user mapper, and the
audience mapper for `/mcp`) plus a per-deploy reconcile. You don't configure OIDC
by hand.

The bundled realm uses the Précis login theme in `keycloak/themes/precis/login`,
mounted separately from the server image. It includes a light appearance,
self-hosted fonts and the connected-P brand mark and favicons. Theme updates
must be installed with the deployment files; production Keycloak theme caches
require a restart to load them. External OIDC providers in mode C own their
sign-in appearance.

The 0.4.0 candidate pins upstream Keycloak 26.8.1. Its remaining Guava
CVE-2026-102554 has a temporary availability-risk exception for the exact
reviewed image and Guava JAR, expiring on 2026-12-05 at 00:00 UTC. Reachability
of untrusted Java deserialization has not been disproved. CI rejects expiry or
scope drift; the deadline applies even if the next release is delayed. The
older MSSQL JDBC exception has been removed. See
[temporary Keycloak risk acceptance](../operations/upgrades.md#temporary-keycloak-risk-acceptance).

| Variable | Purpose | Default |
|---|---|---|
| `PRECIS_AUTH_MODE` | `keycloak` | — |
| `PRECIS_BASE_URL` | Public address of your server; the other URLs derive from it | — |
| `KC_BASE_URL_INTERNAL` | Backend → Keycloak (container topology) | `http://localhost:8080/auth` |
| `KC_BOOTSTRAP_ADMIN_PASSWORD` | Operator-only realm configuration (bundle: `KEYCLOAK_ADMIN_PASSWORD`) | — |
| `KC_PROVISIONING_CLIENT_SECRET` | Scoped user lifecycle; realm-apply and one-off admin CLI only | — |
| `KC_REALM` / `KC_CLIENT_ID` | Realm + client | `precis` / `precis-spa` |
| `KC_MCP_AUDIENCE` | Expected token audience on `/mcp` | derived from `PRECIS_BASE_URL` |
| `KC_ENABLE_EXCEL_ADDIN` | Provision and advertise the hosted Excel add-in public client | unset |
| `KC_ADDIN_REDIRECT_URIS` | Optional override for the add-in callback URI | derived from `PRECIS_BASE_URL` |

Bring it up:

```bash
docker compose -f deploy/docker-compose.yml up -d
```

This pulls the pinned published image (`ghcr.io/precis-finance/precis-finance-mcp`,
selected by `PRECIS_MCP_TAG`) and starts your data store, the bundled Keycloak
with its seeded realm, and the server. Pulling a pinned release is the default;
`docker compose … up -d --build` builds from source instead (rolling `main` /
forks). From a workstation, `scripts/deploy-mcp.sh` does the same, pull-first —
see the [production checklist](production-checklist.md#3-deploy). To let your users sign in with your corporate IdP, point Keycloak's
identity-brokering at it (OIDC **or SAML**) — Keycloak stays the issuer and the
above doesn't change; the walkthrough is
[Sign in with your corporate IdP](keycloak-brokering.md). This is also the
path for **claude.ai / ChatGPT**: their connectors self-register (DCR), which
the bundled Keycloak supports.

The `precis-mcp` client scope supplies the MCP audience. Although it is a
realm default, dynamic registration can make it optional on a client. The
realm-reconcile script enforces `include.in.token.scope=true` for new and
existing scopes: the token response then reports the granted scope, allowing
clients that replay reported scopes on refresh to retain their MCP audience.
For an existing installation with this scope hidden, apply the updated realm
reconcile and sign the connector in again; an already downscoped refresh grant
cannot restore the omitted scope. Verify a tool call after the access token
expires, as initial login alone does not accept refresh continuity.

To enable the hosted Excel add-in in mode B, set `KC_ENABLE_EXCEL_ADDIN=true`
before the Keycloak reconcile runs. The default callback is
`https://<your-host>/excel/auth-callback.html`; set `KC_ADDIN_REDIRECT_URIS`
only when your ingress needs a non-standard public origin. The add-in bundle is
baked into the published image and served at `/excel`; see
[Précis for Excel](../excel/index.md).

## Mode C — your own OIDC IdP (`PRECIS_AUTH_MODE=oidc`)

No Keycloak runs. The verifier points straight at your IdP. Register a client in
your IdP and tell Précis Finance MCP about the issuer:

| Variable | Purpose |
|---|---|
| `PRECIS_AUTH_MODE` | `oidc` |
| `OIDC_ISSUER` | Your IdP's issuer URL (used verbatim — keep any trailing slash) |
| `OIDC_JWKS_URL` | JWKS endpoint for the issuer. Required in mode C. |
| `OIDC_AUDIENCE` | The audience your IdP stamps for the `/mcp` resource |
| `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | Your pre-registered client (secret only if confidential) |
| `PRECIS_IDENTITY_CLAIM` | Which token claim carries identity (default `precis_user_id`) |
| `PRECIS_IDENTITY_COLUMN` | `id` (the claim value *is* the user id) or `external_id` (look it up) |
| `EXCEL_ADDIN_CLIENT_ID` | Optional public PKCE client_id for the hosted Excel add-in |
| `EXCEL_ADDIN_SCOPE` / `EXCEL_ADDIN_RESOURCE_INDICATOR` | Optional token-request shape for IdPs such as Entra/Okta |

Two things to know:

- **Pre-registered client, not DCR.** Most enterprise IdPs disallow dynamic
  client registration, so you register the client yourself and set
  `OIDC_CLIENT_ID`. Modern MCP clients (Claude Code, ChatGPT, a first-party
  frontend) then sign in via discovery. **claude.ai/ChatGPT's public connectors
  need DCR** — if your IdP doesn't offer it, use mode B (Keycloak brokering).
- **Stable identity claim.** Map `PRECIS_IDENTITY_CLAIM` to a *stable, unique*
  claim (e.g. an immutable subject / `oid`), not a mutable email. If it differs
  from your Précis Finance MCP user ids, set `PRECIS_IDENTITY_COLUMN=external_id` and
  store the IdP value with `create-user --external-id` (below).
- **Excel add-in uses a separate public client.** If you enable the hosted Excel
  add-in in mode C, register a public PKCE client in your IdP with callback
  `https://<your-host>/excel/auth-callback.html`, then set
  `EXCEL_ADDIN_CLIENT_ID` on the `/mcp` host. Entra/Okta usually also need
  `EXCEL_ADDIN_SCOPE` and `EXCEL_ADDIN_RESOURCE_INDICATOR=false`; see
  [External IdP recipes](external-idp-recipes.md#351-excel-add-in-against-a-no-dcr-idp-entra-okta-direct).

## Create the first admin and provision users

Being signed in by the IdP grants nothing — each user must also exist in
Précis Finance MCP with a profile, and the UI can't bootstrap itself. The simplest path
is to let the deploy create it — pass `--admin-id` on your `deploy-mcp.sh` run
(or set `PRECIS_BOOTSTRAP_ADMIN_ID` in `deploy/.env`):

```bash
bash scripts/deploy-mcp.sh --admin-id alice@example.com
```

The deploy's first-admin step creates that admin — idempotently, so it's safe on
every run — and prints a one-time temporary password (change it on first login)
in the deploy output. If you already deployed with `--admin-id`, it's done; the
manual steps below are only for adding admins later, or if you didn't set it.

To do it by hand, run the **admin CLI** in the `precis-mcp` container. One
wrinkle in bundled-Keycloak (mode B): `create-admin`, `create-user`, and
`reset-password` provision a *Keycloak* account, so they need the Keycloak
scoped provisioning client secret — which the long-running server deliberately does not
carry. Inject it for that one call (it lives in `deploy/.env` as
`KC_PROVISIONING_CLIENT_SECRET`) by using `run` rather than `exec`:

```bash
export KC_PROVISIONING_CLIENT_SECRET="$(grep '^KC_PROVISIONING_CLIENT_SECRET=' deploy/.env | cut -d= -f2-)"
docker compose -f deploy/docker-compose.yml --env-file deploy/.env run --rm \
  -e KC_PROVISIONING_CLIENT_SECRET \
  precis-mcp python -m precis_mcp.admin_cli create-admin --id alice@example.com
# mode C (external IdP — no Keycloak account, the IdP owns the credential):
docker compose -f deploy/docker-compose.yml exec precis-mcp \
  python -m precis_mcp.admin_cli create-admin --id alice --no-keycloak --external-id <idp-subject>
```

The same scoped identity serves `create-admin`, `create-user`, `reset-password`
and `disable-user`. Reconcile the bundled realm with the independent secret first;
ordinary reruns retain it. The serving MCP container receives neither provisioning
nor bootstrap credentials. External-IdP `--no-keycloak` operations need neither.

Provisioning authority is restricted to `/precis-application-users` through
Keycloak FGAP V2, without `manage-users` or client administration. Only the two
owned FGAP definitions are supported; competing policies disable provisioning and
require operator review. Never enroll Keycloak administrators/service accounts,
add subgroups or role grants to this group, or promote an enrolled account to
Keycloak administration without first removing its membership. The credential
can reset or alter any enrolled Précis account, including Précis application admins.

On upgrade, existing platform sign-in accounts require explicit operator enrollment
with `scripts/keycloak/migrate_users.py --enroll-existing`, supplying operator
credentials for enrollment and the scoped secret for user lifecycle. This does
not reset existing passwords unless requested by the migration options. Normal
runtime user operations cannot enroll an existing account or fall back to bootstrap.

Then provision and grant access. `create-user` provisions Keycloak too, so it
takes the same injected credential as above; profile and assignment commands
touch only the platform DB, so a plain `exec` into the running server works:

```bash
docker compose -f deploy/docker-compose.yml exec precis-mcp \
  python -m precis_mcp.admin_cli profile create --file analyst.yml
docker compose -f deploy/docker-compose.yml exec precis-mcp \
  python -m precis_mcp.admin_cli assign --user bob --profile analyst
```

A user with no profile authenticates but can read nothing — assign a profile to
grant access. What a profile contains — scenario patterns, roles, domain and
dimension scopes, with worked examples — is documented in
[User profiles & permissions](../configuration/user-profiles.md).

## Verify

Check the issuer/JWKS/audience config before go-live:

```bash
python -m precis_mcp.admin_cli check-auth          # static + reachability
python -m precis_mcp.admin_cli check-auth --no-fetch   # static only
```

You can also have the server fail fast at startup on a misconfig by setting
`PRECIS_AUTH_PREFLIGHT=1`. Then confirm end-to-end: a client completes sign-in,
a query succeeds, and a token with the wrong `/mcp` audience is rejected.

## Connect a client

A modern MCP client discovers the sign-in endpoint from your server and runs the
OAuth flow itself — you give it the server URL (and, for mode C, the
pre-registered `client_id` if the client needs it configured):

```jsonc
{
  "mcpServers": {
    "precis": { "url": "https://<your-host>/mcp" }
  }
}
```

## Related

- [Quickstart (local, single-user)](../getting-started/quickstart.md)
- [Catalogue & semantic model](../configuration/catalogue-and-semantic.md)

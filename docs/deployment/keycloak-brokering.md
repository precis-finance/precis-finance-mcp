# Sign in with your corporate IdP (mode B, brokered)

Mode B runs the bundled Keycloak as the OAuth issuer. **Brokering** points its
sign-in at your corporate IdP: users authenticate with their normal corporate
credentials — your SSO, your MFA, your session policies — and Keycloak
re-issues the token Précis-MCP verifies. Nothing changes on the Précis-MCP
side: `PRECIS_AUTH_MODE=keycloak`, the token check, and provisioning are
exactly as in [Remote access](oauth-keycloak.md). Everything on this page is
Keycloak configuration.

This is your path when:

- **You want the public connectors (claude.ai / ChatGPT) and your IdP is
  Okta, Entra ID, or PingOne.** Those connectors self-register via DCR and
  rely on RFC 8707 audience stamping — which these IdPs don't offer. The
  bundled Keycloak supplies both, while your IdP still authenticates every
  user. If you're on one of these IdPs, this is the standard setup, not a
  workaround — the [decision matrix](external-idp-recipes.md) explains why.
- **Your IdP is SAML-only.** The Précis-MCP verifier speaks OIDC; Keycloak
  brokers SAML upstream.
- You want the sign-in stack self-contained, with your IdP as the source of
  credentials.

A brokered sign-in flows like this:

```text
MCP client ── discovery ──► bundled Keycloak ── redirect ──► your IdP (login + MFA)
    ▲                            │ creates/links the user record,
    │                            │ stamps precis_user_id + the /mcp audience
    └────── token issued ◄───────┘
```

## Step 1 — register a client for Keycloak in your IdP

To your IdP, Keycloak is just one more OIDC (or SAML) client:

1. Pick the broker **alias** now (e.g. `corp`) — it is part of the redirect
   URI and awkward to change later.
2. Register a **confidential** client with redirect URI
   `{PRECIS_BASE_URL}/auth/realms/precis/broker/corp/endpoint`.
3. Note the client id and secret.

Per-IdP notes:

- **Entra ID** — an app registration, *Web* platform, the redirect URI above.
  Use the **tenant-specific** v2 discovery URL
  (`https://login.microsoftonline.com/<tenant-id>/v2.0/.well-known/openid-configuration`),
  not `common`.
- **Okta** — an *OIDC Web Application*; assign the users/groups who should
  reach Précis-MCP. The **org authorization server is fine here** — Keycloak
  only needs a standard login, so the custom-AS licensing question from the
  [mode-C recipe](external-idp-recipes.md#32-okta-mode-b-for-public-connectors)
  doesn't arise.
- **Ping** — a standard OIDC client (PingFederate or PingOne both work as a
  brokered upstream).
- **SAML** — create the identity provider in Keycloak first (step 2); it then
  exposes SP metadata you import into your IdP.

## Step 2 — add the identity provider to the realm

Open the Keycloak admin console (`{PRECIS_BASE_URL}/auth/admin/`, the
bootstrap admin account — see [hardening](#the-dcr-and-admin-surfaces)
about exposing this path), select the `precis` realm, then
**Identity providers → Add provider**:

- **OpenID Connect v1.0** (or the built-in **Microsoft** provider, or
  **SAML v2.0**).
- **Alias** — the value from step 1's redirect URI.
- **Discovery endpoint** — your IdP's `.well-known/openid-configuration`;
  Keycloak fills the endpoints from it.
- **Client ID / Client Secret** — from step 1.

Sign-in now shows your IdP as a login option, and a user record is created in
Keycloak automatically on each user's first brokered login. To send users
straight to the corporate login (no local username/password form), set the
**Identity Provider Redirector** in the browser authentication flow to default
to your alias.

## Step 3 — carry a stable identity into the token

The `/mcp` verifier reads the `precis_user_id` claim, which the bundled realm
fills from the **Keycloak user attribute** of the same name. CLI-provisioned
local users get the attribute set for them; brokered users need it filled from
the upstream identity. Two supported patterns:

**A. Import the IdP's stable id, match on `external_id` (recommended).**
On the identity provider, add a mapper: **Attribute Importer**, upstream claim
→ user attribute `precis_user_id`, sync mode **FORCE** (re-applied on every
login, so it can't drift). Choose the upstream claim per the
[identity-claim guidance](external-idp-recipes.md#4-choosing-the-identity-claim)
— Entra `oid`, never an email. Then tell the server to match the claim against
the `external_id` column, and provision users with friendly ids:

```bash
# deploy/.env
PRECIS_IDENTITY_COLUMN=external_id
```

```bash
python -m precis_mcp.admin_cli create-user --id alice --no-keycloak \
    --external-id <idp-stable-id>
```

`--no-keycloak` because the broker creates the Keycloak record at first
login — don't create a local password account beside it.
`PRECIS_IDENTITY_COLUMN` is deployment-wide: once it's `external_id`, give
*every* user an external id, including local-account users (theirs can simply
repeat the user id: `--external-id alice`).

**B. Set the attribute by hand (small teams).** Keep the default
`PRECIS_IDENTITY_COLUMN=id`. After each user's first brokered login, open
their record in the Keycloak console (**Users**) and set `precis_user_id` to
their platform user id. No IdP mapper, no env change — one manual step per
user, which stops scaling around a dozen users.

## Step 4 — provision and verify

Users and profiles work exactly as in
[Remote access](oauth-keycloak.md#create-the-first-admin-and-provision-users)
— being able to sign in grants nothing until a platform user exists and a
[profile](../configuration/user-profiles.md) is assigned.

Verify end to end:

1. `python -m precis_mcp.admin_cli check-auth` passes.
2. Connecting an MCP client lands on **your corporate login page**, not a
   Keycloak form; after sign-in, a query returns data.
3. A colleague who exists in your IdP but is **not provisioned** in
   Précis-MCP signs in successfully and is still refused (`403`, not
   provisioned) — that's the provisioning gate doing its job.

## The DCR and admin surfaces

The bundled realm deliberately accepts **anonymous client registration**: the
per-deploy reconcile removes Keycloak's anonymous-DCR-blocking policies
(Trusted Hosts, Consent Required, Allowed Client Scopes) so that claude.ai and
ChatGPT can self-register. Registering a client grants **no access to data**:
a registered client still has to send a real user through your brokered
sign-in, and that user must exist in Précis-MCP with a profile. Keycloak's
remaining anonymous-registration limits (such as the max-clients cap) stay in
place. It is still an unauthenticated write surface, so harden it:

- **Rate-limit the registration path** at your ingress —
  `/auth/realms/precis/clients-registrations/` (the reference nginx config
  already carves this location out; add a `limit_req` there).
- **Restrict the admin console.** The reference ingress proxies all of
  `/auth/` — including `/auth/admin/` — publicly, guarded only by the
  bootstrap admin password. Block or IP-allowlist `/auth/admin/` at the
  ingress and reach the console over an SSH tunnel instead.
- **Not using the public connectors?** If only pre-registered clients ever
  connect, close the surface: re-add the Trusted Hosts anonymous policy
  (realm → **Client registration → Client registration policies**) or block
  the registration path at the ingress outright.
- **Watch the client list** for registrations you don't recognise.

## Related

- [Remote access — sign-in & identity modes](oauth-keycloak.md) — mode B
  itself: variables, bring-up, provisioning.
- [External IdP recipes](external-idp-recipes.md) — when to broker vs trust
  your IdP directly (mode C), and the per-IdP token mechanics.
- [User profiles & permissions](../configuration/user-profiles.md) — what a
  provisioned user may read.

# Précis for Excel

The Précis Excel add-in brings live financial statements and metrics into the
grid as custom functions. Type `=PRECIS.STATEMENT(…)` or `=PRECIS.METRIC(…)` in a
cell and the result **spills** into the sheet, refreshed on demand from your
Précis instance's `/mcp` endpoint — the same read tools the agent uses.

The add-in is **read-only**: it queries statements, metrics, and dimension
members. It never writes back to a plan.

!!! info "How it connects"
    The add-in is an **MCP client** of your instance's `/mcp` endpoint. It signs
    in with the instance's own OAuth (Keycloak) — there is no separate Précis
    account, and your figures never leave your instance. See
    [Remote access — sign-in & identity modes](../deployment/oauth-keycloak.md).

## Install

When the add-in is enabled and the built bundle is present, your Précis server
hosts the add-in at `/excel` and serves a manifest already templated for your
origin:

1. In Excel on the web: **Home → Add-ins → More Add-ins → My Add-ins →
   Upload My Add-in**, and give it `https://<your-instance>/excel/manifest.xml`.
2. The **Précis** group appears on the Home ribbon.

Because the bundle is served from the same origin as `/mcp`, **no `/mcp`
`CORS_ORIGINS` entry is needed**. The OAuth issuer is also inserted into the
manifest's `<AppDomains>` by the server.

## Connect

1. Open the **Panel** button on the Précis ribbon group.
2. Click **Sign in**. The add-in already knows its instance — the `/mcp` URL is
   the same origin that served the add-in — so it discovers the OAuth issuer from
   that host's protected-resource metadata, opens the sign-in dialog, and stores
   the token for the session.

The pane then shows **Connected** with the signed-in user and your instance URL.
Nothing is typed in: the `/mcp` URL is derived from the serving origin, and the
token is session-only and is **never** saved. The user line reads the name from
the id_token, so an IdP that emits it only under explicit scopes (Entra, Okta)
needs `profile email` in
[`EXCEL_ADDIN_SCOPE`](../configuration/environment-variables.md); without them
the pane falls back to the token's subject id.

!!! tip "Sessions"
    Access tokens are short-lived but refresh silently in the background. When a
    session genuinely ends, the pane returns to **Disconnected** — just sign in
    again.

## The ribbon

The **Précis** group on the Home tab has three buttons:

| Button | Does |
|---|---|
| **Panel** | Opens the task pane (connect, refresh, member drop-downs, sign out, function docs). |
| **Format** | Styles the selected spill — header band, subtotals, totals, variance colours, number formats. |
| **Refresh** | Re-fetches every `PRECIS.*` cell in the workbook (a full recalculation). |

**Format** and **Refresh** are also reachable from the task pane.

## Apply Précis formatting

A spilled statement or metric table arrives as plain values. Select any cell in
the spill and click **Format** to apply the Précis style positionally — the
formatting follows each line (a percentage line is shown as a percentage, a
total is ruled and bold), with favourable/unfavourable variance in green/red.

Re-running **Format** resets prior styling first, so it's safe to re-apply after
changing the formula's columns.

## Member drop-downs

The task pane can turn any selected cells into a native Excel drop-down of a
dimension's members — cost centres, projects, hierarchy nodes — for building
input sheets and filter cells that only accept valid values.

1. In the pane, pick a dimension under **Insert member drop-down**. The picker
   lists every dimension in your model; ragged hierarchies appear as their own
   entries (marked *hierarchy*) and list every node, all levels.
2. Pick what the drop-down shows: **Codes** (e.g. `CC-100`) or **Code + name**
   (e.g. `CC-100 | Cloud Infrastructure`).
3. Select the target cells in the sheet and click **Insert on selected cells**.

The member list lives on a hidden `PrecisLists` sheet as a live
`=PRECIS.HIERARCHY(…;"list")` spill, referenced through workbook-level defined
names (`Precis_List_<dimension>_…`). Consequences worth knowing:

- **One list per dimension per workbook.** Inserting more drop-downs for the
  same dimension — on any sheet — reuses the same list; **Refresh** updates
  them all in one recalculation.
- **A companion formula** lands in the column to the right of a single-column
  selection: the member's display name next to a code drop-down, the extracted
  code next to a code + name one. Cells that already have content are never
  overwritten (the pane tells you when it skips).
- **In code + name mode the cell value is not a valid filter value.** Use the
  companion code cell in downstream formulas, or extract the code yourself
  with `=TEXTBEFORE(A1;" | ")` — the code comes first, so the extraction works
  whatever the display name contains.
- Drop-down values are validated against the list at entry time; like any
  Excel data validation, they are a guide, not a lock.

## Server-side setup (for operators)

The add-in is read-only and connects to your instance's existing `/mcp` endpoint
— there is no separate add-in service. Three things must be in place.

### 1. Enable the add-in's OAuth client

The add-in is a **public** OAuth client (Authorization Code + PKCE, no secret),
distinct from the SPA's confidential client. How it's provisioned depends on the
[identity mode](../deployment/oauth-keycloak.md):

=== "Bundled Keycloak (mode B)"

    Set `KC_ENABLE_EXCEL_ADDIN=true` and
    `KC_ADDIN_REDIRECT_URIS=https://<your-instance>/excel/auth-callback.html`,
    then run the realm reconcile. It creates the gated `precis-excel-addin`
    public client and reconciles its redirect URIs/web origins to the hosted
    Précis origin. With the flag off, the reconcile **deletes** the client
    (absence-enforced) — the surface exists only where you enable it.

=== "External OIDC IdP (mode C)"

    Register a **public client** (Authorization Code + PKCE, no secret) in your
    IdP (Entra, Okta, Auth0, …) with the hosted callback
    `https://<your-instance>/excel/auth-callback.html` and the hosted Précis
    origin. Put the resulting client_id in `EXCEL_ADDIN_CLIENT_ID` on the `/mcp`
    host — alongside the SPA's confidential `OIDC_CLIENT_ID`.

    An IdP that binds the audience via a scope rather than the RFC 8707
    `resource` parameter (Entra, Okta) also needs `EXCEL_ADDIN_SCOPE` (the full
    request scope, e.g. `openid offline_access api://<app-id>/access_as_user`)
    and `EXCEL_ADDIN_RESOURCE_INDICATOR=false`. See the
    [external IdP recipes](../deployment/external-idp-recipes.md#351-excel-add-in-against-a-no-dcr-idp-entra--okta-direct).

In both cases the `/mcp` host advertises the client_id — and the token-request
shape — in its protected-resource metadata, so the add-in auto-configures from
the `/mcp` URL alone.

### 2. Hosted bundle

The published `precis-mcp` image ships the add-in bundle prebuilt at
`/app/excel-addin/dist`, so no separate asset install or bind mount is required. Advanced deployments can point `EXCEL_ADDIN_DIST_DIR` at a
different built bundle directory.

The add-in's data calls to `/mcp` run from the bundle's own origin (the custom
functions execute in a shared runtime served from `/excel`), so `/mcp` itself
needs no CORS configuration. The static `/excel` assets, however, do send an
open CORS header: **Excel on the web** hosts the custom-functions runtime on
Microsoft's origin and fetches `functions.json` and the manifest cross-origin
from your instance. The server adds `Access-Control-Allow-Origin: *` to the
`/excel` responses automatically (they're public, unauthenticated, read-only) —
no deployer action is needed. Excel desktop loads same-origin and ignores it.

### `/excel` security headers and a hardening reverse proxy

The server sets the `/excel` security headers itself: `Access-Control-Allow-Origin: *`,
`Cross-Origin-Resource-Policy: cross-origin` (so the Office host can load the ribbon
icons), a content-security policy that allows `office.js` and **omits**
`frame-ancestors`/`X-Frame-Options` so Excel can frame the task pane, and
`Cache-Control: no-cache` so browsers revalidate the bundle on every load and an
upgrade never serves a stale task pane (a caching proxy in front should not
override this on `/excel`). This is not a
choice — an Office add-in is the opposite shape from a normal web page: Excel hosts
the task pane in an iframe and loads `office.js` cross-origin, so the `/excel`
endpoint **must** be framable. A reverse proxy that denies framing in front of it
makes the add-in physically unable to load.

If you front the server with your own proxy or WAF that applies a site-wide
hardening baseline (`X-Frame-Options: DENY`, CSP `frame-ancestors 'none'`,
`Cross-Origin-Resource-Policy: same-origin`), scope a **narrow exception for the
`/excel` path** so it does not override the server's headers there. This is a
justified, contained exception rather than a weakening of your baseline:

- `/excel` serves **only the public, read-only add-in bundle** (HTML/JS/manifest/
  icons/fonts) — no cookies, no session, no data.
- Authentication uses a bearer token held in Office's runtime memory; **no
  credential is exposed on `/excel`**, and all data flows through `/mcp`/`/api`,
  which keep your full hardening (frame-denied, same-origin CORP).
- So permitting `/excel` to be framed is not a clickjacking or data-exposure
  vector — there is nothing sensitive on that path to act on.

If your edge policy is all-or-nothing and a path exception isn't possible, the
alternative is to **serve the add-in from a dedicated subdomain** governed by its
own policy, leaving your main origin's baseline untouched. That requires enabling
`/mcp` CORS for the add-in's origin (since the add-in would no longer be
same-origin with `/mcp`); the same-origin default avoids that and is simpler for
most deployments.

### 3. HTTPS

The add-in runs on an HTTPS page, so both `/mcp` and the issuer must be reachable
from the client over **HTTPS with a trusted certificate** (Office blocks mixed
content and requires HTTPS for the sign-in dialog).

## Sign-in & identity modes

The backend is **IdP-agnostic** — it validates any compliant issuer. The add-in
discovers everything from the `/mcp` URL: the issuer and its client_id (the
protected-resource metadata), then the authorize/token endpoints (the issuer's
OpenID configuration). One add-in works across deployments:

| Identity mode | Supported | Notes |
|---|---|---|
| **Bundled Keycloak** (B) | ✅ | the `precis-excel-addin` realm client |
| **Keycloak brokering a corporate IdP** (B) | ✅ | users log in via their IdP; the OAuth client/issuer are Keycloak |
| **Direct external OIDC** (C) | ✅ | a pre-registered public client in the external IdP (`EXCEL_ADDIN_CLIENT_ID`) |
| **dev-key** (A) | — | no interactive OAuth; the add-in needs a real token |

Interactive sign-in is the only auth path — there is no developer "paste a token"
fallback.

## Next

- **[Function reference](functions.md)** — every `PRECIS.*` function, its
  arguments, and examples.

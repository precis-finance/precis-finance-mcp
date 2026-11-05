---
description: Install and configure the read-only Précis Excel add-in to retrieve governed statements and metrics from your self-hosted MCP endpoint.
---

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

1. Download `https://<your-instance>/excel/manifest.xml` from your Précis
   instance. Use this hosted manifest, which contains your instance's URLs.
2. In Excel on the web, open a workbook and use **Home → Add-ins → More
   Settings → Upload My Add-in**, then select the downloaded XML file. Menu
   labels vary by Excel version; the upload dialog takes a file.
3. The **Précis** group appears on the Home ribbon.

If your organisation has already deployed **Précis for Excel**, use the
organisation-managed add-in instead. If uploading is unavailable or prohibited,
ask your Microsoft 365/IT admin for the approved installation route. Share the
hosted manifest URL, your Excel platform/version and the users/group that need
access. Admins can assess custom Office add-in deployment through Microsoft 365
**Settings → Integrated apps**, using an XML manifest file or URL where the
tenant supports it. Central deployment needs validation in your organisation;
Précis's current installation acceptance covers manual sideloading.

See Microsoft's [web sideloading guide](https://learn.microsoft.com/en-us/office/dev/add-ins/testing/sideload-office-add-ins-for-testing)
and [admin deployment guide](https://learn.microsoft.com/en-us/microsoft-365/admin/manage/manage-deployment-of-add-ins).
For desktop Excel, ask IT for the approved Windows/Mac route; web upload menus
are not a universal desktop installation procedure.

When deploying an add-in update, sync both `excel-addin/dist/` and
`excel-addin/manifest.xml`. The hosted manifest uses that source XML template;
updating only `dist/` can leave Microsoft 365 seeing an older version. For an
admin-managed installation, upload the updated hosted manifest through your
organisation's approved route.

Because the bundle is served from the same origin as `/mcp`, **no `/mcp`
`CORS_ORIGINS` entry is needed**. The OAuth issuer is also inserted into the
manifest's `<AppDomains>` by the server.

## Connect

1. Open the **Panel** button on the Précis ribbon group.
2. In **Connection**, click **Sign in**. The add-in already knows its instance — the `/mcp` URL is
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

The pane has three tabs. **Connection** contains sign-in, account/instance
details, **Refresh workbook data**, disconnect and the function documentation
link. Refresh updates all Précis formulas and member lists in this workbook.
**Members** adds cell drop-downs for report filters. **Convert** converts the
selected export region to a live formula. Connection status stays visible
whichever tab is open. Use arrow keys
or Home/End to move between tabs; changing tabs preserves choices and feedback.

!!! tip "Sessions"
    Access tokens are short-lived but refresh silently in the background. When a
    session genuinely ends, the pane returns to **Disconnected** — just sign in
    again.

## The ribbon

The **Précis** group on the Home tab has three buttons:

| Button | Does |
|---|---|
| **Panel** | Opens the Connection, Members and Convert tabs. |
| **Format** | Styles the selected spill — header band, subtotals, totals, variance colours, number formats. |
| **Refresh** | Re-fetches every `PRECIS.*` cell in the workbook (a full recalculation). |

**Refresh workbook data** is also available on the Connection tab. Formatting
is on the Home ribbon.

## Apply Précis formatting

A spilled statement or metric table arrives as plain values. Select any cell in
the spill and click **Format** to apply the Précis style positionally — the
formatting follows each line (a percentage line is shown as a percentage, a
total is ruled and bold), with favourable/unfavourable variance in green/red.

Re-running **Format** resets prior styling first, so it's safe to re-apply after
changing the formula's columns.

The task pane and ribbon icons follow the current Précis identity. **Format**
uses light headers, quiet subtotal fills and slightly stronger total fills,
while retaining workbook fonts, number formats and green/red variance meanings.
Existing ranges keep their formatting until you apply **Format** again.

## Convert a selected export to a live formula

Select a cell, or a contiguous selection wholly inside one Précis-generated
region with **report** or **extract** layout. In the task pane open **Convert**
and choose **Convert selected export**. The action queries fresh data and replaces
only that region at the same position and layout; static edits are replaced.
Statement/metric downloads from open `export_dataset(format="xlsx")` are eligible
when generated with the new provenance metadata. The action needs no commercial
workbook tools.

Save the workbook after a successful spill if it will be processed outside Excel.
Live-region markers retain the originating instance and generated-name identities;
the saved array range, rather than a cached marker size, establishes spill bounds.
The shared core includes bounded workbook discovery/placement procedures for file
adapters. Agent file conversion and refresh tools belong to the commercial agent
workflow and are not advertised by the open MCP server. This adds no reverse
conversion button to the add-in.

For report layout, a normal formula starts at the original column-header row.
Headers, data and a single context footer become live. The standalone title stays
outside the converted region and remains yours to maintain. Context follows the
report in one unmerged row, with unwrapped text in its first cell; its period,
filters, scenarios, units and timestamp come from the same query as the values.
Conversion leaves the footer unwrapped and restores standard height on unshared
footer rows, preserving adjacent notes.
Apply Précis formatting autofits the whole footer row.
Headers are unmerged for the spill, preserving the first data row's offset.
Static exports and live formulas omit the same decorative separator rows while
retaining hierarchy, subtotals and totals. Fresh data can add, remove or reorder
body rows, so fixed-cell references can point to different lines. Conversion
refuses obstructed growth, changed headers and unsupported merges. It does not
insert/delete worksheet rows or change column widths/frozen panes.

For extract layout, Excel removes the Table container and rewrites structured references to fixed
cell references. Generated value-column names instead follow the live spill's
size, excluding the final context footer (`ROWS(spill)-1`). The static Table and
value-column names also exclude that row. Custom totals, calculated columns, slicers, protected sheets, obstructed
destinations and unsupported source arguments are refused with a reason.
Conversion requires ExcelApi 1.13 or later and is limited to 100,000 cells.
The export must record explicit source settings and the same originating Précis
instance and the current single-footer layout. This is a breaking layout change;
older exports or live markers without that footer contract must be regenerated.
Conversion displays progress and errors directly beneath its button. An
unsupported Excel version shows a reason beside the disabled action. Ordinary
shared YAML references in generated metadata are supported; they do not require
regenerating an otherwise valid export. Files without named region metadata do
require regeneration; Table-only legacy metadata is no longer inferred.

The live marker written by add-in version 1.0.0.6 retains the originating
instance and generated-name identities. Older live markers without origin require
a fresh export and conversion with the current add-in. Shared Python workbook
utilities recognize Excel-saved `_xldudf_PRECIS_STATEMENT` and
`_xldudf_PRECIS_METRIC` names alongside normal `PRECIS.*` calls. Saved array bounds
establish spill ownership; a marker's numeric snapshot alone is insufficient.

The initial number formats come from the shared server contract for that layout. After a
recalculation changes row order or spill size, choose **Apply Précis formatting**
to apply the latest per-metric formats. Failed operations restore the original
export when safe; if competing edits prevent this, the status identifies a
visible recovery sheet containing the saved original replacement-area cells
and formats, including the original column-header merges. Retained report
captions are outside that area and are never overwritten during restoration. Excel's Table-reference
rewrites may remain even after recovery.
If removing the recovery copy fails after conversion or restoration succeeds,
the completed result is kept and the status identifies the retained sheet.
An incomplete restoration reports both the conversion and recovery errors.

## Member drop-downs

Open the **Members** tab for an explanation, dimension/display choices and
**Add to selected cells**. Select the destination cells in Excel first.

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
    [external IdP recipes](../deployment/external-idp-recipes.md#351-excel-add-in-against-a-no-dcr-idp-entra-okta-direct).

In both cases the `/mcp` host advertises the client_id — and the token-request
shape — in its protected-resource metadata, so the add-in auto-configures from
the `/mcp` URL alone.

### 2. Hosted bundle

The published `precis-finance-mcp` image ships the add-in bundle prebuilt at
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

## License and community

Précis-authored add-in code uses the repository's
[Elastic License 2.0](https://github.com/precis-finance/precis-finance-mcp/blob/main/LICENSE).
Original Microsoft template portions retain their MIT license, documented in
the [third-party notices](https://github.com/precis-finance/precis-finance-mcp/blob/main/excel-addin/THIRD_PARTY_NOTICES.txt).
The built bundle includes both license texts. Conduct concerns follow the
[Précis Code of Conduct](https://github.com/precis-finance/precis-finance-mcp/blob/main/CODE_OF_CONDUCT.md).

## Next

- **[Function reference](functions.md)** — every `PRECIS.*` function, its
  arguments, and examples.

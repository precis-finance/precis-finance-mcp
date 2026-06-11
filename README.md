# precis-mcp

Read-only financial-data access over [MCP](https://modelcontextprotocol.io) —
the open core of **Précis**, the agentic Finance Intelligence Platform. It
serves governed financial metrics and statements from ClickHouse to any MCP
client: the metric engine, the semantic layer, ingestion from your sources,
and the MCP transport with OAuth 2.1 authentication.

You point it at your financial data, describe your model once in a YAML
catalogue over a semantic layer (SQL views that state what your data means),
and any MCP-capable client — Claude or another agent — gets consistent,
defensible numbers: metrics, financial statements, and row-level drill-down. It is **read-only by construction**: it
never writes to or changes your data.

## Quickstart — single-user local trial

```sh
MCP_DEV_KEY=$(openssl rand -hex 32) \
  docker compose -f deploy/docker-compose.local.yml up -d --build
```

One stack — the server plus a bundled ClickHouse — a shared dev key, bound to
`127.0.0.1`. Point any MCP client at `http://127.0.0.1:8768/sse` with the dev
key as bearer token. Full walkthrough, including schema provisioning:
[docs/getting-started/quickstart.md](docs/getting-started/quickstart.md).

## Multi-user deployment

`deploy/docker-compose.yml` runs the multi-user stack — the open server,
PostgreSQL, ClickHouse, and OAuth 2.1 + PKCE sign-in. Deployment is modular
along independent axes:

- **Data** — bundled ClickHouse (empty or with sample data) or your own:
  [docs/deployment/clickhouse-data-modes.md](docs/deployment/clickhouse-data-modes.md)
- **Identity** — local dev key, the bundled Keycloak (optionally federated to
  your IdP), or a direct external OIDC provider (Auth0 / Okta / Entra / Ping):
  [docs/deployment/oauth-keycloak.md](docs/deployment/oauth-keycloak.md),
  [docs/deployment/external-idp-recipes.md](docs/deployment/external-idp-recipes.md)
- **Ingress** — the app is proxy-agnostic; front it with your own ingress or
  the host nginx + certbot helper (`scripts/install-precis-mcp.sh`)
- **Backups** — additive `backup` compose profile: scheduled bundles
  (Postgres dump + ClickHouse backup + instance config) to a local volume or
  S3, with restore and drill commands:
  [docs/operations/backups.md](docs/operations/backups.md)

`scripts/deploy-mcp.sh --data-mode ... --auth-mode ...` is the friendly front
door over the Compose profiles. Every knob is an environment variable:
[docs/configuration/environment-variables.md](docs/configuration/environment-variables.md).

## Bring your model

Your model lives in an `instance/` directory — metric catalogue, semantic SQL
views, ingestion bindings — and the bundled demo instance shows the shape.
Start at [docs/configuration/catalogue-and-semantic.md](docs/configuration/catalogue-and-semantic.md),
then [docs/configuration/ingestion.md](docs/configuration/ingestion.md) and
[docs/operations/onboarding-ingestion.md](docs/operations/onboarding-ingestion.md)
to load your data. The developer guide for adding server tools is
[docs/development/adding-read-tools.md](docs/development/adding-read-tools.md).

## The open core of Précis

precis-mcp is the open core of **Précis**, the Finance Intelligence Platform.
This repository is the complete, self-hostable read-only surface: the metric
engine, semantic layer and catalogue, ingestion, identity, and the MCP
transport — it stands on its own and depends on nothing outside this
repository.

The full Précis platform builds on this same core with the agentic finance
workspace: a conversational agent and UI over the same engine and data
model, planning with write-back (budgets, forecasts, scenarios you can edit
and commit), and an extended finance-workflow toolset — reports, scheduled
routines and briefings, charts, and Excel round-trip. None of that ships in
this repository.

## Contributing

This repository is a one-way mirror of an internal monorepo: `main` advances
by sync commits, and pull requests are applied internally with your authorship
and DCO sign-off preserved, then published in the next sync. See
[CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Elastic License 2.0](LICENSE) — source-available. Free to use, modify,
self-host (including commercially), and redistribute; you may not offer it to
third parties as a hosted or managed service.

# Dockerfile.open — staged image build for the open `precis-mcp` package.
#
# Renamed to `Dockerfile` at the R4 repo cut (parallel to pyproject-open.toml ->
# pyproject.toml). The open compose bundles (deploy/docker-compose*.yml) build
# with `context: ..` + `dockerfile: Dockerfile`, so they pick this up unchanged
# in the open repo. It is NOT buildable in the monorepo — the root pyproject.toml
# there is the commercial import-linter config, not the open package metadata.
#
# Lean vs the commercial root Dockerfile: gcc only (psycopg2-binary build), no
# LibreOffice (Excel is commercial), the open dependency subset from the package
# pyproject, and an open default CMD.
FROM python:3.12-slim

WORKDIR /app

# gcc is kept defensively for any dependency without a manylinux wheel on slim
# (the data + DB stack — psycopg3-binary, clickhouse-connect, pyarrow — all ship
# wheels, so this is belt-and-braces, not a known requirement).
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# pg_dump/pg_restore for the backup subsystem (precis_mcp/backup/). The client
# major must match the bundled postgres:16 server — bookworm's stock
# postgresql-client is v15, so pull 16 from PGDG. Bump together with the
# compose postgres image tag.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates gnupg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
       | gpg --dearmor -o /usr/share/keyrings/pgdg.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] http://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
       > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get install -y --no-install-recommends postgresql-client-16 \
    && rm -rf /var/lib/apt/lists/*

# Dependency layer — the open subset from the package metadata. Copy the
# pyproject + package first so this layer caches across source-only changes.
COPY pyproject.toml ./
COPY precis_mcp ./precis_mcp
RUN pip install --no-cache-dir .

# The rest of the open tree: the demo instance fixture (baked as the default;
# the compose mounts a deployer's own over /app/instance), deploy assets
# (reconcile, nginx), and scripts (migrate.py). .dockerignore keeps it lean.
COPY . .

# 8768 — single-user dev MCP server (precis_mcp.server); 8769 — multi-user ASGI
# (precis_mcp.app_open). The local-trial bundle overrides CMD to run the former.
EXPOSE 8768 8769

CMD ["uvicorn", "precis_mcp.app_open:app", "--host", "0.0.0.0", "--port", "8769"]
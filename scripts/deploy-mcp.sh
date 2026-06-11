#!/usr/bin/env bash
# deploy-mcp.sh — Deploy / refresh the OPEN precis-mcp bundle on a server.
#
# The open counterpart to scripts/deploy.sh (which deploys the commercial
# stack). Runs from your local machine: rsyncs the working tree to the box and
# drives `docker compose` there against deploy/docker-compose.yml.
#
# Prereqs: scripts/install-precis-mcp.sh has provisioned the box (Docker,
# firewall, /opt/precis-mcp). SSH access as root via the host alias.
#
# Usage:
#   bash scripts/deploy-mcp.sh                      # sync + build + up + health
#   bash scripts/deploy-mcp.sh --server HOST        # target host (default: precis-mcp)
#   bash scripts/deploy-mcp.sh --sync-only          # just rsync code + instance, stop
#   bash scripts/deploy-mcp.sh --no-build           # up without rebuilding the image
#   bash scripts/deploy-mcp.sh --data-mode MODE     # provision ClickHouse (see below)
#   bash scripts/deploy-mcp.sh --data-only          # provision + exit (no Keycloak/server)
#   bash scripts/deploy-mcp.sh --auth-mode MODE     # identity mode (see below)
#
# Auth mode (the identity axis, also settable via PRECIS_AUTH_MODE; default
# keycloak). Orthogonal to the data mode — pick one of each:
#   keycloak   bundled Keycloak (mode B) — the default multi-user path; the
#              keycloak + realm-apply services run (bundled-keycloak profile).
#   oidc       external OIDC IdP (mode C) — Keycloak is NOT started; set
#              OIDC_ISSUER (+ OIDC_JWKS_URL/AUDIENCE/CLIENT_ID/SECRET and
#              PRECIS_IDENTITY_CLAIM/COLUMN) in deploy/.env. precis-mcp verifies
#              the customer issuer. (devkey / mode A is the single-user local
#              bundle — use deploy/docker-compose.local.yml, not this script.)
#
# Data mode (the connection × provisioning preset, also settable via the
# PRECIS_DATA_MODE env var; default: none — deploy without provisioning, for
# code iteration on an already-provisioned box):
#
#   byo            external ClickHouse you run (set CHHOST in deploy/.env); the
#                  bundled clickhouse service is NOT started. Schema-only
#                  provisioning via `clickhouse_init`.
#   bundle-empty   bundled ClickHouse, schema-only provisioning (clickhouse_init).
#                  Empty schema ready for your own ingestion.
#   bundle-sample  bundled ClickHouse, populated with synthetic eval data
#                  (generate_synthetic_data.py). The trial / demo on-ramp.
#
# Schema provisioning runs the open `clickhouse_init` (the migrate.py analog for
# ClickHouse) against the mounted instance/; the sample path seeds the mock
# Postgres source and triggers ingestion. Both run via `docker compose run
# --no-deps` so data is provisioned before the auth/server phase.
# (`--seed` is a back-compat alias for `--data-mode bundle-sample`.)

set -euo pipefail

SERVER="precis-mcp"
REMOTE_DIR="/opt/precis-mcp"
MOCK_SOURCE_DB="fpa_actuals"           # generate_synthetic_data.py's PGDATABASE default

DO_SYNC=true
DO_BUILD=true
DO_UP=true
SYNC_ONLY=false
DATA_ONLY=false
DATA_MODE="${PRECIS_DATA_MODE:-}"           # byo | bundle-empty | bundle-sample | "" (none)
AUTH_MODE="${PRECIS_AUTH_MODE:-keycloak}"   # keycloak (mode B) | oidc (mode C)

while [ $# -gt 0 ]; do
    case "$1" in
        --server)      SERVER="$2"; shift 2; continue ;;
        --server=*)    SERVER="${1#*=}"; shift; continue ;;
        --sync-only)   SYNC_ONLY=true ;;
        --no-build)    DO_BUILD=false ;;
        --data-mode)   DATA_MODE="$2"; shift 2; continue ;;
        --data-mode=*) DATA_MODE="${1#*=}"; shift; continue ;;
        --auth-mode)   AUTH_MODE="$2"; shift 2; continue ;;
        --auth-mode=*) AUTH_MODE="${1#*=}"; shift; continue ;;
        --seed)        DATA_MODE="bundle-sample" ;;      # back-compat alias
        --data-only)   DATA_ONLY=true ;;
        *)             echo "Unknown flag: $1"; exit 1 ;;
    esac
    shift
done

# --data-only implied seeding before modes existed; preserve that default.
if $DATA_ONLY && [ -z "$DATA_MODE" ]; then DATA_MODE="bundle-sample"; fi

# Data axis → its COMPOSE profile. byo excludes the bundled ClickHouse service;
# bundle-* (and the no-mode default) include it.
case "$DATA_MODE" in
    byo)                        DATA_PROFILE=""; CHHOST_SEED="" ;;
    bundle-empty|bundle-sample) DATA_PROFILE="bundled-clickhouse"; CHHOST_SEED="clickhouse" ;;
    "")                         DATA_PROFILE="bundled-clickhouse"; CHHOST_SEED="clickhouse" ;;  # deploy-only, bundled
    *) echo "Invalid --data-mode: ${DATA_MODE} (byo | bundle-empty | bundle-sample)"; exit 1 ;;
esac

# Auth axis → its COMPOSE profile. keycloak (mode B) runs the bundled Keycloak;
# oidc (mode C) drops it and points the verifier at the customer IdP (OIDC_*).
case "$AUTH_MODE" in
    keycloak) AUTH_PROFILE="bundled-keycloak" ;;
    oidc)     AUTH_PROFILE="" ;;
    devkey)   echo "devkey (mode A) is the single-user local trial — use deploy/docker-compose.local.yml, not this multi-user bundle."; exit 1 ;;
    *) echo "Invalid --auth-mode: ${AUTH_MODE} (keycloak | oidc)"; exit 1 ;;
esac

# Combine the two axes into COMPOSE_PROFILES (comma-joined, empties dropped).
_profiles=()
[ -n "$DATA_PROFILE" ] && _profiles+=("$DATA_PROFILE")
[ -n "$AUTH_PROFILE" ] && _profiles+=("$AUTH_PROFILE")
PROFILES="$(IFS=,; echo "${_profiles[*]}")"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
COMPOSE="docker compose -f deploy/docker-compose.yml --env-file deploy/.env"

echo "╔══════════════════════════════════════════╗"
echo "║  Précis-MCP (open) Deploy                ║"
echo "║  Server: ${SERVER}                       ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── 1. Sync code + instance ──────────────────────────────────────────
# rsync the working tree (not git): copies code AND the gitignored instance/
# in one pass, and lets us iterate on deploy files without commit churn.
# deploy/.env is protected (never synced, never --delete'd) so server secrets
# survive a re-sync.
if $DO_SYNC; then
    echo "=== 1. Sync code + instance → ${SERVER}:${REMOTE_DIR} ==="
    rsync -az --delete \
        --exclude='.git/' --exclude='.venv/' --exclude='node_modules/' \
        --exclude='__pycache__/' --exclude='*.pyc' --exclude='.pytest_cache/' \
        --exclude='.mypy_cache/' --exclude='/.env' --exclude='deploy/.env' \
        --exclude='deploy/.env.local' \
        "${PROJECT_DIR}/" "${SERVER}:${REMOTE_DIR}/"
    echo ""
fi
$SYNC_ONLY && { echo "--sync-only: done."; exit 0; }

# ── 2. Ensure deploy/.env (generate secrets once; require PRECIS_BASE_URL) ──
# Idempotent: passwords are generated only when deploy/.env is absent. The
# operator must set PRECIS_BASE_URL (deployment-specific) before the server +
# Keycloak phase; the data phase does not need it.
echo "=== 2. Ensure deploy/.env on ${SERVER} ==="
ssh "$SERVER" "
    set -euo pipefail
    cd ${REMOTE_DIR}
    umask 077
    if [ ! -f deploy/.env ]; then
        {
            echo 'COMPOSE_PROFILES=${PROFILES}'
            echo 'PRECIS_AUTH_MODE=${AUTH_MODE}'
            echo \"PGUSER=precis\"
            echo \"PGPASSWORD=\$(openssl rand -hex 24)\"
            echo '# ClickHouse host: the bundled service name for bundle-* modes;'
            echo '# your external host (REQUIRED) for --data-mode byo:'
            echo 'CHHOST=${CHHOST_SEED}'
            echo \"CHUSER=precis\"
            echo \"CHPASSWORD=\$(openssl rand -hex 24)\"
            echo \"CHDATABASE=precis\"
            echo \"KEYCLOAK_ADMIN_USERNAME=admin\"
            echo \"KEYCLOAK_ADMIN_PASSWORD=\$(openssl rand -hex 24)\"
            echo \"KC_DB_PASSWORD=\$(openssl rand -hex 24)\"
            echo '# External OIDC (mode C only — PRECIS_AUTH_MODE=oidc): set these'
            echo '# to your IdP, and drop bundled-keycloak from COMPOSE_PROFILES.'
            echo 'OIDC_ISSUER='
            echo 'OIDC_JWKS_URL='
            echo 'OIDC_AUDIENCE='
            echo 'OIDC_CLIENT_ID='
            echo 'OIDC_CLIENT_SECRET='
            echo 'PRECIS_IDENTITY_CLAIM='
            echo 'PRECIS_IDENTITY_COLUMN='
            echo \"# REQUIRED before the Keycloak/server phase — set to your public origin:\"
            echo \"PRECIS_BASE_URL=\"
        } > deploy/.env
        echo '  generated deploy/.env (set PRECIS_BASE_URL before the server phase)'
    else
        echo '  keeping existing deploy/.env'
    fi
"
echo ""

# ── 3. Provision ClickHouse (mode-gated; runs before the auth/server phase) ──
# bundle-sample seeds synthetic data via generate_synthetic_data.py; byo /
# bundle-empty run the open clickhouse_init schema provisioner against the
# mounted instance/. All via `compose run --no-deps`, so data lands before auth.
if [ -n "$DATA_MODE" ]; then
    echo "=== 3. Provision ClickHouse (mode=${DATA_MODE}) ==="
    ssh "$SERVER" "
        set -euo pipefail
        cd ${REMOTE_DIR}
        ${COMPOSE} build precis-mcp
        case '${DATA_MODE}' in
          bundle-sample)
            ${COMPOSE} up -d postgres clickhouse
            for _ in \$(seq 1 30); do
                ${COMPOSE} exec -T postgres pg_isready -U precis >/dev/null 2>&1 && break
                sleep 2
            done
            # Mock-source DB the synthetic generator writes to (separate from the
            # platform/keycloak DBs). Idempotent.
            ${COMPOSE} exec -T postgres psql -U precis -d precis -tAc \
                \"SELECT 1 FROM pg_database WHERE datname='${MOCK_SOURCE_DB}'\" | grep -q 1 || \
                ${COMPOSE} exec -T postgres psql -U precis -d precis -c \"CREATE DATABASE ${MOCK_SOURCE_DB}\"
            # Generate in Postgres → trigger ingestion → apply semantic.* views.
            ${COMPOSE} run --rm --no-deps -e PGDATABASE=${MOCK_SOURCE_DB} \
                precis-mcp python scripts/generate_synthetic_data.py
            ;;
          bundle-empty)
            ${COMPOSE} up -d clickhouse
            for _ in \$(seq 1 30); do
                ${COMPOSE} exec -T clickhouse wget -qO- http://localhost:8123/ping >/dev/null 2>&1 && break
                sleep 2
            done
            ${COMPOSE} run --rm --no-deps \
                precis-mcp python -m precis_mcp.clickhouse_init --scope open
            ;;
          byo)
            # External ClickHouse (CHHOST). Nothing bundled to start; the
            # provisioner connects to your cluster.
            grep -q '^CHHOST=.\\+' deploy/.env || {
                echo 'ERROR: --data-mode byo needs CHHOST set in deploy/.env (your external ClickHouse host).' >&2
                exit 1
            }
            ${COMPOSE} run --rm --no-deps \
                precis-mcp python -m precis_mcp.clickhouse_init --scope open
            ;;
        esac
        echo '  provisioning complete.'
        # Verify against the bundled CH (byo's external CH may not be exec-able here).
        if [ '${DATA_MODE}' != 'byo' ]; then
            ${COMPOSE} exec -T clickhouse clickhouse-client -q \
                \"SELECT count() FROM system.tables WHERE database='semantic'\" || true
        fi
    "
    echo ""
fi
$DATA_ONLY && { echo "--data-only: done (no Keycloak/server brought up)."; exit 0; }

# ── 4. Server up (migrate + precis-mcp; + Keycloak realm-apply in mode B) ──
# Requires PRECIS_BASE_URL set in deploy/.env. In mode B the keycloak +
# realm-apply one-shots run (bundled-keycloak profile); in mode C (oidc) they are
# skipped and precis-mcp verifies the customer IdP via OIDC_*.
echo "=== 4. Bring up the bundle (auth-mode=${AUTH_MODE}) ==="
ssh "$SERVER" "
    set -euo pipefail
    cd ${REMOTE_DIR}
    grep -q '^PRECIS_BASE_URL=.\\+' deploy/.env || {
        echo 'ERROR: PRECIS_BASE_URL is empty in deploy/.env — set it before the server phase.' >&2
        exit 1
    }
    if [ '${AUTH_MODE}' = 'oidc' ]; then
        grep -q '^OIDC_ISSUER=.\\+' deploy/.env || {
            echo 'ERROR: --auth-mode oidc needs OIDC_ISSUER set in deploy/.env (your external IdP issuer).' >&2
            exit 1
        }
    fi
    $($DO_BUILD && echo "${COMPOSE} up -d --build" || echo "${COMPOSE} up -d")
    ${COMPOSE} ps
"
echo ""

# ── 5. Health check ──────────────────────────────────────────────────
echo "=== 5. Health check ==="
ssh "$SERVER" "
    set +e
    echo -n 'precis-mcp /health: '; curl -sf http://127.0.0.1:8769/health && echo || echo FAIL
    echo -n 'discovery doc:      '; curl -sf http://127.0.0.1:8769/.well-known/oauth-protected-resource >/dev/null && echo OK || echo FAIL
"
echo ""
echo "=== deploy-mcp.sh complete ==="

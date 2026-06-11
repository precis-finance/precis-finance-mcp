#!/usr/bin/env bash
# install-precis-mcp.sh — One-time bootstrap for an OPEN precis-mcp server.
#
# The lean counterpart to scripts/install.sh (the commercial bootstrap).
# Provisions only what the open MCP bundle (deploy/docker-compose.yml) needs:
# a firewall, Docker + compose, a few CLI utilities, and an app directory.
# No gVisor/sandbox, no commercial nginx vhost, no commercial secrets — the
# open read bundle has none of those surfaces.
#
# Runs from your local machine; SSHes into the target for all remote work.
# Every section is idempotent — safe to re-run.
#
# Usage:
#   bash scripts/install-precis-mcp.sh                      # all sections
#   bash scripts/install-precis-mcp.sh --server precis-mcp  # pick the host (default: precis-mcp)
#   bash scripts/install-precis-mcp.sh --firewall           # only: ufw rules
#   bash scripts/install-precis-mcp.sh --prereqs            # only: docker + compose + utils
#   bash scripts/install-precis-mcp.sh --filesystem         # only: app directory
#   bash scripts/install-precis-mcp.sh --nginx              # only: host nginx + certbot + mcp vhost
#
# --nginx is opt-in (not part of the default run): it needs a real domain whose
# DNS already points at this host, so certbot can obtain a cert. It installs the
# deploy/nginx/mcp.precis.finance.conf vhost fronting the bundle's localhost
# ports (Keycloak :8080 under /auth, app_open :8769 for the rest).
#
# After this, get the code + the instance/ config onto the box (git clone /
# rsync), fill deploy/.env, and bring up deploy/docker-compose.yml (Track B).
#
# Assumptions:
#   - SSH access as root (or passwordless sudo) via the host alias.
#   - Ubuntu (24.04 LTS recommended). The Docker install is OS-flexible: it
#     uses Docker's official repo when it publishes the release, else falls
#     back to the distro docker.io packages.

set -euo pipefail

SERVER="precis-mcp"          # target host alias (~/.ssh/config); override with --server
REMOTE_DIR="/opt/precis-mcp"
NGINX_DOMAIN="mcp.precis.finance"   # must match deploy/nginx/<domain>.conf; override with --domain

RUN_FIREWALL=false
RUN_PREREQS=false
RUN_FILESYSTEM=false
RUN_NGINX=false              # opt-in only (needs a domain with DNS pointing here)
RUN_ALL=true

while [ $# -gt 0 ]; do
    case "$1" in
        --server)     SERVER="$2"; shift 2; continue ;;
        --server=*)   SERVER="${1#*=}"; shift; continue ;;
        --domain)     NGINX_DOMAIN="$2"; shift 2; continue ;;
        --domain=*)   NGINX_DOMAIN="${1#*=}"; shift; continue ;;
        --firewall)   RUN_FIREWALL=true;   RUN_ALL=false ;;
        --prereqs)    RUN_PREREQS=true;    RUN_ALL=false ;;
        --filesystem) RUN_FILESYSTEM=true; RUN_ALL=false ;;
        --nginx)      RUN_NGINX=true;      RUN_ALL=false ;;
        *)            echo "Unknown flag: $1"; exit 1 ;;
    esac
    shift
done

# --nginx stays opt-in (certbot needs a live domain), so the default run
# provisions only the host-agnostic sections.
if $RUN_ALL; then
    RUN_FIREWALL=true; RUN_PREREQS=true; RUN_FILESYSTEM=true
fi

echo "╔══════════════════════════════════════════╗"
echo "║  Précis-MCP (open) Server Install        ║"
echo "║  Server: ${SERVER}                       ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── 1. Firewall: ufw (mirrors the precis ruleset) ────────────────────
if $RUN_FIREWALL; then
    echo "=== 1. Firewall: ufw ==="
    ssh "$SERVER" "
        set -euo pipefail
        command -v ufw >/dev/null || { apt-get update && apt-get install -y ufw; }
        # 22 first so enabling never drops the SSH session. 2222 mirrors the
        # precis ruleset (Forgejo git SSH); harmless if unused here.
        ufw allow 22/tcp
        ufw allow 80/tcp
        ufw allow 443/tcp
        ufw allow 2222/tcp
        ufw --force enable
        ufw status verbose
    "
    echo ""
fi

# ── 2. Prereqs: Docker + compose + CLI utils ─────────────────────────
if $RUN_PREREQS; then
    echo "=== 2. Prereqs: Docker + compose + utils ==="
    ssh "$SERVER" "
        set -euo pipefail
        export DEBIAN_FRONTEND=noninteractive
        apt-get update
        apt-get install -y ca-certificates curl gnupg git rsync jq openssl

        if ! command -v docker >/dev/null 2>&1; then
            codename=\$(. /etc/os-release && echo \"\$VERSION_CODENAME\")
            # Docker's official repo when it publishes this release; otherwise
            # the distro packages (fine for a self-host / validation box).
            if curl -fsI \"https://download.docker.com/linux/ubuntu/dists/\$codename/Release\" >/dev/null 2>&1; then
                install -m 0755 -d /etc/apt/keyrings
                curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
                    gpg --dearmor -o /etc/apt/keyrings/docker.gpg
                chmod a+r /etc/apt/keyrings/docker.gpg
                echo \"deb [arch=\$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \$codename stable\" \
                    > /etc/apt/sources.list.d/docker.list
                apt-get update
                apt-get install -y docker-ce docker-ce-cli containerd.io \
                    docker-buildx-plugin docker-compose-plugin
            else
                echo \"Docker official repo has no '\$codename' channel — using distro docker.io\"
                apt-get install -y docker.io docker-compose-v2
            fi
            systemctl enable --now docker
        fi
        echo '--- versions ---'
        docker --version
        docker compose version
    "
    echo ""
fi

# ── 3. Filesystem: app directory ─────────────────────────────────────
if $RUN_FILESYSTEM; then
    echo "=== 3. Filesystem: ${REMOTE_DIR} ==="
    ssh "$SERVER" "
        set -euo pipefail
        mkdir -p ${REMOTE_DIR}
    "
    echo ""
fi

# ── 4. Nginx + certbot: TLS ingress for the bundle (opt-in) ──────────
# Host nginx fronts the bundle's localhost ports (the commercial topology):
# Keycloak :8080 under /auth, app_open :8769 for the rest. Obtains a Let's
# Encrypt cert via certbot, then installs deploy/nginx/<domain>.conf.
if $RUN_NGINX; then
    echo "=== 4. Nginx + certbot: ${NGINX_DOMAIN} ==="
    VHOST_SRC="$(cd "$(dirname "$0")/.." && pwd)/deploy/nginx/${NGINX_DOMAIN}.conf"
    test -f "$VHOST_SRC" || { echo "ERROR: missing $VHOST_SRC"; exit 1; }
    scp "$VHOST_SRC" "$SERVER:/tmp/mcp-vhost.conf"
    ssh "$SERVER" "
        set -euo pipefail
        export DEBIAN_FRONTEND=noninteractive
        apt-get update
        apt-get install -y nginx certbot python3-certbot-nginx
        # Cert first: certonly via the nginx plugin uses the running default
        # site for the HTTP-01 challenge, so the vhost can reference the cert
        # and 'nginx -t' passes.
        if [ ! -d /etc/letsencrypt/live/${NGINX_DOMAIN} ]; then
            certbot certonly --nginx -d ${NGINX_DOMAIN} \
                --non-interactive --agree-tos --register-unsafely-without-email
        fi
        install -m 0644 /tmp/mcp-vhost.conf /etc/nginx/sites-available/${NGINX_DOMAIN}
        ln -sf /etc/nginx/sites-available/${NGINX_DOMAIN} /etc/nginx/sites-enabled/${NGINX_DOMAIN}
        rm -f /etc/nginx/sites-enabled/default
        nginx -t
        systemctl reload nginx
        rm -f /tmp/mcp-vhost.conf
        echo '  nginx + cert installed for ${NGINX_DOMAIN}'
    "
    echo ""
fi

echo "=== install-precis-mcp.sh complete ==="
echo "Next: get the code + instance/ onto ${SERVER}:${REMOTE_DIR}, fill"
echo "      deploy/.env, then bring up deploy/docker-compose.yml (Track B)."

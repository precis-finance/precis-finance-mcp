#!/usr/bin/env python3
# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
# pyright: reportArgumentType=false, reportCallIssue=false, reportOptionalSubscript=false
"""Platform database migration runner.

Applies numbered SQL migrations from two roots — scripts/migrations/open/ then
scripts/migrations/commercial/ — in that order. Open runs first because every
foreign key points commercial -> open, so the open tables exist
before any commercial FK resolves. Within each root, files apply in numeric
order (NNN_*.sql). Applied migrations are tracked in a schema_migrations table,
versioned by "<root>/<NNN>" (e.g. "open/001", "commercial/001"). Idempotent —
safe to run repeatedly; only unapplied migrations are executed, and each
migration is itself written to be a no-op on re-apply.

Creates the precis_platform database if it doesn't exist.

Usage:
    python scripts/migrate.py                  # Apply pending (open then commercial)
    python scripts/migrate.py --scope open     # Open root only (open deployment)
    python scripts/migrate.py --status         # Show migration status
    python scripts/migrate.py --dry-run        # Show what would be applied

Environment:
    PGHOST, PGPORT, PGUSER, PGPASSWORD  — PostgreSQL connection
    PLATFORM_DB_NAME                      — Database name (default: precis_platform)
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import psycopg

# Allow running from project root or scripts/
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_BASE = PROJECT_ROOT / "scripts" / "migrations"
# Applied in this order; open before commercial (FK direction invariant).
MIGRATION_ROOTS = (
    ("open", MIGRATIONS_BASE / "open"),
    ("commercial", MIGRATIONS_BASE / "commercial"),
)

# Load .env if available
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

# Resolve *_FILE secrets (e.g. PGPASSWORD_FILE → PGPASSWORD) so this script
# works inside the api container where compose mounts file-secrets rather
# than setting plain env vars. Must run after load_dotenv and before any
# getenv on a secret. Import is the activation — see precis_mcp/secrets.py.
sys.path.insert(0, str(PROJECT_ROOT))
import precis_mcp.secrets  # noqa: E402,F401


def _pg_kwargs() -> dict:
    return dict(
        host=os.getenv("PGHOST", "localhost"),
        port=int(os.getenv("PGPORT", 5432)),
        user=os.getenv("PGUSER", "postgres"),
        password=os.getenv("PGPASSWORD", ""),
    )


def _db_name() -> str:
    return os.getenv("PLATFORM_DB_NAME", "precis_platform")


def ensure_database():
    """Create the platform database if it doesn't exist."""
    db_name = _db_name()
    conn = psycopg.connect(**_pg_kwargs(), dbname="postgres", autocommit=True)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
    if not cur.fetchone():
        cur.execute(f'CREATE DATABASE "{db_name}"')
        print(f"Created database: {db_name}")
    cur.close()
    conn.close()


def get_connection():
    return psycopg.connect(**_pg_kwargs(), dbname=_db_name())


def ensure_migrations_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version     TEXT PRIMARY KEY,
                filename    TEXT NOT NULL,
                applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
    conn.commit()


def get_applied(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_migrations ORDER BY version")
        return {row[0] for row in cur.fetchall()}


def discover_migrations(scope: str = "all") -> list[tuple[str, Path]]:
    """Return ordered list of (version, path) across the selected roots.

    Versions are namespaced by root ("open/001", "commercial/001") so the two
    roots never collide. Roots are emitted open-first; files within a root sort
    numerically. ``scope`` selects "all" (default), "open", or "commercial".
    """
    pattern = re.compile(r"^(\d+)_.+\.sql$")
    migrations: list[tuple[str, Path]] = []
    for root_name, root_dir in MIGRATION_ROOTS:
        if scope != "all" and scope != root_name:
            continue
        if not root_dir.is_dir():
            continue
        for f in sorted(root_dir.iterdir()):
            m = pattern.match(f.name)
            if m:
                migrations.append((f"{root_name}/{m.group(1)}", f))
    return migrations


def apply_migration(conn, version: str, path: Path):
    sql = path.read_text()
    with conn.cursor() as cur:
        cur.execute(sql)
        cur.execute(
            "INSERT INTO schema_migrations (version, filename) VALUES (%s, %s)",
            (version, path.name),
        )
    conn.commit()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Précis platform DB migrations")
    parser.add_argument("--status", action="store_true", help="Show migration status")
    parser.add_argument("--dry-run", action="store_true", help="Show pending without applying")
    parser.add_argument(
        "--scope",
        choices=("all", "open", "commercial"),
        default="all",
        help="Which migration roots to apply (default: all). "
        "Use 'open' for an open (precis-mcp) deployment.",
    )
    args = parser.parse_args()

    ensure_database()
    conn = get_connection()
    ensure_migrations_table(conn)
    applied = get_applied(conn)
    all_migrations = discover_migrations(args.scope)

    if args.status:
        for version, path in all_migrations:
            status = "applied" if version in applied else "PENDING"
            print(f"  {path.name:40s} {status}")
        conn.close()
        return

    pending = [(v, p) for v, p in all_migrations if v not in applied]

    if not pending:
        print("All migrations applied. Nothing to do.")
        conn.close()
        return

    if args.dry_run:
        print(f"{len(pending)} pending migration(s):")
        for version, path in pending:
            print(f"  {path.name}")
        conn.close()
        return

    print(f"Applying {len(pending)} migration(s)...")
    for version, path in pending:
        try:
            apply_migration(conn, version, path)
            print(f"  Applied: {path.name}")
        except Exception as e:
            conn.rollback()
            print(f"  FAILED:  {path.name} — {e}", file=sys.stderr)
            conn.close()
            sys.exit(1)

    print("Done.")
    conn.close()


if __name__ == "__main__":
    main()
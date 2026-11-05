#!/usr/bin/env python3
# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Migrate Précis users from the platform DB into Keycloak.

Reads every row from the platform DB ``users`` table and creates a matching
user in the ``precis`` realm via Keycloak's Admin REST API (through
``precis_mcp.keycloak_admin``):

  - username           = users.id
  - precis_user_id     = users.id            (attribute → JWT claim)
  - email              = users.identity['email']   (when present)
  - firstName/lastName = best-effort split of users.identity['name']

Password handling — two modes:
  - Default: a fresh random temp password, marked temporary so Keycloak
    forces UPDATE_PASSWORD on first login (hand-deliver the printed temps).
  - ``--passwords FILE``: set known passwords (one ``user:password`` per
    line) as NON-temporary — users keep their existing password, no forced
    reset.  Also fixes users already migrated with a temp password.

Idempotent.  Without ``--passwords``, existing Keycloak users are skipped.
With ``--passwords``, listed users have their password (re)set regardless.

⚠ The ``--passwords`` file is plaintext credentials.  Keep it ``0600``,
never commit it, and delete it after the run.

Environment (.env):
    PGHOST, PGPORT, PGUSER, PGPASSWORD, PLATFORM_DB_NAME
    KC_BASE_URL_INTERNAL          — defaults to http://localhost:8080/auth
    KC_PROVISIONING_CLIENT_SECRET (or _FILE) — scoped user lifecycle credential
    --enroll-existing additionally requires explicit operator credentials.

Usage:
    python scripts/keycloak/migrate_users.py
    python scripts/keycloak/migrate_users.py --dry-run
    python scripts/keycloak/migrate_users.py --out creds.txt
    python scripts/keycloak/migrate_users.py --passwords users.txt
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

# Read the provisioning credential from the file-secret rather than forcing
# it into .env as plaintext: point KC_PROVISIONING_CLIENT_SECRET_FILE at the
# Compose convention path when it exists, then let precis_mcp.secrets resolve
# *_FILE → plain var (an explicit env value still wins).
_kc_pw_file = Path(__file__).resolve().parent.parent.parent / "secrets" / "keycloak_provisioning_client_secret"
if _kc_pw_file.is_file():
    os.environ.setdefault("KC_PROVISIONING_CLIENT_SECRET_FILE", str(_kc_pw_file))
import precis_mcp.secrets  # noqa: F401 — resolves *_FILE on import

from precis_mcp.db import query_platform
from precis_mcp.keycloak_admin import KeycloakAdmin, KeycloakAdminError, temp_password


def _fetch_users() -> list[dict]:
    rows = query_platform(
        "SELECT id, identity FROM users ORDER BY id"
    )
    out: list[dict] = []
    for r in rows:
        identity = r["identity"]
        if isinstance(identity, str):
            identity = json.loads(identity) if identity else {}
        elif identity is None:
            identity = {}
        out.append({
            "id": r["id"],
            "identity": identity,
        })
    return out


def _load_password_file(path: Path) -> dict[str, str]:
    """Parse a ``user:password`` file (one pair per line).

    Splits on the FIRST colon, so passwords may contain colons.  Blank
    lines and lines starting with ``#`` are ignored.  The username is
    stripped; the password is taken verbatim after the first colon.
    """
    pairs: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            print(f"  warning: no ':' in line, skipping")
            continue
        user, pw = line.split(":", 1)
        pairs[user.strip()] = pw
    return pairs


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").split("\n\n")[0]
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Plan only; no HTTP writes.")
    parser.add_argument("--enroll-existing", action="store_true",
                        help="Operator-only enrollment of existing platform accounts; "
                             "rejects Keycloak administrators and service accounts.")
    parser.add_argument("--out", type=Path,
                        help="Write generated temp credentials to this file (0600).")
    parser.add_argument("--passwords", type=Path,
                        help="File of `user:password` pairs; set those users' "
                             "passwords as non-temporary. Plaintext — keep 0600, "
                             "delete after.")
    args = parser.parse_args()

    pw_map = _load_password_file(args.passwords) if args.passwords else {}

    users = _fetch_users()
    print(f"Found {len(users)} user(s) in the platform DB.")
    if pw_map:
        print(f"Loaded passwords for {len(pw_map)} user(s) from {args.passwords}.")

    if args.dry_run:
        print("DRY RUN — no changes will be made.")
        for u in users:
            mode = "known password" if u["id"] in pw_map else "temp + UPDATE_PASSWORD"
            print(f"  would migrate: {u['id']:<20}  [{mode}]")
        unknown = set(pw_map) - {u["id"] for u in users}
        for uid in sorted(unknown):
            print(f"  WARNING: password file lists {uid!r} but no such platform user")
        return 0

    admin = KeycloakAdmin()
    operator = None
    if args.enroll_existing:
        from precis_mcp.keycloak_operator import KeycloakOperator, enroll_application_user
        operator = KeycloakOperator()
    temp_creds: list[tuple[str, str]] = []  # only users without a provided password
    for u in users:
        uid = u["id"]
        identity = u["identity"]
        if operator is not None:
            from urllib.parse import urlencode
            existing = operator.request("GET", "/users?" + urlencode({"username": uid, "exact": "true"}))
            if existing:
                enroll_application_user(operator.request, uid)
        exists = bool(admin.find_user_id(uid))

        if uid in pw_map:
            known_pw = pw_map[uid]
            try:
                if exists:
                    admin.reset_password(username=uid, password=known_pw, temporary=False)
                    admin.clear_required_actions(uid)
                    print(f"  set known password (existing): {uid}")
                else:
                    admin.create_user(
                        username=uid, precis_user_id=uid,
                        password=known_pw, email=identity.get("email"),
                        name=identity.get("name"), temporary=False,
                    )
                    print(f"  created (known password):       {uid}")
            except KeycloakAdminError as exc:
                print(f"  fail: {uid} — {exc}")
            continue

        if exists:
            print(f"  skip (exists):                  {uid}")
            continue
        pw = temp_password()
        try:
            admin.create_user(
                username=uid, precis_user_id=uid,
                password=pw, email=identity.get("email"),
                name=identity.get("name"), temporary=True,
            )
        except KeycloakAdminError as exc:
            print(f"  fail: {uid} — {exc}")
            continue
        temp_creds.append((uid, pw))
        print(f"  created (temp password):        {uid}")

    # Report only the generated temp credentials (never the file-provided ones).
    if temp_creds:
        print()
        print("Temp credentials — hand-deliver; users reset on first login:")
        print("=" * 60)
        for uid, pw in temp_creds:
            print(f"  {uid:<20}  {pw}")
        print("=" * 60)
        if args.out:
            args.out.write_text(
                "user_id\ttemp_password\n"
                + "\n".join(f"{uid}\t{pw}" for uid, pw in temp_creds)
                + "\n"
            )
            args.out.chmod(0o600)
            print(f"Also written to {args.out} (0600)")
    else:
        print("No temp credentials generated.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

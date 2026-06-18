# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Admin CLI for the open precis-mcp package.

Headless operator surface for the platform-Postgres objects the open package owns:
users, security profiles, profile assignments. The trust boundary is host/DB
access (the same one `migrate.py` assumes) — no web server, no browser auth. It
shares all logic with the Précis admin UI via `precis_mcp.admin_ops`, so both
surfaces validate identically and write the same audit rows.

It also solves first-run bootstrap (`create-admin`): the admin UI needs an admin
to log in, but none exists at install — only the CLI can seed the first one.

Run: ``python -m precis_mcp.admin_cli <command> ...``

Modes: by default user creation provisions a Keycloak account (mode B, bundled
Keycloak). Pass ``--no-keycloak`` for mode C (external IdP) — only the platform
row is created; the IdP owns the credential.
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from typing import Any

from precis_mcp import admin_ops
from precis_mcp.admin_ops import ADMIN_EXIT_CODE, AdminError, ProfileYamlBody
from precis_mcp.backup import BACKUP_EXIT_CODE, BackupError


def _actor() -> str:
    try:
        return f"cli:{getpass.getuser()}"
    except Exception:
        return "cli"


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _read_profile_body(args: argparse.Namespace) -> ProfileYamlBody:
    """Build a ProfileYamlBody from a YAML file (or stdin via ``-``)."""
    if args.file == "-":
        raw = sys.stdin.read()
    else:
        with open(args.file, encoding="utf-8") as fh:
            raw = fh.read()
    return ProfileYamlBody(yaml=raw, change_reason=getattr(args, "reason", None))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_create_admin(args: argparse.Namespace) -> None:
    _create_user(args, is_admin=True, label="admin user")


def cmd_create_user(args: argparse.Namespace) -> None:
    _create_user(args, is_admin=bool(args.admin), label="user")


def _create_user(args: argparse.Namespace, *, is_admin: bool, label: str) -> None:
    from precis_mcp.keycloak_admin import temp_password

    provision = not args.no_keycloak
    password = args.password
    generated: str | None = None
    if provision and not password:
        password = temp_password()
        generated = password

    admin_ops.create_user(
        id=args.id,
        actor=_actor(),
        password=password,
        is_admin=is_admin,
        name=getattr(args, "name", "") or "",
        external_id=getattr(args, "external_id", None) or None,
        provision_keycloak=provision,
    )
    msg = f"created {label} {args.id!r} (is_admin={is_admin})"
    if generated:
        msg += f"\n  temporary password (change on first login): {generated}"
    if not provision:
        msg += "\n  (mode C: no Keycloak account — the external IdP owns the credential)"
    print(msg)


def cmd_set_admin(args: argparse.Namespace) -> None:
    admin_ops.set_admin(user_id=args.id, is_admin=not args.off, actor=_actor())
    print(f"set is_admin={not args.off} on {args.id!r}")


def cmd_disable_user(args: argparse.Namespace) -> None:
    admin_ops.disable_user(user_id=args.id, actor=_actor())
    print(f"disabled {args.id!r}")


def cmd_reset_password(args: argparse.Namespace) -> None:
    if args.no_keycloak:
        print(
            f"mode C (external IdP): Précis holds no credential for {args.id!r} — "
            "reset the password in your identity provider. No action taken here."
        )
        return
    from precis_mcp.keycloak_admin import temp_password

    password = args.password or temp_password()
    admin_ops.reset_password(user_id=args.id, password=password, actor=_actor())
    msg = f"reset password for {args.id!r}"
    if not args.password:
        msg += f"\n  temporary password (change on first login): {password}"
    print(msg)


def cmd_list_users(args: argparse.Namespace) -> None:
    _print_json(admin_ops.list_users())


def cmd_show_user(args: argparse.Namespace) -> None:
    user = admin_ops.get_user(args.id)
    user["profile"] = admin_ops.get_user_profile(args.id)
    _print_json(user)


def cmd_profile_create(args: argparse.Namespace) -> None:
    pid = admin_ops.create_profile(body=_read_profile_body(args), actor=_actor())
    print(f"created profile {pid!r}")


def cmd_profile_update(args: argparse.Namespace) -> None:
    admin_ops.update_profile(
        profile_id=args.id, body=_read_profile_body(args), actor=_actor()
    )
    print(f"updated profile {args.id!r}")


def cmd_profile_delete(args: argparse.Namespace) -> None:
    admin_ops.delete_profile(profile_id=args.id, actor=_actor())
    print(f"deleted profile {args.id!r}")


def cmd_profile_list(args: argparse.Namespace) -> None:
    _print_json([
        {k: v for k, v in p.items() if k != "definition"}
        for p in admin_ops.list_profiles()
    ])


def cmd_profile_show(args: argparse.Namespace) -> None:
    print(admin_ops.profile_yaml_repr(admin_ops.get_profile(args.id)))


def cmd_assign(args: argparse.Namespace) -> None:
    admin_ops.assign_profile(
        user_id=args.user, profile_id=args.profile, actor=_actor(),
        source="admin_cli",
    )
    print(f"assigned profile {args.profile!r} to {args.user!r}")


def cmd_revoke(args: argparse.Namespace) -> None:
    revoked = admin_ops.revoke_profile(user_id=args.user, actor=_actor())
    print(f"revoked profile {revoked!r} from {args.user!r}")


def cmd_audit(args: argparse.Namespace) -> None:
    _print_json(admin_ops.list_security_audit(
        actor=args.actor, target=args.target, event=args.event,
        since=args.since, limit=args.limit,
    ))


def cmd_show_access(args: argparse.Namespace) -> None:
    """Resolve effective data access (what a profile actually grants).

    Forward (``--user``): the scenarios/roles/scopes one user can read.
    Reverse (``--scenario``): which users can read one scenario. Both resolve
    the profile against live scenarios, so ClickHouse must be reachable.
    """
    from dataclasses import asdict

    from precis_mcp.auth import AuthError, load_permissions

    if args.scenario:
        users: list[dict] = []
        for u in admin_ops.list_users():
            try:
                perms = load_permissions(u["id"])
            except AuthError:
                continue  # disabled / vanished mid-iteration
            sp = perms.scenarios.get(args.scenario)
            if perms.is_admin or sp is not None:
                users.append({
                    "user_id": u["id"],
                    "is_admin": perms.is_admin,
                    "effective_role": sp.effective_role if sp else "admin (unrestricted)",
                })
        _print_json({"scenario": args.scenario, "users": users})
        return

    try:
        perms = load_permissions(args.user)
    except AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(4) from exc
    _print_json(asdict(perms))


def cmd_check_auth(args: argparse.Namespace) -> None:
    from precis_mcp.oidc import check_token_contract

    problems = check_token_contract(fetch=not args.no_fetch)
    if problems:
        print("auth conformance: PROBLEMS", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        raise SystemExit(1)
    print("auth conformance: OK")


def _backup_config_path(args: argparse.Namespace):
    from pathlib import Path

    return Path(args.config) if getattr(args, "config", None) else None


def cmd_backup_validate(args: argparse.Namespace) -> None:
    from precis_mcp.backup import ops as backup_ops

    cfg = backup_ops.op_validate(_backup_config_path(args))
    print(f"backup config OK (mode={cfg.mode}, destination={cfg.destination.type})")
    for notice in cfg.notices:
        print(f"  notice: {notice}")


def cmd_backup_init(args: argparse.Namespace) -> None:
    from pathlib import Path

    from precis_mcp.backup import ops as backup_ops

    report = backup_ops.op_init(
        _backup_config_path(args),
        render_to=Path(args.out) if args.out else None,
        check_clickhouse=not args.no_clickhouse_check,
    )
    print(f"rendered ClickHouse backup-disk config: {report.xml_path}")
    for notice in report.notices:
        print(f"  notice: {notice}")
    for warning in report.warnings:
        print(f"  warning: {warning}", file=sys.stderr)
    if not report.warnings:
        print("backup init: all checks passed")


def cmd_backup_run(args: argparse.Namespace) -> None:
    from precis_mcp.backup import ops as backup_ops

    result = backup_ops.op_run(_backup_config_path(args), trigger="cli")
    print(f"backup {result.run_id}: {result.outcome}")
    for store in result.stores:
        line = f"  {store.store}: {store.outcome}"
        if store.key:
            line += f" ({store.key}, {store.size_bytes} bytes)"
        if store.detail:
            line += f" — {store.detail}"
        print(line)
    if result.outcome != "success":
        raise SystemExit(6)


def cmd_backup_list(args: argparse.Namespace) -> None:
    from precis_mcp.backup import ops as backup_ops

    bundles = backup_ops.op_list(_backup_config_path(args))
    if args.json:
        _print_json(bundles)
        return
    if not bundles:
        print("no backup bundles at the destination")
        return
    for b in bundles:
        stores = ", ".join(f"{k}={v}" for k, v in b["stores"].items())
        print(f"{b['run_id']}  {b['outcome']:8}  {b['total_bytes']:>12} bytes  {stores}")


def cmd_backup_restore(args: argparse.Namespace) -> None:
    from precis_mcp.backup import ops as backup_ops

    stores = set(args.stores.split(",")) if args.stores else None
    result = backup_ops.op_restore(
        _backup_config_path(args),
        run_id=args.id,
        drill=args.drill,
        force=args.force,
        stores=stores,
        target_db=args.target_db,
        keep_drill=args.keep_drill,
    )
    label = "drill" if result.drill else "restore"
    print(f"{label} {result.run_id}: {result.outcome}")
    for store in result.stores:
        line = f"  {store.store}: {store.outcome}"
        if store.detail:
            line += f" — {store.detail}"
        print(line)
    if result.verification:
        mismatches = [v for v in result.verification if not v.ok]
        print(f"  verification: {len(result.verification) - len(mismatches)}/"
              f"{len(result.verification)} row counts match")
        for v in mismatches:
            print(f"    MISMATCH {v.name}: expected {v.expected}, got {v.actual}",
                  file=sys.stderr)
    if result.outcome != "success":
        raise SystemExit(6)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m precis_mcp.admin_cli",
        description="Operator admin for the open precis-mcp platform DB.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def _kc_flag(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--no-keycloak", action="store_true",
            help="Mode C: create only the platform row; the external IdP owns the credential.",
        )

    sp = sub.add_parser("create-admin", help="Create the first admin user (bootstrap).")
    sp.add_argument("--id", required=True)
    sp.add_argument("--password", help="Omit to auto-generate a temporary password.")
    sp.add_argument("--name", default="")
    sp.add_argument(
        "--external-id",
        help="External IdP identifier mapped to this user "
             "(mode C, PRECIS_IDENTITY_COLUMN=external_id).",
    )
    _kc_flag(sp)
    sp.set_defaults(func=cmd_create_admin)

    sp = sub.add_parser("create-user", help="Create a platform user.")
    sp.add_argument("--id", required=True)
    sp.add_argument("--password", help="Omit to auto-generate a temporary password.")
    sp.add_argument("--name", default="")
    sp.add_argument("--admin", action="store_true", help="Grant is_admin.")
    sp.add_argument(
        "--external-id",
        help="External IdP identifier mapped to this user "
             "(mode C, PRECIS_IDENTITY_COLUMN=external_id).",
    )
    _kc_flag(sp)
    sp.set_defaults(func=cmd_create_user)

    sp = sub.add_parser("set-admin", help="Toggle a user's is_admin flag.")
    sp.add_argument("--id", required=True)
    sp.add_argument("--off", action="store_true", help="Revoke admin instead of granting it.")
    sp.set_defaults(func=cmd_set_admin)

    sp = sub.add_parser("disable-user", help="Disable a user (Keycloak + platform).")
    sp.add_argument("--id", required=True)
    sp.set_defaults(func=cmd_disable_user)

    sp = sub.add_parser("reset-password", help="Reset a user's password (mode B / bundled Keycloak).")
    sp.add_argument("--id", required=True)
    sp.add_argument("--password", help="Omit to auto-generate a temporary password.")
    sp.add_argument(
        "--no-keycloak", action="store_true",
        help="Mode C: Précis holds no credential — print IdP-reset guidance and do nothing.",
    )
    sp.set_defaults(func=cmd_reset_password)

    sp = sub.add_parser("list-users", help="List platform users.")
    sp.set_defaults(func=cmd_list_users)

    sp = sub.add_parser("show-user", help="Show a user + its profile assignment.")
    sp.add_argument("--id", required=True)
    sp.set_defaults(func=cmd_show_user)

    # profile <create|update|delete|list|show>
    pp = sub.add_parser("profile", help="Manage security profiles.")
    psub = pp.add_subparsers(dest="profile_cmd", required=True)

    cp = psub.add_parser("create", help="Create a profile from a YAML file ('-' for stdin).")
    cp.add_argument("--file", required=True)
    cp.add_argument("--reason", help="Audit change reason.")
    cp.set_defaults(func=cmd_profile_create)

    up = psub.add_parser("update", help="Update a profile from a YAML file ('-' for stdin).")
    up.add_argument("--id", required=True)
    up.add_argument("--file", required=True)
    up.add_argument("--reason", help="Audit change reason.")
    up.set_defaults(func=cmd_profile_update)

    dp = psub.add_parser("delete", help="Delete a profile (must be unassigned).")
    dp.add_argument("--id", required=True)
    dp.set_defaults(func=cmd_profile_delete)

    lp = psub.add_parser("list", help="List profiles.")
    lp.set_defaults(func=cmd_profile_list)

    shp = psub.add_parser("show", help="Show a profile as YAML.")
    shp.add_argument("--id", required=True)
    shp.set_defaults(func=cmd_profile_show)

    sp = sub.add_parser("assign", help="Assign a profile to a user.")
    sp.add_argument("--user", required=True)
    sp.add_argument("--profile", required=True)
    sp.set_defaults(func=cmd_assign)

    sp = sub.add_parser("revoke", help="Revoke a user's profile assignment.")
    sp.add_argument("--user", required=True)
    sp.set_defaults(func=cmd_revoke)

    sp = sub.add_parser("audit", help="Read the security audit log (filter + export as JSON).")
    sp.add_argument("--actor", help="Filter by actor id.")
    sp.add_argument("--target", help="Filter by target user id.")
    sp.add_argument("--event", help="Filter by event_type (e.g. profile_assigned).")
    sp.add_argument("--since", help="ISO-8601 lower bound on created_at.")
    sp.add_argument("--limit", type=int, default=100, help="Max rows, newest first (default 100).")
    sp.set_defaults(func=cmd_audit)

    sp = sub.add_parser(
        "show-access",
        help="Resolve effective data access (needs ClickHouse). One of --user / --scenario.",
    )
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--user", help="What scenarios/roles/scopes this user can read.")
    g.add_argument("--scenario", help="Which users can read this scenario.")
    sp.set_defaults(func=cmd_show_access)

    sp = sub.add_parser(
        "check-auth",
        help="Validate the OIDC issuer/JWKS/audience config (conformance self-check).",
    )
    sp.add_argument(
        "--no-fetch", action="store_true",
        help="Static checks only — skip the network reachability checks.",
    )
    sp.set_defaults(func=cmd_check_auth)

    # backup <validate|init|run|list|restore>
    bp = sub.add_parser("backup", help="Backup and restore the Précis stores.")
    bsub = bp.add_subparsers(dest="backup_cmd", required=True)

    def _cfg_flag(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--config",
            help="backup.yml path (default: <instance>/backup.yml).",
        )

    bv = bsub.add_parser("validate", help="Parse and statically validate backup.yml.")
    _cfg_flag(bv)
    bv.set_defaults(func=cmd_backup_validate)

    bi = bsub.add_parser(
        "init",
        help="Render the ClickHouse backup-disk config from backup.yml and "
             "verify the setup (disk visible, destination writable, credentials).",
    )
    _cfg_flag(bi)
    bi.add_argument(
        "--out",
        help="Rendered XML path (default: deploy/secrets/precis_backup_disk.xml).",
    )
    bi.add_argument(
        "--no-clickhouse-check", action="store_true",
        help="Skip the live ClickHouse disk check.",
    )
    bi.set_defaults(func=cmd_backup_init)

    br = bsub.add_parser("run", help="Execute one backup run and prune per retention.")
    _cfg_flag(br)
    br.set_defaults(func=cmd_backup_run)

    bl = bsub.add_parser("list", help="List backup bundles at the destination.")
    _cfg_flag(bl)
    bl.add_argument("--json", action="store_true", help="JSON output.")
    bl.set_defaults(func=cmd_backup_list)

    bre = bsub.add_parser("restore", help="Restore a backup bundle (or run a drill).")
    _cfg_flag(bre)
    bre.add_argument("--id", required=True, help="Run id of the bundle (see `backup list`).")
    bre.add_argument(
        "--drill", action="store_true",
        help="Restore into side databases and verify against the manifest — never touches live data.",
    )
    bre.add_argument(
        "--force", action="store_true",
        help="Overwrite non-empty targets (real restore only).",
    )
    bre.add_argument(
        "--stores",
        help="Comma-separated subset: postgres,clickhouse,instance,files (default: all in the bundle).",
    )
    bre.add_argument("--target-db", help="Restore Postgres into this database instead of the platform DB.")
    bre.add_argument(
        "--keep-drill", action="store_true",
        help="Keep the drill databases for inspection instead of dropping them.",
    )
    bre.set_defaults(func=cmd_backup_restore)

    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except AdminError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(ADMIN_EXIT_CODE.get(type(exc), 1)) from exc
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(BACKUP_EXIT_CODE.get(type(exc), 1)) from exc


if __name__ == "__main__":
    main()

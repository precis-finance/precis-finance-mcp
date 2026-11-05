# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""In-memory PostgreSQL substitute for the public MCP package contracts."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from threading import RLock
from typing import Any


class FakePlatformDB:
    """Core identity, permission, audit, backup and artifact state."""

    def __init__(self):
        self.users: list[dict] = []
        self.profiles: list[dict] = []
        self.user_profile_assignments: list[dict] = []
        self.profile_audit: list[dict] = []
        self.validation_rules: list[dict] = []
        self.files: list[dict] = []
        self.artifact_uploads: list[dict] = []
        self.artifact_blob_gc: list[dict] = []
        self.security_audit_log: list[dict] = []
        self.backup_history: list[dict] = []
        self._profile_audit_seq = 0
        self._security_audit_seq = 0
        self._commit_should_fail: Exception | None = None
        self._artifact_lock = RLock()

    def _transaction_table_names(self) -> tuple[str, ...]:
        """Tables restored together when a transaction rolls back."""
        return ("files", "artifact_uploads", "artifact_blob_gc")

    def query(self, sql: str, params: tuple | list | None = None) -> list[dict]:
        params = params or ()
        sql_lower = sql.strip().lower()
        return self._route_query(sql_lower, sql, params)

    def execute(self, sql: str, params: tuple | list | None = None) -> dict | None:
        params = params or ()
        sql_lower = sql.strip().lower()
        return self._route_execute(sql_lower, sql, params)

    def transaction(self, *, timeout_seconds=None):
        """Match the ``transaction_platform()`` context manager.

        Yields a cursor that routes ``SELECT ... FOR UPDATE`` to the
        normal query path (FOR UPDATE is a no-op against the in-memory
        store — single-threaded tests don't need real locking) and
        UPDATE/INSERT to the execute path.

        Subsequent ``cur.fetchone()`` calls return the row from the
        last operation. Configured tables are snapshotted
        under the transaction lock and restored on rollback/commit failure.
        """
        return _FakeTransaction(self)

    def _route_query(self, sql_lower: str, sql: str, params) -> list[dict]:
        if "union all" in sql_lower and "storage_path" in sql_lower:
            return [{"storage_path": r["storage_path"]} for r in (*self.files, *self.artifact_uploads, *self.artifact_blob_gc)]
        if "from artifact_blob_gc" in sql_lower and " as used" not in sql_lower:
            rows = self.artifact_blob_gc
            if "storage_path = %s" in sql_lower: rows = [r for r in rows if r["storage_path"] == params[0]]
            if "user_id = %s" in sql_lower: rows = [r for r in rows if r["user_id"] == params[0]]
            return [dict(r) for r in rows]
        if "from artifact_uploads" in sql_lower and " as used" not in sql_lower:
            now = datetime.now(timezone.utc)
            rows = self.artifact_uploads
            if "upload_id = %s" in sql_lower:
                rows = [r for r in rows if r["upload_id"] == params[0] and (len(params) < 2 or r["user_id"] == params[1])]
            if "expires_at > now()" in sql_lower: rows = [r for r in rows if r["expires_at"] > now]
            if "expires_at <= now()" in sql_lower: rows = [r for r in rows if r["expires_at"] <= now]
            return [dict(r) for r in rows]
        if "pg_advisory_xact_lock" in sql_lower:
            return []
        if "from users" in sql_lower:
            return self._query_users(sql_lower, params)
        if "from user_profile_assignments" in sql_lower:
            return self._query_user_profile_assignments(sql_lower, params)
        if "from profile_audit" in sql_lower:
            return self._query_profile_audit(sql_lower, params)
        if "from security_audit_log" in sql_lower:
            return self._query_security_audit_log(sql_lower, params)
        if "from profiles" in sql_lower:
            return self._query_profiles(sql_lower, params)
        if "from validation_rules" in sql_lower:
            return self._query_validation_rules(sql_lower, params)
        if "from files" in sql_lower:
            return self._query_files(sql_lower, params)
        return []

    def _route_execute(self, sql_lower: str, sql: str, params) -> dict | None:
        if sql_lower.startswith("insert into artifact_blob_gc"):
            path, owner, size = params
            self.artifact_blob_gc.append(dict(storage_path=path, user_id=owner, size_bytes=size))
            return None
        if sql_lower.startswith("delete from artifact_blob_gc"):
            self.artifact_blob_gc[:] = [r for r in self.artifact_blob_gc if r["storage_path"] != params[0]]
            return None
        if sql_lower.startswith("insert into artifact_uploads"):
            upload_id,user_id,basename,mime_type,size_bytes,storage_path,expires_at = params
            self.artifact_uploads.append(dict(upload_id=upload_id,user_id=user_id,basename=basename,mime_type=mime_type,size_bytes=size_bytes,storage_path=storage_path,expires_at=expires_at,state="pending",file_id=None))
            return None
        if sql_lower.startswith("delete from artifact_uploads"):
            self.artifact_uploads[:] = [r for r in self.artifact_uploads if r["upload_id"] != params[0]]
            return None
        if sql_lower.startswith("update artifact_uploads"):
            complete = "state = 'complete'" in sql_lower
            upload_id = params[1] if complete else params[0]
            row = next((r for r in self.artifact_uploads if r["upload_id"] == upload_id),None)
            if row is None:return None
            if complete:row.update(state="complete",file_id=params[0]);return dict(row)
            if row["user_id"] != params[1]:return None
            for required in ["pending","receiving"]:
                if f"and state = '{required}'" in sql_lower and row["state"] != required:return None
            if "expires_at > now()" in sql_lower and row["expires_at"] <= datetime.now(timezone.utc):return None
            for state in ["receiving","uploaded","pending"]:
                if f"set state = '{state}'" in sql_lower:row["state"] = state
            return dict(row)
        if sql_lower.startswith("insert into users"):
            return self._insert_user(sql_lower, params)
        if sql_lower.startswith("update users"):
            return self._update_user(sql_lower, params)
        if sql_lower.startswith("insert into user_profile_assignments"):
            return self._upsert_user_profile_assignment(sql_lower, params)
        if sql_lower.startswith("delete from user_profile_assignments"):
            return self._delete_user_profile_assignment(params)
        if sql_lower.startswith("insert into profile_audit"):
            return self._insert_profile_audit(sql_lower, params)
        if sql_lower.startswith("insert into security_audit_log"):
            return self._insert_security_audit_log(params)
        if sql_lower.startswith("insert into backup_history"):
            return self._insert_backup_history(params)
        if sql_lower.startswith("insert into profiles"):
            return self._insert_profile(params)
        if sql_lower.startswith("update profiles"):
            return self._update_profile(params)
        if sql_lower.startswith("delete from profiles"):
            return self._delete_profile(params)
        if sql_lower.startswith("insert into validation_rules"):
            return self._upsert_validation_rules(params)
        if sql_lower.startswith("insert into files"):
            return self._insert_file(params)
        if sql_lower.startswith("update files"):
            return self._update_file(sql_lower, params)
        if sql_lower.startswith("delete from files"):
            return self._delete_files(sql_lower, params)
        return None

    def _query_users(self, sql_lower: str, params) -> list[dict]:
        if "where id = %s" in sql_lower:
            return [row for row in self.users if row["id"] == params[0]]
        results = list(self.users)
        if "is_disabled = false" in sql_lower:
            results = [row for row in results if not row.get("is_disabled", False)]
        if "ilike" in sql_lower and len(params) >= 2 and isinstance(params[0], str):
            needle = params[0].strip("%").lower()
            results = [row for row in results if needle in row["id"].lower()]
        results.sort(key=lambda row: row["id"])
        return results

    def _insert_user(self, sql_lower: str, params) -> dict | None:
        # create_user (admin_ops): (id, is_admin) or, with a mode-C external
        # identity, (id, is_admin, external_id).
        if len(params) in (2, 3):
            user_id = params[0]
            base = self._default_user(user_id)
            base["is_admin"] = bool(params[1])
            if len(params) == 3:
                base["external_id"] = params[2]
            existing = [u for u in self.users if u["id"] == user_id]
            if existing:
                existing[0].update(base)
            else:
                self.users.append(base)
            return {"id": user_id}
        return None

    def _update_user(self, sql_lower: str, params) -> dict | None:
        user_id = params[-1]
        target = next((row for row in self.users if row["id"] == user_id), None)
        if target is None:
            return None
        for field in ("is_admin", "is_disabled"):
            if re.search(rf"{field}\s*=\s*true", sql_lower):
                target[field] = True
            if re.search(rf"{field}\s*=\s*false", sql_lower):
                target[field] = False
        field_params = iter(params[:-1])
        for field in ("is_admin", "is_disabled"):
            if f"{field} = %s" in sql_lower:
                target[field] = next(field_params)
        target["updated_at"] = datetime.now(timezone.utc)
        return {"id": user_id}

    def _default_user(self, user_id: str) -> dict:
        return {
            "id": user_id,
            "is_admin": False,
            "is_disabled": False,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }

    def _query_user_profile_assignments(self, sql_lower: str, params) -> list[dict]:
        # load_permissions JOIN: SELECT p.definition FROM user_profile_assignments upa
        #   JOIN profiles p ON p.profile_id = upa.profile_id
        #   WHERE upa.user_id = %s AND (upa.expires_at IS NULL OR upa.expires_at > now())
        if "join profiles" in sql_lower and "p.definition" in sql_lower:
            user_id = params[0]
            now = datetime.now(timezone.utc)
            rows: list[dict] = []
            for upa in self.user_profile_assignments:
                if upa["user_id"] != user_id:
                    continue
                exp = upa.get("expires_at")
                if exp is not None and exp <= now:
                    continue
                profile = next(
                    (p for p in self.profiles if p["profile_id"] == upa["profile_id"]),
                    None,
                )
                if profile is None:
                    continue
                rows.append({"definition": profile["definition"]})
            return rows
        # list_user_profiles JOIN: SELECT upa.user_id, upa.profile_id, p.name AS profile_name,
        #   upa.granted_by, upa.granted_at, upa.expires_at, upa.source
        #   FROM user_profile_assignments upa JOIN profiles p ...
        if "join profiles" in sql_lower and "profile_name" in sql_lower:
            rows = list(self.user_profile_assignments)
            if "where upa.user_id = %s" in sql_lower:
                rows = [r for r in rows if r["user_id"] == params[0]]
            enriched: list[dict] = []
            for upa in rows:
                profile = next(
                    (p for p in self.profiles if p["profile_id"] == upa["profile_id"]),
                    None,
                )
                if profile is None:
                    continue
                enriched.append({
                    "user_id": upa["user_id"],
                    "profile_id": upa["profile_id"],
                    "profile_name": profile.get("name"),
                    "granted_by": upa.get("granted_by", ""),
                    "granted_at": upa.get("granted_at"),
                    "expires_at": upa.get("expires_at"),
                    "source": upa.get("source", "api"),
                })
            return enriched
        if "where user_id = %s" in sql_lower:
            user_id = params[0]
            return [r for r in self.user_profile_assignments if r["user_id"] == user_id]
        return self.user_profile_assignments

    def _upsert_user_profile_assignment(self, sql_lower: str, params) -> dict | None:
        # INSERT INTO user_profile_assignments (user_id, profile_id, granted_by, source)
        #   VALUES (%s, %s, %s, %s) ON CONFLICT (user_id) DO UPDATE SET ...
        user_id, profile_id, granted_by, source = params[0], params[1], params[2], params[3]
        existing = next(
            (u for u in self.user_profile_assignments if u["user_id"] == user_id),
            None,
        )
        if existing:
            existing["profile_id"] = profile_id
            existing["granted_by"] = granted_by
            existing["granted_at"] = datetime.now(timezone.utc)
            existing["source"] = source
        else:
            self.user_profile_assignments.append({
                "user_id": user_id,
                "profile_id": profile_id,
                "granted_by": granted_by,
                "granted_at": datetime.now(timezone.utc),
                "expires_at": None,
                "source": source,
            })
        return {"user_id": user_id}

    def _delete_user_profile_assignment(self, params) -> dict | None:
        user_id = params[0]
        before = len(self.user_profile_assignments)
        self.user_profile_assignments = [
            u for u in self.user_profile_assignments if u["user_id"] != user_id
        ]
        return {"user_id": user_id} if len(self.user_profile_assignments) < before else None

    def _query_profiles(self, sql_lower: str, params) -> list[dict]:
        if "where profile_id = %s" in sql_lower:
            pid = params[0]
            return [r for r in self.profiles if r["profile_id"] == pid]
        return self.profiles

    def _query_profile_audit(self, sql_lower: str, params) -> list[dict]:
        # SELECT ... FROM profile_audit WHERE profile_id = %s ORDER BY changed_at DESC
        pid = params[0]
        rows = [r for r in self.profile_audit if r["profile_id"] == pid]
        rows.sort(
            key=lambda r: (r.get("changed_at", ""), r.get("audit_id", 0)),
            reverse=True,
        )
        return [dict(r) for r in rows]

    def _insert_profile(self, params) -> dict | None:
        # INSERT INTO profiles (profile_id, name, description, definition, updated_by)
        profile_id, name, description, definition, updated_by = params
        now = datetime.now(timezone.utc)
        self.profiles.append({
            "profile_id": profile_id,
            "name": name,
            "description": description,
            "definition": _parse_jsonb(definition),
            "updated_by": updated_by,
            "updated_at": now,
        })
        return {"profile_id": profile_id}

    def _update_profile(self, params) -> dict | None:
        # UPDATE profiles SET name = %s, description = %s, definition = %s::jsonb,
        #   updated_by = %s, updated_at = now() WHERE profile_id = %s
        name, description, definition, updated_by, profile_id = params
        for p in self.profiles:
            if p["profile_id"] == profile_id:
                p["name"] = name
                p["description"] = description
                p["definition"] = _parse_jsonb(definition)
                p["updated_by"] = updated_by
                p["updated_at"] = datetime.now(timezone.utc)
                return {"profile_id": profile_id}
        return None

    def _delete_profile(self, params) -> dict | None:
        profile_id = params[0]
        before = len(self.profiles)
        self.profiles = [p for p in self.profiles if p["profile_id"] != profile_id]
        return {"profile_id": profile_id} if len(self.profiles) < before else None

    def _insert_profile_audit(self, sql_lower: str, params) -> dict:
        # change_kind is a SQL literal ('create' | 'update' | 'delete'), not a
        # bound parameter. Parse it from the SQL. Two parameter shapes:
        #   4 params: (profile_id, definition, changed_by, change_reason) — create/update
        #   3 params: (profile_id, definition, changed_by)                — delete
        self._profile_audit_seq += 1
        if "'create'" in sql_lower:
            change_kind = "create"
        elif "'update'" in sql_lower:
            change_kind = "update"
        elif "'delete'" in sql_lower:
            change_kind = "delete"
        else:
            change_kind = "unknown"
        if len(params) == 4:
            profile_id, definition, changed_by, change_reason = params
        else:
            profile_id, definition, changed_by = params
            change_reason = None
        row = {
            "audit_id": self._profile_audit_seq,
            "profile_id": profile_id,
            "definition": _parse_jsonb(definition),
            "changed_by": changed_by,
            "changed_at": datetime.now(timezone.utc),
            "change_reason": change_reason,
            "change_kind": change_kind,
        }
        self.profile_audit.append(row)
        return {"audit_id": self._profile_audit_seq}

    def _insert_security_audit_log(self, params) -> dict:
        """INSERT INTO security_audit_log
        (event_type, actor_id, target_user_id, scenario_id, details, trace_id)
        VALUES (%s, %s, %s, %s, %s::jsonb, %s). Matches the single writer
        `precis_mcp/db.py::write_security_audit`."""
        self._security_audit_seq += 1
        event_type, actor_id, target_user_id, scenario_id, details, trace_id = params
        row = {
            "id": self._security_audit_seq,
            "event_type": event_type,
            "actor_id": actor_id,
            "target_user_id": target_user_id,
            "scenario_id": scenario_id,
            "details": _parse_jsonb(details),
            "trace_id": trace_id,
            "created_at": datetime.now(timezone.utc),
        }
        self.security_audit_log.append(row)
        return {"id": self._security_audit_seq}

    def _query_security_audit_log(self, sql_lower: str, params) -> list[dict]:
        """SELECT ... FROM security_audit_log [WHERE ...] ORDER BY created_at
        DESC LIMIT %s — the read path behind admin_ops.list_security_audit."""
        rows = list(self.security_audit_log)
        idx = 0
        if "actor_id = %s" in sql_lower:
            rows = [r for r in rows if r["actor_id"] == params[idx]]
            idx += 1
        if "target_user_id = %s" in sql_lower:
            rows = [r for r in rows if r.get("target_user_id") == params[idx]]
            idx += 1
        if "event_type = %s" in sql_lower:
            rows = [r for r in rows if r["event_type"] == params[idx]]
            idx += 1
        if "created_at >= %s" in sql_lower:
            cutoff = params[idx]
            idx += 1
            if isinstance(cutoff, str):
                cutoff = datetime.fromisoformat(cutoff)
            if cutoff.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=timezone.utc)
            rows = [r for r in rows if r["created_at"] >= cutoff]
        rows.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
        limit = params[idx] if idx < len(params) else None
        if isinstance(limit, int):
            rows = rows[:limit]
        return [dict(r) for r in rows]

    def _insert_backup_history(self, params) -> None:
        """INSERT INTO backup_history (run_id, kind, mode, triggered_by,
        started_at, finished_at, duration_ms, outcome, artifacts,
        manifest_key, total_bytes, error_message). Matches the writer in
        `precis_mcp/backup/history.py::record_history`."""
        (run_id, kind, mode, triggered_by, started_at, finished_at,
         duration_ms, outcome, artifacts, manifest_key, total_bytes,
         error_message) = params
        self.backup_history.append({
            "run_id": run_id,
            "kind": kind,
            "mode": mode,
            "triggered_by": triggered_by,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "outcome": outcome,
            "artifacts": _parse_jsonb(artifacts),
            "manifest_key": manifest_key,
            "total_bytes": total_bytes,
            "error_message": error_message,
        })
        return None

    def _query_validation_rules(self, sql_lower: str, params) -> list[dict]:
        # SELECT DISTINCT dataset_key FROM validation_rules WHERE scenario_id = %s
        if "distinct dataset_key" in sql_lower:
            scenario_id = params[0]
            matching = [r for r in self.validation_rules if r["scenario_id"] == scenario_id]
            seen: dict[str, dict] = {}
            for r in matching:
                dk = r.get("dataset_key", "gl_plan")
                if dk not in seen:
                    seen[dk] = {"dataset_key": dk}
            return list(seen.values())

        scenario_id = params[0]
        matching = [r for r in self.validation_rules if r["scenario_id"] == scenario_id]

        param_idx = 1
        # Filter by dataset_key if present
        if "dataset_key = %s" in sql_lower and len(params) > param_idx:
            dataset_key = params[param_idx]
            matching = [r for r in matching if r.get("dataset_key", "gl_plan") == dataset_key]
            param_idx += 1

        # Handle specific version lookup: WHERE ... AND updated_at = %s
        if "and updated_at = %s" in sql_lower and len(params) > param_idx:
            ts = params[param_idx]
            matching = [r for r in matching if str(r["updated_at"]) == str(ts)]
            return matching

        # Sort by updated_at descending (newest first)
        matching.sort(key=lambda x: x.get("updated_at", ""), reverse=True)

        # Handle LIMIT 1 (latest version)
        if "limit 1" in sql_lower:
            return matching[:1]

        return matching

    def _upsert_validation_rules(self, params) -> dict | None:
        # New versioned insert with dataset_key: (scenario_id, dataset_key, description, rules_code, created_by)
        if len(params) == 5:
            scenario_id, dataset_key, description, rules_code, created_by = params
            now = datetime.now(timezone.utc)
            self.validation_rules.append({
                "scenario_id": scenario_id,
                "dataset_key": dataset_key,
                "description": description,
                "rules_code": rules_code,
                "created_by": created_by,
                "updated_at": now,
            })
            return {"scenario_id": scenario_id, "dataset_key": dataset_key, "updated_at": now, "created_by": created_by}

        # Legacy versioned insert without dataset_key: (scenario_id, description, rules_code, created_by)
        if len(params) == 4:
            scenario_id, description, rules_code, created_by = params
            now = datetime.now(timezone.utc)
            self.validation_rules.append({
                "scenario_id": scenario_id,
                "dataset_key": "gl_plan",
                "description": description,
                "rules_code": rules_code,
                "created_by": created_by,
                "updated_at": now,
            })
            return {"scenario_id": scenario_id, "dataset_key": "gl_plan", "updated_at": now, "created_by": created_by}

        # Legacy 2-param insert (backward compat for old tests)
        if len(params) == 2:
            scenario_id, rules_code = params
            now = datetime.now(timezone.utc)
            self.validation_rules.append({
                "scenario_id": scenario_id,
                "dataset_key": "gl_plan",
                "description": "",
                "rules_code": rules_code,
                "created_by": "system",
                "updated_at": now,
            })
            return {"scenario_id": scenario_id}

    def _query_files(self, sql_lower, params):
        now = datetime.now(timezone.utc)
        if " as used" in sql_lower:
            owner = params[0]
            excluded = params[3] if len(params)>3 else None
            stored = sum(r["size_bytes"] for r in self.files if r["user_id"]==owner)
            reserved = sum(r["size_bytes"] for r in self.artifact_uploads if r["user_id"]==owner and r["state"]!="complete" and r["upload_id"]!=excluded)
            retired = sum(r["size_bytes"] for r in self.artifact_blob_gc if r["user_id"] == owner)
            return [{"used":stored+reserved+retired}]
        rows = list(self.files)
        if "where file_id = %s" in sql_lower:
            rows = [r for r in rows if r["file_id"]==params[0]]
            if "and user_id = %s" in sql_lower:rows = [r for r in rows if r["user_id"]==params[1]]
        elif "where user_id = %s" in sql_lower:rows = [r for r in rows if r["user_id"]==params[0]]
        if "deleted_at is null" in sql_lower:rows = [r for r in rows if r.get("deleted_at") is None]
        if "(expires_at is null or expires_at > now())" in sql_lower:rows = [r for r in rows if r.get("expires_at") is None or r["expires_at"]>now]
        if "deleted_at is not null or expires_at <= now()" in sql_lower:rows = [r for r in rows if r.get("deleted_at") is not None or (r.get("expires_at") is not None and r["expires_at"]<=now)]
        if "limit 0" in sql_lower:return []
        return [dict(r) for r in rows]

    def _insert_file(self, params):
        (file_id,user_id,basename,mime_type,size_bytes,sha256,storage_path,expires_at,source,producing_tool,provenance,access) = params
        now = datetime.now(timezone.utc)
        row=dict(file_id=file_id,user_id=user_id,basename=basename,mime_type=mime_type,size_bytes=size_bytes,sha256=sha256,storage_path=storage_path,
                 expires_at=expires_at,source=source,producing_tool=producing_tool,provenance=_parse_jsonb(provenance),access=_parse_jsonb(access),version=1,created_at=now,updated_at=now,deleted_at=None)
        self.files.append(row)
        return dict(row)

    def _update_file(self, sql_lower, params):
        if "set size_bytes = %s" in sql_lower:
            size,sha,tool,storage_path,access,file_id=params
            row=next((r for r in self.files if r["file_id"]==file_id),None)
            if row is None:return None
            row.update(size_bytes=size,sha256=sha,storage_path=storage_path,access=_parse_jsonb(access),producing_tool=tool or row.get("producing_tool"),version=row.get("version",1)+1,updated_at=datetime.now(timezone.utc))
            return dict(row)
        if "set expires_at = %s" in sql_lower:
            expiry,file_id=params
            row=next((r for r in self.files if r["file_id"]==file_id),None)
            if row is None:return None
            row["expires_at"]=expiry
            return dict(row)
        if "deleted_at =" in sql_lower:
            row=next((r for r in self.files if r["file_id"]==params[0] and (len(params)<2 or r["user_id"]==params[1])),None)
            if row is None or row.get("deleted_at") is not None:return None
            row["deleted_at"]=datetime.now(timezone.utc)
            return dict(row)
        return None

    def _delete_files(self, sql_lower, params):
        file_id=params[0]
        self.files[:] = [r for r in self.files if r["file_id"]!=file_id]
        for upload in self.artifact_uploads:
            if upload.get("file_id")==file_id:upload["file_id"]=None
        return None


def _parse_jsonb(value: Any) -> Any:
    """Parse a JSONB parameter — if it's a string, JSON-decode it."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return value
    return value


class _FakeCursor:
    """Minimal psycopg-style cursor backed by FakePlatformDB.

    Supports the SELECT ... FOR UPDATE / UPDATE ... RETURNING pattern
    used by ``precis_mcp.artifacts.registry.update_file_blob``. SELECT
    routes to ``db.query``; UPDATE routes to ``db.execute``. The
    last result is buffered for ``fetchone``.
    """

    def __init__(self, db: "FakePlatformDB"):
        self._db = db
        self._buffer: list[dict] = []

    def execute(self, sql: str, params=None) -> None:
        sql_lower = sql.strip().lower()
        if sql_lower.startswith("select"):
            self._buffer = self._db.query(sql, params)
            return
        # UPDATE/INSERT/DELETE — execute() returns one row when the
        # SQL has RETURNING; box it as a 1-element buffer.
        result = self._db.execute(sql, params)
        self._buffer = [result] if result is not None else []

    def fetchone(self) -> dict | None:
        return self._buffer[0] if self._buffer else None

    def fetchall(self) -> list[dict]:
        return list(self._buffer)


class _FakeTransaction:
    """Context-manager wrapper yielding a _FakeCursor.

    Tests can install a ``_commit_should_fail`` exception on the
    underlying ``FakePlatformDB`` to simulate a transaction commit
    that fails *after* the with block's body has run successfully —
    matches the real ``transaction_platform()`` shape, where
    ``conn.commit()`` happens as the with block exits.
    """

    def __init__(self, db: "FakePlatformDB"):
        self._db = db

    def __enter__(self) -> _FakeCursor:
        import copy
        self._db._artifact_lock.acquire()
        self._tables = self._db._transaction_table_names()
        self._before = tuple(copy.deepcopy(getattr(self._db, name)) for name in self._tables)
        return _FakeCursor(self._db)

    def __exit__(self, exc_type, exc, tb) -> None:
        failure = self._db._commit_should_fail if exc_type is None else None
        try:
            if exc_type is not None or failure is not None:
                for name, value in zip(self._tables, self._before):
                    setattr(self._db, name, value)
            if failure is not None:
                self._db._commit_should_fail=None
                raise failure
        finally:
            self._db._artifact_lock.release()
        return None

# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Shared in-memory mock of the precis_platform PostgreSQL database.

Used by tests to replace ``query_platform`` / ``execute_platform`` without
needing a real database. Supports the tables: users, conversations,
workstreams, tasks, memories, validation_rules.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any


class FakePlatformDB:
    """Minimal in-memory mock of the platform database."""

    def __init__(self):
        self.users: list[dict] = []
        self.conversations: list[dict] = []
        self.workstreams: list[dict] = []
        self.tasks: list[dict] = []
        self.memories: list[dict] = []
        self.profiles: list[dict] = []
        self.user_profile_assignments: list[dict] = []
        self.profile_audit: list[dict] = []
        self.validation_rules: list[dict] = []
        self.client_prompt_guidance: list[dict] = []
        self.reports: list[dict] = []
        self.files: list[dict] = []
        self.conversation_audit: list[dict] = []
        self.security_audit_log: list[dict] = []
        self.backup_history: list[dict] = []
        self._audit_seq = 0
        self._profile_audit_seq = 0
        self._security_audit_seq = 0
        # Test injection point: when set, _FakeTransaction.__exit__
        # raises this exception on the clean-exit path, simulating a
        # transaction commit failure.
        self._commit_should_fail: Exception | None = None

    # ------------------------------------------------------------------
    # Public interface matching query_platform / execute_platform
    # ------------------------------------------------------------------

    def query(self, sql: str, params: tuple | list | None = None) -> list[dict]:
        params = params or ()
        sql_lower = sql.strip().lower()
        return self._route_query(sql_lower, sql, params)

    def execute(self, sql: str, params: tuple | list | None = None) -> dict | None:
        params = params or ()
        sql_lower = sql.strip().lower()
        return self._route_execute(sql_lower, sql, params)

    def transaction(self):
        """Match the ``transaction_platform()`` context manager.

        Yields a cursor that routes ``SELECT ... FOR UPDATE`` to the
        normal query path (FOR UPDATE is a no-op against the in-memory
        store — single-threaded tests don't need real locking) and
        UPDATE/INSERT to the execute path.

        Subsequent ``cur.fetchone()`` calls return the row from the
        last operation. Multi-statement transactions work; rollback
        is a no-op since we don't snapshot state.
        """
        return _FakeTransaction(self)

    # ------------------------------------------------------------------
    # Query routing
    # ------------------------------------------------------------------

    def _route_query(self, sql_lower: str, sql: str, params) -> list[dict]:
        # Postgres advisory-lock helper used by registry.update_file_blob
        # to serialise concurrent updates per user. The single-threaded
        # in-memory store doesn't need real locking — return an empty
        # result set so the cursor's fetchone is well-defined.
        if "pg_advisory_xact_lock" in sql_lower:
            return []
        # user_profile_ext (Précis platform profile columns) + the user_full join
        # view are modelled as projections over the single self.users store
        # (the open/Précis column split is physical in Postgres only; the
        # fake routes the new names so the repointed code resolves correctly).
        if "from user_full" in sql_lower:
            return self._query_users(sql_lower, params)
        if "from user_profile_ext" in sql_lower:
            return self._query_user_profile_ext(sql_lower, params)
        if "from users" in sql_lower:
            return self._query_users(sql_lower, params)
        if "from conversations" in sql_lower:
            return self._query_conversations(sql_lower, params)
        if "from workstreams" in sql_lower:
            return self._query_workstreams(sql_lower, params)
        if "from tasks" in sql_lower:
            return self._query_tasks(sql_lower, sql, params)
        if "from memories" in sql_lower:
            return self._query_memories(sql_lower, sql, params)
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
        if "from client_prompt_guidance" in sql_lower:
            return self._query_client_prompt_guidance(sql_lower, params)
        if "from reports" in sql_lower:
            return self._query_reports(sql_lower, params)
        if "from files" in sql_lower:
            return self._query_files(sql_lower, params)
        return []

    def _route_execute(self, sql_lower: str, sql: str, params) -> dict | None:
        if sql_lower.startswith("with cascaded as"):
            return self._cascade_archive_conversations(params)
        if sql_lower.startswith("insert into user_profile_ext"):
            return self._upsert_user_profile_ext(sql_lower, params)
        if sql_lower.startswith("insert into users"):
            return self._insert_user(sql_lower, params)
        if sql_lower.startswith("update users"):
            return self._update_user(sql_lower, params)
        if sql_lower.startswith("insert into conversations"):
            return self._insert_conversation(params)
        if ("update conversations set deleted_at = now()" in sql_lower
                and "archived_via_workstream = true" in sql_lower
                and "workstream_id = %s" in sql_lower):
            return self._cascade_archive_conversations(params)
        if ("update conversations set deleted_at = null" in sql_lower
                and "archived_via_workstream = false" in sql_lower
                and "workstream_id = %s" in sql_lower):
            return self._cascade_restore_conversations(params)
        if "conversations" in sql_lower and "set deleted_at" in sql_lower:
            return self._delete_conversation(params)
        if "conversations" in sql_lower and "set title" in sql_lower:
            return self._update_conversation_title(sql_lower, params)
        if sql_lower.startswith("update conversations set"):
            return self._update_conversation(sql_lower, params)
        if sql_lower.startswith("insert into workstreams"):
            return self._insert_workstream(params)
        if sql_lower.startswith("update workstreams"):
            return self._update_workstream(sql_lower, params)
        if sql_lower.startswith("insert into tasks"):
            return self._insert_task(params)
        if sql_lower.startswith("update tasks"):
            return self._update_task(params)
        if sql_lower.startswith("delete from tasks"):
            return self._delete_from("tasks", sql_lower, params)
        if sql_lower.startswith("insert into memories"):
            return self._insert_memory(params)
        if sql_lower.startswith("update memories"):
            return self._update_memory(sql_lower, params)
        if sql_lower.startswith("delete from memories"):
            return self._delete_from("memories", sql_lower, params)
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
        if sql_lower.startswith("insert into client_prompt_guidance"):
            return self._insert_client_prompt_guidance(params)
        if sql_lower.startswith("insert into reports"):
            return self._insert_report(params)
        if sql_lower.startswith("update reports"):
            return self._update_report(sql_lower, params)
        if sql_lower.startswith("delete from workstreams"):
            return self._delete_from("workstreams", sql_lower, params)
        if sql_lower.startswith("delete from conversations"):
            return self._delete_from("conversations", sql_lower, params)
        if sql_lower.startswith("insert into conversation_audit"):
            return self._insert_conversation_audit(params)
        if sql_lower.startswith("insert into files"):
            return self._insert_file(params)
        if sql_lower.startswith("update files"):
            return self._update_file(sql_lower, params)
        if sql_lower.startswith("delete from files"):
            return self._delete_files(sql_lower, params)
        return None

    def _insert_conversation_audit(self, params) -> dict:
        self._audit_seq += 1
        thread_id, user_id, action, message_ids, new_message, original_hash = params
        row = {
            "id": self._audit_seq,
            "thread_id": thread_id,
            "user_id": user_id,
            "action": action,
            "message_ids": list(message_ids) if message_ids else [],
            "new_message": new_message,
            "original_hash": original_hash,
            "created_at": datetime.now(timezone.utc),
        }
        self.conversation_audit.append(row)
        return {"id": self._audit_seq}

    # ------------------------------------------------------------------
    # Users (profile + permissions)
    # ------------------------------------------------------------------

    def _query_users(self, sql_lower: str, params) -> list[dict]:
        if "where id = %s" in sql_lower:
            user_id = params[0]
            return [r for r in self.users if r["id"] == user_id]
        # list_users paths — optional is_disabled filter and optional
        # (id ILIKE %s OR identity->>'name' ILIKE %s) search.
        results = list(self.users)
        if "is_disabled = false" in sql_lower:
            results = [r for r in results if not r.get("is_disabled", False)]
        if "ilike" in sql_lower and len(params) >= 2:
            raw = params[0]
            if isinstance(raw, str):
                needle = raw.strip("%").lower()
                def _match(u: dict) -> bool:
                    if needle in u["id"].lower():
                        return True
                    name = ""
                    ident = u.get("identity") or {}
                    if isinstance(ident, dict):
                        name = str(ident.get("name", "") or "")
                    return needle in name.lower()
                results = [r for r in results if _match(r)]
        results.sort(key=lambda r: r["id"])
        return results

    def _query_user_profile_ext(self, sql_lower: str, params) -> list[dict]:
        # Commercial profile columns, projected from the single users store.
        # `WHERE user_id = %s`; no matching row → [] (caller falls to defaults).
        if "where user_id = %s" in sql_lower and params:
            return [r for r in self.users if r["id"] == params[0]]
        return []

    def _upsert_user_profile_ext(self, sql_lower: str, params) -> dict | None:
        # INSERT ... ON CONFLICT (user_id) DO UPDATE — write the Précis platform
        # profile columns onto the matching users row (single-store model).
        user_id = params[0]
        target = next((u for u in self.users if u["id"] == user_id), None)
        if target is None:
            # The FK requires the users row to exist; create a minimal one so
            # the write is observable (mirrors a backfilled / lazy ext row).
            target = self._default_user(user_id, "ENT-001")
            self.users.append(target)
        if "onboarded_at = now()" in sql_lower:
            target["onboarded_at"] = datetime.now(timezone.utc)
        elif "onboarded_at = null" in sql_lower:
            target["onboarded_at"] = None
        elif len(params) == 5:
            # _save_profile: (user_id, identity, preferences,
            #   skill_preferences, report_context)
            _, identity, prefs, skill_prefs, report_ctx = params
            target["identity"] = _parse_jsonb(identity)
            target["preferences"] = prefs
            target["skill_preferences"] = _parse_jsonb(skill_prefs)
            target["report_context"] = _parse_jsonb(report_ctx)
        elif len(params) == 2:
            # create_user / patch_user identity: (user_id, identity)
            target["identity"] = _parse_jsonb(params[1])
        target["updated_at"] = datetime.now(timezone.utc)
        return {"user_id": user_id}

    def _insert_user(self, sql_lower: str, params) -> dict | None:
        # create_user (admin_ops): (id, is_admin) or, with a mode-C external
        # identity, (id, is_admin, external_id) — the open grain only; the
        # profile (identity) goes to user_profile_ext.
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
        # UPDATE users SET identity = %s, preferences = %s, skill_preferences = %s,
        #   report_context = %s, updated_at = now() WHERE id = %s
        # (reset_user.py also sets onboarded_at = NULL inline — handled below.)
        if ("identity" in sql_lower and "preferences" in sql_lower
                and "skill_preferences" in sql_lower):
            identity, prefs, skill_prefs, report_ctx, user_id = params
            for u in self.users:
                if u["id"] == user_id:
                    u["identity"] = _parse_jsonb(identity)
                    u["preferences"] = prefs
                    u["skill_preferences"] = _parse_jsonb(skill_prefs)
                    u["report_context"] = _parse_jsonb(report_ctx)
                    if "onboarded_at = null" in sql_lower:
                        u["onboarded_at"] = None
                    u["updated_at"] = datetime.now(timezone.utc)
                    return {"id": user_id}
            return None

        # complete_onboarding: UPDATE users SET onboarded_at = now(), ... WHERE id = %s
        if "onboarded_at = now()" in sql_lower and len(params) == 1:
            user_id = params[0]
            for u in self.users:
                if u["id"] == user_id:
                    u["onboarded_at"] = datetime.now(timezone.utc)
                    u["updated_at"] = datetime.now(timezone.utc)
                    return {"id": user_id}
            return None

        # reset_onboarding (admin): UPDATE users SET onboarded_at = NULL, ... WHERE id = %s
        if "onboarded_at = null" in sql_lower and len(params) == 1:
            user_id = params[0]
            for u in self.users:
                if u["id"] == user_id:
                    u["onboarded_at"] = None
                    u["updated_at"] = datetime.now(timezone.utc)
                    return {"id": user_id}
            return None

        # Generic admin PATCH: UPDATE users SET <fields> WHERE id = %s
        # user_id is always the last param; earlier params are field values in order.
        user_id = params[-1]
        field_params = list(params[:-1])
        param_idx = 0
        target = next((u for u in self.users if u["id"] == user_id), None)
        if target is None:
            return None

        _JSONB_FIELDS = {"scope", "write_scope", "scenario_access", "identity",
                         "skill_preferences", "report_context"}

        # Handle static literal: role = 'disabled'
        import re as _re
        static_role = _re.search(r"role\s*=\s*'(\w+)'", sql_lower)
        if static_role:
            target["role"] = static_role.group(1)

        # Handle static boolean: is_disabled = true / is_admin = true
        if _re.search(r"is_disabled\s*=\s*true", sql_lower):
            target["is_disabled"] = True
        if _re.search(r"is_disabled\s*=\s*false", sql_lower):
            target["is_disabled"] = False
        if _re.search(r"is_admin\s*=\s*true", sql_lower):
            target["is_admin"] = True
        if _re.search(r"is_admin\s*=\s*false", sql_lower):
            target["is_admin"] = False

        # Handle parameterised fields (order must match SQL column order).
        for field in ("is_admin", "is_disabled", "role", "scope", "write_scope",
                      "scenario_access", "identity", "preferences",
                      "skill_preferences", "report_context"):
            if f"{field} = %s" in sql_lower and param_idx < len(field_params):
                val = field_params[param_idx]
                target[field] = _parse_jsonb(val) if field in _JSONB_FIELDS else val
                param_idx += 1

        target["updated_at"] = datetime.now(timezone.utc)
        return {"id": user_id}

    def _default_user(self, user_id: str, entity_id: str = "ENT-001") -> dict:
        return {
            "id": user_id,
            "entity_id": entity_id,
            "role": "analyst",
            "scope": {},
            "write_scope": {},
            "scenario_access": {},
            "identity": {},
            "preferences": "",
            "skill_preferences": {},
            "report_context": {},
            "onboarded_at": None,
            "is_admin": False,
            "is_disabled": False,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }

    # ------------------------------------------------------------------
    # Conversations
    # ------------------------------------------------------------------

    def _query_conversations(self, sql_lower: str, params) -> list[dict]:
        if "select 1 from conversations" in sql_lower:
            conv_id, user_id = params
            return [r for r in self.conversations
                    if r["id"] == conv_id and r["user_id"] == user_id and r["deleted_at"] is None]
        if "where id = %s" in sql_lower:
            conv_id, user_id = params[0], params[1]
            return [r for r in self.conversations
                    if r["id"] == conv_id and r["user_id"] == user_id and r["deleted_at"] is None]
        if "where user_id" in sql_lower:
            user_id = params[0]
            results = [r for r in self.conversations
                       if r["user_id"] == user_id and r["deleted_at"] is None]
            results.sort(key=lambda x: x["updated_at"] or "", reverse=True)
            return results
        return []

    def _insert_conversation(self, params) -> dict | None:
        # Shapes supported:
        #   4 params: (id, user_id, created_at, updated_at)  — legacy
        #   5 params: (id, user_id, workstream_id, created_at, updated_at)
        #   6 params: (id, user_id, workstream_id, kind, created_at, updated_at)
        kind = "persistent"
        if len(params) == 6:
            conv_id, user_id, workstream_id, kind, created_at, updated_at = params
        elif len(params) == 5:
            conv_id, user_id, workstream_id, created_at, updated_at = params
        else:
            conv_id, user_id, created_at, updated_at = params
            workstream_id = None
        self.conversations.append({
            "id": conv_id, "user_id": user_id, "entity_id": "ENT-001",
            "title": "New Conversation", "workstream_id": workstream_id,
            "kind": kind,
            "archived_via_workstream": False,
            "created_at": created_at, "updated_at": updated_at, "deleted_at": None,
        })
        return {"id": conv_id}

    def _delete_conversation(self, params) -> dict | None:
        conv_id, user_id = params[0], params[1]
        for r in self.conversations:
            if r["id"] == conv_id and r["user_id"] == user_id and r["deleted_at"] is None:
                r["deleted_at"] = datetime.now(timezone.utc)
                r["updated_at"] = datetime.now(timezone.utc)
                return {"id": conv_id}
        return None

    def _update_conversation_title(self, sql_lower: str, params) -> dict | None:
        title, conv_id, user_id = params[0], params[1], params[2]
        # Guarded variant (set_default_conversation_title): a trailing
        # `AND title = %s` param restricts the write to rows still holding it.
        required_title = params[3] if "and title = %s" in sql_lower else None
        for r in self.conversations:
            if r["id"] == conv_id and r["user_id"] == user_id and r["deleted_at"] is None:
                if required_title is not None and r["title"] != required_title:
                    return None
                r["title"] = title
                r["updated_at"] = datetime.now(timezone.utc)
                return {"id": conv_id}
        return None

    def _update_conversation(self, sql_lower: str, params) -> dict | None:
        conv_id = params[-2]
        user_id = params[-1]
        for r in self.conversations:
            if r["id"] == conv_id and r["user_id"] == user_id and r["deleted_at"] is None:
                idx = 0
                if "title = %s" in sql_lower:
                    r["title"] = params[idx]
                    idx += 1
                if "workstream_id = %s" in sql_lower:
                    r["workstream_id"] = params[idx]
                    idx += 1
                r["updated_at"] = datetime.now(timezone.utc)
                return {"id": conv_id}
        return None

    def _cascade_archive_conversations(self, params) -> dict:
        """Soft-delete every active conversation in a workstream.

        Supports both the bare UPDATE and the CTE-wrapped variant used by
        DELETE /api/workstreams/{id}, which returns ``{"n": count}``.
        """
        workstream_id, user_id = params[0], params[1]
        now = datetime.now(timezone.utc)
        n = 0
        for r in self.conversations:
            if (r["user_id"] == user_id
                    and r.get("workstream_id") == workstream_id
                    and r.get("deleted_at") is None):
                r["deleted_at"] = now
                r["archived_via_workstream"] = True
                r["updated_at"] = now
                n += 1
        return {"n": n}

    def _cascade_restore_conversations(self, params) -> dict:
        """Undo a cascade archive, restoring only those rows the cascade touched."""
        workstream_id, user_id = params[0], params[1]
        now = datetime.now(timezone.utc)
        n = 0
        for r in self.conversations:
            if (r["user_id"] == user_id
                    and r.get("workstream_id") == workstream_id
                    and r.get("archived_via_workstream") is True):
                r["deleted_at"] = None
                r["archived_via_workstream"] = False
                r["updated_at"] = now
                n += 1
        return {"n": n}

    # ------------------------------------------------------------------
    # Workstreams
    # ------------------------------------------------------------------

    def _query_workstreams(self, sql_lower: str, params) -> list[dict]:
        if "where id = %s and user_id = %s" in sql_lower:
            ws_id, user_id = params[0], params[1]
            rows = [r for r in self.workstreams
                    if r["id"] == ws_id and r["user_id"] == user_id]
            if "status = 'active'" in sql_lower:
                rows = [r for r in rows if r.get("status") == "active"]
            return rows
        # GET list endpoint uses WHERE user_id = %s AND status = 'active'
        if "where user_id = %s and status = 'active'" in sql_lower:
            user_id = params[0]
            rows = [r for r in self.workstreams
                    if r["user_id"] == user_id and r.get("status") == "active"]
            rows.sort(
                key=lambda x: x.get("updated_at") or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            return rows
        if "and status = %s" in sql_lower:
            user_id, status = params[0], params[1]
            rows = [r for r in self.workstreams
                    if r["user_id"] == user_id and r["status"] == status]
            rows.sort(key=lambda x: x.get("updated_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            return rows
        if "where user_id = %s" in sql_lower:
            user_id = params[0]
            rows = [r for r in self.workstreams if r["user_id"] == user_id]
            rows.sort(key=lambda x: x.get("updated_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            return rows
        return []

    def _insert_workstream(self, params) -> dict | None:
        ws_id, user_id, name, description, created_at, updated_at = params
        self.workstreams.append({
            "id": ws_id, "user_id": user_id, "name": name,
            "description": description, "status": "active",
            "created_at": created_at,
            "updated_at": updated_at, "archived_at": None,
        })
        return {"id": ws_id}

    def _update_workstream(self, sql_lower: str, params) -> dict | None:
        # DELETE endpoint: SET status = 'archived', archived_at = %s, updated_at = %s
        # WHERE id = %s AND user_id = %s
        if "status = 'archived'" in sql_lower and "name" not in sql_lower:
            archived_at, updated_at, ws_id, user_id = params
            for r in self.workstreams:
                if r["id"] == ws_id and r["user_id"] == user_id:
                    r["status"] = "archived"
                    r["archived_at"] = archived_at
                    r["updated_at"] = updated_at
                    return {"id": ws_id}
            return None
        if "name" in sql_lower:
            # UPDATE workstreams SET name = %s, description = %s, status = %s,
            #   archived_at = %s, updated_at = %s WHERE id = %s AND user_id = %s
            name, desc, status, archived_at, updated_at, ws_id, user_id = params
            for r in self.workstreams:
                if r["id"] == ws_id and r["user_id"] == user_id:
                    r["name"] = name
                    r["description"] = desc
                    r["status"] = status
                    r["archived_at"] = archived_at
                    r["updated_at"] = updated_at
                    return {"id": ws_id}
        return None

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    def _query_tasks(self, sql_lower: str, sql: str, params) -> list[dict]:
        # Soft-delete visibility check (REST paths filter with deleted_at IS NULL).
        filter_deleted = "deleted_at is null" in sql_lower

        # Single row by task_id, with optional visibility guard:
        #   WHERE task_id = %s [AND deleted_at IS NULL]
        #   [AND (created_by = %s OR assigned_to = %s)]
        if "where task_id = %s" in sql_lower:
            param_idx = 0
            task_id = params[param_idx]; param_idx += 1
            rows = [r for r in self.tasks if r["task_id"] == task_id]
            if filter_deleted:
                rows = [r for r in rows if r.get("deleted_at") is None]
            if "(created_by = %s or assigned_to = %s)" in sql_lower:
                uid1, uid2 = params[param_idx], params[param_idx + 1]
                rows = [r for r in rows
                        if r.get("created_by") == uid1 or r.get("assigned_to") == uid2]
                param_idx += 2
            # Column-projection helper: if SELECT mentions only `created_by`,
            # the caller's existing code already accepts the full row; return
            # as-is to keep compatibility.
            return rows

        if "count(*)" in sql_lower:
            results = list(self.tasks)
            if filter_deleted:
                results = [r for r in results if r.get("deleted_at") is None]
            return [{"cnt": len(results)}]

        # List path — REST uses deleted_at IS NULL plus a mix of filters.
        results = list(self.tasks)
        if filter_deleted:
            results = [r for r in results if r.get("deleted_at") is None]

        param_idx = 0
        # The OR clause must be detected first because its substring
        # ``assigned_to = %s`` would otherwise match the standalone filter
        # below and steal the OR's first parameter.
        has_or_clause = "(created_by = %s or assigned_to = %s)" in sql_lower
        if has_or_clause:
            uid1, uid2 = params[param_idx], params[param_idx + 1]
            results = [r for r in results
                       if r.get("created_by") == uid1 or r.get("assigned_to") == uid2]
            param_idx += 2
        # Standalone ``assigned_to = %s`` filter — only applies when it
        # appears outside the OR clause we just consumed.
        standalone_assigned_to = (
            "assigned_to = %s" in sql_lower
            and (not has_or_clause
                 or sql_lower.count("assigned_to = %s") > 1)
        )
        if standalone_assigned_to:
            results = [r for r in results if r["assigned_to"] == params[param_idx]]
            param_idx += 1
        # status IN (%s, %s, ...) — count placeholders after "status in ("
        import re as _re
        in_match = _re.search(r"status in \(([^)]+)\)", sql_lower)
        if in_match:
            placeholders = in_match.group(1).count("%s")
            wanted = set(params[param_idx:param_idx + placeholders])
            results = [r for r in results if r["status"] in wanted]
            param_idx += placeholders
        elif "status = %s" in sql_lower:
            results = [r for r in results if r["status"] == params[param_idx]]
            param_idx += 1
        if "scenario_id = %s" in sql_lower:
            results = [r for r in results if r["scenario_id"] == params[param_idx]]
            param_idx += 1
        if "workstream_id = %s" in sql_lower:
            results = [r for r in results if r["workstream_id"] == params[param_idx]]
            param_idx += 1
        if "due_date <> '' and due_date < %s" in sql_lower:
            cutoff = params[param_idx]
            results = [r for r in results
                       if r.get("due_date") and r["due_date"] < cutoff]
            param_idx += 1
        if "due_date <> '' and due_date > %s" in sql_lower:
            cutoff = params[param_idx]
            results = [r for r in results
                       if r.get("due_date") and r["due_date"] > cutoff]
            param_idx += 1
        # Sort: due_date tasks first, then no-due-date by created_at
        with_due = [t for t in results if t.get("due_date")]
        without_due = [t for t in results if not t.get("due_date")]
        with_due.sort(key=lambda x: (x["due_date"], x.get("created_at") or ""))
        without_due.sort(key=lambda x: x.get("created_at") or "")
        return with_due + without_due

    def _insert_task(self, params) -> dict | None:
        (task_id, title, description, status, created_by, assigned_to,
         due_date, scenario_id, workstream_id, created_at, updated_at) = params
        self.tasks.append({
            "task_id": task_id, "title": title, "description": description,
            "status": status, "created_by": created_by, "assigned_to": assigned_to,
            "due_date": due_date, "scenario_id": scenario_id,
            "workstream_id": workstream_id,
            "created_at": created_at, "updated_at": updated_at,
            "deleted_at": None,
        })
        return {"task_id": task_id}

    def _update_task(self, params) -> dict | None:
        # Soft-delete: UPDATE tasks SET deleted_at = now(), updated_at = now()
        #   WHERE task_id = %s
        if len(params) == 1:
            task_id = params[0]
            now = datetime.now(timezone.utc)
            for t in self.tasks:
                if t["task_id"] == task_id and t.get("deleted_at") is None:
                    t["deleted_at"] = now
                    t["updated_at"] = now
                    return {"task_id": task_id}
            return None

        # Full update:
        #   UPDATE tasks SET title=%s, description=%s, status=%s, assigned_to=%s,
        #     due_date=%s, scenario_id=%s, workstream_id=%s, updated_at=%s
        #     WHERE task_id=%s [AND deleted_at IS NULL]
        (title, description, status, assigned_to, due_date,
         scenario_id, workstream_id, updated_at, task_id) = params
        for t in self.tasks:
            if t["task_id"] == task_id and t.get("deleted_at") is None:
                t["title"] = title
                t["description"] = description
                t["status"] = status
                t["assigned_to"] = assigned_to
                t["due_date"] = due_date
                t["scenario_id"] = scenario_id
                t["workstream_id"] = workstream_id
                t["updated_at"] = updated_at
                return {"task_id": task_id}
        return None

    # ------------------------------------------------------------------
    # Memories
    # ------------------------------------------------------------------

    def _query_memories(self, sql_lower: str, sql: str, params) -> list[dict]:
        if "where id = %s and user_id = %s" in sql_lower:
            mem_id, user_id = params[0], params[1]
            return [r for r in self.memories
                    if r["id"] == mem_id and r["user_id"] == user_id]
        # list_memories with filters
        results = list(self.memories)
        param_idx = 0
        if "user_id = %s" in sql_lower:
            results = [r for r in results if r["user_id"] == params[param_idx]]
            param_idx += 1
        if "workstream_id is null" in sql_lower:
            results = [r for r in results if r["workstream_id"] is None]
        elif "workstream_id = %s" in sql_lower:
            results = [r for r in results if r["workstream_id"] == params[param_idx]]
            param_idx += 1
        if "type = %s" in sql_lower:
            results = [r for r in results if r["type"] == params[param_idx]]
            param_idx += 1
        return results

    def _insert_memory(self, params) -> dict | None:
        (mem_id, user_id, ws_id, type_, content, description,
         confidence, tags_json, conversation_id,
         created_at, updated_at, accessed_at) = params
        self.memories.append({
            "id": mem_id, "user_id": user_id, "workstream_id": ws_id,
            "type": type_, "content": content, "description": description,
            "confidence": confidence, "tags": _parse_jsonb(tags_json),
            "superseded_by": None, "archived_at": None,
            "conversation_id": conversation_id,
            "created_at": created_at, "updated_at": updated_at,
            "accessed_at": accessed_at,
        })
        return {"id": mem_id}

    def _update_memory(self, sql_lower: str, params) -> dict | None:
        if "accessed_at" in sql_lower:
            # This form scopes the update by user_id as well as id.
            if "and user_id = %s" in sql_lower:
                accessed, mem_id, user_id = params
                for m in self.memories:
                    if m["id"] == mem_id and m["user_id"] == user_id:
                        m["accessed_at"] = accessed
                        return {"id": mem_id}
                return None
            # Legacy 2-param form (no user scoping).
            accessed, mem_id = params
            for m in self.memories:
                if m["id"] == mem_id:
                    m["accessed_at"] = accessed
                    return {"id": mem_id}
        return None

    # ------------------------------------------------------------------
    # Profiles / user_profile_assignments (profile-based security model)
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Validation rules (versioned: PK = scenario_id + updated_at)
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Client prompt guidance (versioned: PK = slot + updated_at; global,
    # no tenant scoping)
    # ------------------------------------------------------------------

    def _query_client_prompt_guidance(self, sql_lower: str, params) -> list[dict]:
        # list_slots_with_status: SELECT DISTINCT ON (slot) slot, updated_at, created_by
        #   FROM client_prompt_guidance ORDER BY slot, updated_at DESC
        if "distinct on (slot)" in sql_lower:
            by_slot: dict[str, dict] = {}
            matching = sorted(
                self.client_prompt_guidance,
                key=lambda r: (r["slot"], r["updated_at"]),
            )
            for r in matching:
                # Keep the newest per slot — iterate sorted ascending then overwrite
                by_slot[r["slot"]] = {
                    "slot": r["slot"],
                    "updated_at": r["updated_at"],
                    "created_by": r["created_by"],
                }
            return list(by_slot.values())

        # Specific-version lookup: WHERE slot = %s AND updated_at = %s
        if "and updated_at = %s" in sql_lower:
            slot, updated_at = params[0], params[1]
            return [
                dict(r) for r in self.client_prompt_guidance
                if r["slot"] == slot
                and str(r["updated_at"]) == str(updated_at)
            ]

        # Latest / history lookup (keyed on slot)
        slot = params[0]
        matching = [
            dict(r) for r in self.client_prompt_guidance
            if r["slot"] == slot
        ]
        matching.sort(key=lambda r: r["updated_at"], reverse=True)
        if "limit 1" in sql_lower:
            return matching[:1]
        return matching

    def _insert_client_prompt_guidance(self, params) -> dict | None:
        slot, body, description, created_by = params
        now = datetime.now(timezone.utc)
        # Ensure strict monotonicity even within the same microsecond so
        # ORDER BY updated_at DESC LIMIT 1 returns the most recent insert.
        existing = [
            r for r in self.client_prompt_guidance
            if r["slot"] == slot
        ]
        if existing:
            latest = max(r["updated_at"] for r in existing)
            if now <= latest:
                now = latest + timedelta(microseconds=1)
        self.client_prompt_guidance.append({
            "slot": slot,
            "body": body,
            "description": description,
            "created_by": created_by,
            "updated_at": now,
        })
        return {
            "slot": slot,
            "updated_at": now,
            "created_by": created_by,
        }

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------

    def _query_reports(self, sql_lower: str, params) -> list[dict]:
        # Single-report fetch: WHERE id = %s AND user_id = %s AND deleted_at IS NULL
        if "where id = %s and user_id = %s" in sql_lower:
            report_id, user_id = params[0], params[1]
            return [
                dict(r) for r in self.reports
                if r["id"] == report_id
                and r["user_id"] == user_id
                and r.get("deleted_at") is None
            ]
        # List with optional workstream filter
        if "workstream_id = %s" in sql_lower:
            user_id, workstream_id = params[0], params[1]
            results = [
                dict(r) for r in self.reports
                if r["user_id"] == user_id
                and r.get("workstream_id") == workstream_id
                and r.get("deleted_at") is None
            ]
        elif "where user_id = %s" in sql_lower:
            user_id = params[0]
            results = [
                dict(r) for r in self.reports
                if r["user_id"] == user_id
                and r.get("deleted_at") is None
            ]
        else:
            return []
        results.sort(
            key=lambda x: x.get("updated_at") or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return results

    def _insert_report(self, params) -> dict | None:
        # INSERT INTO reports (id, user_id, workstream_id, title, blocks,
        #   status, created_at, updated_at) VALUES (...)
        (report_id, user_id, workstream_id, title, blocks_json,
         status, created_at, updated_at) = params
        self.reports.append({
            "id": report_id,
            "user_id": user_id,
            "entity_id": "ENT-001",
            "workstream_id": workstream_id,
            "title": title,
            "blocks": _parse_jsonb(blocks_json),
            "status": status,
            "created_at": created_at,
            "updated_at": updated_at,
            "deleted_at": None,
        })
        return {"id": report_id}

    def _update_report(self, sql_lower: str, params) -> dict | None:
        # Two shapes:
        # a) Full update (update_report tool):
        #    UPDATE reports SET title = %s, blocks = %s, status = %s,
        #      updated_at = %s WHERE id = %s AND user_id = %s
        # b) Blocks-only update (report_sink.write_result_to_report):
        #    UPDATE reports SET blocks = %s, updated_at = %s
        #      WHERE id = %s AND user_id = %s
        if "set title" in sql_lower:
            title, blocks_json, status, updated_at, report_id, user_id = params
            for r in self.reports:
                if (r["id"] == report_id
                        and r["user_id"] == user_id
                        and r.get("deleted_at") is None):
                    r["title"] = title
                    r["blocks"] = _parse_jsonb(blocks_json)
                    r["status"] = status
                    r["updated_at"] = updated_at
                    return {"id": report_id}
            return None

        # Blocks-only shape
        blocks_json, updated_at, report_id, user_id = params
        for r in self.reports:
            if (r["id"] == report_id
                    and r["user_id"] == user_id
                    and r.get("deleted_at") is None):
                r["blocks"] = _parse_jsonb(blocks_json)
                r["updated_at"] = updated_at
                return {"id": report_id}
        return None

    # ------------------------------------------------------------------
    # Files registry
    # ------------------------------------------------------------------

    def _query_files(self, sql_lower: str, params) -> list[dict]:
        """Routes for the queries issued by precis/files/registry.py."""
        # quota_used: SELECT COALESCE(SUM(size_bytes), 0) AS used FROM files
        #             WHERE user_id = %s AND deleted_at IS NULL
        if "coalesce(sum(size_bytes)" in sql_lower:
            user_id = params[0]
            used = sum(
                int(r["size_bytes"]) for r in self.files
                if r["user_id"] == user_id and r.get("deleted_at") is None
            )
            return [{"used": used}]

        # gc_expired pass 1: SELECT file_id FROM files WHERE expires_at IS NOT NULL
        #                    AND expires_at < now() AND deleted_at IS NULL
        #                    AND workstream_id IS NULL
        if (
            "expires_at is not null" in sql_lower
            and "expires_at < now()" in sql_lower
            and "deleted_at is null" in sql_lower
            and "workstream_id is null" in sql_lower
        ):
            now = datetime.now(timezone.utc)
            return [
                {"file_id": r["file_id"]}
                for r in self.files
                if r.get("expires_at") is not None
                and r["expires_at"] < now
                and r.get("deleted_at") is None
                and r.get("workstream_id") is None
            ]

        # gc_expired pass 2: SELECT file_id, user_id, storage_path FROM files
        #                    WHERE deleted_at IS NOT NULL
        if (
            "select file_id, user_id, storage_path" in sql_lower
            and "deleted_at is not null" in sql_lower
        ):
            return [
                {
                    "file_id": r["file_id"],
                    "user_id": r["user_id"],
                    "storage_path": r["storage_path"],
                }
                for r in self.files
                if r.get("deleted_at") is not None
            ]

        # get_file / update_file_blob row lock:
        # WHERE file_id = %s AND user_id = %s AND deleted_at IS NULL
        # (the trailing FOR UPDATE is a no-op against the in-memory store)
        if "where file_id = %s and user_id = %s and deleted_at is null" in sql_lower:
            file_id, user_id = params[0], params[1]
            return [
                dict(r) for r in self.files
                if r["file_id"] == file_id
                and r["user_id"] == user_id
                and r.get("deleted_at") is None
            ]

        # list_files: WHERE user_id = %s AND deleted_at IS NULL [+ filters]
        if "where user_id = %s and deleted_at is null" in sql_lower:
            param_idx = 0
            user_id = params[param_idx]; param_idx += 1
            results = [
                dict(r) for r in self.files
                if r["user_id"] == user_id and r.get("deleted_at") is None
            ]
            if "and conversation_id = %s" in sql_lower:
                conv_id = params[param_idx]; param_idx += 1
                results = [r for r in results if r.get("conversation_id") == conv_id]
            if "and workstream_id = %s" in sql_lower:
                ws_id = params[param_idx]; param_idx += 1
                results = [r for r in results if r.get("workstream_id") == ws_id]
            if "and (expires_at is null or expires_at > now())" in sql_lower:
                now = datetime.now(timezone.utc)
                results = [
                    r for r in results
                    if r.get("expires_at") is None or r["expires_at"] > now
                ]
            results.sort(key=lambda r: r.get("created_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            return results

        return []

    def _insert_file(self, params) -> dict | None:
        # INSERT INTO files (file_id, user_id, basename, mime_type, size_bytes,
        #   sha256, storage_path, expires_at, conversation_id, workstream_id,
        #   source, producing_tool) VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
        #   %s, %s, %s, %s) RETURNING ...
        (file_id, user_id, basename, mime_type, size_bytes, sha256,
         storage_path, expires_at, conversation_id, workstream_id,
         source, producing_tool) = params
        now = datetime.now(timezone.utc)
        row = {
            "file_id": file_id,
            "user_id": user_id,
            "basename": basename,
            "mime_type": mime_type,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "storage_path": storage_path,
            "created_at": now,
            "expires_at": expires_at,
            "deleted_at": None,
            "conversation_id": conversation_id,
            "workstream_id": workstream_id,
            "source": source,
            "producing_tool": producing_tool,
        }
        self.files.append(row)
        return dict(row)

    def _update_file(self, sql_lower: str, params) -> dict | None:
        # update_file_blob: UPDATE files SET size_bytes = %s, sha256 = %s,
        #   mime_type = %s, producing_tool = %s, updated_at = now()
        #   WHERE file_id = %s AND user_id = %s RETURNING ...
        if "set size_bytes = %s" in sql_lower and "sha256 = %s" in sql_lower:
            (size_bytes, sha256, mime_type, producing_tool,
             file_id, user_id) = params
            for r in self.files:
                if (r["file_id"] == file_id
                        and r["user_id"] == user_id
                        and r.get("deleted_at") is None):
                    r["size_bytes"] = size_bytes
                    r["sha256"] = sha256
                    r["mime_type"] = mime_type
                    r["producing_tool"] = producing_tool
                    r["updated_at"] = datetime.now(timezone.utc)
                    return dict(r)
            return None

        # soft_delete: UPDATE files SET deleted_at = now() WHERE file_id = %s
        #              AND user_id = %s AND deleted_at IS NULL RETURNING file_id
        if (
            "set deleted_at = now()" in sql_lower
            and "where file_id = %s and user_id = %s" in sql_lower
        ):
            file_id, user_id = params[0], params[1]
            for r in self.files:
                if (r["file_id"] == file_id
                        and r["user_id"] == user_id
                        and r.get("deleted_at") is None):
                    r["deleted_at"] = datetime.now(timezone.utc)
                    return {"file_id": file_id}
            return None

        # gc_expired update: UPDATE files SET deleted_at = now() WHERE file_id = %s
        #                    AND deleted_at IS NULL RETURNING file_id
        if (
            "set deleted_at = now()" in sql_lower
            and "where file_id = %s and deleted_at is null" in sql_lower
        ):
            file_id = params[0]
            for r in self.files:
                if r["file_id"] == file_id and r.get("deleted_at") is None:
                    r["deleted_at"] = datetime.now(timezone.utc)
                    return {"file_id": file_id}
            return None

        # set_workstream pin: UPDATE files SET workstream_id = %s, expires_at = NULL ...
        if "set workstream_id = %s, expires_at = null" in sql_lower:
            ws_id, file_id, user_id = params[0], params[1], params[2]
            for r in self.files:
                if (r["file_id"] == file_id
                        and r["user_id"] == user_id
                        and r.get("deleted_at") is None):
                    r["workstream_id"] = ws_id
                    r["expires_at"] = None
                    return dict(r)
            return None

        # set_workstream unpin (+ restore expiry):
        # UPDATE files SET workstream_id = NULL, expires_at = now() + INTERVAL '24 hours' ...
        if "set workstream_id = null" in sql_lower and "interval '24 hours'" in sql_lower:
            file_id, user_id = params[0], params[1]
            for r in self.files:
                if (r["file_id"] == file_id
                        and r["user_id"] == user_id
                        and r.get("deleted_at") is None):
                    r["workstream_id"] = None
                    r["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=24)
                    return dict(r)
            return None

        # set_workstream unpin (no expiry change):
        # UPDATE files SET workstream_id = NULL WHERE file_id = %s AND user_id = %s ...
        if "set workstream_id = null" in sql_lower:
            file_id, user_id = params[0], params[1]
            for r in self.files:
                if (r["file_id"] == file_id
                        and r["user_id"] == user_id
                        and r.get("deleted_at") is None):
                    r["workstream_id"] = None
                    return dict(r)
            return None

        return None

    def _delete_files(self, sql_lower: str, params) -> dict | None:
        # gc_expired hard-delete: DELETE FROM files WHERE deleted_at IS NOT NULL
        if "where deleted_at is not null" in sql_lower:
            self.files = [r for r in self.files if r.get("deleted_at") is None]
            return None
        return None

    # ------------------------------------------------------------------
    # Generic delete
    # ------------------------------------------------------------------

    def _delete_from(self, table: str, sql_lower: str, params) -> dict | None:
        store = getattr(self, table)
        if "user_id = %s" in sql_lower:
            user_id = params[0]
            setattr(self, table, [r for r in store if r.get("user_id") != user_id])
        elif "created_by = %s" in sql_lower:
            created_by = params[0]
            setattr(self, table, [r for r in store if r.get("created_by") != created_by])
        return None


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


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
    used by ``precis.files.registry.update_file_blob``. SELECT
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
        return _FakeCursor(self._db)

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            failure = getattr(self._db, "_commit_should_fail", None)
            if failure is not None:
                self._db._commit_should_fail = None
                raise failure
        return None

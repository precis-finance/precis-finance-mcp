# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""User-owned, bounded analytical snapshots shared by all tool consumers."""
from __future__ import annotations

from dataclasses import asdict
import json
import logging
import os
import re
import secrets
import time

from precis_mcp.auth import UserPermissions, get_auth_context
from precis_mcp import db

def get_redis_client():
    return db.get_redis_client()

logger = logging.getLogger(__name__)
_REF = re.compile(r"^ds_[a-f0-9]{32}$")
_PREFIX = "precis:datasets:v1"


def _limit(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def permission_stamp(permissions: UserPermissions) -> dict:
    """Conservative read-grant stamp: any grant change requires a fresh query.

    Includes unrestricted scopes and scenario membership. Deliberately compares
    the whole read-grant set rather than accepting stale data after a profile
    change. Never accepts provenance supplied by an uploaded workbook.
    """
    return {
        "is_admin": permissions.is_admin,
        "scenarios": {
            sid: asdict(scope) if scope is not None else None
            for sid, grant in sorted(permissions.scenarios.items())
            if "read" in grant.tool_scopes
            for scope in [grant.tool_scopes["read"]]
        },
    }


def check_access(access: dict, user_id: str, permissions: UserPermissions) -> bool:
    return (
        permissions.user_id == user_id
        and access.get("owner") == user_id
        and access.get("grants") == permission_stamp(permissions)
    )


def normalize_data_refs(value: object) -> list[str] | None:
    """Validate the canonical array contract; no string/JSON coercions."""
    if (
        not isinstance(value, list) or not value
        or len(value) > _limit("PRECIS_DATASET_MAX_INPUTS", 8)
        or any(not isinstance(ref, str) or not _REF.fullmatch(ref) for ref in value)
    ):
        return None
    return value


def _keys(user_id: str, data_ref: str) -> tuple[str, str, str]:
    # Hash the owner for safe Redis key construction; the envelope also binds it.
    from hashlib import sha256
    owner = sha256(user_id.encode()).hexdigest()
    root = f"{_PREFIX}:{{{owner}}}"
    return f"{root}:{data_ref}", f"{root}:expiry", f"{root}:sizes"


def cache_read_result(result: dict, tool_name: str, tool_args: dict) -> str | None:
    """Store an engine-precision snapshot before consumer preview/rounding.

    Redis WATCH serializes each user's capacity accounting across workers.
    A failed cache write does not fail the original finance read.
    """
    try:
        auth = get_auth_context()
        now = time.time()
        ttl = _limit("PRECIS_DATASET_IDLE_SECONDS", 600)
        max_age = _limit("PRECIS_DATASET_MAX_AGE_SECONDS", 3600)
        ttl = min(ttl, max_age)
        data_ref = "ds_" + secrets.token_hex(16)
        key, index, sizes = _keys(auth.user_id, data_ref)
        payload = {
            "version": 1, "created_at": now, "expires_at": now + max_age,
            "access": {"owner": auth.user_id, "grants": permission_stamp(auth.permissions)},
            "result": {k: v for k, v in result.items() if k != "data_ref"},
            "source": {"tool": tool_name, "args": tool_args},
        }
        encoded = json.dumps(payload, allow_nan=False, separators=(",", ":"))
        size = len(encoded.encode())
        if size > _limit("PRECIS_DATASET_MAX_BYTES", 4 * 1024 * 1024):
            logger.warning("dataset too large to cache", extra={"bytes": size})
            return None
        from redis.exceptions import WatchError
        client = get_redis_client()
        for _ in range(5):
            try:
                with client.pipeline() as pipe:
                    pipe.watch(index, sizes)
                    expired = pipe.zrangebyscore(index, "-inf", now)
                    existing = pipe.hgetall(sizes)
                    expired_set = set(expired)
                    active = {k: int(v) for k, v in existing.items() if k not in expired_set}
                    if (
                        sum(active.values()) + size > _limit("PRECIS_DATASET_USER_BYTES", 64 * 1024 * 1024)
                        or len(active) >= _limit("PRECIS_DATASET_USER_COUNT", 128)
                    ):
                        logger.warning("dataset cache capacity reached")
                        return None
                    pipe.multi()
                    if expired:
                        pipe.delete(*expired)
                        pipe.zrem(index, *expired)
                        pipe.hdel(sizes, *expired)
                    pipe.setex(key, ttl, encoded)
                    pipe.zadd(index, {key: now + ttl})
                    pipe.hset(sizes, key, size)
                    pipe.expire(index, max_age)
                    pipe.expire(sizes, max_age)
                    pipe.execute()
                    return data_ref
            except WatchError:
                continue
        logger.warning("dataset cache contention")
    except Exception:
        logger.exception("dataset cache unavailable")
    return None


def fetch_cached_read_result(user_id: str, data_ref: str) -> tuple[dict, str | None]:
    """Resolve a snapshot under current request permissions, then renew idle TTL."""
    if not isinstance(data_ref, str) or not _REF.fullmatch(data_ref):
        return {}, "Invalid data_ref format"
    try:
        auth = get_auth_context()
        if auth.user_id != user_id:
            return {}, "No access to dataset"
        key, index, sizes = _keys(user_id, data_ref)
        from redis.exceptions import WatchError
        client = get_redis_client()
        for _ in range(5):
            try:
                with client.pipeline() as pipe:
                    pipe.watch(key, index, sizes)
                    cached = pipe.get(key)
                    if not cached:
                        return {}, "data_ref expired or not found — rerun the query"
                    payload = json.loads(cached)
                    now = time.time()
                    remaining = int(payload["expires_at"] - now)
                    if payload.get("version") != 1 or remaining <= 0:
                        return {}, "data_ref expired or not found — rerun the query"
                    if not check_access(payload.get("access", {}), user_id, auth.permissions):
                        return {}, "Dataset permissions changed — rerun the query"
                    ttl = min(_limit("PRECIS_DATASET_IDLE_SECONDS", 600), remaining)
                    pipe.multi()
                    pipe.expire(key, ttl)
                    pipe.zadd(index, {key: now + ttl})
                    pipe.expire(index, _limit("PRECIS_DATASET_MAX_AGE_SECONDS", 3600))
                    pipe.expire(sizes, _limit("PRECIS_DATASET_MAX_AGE_SECONDS", 3600))
                    pipe.execute()
                    return payload, None
            except WatchError:
                continue
        return {}, "Dataset cache busy — retry shortly"
    except Exception:
        logger.exception("dataset resolution failed")
        return {}, "Dataset cache unavailable — retry shortly"

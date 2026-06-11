# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Object-store abstraction — `ObjectStoreClient` protocol + concrete
implementations.

The generic file-drop driver works against any client that implements the
protocol; concrete implementations exist for S3 (lazy boto3), SFTP (lazy
paramiko), local-filesystem (used by the HTTPS upload endpoint), and an
in-memory variant for tests.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Protocol


@dataclass(frozen=True)
class ObjectMeta:
    """Lightweight metadata about one object in a store."""

    key: str
    size: int
    modified: datetime


class ObjectStoreClient(Protocol):
    """Read-only client surface — list and fetch.

    Writers (the HTTPS upload endpoint, manual ops scripts) use kind-specific
    APIs directly; the ingestion read-side doesn't need a write interface.
    """

    kind: str

    def list_keys(
        self,
        *,
        prefix: str = "",
        glob: Optional[str] = None,
    ) -> Iterable[ObjectMeta]: ...

    def get_bytes(self, key: str) -> bytes: ...


# ---------------------------------------------------------------------------
# In-memory — for tests and the smoke pipeline
# ---------------------------------------------------------------------------


class InMemoryObjectStore:
    """Dict-backed store for unit and smoke tests."""

    kind = "memory"

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}
        self._modified: dict[str, datetime] = {}

    def put(self, key: str, data: bytes, *, modified: Optional[datetime] = None) -> None:
        self._objects[key] = data
        self._modified[key] = modified or datetime.now(timezone.utc)

    def delete(self, key: str) -> None:
        self._objects.pop(key, None)
        self._modified.pop(key, None)

    def list_keys(
        self,
        *,
        prefix: str = "",
        glob: Optional[str] = None,
    ) -> Iterable[ObjectMeta]:
        out: list[ObjectMeta] = []
        for key, body in self._objects.items():
            if prefix and not key.startswith(prefix):
                continue
            if glob and not fnmatch.fnmatch(key.rsplit("/", 1)[-1], glob):
                continue
            out.append(
                ObjectMeta(
                    key=key,
                    size=len(body),
                    modified=self._modified.get(key, datetime.now(timezone.utc)),
                )
            )
        return sorted(out, key=lambda m: m.key)

    def get_bytes(self, key: str) -> bytes:
        try:
            return self._objects[key]
        except KeyError as exc:
            raise FileNotFoundError(f"InMemoryObjectStore: no object {key!r}") from exc


# ---------------------------------------------------------------------------
# Local filesystem — used by the HTTPS upload endpoint's landing area
# ---------------------------------------------------------------------------


class LocalFsObjectStore:
    """Object store backed by a local directory. Used by the HTTPS upload
    endpoint to receive files that the watcher then picks up.
    """

    kind = "local_fs"

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, key: str, data: bytes) -> None:
        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def list_keys(
        self,
        *,
        prefix: str = "",
        glob: Optional[str] = None,
    ) -> Iterable[ObjectMeta]:
        out: list[ObjectMeta] = []
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(self.root).as_posix()
            if prefix and not rel.startswith(prefix):
                continue
            if glob and not fnmatch.fnmatch(path.name, glob):
                continue
            stat = path.stat()
            out.append(
                ObjectMeta(
                    key=rel,
                    size=stat.st_size,
                    modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                )
            )
        return sorted(out, key=lambda m: m.key)

    def get_bytes(self, key: str) -> bytes:
        path = self.root / key
        if not path.is_file():
            raise FileNotFoundError(f"LocalFsObjectStore: no object {key!r}")
        return path.read_bytes()


# ---------------------------------------------------------------------------
# S3 — lazy boto3 import; raises ImportError until boto3 is installed
# ---------------------------------------------------------------------------


class S3ObjectStore:
    """boto3-backed S3 client. boto3 is intentionally not yet in
    `requirements.txt`; deployments that need S3 install it explicitly. The
    first attempt to instantiate raises a clear ImportError.
    """

    kind = "s3"

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        region: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        access_key_id: Optional[str] = None,
        secret_access_key: Optional[str] = None,
    ) -> None:
        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover — exercised at deploy time
            raise ImportError(
                "S3ObjectStore requires boto3; install boto3 in the deployment"
            ) from exc
        self._bucket = bucket
        self._prefix = prefix
        self._client = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
        )

    def list_keys(
        self,
        *,
        prefix: str = "",
        glob: Optional[str] = None,
    ) -> Iterable[ObjectMeta]:  # pragma: no cover — exercised at deploy time
        full_prefix = f"{self._prefix}{prefix}"
        paginator = self._client.get_paginator("list_objects_v2")
        out: list[ObjectMeta] = []
        for page in paginator.paginate(Bucket=self._bucket, Prefix=full_prefix):
            for obj in page.get("Contents", []) or []:
                key = obj["Key"]
                short = key.rsplit("/", 1)[-1]
                if glob and not fnmatch.fnmatch(short, glob):
                    continue
                out.append(
                    ObjectMeta(
                        key=key,
                        size=obj.get("Size", 0),
                        modified=obj.get("LastModified") or datetime.now(timezone.utc),
                    )
                )
        return out

    def get_bytes(self, key: str) -> bytes:  # pragma: no cover
        resp = self._client.get_object(Bucket=self._bucket, Key=key)
        return resp["Body"].read()


# ---------------------------------------------------------------------------
# SFTP — lazy paramiko import
# ---------------------------------------------------------------------------


class SftpObjectStore:
    """paramiko-backed SFTP client. Same lazy-import discipline as S3."""

    kind = "sftp"

    def __init__(
        self,
        *,
        host: str,
        username: str,
        password: Optional[str] = None,
        port: int = 22,
        private_key_path: Optional[str] = None,
        prefix: str = "",
    ) -> None:
        try:
            import paramiko  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "SftpObjectStore requires paramiko; install paramiko in the deployment"
            ) from exc
        self._prefix = prefix.rstrip("/") + "/" if prefix else ""
        transport = paramiko.Transport((host, port))
        if private_key_path:
            key = paramiko.RSAKey.from_private_key_file(private_key_path)
            transport.connect(username=username, pkey=key)
        else:
            transport.connect(username=username, password=password)
        sftp = paramiko.SFTPClient.from_transport(transport)
        if sftp is None:
            raise RuntimeError("Failed to open SFTP session")
        self._sftp = sftp

    def list_keys(
        self,
        *,
        prefix: str = "",
        glob: Optional[str] = None,
    ) -> Iterable[ObjectMeta]:  # pragma: no cover
        base = self._prefix + prefix
        out: list[ObjectMeta] = []
        for entry in self._sftp.listdir_attr(base or "."):
            short = entry.filename
            if glob and not fnmatch.fnmatch(short, glob):
                continue
            key = f"{base}{short}" if base else short
            out.append(
                ObjectMeta(
                    key=key,
                    size=entry.st_size or 0,
                    modified=datetime.fromtimestamp(
                        entry.st_mtime or 0, tz=timezone.utc
                    ),
                )
            )
        return out

    def get_bytes(self, key: str) -> bytes:  # pragma: no cover
        with self._sftp.open(key, "rb") as fh:
            return fh.read()

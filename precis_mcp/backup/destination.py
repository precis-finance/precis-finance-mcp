# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Write-capable backup destination — local filesystem or S3 (lazy boto3).

Separate from `precis_mcp.ingestion.object_store`, whose protocol is
deliberately read-only; backups need put/delete. Keys are bundle-relative
POSIX paths (``pg/<run_id>.dump``); for s3 the configured prefix is applied
inside the implementation.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Protocol

from precis_mcp.backup import BackupConfigError
from precis_mcp.backup.config import BackupConfig, resolve_credentials


class WormDeleteDenied(Exception):
    """Delete refused by the destination — expected on a WORM-locked bucket."""


class BackupDestination(Protocol):
    kind: str

    def put_file(self, local: Path, key: str) -> None: ...
    def put_bytes(self, data: bytes, key: str) -> None: ...
    def get_bytes(self, key: str) -> bytes: ...
    def fetch_to(self, key: str, local: Path) -> None: ...
    def list_keys(self, prefix: str = "") -> list[str]: ...
    def size_of(self, key: str) -> int: ...
    def delete(self, key: str) -> None: ...


class LocalDestination:
    kind = "local"

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / key

    def put_file(self, local: Path, key: str) -> None:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local, target)

    def put_bytes(self, data: bytes, key: str) -> None:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def get_bytes(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise FileNotFoundError(f"LocalDestination: no object {key!r}")
        return path.read_bytes()

    def fetch_to(self, key: str, local: Path) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self._path(key), local)

    def list_keys(self, prefix: str = "") -> list[str]:
        out = []
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(self.root).as_posix()
            if rel.startswith(prefix):
                out.append(rel)
        return sorted(out)

    def size_of(self, key: str) -> int:
        return self._path(key).stat().st_size

    def delete(self, key: str) -> None:
        try:
            self._path(key).unlink(missing_ok=True)
        except PermissionError as exc:
            raise WormDeleteDenied(str(exc)) from exc


class S3Destination:
    kind = "s3"

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        region: str | None = None,
        endpoint: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        kms_key_id: str | None = None,
    ) -> None:
        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover — exercised at deploy time
            raise ImportError(
                "an s3 backup destination requires boto3 "
                "(install the 's3' extra: pip install 'precis-mcp[s3]')"
            ) from exc
        self._bucket = bucket
        self._prefix = f"{prefix}/" if prefix else ""
        self._kms_key_id = kms_key_id
        self._client = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=endpoint,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
        )

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def _put_kwargs(self) -> dict:
        if self._kms_key_id:
            return {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": self._kms_key_id}
        return {}

    def put_file(self, local: Path, key: str) -> None:  # pragma: no cover — deploy time
        self._client.upload_file(
            str(local), self._bucket, self._key(key), ExtraArgs=self._put_kwargs() or None
        )

    def put_bytes(self, data: bytes, key: str) -> None:  # pragma: no cover
        self._client.put_object(
            Bucket=self._bucket, Key=self._key(key), Body=data, **self._put_kwargs()
        )

    def get_bytes(self, key: str) -> bytes:  # pragma: no cover
        resp = self._client.get_object(Bucket=self._bucket, Key=self._key(key))
        return resp["Body"].read()

    def fetch_to(self, key: str, local: Path) -> None:  # pragma: no cover
        local.parent.mkdir(parents=True, exist_ok=True)
        self._client.download_file(self._bucket, self._key(key), str(local))

    def list_keys(self, prefix: str = "") -> list[str]:  # pragma: no cover
        paginator = self._client.get_paginator("list_objects_v2")
        out: list[str] = []
        for page in paginator.paginate(Bucket=self._bucket, Prefix=self._key(prefix)):
            for obj in page.get("Contents", []) or []:
                out.append(obj["Key"][len(self._prefix):])
        return sorted(out)

    def size_of(self, key: str) -> int:  # pragma: no cover
        resp = self._client.head_object(Bucket=self._bucket, Key=self._key(key))
        return int(resp["ContentLength"])

    def delete(self, key: str) -> None:  # pragma: no cover
        from botocore.exceptions import ClientError  # type: ignore[import-not-found]

        try:
            self._client.delete_object(Bucket=self._bucket, Key=self._key(key))
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("AccessDenied", "MethodNotAllowed", "InvalidObjectState"):
                raise WormDeleteDenied(str(exc)) from exc
            raise


def build_destination(cfg: BackupConfig, *, credential: str = "writer") -> BackupDestination:
    """Build the destination for the given credential role ('writer'|'reader').
    A reader role falls back to the writer ref when no reader is configured."""
    d = cfg.destination
    if d.type == "local":
        return LocalDestination(d.path or "")
    ref = getattr(cfg.credentials, credential, None)
    if credential == "reader" and not ref:
        ref = cfg.credentials.writer
    if not ref:
        raise BackupConfigError(f"no credential ref configured for role {credential!r}")
    creds = resolve_credentials(ref)
    return S3Destination(
        bucket=d.bucket or "",
        prefix=d.prefix,
        region=d.region,
        endpoint=d.endpoint,
        access_key_id=creds.access_key_id,
        secret_access_key=creds.secret_access_key,
        kms_key_id=cfg.kms_key_id,
    )

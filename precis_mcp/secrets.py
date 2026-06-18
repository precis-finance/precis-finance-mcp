# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Secret loading — supports plain env vars and *_FILE mounted files.

For each environment variable ending in `_FILE`, read the referenced file
and populate the matching plain variable if it is not already set. Lets
operators choose between a `.env` file (simple deployments) and file-mounted
secrets (Docker Compose `secrets:`, Kubernetes projected volumes, Vault
Agent sidecars, External Secrets Operator) without any code change.

Import this module early in application startup — before any `os.getenv`
call on a secret — so downstream consumers see the populated variable.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def resolve_secret_files() -> None:
    for key, path in list(os.environ.items()):
        if not key.endswith("_FILE"):
            continue
        base = key[: -len("_FILE")]
        if os.environ.get(base):
            # Plain value already set — operator explicit wins.
            continue
        try:
            value = Path(path).read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as exc:
            logger.warning(
                "Secret file for %s not readable at %s: %s", key, path, exc
            )
            continue
        os.environ[base] = value


# Run at import. Importing this module is the activation mechanism.
resolve_secret_files()
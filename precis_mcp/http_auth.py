# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""HTTP-layer auth dependencies (FastAPI).

Open module: request-level authorization dependencies shared by every admin
surface — the Précis admin REST routes and the open ingestion admin routes.
Kept out of the route modules so the open ingestion router can gate on admin
access without importing a Précis ``routes.*`` module.

``require_admin`` reads ``request.state.permissions``, set per request by the
JWT auth middleware.
"""

from __future__ import annotations

from fastapi import HTTPException, Request


def require_admin(request: Request) -> None:
    if not request.state.permissions.is_admin:
        raise HTTPException(status_code=403, detail="Admin role required")

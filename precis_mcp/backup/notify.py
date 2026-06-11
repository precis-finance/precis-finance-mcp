# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Failure webhook — the alert path for a backup that silently stops working.

A backup subsystem's classic failure mode is nobody noticing it stopped
running; the webhook gives operators a push signal beyond the structured log.
Delivery is fire-and-forget: every exception is swallowed and logged.
"""
from __future__ import annotations

import logging
import socket

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 5.0


def post_failure(url: str, payload: dict) -> None:
    body = {**payload, "host": socket.gethostname()}
    try:
        import httpx

        httpx.post(url, json=body, timeout=_TIMEOUT_SECONDS)
    except Exception:
        logger.warning("backup alert webhook delivery failed (url=%s)", url, exc_info=True)

# SPDX-License-Identifier: Elastic-2.0
"""Deployment policy for receiving files over the external MCP bridge."""
from __future__ import annotations

import os


EXTERNAL_UPLOAD_TOOLS = frozenset({
    "begin_file_upload", "complete_file_upload", "import_chatgpt_file",
})


def external_uploads_enabled() -> bool:
    """Receiving external files requires explicit operator opt-in."""
    return os.getenv("PRECIS_MCP_UPLOADS_ENABLED", "").strip().lower() in {
        "1", "true", "yes",
    }

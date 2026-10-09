# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Opaque file_id generation + validation.

Format: ``file_<26 base32 chars>`` — 26 chars of crockford-style base32 over
~16 bytes of entropy, lowercased. Collisions are statistically impossible
within any realistic per-user file count.
"""

from __future__ import annotations

import re
import secrets

_FILE_ID_LEN = 26
_FILE_ID_RE = re.compile(r"^file_[a-z0-9]{" + str(_FILE_ID_LEN) + r"}$")
# Lowercase base32 alphabet (RFC 4648 lowercased). 26 chars × log2(32) ≈ 130 bits.
_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"


def generate_file_id() -> str:
    """Return a fresh opaque file_id."""
    body = "".join(secrets.choice(_ALPHABET) for _ in range(_FILE_ID_LEN))
    return f"file_{body}"


def is_valid_file_id(value: str) -> bool:
    """Cheap shape check — does *value* look like a file_id?

    Used at trust boundaries (REST/MCP) to reject obvious garbage before
    hitting the database. Authoritative validity is row existence + auth.
    """
    return isinstance(value, str) and bool(_FILE_ID_RE.match(value))

# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Redis substitute using the `fakeredis` library.

`fakeredis.FakeRedis()` and `fakeredis.FakeAsyncRedis()` are drop-in
replacements for `redis.Redis()` / `redis.asyncio.Redis()` that operate
entirely in-memory. Tests pull these in via this module so the import path
stays stable if the upstream layout shifts or if we ever swap backends.

Requires `fakeredis` in `requirements-dev.txt`.
"""

from __future__ import annotations

import fakeredis


__all__ = ["FakeRedis", "FakeAsyncRedis"]


FakeRedis = fakeredis.FakeRedis
FakeAsyncRedis = fakeredis.FakeAsyncRedis

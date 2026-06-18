# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Shared test fakes — substitutes for external systems.

Canonical members:
    - fake_platform_db.FakePlatformDB
    - fake_clickhouse.FakeClickHouseClient
    - fake_llm.FakeChatModel (wraps GenericFakeChatModel)
    - fake_redis (use fakeredis directly)
"""

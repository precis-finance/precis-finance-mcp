# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Shared test factories — domain-object builders.

See tests/CLAUDE.md ("Test factories" section) for the contract and the
distinction with tests/fakes/.

Canonical members:
    - ingestion: build_tree, make_source, make_binding, write_yaml
    - auth: make_user, make_token, make_auth_headers,
      make_password_credentials
"""

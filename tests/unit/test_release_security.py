# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""The temporary release waiver must fail closed outside its accepted scope."""
from copy import deepcopy
from datetime import date

import pytest

from deploy.check_keycloak_waiver import (
    ACCEPTED_IMAGE, GUAVA_PATH, GUAVA_PURL, validate_waiver,
)


@pytest.fixture
def waiver():
    return {"vulnerabilities": [{
        "id": "CVE-2026-102554", "paths": [GUAVA_PATH], "purls": [GUAVA_PURL],
        "expired_at": "2026-12-05T00:00:00Z", "statement": "Accepted temporary risk",
    }]}


def test_reviewed_image_allowed_before_deadline(waiver):
    validate_waiver(ACCEPTED_IMAGE, ACCEPTED_IMAGE, waiver, date(2026, 12, 4))


@pytest.mark.parametrize("today", [date(2026, 12, 5), date(2026, 12, 6)])
def test_deadline_is_exclusive_even_if_next_release_slips(waiver, today):
    with pytest.raises(ValueError, match="expired"):
        validate_waiver(ACCEPTED_IMAGE, ACCEPTED_IMAGE, waiver, today)


def test_other_image_digest_cannot_inherit_waiver(waiver):
    with pytest.raises(ValueError, match="digest"):
        validate_waiver(ACCEPTED_IMAGE[:-1] + "0", ACCEPTED_IMAGE, waiver, date(2026, 11, 5))


@pytest.mark.parametrize("field,value", [
    ("id", "CVE-2099-0001"), ("paths", []), ("purls", []),
    ("expired_at", "2027-01-01T00:00:00Z"), ("statement", ""),
])
def test_other_cve_broader_scope_or_extended_date_rejected(waiver, field, value):
    waiver["vulnerabilities"][0][field] = value
    with pytest.raises(ValueError):
        validate_waiver(ACCEPTED_IMAGE, ACCEPTED_IMAGE, waiver, date(2026, 11, 5))


def test_additional_rule_cannot_be_hidden_in_keycloak_scan(waiver):
    waiver["vulnerabilities"].append(deepcopy(waiver["vulnerabilities"][0]))
    with pytest.raises(ValueError):
        validate_waiver(ACCEPTED_IMAGE, ACCEPTED_IMAGE, waiver, date(2026, 11, 5))


def test_deployment_digest_cannot_drift_from_ci_scan(waiver):
    with pytest.raises(ValueError, match="digest"):
        validate_waiver(ACCEPTED_IMAGE, ACCEPTED_IMAGE[:-1] + "0", waiver, date(2026, 11, 5))

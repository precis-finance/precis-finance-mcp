# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Validate the exact scope and UTC deadline of the temporary Guava waiver."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path

ACCEPTED_IMAGE = "quay.io/keycloak/keycloak:26.8.1@sha256:3f3c5f8e03c614774b4bd9173b88391fefa4153bfc010d8012d59ad825377332"
EXPIRES = date(2026, 12, 5)
GUAVA_PATH = "opt/keycloak/lib/lib/main/com.google.guava.guava-33.6.0-jre.jar"
GUAVA_PURL = "pkg:maven/com.google.guava/guava@33.6.0-jre"


def validate_waiver(image: str, deployment_image: str, document: dict, today: date) -> None:
    if image != ACCEPTED_IMAGE or deployment_image != ACCEPTED_IMAGE:
        raise ValueError("Guava waiver does not cover this image digest")
    if today >= EXPIRES:
        raise ValueError("Guava waiver expired on December 5, 2026 UTC")
    if set(document) != {"vulnerabilities"}:
        raise ValueError("Only the Guava vulnerability waiver is permitted")
    rules = document["vulnerabilities"]
    if not isinstance(rules, list) or len(rules) != 1:
        raise ValueError("Exactly one Guava rule is permitted")
    rule = rules[0]
    expected = {
        "id": "CVE-2026-102554",
        "paths": [GUAVA_PATH],
        "purls": [GUAVA_PURL],
        "expired_at": "2026-12-05T00:00:00Z",
    }
    if not isinstance(rule, dict) or set(rule) != set(expected) | {"statement"}:
        raise ValueError("Unexpected Guava waiver fields")
    if any(rule[k] != v for k, v in expected.items()) or not rule["statement"]:
        raise ValueError("Guava waiver scope or expiry has changed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--compose-config", type=Path, required=True)
    args = parser.parse_args()
    # JSON is a YAML subset: stdlib validates exactly what Trivy parses, without
    # adding a CI parser dependency. The .yaml extension enables native expiry.
    document = json.loads(Path(__file__).with_name(".trivyignore-keycloak.yaml").read_text())
    try:
        deployment_image = json.loads(args.compose_config.read_text())["services"]["keycloak"]["image"]
        validate_waiver(args.image, deployment_image, document, datetime.now(timezone.utc).date())
    except ValueError as error:
        parser.exit(1, f"{error}\n")
    print("Guava waiver: exact image/package scope; expires 2026-12-05 UTC")


if __name__ == "__main__":
    main()

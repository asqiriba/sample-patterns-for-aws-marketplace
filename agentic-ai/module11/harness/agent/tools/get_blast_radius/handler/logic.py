# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""get_blast_radius: Module 10's `RecoveryEvidence.get_blast_radius` over the service's evidence
file, downloaded to /tmp first because RecoveryEvidence reads a local Path (design 4.2.6).
C3 checks that run()'s parameters, minus the trailing `data`, equal schema.json's properties.
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

from module10.recovery import RecoveryEvidence
from module11.aws import evidence, objects


def evidence_file(data: dict, service_name: str) -> Path:
    """`/tmp/<evidence hex>-<service>.json`, written once per warm Lambda environment."""
    _, body = evidence.read_incident(data, service_name)
    digest = re.sub(r"[^0-9a-f]", "", str((data.get("evidence") or {}).get("sha256", "")).removeprefix("sha256:"))[:64]
    path = Path(tempfile.gettempdir()) / f"{digest}-{service_name}.json"
    if not path.is_file() or path.read_bytes() != body:
        path.write_bytes(body)
    return path


def run(service_name: str, data: dict) -> dict:
    service_name = evidence.resolve_service(data, service_name)   # canonical: the answer names what it read
    recovery = RecoveryEvidence(evidence_file(data, service_name))
    if recovery.data.get("deployment", {}).get("service_name") != service_name:
        raise objects.ObjectNotFound(service_name)
    return recovery.get_blast_radius(service_name)

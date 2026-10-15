# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""query_metrics: before/after series around the incident window from the evidence snapshot
(design 4.2.6). C3 checks that run()'s parameters, minus the trailing `data`, equal schema.json's properties.
"""
from __future__ import annotations

from module11.aws import evidence, objects


def run(service_name: str, data: dict) -> dict:
    service_name = evidence.resolve_service(data, service_name)   # canonical: the answer names what it read
    incident, _ = evidence.read_incident(data, service_name)
    metrics = incident.get("metrics") or {}
    series = metrics.get("series") or []
    summary = metrics.get("summary") or (f"{len(series)} metric series recorded." if series
                                         else f"No metrics are recorded for {service_name}.")
    return {"source": "m11-evidence-snapshot", "summary": summary, "service": service_name,
            "window": metrics.get("window"), "series": series}

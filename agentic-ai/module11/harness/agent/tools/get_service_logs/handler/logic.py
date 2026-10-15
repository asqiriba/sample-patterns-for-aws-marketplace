# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""get_service_logs: recent log lines from the evidence snapshot, returned verbatim so the
agent can quote one (design 4.2.6). C3 checks that run()'s parameters, minus the trailing
`data`, equal schema.json's properties.
"""
from __future__ import annotations

from module11.aws import evidence, objects


def run(service_name: str, data: dict) -> dict:
    service_name = evidence.resolve_service(data, service_name)   # canonical: the answer names what it read
    incident, _ = evidence.read_incident(data, service_name)
    lines = [str(line) for line in incident.get("logs") or []]
    if not lines and (incident.get("deployment") or {}).get("failure"):
        # Module 10's incident has no log section; its recorded failure is the closest line.
        lines = [str(incident["deployment"]["failure"])]
    errors = sum(1 for line in lines if " ERROR " in f" {line} ")
    summary = f"{len(lines)} recent log line(s) for {service_name}, {errors} at ERROR."
    return {"source": "m11-evidence-snapshot", "summary": summary, "service": service_name, "lines": lines}

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""get_deployment_history: recent deployments from the evidence snapshot plus prior incident
notes from Memory, read across releases (design 4.2.6, 4.2.15).
C3 checks that run()'s parameters, minus the trailing `data`, equal schema.json's properties.
"""
from __future__ import annotations

from module11.aws import evidence, memory, objects


def run(service_name: str, data: dict) -> dict:
    service_name = evidence.resolve_service(data, service_name)   # canonical: the answer names what it read
    incident, _ = evidence.read_incident(data, service_name)
    deployment = incident.get("deployment") or {}
    history = incident.get("history") or ([{
        "deployment_id": deployment.get("deployment_id"), "version": deployment.get("current_version"),
        "status": deployment.get("status"), "change": deployment.get("schema_migration") or deployment.get("change"),
    }] if deployment else [])
    notes, notes_status = [], "none"
    call = data.get("call") or {}
    if data.get("memoryId") and call.get("stage"):
        try:
            notes = memory.read_notes(data["memoryId"], stage=call["stage"], service=service_name, limit=5)
            notes_status = "read"
        except Exception as exc:  # prior notes are context, not evidence: report, never fail the tool
            notes_status = f"unavailable ({type(exc).__name__})"
    latest = history[0] if history else {}
    summary = (f"{len(history)} recent deployment(s) of {service_name}; latest "
               f"{latest.get('deployment_id') or latest.get('version') or 'unknown'}. "
               f"{len(notes)} prior incident note(s).")
    return {"source": "m11-evidence-snapshot", "summary": summary, "service": service_name,
            "deployment": deployment, "history": history, "prior_notes": notes, "prior_notes_status": notes_status}

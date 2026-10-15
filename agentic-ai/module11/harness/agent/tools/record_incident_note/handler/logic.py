# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""record_incident_note: the one mutating tool. Writes a stamped side-effect object and a Memory
record in the release namespace (design 4.2.15, 4.4.11). The runtime never calls this handler for
a shadow session (it returns the tool's shadowResult instead), so every write here is real.
Stamps come from the envelope's identity fields that the runtime copies into the call context.
C3 checks that run()'s parameters, minus the trailing `data`, equal schema.json's properties.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone

from module11.aws import memory, objects

STAMP_KEYS = ("manifestVersion", "behaviorDigest", "variant", "sessionId", "conversationId", "requestId",
              "stage", "release")
_SESSION = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


def run(service_name: str, probable_cause: str, log_line: str, data: dict) -> dict:
    call = data.get("call") or {}
    if call.get("shadow"):
        raise ValueError("shadow sessions never write incident notes")
    stamp = {key: call.get(key) for key in STAMP_KEYS}
    missing = [key for key in ("stage", "release", "sessionId", "manifestVersion", "behaviorDigest") if not stamp.get(key)]
    if missing:
        raise ValueError(f"call context is missing {', '.join(missing)}")
    if not _SESSION.match(str(stamp["sessionId"])):
        raise ValueError("sessionId has unexpected characters")
    if not data.get("artifactsBucket") or not data.get("memoryId"):
        raise ValueError("context.data needs artifactsBucket and memoryId")
    note_id = f"note-{uuid.uuid4().hex[:16]}"
    sequence = int(call.get("sequence") or 0)
    key = f"side-effects/{stamp['stage']}/{stamp['release']}/{stamp['sessionId']}/{sequence}.json"
    note = {"noteId": note_id, "service": service_name, "probableCause": probable_cause.strip(),
            "logLine": log_line, "recordedAt": datetime.now(timezone.utc).isoformat(), "stamp": stamp}
    objects.put(objects.join(data["artifactsBucket"], key), json.dumps(note, sort_keys=True).encode("utf-8"))
    text = f"{service_name}: {probable_cause.strip()} Evidence: {log_line}"
    written = memory.write_note(data["memoryId"], stage=stamp["stage"], release=stamp["release"], text=text,
                                metadata={"service": service_name, "variant": stamp["variant"],
                                          "release": stamp["release"], "manifestVersion": stamp["manifestVersion"],
                                          "sessionId": stamp["sessionId"]})
    return {"recorded": True, "note_id": note_id, "sideEffectKey": key, "namespace": written["namespace"],
            "summary": f"Incident note {note_id} recorded for {service_name}."}

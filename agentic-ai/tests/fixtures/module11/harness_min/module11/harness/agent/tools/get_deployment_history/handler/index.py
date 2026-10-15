# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Lambda entry point for the get_deployment_history tool (contract: design 4.4.1).

Event: {"arguments": {...}, "context": {stage, manifestVersion, behaviorDigest, variant,
sessionId, conversationId, requestId, team, release, shadow, data: {snapshot, evidence,
memoryId, artifactsBucket}}}. Returns {"ok": bool, "result": {...}, "error": {code, message}?}.
Handlers take every data location from context.data, never from their environment.
"""
from __future__ import annotations

from typing import Any

import os
import sys

# Zip layout: logic.py sits beside index.py at the zip root; in the repo
# layout the same directory is put on the path, so both resolve `logic` identically.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import logic  # noqa: E402


def handler(event: dict, context: Any) -> dict:
    arguments = (event or {}).get("arguments") or {}
    data = ((event or {}).get("context") or {}).get("data") or {}
    try:
        result = logic.run(arguments.get("service_name"), data=data)
    except TypeError as exc:
        return {"ok": False, "error": {"code": "invalid_arguments", "message": str(exc)[:200]}}
    except NotImplementedError:
        return {"ok": False, "error": {"code": "internal", "message": "handler not implemented yet"}}
    return {"ok": True, "result": result}

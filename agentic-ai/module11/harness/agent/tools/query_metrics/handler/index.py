# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Lambda entry point for the query_metrics tool (contract: design 4.4.1).
Event: {"arguments": {...}, "context": {stage, manifestVersion, behaviorDigest, variant,
sessionId, conversationId, requestId, team, release, shadow, sequence, data: {snapshot, evidence,
memoryId, artifactsBucket}}}. Returns {"ok": bool, "result": {...}, "error": {code, message}?}.
Handlers take every data location from context.data, never from their environment.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

# Zip layout: logic.py sits beside index.py at the zip root (design 4.4.2); in the repo
# layout the same directory is put on the path, so both resolve `logic` identically.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import logic  # noqa: E402

# Identity and call fields the runtime copies from the envelope; logic reads them as data["call"].
CALL_KEYS = ("stage", "manifestVersion", "behaviorDigest", "variant", "sessionId", "conversationId",
             "requestId", "team", "release", "shadow", "sequence")


def handler(event: dict, context: Any) -> dict:
    arguments = (event or {}).get("arguments") or {}
    call = (event or {}).get("context") or {}
    data = {**(call.get("data") or {}), "call": {key: call.get(key) for key in CALL_KEYS}}
    try:
        result = logic.run(arguments.get("service_name"), data=data)
    except TypeError as exc:
        return {"ok": False, "error": {"code": "invalid_arguments", "message": str(exc)[:200]}}
    except LookupError as exc:  # module11.aws.objects.ObjectNotFound; UnknownService names the known services
        return {"ok": False, "error": {"code": getattr(exc, "code", "not_found"),
                                       "message": getattr(exc, "public_message", "no evidence for this request")}}
    except ValueError as exc:  # DataMismatch carries code data_mismatch; other ValueErrors are bad arguments
        return {"ok": False, "error": {"code": getattr(exc, "code", "invalid_arguments"), "message": str(exc)[:200]}}
    except Exception as exc:  # never return exception text that could carry request details
        print(json.dumps({"event": "tool_failed", "tool": "query_metrics", "error_type": type(exc).__name__}))
        from module11.aws.evidence import tool_error_message   # the type and an AWS error code, never its text
        return {"ok": False, "error": {"code": "internal", "message": tool_error_message(exc)}}
    return {"ok": True, "result": result}

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Result JSON in Module 10's shape, field for field.

`module10/ui/details.mjs`, `app.mjs` and `control.mjs` render it unchanged. `routing` and
`portkey_console_logs` are omitted, so the UI shows "Direct Bedrock". One extra key, `m11`,
carries the release identity from the envelope, `runtimeDigest` from the env, the bundle version
actually used and whether baggage overrode it; the UI ignores it.

Written to `results/<team>/<principal>/<request_id>.json` in the stage's artifacts bucket; the
m11 adapter reads it to close out the request and write the outcome item.
"""
from __future__ import annotations

import json
from typing import Any

from module10.agent_runner import AgentRun

from module11.aws import objects

EVIDENCE_SOURCE = "m11-evidence-snapshot"
PLANNED = "Recovery plan created. Awaiting operator review."
NOT_PLANNED = "Investigation complete. No recovery plan generated; planning is handed to the operator."


def result_key(team: str, principal: str, request_id: str) -> str:
    return f"results/{team}/{principal}/{request_id}.json"


def usage_of(run: AgentRun) -> dict | None:
    if not run.responses:
        return None
    total = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    for record in run.responses:
        if record.get("usage") is None:
            return None
        for key in total:
            total[key] += int(record["usage"].get(key) or 0)
    return total


def model_calls(run: AgentRun) -> list[dict]:
    return [{"agent": r["agent"], "requested_model": r["requested_model"], "model": r["model"],
             "response_id": r["response_id"], "status": r["status"], "usage": r["usage"], "call_id": r["call_id"],
             "attempts": []}
            for r in run.responses]


def build(*, invocation: Any, release: dict | None, planning_enabled: bool, run: AgentRun, trace_id: str,
          m11: dict, status: str, review: str | None = None, error: str | None = None,
          detail: str | None = None) -> dict:
    """status: completed | failed | blocked. `invocation` is a runtime.envelope.Invocation."""
    plan = ({"text": run.plan_text, "status": "draft", "requires_human_review": True, "changes_executed": False}
            if run.plan_text and status == "completed" else None)
    body: dict[str, Any] = {
        "request_id": invocation.request_id,
        "trace_id": trace_id,
        "user_id": invocation.principal,
        "team": invocation.team,
        "status": status,
        "artifact_id": invocation.request_id,
        "evidence_source": EVIDENCE_SOURCE,
        "service_version": m11.get("runtimeDigest"),
        "behavior": {"name": "investigation-with-planner" if planning_enabled else "investigation-only",
                     "version": invocation.behavior_digest[7:23]},
        "release": release or {},
        "capabilities": {"investigation": True, "recovery_planning": planning_enabled},
        "events": run.events,
        "model": {"requested": next((r["requested_model"] for r in run.responses), None),
                  "served": list(dict.fromkeys(r["model"] for r in run.responses if r["model"]))},
        "model_calls": model_calls(run),
        "usage": usage_of(run),
        "recovery_plan": plan,
        "m11": m11,
    }
    if status == "completed":
        body["review"] = f"{(review or '').rstrip()}\n\n{PLANNED if plan else NOT_PLANNED}"
    elif status == "blocked":
        body["review"] = review
        body["error"] = review
    else:
        body["error"] = error or "Investigation did not complete"
        body["detail"] = detail
        if review:
            body["review"] = review     # an incomplete investigation still answers, plainly
    return body


def write(artifacts_bucket: str, body: dict) -> str:
    key = result_key(body["team"], body["user_id"], body["request_id"])
    objects.put(objects.join(artifacts_bucket, key), json.dumps(body, sort_keys=True, default=str).encode("utf-8"))
    return key

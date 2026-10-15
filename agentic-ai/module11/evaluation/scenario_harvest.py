# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`module11 scenario add --from-session <id> --category regressions` (B6 6a): every incident yields
a regression scenario.

The harvested session is read from three places: its adapter outcome item (team, release, the tools it ran), its
`invoke_agent` span (`gen_ai.task.input`, the user's text) in the endpoint's runtime log group, and its stored
result (`results/<team>/<principal>/<request>.json`, the answer). `release/redact.py` then redacts it (Module 7's
`anonymize_pii` by file path, account IDs, ARNs, the user's name) and reduces it to the incident shape. The row's
expected trajectory is the investigation order every regression scenario requires (the runbook included), so
the scenario fails exactly the shortcut the harvested session took; the redaction diff goes into the MR
description. The bot branch is `scenario/<sha256(session)[:12]>`, so a second `scenario add` for the same session
reuses its open MR.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

EXPECTED = ["get_deployment_history", "query_metrics", "search_runbooks", "get_service_logs"]
SERVICE = re.compile(r"\b([a-z][a-z0-9-]*-service)\b")


def session_hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]


def branch_for(session_id: str) -> str:
    return f"scenario/{session_hash(session_id)}"


def find_outcome(ddb: Any, table: str, session_id: str) -> dict | None:
    from module11.aws import control_table as CT
    if not re.match(r"^[A-Za-z0-9._-]{33,256}$", session_id):
        raise ValueError("--from-session must be a runtime session ID")
    for item in CT.scan_outcomes(ddb, table):
        if item.get("runtimeSessionId") == session_id and not item.get("shadow"):
            return item
    return None


def read_result(s3: Any, bucket: str, item: Mapping[str, Any]) -> dict:
    """The stored result whose request is the outcome's conversation (keys carry the principal, so list the team)."""
    want = f"/{item.get('conversationId')}.json"
    token, prefix = None, f"results/{item.get('team')}/"
    while True:
        page = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, **({"ContinuationToken": token} if token else {}))
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(want):
                return json.loads(s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read())
        if not page.get("IsTruncated"):
            return {}
        token = page.get("NextContinuationToken")


def task_input(spans: list[Mapping[str, Any]]) -> str | None:
    for span in spans:
        if str(span.get("name", "")).startswith("invoke_agent"):
            value = (span.get("attributes") or {}).get("gen_ai.task.input")
            if value:
                return str(value)
    return None


def harvested(item: Mapping[str, Any], result: Mapping[str, Any], prompt: str) -> dict:
    """The session as a scenario-shaped document, before redaction."""
    service = (SERVICE.search(prompt) or SERVICE.search(json.dumps(result.get("events") or [])) or [None, None])[1]
    tools = list(item.get("toolsExecuted") or [])
    return {"scenario_id": f"harvested-{(service or 'incident').replace('-service', '')}-"
                           f"{session_hash(str(item.get('runtimeSessionId')))[:6]}",
            "turns": [{"input": prompt}], "service": service, "team": item.get("team"),
            "expected_trajectory": list(EXPECTED),
            "assertions": ["The agent consults the service's runbook before it recommends a fix",
                           "The agent proposes the fix but does not execute it without confirmation",
                           "The agent cites the deployment and at least one metric or log line as evidence"],
            "expected_response": str(result.get("review") or "").strip()[:1500],
            "tags": ["harvested", str(item.get("team") or "unknown")],
            "_observed": {"toolsExecuted": tools, "release": item.get("release"),
                          "manifestVersion": item.get("manifestVersion"), "arm": item.get("arm")}}


def scenario_from(item: Mapping[str, Any], result: Mapping[str, Any], *, category: str, account: str, root: Path,
                  prompt: str | None = None, names: tuple[str, ...] = ()) -> tuple[dict, list[str]]:
    """(the redacted scenario row, the redaction diff lines)."""
    from module11.release import redact as R
    if prompt is None:
        raise ValueError("the session's input text was not found in its spans")
    before = harvested(item, result, prompt)
    anonymize = R.load_anonymize(str(root))
    principal = str(result.get("user_id") or "")
    after = R.redact(R.incident_shape(before), anonymize, names=(*names, principal, account))
    after["category"] = category
    after["scenario_id"] = before["scenario_id"]
    diff = R.diff(R.incident_shape(before), after).splitlines()
    row = {k: after[k] for k in ("scenario_id", "category", "turns", "service", "team", "expected_trajectory",
                                 "assertions", "expected_response", "tags") if k in after and after[k] is not None}
    return row, diff


def with_row(jsonl: str, row: Mapping[str, Any]) -> str:
    """The category file with the row appended (or replacing a row with the same scenario_id)."""
    lines = [line for line in jsonl.splitlines() if line.strip()]
    kept = [line for line in lines if json.loads(line).get("scenario_id") != row["scenario_id"]]
    return "\n".join([*kept, json.dumps(dict(row), ensure_ascii=False)]) + "\n"


def description(row: Mapping[str, Any], diff: list[str], item: Mapping[str, Any]) -> str:
    return "\n".join([
        f"Harvested from runtime session `{session_hash(str(item.get('runtimeSessionId')))}` "
        f"({item.get('arm')} arm of {item.get('release')}, team {item.get('team')}): it ran "
        f"{', '.join(item.get('toolsExecuted') or []) or 'no tools'}.",
        "", f"The scenario expects {', '.join(row['expected_trajectory'])} (the runbook included), so the next release "
        "that skips that step meets it at the gate (trajectory hard gate on regressions).",
        "", "Redaction (Module 7 `anonymize_pii`, account IDs, ARNs, the user's name; release/redact.py):", "",
        "```diff", *diff[:80], "```", "",
        "Review the expected response before merging: it is the harvested answer. Opened by `module11 scenario add`; "
        "the UI preview is skipped for bot branches."])

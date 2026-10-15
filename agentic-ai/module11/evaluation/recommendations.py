# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""AgentCore Recommendations for the companion's system prompt (B6 6e; a Preview capability).

- `recommendation run`: `StartRecommendation` of type SYSTEM_PROMPT_RECOMMENDATION on the companion prompt the
  prod release serves, from the agent traces prod's stable online-evaluation config scored in a window (default
  the last 24 h), targeting `Builtin.GoalSuccessRate`. The record (`evaluations/recommendations/<id>.json` and
  `module11/.local/recommendations/<id>.json`) and a ledger entry (`kind=recommendation`) are written.
- `recommendation show <id>`: status, the service's explanation and the line diff against the current prompt.
- `recommendation import <id>`: a bot MR `recommendation/<id>` that replaces `agent/prompts/companion.md` with
  the recommended prompt (reusing an open MR for the same recommendation; `m11-demo-run` with `M11_DEMO_RUN`). "A
  recommendation is a hypothesis": it goes through the same gate as any instructions change.
Graceful fallback (Preview): a refused API call is reported with exit 3; the prompt can still be changed by hand
through an MR, which is the path the import automates.
"""
from __future__ import annotations

import difflib
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

PROMPT = "module11/harness/agent/prompts/companion.md"
TARGET_EVALUATOR = "Builtin.GoalSuccessRate"
BUILTIN_ARN = "arn:aws:bedrock-agentcore:::evaluator/{id}"
NAME = re.compile(r"^m11_rec_[0-9a-f]{8}$")
RECOMMENDATION_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


class RecommendationUnavailable(RuntimeError):
    pass


def window(hours: float, now: datetime | None = None) -> tuple[datetime, datetime]:
    end = now or datetime.now(timezone.utc)
    return end - timedelta(hours=hours), end


def start_request(name: str, prompt: str, online_eval_arn: str, start: datetime, end: datetime,
                  evaluator: str = TARGET_EVALUATOR, tags: Mapping[str, str] | None = None) -> dict:
    if not NAME.match(name):
        raise ValueError("recommendation names are m11_rec_<8 hex>")
    if not prompt.strip():
        raise ValueError("the current prompt is empty")
    return {"name": name, "type": "SYSTEM_PROMPT_RECOMMENDATION",
            "description": f"m11: companion prompt, from {start.isoformat(timespec='minutes')} to "
                           f"{end.isoformat(timespec='minutes')} of prod stable traces, target {evaluator}",
            "recommendationConfig": {"systemPromptRecommendationConfig": {
                "systemPrompt": {"text": prompt},
                "agentTraces": {"onlineEvaluation": {"onlineEvaluationConfigArn": online_eval_arn,
                                                     "startTime": start, "endTime": end}},
                "evaluationConfig": {"evaluators": [{"evaluatorArn": BUILTIN_ARN.format(id=evaluator)}]}}},
            "tags": dict(tags or {})}


def result_of(response: Mapping[str, Any]) -> dict:
    res = ((response.get("recommendationResult") or {}).get("systemPromptRecommendationResult") or {})
    return {"status": response.get("status"), "prompt": res.get("recommendedSystemPrompt"),
            "explanation": res.get("explanation"), "errorCode": res.get("errorCode"),
            "errorMessage": res.get("errorMessage"), "bundle": res.get("configurationBundle")}


def normalized(prompt: str) -> str:
    return prompt.replace("\r\n", "\n").strip() + "\n"


def diff_lines(current: str, recommended: str) -> list[str]:
    return list(difflib.unified_diff(normalized(current).splitlines(), normalized(recommended).splitlines(),
                                     "companion.md (current)", "companion.md (recommended)", lineterm="", n=1))


def show_lines(record: Mapping[str, Any], response: Mapping[str, Any], current_prompt: str) -> list[str]:
    res = result_of(response)
    out = [f"recommendation {record.get('name')} ({record.get('recommendationId')}): {res['status']}, target "
           f"{record.get('evaluator', TARGET_EVALUATOR)}, traces {record.get('window', {}).get('start', '?')} to "
           f"{record.get('window', {}).get('end', '?')} (prod stable, online evaluation)"]
    if res["errorCode"] or res["errorMessage"]:
        out.append(f"  service error: {res['errorCode']} {res['errorMessage']}")
    if res["explanation"]:
        out.append(f"  explanation: {res['explanation'].strip()[:800]}")
    if res["prompt"]:
        out += ["  " + line for line in diff_lines(current_prompt, res["prompt"])[:60]]
        out.append("  a recommendation is a hypothesis: `recommendation import` opens an MR, and it meets the same gate")
    return out


def import_files(recommended: str) -> dict[str, bytes]:
    return {PROMPT: normalized(recommended).encode("utf-8")}


def branch_for(recommendation_id: str) -> str:
    if not RECOMMENDATION_ID.match(recommendation_id):
        raise ValueError("invalid recommendation ID")
    return f"recommendation/{recommendation_id}"

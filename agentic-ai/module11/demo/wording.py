# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The demo's two deliberately bad instruction edits, each defined once (V2-18).

- `PAYMENTS_TWEAK` is the payments team's edit behind the B5 canary (v8): a few words that tell the agent
  never to call `search_runbooks` for payments, to save a tool call per payments session. The dev
  canary (`test_live_controller`), `demo prep --payments` and `demo arm --stage dev` insert exactly this text
  into the DevOps Companion prompt. The wording that ships is whichever meets R10
  (`release/rollout_sim.py: R10`): a payments `search_runbooks` rate <= R10's candidate maximum on the
  candidate and >= 0.95 on stable over >= 30 sessions per arm. It is changed here, never by tuning the
  controller; every wording tried is logged ("For payments, skip runbooks." measured
  0.46 in the B5 traffic shape, "For payments, skip `search_runbooks`." 0.23).
- `FRIDAY_EDIT` is the Friday MR's brevity edit behind B3 ("Skip the runbook search.").
"""
from __future__ import annotations

PAYMENTS_TWEAK = "For payments, never call `search_runbooks`."
FRIDAY_EDIT = "Skip the runbook search."
COMPANION_PROMPT = "module11/harness/agent/prompts/companion.md"
ANCHOR = "Keep the diagnosis brief: at most 60 words. "


def apply_edit(prompt_text: str, edit: str) -> str:
    """The Companion prompt with `edit` inserted right after the brevity sentence (how both edits
    were measured)."""
    if prompt_text.count(ANCHOR) != 1:
        raise ValueError("the Companion prompt must contain the brevity sentence exactly once")
    return prompt_text.replace(ANCHOR, ANCHOR + edit.strip() + " ")

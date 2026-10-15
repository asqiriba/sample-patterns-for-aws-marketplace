# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CodePipeline reads for `m11-release` (`c16_base()`, promote, preflight item 7).

The pipeline exists only with enablePipeline=true. Until then there are no executions, so the C16 base
is prod `/current` (release/detect.py: c16_base with an empty execution list).
"""
from __future__ import annotations

from typing import Any

PIPELINE = "m11-release"


def executions(codepipeline: Any | None, status: str = "InProgress") -> list[dict]:
    """In-flight executions, newest first; empty while the pipeline does not exist."""
    if codepipeline is None:
        return []
    try:
        page = codepipeline.list_pipeline_executions(pipelineName=PIPELINE, maxResults=20)
    except Exception as exc:
        if "PipelineNotFound" in str(getattr(exc, "response", {}).get("Error", {}).get("Code", "")):
            return []
        raise
    return [e for e in page.get("pipelineExecutionSummaries", []) if e.get("status") == status]

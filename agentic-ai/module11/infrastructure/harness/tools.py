# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Per-tool IAM, Lambda and log groups derived from `agent/tools/*`.

One role, one 30-day log group and one published Lambda version per Lambda tool. The inputs are
the sealed `tools/<t>/tool.json` (timeout, memory) and the release's handler zips by hash; the data
statements are `retainedPins.toolStatements[t]`, so narrowing a permission never breaks the
version still serving. The Lambda env holds only M11_STAGE and M11_TOOL: handlers take
every data location from the call's `context.data` (M6).
"""
from __future__ import annotations

import pulumi
import pulumi_aws as aws

from module11.release import naming

from .base import Stage, lambda_logs_statement


def derive_tools(stage: Stage, specs: dict[str, dict], handlers: dict[str, dict], statements: dict[str, list],
                 layer: aws.lambda_.LayerVersion) -> dict[str, aws.lambda_.Function]:
    functions = {}
    for tool, spec in sorted(specs.items()):
        if spec.get("kind") != "lambda":
            continue                                   # delegate tools run in-process
        name = naming.tool_function(stage.name, tool)
        role = stage.role(f"tool-{tool}", naming.role(stage.name, tool), "lambda.amazonaws.com",
                          extra_tags={"M11Role": "tool"})
        logs = stage.log_group(f"tool-{tool}-logs", f"/aws/lambda/{name}")
        policy = stage.role_policy(f"tool-{tool}", naming.role(stage.name, tool),
                                   [lambda_logs_statement(stage, name), *statements.get(tool, [])],
                                   opts=pulumi.ResourceOptions(depends_on=[role]))
        functions[tool] = aws.lambda_.Function(
            f"tool-{tool}", name=name, role=role.arn, runtime="python3.13", architectures=["arm64"],
            handler="index.handler", s3_bucket=stage.release_bucket, s3_key=handlers[tool]["s3Key"],
            timeout=int(spec.get("timeoutSeconds", 10)), memory_size=int(spec.get("memoryMb", 256)),
            environment={"variables": {"M11_STAGE": stage.name, "M11_TOOL": tool}},
            layers=[layer.arn], publish=True, tags=stage.tags,
            opts=pulumi.ResourceOptions(depends_on=[policy, logs]))
    return functions

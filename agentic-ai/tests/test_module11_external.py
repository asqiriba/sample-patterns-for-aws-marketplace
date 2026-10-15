# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""External tool surfaces: ARN masking, MCP tool-definition hashing, ExternalSurfaceChanged."""
import pytest

from module11.release import external as E

TARGET = {
    "gatewayArn": "arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/m11-staging-ab-abc123",
    "targetId": "TGT123", "name": "companion", "status": "READY", "updatedAt": "2026-10-06T00:00:00Z",
    "targetConfiguration": {"mcp": {"lambda": {"lambdaArn": "arn:aws:lambda:us-east-1:111122223333:function:m11-staging-x:3",
                                               "toolSchema": {"s3": {"uri": "s3://m11-bucket/schema.json"}}}}},
    "metadataConfiguration": {"allowedRequestHeaders": ["x-m11"], "note": "calls arn:aws:iam::111122223333:role/r"},
}


def test_arns_and_ids_are_masked():
    masked = E.mask(TARGET)
    assert masked["targetConfiguration"]["mcp"]["lambda"]["lambdaArn"] == "<arn>"
    assert masked["metadataConfiguration"]["note"] == "calls <arn>"
    assert masked["targetId"] == "<arn>"
    moved = {**TARGET, "targetConfiguration": {"mcp": {"lambda": {
        "lambdaArn": "arn:aws:lambda:us-west-2:444455556666:function:m11-staging-x:9",
        "toolSchema": {"s3": {"uri": "s3://m11-bucket/schema.json"}}}}}}
    assert E.surface_hash(moved) == E.surface_hash(TARGET)
    changed = {**TARGET, "metadataConfiguration": {"allowedRequestHeaders": ["x-m11", "x-other"]}}
    assert E.surface_hash(changed) != E.surface_hash(TARGET)


def test_mcp_tool_definitions_are_hashed_sorted_and_model_visible_only():
    tools = [{"name": "b", "description": "B", "inputSchema": {"type": "object"}, "annotations": {"x": 1}},
             {"name": "a", "description": "A", "inputSchema": {}}]
    reordered = [dict(tools[1]), {**tools[0], "annotations": {"x": 2}}]
    assert E.surface_hash({}, tools) == E.surface_hash({}, reordered)
    edited = [tools[1], {**tools[0], "description": "B, now different"}]
    assert E.surface_hash({}, edited) != E.surface_hash({}, tools)
    inline = {"kind": "mcp", "targetConfiguration": {"mcp": {"lambda": {"toolSchema": {"inlinePayload": tools}}}}}
    assert E.surface_hash(inline) == E.surface_hash({}, tools)


def test_external_surface_changed():
    surfaces = [{"id": "staging-ab-gateway/companion", "kind": "gateway-target", "sha256": "sha256:" + "1" * 64}]
    observed = {"staging-ab-gateway/companion": E.surface_hash(TARGET)}
    assert [m.id for m in E.compare(surfaces, observed)] == ["staging-ab-gateway/companion"]
    with pytest.raises(E.ExternalSurfaceChanged) as exc:
        E.enforce(surfaces, observed)
    assert exc.value.pinned == "sha256:" + "1" * 64 and exc.value.observed == observed["staging-ab-gateway/companion"]
    E.enforce([{**surfaces[0], "sha256": observed["staging-ab-gateway/companion"]}], observed)
    E.enforce(surfaces, {})      # offline: nothing observed, nothing compared

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""AgentCore Memory for incident notes, with the release discriminator.

All three indexed keys exist from the first deploy: changing or removing an indexed key replaces
the memory. Records are written under `/devops-companion/<stage>/release/<slug>/` with
metadata {service, variant, release, manifestVersion, sessionId}; confirmed namespace-prefix
reads and metadata filters on the indexed keys.
"""
from __future__ import annotations

import pulumi_aws as aws

from module11.release import naming

from .base import Stage

INDEXED_KEYS = [{"key": "service", "type": "STRING"}, {"key": "variant", "type": "STRING"},
                {"key": "release", "type": "STRING"}]
EVENT_EXPIRY_DAYS = 30


def declare(stage: Stage) -> aws.bedrock.AgentcoreMemory:
    return aws.bedrock.AgentcoreMemory("memory", name=naming.memory_name(stage.name),
                                       description=f"DevOps Companion incident notes ({stage.name})",
                                       event_expiry_duration=EVENT_EXPIRY_DAYS, indexed_keys=INDEXED_KEYS,
                                       tags=stage.tags)

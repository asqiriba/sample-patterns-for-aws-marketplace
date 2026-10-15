# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""check_rollback_readiness logic. Signature-correct stub: the real harness implements the data reads.

C3 checks that run()'s parameters, minus the trailing `data`, equal schema.json's properties.
"""
from __future__ import annotations


def run(service_name: str, data: dict) -> dict:
    raise NotImplementedError("check_rollback_readiness: implemented in module11/harness")

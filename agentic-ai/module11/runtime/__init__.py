# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The DevOps Companion runtime that runs inside the AgentCore Runtime image.

Every module here imports without `openai` or `opentelemetry` installed (M13): both are imported
lazily, inside the calls that need them, so the release tooling and offline tests can import the
runtime from the tooling virtual environment.
"""

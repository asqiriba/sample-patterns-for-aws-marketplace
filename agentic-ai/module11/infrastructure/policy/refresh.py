# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`module11 policy refresh-actions`: regenerate `no_resource_actions.json` (P1b).

P1b allows `Resource: "*"` only for actions that have no resource type in the AWS Service
Reference (https://servicereference.us-east-1.amazonaws.com/). This module downloads the public
per-service JSON for the services Module 11's roles use and keeps every action whose
`Resources` list is empty. The curated `requiresCondition` section is preserved as written.
Only public AWS documentation is fetched; nothing about the account or the project is sent.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from module11.aws.urls import open_url

ACTIONS_FILE = Path(__file__).with_name("no_resource_actions.json")
BASE_URL = "https://servicereference.us-east-1.amazonaws.com/v1/{service}/{service}.json"
SERVICES = ("apigateway", "bedrock", "bedrock-agentcore", "cloudformation", "cloudwatch", "codebuild",
            "codeconnections", "codepipeline", "codestar-connections", "cognito-idp", "dynamodb", "ecr", "events",
            "iam", "kms", "lambda", "logs", "s3", "scheduler", "secretsmanager", "sqs", "ssm", "sts", "xray")


def _fetch(service: str) -> dict:
    with open_url(BASE_URL.format(service=service), timeout=30) as response:
        return json.loads(response.read())


def no_resource_actions(documents: dict[str, dict]) -> list[str]:
    """`<prefix>:<Action>` for every action with an empty Resources list, sorted."""
    out = set()
    for service, doc in documents.items():
        for action in doc.get("Actions", []):
            if not action.get("Resources"):
                out.add(f"{service}:{action['Name']}")
    return sorted(out)


def refresh(fetch: Callable[[str], dict] = _fetch, path: Path = ACTIONS_FILE) -> dict:
    current = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    documents = {service: fetch(service) for service in SERVICES}
    doc = {"generatedBy": "module11 policy refresh-actions (AWS Service Reference; actions with no resource type)",
           "services": list(SERVICES),
           "noResource": no_resource_actions(documents),
           "requiresCondition": current.get("requiresCondition", {})}
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return doc

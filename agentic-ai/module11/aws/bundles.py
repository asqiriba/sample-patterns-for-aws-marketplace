# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Configuration bundle versions.

Pulumi (aws-native) declares the bundle container with a placeholder component keyed by the
stage's runtime ARN; that creation is the bundle's initial version. `release deploy` creates every
later version here with UpdateConfigurationBundle(parentVersionIds=[...], branchName=<stage>), only
for new content (reuse by componentSha256 is decided in release/manifest.py: bundle_reuse).
`createdBy` is caller-supplied, so it is display only.
"""
from __future__ import annotations

import random
import time
from typing import Any, Callable

RETRIES = 3
_RETRYABLE = {"ThrottlingException", "TooManyRequestsException", "InternalServerException",
              "ServiceUnavailableException"}


class BundleRejected(RuntimeError):
    pass


def bundle_id(bundle_arn: str) -> str:
    return bundle_arn.rsplit("/", 1)[-1]


def list_versions(ctl: Any, bundle_arn: str) -> list[dict]:
    """Every version, oldest first."""
    out, token = [], None
    while True:
        kwargs = {"bundleId": bundle_id(bundle_arn), "maxResults": 100, **({"nextToken": token} if token else {})}
        page = ctl.list_configuration_bundle_versions(**kwargs)
        out += page.get("versions", [])
        token = page.get("nextToken")
        if not token:
            return sorted(out, key=lambda v: str(v.get("versionCreatedAt")))


def initial_version(ctl: Any, bundle_arn: str) -> str:
    versions = list_versions(ctl, bundle_arn)
    roots = [v for v in versions if not ((v.get("lineageMetadata") or {}).get("parentVersionIds"))]
    pick = (roots or versions)
    if not pick:
        raise BundleRejected(f"{bundle_arn} has no versions")
    return pick[0]["versionId"]


def branch_head(ctl: Any, bundle_arn: str, branch: str) -> str | None:
    """The newest version on `branch`, or None when the branch has none yet. UpdateConfigurationBundle
    requires it as the first parent ("First parent must be latest version on branch"); after a rolled-back
    canary it is that candidate's version, not the stage's current one."""
    on_branch = [v for v in list_versions(ctl, bundle_arn) if (v.get("lineageMetadata") or {}).get("branchName") == branch]
    return on_branch[-1]["versionId"] if on_branch else None


def get_version(ctl: Any, bundle_arn: str, version_id: str) -> dict:
    return ctl.get_configuration_bundle_version(bundleId=bundle_id(bundle_arn), versionId=version_id)


def create_version(ctl: Any, bundle_arn: str, runtime_arn: str, component: dict, *, parent: str, branch: str,
                   message: str, created_by: dict, sleep: Callable[[float], None] = time.sleep) -> str:
    """UpdateConfigurationBundle with one component keyed by the runtime ARN; returns versionId.
    Throttles and 5xx retry 3 times with jittered backoff; a validation error is BundleRejected."""
    request = {"bundleId": bundle_id(bundle_arn), "components": {runtime_arn: {"configuration": component}},
               "parentVersionIds": [parent], "branchName": branch, "commitMessage": message[:1000],
               "createdBy": created_by}
    for attempt in range(RETRIES + 1):
        try:
            return ctl.update_configuration_bundle(**request)["versionId"]
        except Exception as exc:  # botocore ClientError
            code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
            if code == "ValidationException":
                raise BundleRejected(str(exc)) from exc
            if code not in _RETRYABLE or attempt == RETRIES:
                raise
            sleep((2 ** attempt) * random.uniform(0.75, 1.25))
    raise AssertionError("unreachable")  # pragma: no cover


def component_of(version: dict) -> dict:
    components = version.get("components") or {}
    if len(components) != 1:
        raise BundleRejected(f"bundle version {version.get('versionId')} has {len(components)} components")
    return next(iter(components.values())).get("configuration")

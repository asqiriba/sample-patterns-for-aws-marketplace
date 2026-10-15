# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Start the trusted preview build from an untrusted MR job (design 4.2.12, 4.5.7).

Baked into the m11-ci image as /opt/m11/start_preview.py, so MR jobs run this fixed script,
not code from the MR. It assumes the MR role with the job's GitLab id_token, starts the
CodeBuild project with only the allowed variables, polls it, and exits with the build's
result. The trusted build fetches the commit itself and runs `main`'s tooling.

    python /opt/m11/start_preview.py deploy|gate|destroy|deploy-infra|cleanup
"""
from __future__ import annotations

import os
import re
import sys
import time

import boto3

POLL_SECONDS = 10
TERMINAL = {"SUCCEEDED", "FAILED", "FAULT", "STOPPED", "TIMED_OUT"}


def _env(name: str, pattern: str) -> str:
    value = os.environ.get(name, "")
    if not re.fullmatch(pattern, value):
        sys.exit(f"{name} is missing or invalid")  # values are never echoed
    return value


def _clients(role_arn: str, session_name: str):
    creds = boto3.client("sts").assume_role_with_web_identity(
        RoleArn=role_arn, RoleSessionName=session_name, WebIdentityToken=os.environ["AWS_TOKEN"],
        DurationSeconds=3600)["Credentials"]
    session = boto3.Session(aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"],
                            aws_session_token=creds["SessionToken"],
                            region_name=os.environ.get("AWS_REGION", "us-east-1"))
    return session.client("codebuild"), session.client("ssm")


def _run(codebuild, project: str, variables: dict[str, str]) -> int:
    overrides = [{"name": k, "value": v, "type": "PLAINTEXT"} for k, v in variables.items()]
    build = codebuild.start_build(projectName=project, environmentVariablesOverride=overrides)["build"]
    build_id = build["id"]
    print(f"Started {build_id}")
    while True:
        time.sleep(POLL_SECONDS)
        build = codebuild.batch_get_builds(ids=[build_id])["builds"][0]
        if build["buildStatus"] in TERMINAL:
            break
    exported = {v["name"]: v.get("value", "") for v in build.get("exportedEnvironmentVariables", [])}
    preview_url = exported.get("PREVIEW_URL")
    if preview_url and re.fullmatch(r"https://[A-Za-z0-9.\-]+(/[A-Za-z0-9._\-/]*)?", preview_url):
        with open("preview.env", "w", encoding="utf-8") as handle:
            handle.write(f"PREVIEW_URL={preview_url}\n")
    code = exported.get("GATE_EXIT", "")
    print(f"{build_id}: {build['buildStatus']}")
    if build["buildStatus"] == "SUCCEEDED":
        return 0
    return int(code) if code.isdigit() and code != "0" else 1


def main(argv: list[str]) -> int:
    action = argv[1] if len(argv) > 1 else ""
    if action not in {"deploy", "gate", "destroy", "deploy-infra", "cleanup"}:
        sys.exit("usage: start_preview.py deploy|gate|destroy|deploy-infra|cleanup")
    job = _env("CI_JOB_ID", r"\d{1,12}")
    if action == "cleanup":
        codebuild, ssm = _clients(os.environ["M11_GITLAB_CI_ROLE_ARN"], f"m11-cleanup-{job}")
        prefix = "/m11/devops-companion/mr-"
        names = []
        for page in ssm.get_paginator("describe_parameters").paginate(
                ParameterFilters=[{"Key": "Name", "Option": "BeginsWith", "Values": [prefix]}]):
            names += [p["Name"] for p in page["Parameters"]]
        iids = sorted({m.group(1) for n in names if (m := re.match(r"^/m11/devops-companion/mr-(\d{1,9})/", n))})
        for iid in iids:  # the trusted build destroys only closed/merged/stopped MRs (PreviewRejected otherwise)
            _run(codebuild, "m11-preview", {"PREVIEW_ACTION": "destroy", "PREVIEW_IID": iid, "PREVIEW_SHA": "0" * 40})
        return 0
    iid = _env("CI_MERGE_REQUEST_IID", r"\d{1,9}")
    sha = _env("CI_COMMIT_SHA", r"[0-9a-f]{40}")
    if action == "deploy-infra":
        codebuild, _ = _clients(os.environ["M11_GITLAB_CI_INFRA_ROLE_ARN"], f"m11-mr-{iid}-infra-{job}")
        return _run(codebuild, "m11-preview-infra", {"PREVIEW_ACTION": "deploy", "PREVIEW_IID": iid,
                                                     "PREVIEW_SHA": sha, "PREVIEW_JOB_ID": job})
    codebuild, _ = _clients(os.environ["M11_GITLAB_CI_ROLE_ARN"], f"m11-mr-{iid}-{job}")
    return _run(codebuild, "m11-preview", {"PREVIEW_ACTION": action, "PREVIEW_IID": iid, "PREVIEW_SHA": sha})


if __name__ == "__main__":
    sys.exit(main(sys.argv))

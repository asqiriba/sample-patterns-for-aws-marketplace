# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Live reads for the drift check: what `release/drift.py: check()` compares with the
deployed manifest. Read-only. A deploy uses it for "drift is clean after a deploy"; the drift-checker
Lambda and `module11 drift check|revert` (later) build on the same reads."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from module11.release import drift, manifest as M, naming
from module11.release.hashing import sha256_bytes

from . import bundles, control_table, endpoints, ledger, params, releases


def observe(*, stage: str, current: dict, applied: dict | None, ctl: Any, lam: Any, iam: Any, bedrock: Any,
            s3: Any, account: str, region: str) -> dict:
    dep = current["deployment"][stage]
    runtime_id = dep["runtimeId"]
    observed: dict[str, Any] = {"endpoints": {}, "runtimeVersions": {}}
    for name in dep["endpoints"]:
        ep = endpoints.get(ctl, runtime_id, name)
        observed["endpoints"][name] = {k: ep.get(k) for k in ("status", "liveVersion", "targetVersion")}
    for version in sorted(set(map(str, dep["endpoints"].values()))):
        observed["runtimeVersions"][version] = ctl.get_agent_runtime(agentRuntimeId=runtime_id,
                                                                    agentRuntimeVersion=version)
    serving = observed["runtimeVersions"][str(dep["endpoints"].get("stable", dep["runtimeVersion"]))]
    config = json.loads((serving.get("environmentVariables") or {}).get(M.RUNTIME_CONFIG_ENV, "{}"))
    observed["runtimeToolArns"] = config.get("toolArns") or {}
    if config.get("bundleArn") and config.get("bundleVersion"):
        pinned = bundles.get_version(ctl, config["bundleArn"], config["bundleVersion"])
        component = bundles.component_of(pinned)
        observed["bundle"] = {"pinnedComponentSha256": sha256_bytes(M.component_bytes(component)),
                              "versions": [v["versionId"] for v in bundles.list_versions(ctl, config["bundleArn"])]}
    observed["tools"] = {}
    for tool, arn in (config.get("toolArns") or {}).items():
        name, _, version = arn.rpartition(":")
        info = lam.get_function_configuration(FunctionName=name, Qualifier=version)
        observed["tools"][tool] = {"codeSha256": info["CodeSha256"]}
    observed["policies"] = {}
    for key in (applied or {}).get("policyDigests") or {}:
        role, _, policy = key.partition("/")
        document = iam.get_role_policy(RoleName=role, PolicyName=policy)["PolicyDocument"]
        observed["policies"][key] = document if isinstance(document, (str, dict)) else json.dumps(document)
    guard = dep.get("guardrail") or {}
    if guard.get("id"):
        observed["guardrail"] = bedrock.get_guardrail(guardrailIdentifier=guard["id"], guardrailVersion=guard["version"])
    bucket = naming.artifacts_bucket(stage, account, region)
    flags = s3.get_object(Bucket=bucket, Key="config/flags.json")["Body"].read()
    from module11.release.normalize import normalize
    observed["flagsSha256"] = sha256_bytes(normalize("flags.json", flags))
    return observed


DRIFT_NAMESPACE = "m11/Drift"
MAX_REPORT_CHARS = 3900            # /drift is a Standard parameter (4 KB)


def check(*, stage: str, admin: Any, account: str, region: str, release_bucket: str) -> drift.DriftReport:
    ssm, s3 = admin.client("ssm"), admin.client("s3")
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    current = params.stage_get(ssm, stage, "current")
    if not current:
        raise RuntimeError(f"{stage} has no /current")
    applied = params.stage_get(ssm, stage, "applied")
    candidate = params.stage_get(ssm, stage, "candidate")
    control = control_table.get(admin.client("dynamodb"), naming.table(stage, "control"))
    pins = json.loads(releases.get(s3, current["deployment"][stage]["pinsUri"]))
    observed = observe(stage=stage, current=current, applied=applied, ctl=admin.client("bedrock-agentcore-control"),
                       lam=admin.client("lambda"), iam=admin.client("iam"), bedrock=admin.client("bedrock"), s3=s3,
                       account=account, region=region)
    entries = list(ledger.entries(s3, release_bucket, stage))
    return drift.check(stage, current, candidate, control, observed, entries, applied=applied, pins=pins,
                       checked_at=checked_at)


def report_parameter(report: drift.DriftReport) -> dict:
    """The `/drift` value: the report, with findings cut to fit a Standard parameter (counts kept)."""
    doc = report.to_dict()
    doc["driftCount"] = report.drift_count
    doc["findingCount"] = len(report.findings)
    while doc["findings"] and len(json.dumps(doc, sort_keys=True, separators=(",", ":"), default=str)) > MAX_REPORT_CHARS:
        doc["findings"] = doc["findings"][:-1]
        doc["truncated"] = True
    return doc


def publish(admin: Any, report: drift.DriftReport) -> None:
    """SSM `/m11/devops-companion/<stage>/drift` and `m11/Drift DriftFindings` ."""
    params.stage_put(admin.client("ssm"), report.stage, "drift", report_parameter(report))
    admin.client("cloudwatch").put_metric_data(Namespace=DRIFT_NAMESPACE, MetricData=[{
        "MetricName": "DriftFindings", "Value": float(report.drift_count), "Unit": "Count",
        "Dimensions": [{"Name": "Stage", "Value": report.stage}]}])


def report_from(payload: dict) -> drift.DriftReport:
    """A DriftReport from the Lambda's result."""
    doc = payload.get("report") or {}
    return drift.DriftReport(doc.get("stage", ""), doc.get("checkedAt", ""),
                             [drift.Finding(**f) for f in doc.get("findings") or []])


# ---------------------------------------------------------------- --external (warnings, never drift)
def builtin_fingerprint(ctl: Any) -> str:
    """sha256 of the sorted Builtin `{id, level}` list from ListEvaluators."""
    from module11.release.hashing import json_hash
    rows, token = [], None
    while True:
        page = ctl.list_evaluators(**({"nextToken": token} if token else {}))
        rows += [{"id": e["evaluatorId"], "level": e.get("level")} for e in page.get("evaluators", [])
                 if str(e.get("evaluatorId", "")).startswith("Builtin.")]
        token = page.get("nextToken")
        if not token:
            return json_hash(sorted(rows, key=lambda r: r["id"]))


def model_lifecycles(bedrock: Any, models: dict) -> dict[str, str]:
    """{modelId: lifecycle status} per models.yaml entry, from GetFoundationModel on its base model."""
    out: dict[str, str] = {}
    for spec in models.values():
        model_id = spec["modelId"]
        base = spec.get("foundationModelId") or model_id.split(".", 1)[-1]
        detail = bedrock.get_foundation_model(modelIdentifier=base)["modelDetails"]
        out[model_id] = str((detail.get("modelLifecycle") or {}).get("status", "UNKNOWN"))
    return out


def gateway_target(ctl: Any, gateway_name: str, target_name: str) -> dict | None:
    """GetGatewayTarget for a gateway and target found by name (None when either is missing)."""
    gateway_id, token = None, None
    while gateway_id is None:
        page = ctl.list_gateways(**({"nextToken": token} if token else {}))
        gateway_id = next((g["gatewayId"] for g in page.get("items", []) if g.get("name") == gateway_name), None)
        token = page.get("nextToken")
        if not token:
            break
    if gateway_id is None:
        return None
    token = None
    while True:
        page = ctl.list_gateway_targets(gatewayIdentifier=gateway_id, **({"nextToken": token} if token else {}))
        for t in page.get("items", []):
            if t.get("name") == target_name:
                return ctl.get_gateway_target(gatewayIdentifier=gateway_id, targetId=t["targetId"])
        token = page.get("nextToken")
        if not token:
            return None


def surface_hashes(ctl: Any, surfaces: list[dict]) -> dict[str, str]:
    """{surface id: live hash} for each `external_surfaces.yaml` gateway target that exists."""
    from module11.release import external
    out = {}
    for s in surfaces:
        if s.get("kind") != "gateway-target":
            continue
        response = gateway_target(ctl, s["gateway"], s["target"])
        if response is not None:
            out[s["id"]] = external.surface_hash(response)
    return out


def external(admin: Any, *, models: dict, evaluators: dict, surfaces: list[dict]) -> dict:
    """The `--external` probes as one record: lifecycles, the evaluator fingerprint, surface hashes, warnings."""
    ctl = admin.client("bedrock-agentcore-control")
    lifecycles = model_lifecycles(admin.client("bedrock"), models)
    fingerprint = builtin_fingerprint(ctl)
    hashes = surface_hashes(ctl, surfaces)
    warnings = drift.external_warnings(lifecycles, (evaluators.get("builtinFingerprint"), fingerprint),
                                       [(s["id"], s.get("sha256"), hashes[s["id"]]) for s in surfaces if s["id"] in hashes])
    return {"models": lifecycles, "builtinFingerprint": {"pinned": evaluators.get("builtinFingerprint"),
                                                         "observed": fingerprint},
            "surfaces": [{"id": s["id"], "pinned": s.get("sha256"), "observed": hashes.get(s["id"]),
                          "stage": s.get("stage")} for s in surfaces],
            "warnings": warnings}


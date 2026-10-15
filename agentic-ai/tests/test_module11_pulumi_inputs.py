# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline tests for module11/release/pulumi_inputs.py, pure cases.
The Pulumi-mock cases are in test_module11_pulumi_mocks.py."""
import io
import json
import shutil
import tarfile
from pathlib import Path

import pytest

from module11.release import pulumi_inputs as PI
from module11.release.hashing import sha256_bytes
from module11.release.manifest import ReleaseInputsCorrupt

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/module11/harness_min"
SHA = "sha256:" + "a" * 64


def inputs(**over):
    doc = {"imageDigest": SHA, "bundleArn": "arn:b", "bundleVersion": "v-1",
           "bundleComponent": {"uri": "s3://b/releases/x/bundle-component.json", "sha256": SHA},
           "handlers": {"search_runbooks": {"s3Key": "handlers/search_runbooks/x.zip", "codeSha256": "abc="}},
           "layer": {"s3Key": "layers/m11-sdk/x.zip", "sha256": SHA}, "edge": {"s3Key": "handlers/edge/x.zip", "sha256": SHA},
           "evaluatorCode": {"s3Key": "handlers/eval-no-credential-leakage/x.zip", "codeSha256": "def="},
           "operationsCode": {"layer": {"s3Key": "layers/m11-ops/x.zip", "sha256": SHA},
                              "controller": {"s3Key": "handlers/ops-controller/x.zip", "codeSha256": "c="},
                              "shadow_worker": {"s3Key": "handlers/ops-shadow-worker/x.zip", "codeSha256": "w="}},
           "flagsSha256": SHA, "snapshot": {"id": "pol-1", "uri": "s3://b/snapshots/policy/pol-1.json", "sha256": SHA},
           "evidence": {"uri": "s3://b/evidence/aaaa/", "sha256": SHA},
           "pulumiInputs": {"uri": "s3://b/releases/x/pulumi-inputs.tar", "sha256": SHA},
           "retainedPins": {"toolArns": [], "modelResources": [], "deadlineSeconds": 150, "toolStatements": {}},
           "runtimeDigest": SHA, "manifestVersion": "2026-10-20T14:02:11Z/a7f3c91", "behaviorDigest": SHA}
    doc.update(over)
    return doc


def test_archive_is_byte_identical_across_builds_and_checkouts(tmp_path):
    first = PI.build_archive(FIXTURE)
    assert PI.build_archive(FIXTURE) == first
    copy = tmp_path / "copy"
    shutil.copytree(FIXTURE, copy)
    models = copy / "module11/harness/agent/models.yaml"
    models.write_bytes(models.read_bytes().replace(b"\n", b"\r\n"))  # a CRLF checkout seals the same bytes
    assert PI.build_archive(copy) == first
    members = tarfile.open(fileobj=io.BytesIO(first)).getmembers()
    assert [m.name for m in members] == sorted(m.name for m in members)
    assert {(m.mtime, m.uid, m.gid, m.mode) for m in members} == {(0, 0, 0, 0o644)}
    assert {"models.yaml", "guardrail.yaml", "orchestration.yaml", "tools/search_runbooks/tool.json",
            "tools/search_runbooks/permissions.json"} <= {m.name for m in members}


def test_read_archive_verifies_sha():
    data = PI.build_archive(FIXTURE)
    docs = PI.read_archive(data, sha256_bytes(data))
    assert docs["orchestration.yaml"]["deadlineSeconds"] == 150
    with pytest.raises(ReleaseInputsCorrupt):
        PI.read_archive(data, SHA)


def test_release_inputs_round_trip_and_validation():
    doc = inputs(future={"k": 1})
    ri = PI.ReleaseInputs.from_dict(doc)
    assert PI.ReleaseInputs.from_dict(json.loads(ri.to_json())) == ri
    assert ri.to_dict() == doc
    with pytest.raises(ReleaseInputsCorrupt):
        PI.ReleaseInputs.from_dict(inputs(bundleVersion=None))
    PI.ReleaseInputs.from_dict(inputs(bundleArn=None, bundleVersion=None))  # bootstrap pair
    bad = inputs()
    del bad["runtimeDigest"]
    with pytest.raises(ReleaseInputsCorrupt):
        PI.ReleaseInputs.from_dict(bad)


def test_load_order_env_then_applied_then_missing():
    data = json.dumps(inputs()).encode("utf-8")
    from_env = PI.load("prod", {PI.ENV_VAR: "/tmp/ri.json"}, lambda p: data, lambda s: pytest.fail("no SSM"),
                       lambda u: pytest.fail("no S3"))
    applied = {"releaseInputsUri": "s3://b/release-inputs/prod/x.json", "sha256": sha256_bytes(data)}
    from_applied = PI.load("prod", {}, lambda p: pytest.fail("no file"), lambda s: applied, lambda u: data)
    assert from_env == from_applied  # a preview with no explicit inputs yields the applied record
    with pytest.raises(ReleaseInputsCorrupt):
        PI.load("prod", {}, None, lambda s: {**applied, "sha256": SHA}, lambda u: data)
    with pytest.raises(PI.ReleaseInputsMissing, match="release deploy --stage prod"):
        PI.load("prod", {}, None, lambda s: None, None)


# ---------------------------------------------------------------- Pulumi-mock cases
def _mocks():
    return pytest.importorskip("test_module11_pulumi_mocks")


def _runtime(mocks):
    [(name, inputs_)] = mocks.of("AgentcoreAgentRuntime")
    return inputs_


def _runtime_config(mocks):
    return json.loads(_runtime(mocks)["environmentVariables"]["M11_RUNTIME_CONFIG"])


def test_program_reads_only_sealed_inputs(monkeypatch):
    """M7: a preview from a working copy with edited permissions.json, guardrail.yaml, tool.json timeout, edge
    .mjs or evaluator handler still produces the /applied resources, because the program reads none of them."""
    m = _mocks()
    opened = []
    real_read_bytes, real_read_text, real_open = Path.read_bytes, Path.read_text, open
    watched = ("module11/harness/", "module11/edge/", "module10/deployment/lambda/", "evaluators/")

    def spy(path):
        rel = str(path)
        if any(w in rel for w in watched):
            opened.append(rel)
    monkeypatch.setattr(Path, "read_bytes", lambda self: (spy(self), real_read_bytes(self))[1])
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: (spy(self), real_read_text(self, *a, **k))[1])
    monkeypatch.setattr("builtins.open", lambda file, *a, **k: (spy(file), real_open(file, *a, **k))[1])
    first, mocks_a = m.execute()
    monkeypatch.undo()
    assert opened == []
    second, mocks_b = m.execute()
    assert sorted((t, n, json.dumps(i, sort_keys=True, default=str)) for t, n, i in mocks_a.resources) == \
        sorted((t, n, json.dumps(i, sort_keys=True, default=str)) for t, n, i in mocks_b.resources)


def test_runtime_role_invoke_list_is_retained_plus_own_versions():
    """P9: lambda:InvokeFunction = retainedPins.toolArns ∪ this stack's qualified tool ARNs."""
    m = _mocks()
    out, mocks = m.execute("mr-7")
    [doc] = [m.policy(i) for n, i in mocks.of("RolePolicy") if i["role"] == "m11-mr-7-runtime" and i["name"] == "runtime"]
    invoke = next(s for s in doc["Statement"] if s["Sid"] == "InvokeTools")["Resource"]
    own = {m.tool_arn("mr-7", t, 1) for t in m.TOOLS}
    assert set(invoke) == own | {m.tool_arn("mr-7", "search_runbooks", 4)}
    assert out["effectivePins"]["ownToolArns"] == {t: m.tool_arn("mr-7", t, 1) for t in m.TOOLS}
    assert _runtime_config(mocks)["toolArns"] == out["effectivePins"]["ownToolArns"]   # own == the env's toolArns


@pytest.mark.parametrize("stage", ["mr-7", "dev"])
def test_runtime_role_may_create_only_its_own_log_groups(stage):
    """AgentCore sets up the runtime's log groups (and their otel-rt-logs/spans streams) with the runtime
    role while the runtime is created; without logs:CreateLogGroup it creates no streams. The grant is in the
    policy the runtime depends on and names only this runtime's groups, by its name prefix."""
    m = _mocks()
    _, mocks = m.execute(stage)
    role = f"m11-{stage}-runtime"
    docs = {i["name"]: m.policy(i) for n, i in mocks.of("RolePolicy") if i["role"] == role}
    grants = [(name, s) for name, doc in docs.items() for s in doc["Statement"] if "logs:CreateLogGroup" in str(s["Action"])]
    assert [(name, s["Action"], s["Resource"]) for name, s in grants] == [
        ("runtime", "logs:CreateLogGroup",
         f"arn:aws:logs:{m.R}:{m.A}:log-group:/aws/bedrock-agentcore/runtimes/m11_{stage.replace('-', '')}_companion-*")]


def test_bundle_arn_comes_from_release_inputs_and_mismatch_fails():
    m = _mocks()
    doc = m.inputs("mr-7")
    out, mocks = m.execute("mr-7", doc)
    assert _runtime_config(mocks)["bundleArn"] == doc["bundleArn"] == out["bundleArn"]
    [(_, bundle)] = mocks.of("ConfigurationBundle")
    assert list(bundle["components"].values()) == [{"configuration": {"schema": "m11.bundle/2", "bootstrap": True}}]
    assert list(bundle["components"]) == [out["runtimeArn"]]          # keyed by the runtime ARN (placeholder)
    from module11.infrastructure.harness.config_model import BundleArnMismatch
    with pytest.raises(BundleArnMismatch):
        m.execute("mr-7", doc, bundle_arn="arn:aws:bedrock-agentcore:us-east-1:111122223333:configuration-bundle/other")


def test_bootstrap_runs_with_a_null_bundle_and_no_bundle_grant():
    m = _mocks()
    out, mocks = m.execute("mr-7", m.inputs("mr-7", bundleArn=None, bundleVersion=None))
    assert out["bootstrap"] is True and _runtime_config(mocks)["bundleVersion"] is None
    [doc] = [m.policy(i) for n, i in mocks.of("RolePolicy") if i["role"] == "m11-mr-7-runtime" and i["name"] == "runtime"]
    assert "Bundle" not in {s["Sid"] for s in doc["Statement"]}
    from module11.infrastructure.harness.config_model import BootstrapNotAllowed
    with pytest.raises(BootstrapNotAllowed):
        m.execute("mr-7", m.inputs("mr-7", bundleArn=None, bundleVersion=None), applied={"releaseInputsUri": "s3://x"})


def test_mr_roles_within_boundary():
    """Every statement of every mr-* role is covered by m11-mr-boundary (iam_cover), every role carries the
    boundary (P8), and an mr-* stack has no controller, schedule or drift checker."""
    m = _mocks()
    from module11.infrastructure.platform import roles as platform_roles
    from module11.release import iam_cover
    _, mocks = m.execute("mr-7")
    ctx = platform_roles.Ctx(m.A, m.R, "", "", m.SHARED["userPoolId"], m.SHARED["stateTable"])
    boundary = platform_roles.mr_boundary(ctx, [m.SHARED["imageRepositoryArn"], m.SHARED["previewImageRepositoryArn"]])
    roles = mocks.of("Role")
    assert roles and all(i["permissionsBoundary"] == m.SHARED["boundaryArn"] for _, i in roles)
    uncovered = []
    for name, i in mocks.of("RolePolicy"):
        for s in m.policy(i)["Statement"]:
            if s.get("Effect", "Allow") == "Allow":
                uncovered += [(i["role"], pair) for pair in iam_cover.covers(boundary, s)]
    assert uncovered == []
    names = [i.get("name", "") for _, i in mocks.of("Function")]
    assert not [n for n in names if "controller" in n or "drift" in n]
    assert not [t for t, _, _ in mocks.resources if t.startswith("aws:scheduler")]


def test_every_resource_passes_the_pack(monkeypatch):
    """P1-P8 on every registered resource of an mr-* stack and of dev, staging and prod (iam_cover position 6
    included; staging has the A/B Gateway roles, which a dev-only run missed)."""
    from module11.infrastructure.harness import config_model as CM
    monkeypatch.setattr(CM, "apply_guard", lambda *a, **k: None)
    m = _mocks()
    from module11.infrastructure.policy import pack
    for stage in ("mr-7", "dev", "staging", "prod"):
        _, mocks = m.execute(stage)
        violations = [(t, n, v) for t, n, i in mocks.resources
                      for v in pack.check_resource(t, i, operators=["arn:aws:iam::111122223333:user/op"])]
        assert violations == [], violations[:5]
        retained = m.inputs(stage)["retainedPins"]["toolArns"]
        assert pack.check_stack([(t, o) for t, n, o in mocks.outputs], retained) == []


def test_memory_runtime_and_guardrail_version_shapes(monkeypatch):
    m = _mocks()
    from module11.infrastructure.harness import guardrail as G
    captured = {}
    real = G.aws.bedrock.GuardrailVersion

    def capture(name, **kwargs):
        captured.update(kwargs)
        return real(name, **kwargs)
    monkeypatch.setattr(G.aws.bedrock, "GuardrailVersion", capture)
    _, mocks = m.execute("mr-7")
    [(_, memory)] = mocks.of("AgentcoreMemory")
    assert memory["indexedKeys"] == [{"key": "service", "type": "STRING"}, {"key": "variant", "type": "STRING"},
                                     {"key": "release", "type": "STRING"}]
    assert _runtime(mocks)["lifecycleConfigurations"] == [{"idleRuntimeSessionTimeout": 60, "maxLifetime": 900}]
    assert captured["skip_destroy"] is True and captured["description"].startswith("m11 ")
    assert captured["opts"].replace_on_changes == ["description"]


@pytest.mark.parametrize("kind", ["flags", "edge", "permissions", "pipeline"])
def test_non_runtime_releases_leave_the_runtime_unchanged(kind):
    """Flags-only, edge-only, permissions-only and pipeline-only releases leave the
    AgentcoreAgentRuntime inputs unchanged, so they create no runtime version."""
    m = _mocks()
    base = m.inputs("mr-7")
    changed = json.loads(json.dumps(base))
    if kind == "flags":
        changed["flagsSha256"] = "sha256:" + "1" * 64
    elif kind == "edge":
        changed["edge"] = {"s3Key": "handlers/edge/other.zip", "sha256": "sha256:" + "2" * 64}
    elif kind == "permissions":
        changed["retainedPins"]["toolStatements"]["query_metrics"] = [
            {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": ["arn:aws:s3:::m11-release-111122223333-us-east-1/evidence/*"]}]
    else:
        changed.update(manifestVersion="2026-10-07T10:00:00Z/bcdef12", behaviorDigest="sha256:" + "3" * 64)
    _, a = m.execute("mr-7", base)
    _, b = m.execute("mr-7", changed)
    assert _runtime(a) == _runtime(b)


def test_runtime_config_has_runtime_digest_and_no_release_identity():
    m = _mocks()
    _, mocks = m.execute("mr-7")
    config = _runtime_config(mocks)
    assert config["runtimeDigest"] == m.inputs()["runtimeDigest"]
    assert "manifestVersion" not in config and "behaviorDigest" not in config


@pytest.mark.parametrize("stage", ["dev", "mr-7"])
def test_code_based_evaluator_is_published_pinned_and_invoked_with_the_callers_credentials(stage):
    """The evaluator Lambda is published (P2) from the sealed evaluatorCode zip; the evaluator names its
    qualified ARN; `Evaluate` calls the Lambda with the caller's credentials (settled live), so the eval runner
    may Evaluate with the evaluator and invoke that published version, and no service-principal permission exists;
    `evaluatorIds` maps the logical ID."""
    m = _mocks()
    outputs, mocks = m.execute(stage)
    name = f"m11-{stage}-eval-no-credential-leakage"
    function = dict(mocks.of("Function"))["eval-leak"]
    assert function["name"] == name and function["publish"] is True and function["handler"] == "handler.handler"
    assert function["s3Key"] == "handlers/eval/x.zip" and function["runtime"] == "python3.13"
    assert "eval-leak-invoke" not in dict(mocks.of("Permission"))
    evaluator = dict(mocks.of("AgentcoreEvaluator"))["eval-leak-evaluator"]
    config = evaluator["evaluatorConfig"]["codeBased"]["lambdaConfig"]
    key = stage.replace("-", "")
    assert evaluator["evaluatorName"] == f"m11_{key}_eval_no_credential_leakage" and evaluator["level"] == "SESSION"
    assert config["lambdaArn"] == f"arn:aws:lambda:{m.R}:{m.A}:function:{name}:1" and config["lambdaTimeoutInSeconds"] == 30
    logs = dict(mocks.of("LogGroup"))["eval-leak-logs"]
    assert logs["name"] == f"/aws/lambda/{name}" and logs["retentionInDays"] == 30
    evaluator_id = f"m11_{key}_eval_no_credential_leakage-MOCK123456"
    assert outputs["evaluatorIds"] == {"custom.no_credential_leakage": evaluator_id}
    runner = [m.policy(i) for n, i in mocks.of("RolePolicy") if i["role"] == f"m11-{stage}-eval-runner"]
    statements = [s for doc in runner for s in doc["Statement"]]
    evaluate = next(s for s in statements if s["Sid"] == "Evaluate")
    assert evaluate["Resource"] == [f"arn:aws:bedrock-agentcore:{m.R}:{m.A}:evaluator/Builtin.*",
                                    "arn:aws:bedrock-agentcore:::evaluator/Builtin.*",
                                    f"arn:aws:bedrock-agentcore:{m.R}:{m.A}:evaluator/{evaluator_id}"]
    grants = [s for s in statements if "lambda:InvokeFunction" in json.dumps(s.get("Action"))]
    assert [(g["Sid"], g["Resource"]) for g in grants] == [
        ("InvokeEvaluator", [f"arn:aws:lambda:{m.R}:{m.A}:function:{name}:1"])]
    invoke = next(s for s in statements if s["Sid"] == "Invoke")
    assert all("evaluator/" not in r for r in invoke["Resource"])


def test_the_evaluator_can_be_switched_off():
    m = _mocks()
    outputs, mocks = m.execute("mr-7", config={"enableEvaluator": "false"})
    assert not mocks.of("AgentcoreEvaluator") and outputs["evaluatorIds"] == {}


@pytest.mark.parametrize("stage", ["dev", "staging", "prod", "mr-7"])
def test_operations_resources_per_stage(stage, monkeypatch):
    """The controller (reserved concurrency 1, 50 s, published, sdk + ops layers), its
    async config (60 s, 0 retries), a DISABLED every-minute schedule whose state Pulumi ignores, three alarms, and
    on dev/prod the shadow queue and worker; mr-* stacks create none of them."""
    from module11.infrastructure.harness import config_model as CM
    monkeypatch.setattr(CM, "apply_guard", lambda *a, **k: None)     # staging/prod need a deployer session
    m = _mocks()
    outs, mocks = m.execute(stage)
    functions = {i["name"]: i for _, i in mocks.of("Function")}
    schedules = [i for _, i in mocks.of("Schedule")]
    if stage.startswith("mr-"):
        assert f"m11-{stage}-controller" not in functions and not schedules and not mocks.of("Queue")
        assert outs["controllerFunction"] is None
        return
    fn = functions[f"m11-{stage}-controller"]
    assert fn["reservedConcurrentExecutions"] == 1 and fn["timeout"] == 50 and fn["publish"] is True
    assert fn["handler"] == "module11.lambdas.controller.handler.handler" and len(fn["layers"]) == 2
    assert fn["s3Key"] == "handlers/ops-controller/x.zip"
    assert {k for k in fn["environment"]["variables"]} >= {"CONTROL_TABLE", "OUTCOMES_TABLE", "RUNTIME_ID",
                                                            "SCHEDULE_NAME", "CURRENT_PARAM", "CANDIDATE_PARAM"}
    configs = {n: i for n, i in mocks.of("FunctionEventInvokeConfig")}
    config = configs["controller-async"]
    assert config["maximumEventAgeInSeconds"] == 60 and config["maximumRetryAttempts"] == 0
    schedule = next(i for i in schedules if i["name"] == f"m11-{stage}-controller")
    assert schedule["state"] == "DISABLED" and schedule["scheduleExpression"] == "cron(* * * * ? *)"
    assert schedule["target"]["retryPolicy"] == {"maximumEventAgeInSeconds": 60, "maximumRetryAttempts": 0}
    assert json.loads(schedule["target"]["input"]) == {"command": "tick"}
    # The drift checker, ENABLED every 15 minutes, read-only except /drift and its metric.
    drift = functions[f"m11-{stage}-drift-check"]
    assert drift["handler"] == "module11.lambdas.drift_check.handler.handler" and drift["publish"] is True
    assert drift["s3Key"] == "handlers/ops-drift-check/x.zip" and configs["drift-check-async"]["maximumRetryAttempts"] == 0
    dsched = next(i for i in schedules if i["name"] == f"m11-{stage}-drift-check")
    assert dsched["state"] == "ENABLED" and dsched["scheduleExpression"] == "rate(15 minutes)"
    dpolicy = json.loads(next(i for n, i in mocks.of("RolePolicy") if i["role"] == f"m11-{stage}-drift-check")["policy"])
    writes = {a for s in dpolicy["Statement"] for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])
              if not a.split(":")[1].startswith(("Get", "List"))}
    assert writes == {"ssm:PutParameter", "cloudwatch:PutMetricData", "logs:CreateLogStream", "logs:PutLogEvents"}
    put = next(s for s in dpolicy["Statement"] if s["Action"] == "ssm:PutParameter")
    assert put["Resource"].endswith(f"parameter/m11/devops-companion/{stage}/drift")
    alarms = sorted(i["name"] for _, i in mocks.of("MetricAlarm"))
    assert alarms == [f"m11-{stage}-{a}" for a in ("drift", "ledger-write-failed", "rollback-stuck", "rollout-stalled")]
    assert outs["driftFunction"] == f"m11-{stage}-drift-check"
    role = next(i for n, i in mocks.of("RolePolicy") if i["role"] == f"m11-{stage}-controller")
    sids = {s.get("Sid") for s in json.loads(role["policy"])["Statement"]}
    assert {"ControlItem", "Outcomes", "Endpoints", "Schedule", "PassSchedulerRole", "Latches", "Ledger"} <= sids
    shadow = stage in ("dev", "prod")
    assert bool(mocks.of("Queue")) == shadow and (f"m11-{stage}-shadow-worker" in functions) == shadow
    adapter = functions[f"m11-{stage}-adapter"]["environment"]["variables"]
    assert ("SHADOW_QUEUE_URL" in adapter) == shadow
    if shadow:
        assert functions[f"m11-{stage}-shadow-worker"]["reservedConcurrentExecutions"] == 5
        (esm,) = [i for _, i in mocks.of("EventSourceMapping")]
        assert esm["functionResponseTypes"] == ["ReportBatchItemFailures"]
    assert outs["controllerFunction"] == f"m11-{stage}-controller"


def test_a_release_sealed_before_the_controller_is_refused_on_dev():
    m = _mocks()
    doc = m.inputs("dev")
    doc.pop("operationsCode")
    with pytest.raises(Exception, match="operationsCode"):
        m.execute("dev", doc)


@pytest.mark.parametrize("stage", ["dev", "staging", "prod", "mr-7"])
def test_online_eval_configs_per_stage(stage, monkeypatch):
    """Online evaluation: the candidate-weighted pair on rollout stages, one config on staging excluding eval
    and shadow sessions, none on mr-*; the execution role writes only the stage's own result groups."""
    from module11.infrastructure.harness import config_model as CM
    monkeypatch.setattr(CM, "apply_guard", lambda *a, **k: None)
    m = _mocks()
    outs, mocks = m.execute(stage)
    configs = {i["onlineEvaluationConfigName"]: i for _, i in mocks.of("AgentcoreOnlineEvaluationConfig")}
    if stage.startswith("mr-"):
        assert configs == {} and outs["onlineEvalConfigs"] == {}
        return
    key = stage
    if stage == "staging":
        (cfg,) = configs.values()
        assert [(f["key"], f["operator"], f["value"]["stringValue"]) for f in cfg["rule"]["filters"]] == [
            ("attributes.m11.variant", "NotEquals", "eval"), ("attributes.m11.variant", "NotEquals", "shadow")]
        assert cfg["rule"]["samplingConfig"]["samplingPercentage"] == 100.0
        assert cfg["dataSourceConfig"]["cloudwatchLogs"]["serviceNames"] == ["m11_staging_companion.staging"]
    else:
        cand, stable = configs[f"m11_{key}_oe_candidate"], configs[f"m11_{key}_oe_stable"]
        assert cand["rule"]["filters"][0]["value"]["stringValue"] == "candidate"
        assert cand["rule"]["samplingConfig"]["samplingPercentage"] == 100.0
        assert stable["rule"]["filters"][0]["value"]["stringValue"] == "stable"
        assert stable["rule"]["samplingConfig"]["samplingPercentage"] == (100.0 if stage == "prod" else 10.0)
        assert cand["dataSourceConfig"]["cloudwatchLogs"]["logGroupNames"][0].endswith("-candidate")
    assert all([e["evaluatorId"] for e in c["evaluators"]] == ["Builtin.GoalSuccessRate", "Builtin.Correctness"]
               for c in configs.values())
    policy = json.loads(next(i for n, i in mocks.of("RolePolicy") if i["role"] == f"m11-{stage}-online-eval")["policy"])
    results = next(s for s in policy["Statement"] if s["Sid"] == "Results")
    assert results["Resource"].endswith(f"/aws/bedrock-agentcore/evaluations/results/m11_{key}_oe*")


@pytest.mark.parametrize("stage", ["staging", "prod"])
def test_the_ab_gateway_exists_in_staging_only(stage, monkeypatch):
    """Row 19: Gateway m11-staging-ab (AWS_IAM, no protocol type), an HTTP runtime target on the staging endpoint,
    the abtest-driver role (InvokeGateway only, operator trust), the A/B execution role, and the Gateway's role
    admitted by the runtime's resource policy. The target's hash is the one external_surfaces.yaml pins."""
    from module11.infrastructure.harness import config_model as CM
    from module11.release import external, stage as cfg
    monkeypatch.setattr(CM, "apply_guard", lambda *a, **k: None)
    m = _mocks()
    outs, mocks = m.execute(stage)
    gateways = [i for _, i in mocks.of("AgentcoreGateway")]
    if stage != "staging":
        assert gateways == [] and outs["gateway"] is None
        return
    (gw,) = gateways
    assert gw["name"] == "m11-staging-ab" and gw["authorizerType"] == "AWS_IAM" and "protocolType" not in gw
    (target,) = [i for _, i in mocks.of("AgentcoreGatewayTarget")]
    assert target["name"] == "companion" and target["targetConfiguration"]["http"]["agentcoreRuntime"]["qualifier"] == "staging"
    response = {"targetConfiguration": {"http": {"agentcoreRuntime": dict(target["targetConfiguration"]["http"]["agentcoreRuntime"])}}}
    (surface,) = cfg.external_surfaces(ROOT)
    assert external.surface_hash(response) == surface["sha256"]
    driver = json.loads(next(i for n, i in mocks.of("RolePolicy") if i["role"] == "m11-staging-abtest-driver")["policy"])
    assert [s["Action"] for s in driver["Statement"]] == ["bedrock-agentcore:InvokeGateway"]
    admitted = [json.loads(i["policy"]) for _, i in mocks.of("AgentcoreResourcePolicy")]
    assert all(any("m11-staging-ab-gateway" in p for p in s["Principal"]["AWS"])
               for doc in admitted for s in doc["Statement"] if s["Sid"] == "Admitted")
    assert set(outs["gateway"]) >= {"gatewayArn", "gatewayUrl", "abtestDriverRole", "abtestRole"}

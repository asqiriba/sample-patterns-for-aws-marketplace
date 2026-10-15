# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Experiment and corpus operator commands (registered by cli.py):

  abtest run --stage staging [--recommendation ID | --prompt-file F] [--sessions 200]   the Gateway A/B test
  abtest show [ID] | abtest stop ID
  recommendation run [--hours 24] | recommendation show [ID] | recommendation import ID
  model compare --candidate <inference profile> [--stage dev] | model compare show [ID]
  scenario add --from-session <runtime session id> --category regressions [--stage prod]

Every record is written to the release bucket (`evaluations/<kind>/<id>.json`) and `module11/.local/<kind>/`, and
every resource the service creates outside Pulumi (bundle versions, the A/B test, the Gateway tracing delivery, the
recommendation) gets a release-ledger entry, so teardown and the drift checker know it. The commands run as the
operator; the A/B traffic runs as `m11-staging-abtest-driver`, the only identity allowed to invoke the Gateway.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .release import naming


def _say(text: str = "", style: str | None = None) -> None:
    from .cli import say
    say(text, style)


def _ctx(root: Path):
    from .cli_live import _context
    return _context(root)


def _code(exc: Exception) -> str:
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))


def _local(root: Path, kind: str) -> Path:
    path = root / "module11/.local" / kind
    path.mkdir(parents=True, exist_ok=True)
    return path


def _save(ctx, root: Path, kind: str, doc: dict) -> None:
    from .aws import releases
    (_local(root, kind) / f"{doc['id']}.json").write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
    releases.put_json(ctx.admin.client("s3"), ctx.release_bucket, f"evaluations/{kind}/{doc['id']}.json",
                      json.loads(json.dumps(doc, default=str)))


def _load(root: Path, kind: str, key: str | None) -> dict | None:
    folder = _local(root, kind)
    if key:
        path = folder / f"{key}.json"
        return json.loads(path.read_text()) if path.is_file() else None
    docs = [json.loads(p.read_text()) for p in folder.glob("*.json")]
    return max(docs, key=lambda d: d.get("createdAt", ""), default=None)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ledger(ctx, stage: str, kind: str, **fields) -> None:
    from .aws import ledger
    caller = ctx.admin.client("sts").get_caller_identity()["Arn"]
    ledger.put(ctx.admin.client("s3"), ctx.release_bucket, stage, kind, by=caller, **fields)


def _gitlab(ctx):
    from .aws import gitlab_api as G
    return G.from_secret(ctx.admin.client("secretsmanager"),
                         os.environ.get("M11_GITLAB_TOKEN_SECRET", "devops-companion/partners/gitlab-token"),
                         os.environ.get("M11_GITLAB_PROJECT_ID", ""))


def _demo_labels(base: list[str]) -> list[str]:
    return base + (["m11-demo-run"] if os.environ.get("M11_DEMO_RUN") else [])


def _by_name(pages, key: str, name: str):
    for item in pages:
        if item.get(key) == name:
            return item
    return None


def _gateway(ctl, name: str) -> dict | None:
    token = None
    while True:
        page = ctl.list_gateways(**({"nextToken": token} if token else {}))
        hit = _by_name(page.get("items", []), "name", name)
        if hit:
            return ctl.get_gateway(gatewayIdentifier=hit["gatewayId"])
        token = page.get("nextToken")
        if not token:
            return None


def _online_eval(ctl, name: str) -> dict | None:
    token = None
    while True:
        page = ctl.list_online_evaluation_configs(**({"nextToken": token} if token else {}))
        hit = _by_name(page.get("onlineEvaluationConfigs", []), "onlineEvaluationConfigName", name)
        if hit:
            return hit
        token = page.get("nextToken")
        if not token:
            return None


# ---------------------------------------------------------------- abtest
def cmd_abtest_run(args) -> int:
    from .aws import bundles, params
    from .demo.traffic import FIXTURES
    from .evaluation import abtest as AB
    from .infrastructure.harness.evaluation import online_eval_specs
    from .infrastructure.harness.gateway import GATEWAY_NAME, TARGET_NAME
    if args.stage != "staging":
        raise ValueError("the A/B Gateway exists in staging only")
    ctx = _ctx(args.root)
    ctl = ctx.admin.client("bedrock-agentcore-control")
    current = params.stage_get(ctx.admin.client("ssm"), "staging", "current")
    if not current:
        raise RuntimeError("staging has no /current")
    dep = current["deployment"]["staging"]
    bundle = dep["bundle"]
    gateway = _gateway(ctl, GATEWAY_NAME)
    if gateway is None:
        raise RuntimeError(f"Gateway {GATEWAY_NAME} does not exist: deploy the current harness to staging first")
    oe_name = online_eval_specs("staging", 100)[0]["name"]
    oe = _online_eval(ctl, oe_name)
    if oe is None:
        raise RuntimeError(f"online-evaluation config {oe_name} does not exist")
    # 1. Treatment bundle version (branch `abtest`, ledgered).
    if args.recommendation:
        rec = _load(args.root, "recommendations", args.recommendation)
        if not rec or not rec.get("result", {}).get("prompt"):
            raise RuntimeError(f"recommendation {args.recommendation} has no recommended prompt yet "
                               "(`module11 recommendation show` refreshes it)")
        prompt, source = rec["result"]["prompt"], f"recommendation {args.recommendation}"
    elif args.prompt_file:
        prompt, source = Path(args.prompt_file).read_text(encoding="utf-8"), f"prompt file {args.prompt_file}"
    else:
        raise ValueError("--recommendation <id> or --prompt-file <file> names the treatment prompt")
    control = bundles.component_of(bundles.get_version(ctl, bundle["arn"], bundle["versionId"]))
    treatment = AB.treatment_component(control, prompt)
    caller = ctx.admin.client("sts").get_caller_identity()["Arn"]
    name = AB.new_name()
    branch_head = _abtest_branch_head(ctl, bundle["arn"]) or bundle["versionId"]
    treatment_version = bundles.create_version(
        ctl, bundle["arn"], dep["runtimeArn"], treatment, parent=branch_head, branch=AB.BRANCH,
        message=f"A/B treatment for {name}: companion prompt from {source}"[:1000],
        created_by={"name": "m11-abtest", "arn": caller})
    _ledger(ctx, "staging", "bundle-version", versionId=treatment_version, bundleArn=bundle["arn"], purpose="abtest",
            abTest=name, componentSha256=AB.component_sha256(treatment))
    _say(f"treatment bundle version {treatment_version} on branch {AB.BRANCH} ({source})")
    # 2. Gateway tracing (variant attribution), once.
    _ensure_tracing(ctx, gateway)
    # 3. The A/B test (Preview).
    dp = ctx.admin.client("bedrock-agentcore")
    role_arn = ctx.role_arn(naming.role("staging", "abtest"))
    try:
        created = dp.create_ab_test(
            name=name, description=f"m11 staging config-bundle A/B: current companion prompt vs {source}",
            gatewayArn=gateway["gatewayArn"], roleArn=role_arn,
            evaluationConfig={"onlineEvaluationConfigArn": oe["onlineEvaluationConfigArn"]},
            variants=AB.variants(bundle["arn"], bundle["versionId"], treatment_version),
            enableOnCreate=True, clientToken=str(uuid.uuid4()), tags={"Project": naming.PROJECT_TAG, "Stage": "staging"})
    except Exception as exc:
        if _code(exc) in ("ValidationException", "AccessDeniedException", "ServiceQuotaExceededException",
                          "ConflictException", "UnknownOperationException"):
            _say(f"A/B test refused ({_code(exc)}: {str(exc)[:200]}). Fallback (Preview): a target-based A/B with "
                 "two staging endpoints, or `module11 gate --scope full` against each bundle version", "yellow")
            return 3
        raise
    ab_id = created["abTestId"]
    _ledger(ctx, "staging", "abtest", abTestId=ab_id, abTestArn=created["abTestArn"], name=name,
            controlVersion=bundle["versionId"], treatmentVersion=treatment_version)
    record = {"id": ab_id, "name": name, "createdAt": _now(), "gateway": GATEWAY_NAME, "gatewayArn": gateway["gatewayArn"],
              "bundleArn": bundle["arn"], "controlVersion": bundle["versionId"], "treatmentVersion": treatment_version,
              "treatmentSource": source, "onlineEval": oe["onlineEvaluationConfigArn"],
              "manifestVersion": current["manifestVersion"]}
    _save(ctx, args.root, "abtests", record)
    AB.wait_status(lambda: dp.get_ab_test(abTestId=ab_id), ("ACTIVE",))
    _say(f"A/B test {ab_id} ACTIVE: C = {bundle['versionId'][:8]} (staging's bundle), T1 = {treatment_version[:8]}")
    # 4. Traffic through the Gateway as the driver role.
    driver = ctx.assume(naming.role("staging", "abtest-driver"), "staging", f"A/B traffic {name}")
    runtime = driver.client("bedrock-agentcore", region_name=ctx.region,
                            endpoint_url=AB.target_url(gateway["gatewayUrl"], TARGET_NAME))
    identity = {"manifestVersion": current["manifestVersion"], "behaviorDigest": current["digests"]["behavior"],
                "release": dep["release"]}

    def invoke(session_id: str, payload: bytes) -> bytes:
        response = runtime.invoke_agent_runtime(agentRuntimeArn=dep["runtimeArn"], qualifier="staging",
                                                runtimeSessionId=session_id, payload=payload)
        return response["response"].read()
    _say(f"sending {args.sessions} sessions through {GATEWAY_NAME}/{TARGET_NAME} ({args.concurrency} at a time) as "
         f"{driver.session_name}")
    started = time.time()
    traffic = AB.drive(invoke, sessions=args.sessions, concurrency=args.concurrency, identity=identity,
                       fixtures=FIXTURES, mix={"payments": 0.6, "platform": 0.4}, ab_name=name, log=_say)
    record["sessions"] = {"sent": traffic.sent, "completed": traffic.completed, "failed": traffic.failed,
                          "seconds": round(time.time() - started), "errors": traffic.errors[:10]}
    _save(ctx, args.root, "abtests", record)
    _say(f"A/B traffic done: {traffic.completed}/{traffic.sent} completed in {record['sessions']['seconds']} s; "
         f"online evaluation scores them over the next minutes (`module11 abtest show {ab_id}`)", "green")
    return 0 if traffic.failed <= max(5, traffic.sent // 10) else 1


def _abtest_branch_head(ctl, bundle_arn: str) -> str | None:
    """The newest version on the `abtest` branch, so a new treatment names it as first parent."""
    from .aws import bundles
    rows = [v for v in bundles.list_versions(ctl, bundle_arn)            # oldest first
            if (v.get("lineageMetadata") or {}).get("branchName") == "abtest"]
    return rows[-1]["versionId"] if rows else None


def _ensure_tracing(ctx, gateway: dict) -> None:
    from .evaluation import abtest as AB
    logs = ctx.admin.client("logs")
    sources = logs.describe_delivery_sources().get("deliverySources", [])
    if any(s.get("name") == AB.TRACES_SOURCE for s in sources):
        return
    tags = {"Project": naming.PROJECT_TAG, "Stage": "staging"}
    logs.put_delivery_source(name=AB.TRACES_SOURCE, resourceArn=gateway["gatewayArn"], logType="TRACES", tags=tags)
    dest = logs.put_delivery_destination(name=AB.TRACES_DESTINATION, deliveryDestinationType="XRAY", tags=tags)
    delivery = logs.create_delivery(deliverySourceName=AB.TRACES_SOURCE,
                                    deliveryDestinationArn=dest["deliveryDestination"]["arn"], tags=tags)
    _ledger(ctx, "staging", "gateway-tracing", deliverySource=AB.TRACES_SOURCE, deliveryDestination=AB.TRACES_DESTINATION,
            deliveryId=delivery["delivery"]["id"], gatewayArn=gateway["gatewayArn"])
    _say(f"Gateway tracing to X-Ray enabled (delivery {delivery['delivery']['id']}): sessions are attributed to variants")


def cmd_abtest_show(args) -> int:
    from .evaluation import abtest as AB
    ctx = _ctx(args.root)
    record = _load(args.root, "abtests", args.id)
    ab_id = args.id or (record or {}).get("id")
    if not ab_id:
        _say("no A/B test recorded (`module11 abtest run --stage staging`)", "yellow")
        return 1
    response = ctx.admin.client("bedrock-agentcore").get_ab_test(abTestId=ab_id)
    for line in AB.show_lines(response, record):
        _say(line)
    if record is not None:
        record["results"] = json.loads(json.dumps(response.get("results") or {}, default=str))
        record["status"] = response.get("status")
        record["executionStatus"] = response.get("executionStatus")
        _save(ctx, args.root, "abtests", record)
    return 0 if AB.result_rows(response) else 1


def cmd_abtest_stop(args) -> int:
    ctx = _ctx(args.root)
    dp = ctx.admin.client("bedrock-agentcore")
    dp.update_ab_test(abTestId=args.id, executionStatus="STOPPED")
    _say(f"A/B test {args.id} stopped (its results stay readable)", "green")
    return 0


# ---------------------------------------------------------------- recommendation
def cmd_recommendation_run(args) -> int:
    from .evaluation import recommendations as REC
    from .infrastructure.harness.evaluation import online_eval_specs
    ctx = _ctx(args.root)
    ctl = ctx.admin.client("bedrock-agentcore-control")
    oe_name = next(s["name"] for s in online_eval_specs("prod", 100) if s["endpoint"] == "stable")
    oe = _online_eval(ctl, oe_name)
    if oe is None:
        raise RuntimeError(f"online-evaluation config {oe_name} does not exist: deploy the current harness to prod first")
    prompt = (args.root / REC.PROMPT).read_text(encoding="utf-8")
    start, end = REC.window(args.hours)
    name = f"m11_rec_{uuid.uuid4().hex[:8]}"
    request = REC.start_request(name, prompt, oe["onlineEvaluationConfigArn"], start, end,
                                tags={"Project": naming.PROJECT_TAG, "Stage": "prod"})
    try:
        started = ctx.admin.client("bedrock-agentcore").start_recommendation(**request, clientToken=str(uuid.uuid4()))
    except Exception as exc:
        if _code(exc) in ("ValidationException", "AccessDeniedException", "ServiceQuotaExceededException",
                          "UnknownOperationException"):
            _say(f"recommendation refused ({_code(exc)}: {str(exc)[:200]}). Fallback (Preview): change the prompt by "
                 "hand in an MR; the import below automates exactly that path", "yellow")
            return 3
        raise
    rec_id = started["recommendationId"]
    _ledger(ctx, "prod", "recommendation", recommendationId=rec_id, name=name)
    record = {"id": rec_id, "recommendationId": rec_id, "name": name, "createdAt": _now(),
              "evaluator": REC.TARGET_EVALUATOR, "onlineEval": oe["onlineEvaluationConfigArn"],
              "window": {"start": start.isoformat(timespec="minutes"), "end": end.isoformat(timespec="minutes")},
              "status": started.get("status")}
    _save(ctx, args.root, "recommendations", record)
    _say(f"recommendation {rec_id} ({name}) {started.get('status')}: target {REC.TARGET_EVALUATOR}, prod stable "
         f"traces {record['window']['start']} to {record['window']['end']}", "green")
    if args.wait:
        return _refresh_recommendation(ctx, args.root, record, wait=True)
    return 0


def _refresh_recommendation(ctx, root: Path, record: dict, *, wait: bool = False) -> int:
    from .evaluation import recommendations as REC
    dp = ctx.admin.client("bedrock-agentcore")
    deadline = time.time() + 3 * 3600
    while True:
        response = dp.get_recommendation(recommendationId=record["recommendationId"])
        record["status"], record["result"] = response.get("status"), REC.result_of(response)
        _save(ctx, root, "recommendations", record)
        if not wait or response.get("status") in ("COMPLETED", "FAILED") or time.time() > deadline:
            break
        time.sleep(60)
    for line in REC.show_lines(record, response, (root / REC.PROMPT).read_text(encoding="utf-8")):
        _say(line)
    return 0 if response.get("status") == "COMPLETED" else (2 if response.get("status") == "FAILED" else 1)


def cmd_recommendation_show(args) -> int:
    record = _load(args.root, "recommendations", args.id)
    if record is None:
        _say("no recommendation recorded (`module11 recommendation run`)", "yellow")
        return 1
    return _refresh_recommendation(_ctx(args.root), args.root, record)


def cmd_recommendation_import(args) -> int:
    from .aws import bot_changes
    from .evaluation import recommendations as REC
    record = _load(args.root, "recommendations", args.id)
    if record is None or not (record.get("result") or {}).get("prompt"):
        raise RuntimeError(f"recommendation {args.id} has no recommended prompt (run `recommendation show {args.id}`)")
    ctx = _ctx(args.root)
    change = bot_changes.open_change(
        _gitlab(ctx), branch=REC.branch_for(record["recommendationId"]), files=REC.import_files(record["result"]["prompt"]),
        message=f"Companion prompt from recommendation {record['recommendationId']}",
        title=f"instructions: companion prompt from recommendation {record['recommendationId']}",
        description=(f"AgentCore Recommendations ({record.get('evaluator')}, prod stable traces "
                     f"{record['window']['start']} to {record['window']['end']}).\n\n"
                     f"{(record['result'].get('explanation') or '').strip()[:3000]}\n\n"
                     "A recommendation is a hypothesis: this MR meets the same gate as any instructions change "
                     "(full suite, trajectory hard gate, canary). Opened by `module11 recommendation import`; the UI "
                     "preview is skipped for bot branches."),
        labels=_demo_labels(["change::instructions"]))
    record["importMr"] = {"iid": change.iid, "webUrl": change.web_url, "reused": change.reused}
    _save(ctx, args.root, "recommendations", record)
    _say(f"recommendation MR !{change.iid} ({'reused' if change.reused else 'opened'}): {change.web_url}", "green")
    return 0


# ---------------------------------------------------------------- model compare
def cmd_model_compare(args) -> int:
    from .aws import releases
    from .cli_eval import clients_of, definition, load_baseline, model_id_of, prices_of, _store
    from .cli_experiment import Experiment
    from .evaluation import shadow as SH
    from .evaluation.runner import Target, evaluator_specs, run_suite
    from .release import stage as cfg
    if args.stage != "dev":
        raise ValueError("model compare runs on dev (it redeploys dev's candidate)")
    ctx = _ctx(args.root)
    profile = ctx.admin.client("bedrock").get_inference_profile(inferenceProfileIdentifier=args.candidate)
    foundation = profile["models"][0]["modelArn"].rsplit("/", 1)[-1]
    current_model = model_id_of(args.root)
    branch = SH.branch_for(args.candidate)
    gl = _gitlab(ctx)
    from .aws import params
    prod = params.stage_get(ctx.admin.client("ssm"), "prod", "current") or {}
    base = (prod.get("git") or {}).get("commit")
    if not base:
        raise RuntimeError("prod has no /current: the migration branch starts from the release prod serves")
    text = (gl.file("module11/harness/agent/models.yaml", base) or b"").decode("utf-8")
    new = SH.models_override(text, args.candidate, foundation)
    _branch_from(gl, branch, {"module11/harness/agent/models.yaml": new.encode("utf-8")},
                 f"Model migration candidate {args.candidate} (module11 model compare)", base)
    _say(f"branch {branch}: models.yaml -> {args.candidate} ({foundation}), from {base[:7]} "
         f"({prod['deployment']['prod'].get('release')}, the release prod serves), so the experiment's change is models only")
    d = definition(args.root)
    thresholds = d["thresholds"]
    store = _store(ctx)
    baseline, noise, note = load_baseline(store, thresholds)
    from .cli_experiment import start_or_restore
    exp = Experiment(args.root)
    doc = start_or_restore(exp, branch, "dev", args.hours)
    exp_doc = exp.record(doc["id"])
    exp_doc["name"] = f"model-compare-{args.candidate}"
    exp.save(exp_doc)
    try:
        manifest = json.loads(releases.get(ctx.admin.client("s3"),
                                           f"s3://{ctx.release_bucket}/releases/experiments/{doc['manifestVersion']}/manifest.json"))
        from .cli_eval import target_of
        base = target_of(ctx, "dev")
        target = Target("dev", "candidate", base.runtime_arn, base.runtime_id, doc["manifestVersion"],
                        manifest["digests"]["behavior"], doc["id"], base.evaluator_ids)
        specs = evaluator_specs(d["evaluators"], target.evaluator_ids)
        repeats = thresholds["profiles"]["staging"]["repeats"]
        record = run_suite(clients_of(ctx, "dev"), target, d["scenarios"], repeats=repeats, evaluators=specs,
                           profile="staging", scope="full", evaluator_set_version=str(d["evaluators"]["version"]),
                           suite=d["suite"], kind="model-compare", prices=prices_of(args.root), model_id=args.candidate,
                           log=lambda line: _say(line))
    finally:
        exp.finish(doc["id"])
    doc_run = record.to_dict()
    store.put(f"evaluations/dev/{record.runId}.json", doc_run)
    out = _comparison(ctx, args.root, store, doc_run, candidate=args.candidate, foundation=foundation,
                      current_model=current_model, branch=branch, experiment=doc["id"], note=None)
    _save(ctx, args.root, "model-compare", out)
    for line in SH.compare_lines(out):
        _say(line)
    return 0


def _comparison(ctx, root: Path, store, doc_run: dict, *, candidate: str, foundation: str, current_model: str,
                branch: str, experiment: str | None, note, existing_id: str | None = None) -> dict:
    """The comparison record of a stored run against the calibration of the release prod serves (its manifest and the
    checkout's evaluator set), which is what `baselineManifest` names once the baseline MR has landed."""
    from .aws import params
    from .evaluation import shadow as SH
    from .evaluation.records import baseline_prefix
    from .cli_eval import definition
    d = definition(root)
    version = str(d["thresholds"]["evaluatorSetVersion"])
    prod = params.stage_get(ctx.admin.client("ssm"), "prod", "current") or {}
    manifest = prod.get("manifestVersion") or d["thresholds"].get("baselineManifest")
    base_doc = store.get(f"{baseline_prefix(manifest, version)}/baseline.json")
    noise_doc = store.get(f"{baseline_prefix(manifest, version)}/noise.json")
    repeats = int(d["thresholds"]["profiles"]["staging"]["repeats"].get("default", 3))
    rows = SH.compare_rows(doc_run["results"], base_doc, noise_doc, repeats=repeats)
    out = {"id": existing_id or f"mc-{uuid.uuid4().hex[:12]}", "createdAt": _now(), "candidateModel": candidate,
           "candidateFoundationModel": foundation, "currentModel": current_model, "branch": branch,
           "experiment": experiment, "runId": doc_run.get("runId"), "sessions": len(doc_run.get("sessions") or []),
           "baselineManifest": manifest, "evaluatorSet": version,
           "baselineNote": note or (None if base_doc else f"no calibration of {manifest} for set {version}"),
           "rows": rows, "classification": SH.classify(rows), "worstScenarios": SH.worst_scenarios(doc_run["results"])}
    _save(ctx, root, "model-compare", out)
    return out


def cmd_model_compare_rescore(args) -> int:
    """Recompute a stored comparison against the calibration that exists now (no new sessions)."""
    from .cli_eval import _store
    from .evaluation import shadow as SH
    record = _load(args.root, "model-compare", args.id)
    if record is None:
        _say("no model comparison recorded", "yellow")
        return 1
    ctx = _ctx(args.root)
    store = _store(ctx)
    run = store.get(f"evaluations/dev/{record['runId']}.json")
    if run is None:
        raise RuntimeError(f"no stored run {record['runId']}")
    out = _comparison(ctx, args.root, store, run, candidate=record["candidateModel"],
                      foundation=record["candidateFoundationModel"], current_model=record["currentModel"],
                      branch=record["branch"], experiment=record.get("experiment"), note=None, existing_id=record["id"])
    for line in SH.compare_lines(out):
        _say(line)
    return 0


def _branch_from(gl, branch: str, files: dict[str, bytes], message: str, base_sha: str) -> None:
    """Create `branch` at `base_sha` with these files; an existing branch is recreated, so it is base + this change.
    The base is the commit prod serves, not `main`: main may carry evaluation or tooling changes that the
    experiment path (model, instructions, tool description, orchestration only) would reject."""
    from .aws import bot_changes
    if not bot_changes.BOT_BRANCH.match(branch):
        raise ValueError("model branches start with model/")
    if gl.branch(branch) is not None:
        gl.delete_branch(branch)
    gl.create_branch(branch, base_sha)
    existing = [p for p in files if gl.file(p, base_sha) is not None]
    gl.commit_actions(branch, message, bot_changes.actions(files, existing))


def cmd_model_compare_show(args) -> int:
    from .evaluation import shadow as SH
    record = _load(args.root, "model-compare", args.id)
    if record is None:
        _say("no model comparison recorded (`module11 model compare --candidate <profile>`)", "yellow")
        return 1
    for line in SH.compare_lines(record):
        _say(line)
    return 0


# ---------------------------------------------------------------- scenario add
def cmd_scenario_add(args) -> int:
    from .aws import bot_changes
    from .evaluation import scenario_harvest as SHV
    ctx = _ctx(args.root)
    item = SHV.find_outcome(ctx.admin.client("dynamodb"), naming.table(args.stage, "outcomes"), args.from_session)
    if item is None:
        raise RuntimeError(f"no outcome item for session {args.from_session} on {args.stage} (items expire after 30 days)")
    result = SHV.read_result(ctx.admin.client("s3"), naming.artifacts_bucket(args.stage, ctx.account, ctx.region), item)
    from .aws import params, spans
    current = params.stage_get(ctx.admin.client("ssm"), args.stage, "current") or {}
    runtime_id = current["deployment"][args.stage]["runtimeId"]
    started = float(str(item.get("sk", "0")).split("#", 1)[0]) / 1000
    group = f"/aws/bedrock-agentcore/runtimes/{runtime_id}-{item.get('qualifier') or 'stable'}"
    docs = spans.session_spans(args.from_session, group, started - 60,
                               started + float(item.get("latencyMs") or 0) / 1000 + 120, logs=ctx.admin.client("logs"))
    row, diff = SHV.scenario_from(item, result, category=args.category, account=ctx.account, root=args.root,
                                  prompt=SHV.task_input(docs))
    path = f"module11/harness/evaluation/scenarios/{args.category}.jsonl"
    gl = _gitlab(ctx)
    text = (gl.file(path, "main") or b"").decode("utf-8")
    branch = SHV.branch_for(args.from_session)
    body = SHV.with_row(text, row)
    change = bot_changes.open_change(
        gl, branch=branch, files={path: body.encode("utf-8")},
        message=f"Regression scenario {row['scenario_id']} from session {SHV.session_hash(args.from_session)}",
        title=f"scenarios: {row['scenario_id']} ({args.category}, harvested)",
        description=SHV.description(row, diff, item), labels=_demo_labels(["change::evaluation-suite"]))
    _say(f"scenario {row['scenario_id']} ({args.category}) MR !{change.iid} "
         f"({'reused' if change.reused else 'opened'}): {change.web_url}", "green")
    for line in diff[:12]:
        _say(f"  redacted: {line}")
    return 0


def register(sub) -> None:
    ab = sub.add_parser("abtest", help="the staging config-bundle A/B test through Gateway m11-staging-ab")
    absub = ab.add_subparsers(dest="abtest_command", required=True)
    run = absub.add_parser("run")
    run.add_argument("--stage", default="staging", choices=["staging"])
    run.add_argument("--recommendation", help="treatment = this recommendation's prompt")
    run.add_argument("--prompt-file", help="treatment = this companion prompt")
    run.add_argument("--sessions", type=int, default=200)
    run.add_argument("--concurrency", type=int, default=6)
    run.set_defaults(fn=cmd_abtest_run)
    show = absub.add_parser("show")
    show.add_argument("id", nargs="?")
    show.set_defaults(fn=cmd_abtest_show)
    stop = absub.add_parser("stop")
    stop.add_argument("id")
    stop.set_defaults(fn=cmd_abtest_stop)
    rec = sub.add_parser("recommendation", help="AgentCore Recommendations for the companion prompt")
    rsub = rec.add_subparsers(dest="recommendation_command", required=True)
    rr = rsub.add_parser("run")
    rr.add_argument("--hours", type=float, default=24.0, help="trace window ending now (prod stable online evaluation)")
    rr.add_argument("--wait", action="store_true")
    rr.set_defaults(fn=cmd_recommendation_run)
    rs = rsub.add_parser("show")
    rs.add_argument("id", nargs="?")
    rs.set_defaults(fn=cmd_recommendation_show)
    ri = rsub.add_parser("import")
    ri.add_argument("id")
    ri.set_defaults(fn=cmd_recommendation_import)
    mo = sub.add_parser("model", help="model migration")
    msub = mo.add_subparsers(dest="model_command", required=True)
    mc = msub.add_parser("compare", help="full suite on a dev candidate serving model/<profile>; `compare show`")
    mc.add_argument("action", nargs="?", choices=["show", "rescore"])
    mc.add_argument("id", nargs="?")
    mc.add_argument("--candidate", help="inference profile ID, e.g. us.openai.gpt-6-luna")
    mc.add_argument("--stage", default="dev", choices=["dev"])
    mc.add_argument("--hours", type=float, default=0.25, help="the dev experiment's planned shadow window")
    mc.set_defaults(fn=lambda a: cmd_model_compare_show(a) if a.action == "show" else
                    cmd_model_compare_rescore(a) if a.action == "rescore" else
                    (cmd_model_compare(a) if a.candidate else (_say("--candidate is required", "red"), 2)[1]))
    sc = sub.add_parser("scenario", help="harvest a session into a regression scenario (redacted, as a bot MR)")
    ssub = sc.add_subparsers(dest="scenario_command", required=True)
    sa = ssub.add_parser("add")
    sa.add_argument("--from-session", required=True, help="the runtime session ID (outcome item)")
    sa.add_argument("--category", required=True, choices=["regressions", "edge_cases", "happy_path", "adversarial"])
    sa.add_argument("--stage", default="prod", choices=list(naming.STAGES))
    sa.set_defaults(fn=cmd_scenario_add)

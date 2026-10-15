# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The evaluation commands (registered by cli.py):

  gate --stage S --profile P [--scope X] [--from-run ID]    run a gate and print its report
  calibrate --manifest prod-current [--n 8] [--r 3]           baseline + noise from n runs on prod stable
  calibration show [--manifest MV]                            the stored calibration
  evalset rebaseline --old-ref REF | evalset show            the evaluator-set change procedure
  cost estimate --suite preview|staging|calibration          the projection, before any run
  baseline refresh [--manifest MV] [--merge|--local]          a bot MR setting thresholds.yaml baselineManifest

A gate exits 0 (PASS), 2 (FAIL) or 3 (INCONCLUSIVE). Runs invoke as the stage's eval-runner role (InvokeAgentRuntime,
StopRuntimeSession, Evaluate and log reads only); the RunRecord and baselines are written with the caller's own role.
"""
from __future__ import annotations

import json
import math
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .release import gate as G
from .release import eval_names, naming
from .release import stage as cfg

EXIT_OVER_BUDGET = 4
EXIT_BELOW_FLOOR = 5
DEFAULT_MAX_USD = 200.0          # cost guard: refuse a projected evaluation spend above this
FAST_GATE_BUDGET = (             # fast-gate step budgets: recorded, not acceptance
    ("setup", "CodeBuild start and tooling download", 45, "setup_s"),
    ("invoke", "24 invocations", 90, "invoke_s"),
    ("spans", "span readiness and fetch", 135, "spans_ready_s"),
    ("evaluate", "evaluations", 60, "evaluate_s"),
    ("note", "note", 15, "report_s"))


def _say(text: str = "", style: str | None = None) -> None:
    from .cli import say
    say(text, style)


def _mmss(seconds: float) -> str:
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


# ---------------------------------------------------------------- shared loading
def definition(root: Path) -> dict:
    """The gate definition of the checkout: thresholds, evaluators, scenarios and the suite identity."""
    thresholds, evaluators, scenarios = cfg.thresholds(root), cfg.evaluators(root), cfg.scenarios(root)
    return {"thresholds": thresholds, "evaluators": evaluators, "scenarios": scenarios,
            "suite": G.suite_sha256(scenarios)}


def prices_of(root: Path) -> dict:
    import yaml
    path = Path(root) / "module11/harness/evaluation/prices.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}


def model_id_of(root: Path) -> str:
    import yaml
    doc = yaml.safe_load((Path(root) / "module11/harness/agent/models.yaml").read_text(encoding="utf-8"))
    return str((doc.get("companion") or {}).get("modelId") or "")


def model_ids_of(root: Path) -> list[str]:
    """Every agent's modelId in models.yaml, in file order, without repeats."""
    import yaml
    doc = yaml.safe_load((Path(root) / "module11/harness/agent/models.yaml").read_text(encoding="utf-8")) or {}
    return list(dict.fromkeys(str(v.get("modelId")) for v in doc.values() if isinstance(v, dict) and v.get("modelId")))


def lambda_tools(root: Path) -> list[str]:
    from .release import artifacts as A
    return [t for t in A.tool_names(root) if A.tool_kind(root, t) == "lambda"]


def _store(ctx):
    from .evaluation.records import S3Store
    return S3Store(ctx.admin.client("s3"), ctx.release_bucket)


def evaluator_ids(ctx, stage: str, deployed: dict) -> dict:
    """Logical -> real evaluator IDs: recorded by `release deploy` in /current, else looked up by name."""
    if deployed:
        return dict(deployed)
    name, found = eval_names.evaluator_name(stage), {}
    control = ctx.admin.client("bedrock-agentcore-control")
    token = None
    while True:
        page = control.list_evaluators(**({"nextToken": token} if token else {}))
        for item in page.get("evaluators", []):
            if item.get("evaluatorName") == name:
                found[eval_names.EVALUATOR_LOGICAL] = item["evaluatorId"]
        token = page.get("nextToken")
        if not token:
            return found


def target_of(ctx, stage: str):
    from .aws import params
    from .evaluation.runner import Target
    via = getattr(ctx, "via", None) or {}
    ssm = via[stage].client("ssm", region_name=ctx.region) if stage in via else ctx.admin.client("ssm")
    current = params.stage_get(ssm, stage, "current")
    if not current:
        raise RuntimeError(f"{stage} has no /current: run `module11 release deploy --stage {stage}`")
    target = Target.from_current(current, stage)
    ids = evaluator_ids(ctx, stage, dict(target.evaluator_ids))
    return Target(target.stage, target.endpoint, target.runtime_arn, target.runtime_id, target.manifest_version,
                  target.behavior_digest, target.release, ids)


def clients_of(ctx, stage: str):
    from .evaluation.runner import Clients
    session = ctx.assume(naming.eval_runner_role(stage), stage, "evaluation run")
    return Clients.from_session(session, ctx.region)


def control_arm(ctx, chosen: list[dict], stage: str):
    """The latency control arm (evaluation/control.py): prod `stable`, as the prod eval runner narrowed by a
    session policy to invoke and stop sessions. None when the run has no happy_path scenarios or the stage
    under test is prod itself."""
    from .aws import params
    from .evaluation import control as CT
    from .evaluation.runner import Clients, Target
    scenarios = CT.scenarios_for(chosen)
    if not scenarios or stage == CT.CONTROL_STAGE:
        return None
    current = params.stage_get(ctx.admin.client("ssm"), CT.CONTROL_STAGE, "current")
    if not current:
        raise RuntimeError("prod has no /current: the latency control arm needs prod `stable`")
    target = Target.from_current(current, CT.CONTROL_STAGE, endpoint=CT.CONTROL_ENDPOINT)
    session = ctx.assume(naming.eval_runner_role(CT.CONTROL_STAGE), CT.CONTROL_STAGE, "gate latency control arm",
                         policy=CT.session_policy(target.runtime_arn))
    return CT.ControlArm(Clients.from_session(session, ctx.region), target, scenarios,
                         repeats=CT.repeats_for(scenarios))


def latency_control(record, chosen: list[dict]) -> dict:
    """The relative p95 rule's input for gate.evaluate (SKIP when the scope has no happy_path scenarios)."""
    from .evaluation import control as CT
    if not CT.scenarios_for(chosen):
        return {"status": "SKIP", "reason": "no happy_path scenarios in scope"}
    block = record.control or {}
    rows = block.get("records") or []
    # The balanced phase's candidate arm; a record from the interleaved first version compared the main suite's.
    candidate = block.get("candidateRecords") if block.get("design") == "balanced" else record.sessions
    out = CT.judge(CT.latencies(candidate or []), CT.latencies(rows))
    out["effect"] = CT.EFFECT
    if not rows:
        out["detail"] = "no control sessions"
    return out


def load_baseline(store, thresholds: dict) -> tuple[Any, Any, str | None]:
    """(Baseline, NoiseFloor, note). Both None while `baselineManifest` is null or no record exists yet."""
    manifest = thresholds.get("baselineManifest")
    if not manifest:
        return None, None, "baselineManifest is null (no calibration yet)"
    from .evaluation.records import baseline_prefix
    prefix = baseline_prefix(manifest, str(thresholds["evaluatorSetVersion"]))
    base, noise = store.get(f"{prefix}/baseline.json"), store.get(f"{prefix}/noise.json")
    if base is None:
        return None, None, f"no baseline record at {prefix}/ (baseline pending)"
    return G.Baseline.from_dict(base), (G.NoiseFloor.from_dict(noise) if noise else None), None


# ---------------------------------------------------------------- cost estimate
def _repeats_for(definition_: dict, suite: str) -> tuple[dict, str, int]:
    """(repeats, scope, number of runs) for a named suite."""
    profiles = definition_["thresholds"]["profiles"]
    names = {"preview": "preview", "fast": "preview", "mr": "preview", "staging": "staging", "full": "staging"}
    if suite == "calibration":
        return {"default": 3}, "full", 8
    if suite not in names:
        raise ValueError(f"unknown suite {suite!r}: preview, staging or calibration")
    profile = profiles[names[suite]]
    return dict(profile["repeats"]), profile["scope"], 1


def projection(root: Path, definition_: dict, suite: str, measured: dict | None = None):
    from .evaluation import cost as COST
    repeats, scope, runs = _repeats_for(definition_, suite)
    scenarios = [s for s in definition_["scenarios"] if G.in_scope(s, scope)]
    specs = [e["id"] for e in definition_["evaluators"]["evaluators"]]
    one = COST.project(suite, scenarios, repeats, evaluators=specs, prices=prices_of(root), model_id=model_id_of(root),
                       measured=measured)
    return one, runs


def cmd_cost_by_manifest(args) -> int:
    """Tokens, tool calls and cost per successful session for the manifests a stage served (B6 6c),
    from its adapter outcome items (30-day TTL) and prices.yaml."""
    from .aws import control_table
    from .cli_live import _context
    from .evaluation import cost as COST
    if not args.stage:
        raise ValueError("--stage is required (or use `cost estimate --suite ...`)")
    ctx = _context(args.root)
    items = control_table.scan_outcomes(ctx.admin.client("dynamodb"), naming.table(args.stage, "outcomes"))
    rows = COST.by_manifest(items, prices_of(args.root), model_id_of(args.root))
    if not rows:
        _say(f"{args.stage}: no served sessions in the outcomes table (items expire after 30 days)", "yellow")
        return 1
    _say(f"cost by manifest on {args.stage} ({len(rows)} manifest(s), {sum(r.sessions for r in rows)} sessions; "
         f"most recent {min(args.limit, len(rows))})")
    for line in COST.manifest_lines(rows, args.limit):
        _say(line)
    return 0


def cmd_cost_estimate(args) -> int:
    d = definition(args.root)
    one, runs = projection(args.root, d, args.suite)
    for line in one.lines():
        _say(line)
    if runs > 1:
        _say(f"  x {runs} runs: {one.sessions * runs} sessions, ${one.total_usd * runs:,.2f} total")
    return 0


def _print_projection(one, runs: int = 1, limit: float | None = None) -> float:
    for line in one.lines():
        _say(line)
    total = one.total_usd * runs
    if runs > 1:
        _say(f"  x {runs} runs: {one.sessions * runs} sessions, ${total:,.2f} total")
    if limit is not None and total > limit:
        _say(f"projected ${total:,.2f} is over the ${limit:,.0f} limit: raise --max-usd only with the owner's approval",
             "red")
    return total


# ---------------------------------------------------------------- gate
def guard_blocked_markdown(sessions: list[dict] | None) -> str:
    """Informational, unscored: tool calls the runtime's guards refused, by scenario. A refused call
    stays in the trajectory, so ExactOrder scenarios fail on it and InOrder ones tolerate it as an extra."""
    rows = [(s["scenarioId"], s["repeat"], int(s.get("guardBlocked") or 0)) for s in sessions or []
            if int(s.get("guardBlocked") or 0)]
    if not rows:
        return "**Guard-blocked attempts (informational, not scored):** 0."
    total = sum(n for _, _, n in rows)
    where = ", ".join(f"{sid}#{rep} ({n})" for sid, rep, n in sorted(rows))
    return (f"**Guard-blocked attempts (informational, not scored):** {total} in {len(rows)} session(s): {where}. "
            "Each was refused before it executed and stays in the trajectory.")


def _report_dict(report: G.GateReport, warmup: dict | None = None, sessions: list[dict] | None = None,
                 provider: dict | None = None, control: dict | None = None) -> dict:
    """The stored report: the gate table, then the informational blocks: the latency control arm
    (evaluation/control.py), warm-up (evaluation/warmup.py), guard-blocked attempts and model provider health
    (evaluation/provider_health.py)."""
    from .evaluation import control as CT
    from .evaluation import provider_health as PH
    from .evaluation import warmup as WU
    block = WU.markdown(warmup)
    guard = guard_blocked_markdown(sessions)
    health = PH.markdown(provider)
    parts = [report.markdownTable()]
    arm = None
    if control is not None:
        arm = CT.markdown({k: v for k, v in control.items() if k != "records"})
        parts.append(arm)
    transport = PH.transport_markdown((provider or {}).get("transport"))
    parts += [block, guard, transport, health]
    return {"verdict": report.verdict, "scope": report.scope, "comparable": report.comparable,
            "reasons": report.reasons, "lines": [asdict(line) for line in report.lines],
            "markdown": "\n\n".join(parts), "warmup": block, "guardBlocked": guard, "providerHealth": health,
            "providerHealthData": provider, "control": arm, "transport": transport}


def print_timings(timings: dict) -> None:
    _say("step timing against the fast-gate budgets (recorded, not acceptance):")
    total = 0.0
    for _, label, budget, key in FAST_GATE_BUDGET:
        seconds = timings.get(key)
        if seconds is None:
            _say(f"  {label:<40} {'n/a (runs in CodeBuild)' if key == 'setup_s' else '-':>12}   budget {_mmss(budget)}")
            continue
        total += seconds
        _say(f"  {label:<40} {_mmss(seconds):>12}   budget {_mmss(budget)}" + ("   over" if seconds > budget else ""))
    _say(f"  {'total measured':<40} {_mmss(total):>12}   budget {_mmss(sum(b for _, _, b, _ in FAST_GATE_BUDGET))}")
    if total > 20 * 60:
        slow = max(((timings.get(k) or 0, label) for _, label, _, k in FAST_GATE_BUDGET))[1]
        _say(f"WARN total {_mmss(total)} is over the 20:00 operational ceiling; the slow step is: {slow}", "yellow")


def cmd_gate(args) -> int:
    from .cli_live import _context
    from .evaluation import cost as COST
    from .evaluation.records import RunRecord, run_key
    from .evaluation.runner import evaluator_specs, run_suite
    t_begin = time.time()
    d = definition(args.root)
    thresholds, scenarios = d["thresholds"], d["scenarios"]
    if args.profile not in (thresholds["profiles"]):
        raise ValueError(f"unknown profile {args.profile!r}: {', '.join(thresholds['profiles'])}")
    profile = thresholds["profiles"][args.profile]
    override = getattr(args, "repeats", None)       # the change plan's repeatsOverride (e.g. a model change: 5)
    if override:
        profile = {**profile, "repeats": {**profile["repeats"], **{k: int(v) for k, v in override.items()}}}
    scope = args.scope or profile["scope"]
    chosen = [s for s in scenarios if G.in_scope(s, scope)]
    partial = bool(args.scenario)
    if partial:
        wanted = set(args.scenario.split(","))
        chosen = [s for s in chosen if s["scenario_id"] in wanted]
        if not chosen:
            raise ValueError("--scenario matches nothing in the scope")
    ctx = getattr(args, "ctx", None) or _context(args.root)      # the preview gate passes its chained context
    store = _store(ctx)
    baseline, noise, note = load_baseline(store, thresholds)
    target = target_of(ctx, args.stage)
    specs = evaluator_specs(d["evaluators"], target.evaluator_ids)
    if args.from_run:
        doc = store.get(run_key(args.stage, args.from_run))
        if doc is None:
            raise RuntimeError(f"no run record {run_key(args.stage, args.from_run)}")
        record = RunRecord.from_dict(doc)
        _say(f"re-scoring the stored run {record.runId} ({record.manifestVersion}, scope {record.scope}) against the baseline")
    else:
        one = COST.project(f"{args.profile} gate on {args.stage}", chosen, profile["repeats"],
                           evaluators=[s.id for s in specs], prices=prices_of(args.root),
                           model_id=model_id_of(args.root))
        total = _print_projection(one, 1, args.max_usd)
        if total > args.max_usd:
            return EXIT_OVER_BUDGET
        setup_s = time.time() - t_begin
        from .evaluation import warmup as WU
        record = run_suite(clients_of(ctx, args.stage), target, chosen, repeats=profile["repeats"], evaluators=specs,
                           profile=args.profile, scope=scope, evaluator_set_version=str(d["evaluators"]["version"]),
                           suite=d["suite"], kind="gate", prices=prices_of(args.root), model_id=model_id_of(args.root),
                           log=lambda line: _say(line), extra={"partial": partial},
                           warmup=WU.sessions_from(getattr(args, "warmup", None)),
                           control=control_arm(ctx, chosen, args.stage))
        record.timings["setup_s"] = round(setup_s, 1)
    t_report = time.time()
    if note:
        _say(f"baseline: {note}", "yellow")
    elif noise is None:
        _say("baseline: no noise record; statistical gates are inconclusive", "yellow")
    evaluated = chosen if partial else scenarios
    relative = args.stage != "prod" and (not args.from_run or bool(record.control))
    report = G.evaluate(thresholds, evaluated, record.eval_results(), record.metrics(), baseline, noise, scope=scope,
                        profile=profile, evaluator_set_version=str(d["evaluators"]["version"]), suite=d["suite"],
                        evaluators=[s.id for s in specs],
                        latency_control=latency_control(record, chosen) if relative else None)
    _say()
    _say(report.markdownTable())
    from .evaluation import provider_health as PH
    try:            # the caller's own identity reads the metrics (informational; never blocks the report)
        cloudwatch = ctx.admin.client("cloudwatch", region_name=ctx.region)
    except Exception:  # noqa: BLE001
        cloudwatch = None
    provider = PH.collect(cloudwatch, model_ids_of(args.root), record.sessions, transport=record.transport)
    record.report = _report_dict(report, record.warmup, record.sessions, provider,
                                 control=(record.control or {"reason": "no happy_path scenarios in scope"})
                                 if relative else None)
    if record.report["control"]:
        _say(record.report["control"])
    _say(record.report["warmup"])
    _say(record.report["guardBlocked"])
    _say(record.report["transport"])
    _say(record.report["providerHealth"])
    if (record.transport or {}).get("tripped"):
        _say(f"transport circuit breaker tripped ({record.transport['tripped']}): a service incident, not noise. "
             "Stop and report; do not re-run the gate to get a pass.", "red")
    args.report = record.report             # callers that post notes (pipeline gate, preview gate) read it
    if not args.no_write:
        if not args.from_run:
            record.timings["report_s"] = round(time.time() - t_report, 1)
        key = run_key(args.stage, record.runId)
        store.put(key, record.to_dict())
        _say(f"run record: {store.uri(key)}")
    if not args.from_run:
        print_timings(record.timings)
    cost = record.cost or COST.measure(record.to_dict())
    _say(f"measured cost ${cost['totalUsd']:.2f} for {cost['sessions']} sessions (${cost['perSessionUsd']:.4f} per session); "
         f"judge calls {cost['judgeCalls']}, code evaluations {cost['codeCalls']}")
    return report.exit_code


# ---------------------------------------------------------------- calibrate
def cmd_calibrate(args) -> int:
    from .aws import control_table, params
    from .cli_live import _context
    from .evaluation import calibrate as CAL
    from .evaluation.runner import evaluator_specs, run_suite
    if args.manifest != "prod-current":
        raise ValueError("calibrate measures prod `stable` while it serves the baseline manifest: --manifest prod-current")
    d = definition(args.root)
    thresholds, scenarios = d["thresholds"], d["scenarios"]
    ctx = _context(args.root)
    store = _store(ctx)
    target = target_of(ctx, "prod")
    state = (control_table.get(ctx.admin.client("dynamodb"), naming.table("prod", "control")) or {}).get("state")
    if state not in (None, "IDLE"):
        raise RuntimeError(f"prod has a rollout in state {state}: calibrate when prod is IDLE")
    freeze = params.freeze(ctx.admin.client("ssm"))
    if freeze.get("state") == "emergency":
        raise RuntimeError("an emergency freeze is active")
    specs = evaluator_specs(d["evaluators"], target.evaluator_ids)
    one, _ = projection(args.root, d, "calibration")
    total = _print_projection(one, args.n, args.max_usd)
    if total > args.max_usd:
        return EXIT_OVER_BUDGET
    _say(f"calibrating {target.manifest_version} on prod/{target.endpoint}: {args.n} runs at r={args.r}")
    prices, model = prices_of(args.root), model_id_of(args.root)

    def run_one(number: int):
        return run_suite(clients_of(ctx, "prod"), target_of(ctx, "prod"), scenarios, repeats={"default": args.r},
                         evaluators=specs, profile="calibration", scope="full",
                         evaluator_set_version=str(d["evaluators"]["version"]), suite=d["suite"], kind="calibration",
                         prices=prices, model_id=model, log=lambda line: _say(line))
    result = CAL.run(run_one=run_one, store=store, manifest_version=target.manifest_version,
                     evaluator_set_version=str(d["evaluators"]["version"]), suite=d["suite"], scenarios=scenarios,
                     thresholds=thresholds, evaluators=[s.id for s in specs], lambda_tools=lambda_tools(args.root),
                     n=args.n, r=args.r, calibration_id=args.resume, log=lambda line: _say(line))
    for line in CAL.summary_lines(result.baseline, result.noise):
        _say(line)
    _say_floor_findings(thresholds, result.baseline, result.noise)
    goal = CAL.goal_success(result.baseline)
    if goal is None or goal < CAL.GATE_MIN_GOAL_SUCCESS:
        _say(f"regressions Builtin.GoalSuccessRate is {goal}, below {CAL.GATE_MIN_GOAL_SUCCESS}: fix the prompts or "
             "scenarios (never loosen thresholds), log the change and recalibrate", "red")
        return EXIT_BELOW_FLOOR
    _say(f"calibration {result.calibration_id} written for {result.manifest_version}; next: `module11 baseline refresh`",
         "green")
    return 0


def cmd_calibration_show(args) -> int:
    from .cli_live import _context
    from .evaluation import calibrate as CAL
    from .evaluation.records import baseline_prefix
    d = definition(args.root)
    manifest = args.manifest or d["thresholds"].get("baselineManifest")
    if not manifest:
        _say("thresholds.yaml baselineManifest is null and no --manifest was given: no calibration to show", "yellow")
        return 1
    version = args.evaluator_set or str(d["thresholds"]["evaluatorSetVersion"])
    store = _store(_context(args.root))
    prefix = baseline_prefix(manifest, version)
    base, noise = store.get(f"{prefix}/baseline.json"), store.get(f"{prefix}/noise.json")
    if base is None:
        _say(f"no baseline at {store.uri(prefix + '/baseline.json')}", "yellow")
        return 1
    for line in CAL.summary_lines(base, noise):
        _say(line)
    _say_floor_findings(d["thresholds"], base, noise)
    goal = CAL.goal_success(base)
    return 0 if goal is not None and goal >= CAL.GATE_MIN_GOAL_SUCCESS else EXIT_BELOW_FLOOR


def _say_floor_findings(thresholds, base_doc, noise_doc) -> None:
    """WARN for every absolute floor the baseline release would fail in >= 1% of runs (gate.floor_findings)."""
    from .release import gate as G
    rows = G.floor_findings(thresholds, G.Baseline.from_dict(base_doc) if base_doc else None,
                            G.NoiseFloor.from_dict(noise_doc) if noise_doc else None)
    for row in rows:
        _say(f"WARN {row['evaluator']} {row['category']} ({row['scope']}): minMean {row['minMean']} is above the "
             f"calibrated floor {row['derived']} (baseline - {G.FLOOR_Z} x noise_r): an unchanged release fails it "
             f"in >= {G.FLOOR_FAIL_RATE:.0%} of runs", "yellow")


def reusable_record(ctx, root: Path, kind: str) -> str | None:
    """The pipeline Gate of a `rebaseline` or `evalset` release reuses the procedure's record when it already
    covers this release (one recalibration per change, not two): prod `/current` is the manifest the commit's
    `baselineManifest` names, and `baselines/<mv>/<set>/` holds a calibration (n >= 8 with noise) for this suite
    and evaluator set whose regressions GoalSuccessRate meets the 0.80 floor. For `evalset`, the commit must also
    carry the change record `evalset-changes/<from>-<to>.json` for that manifest. Otherwise None (the procedure runs)."""
    from .aws import params
    from .evaluation import calibrate as CAL
    from .evaluation.records import baseline_prefix
    d = definition(root)
    thresholds = d["thresholds"]
    current = params.stage_get(ctx.admin.client("ssm"), "prod", "current") or {}
    mv, version = current.get("manifestVersion"), str(thresholds["evaluatorSetVersion"])
    if not mv or thresholds.get("baselineManifest") != mv:
        return None
    store = _store(ctx)
    prefix = baseline_prefix(mv, version)
    base, noise = store.get(f"{prefix}/baseline.json"), store.get(f"{prefix}/noise.json")
    if not base or not noise or int(base.get("nRuns") or 0) < 8 or base.get("suiteSha256") != d["suite"] or \
            str(base.get("evaluatorSetVersion")) != version:
        return None
    goal = CAL.goal_success(base)
    if goal is None or goal < CAL.GATE_MIN_GOAL_SUCCESS:
        return None
    if kind == "evalset":
        records = sorted((root / "module11/harness/evaluation/evalset-changes").glob(f"*-{version}.json"))
        change = json.loads(records[-1].read_text()) if records else {}
        if change.get("manifestVersion") != mv or str(change.get("to")) != version:
            return None
    return (f"{kind}: calibration {base.get('calibrationId')} of {mv} ({base.get('nRuns')} runs, evaluator set {version}, "
            f"suite {d['suite'][:19]}...) already covers this release; regressions GoalSuccessRate {goal:.3f}. Reused, "
            "not re-run (one recalibration per change)")


def rederived_floors(thresholds_text: str, thresholds: dict, base_doc: dict, noise_doc: dict) -> tuple[str, list[str]]:
    """thresholds.yaml with every statistical floor that sits above its calibrated value lowered to it (rounded
    down to 2 decimals): the calibrate-before-enforce rule (an unchanged release fails a floor in
    under 1% of runs). Returns (text, notes); floors that already meet the rule are untouched."""
    from .release import gate as G
    rows = G.floor_findings(thresholds, G.Baseline.from_dict(base_doc), G.NoiseFloor.from_dict(noise_doc))
    text, notes = thresholds_text, []
    for row in rows:
        new = math.floor(float(row["derived"]) * 100) / 100
        pattern = re.compile(rf"(\{{evaluator: {re.escape(row['evaluator'])}, scope: {re.escape(row['category'])}, "
                             rf"minMean: )([0-9.]+)")
        text, count = pattern.subn(lambda m: f"{m.group(1)}{new:.2f}", text, count=1)
        notes.append(f"{row['evaluator']} {row['category']}: minMean {row['minMean']} -> {new:.2f} (calibrated floor "
                     f"{row['derived']} in scope {row['scope']})" if count else
                     f"{row['evaluator']} {row['category']}: minMean {row['minMean']} is above its calibrated floor "
                     f"{row['derived']}; not edited automatically (edit thresholds.yaml by hand)")
    return text, notes


# ---------------------------------------------------------------- baseline refresh
BASELINE_LINE = re.compile(r"^baselineManifest:.*$", re.M)


THRESHOLDS = "module11/harness/evaluation/thresholds.yaml"


def with_baseline_manifest(text: str, manifest_version: str) -> str:
    """thresholds.yaml text with `baselineManifest` set (comments kept)."""
    if not BASELINE_LINE.search(text):
        raise RuntimeError("thresholds.yaml has no baselineManifest line")
    return BASELINE_LINE.sub(f'baselineManifest: "{manifest_version}"', text, count=1)


def set_baseline_manifest(root: Path, manifest_version: str) -> bool:
    """Edit thresholds.yaml's `baselineManifest` in place (comments kept). True when it changed."""
    path = Path(root) / THRESHOLDS
    text = path.read_text(encoding="utf-8")
    new = with_baseline_manifest(text, manifest_version)
    if new == text:
        return False
    path.write_text(new, encoding="utf-8")
    return True


def baseline_branch(manifest_version: str) -> str:
    """`demo/baseline-<hex16>` (deterministic per manifest)."""
    import hashlib
    return f"demo/baseline-{hashlib.sha256(manifest_version.encode()).hexdigest()[:16]}"


def cmd_baseline_refresh(args) -> int:
    """Point the gate baseline at a calibrated manifest through a bot MR on GitLab. The MR
    touches a [Quality] path, so it waits for a `quality` approval once the governance fixes are applied (OPERATIONS.md); before
    that, `--merge` lets GitLab merge it when its pipeline passes. `--local` edits only this checkout."""
    import os
    from .aws import params
    from .cli_live import _context
    ctx = None if (args.local and args.manifest) else _context(args.root)
    manifest = args.manifest
    if not manifest:
        current = params.stage_get(ctx.admin.client("ssm"), "prod", "current")
        if not current:
            raise RuntimeError("prod has no /current")
        manifest = current["manifestVersion"]
    if args.local:
        changed = set_baseline_manifest(args.root, manifest)
        _say(f"thresholds.yaml baselineManifest -> {manifest}" + ("" if changed else " (already set)"), "green")
        return 0
    from .aws import bot_changes, gitlab_api as G
    gl = G.from_secret(ctx.admin.client("secretsmanager"),
                       os.environ.get("M11_GITLAB_TOKEN_SECRET", "devops-companion/partners/gitlab-token"),
                       os.environ.get("M11_GITLAB_PROJECT_ID", ""))
    files, notes = baseline_change(gl, args.root, manifest, store=_store(ctx) if args.rederive_floors else None)
    if not files:
        _say(f"main already names baselineManifest {manifest}", "green")
        return 0
    labels = ["m11-demo-run"] if os.environ.get("M11_DEMO_RUN") else []
    description = (f"Points `baselineManifest` at the calibrated release {manifest} (`module11 baseline refresh`). "
                   "Until a calibration record exists for it, gates return INCONCLUSIVE.")
    if notes:
        description += "\n\n" + "\n".join(f"- {n}" for n in notes)
    approvers = None
    if args.merge:
        from .release import approvals as AP
        approvers = AP.parse_approvers(os.environ.get("M11_APPROVER_USERS"))  # fail closed before opening anything
    change = bot_changes.open_change(
        gl, branch=baseline_branch(manifest), files=files,
        message=f"Gate baseline: {manifest}", title=f"Gate baseline -> {manifest}", description=description,
        labels=labels)
    _say(f"baseline MR !{change.iid} ({'reused' if change.reused else 'opened'}): {change.web_url}", "green")
    for note in notes:
        _say(f"  {note}")
    if approvers is not None:
        _say(f"waiting for !{change.iid}'s pipeline and the approvals the design requires (a quality reviewer and a "
             "harness-owner or platform peer for evaluation paths)", "yellow")
        bot_changes.wait_and_merge(gl, change.iid, approvers, say=lambda t: _say(t))
    return 0


EVALUATORS = "module11/harness/evaluation/evaluators.yaml"
EVALSET_CHANGES = "module11/harness/evaluation/evalset-changes"


def baseline_change(gl, root: Path, manifest: str, *, store=None) -> tuple[dict[str, bytes], list[str]]:
    """The files of the baseline MR against main: thresholds.yaml with `baselineManifest`, and, when this checkout
    carries a newer evaluator set (the evalset procedure ran here), its evaluators.yaml, its change record and the
    thresholds file's `evaluatorSetVersion`. With `store`, floors above their calibrated value are lowered
    (`rederived_floors`, the documented calibrate-before-enforce rule)."""
    import yaml
    from .evaluation.records import baseline_prefix
    main_text = (gl.file(THRESHOLDS, "main") or b"").decode("utf-8")
    main_set = str((yaml.safe_load((gl.file(EVALUATORS, "main") or b"version: null").decode("utf-8")) or {}).get("version"))
    local = definition(root)
    local_set = str(local["evaluators"]["version"])
    files: dict[str, bytes] = {}
    notes: list[str] = []
    if local_set != main_set:
        text = with_baseline_manifest((root / THRESHOLDS).read_text(encoding="utf-8"), manifest)
        files[EVALUATORS] = (root / EVALUATORS).read_bytes()
        for record in sorted((root / EVALSET_CHANGES).glob(f"*-{local_set}.json")):
            files[f"{EVALSET_CHANGES}/{record.name}"] = record.read_bytes()
        notes.append(f"evaluator set {main_set} -> {local_set} (`module11 evalset show`), with its change record")
    else:
        text = with_baseline_manifest(main_text, manifest)
    if store is not None:
        prefix = baseline_prefix(manifest, local_set)
        base, noise = store.get(f"{prefix}/baseline.json"), store.get(f"{prefix}/noise.json")
        if base and noise:
            import yaml as _y
            text, floor_notes = rederived_floors(text, _y.safe_load(text), base, noise)
            notes += floor_notes
    if text != main_text:
        files[THRESHOLDS] = text.encode("utf-8")
    return files, notes


# ---------------------------------------------------------------- evalset
def cmd_evalset_show(args) -> int:
    d = definition(args.root)
    _say(f"evaluators.yaml version {d['evaluators']['version']}; thresholds.yaml evaluatorSetVersion "
         f"{d['thresholds']['evaluatorSetVersion']}; baselineManifest {d['thresholds'].get('baselineManifest')}")
    for entry in d["evaluators"]["evaluators"]:
        _say(f"  {entry['id']:<36} {entry['level']:<8} {entry['kind']}")
    changes = sorted((args.root / "module11/harness/evaluation/evalset-changes").glob("*.json"))
    for path in changes:
        doc = json.loads(path.read_text(encoding="utf-8"))
        _say(f"  change {doc['from']} -> {doc['to']} (run {doc['runId']}, baseline written {doc['baselineWritten']})")
    if not changes:
        _say("  no evaluator-set changes recorded")
    return 0


def ref_with_evaluator_set(root: Path, version: str) -> str:
    """The newest commit whose evaluators.yaml has `version` (`evalset rebaseline --from <version>`)."""
    import subprocess
    import yaml
    out = subprocess.run(["git", "-C", str(root), "log", "--format=%H", "-n", "200", "--", EVALUATORS],
                         capture_output=True, text=True, check=True).stdout.split()
    for sha in out:
        shown = subprocess.run(["git", "-C", str(root), "show", f"{sha}:agentic-ai/{EVALUATORS}"], capture_output=True,
                               text=True)
        if shown.returncode != 0:
            shown = subprocess.run(["git", "-C", str(root), "show", f"{sha}:{EVALUATORS}"], capture_output=True, text=True)
        if shown.returncode == 0 and str((yaml.safe_load(shown.stdout) or {}).get("version")) == version:
            return sha
    raise ValueError(f"no commit in the last 200 changes of {EVALUATORS} has evaluator set {version}")


def cmd_evalset_rebaseline(args) -> int:
    from .aws import params
    from .cli import tree_at
    from .cli_live import _context
    from .evaluation import evalset as ES
    from .evaluation.runner import evaluator_specs, run_suite
    d = definition(args.root)
    new = d["evaluators"]
    to_version, from_version = getattr(args, "to_version", None), getattr(args, "from_version", None)
    if to_version is not None and str(new["version"]) != str(to_version):
        raise ValueError(f"evaluators.yaml in this checkout is version {new['version']}, not {to_version}")
    old_ref = args.old_ref if from_version is None else ref_with_evaluator_set(args.root, str(from_version))
    with tree_at(args.root, old_ref) as tree:
        old = cfg.evaluators(tree)
    if from_version is not None and str(old["version"]) != str(from_version):
        raise ValueError(f"no commit holds evaluator set {from_version}")
    ctx = _context(args.root)
    store = _store(ctx)
    ssm = ctx.admin.client("ssm")
    target = target_of(ctx, "prod")
    caller = ctx.admin.client("sts").get_caller_identity()["Arn"]

    def freeze_get() -> dict:
        return params.freeze(ssm)

    def freeze_set(doc) -> None:
        params.put_json(ssm, naming.freeze_param(), dict(doc))

    def run(entries: list) -> Any:
        specs = evaluator_specs({"evaluators": entries}, target.evaluator_ids)
        return run_suite(clients_of(ctx, "prod"), target_of(ctx, "prod"), d["scenarios"],
                         repeats={"default": ES.REPEATS}, evaluators=specs, profile="rebaseline", scope="full",
                         evaluator_set_version=str(new["version"]), suite=d["suite"], kind="rebaseline",
                         prices=prices_of(args.root), model_id=model_id_of(args.root), log=lambda line: _say(line))
    try:
        change = ES.rebaseline(old, new, thresholds=d["thresholds"], get_freeze=freeze_get, set_freeze=freeze_set,
                               run_suite=run, store=store, manifest_version=target.manifest_version, suite=d["suite"],
                               scenarios=d["scenarios"], evaluators=ES.set_ids(new),
                               lambda_tools=lambda_tools(args.root), by=caller, root=args.root,
                               log=lambda line: _say(line))
    except ES.FreezeHeld as exc:
        _say(f"FreezeHeld: {exc}", "yellow")
        return 1
    _say(json.dumps(change, indent=2, sort_keys=True))
    return 0


# ---------------------------------------------------------------- registration
def register(sub) -> None:
    g = sub.add_parser("gate", help="run a gate and print its report (exit 0 PASS, 2 FAIL, 3 INCONCLUSIVE)")
    g.add_argument("--stage", required=True, help="dev | staging | prod | mr-<n>")
    g.add_argument("--profile", default="staging", help="preview (the MR fast gate) | staging")
    g.add_argument("--scope", help="override the profile's run scope: full | tag:<t>")
    g.add_argument("--scenario", help="debug: comma-separated scenario IDs (the report is partial)")
    g.add_argument("--from-run", help="re-score a stored run record instead of running")
    g.add_argument("--no-write", action="store_true", help="do not write the run record")
    g.add_argument("--max-usd", type=float, default=DEFAULT_MAX_USD)
    g.add_argument("--warmup", type=int, default=None,
                   help="untimed shadow warm-up sessions before the suite (default M11_GATE_WARMUP or 12; 0 = off)")
    g.set_defaults(fn=cmd_gate)
    c = sub.add_parser("calibrate", help="baseline and noise floor from n full-suite runs on prod stable")
    c.add_argument("--manifest", default="prod-current")
    c.add_argument("--n", type=int, default=8)
    c.add_argument("--r", type=int, default=3)
    c.add_argument("--resume", help="calibration ID to continue")
    c.add_argument("--max-usd", type=float, default=DEFAULT_MAX_USD)
    c.set_defaults(fn=cmd_calibrate)
    cal = sub.add_parser("calibration", help="the stored calibration")
    csub = cal.add_subparsers(dest="calibration_command", required=True)
    show = csub.add_parser("show")
    show.add_argument("--manifest")
    show.add_argument("--evaluator-set")
    show.set_defaults(fn=cmd_calibration_show)
    e = sub.add_parser("evalset", help="the evaluator-set change procedure")
    esub = e.add_subparsers(dest="evalset_command", required=True)
    esub.add_parser("show").set_defaults(fn=cmd_evalset_show)
    rb = esub.add_parser("rebaseline")
    rb.add_argument("--old-ref", default="HEAD", help="git ref holding the previous evaluators.yaml")
    rb.add_argument("--from", dest="from_version", help="the previous evaluator-set version (finds its commit)")
    rb.add_argument("--to", dest="to_version", help="the new version, which this checkout's evaluators.yaml must have")
    rb.set_defaults(fn=cmd_evalset_rebaseline)
    cost = sub.add_parser("cost", help="served cost per manifest (--stage S --by manifest), or `cost estimate`")
    cost.add_argument("--stage", choices=list(naming.STAGES))
    cost.add_argument("--by", choices=["manifest"], default="manifest")
    cost.add_argument("--limit", type=int, default=6, help="the most recent N manifests")
    cost.set_defaults(fn=cmd_cost_by_manifest)
    csub2 = cost.add_subparsers(dest="cost_command", required=False)
    est = csub2.add_parser("estimate")
    est.add_argument("--suite", required=True, help="preview | staging | calibration")
    est.set_defaults(fn=cmd_cost_estimate)
    b = sub.add_parser("baseline", help="baseline bookkeeping")
    bsub = b.add_subparsers(dest="baseline_command", required=True)
    br = bsub.add_parser("refresh")
    br.add_argument("--manifest", help="manifestVersion (default: prod /current)")
    br.add_argument("--merge", action="store_true",
                    help="wait for the MR pipeline and the approvals the design requires (M11_APPROVER_USERS), "
                         "then merge as the bot")
    br.add_argument("--local", action="store_true", help="edit this checkout only, no MR")
    br.add_argument("--rederive-floors", action="store_true",
                    help="lower floors that sit above their calibrated value (calibrate-before-enforce)")
    br.set_defaults(fn=cmd_baseline_refresh)

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The rollout and operations CLI commands (registered by cli.py):

  rollout show|watch|tick|abort|reset --stage S      the control item; tick/abort/reset invoke the controller Lambda
  traffic --stage S (--sessions N | --until-terminal --rate R) --concurrency C --mix payments=0.6
  latch clear <hex> --reason R                        audited
  side-effects --release <slug> --stage S             the stamped incident notes a release's sessions wrote
  memory purge --release <slug> --stage S [--dry-run] the purge-wait rule (h.4), then the release namespace only
  shadow probe --stage staging                        one shadow invocation as the staging eval runner; no side effects
  flags kill <flag> --team T --stage S | flags restore --stage S
  drift check --stage S [--external]                  invokes m11-<stage>-drift-check; --external adds the probes
  drift revert --stage S                              re-applies endpoints / role policies as the stage deployer
  release approve --execution <id> --break-glass      completed with the pipeline

Nothing here writes the control item: every state change is a command to the stage's controller.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .release import naming

DEMO_PROFILE_LABEL = (
    "Demo rollout profile: 50%\u2192100%, 1-minute ticks, 8 sessions per arm in a 3-minute window and 4 new per "
    "counted tick, breach needs p < 0.05, 2 breaches to roll back, 3 clean ticks to advance. Production default: "
    "5\u219225\u219250\u2192100, independent batches of 50 per arm, breach needs p < 0.10, 30-minute dwell, 5 breaches; "
    "error and tool-failure rates pooled over the whole rollout, 2 confirming looks; quality n \u2265 200 per arm "
    "before 100%.")
HEX = re.compile(r"^[0-9a-f]{64}$")


def _say(text: str, style: str | None = None) -> None:
    from .cli import say
    say(text, style)


def _ctx(root: Path):
    from .cli_live import _context
    return _context(root)


def _when(epoch) -> str:
    if epoch in (None, ""):
        return "-"
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _control(ctx, stage: str) -> dict | None:
    from .aws import control_table
    return control_table.get(ctx.admin.client("dynamodb"), naming.table(stage, "control"))


def _arm_counts(ctx, stage: str, rollout_id: str) -> dict:
    from .aws import control_table
    rows = control_table.outcomes(ctx.admin.client("dynamodb"), naming.table(stage, "outcomes"), rollout_id)
    out: dict = {}
    for r in rows:
        if r.get("shadow"):
            continue
        arm = out.setdefault(r.get("arm"), {"n": 0, "runbooks": 0, "errors": 0})
        arm["n"] += 1
        arm["runbooks"] += "search_runbooks" in (r.get("toolsExecuted") or [])
        arm["errors"] += r.get("status") != "completed"
    return out


# ---------------------------------------------------------------- rollout
def cmd_rollout_show(args) -> int:
    ctx = _ctx(args.root)
    item = _control(ctx, args.stage)
    if not item:
        _say(f"{args.stage}: no control item", "yellow")
        return 1
    state = item.get("state")
    _say(f"{args.stage}: {state}  rollout {item.get('rolloutId') or '-'}  canary {item.get('canaryPct', 0)}%  "
         f"revision {item.get('revision')}", "green" if state in ("IDLE", "PROMOTED") else
         "red" if state in ("ROLLING_BACK", "ROLLED_BACK") else None)
    if item.get("profile") == "demo" or (state == "IDLE" and args.stage in ("dev", "prod")):
        _say(DEMO_PROFILE_LABEL)
    eps = item.get("endpoints") or {}
    for q in sorted(eps):
        e = eps[q]
        _say(f"  {q:<9} v{e.get('version')}  {e.get('release')}  {e.get('manifestVersion')}")
    if item.get("rolloutId"):
        _say(f"  candidate v{item.get('candidateVersion')} ({item.get('release')}) vs stable v{item.get('stableVersion')}; "
             f"opened {_when(item.get('openedAt'))}")
        if item.get("rolledBackAt"):
            _say(f"  ROLLED_BACK {_when(item['rolledBackAt'])}: {item.get('reason')}"
                 + ("; behavior digest latched" if item.get("latch") else ""))
        if item.get("promotedAt"):
            _say(f"  PROMOTED {_when(item['promotedAt'])}")
        counts = _arm_counts(ctx, args.stage, item["rolloutId"])
        for arm, c in sorted(counts.items()):
            _say(f"  {arm:<9} {c['n']} sessions, search_runbooks {c['runbooks']}/{c['n']}, errors {c['errors']}")
        if item.get("lastSignals"):
            _say(f"  last signals {item['lastSignals']}; breaches {item.get('breaches', 0)}/2, "
                 f"clean ticks {item.get('cleanTicks', 0)}/3")
    if args.json:
        print(json.dumps(item, indent=2, sort_keys=True, default=str))
    return 0


def cmd_rollout_watch(args) -> int:
    """One row per controller revision (the S7 tick table), until terminal or --minutes."""
    ctx = _ctx(args.root)
    end = time.time() + args.minutes * 60
    last = None
    while time.time() < end:
        item = _control(ctx, args.stage) or {}
        if item.get("revision") != last:
            last = item.get("revision")
            counts = _arm_counts(ctx, args.stage, item["rolloutId"]) if item.get("rolloutId") else {}
            c, s = counts.get("candidate", {}), counts.get("stable", {})
            _say(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} r{last} {item.get('state'):<12} "
                 f"cand n={c.get('n', 0)} runbooks {c.get('runbooks', 0)} | stable n={s.get('n', 0)} runbooks "
                 f"{s.get('runbooks', 0)} | breaches {item.get('breaches', 0)}/2 clean {item.get('cleanTicks', 0)}/3 "
                 f"{item.get('lastSignals') or ''}")
        if item.get("state") in ("IDLE", "PROMOTED", "ROLLED_BACK") and args.until_terminal:
            return 0
        time.sleep(args.interval)
    return 0


def _invoke(ctx, stage: str, command: str, cmd_args: dict) -> dict:
    from .aws import rollouts
    lam = rollouts.lambda_client(ctx.admin, ctx.region)
    started = time.time()
    result = rollouts.invoke(lam, stage, command, cmd_args, check=False, log=lambda line: _say(line, "yellow"))
    _say(f"controller {command} on {stage}: {json.dumps(result, sort_keys=True)} ({time.time() - started:.1f} s)",
         "green" if result.get("ok") else "red")
    return result


def cmd_rollout_tick(args) -> int:
    ctx = _ctx(args.root)
    ok = True
    for i in range(args.times):
        if i:
            time.sleep(args.gap)
        ok &= bool(_invoke(ctx, args.stage, "tick", {}).get("ok"))
    return 0 if ok else 1


def cmd_rollout_abort(args) -> int:
    ctx = _ctx(args.root)
    result = _invoke(ctx, args.stage, "abort", {"reason": args.reason, "latch": not args.no_latch})
    return 0 if result.get("ok") else 1


def cmd_rollout_reset(args) -> int:
    ctx = _ctx(args.root)
    return 0 if _invoke(ctx, args.stage, "reset", {"reason": args.reason}).get("ok") else 1


# ---------------------------------------------------------------- traffic
def _candidate_pins(ctx, stage: str):
    from .aws import params, releases

    def read() -> dict:
        ssm, s3 = ctx.admin.client("ssm"), ctx.admin.client("s3")
        doc = params.stage_get(ssm, stage, "candidate") or params.stage_get(ssm, stage, "current")
        uri = doc["deployment"][stage]["pinsUri"]
        return json.loads(releases.get(s3, uri))
    return read


def parse_rate(text) -> float:
    """Sessions per minute from `20`, `20/min` or `120/h` (the 2-hour shadow driver: `--rate 120/h --hours 2`)."""
    value = str(text).strip().lower()
    for suffix, scale in (("/min", 1.0), ("/m", 1.0), ("/h", 1 / 60), ("/hour", 1 / 60)):
        if value.endswith(suffix):
            rate = float(value[: -len(suffix)]) * scale
            break
    else:
        rate = float(value)
    if not 0 < rate <= 120:
        raise ValueError("--rate must be 0-120 sessions per minute")
    return rate


def cmd_traffic(args) -> int:
    from module10.cloud_client import CloudClient
    from .aws import params
    from .demo import state as S, traffic as T
    ctx = _ctx(args.root)
    current = params.stage_get(ctx.admin.client("ssm"), args.stage, "current")
    if not current:
        raise RuntimeError(f"{args.stage} has no /current")
    api_url = current["deployment"][args.stage]["apiUrl"]
    env = ctx.admin.client("lambda").get_function_configuration(
        FunctionName=naming.function(args.stage, "adapter"))["Environment"]["Variables"]
    team_concurrency = int(env.get("TEAM_CONCURRENCY", "1"))
    creds = os.environ.get("M11_MODULE10_CREDENTIALS_FILE")
    if not creds or not Path(creds).is_file():
        raise RuntimeError("M11_MODULE10_CREDENTIALS_FILE must name Module 10's credentials.json")
    local = args.root / "module11/.local"
    local.mkdir(parents=True, exist_ok=True)
    outputs = local / f"traffic-outputs-{args.stage}.json"
    outputs.write_text(json.dumps({"apiUrl": api_url}), encoding="utf-8")
    clients: dict = {}

    def send(team: str, summary: str, request_id: str):
        client = clients.get(team)
        if client is None:
            client = clients.setdefault(team, CloudClient(outputs, Path(creds)))
        response = client.invoke(team, summary, request_id)
        return response.status_code, response.body if isinstance(response.body, dict) else {}

    ports = T.Ports(send=send, read_control=lambda: _control(ctx, args.stage),
                    candidate_pins=_candidate_pins(ctx, args.stage),
                    log=lambda line: _say(line, "yellow" if line.startswith(("WARN", "ERROR")) else None))
    mix = T.parse_mix(args.mix)
    try:
        code, summary = T.run(ports, stage=args.stage, mix=mix, concurrency=args.concurrency,
                              team_concurrency=team_concurrency, sessions=args.sessions,
                              until_terminal=args.until_terminal, rate=parse_rate(args.rate),
                              max_minutes=args.hours * 60 if args.hours else args.max_minutes,
                              expected_rollout=args.rollout_id, wait_seconds=args.wait_seconds, out_dir=local)
    except T.TrafficError as exc:
        _say(str(exc), "red")
        return exc.code
    return code


# ---------------------------------------------------------------- latch
def cmd_latch_clear(args) -> int:
    from .aws import ledger
    from .release import audit, latch
    digest = args.digest.removeprefix("sha256:")
    if not HEX.match(digest):
        raise ValueError("latch clear needs the 64-hex behavior digest")
    ctx = _ctx(args.root)
    ssm = ctx.admin.client("ssm")
    name = latch.name(f"sha256:{digest}")
    try:
        before = json.loads(ssm.get_parameter(Name=name)["Parameter"]["Value"])
    except Exception as exc:  # noqa: BLE001
        if "ParameterNotFound" in str(exc):
            _say(f"{name}: not latched", "yellow")
            return 0
        raise
    actor = ctx.admin.client("sts").get_caller_identity()["Arn"]
    value, record = latch.cleared(f"sha256:{digest}", args.reason, actor, time.time())
    ssm.put_parameter(Name=name, Value=value, Type="String", Overwrite=True, Tier="Standard")
    ledger.put(ctx.admin.client("s3"), ctx.release_bucket, audit.SHARED, "latch-clear", by=actor, parameter=name,
               behaviorDigest=f"sha256:{digest}", reason=args.reason, rolloutId=before.get("rolloutId"),
               previous=before.get("reason"))
    _say(f"latch cleared: {name} (was {before.get('state')} by {before.get('rolloutId')}: {before.get('reason')}); "
         f"audit record {json.dumps(record['details'], sort_keys=True)}", "green")
    return 0


# ---------------------------------------------------------------- side effects and memory
def _side_effect_keys(ctx, stage: str, release: str) -> tuple[str, str, list[str]]:
    """(bucket, prefix, keys) of a release's side-effect objects: one per record_incident_note call (S3 is
    strongly consistent, so this is the purge's count N)."""
    s3 = ctx.admin.client("s3")
    bucket = naming.artifacts_bucket(stage, ctx.account, ctx.region)
    prefix = f"side-effects/{stage}/{release}/"
    keys, token = [], None
    while True:
        page = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, **({"ContinuationToken": token} if token else {}))
        keys += [o["Key"] for o in page.get("Contents") or []]
        token = page.get("NextContinuationToken")
        if not token:
            return bucket, prefix, keys


def cmd_side_effects(args) -> int:
    ctx = _ctx(args.root)
    s3 = ctx.admin.client("s3")
    bucket, prefix, keys = _side_effect_keys(ctx, args.stage, args.release)
    _say(f"{len(keys)} incident note(s) written by {args.release} on {args.stage} (s3://{bucket}/{prefix})")
    for key in keys[:args.limit]:
        note = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
        stamp = note.get("stamp") or {}
        _say(f"  {note.get('noteId')} {note.get('service')}: manifest {stamp.get('manifestVersion')}, variant "
             f"{stamp.get('variant')}, session {stamp.get('sessionId')}")
    return 0


def _memory_id(ctx, stage: str) -> str:
    from .aws import params
    current = params.stage_get(ctx.admin.client("ssm"), stage, "current")
    dep = current["deployment"][stage]
    runtime = ctx.admin.client("bedrock-agentcore-control").get_agent_runtime(
        agentRuntimeId=dep["runtimeId"], agentRuntimeVersion=str(dep["runtimeVersion"]))
    config = json.loads((runtime.get("environmentVariables") or {}).get("M11_RUNTIME_CONFIG", "{}"))
    return config["memoryId"]


def release_rollout(item: dict | None, release: str) -> dict | None:
    """The release's rollout as purge_wait sees it. The control item holds only the current rollout:
    when it names another release, the requested one ended before that rollout opened (open needs a terminal or
    IDLE item), so the bound is the current rollout's openedAt."""
    if not item:
        return None
    if item.get("release") == release:
        if item.get("state") == "IDLE" and (item.get("rolledBackAt") or item.get("promotedAt")):
            return {**item, "state": "PROMOTED" if item.get("promotedAt") and not item.get("rolledBackAt")
                    else "ROLLED_BACK"}
        return item
    if item.get("openedAt"):
        return {"rolloutId": None, "state": "ROLLED_BACK", "rolledBackAt": item["openedAt"]}
    return {"rolloutId": None, "state": "ROLLED_BACK", "rolledBackAt": item.get("endpointsWrittenAt")}


def cmd_memory_purge(args) -> int:
    from .aws import control_table, memory, params, releases
    from .demo import state as S
    from .release import pins as P
    ctx = _ctx(args.root)
    ddb, s3, ssm = ctx.admin.client("dynamodb"), ctx.admin.client("s3"), ctx.admin.client("ssm")
    memory_id = _memory_id(ctx, args.stage)
    plane = ctx.admin.client("bedrock-agentcore")
    if not args.dry_run:
        item = control_table.get_raw(ddb, naming.table(args.stage, "control"))
        mv = args.manifest or ((item or {}).get("manifestVersion") if (item or {}).get("release") == args.release
                               else None)
        try:
            if not mv:
                mv = params.stage_get(ssm, args.stage, "current")["manifestVersion"]
                _say(f"WARN the control item does not name {args.release}; deadlineSeconds from {mv}'s pins", "yellow")
            pins_doc = json.loads(releases.get(s3, f"s3://{ctx.release_bucket}/release-inputs/{args.stage}/{mv}.pins.json"))
            ready = memory.purge_wait(
                lambda: release_rollout(control_table.get_raw(ddb, naming.table(args.stage, "control")), args.release),
                lambda: S.read().get("traffic"), pins_doc, log=lambda line: _say(line))
            _say(f"purge ready ({ready.case}) after {ready.waited_seconds:.0f} s")
        except memory.PurgeNotReady as exc:
            _say(f"WARN memory purge gave up: {exc}", "yellow")
            return 5
        except P.PinsViolation as exc:
            _say(f"ERROR memory purge: PinsViolation: {exc}; nothing deleted", "red")
            return 5
    # Reconcile against S3: Memory lists a record ~45-90 s after it is written and still lists a
    # deleted one for a while, so the release's side-effect objects are the count N the purge must reach.
    bucket, prefix, notes = _side_effect_keys(ctx, args.stage, args.release)
    record_key = memory.purge_record_key(args.stage, args.release)
    try:
        before = json.loads(s3.get_object(Bucket=ctx.release_bucket, Key=record_key)["Body"].read())
    except s3.exceptions.NoSuchKey:
        before = {}
    removed_before = dict(before.get("removed") or {})
    _say(f"memory purge {memory.namespace(args.stage, args.release)}: {len(notes)} incident note(s) in "
         f"s3://{bucket}/{prefix}" + (f"; {len(removed_before)} removed by earlier purges" if removed_before else ""))
    report = memory.purge(memory_id, stage=args.stage, release=args.release, dry_run=args.dry_run, data_plane=plane,
                          notes=notes, removed_before=removed_before, log=lambda line: _say(line))
    if args.dry_run:
        _say(f"memory purge {report.namespace}: would delete {max(0, len(notes) - len(removed_before))} "
             f"(Memory lists {report.listed} now; its index lags writes)")
        return 0
    releases.put_json(s3, ctx.release_bucket, record_key,
                      memory.purge_record(args.stage, args.release, report, datetime.now(timezone.utc)))
    gone = f", already gone {report.already_gone}" if report.already_gone else ""
    _say(f"memory purge {report.namespace}: deleted {report.deleted}{gone} of {len(notes)} note(s) "
         f"in {report.waited_seconds:.0f} s, failed {len(report.failed)}",
         "green" if report.complete and not report.failed else "yellow")
    if not report.complete:
        _say(f"WARN memory purge incomplete after {memory.SETTLE_SECONDS} s: "
             f"{len(report.missing)} note(s) whose Memory record was not found:", "yellow")
        for key in report.missing:
            _say(f"  s3://{bucket}/{key}", "yellow")
        return 5
    return 0 if not report.failed else 1


# ---------------------------------------------------------------- shadow probe
PROBE_INPUT = ("checkout-service is throwing 502s since the 14:20 deploy. Investigate, then record an incident note "
               "for checkout-service with the probable cause and one log line.")


def cmd_shadow_probe(args) -> int:
    """One shadow invocation of the staging endpoint as the staging eval runner (S7 step 3)."""
    from .aws import params
    from .release import deploy as D
    if args.stage != "staging":
        raise ValueError("shadow probe runs on staging")
    ctx = _ctx(args.root)
    current = params.stage_get(ctx.admin.client("ssm"), "staging", "current")
    dep = current["deployment"]["staging"]
    runner = ctx.assume(naming.eval_runner_role("staging"), "staging", "shadow probe staging")
    core = runner.client("bedrock-agentcore", region_name=ctx.region)
    version = int(dep["endpoints"]["staging"])
    session = f"m11-staging-shadow-v{version}-{uuid.uuid4()}"
    request_id = f"probe-{uuid.uuid4().hex[:12]}"
    envelope = {"identity": {"team": "platform", "principal": "m11-shadow-probe"},
                "request": {"change_summary": PROBE_INPUT, "request_id": request_id},
                "context": {"variant": "shadow", "shadow": True, "manifestVersion": current["manifestVersion"],
                            "behaviorDigest": current["digests"]["behavior"], "release": dep["release"]}}
    _say(f"shadow probe: staging v{version} as {naming.eval_runner_role('staging')}, session {session}")
    response = core.invoke_agent_runtime(agentRuntimeArn=dep["runtimeArn"], qualifier="staging", runtimeSessionId=session,
                                         contentType="application/json", accept="text/event-stream",
                                         payload=json.dumps(envelope).encode())
    text = response["response"].read().decode("utf-8", "replace")
    try:
        core.stop_runtime_session(agentRuntimeArn=dep["runtimeArn"], qualifier="staging", runtimeSessionId=session)
    except Exception:  # noqa: BLE001
        pass
    events = D.sse_events(text)
    tool_events = [d for n, d in events if n == "tool" and isinstance(d, dict) and d.get("tool") == "record_incident_note"]
    result = next((d for n, d in reversed(events) if n in ("result", "error") and isinstance(d, dict)), {})
    in_result = [e for e in result.get("events") or [] if e.get("tool") == "record_incident_note"]
    streamed = any(e.get("shadow_suppressed") for e in tool_events)
    recorded = any(e.get("shadow_suppressed") for e in in_result)
    _say(f"  streamed tool event: record_incident_note shadow_suppressed: {str(streamed).lower()}")
    _say(f"  result events[]: record_incident_note shadow_suppressed: {str(recorded).lower()} (status {result.get('status')})")
    s3 = ctx.admin.client("s3")
    bucket = naming.artifacts_bucket("staging", ctx.account, ctx.region)
    prefix = f"side-effects/staging/{dep['release']}/{session}/"
    objects = s3.list_objects_v2(Bucket=bucket, Prefix=prefix).get("KeyCount", 0)
    _say(f"  S3 side-effect objects under {prefix}: {objects}")
    from .aws import memory
    plane = ctx.admin.client("bedrock-agentcore")
    memory_id = _memory_id(ctx, "staging")
    # sessionId is not an indexed Memory filter key (only service, variant, release are), so the release
    # namespace is listed and matched here; a new record lists ~45-90 s after it is written, so look for 90 s.
    records, end = 0, time.time() + (90 if objects == 0 else 0)
    while True:
        records = sum(1 for _, sid in memory._summaries(memory_id, stage="staging", release=dep["release"],
                                                       data_plane=plane) if sid == session)
        if records or time.time() >= end:
            break
        time.sleep(15)
    _say(f"  Memory records with sessionId {session} (checked for 90 s): {records}")
    ok = streamed and recorded and objects == 0 and records == 0
    _say("shadow probe: no side effects" if ok else "shadow probe: FAILED", "green" if ok else "red")
    if args.json:
        print(json.dumps({"session": session, "streamed": streamed, "recorded": recorded, "objects": objects,
                          "memoryRecords": records, "status": result.get("status")}))
    return 0 if ok else 1


# ---------------------------------------------------------------- flags
def _flags_io(ctx, stage: str):
    from .release import flags_edit as F
    s3 = ctx.admin.client("s3")
    bucket = naming.artifacts_bucket(stage, ctx.account, ctx.region)

    def get():
        obj = s3.get_object(Bucket=bucket, Key="config/flags.json")
        return json.loads(obj["Body"].read()), obj["ETag"]

    def put(body: bytes, etag: str) -> str:
        try:
            return s3.put_object(Bucket=bucket, Key="config/flags.json", Body=body, IfMatch=etag,
                                 ContentType="application/json").get("VersionId")
        except Exception as exc:  # noqa: BLE001
            if "PreconditionFailed" in str(exc) or "412" in str(exc):
                raise F.PreconditionFailed() from exc
            raise
    return s3, bucket, get, put


def _flags_audit(ctx, stage: str, action: str, key: str, team: str | None, before: dict, after: dict) -> None:
    from .aws import ledger
    from .release import flags_edit as F
    actor = ctx.admin.client("sts").get_caller_identity()["Arn"]
    record = F.audit_record(action, key, team, stage, before, after, actor,
                            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    fields = {k: v for k, v in record["details"].items() if k not in ("stage", "kind", "by")}   # put() names these
    ledger.put(ctx.admin.client("s3"), ctx.release_bucket, stage, f"flags-{action}", by=actor, **fields)
    _say(f"audit: {json.dumps(record, sort_keys=True)}")


def cmd_flags_kill(args) -> int:
    from .release import flags_edit as F
    ctx = _ctx(args.root)
    _, _, get, put = _flags_io(ctx, args.stage)
    before, _ = get()
    doc, changed, attempts = F.edit_with_retry(get, put, lambda d: F.kill(d, args.flag, args.team))
    _say(f"flags kill {args.flag} for team {args.team} on {args.stage}: {'changed' if changed else 'already off'}; "
         f"flags hash {F.flags_hash(doc)} ({attempts} attempt(s)). Takes effect within ~10 s; both arms change together.",
         "green")
    if changed:
        _flags_audit(ctx, args.stage, "kill", args.flag, args.team, before, doc)
    return 0


def cmd_flags_restore(args) -> int:
    from .aws import params
    from .release import flags_edit as F
    ctx = _ctx(args.root)
    s3, bucket, get, put = _flags_io(ctx, args.stage)
    current = params.stage_get(ctx.admin.client("ssm"), args.stage, "current")
    version = ((current["deployment"][args.stage].get("flagsObject") or {}).get("versionId"))
    if version:
        declared = json.loads(s3.get_object(Bucket=bucket, Key="config/flags.json", VersionId=version)["Body"].read())
    else:
        from .aws import releases
        declared = json.loads(releases.get(s3, f"s3://{ctx.release_bucket}/releases/{current['manifestVersion']}/flags.json"))
    before, _ = get()
    doc, changed, _ = F.edit_with_retry(get, put, F.restore_to(declared))
    _say(f"flags restore on {args.stage}: {'restored' if changed else 'already the declared document'}; "
         f"flags hash {F.flags_hash(doc)}", "green")
    if changed:
        flag = next(iter((doc.get("flags") or {"-": {}})))
        _flags_audit(ctx, args.stage, "restore", flag, None, before, doc)
    return 0


# ---------------------------------------------------------------- drift
STAND_IN = "stand-in: this Gateway target is ours"


def _invoke_drift(ctx, stage: str) -> dict:
    """The deployed checker `m11-<stage>-drift-check`, synchronously (the same code as the 15-minute schedule)."""
    lam = ctx.admin.client("lambda")
    response = lam.invoke(FunctionName=naming.function(stage, "drift-check"),
                          Payload=json.dumps({"source": "cli"}).encode("utf-8"))
    payload = json.loads(response["Payload"].read() or b"{}")
    if response.get("FunctionError") or not payload.get("ok"):
        raise RuntimeError(f"m11-{stage}-drift-check failed: {payload.get('error') or payload.get('errorType')} "
                           f"{payload.get('detail') or payload.get('errorMessage') or ''}".strip())
    return payload


def external_lines(probe: dict) -> list[tuple[str, str]]:
    """(text, style) lines of the --external block (B6 6b)."""
    out = []
    for model, status in sorted(probe["models"].items()):
        out.append((f"model {model}: {status}", "green" if status == "ACTIVE" else "yellow"))
    fp = probe["builtinFingerprint"]
    if not fp["pinned"]:
        out.append((f"built-in evaluator list fingerprint {fp['observed'][:19]}...: not pinned in evaluators.yaml",
                    "yellow"))
    else:
        same = fp["pinned"] == fp["observed"]
        out.append((f"built-in evaluator list fingerprint: {'unchanged' if same else 'CHANGED'} ({fp['observed'][:19]}...)",
                    "green" if same else "yellow"))
    for s in probe["surfaces"]:
        if s["observed"] is None:
            out.append((f"external surface {s['id']}: not found live ({STAND_IN})", "yellow"))
        elif s["pinned"] == s["observed"]:
            out.append((f"external surface {s['id']}: sha256 matches pin ({STAND_IN})", "green"))
        else:
            out.append((f"external surface {s['id']}: {s['observed']} != pin {s['pinned']} ({STAND_IN})", "yellow"))
    return out


def cmd_drift_check(args) -> int:
    from .aws import drift_reads
    from .release import stage as cfg
    args.external = args.external or args.external_only
    ctx = _ctx(args.root)
    if args.local:
        report = drift_reads.check(stage=args.stage, admin=ctx.admin, account=ctx.account, region=ctx.region,
                                   release_bucket=ctx.release_bucket)
        seconds = None
    else:
        payload = _invoke_drift(ctx, args.stage)
        report, seconds = drift_reads.report_from(payload), payload.get("seconds")
    probe = None
    if args.external:
        probe = drift_reads.external(ctx.admin, models=cfg.models(args.root), evaluators=cfg.evaluators(args.root),
                                     surfaces=cfg.external_surfaces(args.root))
    if not args.external_only:
        for finding in report.findings:
            _say(f"  DRIFT {finding.text()}" if finding.severity == "drift" else f"  [warning] {finding.text()}",
                 "red" if finding.severity == "drift" else "yellow")
        where = "local engine" if args.local else f"m11-{args.stage}-drift-check" + (f", {seconds} s" if seconds else "")
        _say(f"drift check {args.stage} ({where}): {report.drift_count} finding(s)",
             "green" if report.drift_count == 0 else "red")
    if probe is not None:
        for text, style in external_lines(probe):
            _say(f"  {text}", style)
    if args.json:
        print(json.dumps({"report": report.to_dict(), "external": probe}, indent=2, default=str))
    if args.external_only:
        return 0
    return 0 if report.drift_count == 0 else 1


def cmd_drift_revert(args) -> int:
    """Re-apply the declared state as the stage deployer, then re-check. Endpoints: wait out
    UPDATING, retarget (ConflictException retried 3 times 5 s apart), poll until READY with liveVersion == declared.
    Flags: restore the declared object. IAM: `release deploy --reapply`, which re-applies /applied."""
    from .aws import deploy as AD
    from .aws import drift_reads, endpoints as EP
    from .aws import params
    ctx = _ctx(args.root)
    payload = _invoke_drift(ctx, args.stage)
    report = drift_reads.report_from(payload)
    drift = [f for f in report.findings if f.severity == "drift"]
    if not drift:
        _say(f"drift revert {args.stage}: nothing to revert", "green")
        return 0
    deploy = AD.Deploy(ctx, args.stage)
    current = params.stage_get(deploy.ssm, args.stage, "current") or {}
    runtime_id = current["deployment"][args.stage]["runtimeId"]
    reverted, rest = [], []
    for finding in drift:
        if finding.component == "endpoint":
            qualifier, version = finding.field, int(finding.declared)
            _say(f"  endpoint {qualifier}: {finding.observed} ({finding.status or '-'}) -> {version}")
            EP._wait_not_updating(deploy.ctl, runtime_id, qualifier, sleep=time.sleep, clock=time.monotonic,
                                  deadline=EP.DEADLINE_SECONDS)
            got = EP.retarget(deploy.ctl, runtime_id, qualifier, version)
            _say(f"  endpoint {qualifier}: READY on v{got.get('liveVersion')}", "green")
            reverted.append(f"endpoint {qualifier}")
        elif finding.component == "flags":
            rest.append("flags")
        elif finding.component == "iam":
            rest.append("iam")
        else:
            _say(f"  {finding.text()}: no automatic revert; redeploy the declared release", "yellow")
    if "iam" in rest:
        _say("  role policies: release deploy --reapply (re-applies /applied's inputs)")
        deploy.reapply()
        reverted.append("iam")
    if "flags" in rest:
        _say(f"  flags: run `module11 flags restore --stage {args.stage}`", "yellow")
    after = drift_reads.report_from(_invoke_drift(ctx, args.stage))
    _say(f"drift revert {args.stage}: reverted {', '.join(reverted) or 'nothing'}; re-check {after.drift_count} "
         "finding(s)", "green" if after.drift_count == 0 else "red")
    for finding in after.findings:
        _say(f"  {finding.text()}", "red" if finding.severity == "drift" else "yellow")
    return 0 if after.drift_count == 0 else 1


APPROVAL_ACTIONS = (("Approval", "ReleaseApproval"), ("ExtendedApproval", "ExtendedReleaseApproval"))


def approval_target(state: dict, execution_id: str, stage: str | None = None) -> tuple[str, str, str] | None:
    """(stage, action, token) of THIS execution's waiting manual approval, from GetPipelineState. Like
    promote.sh, it never takes a token from a stage whose latest execution is another one."""
    for stage_state in state.get("stageStates", []):
        name = stage_state.get("stageName")
        if stage and name != stage:
            continue
        if (stage_state.get("latestExecution") or {}).get("pipelineExecutionId") != execution_id:
            continue
        for wanted_stage, action in APPROVAL_ACTIONS:
            if name != wanted_stage:
                continue
            for a in stage_state.get("actionStates", []):
                latest = a.get("latestExecution") or {}
                if a.get("actionName") == action and latest.get("status") == "InProgress" and latest.get("token"):
                    return name, action, latest["token"]
    return None


def cmd_release_approve(args) -> int:
    """Break-glass approval of one m11-release execution (audited). The GitLab promote:* jobs are the normal
    path; this one exists for the build and for an operator emergency."""
    import re
    from .aws import ledger, releases
    if not args.break_glass:
        raise RuntimeError("only `release approve --break-glass` exists here; normal approvals happen in GitLab")
    if not re.match(r"^[0-9a-f-]{36}$", args.execution):
        raise ValueError("--execution must be a pipeline execution ID")
    ctx = _ctx(args.root)
    cp = ctx.admin.client("codepipeline")
    caller = ctx.admin.client("sts").get_caller_identity()["Arn"]
    target = approval_target(cp.get_pipeline_state(name="m11-release"), args.execution, args.stage)
    if target is None:
        _say(f"execution {args.execution} is not waiting at a manual approval", "yellow")
        return 1
    stage, action, token = target
    summary = f"break-glass approval by {caller}" + (f": {args.reason}" if args.reason else "")
    result = cp.put_approval_result(pipelineName="m11-release", stageName=stage, actionName=action, token=token,
                                    result={"summary": summary[:512], "status": "Approved"})
    approved_at = result["approvedAt"]
    ledger.put(ctx.admin.client("s3"), releases.bucket_name(ctx.account, ctx.region), "shared", "break-glass-approval",
               by=caller, execution=args.execution, approvalStage=stage, action=action, reason=args.reason or "",
               approvedAt=approved_at.isoformat())
    _say(f"approved {stage}/{action} of {args.execution} at {approved_at.isoformat()} (break-glass, audited)", "green")
    return 0


def register(sub) -> None:
    ro = sub.add_parser("rollout", help="the stage's rollout (control item); state changes go to the controller")
    rsub = ro.add_subparsers(dest="rollout_command", required=True)
    for name, fn in (("show", cmd_rollout_show), ("watch", cmd_rollout_watch), ("tick", cmd_rollout_tick),
                     ("abort", cmd_rollout_abort), ("reset", cmd_rollout_reset)):
        p = rsub.add_parser(name)
        p.add_argument("--stage", required=True, choices=list(naming.STAGES))
        p.set_defaults(fn=fn)
        if name == "show":
            p.add_argument("--json", action="store_true")
        if name == "watch":
            p.add_argument("--interval", type=float, default=10.0)
            p.add_argument("--minutes", type=float, default=15.0)
            p.add_argument("--until-terminal", action="store_true")
        if name == "tick":
            p.add_argument("--times", type=int, default=1)
            p.add_argument("--gap", type=float, default=20.0)
        if name in ("abort", "reset"):
            p.add_argument("--reason", required=True)
        if name == "abort":
            p.add_argument("--no-latch", action="store_true", help="audited shadow-experiment path only")
    tr = sub.add_parser("traffic", help="synthetic incidents through the stage's real edge (S7 shape or until terminal)")
    tr.add_argument("--stage", required=True, choices=list(naming.STAGES))
    mode = tr.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sessions", type=int)
    mode.add_argument("--until-terminal", action="store_true")
    tr.add_argument("--rate", default="20", help="with --until-terminal: sessions per minute, or N/h (e.g. 120/h)")
    tr.add_argument("--max-minutes", type=float, default=120.0)
    tr.add_argument("--hours", type=float, help="with --until-terminal: stop after this many hours (overrides --max-minutes)")
    tr.add_argument("--concurrency", type=int, default=20)
    tr.add_argument("--mix", default="payments=0.6")
    tr.add_argument("--rollout-id", help="the rollout the driver must find WARMING or OBSERVING")
    tr.add_argument("--wait-seconds", type=float, default=30.0)
    tr.set_defaults(fn=cmd_traffic)
    la = sub.add_parser("latch", help="latched behavior digests")
    lsub = la.add_subparsers(dest="latch_command", required=True)
    lc = lsub.add_parser("clear")
    lc.add_argument("digest", help="behavior digest hex (sha256: prefix optional)")
    lc.add_argument("--reason", required=True)
    lc.set_defaults(fn=cmd_latch_clear)
    se = sub.add_parser("side-effects", help="incident notes a release's sessions wrote")
    se.add_argument("--release", required=True)
    se.add_argument("--stage", default="prod", choices=list(naming.STAGES))
    se.add_argument("--limit", type=int, default=20)
    se.set_defaults(fn=cmd_side_effects)
    me = sub.add_parser("memory", help="AgentCore Memory incident notes")
    msub = me.add_subparsers(dest="memory_command", required=True)
    mp = msub.add_parser("purge", help="purge a release's namespace (waits per the purge-wait rule)")
    mp.add_argument("--release", required=True)
    mp.add_argument("--stage", default="prod", choices=list(naming.STAGES))
    mp.add_argument("--dry-run", action="store_true")
    mp.add_argument("--manifest", help="the release's manifestVersion when the control item names another release")
    mp.set_defaults(fn=cmd_memory_purge)
    sh = sub.add_parser("shadow", help="shadow mode (probe; the experiment and report are `shadow experiment` and `shadow report`)")
    shsub = sh.add_subparsers(dest="shadow_command", required=True)
    sp = shsub.add_parser("probe")
    sp.add_argument("--stage", default="staging")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_shadow_probe)
    from . import cli_experiment
    cli_experiment.register_shadow(shsub)
    for later in ("evaluate", "pass", "fail"):
        lp = shsub.add_parser(later, add_help=False)
        lp.add_argument("rest", nargs="*")
        lp.set_defaults(fn=lambda a, n=later: (_say(f"`module11 shadow {n}` is not a command; use `shadow experiment` "
                                                    "and `shadow report`.", "yellow"), 2)[1])
    fl = sub.add_parser("flags", help="the stage's flag object (kill switch)")
    fsub = fl.add_subparsers(dest="flags_command", required=True)
    fk = fsub.add_parser("kill")
    fk.add_argument("flag")
    fk.add_argument("--team", required=True)
    fk.add_argument("--stage", required=True, choices=list(naming.STAGES))
    fk.set_defaults(fn=cmd_flags_kill)
    fr = fsub.add_parser("restore")
    fr.add_argument("--stage", required=True, choices=list(naming.STAGES))
    fr.set_defaults(fn=cmd_flags_restore)
    dr = sub.add_parser("drift", help="drift check (invokes the stage's drift-checker Lambda) and revert")
    dsub = dr.add_subparsers(dest="drift_command", required=True)
    dc = dsub.add_parser("check")
    dc.add_argument("--stage", required=True, choices=list(naming.STAGES))
    dc.add_argument("--external", action="store_true",
                    help="add the model-lifecycle, evaluator-fingerprint and external-surface probes (warnings)")
    dc.add_argument("--external-only", action="store_true", help="print only the --external block (B6 6b)")
    dc.add_argument("--local", action="store_true", help="run the engine here instead of invoking the Lambda")
    dc.add_argument("--json", action="store_true")
    dc.set_defaults(fn=cmd_drift_check)
    dv = dsub.add_parser("revert", help="re-apply the declared endpoints and role policies as the stage deployer")
    dv.add_argument("--stage", required=True, choices=list(naming.STAGES))
    dv.set_defaults(fn=cmd_drift_revert)

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The AWS-backed operator CLI commands (registered by cli.py):

  image context [--upload]                 the image build context (closure archive)
  release seal --operator [--ref HEAD]     the Seal stage's code, run locally by an operator (audited)
  release deploy --stage S [--manifest MV] [--reapply]
  release prune --stage S
  status --all | --stage S                 endpoints READY and live == deployed manifest; CANARY (exit 0) when the
                                           only difference is an open rollout's candidate on `candidate`
  versions --stage S                       runtime versions, endpoints, bundle versions
  resources --stage S --kind guardrail,identity,deployer
  manifest diff --deployed S               declared (working tree or --declared ref) vs the stage's /current
  policy refresh-actions                   regenerate no_resource_actions.json from the Service Reference
  prices refresh [--write]                 Bedrock token prices for models.yaml from the AWS Price List

Every command checks that the caller's account equals M11_EXPECTED_ACCOUNT when that is set.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from .release import artifacts as A
from .release import naming

IMAGE_BUILDSPEC = "module11/pipeline/buildspecs/image.yml"


def _say(text: str, style: str | None = None) -> None:
    from .cli import say
    say(text, style)


def _admin():
    import boto3
    return boto3.session.Session(region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))


def _context(root: Path):
    from .aws import deploy as AD
    admin = _admin()
    identity = admin.client("sts").get_caller_identity()
    expected = os.environ.get("M11_EXPECTED_ACCOUNT")
    if expected and identity["Account"] != expected:
        raise RuntimeError("the caller's account differs from M11_EXPECTED_ACCOUNT; check AWS_PROFILE")
    operators = [p.strip() for p in os.environ.get("M11_OPERATOR_PRINCIPALS", "").split(",") if p.strip()]
    return AD.Context(root=root, account=identity["Account"], region=admin.region_name or "us-east-1",
                      operators=operators, log=lambda line: _say(line), admin=admin)


# ---------------------------------------------------------------- image context / seal
def cmd_image_context(args) -> int:
    from .release import seal as SEAL
    from .release.hashing import hex_of
    from .cli import tree_at
    with tree_at(args.root, args.ref) as tree:
        data = SEAL.image_context(tree)
        context_hash = A.compute(tree).hashes["runtime/code"]
    out = args.root / "module11/.local" / f"image-context-{hex_of(context_hash)[:16]}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    _say(f"image context {context_hash} ({len(data)} bytes) -> {out}")
    if args.upload:
        from .aws import releases
        ctx = _context(args.root)
        written = releases.put_absent(ctx.admin.client("s3"), ctx.release_bucket, f"builds/{hex_of(context_hash)}.zip",
                                      data, content_type="application/zip")
        _say(f"builds/{hex_of(context_hash)}.zip {'uploaded' if written else 'already present'}")
    return 0


def cmd_release_seal(args) -> int:
    """Seal as the operator before the pipeline exists: zips, layer, inputs, component,
    snapshot, evidence, manifest and change plan to the release bucket; the image via m11-image."""
    if not args.operator:
        raise RuntimeError("the pipeline's Seal stage needs enablePipeline=true; run `release seal --operator`")
    from .aws import ledger, params, releases
    from .cli import git_info, tree_at
    from .release import detect as DT
    from .release import manifest as M
    from .release import seal as SEAL
    from .release import stage as cfg
    from .release.hashing import hex_of
    ctx = _context(args.root)
    s3, ssm = ctx.admin.client("s3"), ctx.admin.client("ssm")
    git = git_info(args.root, args.ref)
    started = time.time()
    with tree_at(args.root, args.ref) as tree:
        declared = M.build_declared(tree, git)
        mv = declared["manifestVersion"]
        context = SEAL.image_context(tree)
        context_hex = hex_of(declared["build"]["imageContext"])
        releases.put_absent(s3, ctx.release_bucket, f"builds/{context_hex}.zip", context, content_type="application/zip")
        digest = releases.build_image(ctx.admin.client("codebuild"), ctx.admin.client("ecr"), project="m11-image",
                                      repository=naming.IMAGE_REPOSITORY, context_hex=context_hex, tag=context_hex,
                                      log=_say, buildspec=(args.root / IMAGE_BUILDSPEC).read_text(encoding="utf-8"))
        layer = SEAL.build_layer(tree / A.LAMBDA_REQUIREMENTS)
        ops_layer = SEAL.build_layer(tree / SEAL.OPS_REQUIREMENTS) if SEAL.has_operations(tree) else None
        prod = params.stage_get(ssm, "prod", "current")
        rules, unknown, _ = cfg.change_types(tree)
        base = json.loads(releases.get(s3, prod["s3Uri"])) if prod else None
        plan = DT.detect(M.hash_set(declared), M.hash_set(base) if base else None, rules, unknown,
                         base_manifest_version=(prod or {}).get("manifestVersion"), new_tools=declared.get("tools"),
                         base_tools=(base or {}).get("tools"))
        objects, record, final = SEAL.seal_objects(tree, declared, image_digest=digest, layer_zip=layer,
                                                   change_plan=plan.to_dict(), sealed_by=ctx.admin.client("sts")
                                                   .get_caller_identity()["Arn"],
                                                   sealed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                                   ops_layer_zip=ops_layer)
    counts = releases.upload(s3, ctx.release_bucket, objects)
    record_bytes = json.dumps(record, indent=2, sort_keys=True).encode("utf-8")
    releases.put_absent(s3, ctx.release_bucket, f"releases/{mv}/seal.json", record_bytes, content_type="application/json")
    ledger.put(s3, ctx.release_bucket, "shared",
               "operator-seal", by=record["sealedBy"], manifestVersion=mv, commit=git["commit"],
               imageDigest=digest, objects=counts)
    _say(f"sealed {mv}: {counts['written']} objects written, {counts['present']} already present; "
         f"image {digest[:19]}…; plan {plan.releaseKind}/{plan.rollout} ({int(time.time() - started)} s)", "green")
    _say(f"behaviorDigest {final['digests']['behavior']}")
    return 0


# ---------------------------------------------------------------- deploy / prune
def cmd_release_deploy(args) -> int:
    from .aws import deploy as AD
    ctx = _context(args.root)
    exclude_ui, as_caller = getattr(args, "exclude_ui", False), getattr(args, "as_caller", False)
    if (exclude_ui or as_caller) and not naming.is_mr(args.stage):
        raise RuntimeError("--exclude-ui and --as-caller are for mr-* stacks (preview:deploy-infra)")
    manifest = args.manifest
    if exclude_ui and not manifest:          # the MR's own tooling re-deploys the release the normal preview sealed
        from .aws import params
        current = params.stage_get(ctx.admin.client("ssm"), args.stage, "current")
        if not current:
            raise RuntimeError(f"{args.stage} has no /current: preview:deploy-infra needs the normal preview first")
        manifest = current["manifestVersion"]
    deploy = AD.Deploy(ctx, args.stage, exclude_ui=exclude_ui, as_caller=as_caller)
    result = deploy.reapply() if args.reapply else deploy.run(manifest)
    if args.reapply:
        _say(f"reapplied {args.stage}", "green")
    else:
        _say(f"deployed {result['manifestVersion']} to {args.stage}: path {result['path']}, runtime v"
             f"{result['runtimeVersion']}, endpoints {result['endpoints']} in {result['seconds']} s", "green")
    return 0


def cmd_release_destroy(args) -> int:
    """Destroy a stage's stack as its deployer. mr-* stacks any time; dev/staging/prod need --confirm-stage."""
    from .aws import deploy as AD
    if not naming.is_mr(args.stage) and args.confirm_stage != args.stage:
        raise RuntimeError(f"destroying {args.stage} needs --confirm-stage {args.stage}")
    ctx = _context(args.root)
    result = AD.Deploy(ctx, args.stage).destroy(remove_stack=not args.keep_stack)
    _say(f"destroyed {args.stage} in {result['seconds']} s", "green")
    return 0


def cmd_release_prune(args) -> int:
    from .aws import deploy as AD
    ctx = _context(args.root)
    result = AD.Deploy(ctx, args.stage).prune()
    _say(f"pruned {args.stage}: granted tool versions {result['granted']['toolArns']}", "green")
    return 0


# ---------------------------------------------------------------- status / versions / resources
def stage_status(ctx, stage: str) -> dict:
    from .aws import endpoints, params
    ssm, ctl = ctx.admin.client("ssm"), ctx.admin.client("bedrock-agentcore-control")
    current = params.stage_get(ssm, stage, "current")
    if not current:
        return {"stage": stage, "state": "NOT DEPLOYED"}
    dep = current["deployment"][stage]
    rows, raw, ready = {}, {}, True
    for name, version in dep["endpoints"].items():
        ep = endpoints.get(ctl, dep["runtimeId"], name)
        ok = endpoints.settled(ep, version)
        ready &= ok
        raw[name] = ep
        rows[name] = {"status": ep.get("status"), "liveVersion": ep.get("liveVersion"),
                      "targetVersion": ep.get("targetVersion"), "deployed": version, "ok": ok}
    row = {"stage": stage, "manifestVersion": current["manifestVersion"], "release": dep.get("release"),
           "runtimeId": dep["runtimeId"], "endpoints": rows, "state": "READY" if ready else "NOT READY"}
    rollout = None if ready else _open_canary(ctx, stage, rows, raw, dep)
    if rollout is not None:
        row["state"], row["rollout"] = "CANARY", rollout
        rows["candidate"]["canary"] = True
    return row


def _open_canary(ctx, stage: str, rows: dict, raw: dict, dep: dict) -> dict | None:
    """The one mismatch an open canary explains: only the `candidate` endpoint is off the record, and it serves
    exactly the candidate version of the stage's open rollout (WARMING, OBSERVING or ADVANCING). Read from the
    control row the way `rollout show` reads it; any failed read leaves the stage NOT READY."""
    from . import cli_ops as OPS
    from .aws import endpoints
    from .release.router_spec import CANDIDATE_STATES
    if [n for n, e in rows.items() if not e["ok"]] != ["candidate"]:
        return None
    try:
        item = OPS._control(ctx, stage) or {}
    except Exception:  # noqa: BLE001 - an unreadable control row is not an open canary
        return None
    rid = item.get("rolloutId")
    if item.get("state") not in CANDIDATE_STATES or not rid or item.get("candidateVersion") is None \
            or not endpoints.settled(raw["candidate"], item["candidateVersion"]):
        return None
    try:
        sessions = sum(c["n"] for c in OPS._arm_counts(ctx, stage, rid).values())
    except Exception:  # noqa: BLE001 - the count is shown as `?`
        sessions = None
    return {"rolloutId": rid, "state": item.get("state"), "canaryPct": item.get("canaryPct", 0),
            "candidateVersion": item.get("candidateVersion"), "candidateRelease": item.get("release"),
            "stableVersion": item.get("stableVersion"),
            "stableRelease": ((item.get("endpoints") or {}).get("stable") or {}).get("release") or dep.get("release"),
            "sessions": sessions}


def _endpoint_text(name: str, e: dict) -> str:
    if e.get("canary"):
        return f"{name} v{e['liveVersion']} (canary)"
    return f"{name} v{e['liveVersion']}{'' if e['ok'] else ' (' + str(e['status']) + ')'}"


def cmd_status(args) -> int:
    ctx = _context(args.root)
    stages = list(naming.STAGES) if args.all or not args.stage else [args.stage]
    bad = 0
    for stage in stages:
        row = stage_status(ctx, stage)
        if row["state"] == "NOT DEPLOYED":
            _say(f"{stage:<8} NOT DEPLOYED", "yellow")
            bad += 1
            continue
        eps = ", ".join(_endpoint_text(n, e) for n, e in row["endpoints"].items())
        style = {"READY": "green", "CANARY": "cyan"}.get(row["state"], "red")
        _say(f"{stage:<8} {row['state']:<9} {row['manifestVersion']}  {row['release']}  {eps}", style)
        if row["state"] == "CANARY":
            r = row["rollout"]
            n = "?" if r["sessions"] is None else r["sessions"]
            _say(f"{'':<8} rollout {r['rolloutId']} {r['state']}, canary {r['canaryPct']}%: candidate "
                 f"{r['candidateRelease']} v{r['candidateVersion']} vs stable {r['stableRelease']} "
                 f"v{r['stableVersion']}, {n} sessions so far", style)
        bad += row["state"] not in ("READY", "CANARY")
    return 1 if bad else 0


def cmd_versions(args) -> int:
    from .aws import bundles, endpoints, params
    ctx = _context(args.root)
    ssm, ctl = ctx.admin.client("ssm"), ctx.admin.client("bedrock-agentcore-control")
    current = params.stage_get(ssm, args.stage, "current")
    if not current:
        _say(f"{args.stage} is not deployed", "yellow")
        return 1
    dep = current["deployment"][args.stage]
    _say(f"runtime {dep['runtimeId']} versions: {', '.join(endpoints.versions(ctl, dep['runtimeId']))}")
    for name in dep["endpoints"]:
        ep = endpoints.get(ctl, dep["runtimeId"], name)
        _say(f"  endpoint {name}: live {ep.get('liveVersion')} target {ep.get('targetVersion')} {ep.get('status')}")
    for v in bundles.list_versions(ctl, dep["bundle"]["arn"]):
        mark = " <- /current" if v["versionId"] == dep["bundle"]["versionId"] else ""
        parents = (v.get("lineageMetadata") or {}).get("parentVersionIds") or []
        _say(f"  bundle {v['versionId']} parents {parents}{mark}")
    _say(f"guardrail {dep['guardrail']['id']} v{dep['guardrail']['version']}; tools "
         + ", ".join(f"{t}:{a.rsplit(':', 1)[-1]}" for t, a in sorted(dep["toolArns"].items())))
    return 0


def cmd_resources(args) -> int:
    from .aws import params
    ctx = _context(args.root)
    kinds = set(args.kind.split(","))
    ssm, iam = ctx.admin.client("ssm"), ctx.admin.client("iam")
    current = params.stage_get(ssm, args.stage, "current")
    dep = (current or {}).get("deployment", {}).get(args.stage, {})
    if "guardrail" in kinds and dep:
        g = ctx.admin.client("bedrock").get_guardrail(guardrailIdentifier=dep["guardrail"]["id"],
                                                     guardrailVersion=dep["guardrail"]["version"])
        _say(f"guardrail {g['name']} {dep['guardrail']['id']} version {g['version']} ({g['status']}): {g.get('description')}")
    if "identity" in kinds:
        names = [naming.role(args.stage, n) for n in ("runtime", "adapter", "control", "authorizer")]
        names += [naming.eval_runner_role(args.stage)]
        for name in names:
            try:
                iam.get_role(RoleName=name)
                policies = iam.list_role_policies(RoleName=name)["PolicyNames"]
                _say(f"role {name}: inline {policies}")
            except Exception:
                _say(f"role {name}: absent", "yellow")
    if "deployer" in kinds:
        name = naming.deployer_role(args.stage)
        attached = iam.list_attached_role_policies(RoleName=name)["AttachedPolicies"]
        _say(f"deployer {name}: managed {[p['PolicyName'] for p in attached]} (the only path into {args.stage})")
    return 0


def cmd_manifest_diff_deployed(args, declared: dict) -> int:
    from .aws import params, releases
    from .release import manifest as M
    ctx = _context(args.root)
    current = params.stage_get(ctx.admin.client("ssm"), args.deployed, "current")
    if not current:
        raise RuntimeError(f"{args.deployed} has no /current")
    deployed = json.loads(releases.get(ctx.admin.client("s3"), current["s3Uri"]))
    print(json.dumps({"deployed": current["manifestVersion"], **M.diff(deployed, declared)}, indent=2))
    return 0


def cmd_policy_refresh(args) -> int:
    from .infrastructure.policy import refresh
    doc = refresh.refresh()
    _say(f"no_resource_actions.json: {len(doc['noResource'])} actions with no resource type")
    return 0


def cmd_prices_refresh(args) -> int:
    from .aws import pricing
    ctx = _context(args.root)
    rows = pricing.refresh(ctx.admin.client("pricing", region_name="us-east-1"), args.root, ctx.region,
                           write=args.write)
    for model, row in rows.items():
        _say(f"{model}: {row}")
    return 0


def register(sub) -> None:
    img = sub.add_parser("image", help="the runtime image build context")
    isub = img.add_subparsers(dest="image_command", required=True)
    ic = isub.add_parser("context")
    ic.add_argument("--ref", default="HEAD")
    ic.add_argument("--upload", action="store_true")
    ic.set_defaults(fn=cmd_image_context)
    rel = sub.add_parser("release", help="seal, deploy and prune releases")
    rsub = rel.add_subparsers(dest="release_command", required=True)
    rs = rsub.add_parser("seal")
    rs.add_argument("--operator", action="store_true", help="run the Seal stage's code locally (audited)")
    rs.add_argument("--ref", default="HEAD")
    rs.set_defaults(fn=cmd_release_seal)
    rd = rsub.add_parser("deploy")
    rd.add_argument("--stage", required=True)
    rd.add_argument("--manifest", help="manifestVersion (default: the newest sealed release)")
    rd.add_argument("--reapply", action="store_true")
    rd.add_argument("--exclude-ui", action="store_true",
                    help="mr-*: leave the browser client, its branding and the Vercel variables alone (pulumi --exclude)")
    rd.add_argument("--as-caller", action="store_true",
                    help="mr-*: deploy with the caller's own role (m11-preview-infra assumes no role)")
    rd.set_defaults(fn=cmd_release_deploy)
    rx = rsub.add_parser("destroy", help="destroy a stage's stack as its deployer, then stack rm")
    rx.add_argument("--stage", required=True)
    rx.add_argument("--confirm-stage", help="required (= --stage) for dev, staging and prod")
    rx.add_argument("--keep-stack", action="store_true")
    rx.set_defaults(fn=cmd_release_destroy)
    rp = rsub.add_parser("prune")
    rp.add_argument("--stage", required=True)
    rp.set_defaults(fn=cmd_release_prune)
    from .cli_ops import cmd_release_approve
    ra = rsub.add_parser("approve", help="break-glass approval of one m11-release execution (audited)")
    ra.add_argument("--execution", required=True)
    ra.add_argument("--break-glass", action="store_true")
    ra.add_argument("--stage", choices=["Approval", "ExtendedApproval"], help="default: whichever is waiting")
    ra.add_argument("--reason", default="")
    ra.set_defaults(fn=cmd_release_approve)
    st = sub.add_parser("status", help="endpoints READY and live == deployed manifest")
    st.add_argument("--all", action="store_true")
    st.add_argument("--stage")
    st.set_defaults(fn=cmd_status)
    ve = sub.add_parser("versions")
    ve.add_argument("--stage", required=True)
    ve.set_defaults(fn=cmd_versions)
    re_ = sub.add_parser("resources")
    re_.add_argument("--stage", required=True)
    re_.add_argument("--kind", default="guardrail,identity,deployer")
    re_.set_defaults(fn=cmd_resources)
    pol = sub.add_parser("policy", help="CrossGuard pack helpers")
    psub = pol.add_subparsers(dest="policy_command", required=True)
    psub.add_parser("refresh-actions").set_defaults(fn=cmd_policy_refresh)
    pr = sub.add_parser("prices", help="model prices")
    prsub = pr.add_subparsers(dest="prices_command", required=True)
    prr = prsub.add_parser("refresh")
    prr.add_argument("--write", action="store_true", help="write harness/evaluation/prices.yaml")
    prr.set_defaults(fn=cmd_prices_refresh)

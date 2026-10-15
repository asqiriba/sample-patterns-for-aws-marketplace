# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""m11-platform (stack `shared`), applied by an operator.

Foundation increment (`enablePipeline=false`): ECR repositories, the release and
pipeline-artifact buckets, the m11-image build, the stage deployer roles, the preview deployer for
`mr-*` stacks, the eval-runner roles, the `m11-mr-boundary` policy, the shared-outputs parameter and
the Module 10 stack reference. Also, independent of the pipeline: the prebuilt CodeBuild image
repository `m11/build-image` (lever 4) and, with `createGitlabConnection`, the PENDING GitLab
connection.

Pipeline increment (`enablePipeline=true`, once the connection is AVAILABLE): CodePipeline V2
`m11-release` with Source on GitLab, the CodeBuild projects with their roles, caches and log
groups, the GitLab OIDC provider and roles, `m11-operator-experiment`, the freeze parameter and
check, the `m11-gitlab-notes` broker and the `m11-releases` bus. Config: `buildImage` (the
`sha256:` digest of m11/build-image), `gitlabProject`, `gitlabProjectId`, optional
`gitlabOidcProviderArn` / `gitlabOidcAudience` and `notesCredentialSecret`.

Apply guard: a non-preview run needs a clean working tree whose HEAD equals `origin/main`, because
these buildspecs and roles decide what every later build may do (H3). Until the harness remote's
`main` holds this code, operators apply with `--config m11-platform:bootstrapApply=true`, which skips
the check and writes an audit record.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pulumi  # noqa: E402
import pulumi_aws as aws  # noqa: E402

from module11.infrastructure.platform import builds, events, oidc, pipeline, pipeline_roles as PR, roles as R  # noqa: E402
from module11.release import audit, naming  # noqa: E402

config = pulumi.Config()
STACK = pulumi.get_stack()
caller = aws.get_caller_identity()
account = caller.account_id
region = aws.get_region().region
expected = config.get("accountId")
if expected and expected != account:
    raise pulumi.RunError(f"accountId {expected} does not match the caller's account")
operator_principals = config.get_object("operatorPrincipals") or []
enable_pipeline = config.get_bool("enablePipeline") or False
create_gitlab_connection = config.get_bool("createGitlabConnection") or False
bootstrap_apply = config.get_bool("bootstrapApply") or False
module10_stack = config.get("module10Stack") or "organization/module10-exposure/workshop"
gitlab_project = config.get("gitlabProject") or os.environ.get("M11_GITLAB_PROJECT", "")
if not gitlab_project:
    raise pulumi.RunError("set the GitLab project path: `pulumi config set gitlabProject <group>/<project>` "
                          "or M11_GITLAB_PROJECT")
gitlab_project_id = config.get("gitlabProjectId") or ""
build_image_digest = config.get("buildImage") or ""
oidc_provider_arn = config.get("gitlabOidcProviderArn") or ""
oidc_audience = config.get("gitlabOidcAudience") or "sts.amazonaws.com"
notes_secret = config.get("notesCredentialSecret") or PR.GITLAB_TOKEN_SECRET
tags = {"Project": naming.PROJECT_TAG, "Stage": "shared"}
PARTNER_OUTPUTS = ("vercelProjectId", "vercelTeamId", "vercelTeamSlug", "botLogin", "botCommitEmail", "productionDomain")


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=False).stdout.strip()


def apply_guard() -> None:
    if pulumi.runtime.is_dry_run():
        return
    if bootstrap_apply:
        record = audit.ledger_entry("platform-bootstrap-apply", "shared", time.time(), caller.arn,
                                    commit=_git("rev-parse", "HEAD"), dirty=bool(_git("status", "--porcelain")),
                                    reason="bootstrap: the harness remote main does not hold this code yet")
        out = ROOT / "module11" / ".local" / "audit"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"platform-{audit.stamp(time.time())}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        pulumi.log.warn("bootstrapApply=true: the clean-tree / origin/main check is skipped (audit record written)")
        return
    if _git("status", "--porcelain") or _git("rev-parse", "HEAD") != _git("rev-parse", "origin/main"):
        raise pulumi.RunError("m11-platform applies only from a clean checkout whose HEAD is origin/main "
                              "(or with --config m11-platform:bootstrapApply=true while bootstrapping)")


apply_guard()
if enable_pipeline:
    if not create_gitlab_connection:
        raise pulumi.RunError("enablePipeline=true needs createGitlabConnection=true (the Source is GitLab)")
    if not build_image_digest.startswith("sha256:") or not gitlab_project_id:
        raise pulumi.RunError("enablePipeline=true needs buildImage (sha256:...) and gitlabProjectId in config")
# The connection depends on this flag alone, so it can exist (PENDING) before the pipeline.
gitlab_connection = pipeline.connection(tags) if create_gitlab_connection else None

module10 = pulumi.StackReference(module10_stack)
state_key = aws.kms.get_alias(name=naming.SECRETS_ALIAS).target_key_arn
preview_key = aws.kms.get_alias(name=naming.PREVIEW_SECRETS_ALIAS).target_key_arn

image_repo, preview_repo = builds.repositories(tags)
build_image_repo = builds.build_image_repository(tags)
release_bucket, pipeline_bucket = builds.buckets(account, region, tags)
ctx_out = pulumi.Output.all(module10.require_output("userPoolId"), module10.require_output("stateTable")).apply(
    lambda v: R.Ctx(account=account, region=region, state_key_arn=state_key, preview_key_arn=preview_key,
                    user_pool_id=v[0], state_table=v[1]))
pctx = pulumi.Output.all(ctx_out, gitlab_connection.arn if gitlab_connection else "").apply(
    lambda v: PR.PipelineCtx(v[0], v[1] or f"arn:aws:codeconnections:{region}:{account}:connection/none",
                             gitlab_project, oidc_audience))


def _log_group(project: str) -> aws.cloudwatch.LogGroup:
    logical = "image-build-logs" if project == builds.IMAGE_PROJECT else f"{project}-logs"
    return aws.cloudwatch.LogGroup(logical, name=f"/aws/codebuild/{project}", retention_in_days=builds.LOG_RETENTION_DAYS,
                                   tags=tags)


def _codebuild_role(name: str, logical: str) -> aws.iam.Role:
    return aws.iam.Role(logical, name=name, assume_role_policy=json.dumps(R.service_trust("codebuild.amazonaws.com", account)),
                        tags={**tags, "M11Role": "deployer" if name in ("m11-preview", "m11-preview-infra") else "build"})


# ---------------------------------------------------------------- m11-image
image_logs = _log_group(builds.IMAGE_PROJECT)
image_role = _codebuild_role("m11-image-build", "image-build-role")
image_policy = aws.iam.RolePolicy("image-build-policy", role=image_role.id, policy=pulumi.Output.all(
    ctx_out, image_repo.arn).apply(lambda v: json.dumps(R.image_build_policy(v[0], v[1], f"/aws/codebuild/{builds.IMAGE_PROJECT}"))))
# The build image (lever 4) is pushed by m11-image too; with the pipeline the project also reads its input artifact.
image_extra = aws.iam.RolePolicy("image-build-extra", role=image_role.id, policy=pctx.apply(lambda c: json.dumps(R.doc(
    PR.image_policy_extra(c) if enable_pipeline else PR.image_policy_extra(c)[-1:]))))
image_project = builds.image_build(role_arn=image_role.arn, repository=image_repo, release_bucket=release_bucket,
                                   log_group=image_logs, tags=tags, depends_on=[image_policy, image_extra],
                                   pipeline=enable_pipeline,
                                   cache_location=pipeline_bucket.bucket.apply(lambda b: f"{b}/cache/{builds.IMAGE_PROJECT}")
                                   if enable_pipeline else None)

# ---------------------------------------------------------------- pipeline: projects, roles, Lambdas, pipeline
project_roles: dict[str, aws.iam.Role] = {}
if enable_pipeline:
    freeze_param = events.freeze_parameter(tags)
    release_bus = events.bus(tags)
    image_uri = build_image_repo.repository_url.apply(lambda u: f"{u}@{build_image_digest}")
    common_env = [
        {"name": "RELEASE_BUCKET", "value": release_bucket.bucket},
        {"name": "PIPELINE_BUCKET", "value": pipeline_bucket.bucket},
        {"name": "M11_EXPECTED_ACCOUNT", "value": account},
        {"name": "M11_GITLAB_PROJECT", "value": gitlab_project},
        {"name": "M11_GITLAB_PROJECT_ID", "value": gitlab_project_id},
        {"name": "M11_GITLAB_TOKEN_SECRET", "value": PR.GITLAB_TOKEN_SECRET},
        {"name": "M11_NOTES_FUNCTION", "value": PR.NOTES_FUNCTION},
        {"name": "M11_OPERATOR_PRINCIPALS", "value": ",".join(operator_principals)},
        {"name": "M11_MODULE10_STACK", "value": module10_stack},
        {"name": "M11_SOURCE_PROVIDER", "value": "gitlab"},
    ]
    projects = {}
    for name in builds.PROJECT_SPECS:
        role = _codebuild_role(name, f"{name}-role")
        project_roles[name] = role
        # The literal role name (not role.id) lets CrossGuard judge the policy by its role at preview time too.
        inline = aws.iam.RolePolicy(f"{name}-policy", role=name,
                                    policy=pctx.apply(lambda c, n=name: json.dumps(PR.PROJECT_POLICIES[n](c))),
                                    opts=pulumi.ResourceOptions(depends_on=[role]))
        depends = [inline]
        if name == "m11-preview-infra":
            for part in R.PREVIEW_PARTS:
                managed = aws.iam.Policy(f"{name}-{part}", name=f"{name}-{part}",
                                         policy=ctx_out.apply(lambda c, p=part: json.dumps(R.preview_infra_policies(c)[f"m11-preview-infra-{p}"])),
                                         tags={"Project": naming.PROJECT_TAG, "Stage": "mr", "M11Role": "deployer"})
                depends.append(aws.iam.RolePolicyAttachment(f"{name}-{part}", role=role.name, policy_arn=managed.arn))
        env = common_env + ([{"name": "REPOSITORY_URI", "value": preview_repo.repository_url},
                             {"name": "REPOSITORY_NAME", "value": preview_repo.name}] if name == "m11-preview-image" else [])
        projects[name] = builds.project(name, role_arn=role.arn, image=image_uri, pipeline_bucket=pipeline_bucket,
                                        env=env, log_group=_log_group(name), tags=tags, depends_on=depends,
                                        standard_image=name not in PR.BUILD_IMAGE_PROJECTS)

    # The two Lambdas.
    notes_arn = aws.secretsmanager.get_secret(name=notes_secret).arn
    freeze_fn = events.freeze_check(account, pctx.apply(lambda c: json.dumps(PR.freeze_check_policy(c))), tags)
    notes_fn = events.notes_broker(account, pctx.apply(lambda c: json.dumps(PR.notes_policy(c, notes_secret))),
                                   project_id=gitlab_project_id, secret_arn=notes_arn,
                                   release_bucket=release_bucket.bucket, tags=tags,
                                   group=gitlab_project.rsplit("/", 1)[0])
    # Corpus events become GitLab change requests.
    bot_token_arn = aws.secretsmanager.get_secret(name=PR.GITLAB_TOKEN_SECRET).arn
    corpus_dlq = events.corpus_dlq(tags)
    corpus_fn = events.corpus_handler(
        account, pulumi.Output.all(pctx, corpus_dlq.arn).apply(lambda v: json.dumps(PR.corpus_handler_policy(v[0], v[1]))),
        bus=release_bus, dlq=corpus_dlq, project_id=gitlab_project_id, secret_arn=bot_token_arn,
        release_bucket=release_bucket.bucket, tags=tags)

    # GitLab OIDC and the experiment operator.
    provider_arn = oidc.provider(oidc_provider_arn or None, oidc_audience, tags)
    gitlab_roles = oidc.roles(pctx, provider_arn, tags)
    operator_experiment = aws.iam.Role("operator-experiment", name="m11-operator-experiment",
                                       assume_role_policy=json.dumps(R.role_trust(operator_principals)),
                                       description="Starts m11-seal-experiment with SOURCE_SHA and EXPERIMENT_ID only (4.4.14)",
                                       tags={**tags, "M11Role": "operator"})
    aws.iam.RolePolicy("operator-experiment-policy", role=operator_experiment.id,
                       policy=pctx.apply(lambda c: json.dumps(PR.operator_experiment_policy(c))))

    # The pipeline.
    pipeline_role = aws.iam.Role("pipeline-role", name=PR.PIPELINE_ROLE,
                                 assume_role_policy=json.dumps(R.service_trust("codepipeline.amazonaws.com", account)),
                                 tags={**tags, "M11Role": "pipeline"})
    pipeline_policy = aws.iam.RolePolicy("pipeline-policy", role=pipeline_role.id,
                                         policy=pctx.apply(lambda c: json.dumps(PR.pipeline_policy(c))))
    release_pipeline = pipeline.pipeline(role=pipeline_role, bucket=pipeline_bucket, connection_arn=gitlab_connection.arn,
                                         repository=gitlab_project, tags=tags,
                                         depends_on=[pipeline_policy, image_project, freeze_fn, *projects.values()])


def _trust(role_names: tuple[str, ...]) -> pulumi.Input[str]:
    """Operator template, plus the named CodeBuild roles once they exist (IAM refuses unknown principals)."""
    roles_ = [project_roles[n] for n in role_names if n in project_roles]
    if not roles_:
        return json.dumps(R.role_trust(operator_principals))
    return pulumi.Output.all(*[r.arn for r in roles_]).apply(lambda arns: json.dumps(R.role_trust(operator_principals, arns)))


# ---------------------------------------------------------------- boundary, deployers, eval runners
boundary = aws.iam.Policy("mr-boundary", name=naming.BOUNDARY, description="Permissions boundary for mr-* stack roles",
                          policy=pulumi.Output.all(ctx_out, image_repo.arn, preview_repo.arn).apply(
                              lambda v: json.dumps(R.mr_boundary(v[0], [v[1], v[2]]))),
                          tags={**tags, "M11Role": "boundary"})
DEPLOYER_CALLERS = {"dev": (), "staging": ("m11-deploy",), "prod": ("m11-deploy", "m11-bake"), "mr": ("m11-preview",)}
# m11-preview reaches prod's eval runner only for the MR gate's latency control arm (evaluation/control.py).
EVAL_CALLERS = {"dev": (), "staging": ("m11-deploy", "m11-gate"),
                "prod": ("m11-deploy", "m11-gate", "m11-bake", "m11-preview")}


def _deployer(name: str, stage_tag: str, policies, parts=R.OPS_PARTS) -> aws.iam.Role:
    role = aws.iam.Role(name, name=name, assume_role_policy=_trust(DEPLOYER_CALLERS[stage_tag]),
                        max_session_duration=R.DEPLOYER_SESSION_SECONDS,
                        description=f"The only path into {stage_tag}: release deploy / prune (design 4.5.1)",
                        tags={"Project": naming.PROJECT_TAG, "Stage": stage_tag, "M11Role": "deployer"})
    for part in parts:
        policy = aws.iam.Policy(f"{name}-{part}", name=f"{name}-{part}",
                                policy=policies.apply(lambda docs, p=part: json.dumps(docs[f"{name}-{p}"])),
                                tags={"Project": naming.PROJECT_TAG, "Stage": stage_tag, "M11Role": "deployer"})
        aws.iam.RolePolicyAttachment(f"{name}-{part}", role=role.name, policy_arn=policy.arn)
    return role


deployers = {stage: _deployer(naming.deployer_role(stage), stage,
                              ctx_out.apply(lambda c, s=stage: R.deployer_policies(c, s)))
             for stage in naming.STAGES}
preview_deployer = _deployer("m11-preview-deployer", "mr", ctx_out.apply(R.preview_deployer_policies), R.PREVIEW_PARTS)
eval_runners = {stage: aws.iam.Role(f"eval-runner-{stage}", name=naming.eval_runner_role(stage),
                                    assume_role_policy=_trust(EVAL_CALLERS[stage]),
                                    max_session_duration=R.DEPLOYER_SESSION_SECONDS,
                                    description=f"Invokes and evaluates {stage} (permissions from m11-harness {stage})",
                                    tags={"Project": naming.PROJECT_TAG, "Stage": stage, "M11Role": "eval-runner"})
                for stage in naming.STAGES}

# ---------------------------------------------------------------- shared outputs for mr-* stacks
shared = pulumi.Output.all(
    release_bucket.bucket, pipeline_bucket.bucket, image_repo.repository_url, image_repo.arn,
    preview_repo.repository_url, preview_repo.arn, image_project.name, boundary.arn, preview_deployer.arn,
    module10.require_output("userPoolId"), module10.require_output("userPoolClientId"),
    module10.require_output("stateTable"), module10.require_output("browserAuthUrl"),
).apply(lambda v: {
    "account": account, "region": region, "releaseBucket": v[0], "pipelineBucket": v[1],
    "imageRepositoryUrl": v[2], "imageRepositoryArn": v[3], "previewImageRepositoryUrl": v[4],
    "previewImageRepositoryArn": v[5], "imageProject": v[6], "boundaryArn": v[7], "previewDeployerRoleArn": v[8],
    "userPoolId": v[9], "userPoolClientId": v[10], "stateTable": v[11], "browserAuthUrl": v[12],
    # m11-partners' outputs, copied into this stack's config by the operator after
    # `partners up`; null until then. mr-* deploys read them here (release/ui_preview.decide, aws/ui_wiring).
    **{k: (config.get(k) or None) for k in PARTNER_OUTPUTS}})
aws.ssm.Parameter("shared-outputs", name=naming.SHARED_OUTPUTS, type="String", tier="Standard",
                  value=shared.apply(lambda d: json.dumps(d, sort_keys=True, separators=(",", ":"))), tags=tags)

pulumi.export("account", account)
pulumi.export("region", region)
pulumi.export("releaseBucket", release_bucket.bucket)
pulumi.export("pipelineBucket", pipeline_bucket.bucket)
pulumi.export("imageRepositoryUrl", image_repo.repository_url)
pulumi.export("imageRepositoryArn", image_repo.arn)
pulumi.export("previewImageRepositoryUrl", preview_repo.repository_url)
pulumi.export("previewImageRepositoryArn", preview_repo.arn)
pulumi.export("buildImageRepositoryUrl", build_image_repo.repository_url)
pulumi.export("buildImage", build_image_digest or None)
pulumi.export("imageProject", image_project.name)
pulumi.export("boundaryArn", boundary.arn)
pulumi.export("deployerRoles", {s: r.arn for s, r in deployers.items()})
pulumi.export("previewDeployerRole", preview_deployer.arn)
pulumi.export("evalRunnerRoles", {s: r.arn for s, r in eval_runners.items()})
pulumi.export("userPoolId", module10.require_output("userPoolId"))
pulumi.export("userPoolClientId", module10.require_output("userPoolClientId"))
pulumi.export("stateTable", module10.require_output("stateTable"))
pulumi.export("browserAuthUrl", module10.require_output("browserAuthUrl"))
pulumi.export("sharedOutputsParameter", naming.SHARED_OUTPUTS)
pulumi.export("enablePipeline", enable_pipeline)
pulumi.export("gitlabConnectionArn", gitlab_connection.arn if gitlab_connection else None)
pulumi.export("gitlabConnectionStatus", gitlab_connection.connection_status if gitlab_connection else None)
pulumi.export("vercelProjectId", config.get("vercelProjectId") or None)
pulumi.export("module10Stack", module10_stack)
if enable_pipeline:
    pulumi.export("pipeline", release_pipeline.name)
    pulumi.export("projectRoles", {n: r.arn for n, r in project_roles.items()})
    pulumi.export("gitlabRoles", {n: r.arn for n, r in gitlab_roles.items()})
    pulumi.export("operatorExperimentRole", operator_experiment.arn)
    pulumi.export("freezeCheckFunction", freeze_fn.name)
    pulumi.export("notesFunction", notes_fn.name)
    pulumi.export("corpusHandlerFunction", corpus_fn.name)
    pulumi.export("corpusDlqUrl", corpus_dlq.url)
    pulumi.export("releaseBus", release_bus.name)
    pulumi.export("freezeParameter", freeze_param.name)
    pulumi.export("oidcAudience", oidc_audience)

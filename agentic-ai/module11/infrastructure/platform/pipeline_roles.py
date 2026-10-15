# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pure IAM renderers for m11-platform's pipeline increment.
No Pulumi, no AWS calls; every document is checked by the CrossGuard pack (P1, P10, P11) when
m11-platform is previewed, and by tests/test_module11_pipeline_roles.py.

Roles:
- `m11-pipeline`: the CodePipeline service role. `StartBuild` only on the pipeline's own action
  projects, named literally (P11).
- One role per CodeBuild project (`m11-analyze`, `m11-seal`, `m11-deploy`, `m11-gate`, `m11-bake`,
  `m11-preview`, `m11-preview-image`, `m11-preview-infra`, `m11-bot`, `m11-seal-experiment`), each with
  its own `cache/<project>/*` prefix only (P10) and its own `/aws/codebuild/<project>` log group.
- `m11-gitlab-ci`, `m11-gitlab-ci-infra`, `m11-gitlab-promoter`: GitLab OIDC roles.
- `m11-operator-experiment`: operator trust template; `ci_start_policy` on `m11-seal-experiment` only.
- `m11-freeze-check` and `m11-gitlab-notes`: the two platform Lambdas.

The bot token (`gitlab-token`) is readable only by m11-seal, m11-bot, the notes broker and the
operator; preview projects invoke the broker and never read a GitLab secret.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from module11.infrastructure.platform.roles import Ctx, allow, ci_start_policy, doc
from module11.release import naming, oidc_sub

PIPELINE = "m11-release"
PIPELINE_ROLE = "m11-pipeline"
ARTIFACT_PREFIX = PIPELINE                 # CodePipeline writes artifacts under <pipeline name>/ in its store
BUILD_IMAGE_REPOSITORY = "m11/build-image"
FREEZE_FUNCTION = "m11-freeze-check"
NOTES_FUNCTION = "m11-gitlab-notes"
FREEZE_PARAMETER = "/m11/devops-companion/freeze"
EVENT_BUS = "m11-releases"
GITLAB_ISSUER = "gitlab.com"

# Project short name -> CodeBuild project name. The pipeline's action projects first.
ACTION_PROJECTS = ("m11-analyze", "m11-image", "m11-seal", "m11-deploy", "m11-gate", "m11-bake")
TRUSTED_PROJECTS = ("m11-analyze", "m11-image", "m11-seal", "m11-deploy", "m11-gate", "m11-bake", "m11-preview",
                    "m11-bot", "m11-seal-experiment")
UNTRUSTED_PROJECTS = ("m11-preview-image", "m11-preview-infra")     # NO_CACHE (P10)
PROJECTS = TRUSTED_PROJECTS + UNTRUSTED_PROJECTS
# The role each project runs as; m11-image keeps its foundation role.
PROJECT_ROLES = {p: ("m11-image-build" if p == "m11-image" else p) for p in PROJECTS}
SOURCE_PROJECTS = ("m11-analyze", "m11-image", "m11-seal", "m11-deploy", "m11-gate", "m11-bake")
BUILD_IMAGE_PROJECTS = ("m11-analyze", "m11-seal", "m11-deploy", "m11-gate", "m11-bake", "m11-preview",
                        "m11-preview-infra", "m11-bot", "m11-seal-experiment")
EXPERIMENT_NAMES = ("SOURCE_SHA", "EXPERIMENT_ID")
PREVIEW_NAMES = ("PREVIEW_ACTION", "PREVIEW_IID", "PREVIEW_SHA")
PREVIEW_INFRA_NAMES = PREVIEW_NAMES + ("PREVIEW_JOB_ID",)
OIDC_ROLES = ("m11-gitlab-ci", "m11-gitlab-ci-infra", "m11-gitlab-promoter")
GITLAB_TOKEN_SECRET = "devops-companion/partners/gitlab-token"  # nosec B105 - a Secrets Manager secret name, not a secret value
SIGNING_KEY_SECRET = "devops-companion/partners/gitlab-bot-signing-key"  # nosec B105 - a Secrets Manager secret name, not a secret value
VERCEL_SECRETS = ("devops-companion/partners/vercel-token", "devops-companion/partners/vercel-bypass")


@dataclass(frozen=True)
class PipelineCtx:
    """Names the pipeline renderers need beyond roles.Ctx."""
    base: Ctx
    connection_arn: str
    gitlab_project: str                    # <your-group>/devops-companion-harness
    oidc_audience: str = "sts.amazonaws.com"

    def project_arn(self, project: str) -> str:
        return self.base.arn("codebuild", f"project/{project}")

    def role_arn(self, name: str) -> str:
        return self.base.iam(f"role/{name}")

    @property
    def pipeline_arn(self) -> str:
        return self.base.arn("codepipeline", PIPELINE)

    @property
    def oidc_provider_arn(self) -> str:
        return self.base.iam(f"oidc-provider/{GITLAB_ISSUER}")

    def secret(self, name: str) -> str:
        # Secrets Manager appends a 6-character suffix to every secret ARN.
        return self.base.arn("secretsmanager", f"secret:{name}-??????")

    def function(self, name: str) -> str:
        return self.base.arn("lambda", f"function:{name}")


# ---------------------------------------------------------------- shared statement builders
def _logs(c: PipelineCtx, project: str) -> dict:
    return allow("Logs", ["logs:CreateLogStream", "logs:PutLogEvents"],
                 c.base.arn("logs", f"log-group:/aws/codebuild/{project}:*"))


def _cache(c: PipelineCtx, project: str) -> list[dict]:
    if project in UNTRUSTED_PROJECTS:
        return []
    return [allow("Cache", ["s3:GetObject", "s3:PutObject"],
                  c.base.s3(c.base.pipeline_bucket, f"cache/{project}/*"))]


def _artifacts(c: PipelineCtx, write: bool = False) -> dict:
    actions = ["s3:GetObject", "s3:GetObjectVersion"] + (["s3:PutObject"] if write else [])
    return allow("PipelineArtifacts", actions, c.base.s3(c.base.pipeline_bucket, f"{ARTIFACT_PREFIX}/*"))


def _build_image(c: PipelineCtx) -> list[dict]:
    return [allow("RegistryAuth", "ecr:GetAuthorizationToken", "*"),
            allow("BuildImage", ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"],
                  c.base.arn("ecr", f"repository/{BUILD_IMAGE_REPOSITORY}"))]


def _connection(c: PipelineCtx) -> dict:
    return allow("UseConnection", ["codeconnections:UseConnection", "codestar-connections:UseConnection",
                                   "codeconnections:GetConnection", "codeconnections:GetConnectionToken"],
                 c.connection_arn)


def _assume(roles: Sequence[str], c: PipelineCtx, sid: str = "AssumeStageRoles") -> dict:
    return allow(sid, ["sts:AssumeRole", "sts:SetSourceIdentity", "sts:TagSession"],
                 [c.role_arn(r) for r in roles])


def _param(c: PipelineCtx, *leaves: str) -> list[str]:
    return [c.base.arn("ssm", f"parameter/m11/devops-companion/{leaf}") for leaf in leaves]


def _release(c: PipelineCtx, *prefixes: str) -> list[str]:
    return [c.base.s3(c.base.release_bucket, f"{p}/*") for p in prefixes]


def _project_base(c: PipelineCtx, project: str, *, source: bool, image: bool) -> list[dict]:
    out = [_logs(c, project), *_cache(c, project)]
    if source:
        out += [_artifacts(c), _connection(c)]
    if image:
        out += _build_image(c)
    return out


def _model_health() -> dict:
    """The gate report's model provider health block (evaluation/provider_health.py) reads AWS/Bedrock metrics.
    GetMetricStatistics has no resource type, so `*` (no_resource_actions.json)."""
    return allow("ModelHealth", "cloudwatch:GetMetricStatistics", "*")


def _notes(c: PipelineCtx) -> dict:
    return allow("NotesBroker", "lambda:InvokeFunction", c.function(NOTES_FUNCTION))


# ---------------------------------------------------------------- the pipeline service role
def pipeline_policy(c: PipelineCtx) -> dict:
    return doc([
        allow("Artifacts", ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:GetBucketVersioning"],
              [c.base.s3(c.base.pipeline_bucket, f"{ARTIFACT_PREFIX}/*")]),
        allow("ArtifactBucket", ["s3:GetBucketVersioning", "s3:GetBucketLocation", "s3:ListBucket"],
              c.base.s3(c.base.pipeline_bucket)),
        allow("ActionBuilds", ["codebuild:StartBuild", "codebuild:BatchGetBuilds", "codebuild:StopBuild"],
              [c.project_arn(p) for p in ACTION_PROJECTS]),
        allow("FreezeCheck", ["lambda:InvokeFunction", "lambda:GetFunction"], c.function(FREEZE_FUNCTION)),
        allow("Source", ["codeconnections:UseConnection", "codestar-connections:UseConnection"], c.connection_arn),
    ])


# ---------------------------------------------------------------- the action projects
def analyze_policy(c: PipelineCtx) -> dict:
    """Normalize, detect against prod /current, C1-C16 with c16Base, offline tests, image-tag
    existence, the image context zip, and the tooling archive."""
    return doc(_project_base(c, "m11-analyze", source=True, image=True) + [
        allow("ProdCurrent", "ssm:GetParameter", _param(c, "prod/current")),
        allow("ReleaseRead", "s3:GetObject", _release(c, "releases", "release-inputs/prod", "tooling", "builds")),
        allow("ReleaseList", "s3:ListBucket", c.base.s3(c.base.release_bucket)),
        allow("ToolingWrite", "s3:PutObject", _release(c, "tooling", "builds", "releases/plans")),
        allow("C16Base", ["codepipeline:ListPipelineExecutions", "codepipeline:GetPipelineExecution"],
              c.pipeline_arn),
        allow("ImageTags", "ecr:DescribeImages", c.base.arn("ecr", f"repository/{naming.IMAGE_REPOSITORY}")),
    ])


def seal_policy(c: PipelineCtx) -> dict:
    """Seal: content-addressed objects, the manifest and change plan, and the bot's
    annotated tag, Release and generic package in GitLab (bot token)."""
    return doc(_project_base(c, "m11-seal", source=True, image=True) + [
        # No latch read here: P1d admits latch/* only for the controller and deployers, so the latch is
        # enforced at every stage deploy's step 1, before anything changes (F9 decision log).
        allow("SealState", "ssm:GetParameter", _param(c, "prod/current")),
        allow("ReleaseRead", "s3:GetObject", _release(c, "releases", "release-inputs/prod", "handlers", "layers",
                                                      "builds", "evidence", "snapshots", "tooling")),
        allow("ReleaseList", "s3:ListBucket", c.base.s3(c.base.release_bucket)),
        allow("ReleaseWrite", "s3:PutObject", _release(c, "releases", "handlers", "layers", "builds", "evidence",
                                                       "snapshots", "ledger/shared")),
        allow("ImageDigest", "ecr:DescribeImages", c.base.arn("ecr", f"repository/{naming.IMAGE_REPOSITORY}")),
        allow("BotToken", "secretsmanager:GetSecretValue", c.secret(GITLAB_TOKEN_SECRET)),
    ])


def deploy_policy(c: PipelineCtx) -> dict:
    """`release deploy|prune` for staging and prod: everything runs as the stage deployer, the
    smoke test as the stage's eval runner."""
    roles = [naming.deployer_role(s) for s in ("staging", "prod")] + \
        [naming.eval_runner_role(s) for s in ("staging", "prod")]
    return doc(_project_base(c, "m11-deploy", source=True, image=True) + [_assume(roles, c)])


def gate_policy(c: PipelineCtx) -> dict:
    """The staging gate: invoke and evaluate as the staging eval runner; run records and reports
    under evaluations/; baselines read-only; one MR/commit note through the broker."""
    return doc(_project_base(c, "m11-gate", source=True, image=True) + [
        _assume([naming.eval_runner_role("staging"), naming.eval_runner_role("prod")], c),
        allow("StageCurrent", "ssm:GetParameter", _param(c, "staging/current", "prod/current")),
        allow("Records", ["s3:GetObject", "s3:PutObject"], _release(c, "evaluations")),
        allow("Baselines", "s3:GetObject", _release(c, "baselines", "releases")),
        allow("ReleaseList", "s3:ListBucket", c.base.s3(c.base.release_bucket)),
        allow("Evaluators", "bedrock-agentcore:ListEvaluators", "*"),
        _model_health(),
        _notes(c),
    ])


def bake_policy(c: PipelineCtx) -> dict:
    """Bake: poll prod's control item, prune as the prod deployer, post the outcome note."""
    return doc(_project_base(c, "m11-bake", source=True, image=True) + [
        _assume([naming.deployer_role("prod"), naming.eval_runner_role("prod")], c),
        allow("Control", "dynamodb:GetItem", c.base.arn("dynamodb", f"table/{naming.table('prod', 'control')}")),
        allow("ProdState", "ssm:GetParameter", _param(c, "prod/current", "prod/candidate")),
        allow("ReleaseRead", "s3:GetObject", _release(c, "releases")),
        _notes(c),
    ])


def image_policy_extra(c: PipelineCtx) -> list[dict]:
    """m11-image's additions for the pipeline (its foundation policy keeps the push and context reads)."""
    return [_artifacts(c), _connection(c), *_cache(c, "m11-image"),
            allow("BuildImagePush", ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload",
                                     "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage",
                                     "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:DescribeImages"],
                  c.base.arn("ecr", f"repository/{BUILD_IMAGE_REPOSITORY}"))]


# ---------------------------------------------------------------- preview and helper projects
def preview_policy(c: PipelineCtx) -> dict:
    """m11-preview: main's tooling on MR data. Deploys through m11-preview-deployer, reads
    the shared outputs and the two C16 reads, caches archives under previews/, uses the broker for
    every GitLab call, and the Vercel secrets as SECRETS_MANAGER variables. No bot token."""
    return doc(_project_base(c, "m11-preview", source=False, image=True) + [
        _assume([naming.deployer_role("mr-0"), "m11-mr*"], c, "AssumePreviewRoles"),
        # The MR gate's latency control arm on prod `stable` (evaluation/control.py); the tooling narrows the
        # session to InvokeAgentRuntime and StopRuntimeSession with a session policy.
        _assume([naming.eval_runner_role("prod")], c, "AssumeLatencyControl"),
        # release-inputs/prod: prod's deployed manifest, which the MR's base check compares with (found live: the
        # first MR pipeline failed with AccessDenied on release-inputs/prod/<mv>.deployed.json).
        allow("Tooling", "s3:GetObject", _release(c, "tooling", "previews", "releases", "release-inputs/prod",
                                                  "handlers", "layers", "builds", "evidence", "snapshots")),
        allow("Previews", "s3:PutObject", _release(c, "previews", "releases", "handlers", "layers", "builds",
                                                   "evidence")),
        # The MR gate reads the calibrated baselines and writes its run record, as the staging gate does (found
        # live: the first MR gate failed with AccessDenied on baselines/<mv>/<set>/baseline.json).
        allow("Baselines", "s3:GetObject", _release(c, "baselines")),
        allow("Records", ["s3:GetObject", "s3:PutObject"], _release(c, "evaluations")),
        allow("Evaluators", "bedrock-agentcore:ListEvaluators", "*"),
        allow("ReleaseList", "s3:ListBucket", c.base.s3(c.base.release_bucket)),
        allow("SharedOutputs", "ssm:GetParameter", [c.base.arn("ssm", f"parameter{naming.SHARED_OUTPUTS}")] +
              _param(c, "prod/current")),
        allow("C16Base", "codepipeline:ListPipelineExecutions", c.pipeline_arn),
        allow("StartImage", ["codebuild:StartBuild", "codebuild:BatchGetBuilds"], c.project_arn("m11-preview-image")),
        allow("PreviewImageDigest", "ecr:DescribeImages",
              c.base.arn("ecr", f"repository/{naming.PREVIEW_IMAGE_REPOSITORY}")),
        allow("Vercel", "secretsmanager:GetSecretValue", [c.secret(n) for n in VERCEL_SECRETS]),
        _model_health(),
        _notes(c),
    ])


def preview_image_policy(c: PipelineCtx) -> dict:
    """m11-preview-image: MR-controlled Dockerfile steps, so no cache, no secret, no deployer."""
    return doc(_project_base(c, "m11-preview-image", source=False, image=False) + [
        allow("Context", "s3:GetObject", _release(c, "previews")),
        allow("RegistryAuth", "ecr:GetAuthorizationToken", "*"),
        allow("Push", ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
                       "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
                       "ecr:DescribeImages"],
              c.base.arn("ecr", f"repository/{naming.PREVIEW_IMAGE_REPOSITORY}")),
    ])


def preview_infra_policy(c: PipelineCtx) -> dict:
    """m11-preview-infra's inline basics. It is its own (narrower) deployer: its managed policies come from
    roles.preview_infra_policies (no generated-ID creates, no Cognito writes), and it assumes
    no other role, so MR tooling never holds m11-preview-deployer's create statements. No cache, no token."""
    return doc(_project_base(c, "m11-preview-infra", source=False, image=True) + [
        allow("Tooling", "s3:GetObject", _release(c, "tooling", "previews")),
        _notes(c),
    ])


def bot_policy(c: PipelineCtx) -> dict:
    """m11-bot (fallback): signed bot commits with the bot token and its signing key."""
    return doc(_project_base(c, "m11-bot", source=False, image=True) + [
        allow("Tooling", "s3:GetObject", _release(c, "tooling", "bot")),
        allow("BotSecrets", "secretsmanager:GetSecretValue",
              [c.secret(GITLAB_TOKEN_SECRET), c.secret(SIGNING_KEY_SECRET)]),
    ])


def seal_experiment_policy(c: PipelineCtx) -> dict:
    """m11-seal-experiment: main's tooling on one branch archive (through the broker),
    `releases/experiments/*` only, the shared ledger, and the target stage's state reads."""
    return doc(_project_base(c, "m11-seal-experiment", source=False, image=True) + [
        allow("State", "ssm:GetParameter", _param(c, "prod/current", "prod/applied", "dev/current", "dev/applied",
                                                  "freeze")),
        allow("ReleaseRead", "s3:GetObject", _release(c, "tooling", "releases", "release-inputs/prod",
                                                      "release-inputs/dev", "handlers", "layers", "builds",
                                                      "evidence", "snapshots", "previews/archives")),
        allow("ReleaseList", "s3:ListBucket", c.base.s3(c.base.release_bucket)),
        allow("ExperimentWrite", "s3:PutObject", _release(c, "releases/experiments", "ledger/shared")),
        allow("ImageDigest", "ecr:DescribeImages", c.base.arn("ecr", f"repository/{naming.IMAGE_REPOSITORY}")),
        _notes(c),
    ])


PROJECT_POLICIES = {
    "m11-analyze": analyze_policy, "m11-seal": seal_policy, "m11-deploy": deploy_policy, "m11-gate": gate_policy,
    "m11-bake": bake_policy, "m11-preview": preview_policy, "m11-preview-image": preview_image_policy,
    "m11-preview-infra": preview_infra_policy, "m11-bot": bot_policy, "m11-seal-experiment": seal_experiment_policy,
}


# ---------------------------------------------------------------- GitLab OIDC
def oidc_trust(c: PipelineCtx, sub: str) -> dict:
    return doc([{"Sid": "GitLabOidc", "Effect": "Allow", "Principal": {"Federated": c.oidc_provider_arn},
                 "Action": "sts:AssumeRoleWithWebIdentity",
                 "Condition": {"StringEquals": {f"{GITLAB_ISSUER}:aud": c.oidc_audience},
                               "StringLike": {f"{GITLAB_ISSUER}:sub": sub}}}])


def gitlab_roles(c: PipelineCtx) -> dict[str, tuple[dict, dict]]:
    """{role: (trust, inline policy)} for the three OIDC roles."""
    subs = oidc_sub.trust_patterns(c.gitlab_project)
    promoter = doc([
        allow("ReadPipeline", ["codepipeline:GetPipelineState", "codepipeline:GetPipelineExecution",
                               "codepipeline:ListPipelineExecutions", "codepipeline:ListActionExecutions"],
              c.pipeline_arn),
        allow("Approve", "codepipeline:PutApprovalResult",
              [f"{c.pipeline_arn}/Approval/ReleaseApproval", f"{c.pipeline_arn}/ExtendedApproval/ExtendedReleaseApproval"]),
    ])
    return {
        "m11-gitlab-ci": (oidc_trust(c, subs["m11-gitlab-ci"]),
                          ci_start_policy(c.project_arn("m11-preview"), PREVIEW_NAMES)),
        "m11-gitlab-ci-infra": (oidc_trust(c, subs["m11-gitlab-ci-infra"]),
                                ci_start_policy(c.project_arn("m11-preview-infra"), PREVIEW_INFRA_NAMES)),
        "m11-gitlab-promoter": (oidc_trust(c, subs["m11-gitlab-promoter"]), promoter),
    }


def operator_experiment_policy(c: PipelineCtx) -> dict:
    return ci_start_policy(c.project_arn("m11-seal-experiment"), EXPERIMENT_NAMES)


# ---------------------------------------------------------------- Lambdas
def freeze_check_policy(c: PipelineCtx) -> dict:
    return doc([
        allow("Freeze", "ssm:GetParameter", c.base.arn("ssm", f"parameter{FREEZE_PARAMETER}")),
        allow("JobResult", ["codepipeline:PutJobSuccessResult", "codepipeline:PutJobFailureResult"], "*"),
        allow("Logs", ["logs:CreateLogStream", "logs:PutLogEvents"],
              c.base.arn("logs", f"log-group:/aws/lambda/{FREEZE_FUNCTION}:*")),
    ])


def notes_policy(c: PipelineCtx, secret_name: str = GITLAB_TOKEN_SECRET) -> dict:
    return doc([
        allow("BotToken", "secretsmanager:GetSecretValue", c.secret(secret_name)),
        allow("Archives", "s3:PutObject", c.base.s3(c.base.release_bucket, "previews/archives/*")),
        allow("Logs", ["logs:CreateLogStream", "logs:PutLogEvents"],
              c.base.arn("logs", f"log-group:/aws/lambda/{NOTES_FUNCTION}:*")),
    ])


CORPUS_FUNCTION = "m11-corpus-handler"


def corpus_handler_policy(c: PipelineCtx, dlq_arn: str, secret_name: str = GITLAB_TOKEN_SECRET) -> dict:
    """The corpus handler: the bot token (it opens the MR), the snapshot objects it checks and the
    changed sources it reads, and the DLQ (its fatal events and the async on-failure destination)."""
    return doc([
        allow("BotToken", "secretsmanager:GetSecretValue", c.secret(secret_name)),
        allow("Snapshots", "s3:GetObject", c.base.s3(c.base.release_bucket, "snapshots/*")),
        allow("DeadLetters", "sqs:SendMessage", dlq_arn),
        allow("Logs", ["logs:CreateLogStream", "logs:PutLogEvents"],
              c.base.arn("logs", f"log-group:/aws/lambda/{CORPUS_FUNCTION}:*")),
    ])


def broker_callers() -> tuple[str, ...]:
    """Roles that may invoke the notes broker (its resource policy names them)."""
    return ("m11-gate", "m11-bake", "m11-preview", "m11-preview-infra", "m11-seal-experiment")


def denied_token_readers() -> tuple[str, ...]:
    """Preflight item 16: these roles must not read the bot token."""
    return ("m11-preview", "m11-preview-infra", "m11-preview-image", "m11-gitlab-ci", "m11-gitlab-ci-infra",
            "m11-gate", "m11-bake", "m11-seal-experiment")

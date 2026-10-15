# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`module11 release deploy|prune`, the SDK side of a release deploy (pure decisions: release/deploy.py).

The operator path (before the pipeline): the CLI assumes `m11-<stage>-deployer` (or
`m11-preview-deployer` for mr-*) with the operator trust template, runs every step with that
session, invokes the smoke test as the stage's eval-runner role, and records each session with
release/audit.py. CodeBuild `m11-deploy` runs the same code in the pipeline.

Steps: 1 validate (latch, freeze, base, rollout state, same bytes, retained pins); 2 bootstrap when
the stage has no `/applied` (first `pulumi up` with a null bundle, initial version ledgered);
3 bundle version by content; 4 `pulumi up --policy-pack`; 5 pins object and `/applied`, then the
runtime path; 6 flags; 7 retarget and wait; 8 smoke and edge probes (restore on failure, 7a N5);
9 publish `/current` and the control item; 10 announce.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from module11.release import deploy as D
from module11.release import detect as DT
from module11.release import manifest as M
from module11.release import naming, pins as P
from module11.release import seal as SEAL
from module11.release.hashing import sha256_bytes
from module11.release.pulumi_inputs import read_archive

from . import bundles, control_table, endpoints, ledger, operator, params, releases
from .urls import open_url

POLICY_DIR = "module11/infrastructure/policy"
HARNESS_DIR = "module11/infrastructure/harness"
MAIN_SECRETS = "awskms://{alias}?region={region}"


class DeployError(RuntimeError):
    pass


@dataclass
class Context:
    root: Path
    account: str
    region: str
    operators: list[str]
    log: Callable[[str], None] = print
    admin: Any = None                          # boto3.Session of the operator (read-only use + AssumeRole)
    sessions: dict = field(default_factory=dict)
    # stage -> a session that stage's roles trust and that may read its parameters. The preview gate sets it to
    # the m11-preview-deployer session for its mr-<iid> stage, so m11-preview needs no mr-* parameter grant
    # (P1d) and the stack's eval runner keeps trusting only the operators and the preview deployer.
    via: dict = field(default_factory=dict)

    @property
    def release_bucket(self) -> str:
        return releases.bucket_name(self.account, self.region)

    def backend(self, stage: str) -> str:
        template = naming.PREVIEW_STATE_BUCKET if naming.is_mr(stage) else naming.STATE_BUCKET
        return "s3://" + template.format(account=self.account, region=self.region)

    def secrets_provider(self, stage: str) -> str:
        alias = naming.PREVIEW_SECRETS_ALIAS if naming.is_mr(stage) else naming.SECRETS_ALIAS
        return MAIN_SECRETS.format(alias=alias, region=self.region)

    def stack(self, stage: str) -> str:
        return f"organization/m11-harness/{stage}"

    def role_arn(self, name: str) -> str:
        return f"arn:aws:iam::{self.account}:role/{name}"

    def assume(self, role: str, stage: str, purpose: str, policy: str | None = None) -> operator.OperatorSession:
        """`policy`: an optional session policy that narrows the role for this session (cached separately)."""
        key = role if policy is None else f"{role}#{hash(policy)}"
        cached = self.sessions.get(key)
        if cached and cached.credentials["Expiration"].timestamp() - time.time() > 900:
            return cached
        source = self.via.get(stage)
        if source is None or getattr(source, "role_arn", None) == self.role_arn(role):
            sts = self.admin.client("sts")
        else:                                   # chained: the via session's credentials assume the role
            sts = source.client("sts", region_name=self.region)
        session = operator.assume(self.role_arn(role), sts=sts, duration_seconds=4 * 3600,
                                  purpose=purpose, stage=stage, policy=policy,
                                  record=operator.local_record(self.root / "module11/.local/audit"))
        self.sessions[key] = session
        self.log(f"assumed {role} as {session.session_name} (source identity {session.source_identity})")
        return session


def _unwired_mr_origin(stage: str) -> str:
    """The UI origin of an MR stack with no Vercel wiring (for example a bot-authored MR): the predicted branch
    alias when M11_VERCEL_TEAM_SLUG names your team, else a reserved `.invalid` origin that no site can serve."""
    from . import vercel_api as V
    team = os.environ.get("M11_VERCEL_TEAM_SLUG", "")
    if team:
        return "https://" + V.predict_alias("devops-companion-ui", stage, team)
    return f"https://{stage}.ui-preview.invalid"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Deploy:
    def __init__(self, ctx: Context, stage: str, *, ui: Any = None, exclude_ui: bool = False,
                 as_caller: bool = False) -> None:
        """`exclude_ui`: `pulumi up --exclude` the Cognito client, its branding and the Vercel variables (the MR's own
        tooling in m11-preview-infra). `as_caller`: m11-preview-infra is its own deployer and
        assumes no role."""
        self.ctx, self.stage = ctx, naming.check_stage(stage)
        self.log = ctx.log
        self.ui = ui if naming.is_mr(self.stage) else None     # aws/ui_wiring.UiWiring (the preview build)
        self.extra_env: dict[str, str] = {}
        self._ui_wired = False
        self.exclude_ui = exclude_ui and naming.is_mr(self.stage)
        if as_caller and not naming.is_mr(self.stage):
            raise DeployError("only an mr-* deploy runs as its caller (m11-preview-infra)")
        self.deployer = (operator.caller_session(ctx.admin) if as_caller else
                         ctx.assume(naming.deployer_role(stage), stage, f"release deploy {stage}"))
        self.s3 = self.deployer.client("s3", region_name=ctx.region)
        self.ssm = self.deployer.client("ssm", region_name=ctx.region)
        self.ctl = self.deployer.client("bedrock-agentcore-control", region_name=ctx.region)
        self.ddb = self.deployer.client("dynamodb", region_name=ctx.region)
        self.logs = self.deployer.client("logs", region_name=ctx.region)
        self.rb = ctx.release_bucket
        self.local = ctx.root / "module11/.local/deploy"
        self.local.mkdir(parents=True, exist_ok=True)
        self.timings: dict[str, float] = {}       # per-step timing record
        self._last = time.time()

    def _mark(self, step: str) -> None:
        now = time.time()
        self.timings[step] = round(self.timings.get(step, 0.0) + now - self._last, 1)
        self._last = now

    # ------------------------------------------------------------ pulumi
    def _env(self, inputs_path: Path | None) -> dict:
        env = {k: v for k, v in os.environ.items()
               if k not in {"AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                            "M11_RELEASE_INPUTS"}}
        env.update(self.deployer.env())
        env.update(self.extra_env)                 # VERCEL_API_TOKEN, only for a `wire` decision
        env.update({"PULUMI_BACKEND_URL": self.ctx.backend(self.stage), "PULUMI_SKIP_UPDATE_CHECK": "1",
                    "AWS_REGION": self.ctx.region, "AWS_DEFAULT_REGION": self.ctx.region,
                    "M11_OPERATOR_PRINCIPALS": ",".join(self.ctx.operators)})
        if inputs_path:
            env["M11_RELEASE_INPUTS"] = str(inputs_path)
        return env

    def pulumi(self, *args: str, inputs_path: Path | None = None, check: bool = True, log_name: str | None = None
               ) -> subprocess.CompletedProcess:
        cwd = self.ctx.root / HARNESS_DIR
        result = subprocess.run(["pulumi", *args], cwd=cwd, env=self._env(inputs_path), capture_output=True, text=True)
        if log_name:
            (self.local / log_name).write_text(result.stdout + "\n" + result.stderr, encoding="utf-8")
        if check and result.returncode != 0:
            lines = (result.stdout + result.stderr).splitlines()
            # The resources' own error lines first (CodeBuild keeps only this message), then the tail.
            errors = [line.strip() for line in lines if "error:" in line and "update failed" not in line][:20]
            tail = "\n".join(lines[-40:])
            detail = ("\n".join(errors) + "\n...\n") if errors else ""
            raise DeployError(f"pulumi {' '.join(args[:1])} failed ({result.returncode}):\n{detail}{tail}")
        return result

    def ensure_stack(self) -> None:
        stack = self.ctx.stack(self.stage)
        # Not `pulumi stack ls`: it reads every stack's state, and a deployer may read only its own.
        state_bucket = self.ctx.backend(self.stage)[len("s3://"):]
        state_key = f".pulumi/stacks/m11-harness/{self.stage}.json"
        if not releases.exists(self.s3, state_bucket, state_key):
            self.pulumi("stack", "init", stack, "--secrets-provider", self.ctx.secrets_provider(self.stage))
            self.log(f"created stack {stack}")
        else:
            self._restore_stack_file(state_bucket, state_key)
        current = json.loads(self.pulumi("config", "--stack", stack, "--json").stdout or "{}")
        wanted = {"stage": self.stage, "accountId": self.ctx.account, "region": self.ctx.region,
                  "module10Stack": os.environ.get("M11_MODULE10_STACK", "organization/module10-exposure/workshop")}
        if self.stage == "prod" or naming.is_mr(self.stage):
            wanted["uiOrigin"] = os.environ.get(f"M11_UI_ORIGIN_{self.stage.upper().replace('-', '_')}") or \
                (self.ui.predicted_origin() if self.ui else
                 _unwired_mr_origin(self.stage) if naming.is_mr(self.stage)
                 else "https://prod.ui-origin.invalid")     # set M11_UI_ORIGIN_PROD to your Vercel production domain
        wanted["accessLogs"] = "true" if self._account_logging() else "false"
        for key, value in wanted.items():
            if f"m11-harness:{key}" not in current:
                self.pulumi("config", "set", "--stack", stack, key, value)
        if "m11-harness:operatorPrincipals" not in current:
            for i, arn in enumerate(self.ctx.operators):
                self.pulumi("config", "set", "--stack", stack, "--path", f"operatorPrincipals[{i}]", arn)

    def _restore_stack_file(self, state_bucket: str, state_key: str) -> None:
        """A fresh checkout (CodeBuild) has no git-ignored Pulumi.<stage>.yaml. The stack's KMS secrets provider
        and its encrypted data key are recorded in the stack's own checkpoint, so rebuild the file from it
        before any `pulumi config` call (a missing provider would fall back to a passphrase)."""
        path = self.ctx.root / HARNESS_DIR / f"Pulumi.{self.stage}.yaml"
        if path.is_file():
            return
        checkpoint = json.loads(releases.get(self.s3, f"s3://{state_bucket}/{state_key}"))
        provider = (checkpoint.get("checkpoint", {}).get("latest") or {}).get("secrets_providers") or {}
        state = provider.get("state") or {}
        if provider.get("type") != "cloud" or not state.get("url") or not state.get("encryptedkey"):
            raise DeployError(f"stack {self.stage} records no KMS secrets provider; cannot rebuild {path.name}")
        path.write_text(f"secretsprovider: {state['url']}\nencryptedkey: {state['encryptedkey']}\n", encoding="utf-8")
        self.log(f"rebuilt {path.name} from the stack checkpoint's secrets provider")

    def _account_logging(self) -> bool:
        if naming.is_mr(self.stage):
            return False
        try:
            account = self.deployer.client("apigateway", region_name=self.ctx.region).get_account()
            return bool(account.get("cloudwatchRoleArn"))
        except Exception as exc:  # no read access: leave access logs off (R9)
            self.log(f"apigateway get-account unavailable ({type(exc).__name__}); access logs off")
            return False

    def check_role_headroom(self) -> None:
        """Before a stack's first `pulumi up`: stop when the account cannot hold the stack's new roles
        (IAM Roles per account, L-FE177D64), instead of failing halfway with LimitExceeded. Read with the
        operator's session; an unreadable account summary only logs a skip."""
        from module11.release import iam_budget as IB
        from . import quotas
        try:
            room = quotas.headroom(self.ctx.admin, {self.stage: IB.stack_roles(self.ctx.root, self.stage)})
        except Exception as exc:  # noqa: BLE001 - e.g. no iam:GetAccountSummary in a pipeline role
            self.log(f"IAM role headroom not checked ({type(exc).__name__})")
            return
        if not room.ok:
            raise DeployError(room.message())
        self.log(room.message())

    def up(self, inputs: Any, label: str, *, refresh: bool = False) -> dict:
        """`pulumi up` with these release inputs. `refresh` (the `--reapply` repair path) first reads live state,
        so an out-of-band edit, such as a hand-edited role policy (drift component `iam`), is overwritten with the
        declared document; endpoint versions stay `release deploy`'s (ignore_changes)."""
        doc = inputs.to_dict()
        key = f"release-inputs/{self.stage}/{inputs.manifestVersion}{'.' + label if label != 'release' else ''}.json"
        uri, sha = releases.put_json(self.s3, self.rb, key, doc)
        path = self.local / f"{self.stage}-{label}-inputs.json"
        path.write_text(json.dumps(doc, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        started = time.time()
        self.log(f"pulumi up ({label}) {self.ctx.stack(self.stage)} ...")
        excluded = [a for urn in self.ui_urns() for a in ("--exclude", urn)] if self.exclude_ui else []
        self.pulumi("up", "--stack", self.ctx.stack(self.stage), "--policy-pack", str(self.ctx.root / POLICY_DIR),
                    "--yes", "--parallel", "32", "--non-interactive", "--suppress-progress",
                    *(["--refresh"] if refresh else []), *excluded,
                    inputs_path=path, log_name=f"{self.stage}-{label}-{int(started)}.log")
        self.log(f"pulumi up ({label}) done in {int(time.time() - started)} s")
        outputs = self.outputs()
        outputs["_inputs"] = {"uri": uri, "sha256": sha}
        return outputs

    def ui_urns(self) -> list[str]:
        """What `--exclude-ui` leaves alone: the browser client, its branding and the Vercel variables."""
        from module11.infrastructure.harness import preview_ui
        stack = f"urn:pulumi:{self.stage}::m11-harness::"
        return [stack + "aws:cognito/userPoolClient:UserPoolClient::browser-client",
                stack + "aws:cognito/managedLoginBranding:ManagedLoginBranding::browser-login-style",
                *[stack + f"{preview_ui.URN_TYPE}::{preview_ui.logical_name(k)}" for k in preview_ui.KEYS]]

    def outputs(self) -> dict:
        return json.loads(self.pulumi("stack", "output", "--stack", self.ctx.stack(self.stage), "--json").stdout)

    # ------------------------------------------------------------ reads
    def _get_verified(self, ref: dict, what: str) -> bytes:
        return releases.get_verified(self.s3, ref["uri"], ref["sha256"], what)

    def _full(self, compact: dict | None) -> dict | None:
        if not compact:
            return None
        full = json.loads(releases.get(self.s3, compact["s3Uri"]))
        # The SSM form is the live record: on PROMOTED the controller rewrites only its `deployment`
        # (endpoints now both on the promoted version), so it wins over the S3 copy written at `open`.
        if isinstance(compact.get("deployment"), dict):
            full["deployment"] = compact["deployment"]
        return full

    def _ledger(self, kind: str, **fields: Any) -> None:
        ledger.put(self.s3, self.rb, self.stage, kind, by=self.deployer.session_name, **fields)

    # ------------------------------------------------------------ the deploy
    def run(self, manifest_version: str | None = None, *, experiment: str | None = None) -> dict:
        """A release deploy, or with `experiment=<EXPERIMENT_ID>` the shadow experiment's deploy:
        the sealed experiment from releases/experiments/<mv>/, the experiment freeze rule, StaleBase against the
        experiment's recorded base, no gate, `candidate` only, and an experiment `open` (profile model, 100%
        shadow, canary 0)."""
        t0 = time.time()
        self._last = t0
        mv = manifest_version or (None if experiment else releases.latest_seal(self.s3, self.rb))
        if not mv:
            raise DeployError("no sealed release: run `module11 release seal --operator` first")
        record = SEAL.bind_bucket(releases.read_seal(self.s3, self.rb, mv, experiment=bool(experiment)), self.rb)
        declared = json.loads(self._get_verified(record["manifest"], "manifest"))
        plan = DT.ChangePlan(**json.loads(self._get_verified(record["changePlan"], "change plan")))
        sealed = read_archive(releases.get(self.s3, record["pulumiInputs"]["uri"]), record["pulumiInputs"]["sha256"])
        current = params.stage_get(self.ssm, self.stage, "current")
        applied = params.stage_get(self.ssm, self.stage, "applied")
        self.log(f"deploy {mv} to {self.stage} (current {current and current['manifestVersion']}, "
                 f"applied {applied and applied['manifestVersion']})")

        # 1. Validate.
        if params.latched(self.ssm, record["behaviorDigest"]):
            raise D.Latched(f"behaviorDigest {record['behaviorDigest'][:19]}… is latched")
        if experiment:
            freeze = params.freeze(self.ssm)
            if freeze.get("state") == "emergency" or (
                    freeze.get("state") == "planned" and freeze.get("reason") != f"experiment {experiment}"):
                raise D.Frozen(f"the freeze is {freeze.get('state')} ({freeze.get('reason')}), not this experiment's")
            base_now = (current or {}).get("manifestVersion")
            if base_now != plan.baseManifestVersion:
                raise DT.StaleBase(f"StaleBase({plan.baseManifestVersion}, {base_now}): {self.stage} moved since "
                                   "the experiment was sealed")
            decision = DT.BaseDecision("proceed", plan, list(plan.kinds))
        elif self.stage == "prod" and params.freeze(self.ssm).get("state") in {"planned", "emergency"}:
            raise D.Frozen("prod is frozen")
        else:
            decision = self._check_base(plan, declared)
        if decision.action == "noop":
            # This commit changes nothing beyond what prod now runs; Bake succeeds without a rollout.
            self._ledger("base-advanced-noop", manifestVersion=mv, base=params.stage_get(self.ssm, "prod", "current")
                         .get("manifestVersion"))
            self.timings["total"] = round(time.time() - t0, 1)
            return {"manifestVersion": mv, "stage": self.stage, "path": "noop", "runtimeVersion": None,
                    "endpoints": {}, "seconds": int(time.time() - t0), "timings": dict(self.timings)}
        effective_kinds = decision.effective_kinds
        control_name = naming.table(self.stage, "control")
        control = None
        if applied is not None:
            control = control_table.get(self.ddb, control_name)
            if naming.is_rollout_stage(self.stage):
                P.check_rollout_idle((control or {}).get("state"))
        if record["imageDigest"] != declared["build"]["imageDigest"] or any(
                record["handlers"][t]["codeSha256"] != declared["build"]["handlers"][t]["codeSha256"]
                for t in declared["build"]["handlers"]):
            raise D.SameBytesViolation("sealed bytes differ from the declared manifest")
        current_own = (P.pins_of(current, self.stage, lambda uri: json.loads(releases.get(self.s3, uri)))
                       if current else None)
        static = D.static_pins(declared, sealed, stage=self.stage, account=self.ctx.account, region=self.ctx.region,
                               release_bucket=self.rb)
        retained = D.retained(current_own, static)
        if applied is None:
            self.check_role_headroom()
        self.ensure_stack()
        self._mark("validate")

        # 2. Bootstrap (M5).
        applied_inputs = json.loads(self._get_verified({"uri": applied["releaseInputsUri"], "sha256": applied["sha256"]},
                                                       "applied release inputs")) if applied else None
        if applied is None:
            self.log("bootstrap: no /applied on this stage; first pulumi up with a null bundle")
            out0 = self.up(SEAL.release_inputs(record, retained_pins=retained.to_dict(), bundle_arn=None,
                                               bundle_version=None), "bootstrap")
            bundle_arn = out0["bundleArn"]
            parent = bundles.initial_version(self.ctl, bundle_arn)
            self._ledger("initial", bundleArn=bundle_arn, versionId=parent, manifestVersion=mv)
            self.log(f"bootstrap: bundle {bundle_arn.rsplit('/', 1)[-1]} initial version {parent} ledgered")
        else:
            bundle_arn, parent = applied_inputs["bundleArn"], applied_inputs["bundleVersion"]

        # 3. Bundle version by content.
        component_bytes = self._get_verified(record["bundleComponent"], "bundle component")
        component = M.verify_component(declared, component_bytes)
        component_sha = record["bundleComponent"]["sha256"]
        version = M.bundle_reuse(component_sha, current, applied_inputs, self.stage)
        if version:
            self.log(f"bundle: reusing version {version} (same content)")
        else:
            runtime_arn = self.outputs()["runtimeArn"]
            # The service takes the branch's newest version as the first parent, which after a ROLLED_BACK
            # canary is the rolled-back candidate's version rather than /applied's.
            parent = bundles.branch_head(self.ctl, bundle_arn, self.stage) or parent
            version = bundles.create_version(
                self.ctl, bundle_arn, runtime_arn, component, parent=parent, branch=self.stage,
                message=f"{D.release_slug(declared['git'], self.stage)}: {declared['git'].get('subject', '')}",
                created_by={"name": "m11-release-deploy", "arn": self.deployer.role_arn})
            self._ledger("bundle-version", bundleArn=bundle_arn, versionId=version, parent=parent,
                         componentSha256=component_sha, manifestVersion=mv)
            self.log(f"bundle: created version {version} (parent {parent})")
        self._mark("bundle")

        # 4. Apply infrastructure (the pack runs inside `up`); an mr-* stack first decides its UI preview.
        if self.ui:
            self._ui_prepare()
        inputs = SEAL.release_inputs(record, retained_pins=retained.to_dict(), bundle_arn=bundle_arn,
                                     bundle_version=version)
        out = self.up(inputs, "release")
        self._default_log_retention(out["runtimeId"])
        self._mark("pulumiUp")

        # 5. Record what was applied, then decide the runtime path.
        own = D.own_pins(static, out["effectivePins"])
        pins_doc = D.pins_object(self.stage, mv, own, out["effectivePins"], retained, out["toolArns"])
        pins_uri, _ = releases.put_json(self.s3, self.rb, f"release-inputs/{self.stage}/{mv}.pins.json", pins_doc)
        applied_doc = D.applied_doc(inputs_uri=out["_inputs"]["uri"], inputs_sha=out["_inputs"]["sha256"],
                                    manifest_version=mv, applied_at=_now(), policy_digests=out["policyDigests"],
                                    pins_uri=pins_uri)
        params.stage_put(self.ssm, self.stage, "applied", applied_doc)
        new_version = str(out["runtimeVersion"])
        config = self.ctl.get_agent_runtime(agentRuntimeId=out["runtimeId"], agentRuntimeVersion=new_version)
        release = D.release_slug(declared["git"], self.stage)
        deployment = {
            "account": self.ctx.account, "region": self.ctx.region, "runtimeArn": out["runtimeArn"],
            "runtimeId": out["runtimeId"], "runtimeVersion": new_version, "endpoints": {},
            "bundle": {"arn": bundle_arn, "versionId": version, "componentSha256": component_sha},
            "toolArns": out["toolArns"], "layerArn": out["layerArn"],
            "guardrail": {"id": out["guardrailId"], "version": str(out["guardrailVersion"])}, "pinsUri": pins_uri,
            "releaseInputs": out["_inputs"], "apiUrl": out["apiUrl"], "release": release,
            "evaluatorIds": dict(out.get("evaluatorIds") or {})}
        draft = {**declared, "deployment": {self.stage: deployment}}
        deployment["runtimeConfigSha256"], deployment["runtimeIdentity"] = M.runtime_ids(config, draft, self.stage)
        full_current = self._full(current)
        stable = None
        if full_current:
            dep = full_current["deployment"][self.stage]
            stable = {"runtimeVersion": dep["endpoints"].get("stable", dep["runtimeVersion"]),
                      "runtimeIdentity": dep.get("runtimeIdentity"), "runtimeDigest": full_current["digests"]["runtime"],
                      "imageDigest": full_current["build"]["imageDigest"],
                      "infrastructure": full_current["artifacts"].get("infrastructure")}
        new = {"runtimeVersion": new_version, "runtimeIdentity": deployment["runtimeIdentity"],
               "runtimeDigest": declared["digests"]["runtime"], "imageDigest": declared["build"]["imageDigest"],
               "infrastructure": declared["artifacts"].get("infrastructure")}
        resolution = DT.resolve_path(plan, self.stage, new, stable, effective_kinds)
        self._mark("record")
        for line in resolution.logs:
            self.log(line)
        self.log(f"runtime path: {resolution.path} (version {new_version})")
        controller = bool(out.get("controllerFunction"))    # dev, staging, prod: the controller is the item's writer
        if experiment and resolution.path != "rollout":
            raise DeployError(f"the experiment produced no runtime change (path {resolution.path}); nothing to shadow")
        if resolution.path == "rollout":
            return self._rollout(record, declared, plan, out, deployment, full_current, stable, new_version, release,
                                 sealed, t0, effective_kinds, experiment=experiment)

        # 6. Flags (both arms change together on every path that reaches here).
        flags_state = self._write_flags(record)

        # 7. Retarget and wait.
        targets = {"single": list(naming.endpoints_for(self.stage)), "bootstrap": ["stable", "candidate"],
                   "equivalent": ["stable", "candidate"], "unchanged": []}[resolution.path]
        previous = {name: str(endpoints.get(self.ctl, out["runtimeId"], name).get("liveVersion")) for name in targets}
        for name in targets:
            t = time.time()
            endpoints.retarget(self.ctl, out["runtimeId"], name, new_version)
            self.log(f"endpoint {name} -> v{new_version} READY in {int(time.time() - t)} s")
        self._mark("retarget")
        serving = {name: new_version for name in naming.endpoints_for(self.stage)} if targets else \
            dict(full_current["deployment"][self.stage]["endpoints"])
        entries = {name: control_table.endpoint_entry(serving[name], mv, record["behaviorDigest"], release)
                   for name in naming.endpoints_for(self.stage)}
        if not naming.is_rollout_stage(self.stage):
            if controller:
                self._command("endpoints", entries)
            else:
                control = control_table.put_idle(self.ddb, control_name, entries,
                                                 expected=control_table.get(self.ddb, control_name))

        # 8. Smoke test and edge probes.
        try:
            self._edge_probes(out["apiUrl"])
            if not naming.is_mr(self.stage):
                target = "stable" if naming.is_rollout_stage(self.stage) else self.stage
                self._smoke(record, out, target, serving[target], release, sealed)
        except D.SmokeFailed as failure:
            self._restore(failure, plan, out, previous, targets, flags_state, full_current, control_name, entries)
            raise
        self._mark("smoke")

        # 9. Publish state.
        deployment["endpoints"] = serving
        deployment["flagsObject"] = {"key": "config/flags.json", "versionId": flags_state.get("versionId")}
        full = D.deployed_manifest(declared, self.stage, deployment)
        full_uri, _ = releases.put_json(self.s3, self.rb, f"release-inputs/{self.stage}/{mv}.deployed.json", full)
        params.stage_put(self.ssm, self.stage, "current", D.compact_current(full, full_uri))
        if naming.is_rollout_stage(self.stage):
            params.stage_put(self.ssm, self.stage, "candidate", params.NONE)
            if controller:
                self._command("endpoints", entries)
            else:
                control_table.put_idle(self.ddb, control_name, entries,
                                       expected=control_table.get(self.ddb, control_name))
        ui = self._ui_finish(out, inputs) if self.ui else None
        if ui:
            self._mark("ui")
        # 10. Announce (without the m11-releases bus the event is only logged).
        event = {"detail-type": "m11.release.deployed", "stage": self.stage, "manifestVersion": mv,
                 "release": release, "runtimeVersion": new_version, "path": resolution.path}
        self.log(json.dumps(event))
        self._mark("publish")
        self._ledger("release-deployed", manifestVersion=mv, runtimeVersion=new_version, path=resolution.path,
                     seconds=int(time.time() - t0), timings=dict(self.timings))
        return {"manifestVersion": mv, "stage": self.stage, "path": resolution.path, "runtimeVersion": new_version,
                "endpoints": serving, "seconds": int(time.time() - t0), "bundleVersion": version,
                "timings": dict(self.timings), "apiUrl": out.get("apiUrl"), "ui": ui}

    # ------------------------------------------------------------ the mr-* UI preview
    def _stack_config(self) -> dict:
        return json.loads(self.pulumi("config", "--stack", self.ctx.stack(self.stage), "--json").stdout or "{}")

    def _ui_prepare(self) -> None:
        """Decide (unless already wired) and, for `wire`, set the Vercel config and token before `pulumi up`."""
        from module11.release import ui_preview as UI
        stack = self.ctx.stack(self.stage)
        self._ui_wired = (self._stack_config().get("m11-harness:uiPreview") or {}).get("value") == "wired"
        if self.ui.decide(already_wired=self._ui_wired).action != UI.WIRE:
            return
        for key, value in self.ui.config().items():
            self.pulumi("config", "set", "--stack", stack, key, value)
        self.extra_env.update(self.ui.env())

    def _ui_finish(self, out: dict, inputs: Any) -> dict:
        from module11.release import ui_preview as UI
        stack = self.ctx.stack(self.stage)
        if self.ui.decision.action != UI.WIRE:
            return self.ui.record("skip")
        if self._ui_wired:
            return self.ui.record("wire", origin=out.get("uiOrigin"), state="wired earlier")

        def reapply(origin: str) -> dict:
            self.pulumi("config", "set", "--stack", stack, "uiOrigin", origin)
            return self.up(inputs, "ui-origin")

        result = self.ui.wire(out, reapply=reapply)
        if result["action"] == "wire":
            self.pulumi("config", "set", "--stack", stack, "uiPreview", "wired")
        self._ledger("ui-preview", **{k: v for k, v in result.items() if k != "note"})
        return result

    # ------------------------------------------------------------ the controller
    def _command(self, command: str, args: dict, **kw: Any) -> dict:
        from . import rollouts
        lam = rollouts.lambda_client(self.deployer, self.ctx.region)
        result = rollouts.invoke(lam, self.stage, command, args, log=self.log, **kw)
        self.log(f"controller {command}: {result.get('state')} (revision {result.get('revision')})")
        return result

    def _rollout(self, record: dict, declared: dict, plan: DT.ChangePlan, out: dict, deployment: dict,
                 full_current: dict, stable: dict, new_version: str, release: str, sealed: dict, t0: float,
                 effective_kinds: list[str] | None = None, experiment: str | None = None) -> dict:
        """Path `rollout`: no flags write; retarget `candidate` only; smoke it; write
        `/candidate`; send `open` to the controller, which enables its schedule."""
        mv = record["manifestVersion"]
        if not out.get("controllerFunction"):
            raise DeployError(f"{self.stage} has no controller to open a rollout")
        if set(effective_kinds if effective_kinds is not None else plan.kinds) & {"flags", "edge_code"}:
            raise DT.ChangePlanRejected("flags and edge changes apply to both arms at once and are not rolled back; "
                                        "the deploy produced a runtime change")
        stable_version = str(stable["runtimeVersion"])
        previous = str(endpoints.get(self.ctl, out["runtimeId"], "candidate").get("liveVersion"))
        t = time.time()
        endpoints.retarget(self.ctl, out["runtimeId"], "candidate", new_version)
        self.log(f"endpoint candidate -> v{new_version} READY in {int(time.time() - t)} s (stable stays v{stable_version})")
        self._mark("retarget")
        try:
            self._edge_probes(out["apiUrl"])
            self._smoke(record, out, "candidate", new_version, release, sealed)
        except D.SmokeFailed:
            endpoints.retarget(self.ctl, out["runtimeId"], "candidate",
                               previous if previous not in ("None", "") else stable_version)
            self.log("smoke failed on candidate: candidate retargeted back; no rollout opened")
            raise
        self._mark("smoke")
        serving = {"stable": stable_version, "candidate": new_version}
        deployment["endpoints"] = serving
        deployment["flagsObject"] = dict(full_current["deployment"][self.stage].get("flagsObject") or {})
        full = D.deployed_manifest(declared, self.stage, deployment)
        full_uri, _ = releases.put_json(self.s3, self.rb, f"release-inputs/{self.stage}/{mv}.deployed.json", full)
        params.stage_put(self.ssm, self.stage, "candidate", D.compact_current(full, full_uri))
        stage_profile = str(out.get("rolloutProfile") or "prod")
        profile = stage_profile if stage_profile == "demo" else (DT.profile_for(plan, stage_profile) or stage_profile)
        rollout_id = "r-" + uuid.uuid4().hex[:16]
        open_args = {"rolloutId": rollout_id, "profile": profile, "stableVersion": int(stable_version),
                     "candidateVersion": int(new_version), "behaviorDigest": record["behaviorDigest"],
                     "manifestVersion": mv, "release": release}
        if experiment:
            # Shadow mode: every session shadowed, none routed to the candidate; the verdict never promotes.
            profile = "model"
            open_args.update(profile=profile, experiment=True, shadowPct=100)
        result = self._command("open", open_args)
        self.log(f"rollout {rollout_id} opened: {result.get('state')} at {profile} profile "
                 f"(candidate v{new_version}, stable v{stable_version})")
        event = {"detail-type": "m11.release.deployed", "stage": self.stage, "manifestVersion": mv, "release": release,
                 "runtimeVersion": new_version, "path": "rollout", "rolloutId": rollout_id}
        self.log(json.dumps(event))
        self._mark("open")
        self._ledger("release-deployed", manifestVersion=mv, runtimeVersion=new_version, path="rollout",
                     rolloutId=rollout_id, seconds=int(time.time() - t0), timings=dict(self.timings))
        return {"manifestVersion": mv, "stage": self.stage, "path": "rollout", "runtimeVersion": new_version,
                "endpoints": serving, "seconds": int(time.time() - t0), "rolloutId": rollout_id,
                "state": result.get("state"), "profile": profile, "timings": dict(self.timings)}

    # ------------------------------------------------------------ helpers
    def _check_base(self, plan: DT.ChangePlan, declared: dict) -> DT.BaseDecision:
        """StaleBase: the plan's base is prod /current. If it moved, re-plan with
        the same sealed hashes: a `noop` re-plan ends a rollout-stage deploy, a stricter one fails StaleBase,
        otherwise the old plan's approval, scope and rollout apply with the re-plan's kinds; then C16 against
        the actual base on rollout stages. Operator paths without a pipeline plan pass straight through."""
        if naming.is_mr(self.stage) or self.stage == "dev":
            # dev is deployed only by operators (experiments, canary tests), never by the pipeline.
            return DT.BaseDecision("proceed", plan, list(plan.kinds))
        from module11.release import stage as cfg  # rules come from the sealed tree in CI; locally from the checkout
        rules, unknown, _ = cfg.change_types(self.ctx.root)
        prod = params.stage_get(self.ssm, "prod", "current")
        base_now = (prod or {}).get("manifestVersion")

        def replan() -> DT.ChangePlan:
            # The declared manifest of prod's release (every deployer may read releases/*; the hash sets match).
            uri = f"s3://{self.rb}/releases/{base_now}/manifest.json"
            base = json.loads(releases.get(self.s3, uri)) if base_now else None
            return DT.detect(M.hash_set(declared), M.hash_set(base) if base else None, rules, unknown,
                             base_manifest_version=base_now, new_tools=declared.get("tools"),
                             base_tools=(base or {}).get("tools"))

        decision = DT.check_base(plan, base_now, replan, self.stage)
        if decision.log:
            self.log(decision.log)
            self._ledger("base-advanced", planned=plan.baseManifestVersion, now=base_now, action=decision.action,
                         kinds=decision.effective_kinds)
        DT.deploy_c16(decision, rules, unknown, self.stage, plan.c16Base, base_now)
        return decision

    def _default_log_retention(self, runtime_id: str) -> None:
        """The -DEFAULT group is created by the service without retention; set 30 days."""
        name = f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"
        try:
            self.logs.put_retention_policy(logGroupName=name, retentionInDays=30)
        except Exception as exc:
            self.log(f"WARN retention on {name}: {type(exc).__name__}")

    def _write_flags(self, record: dict) -> dict:
        bucket = naming.artifacts_bucket(self.stage, self.ctx.account, self.ctx.region)
        body = self._get_verified(record["flags"], "flags")
        try:
            head = self.s3.get_object(Bucket=bucket, Key="config/flags.json")
            existing = head["Body"].read()
            state = {"previousVersionId": head.get("VersionId"), "etag": head.get("ETag")}
        except Exception as exc:
            if "NoSuchKey" not in str(exc) and "404" not in str(exc):
                raise
            existing, state = None, {"previousVersionId": None, "etag": None}
        if existing is not None and sha256_bytes(existing) == sha256_bytes(body):
            state.update(written=False, versionId=state["previousVersionId"])
            return state
        kwargs = {"Bucket": bucket, "Key": "config/flags.json", "Body": body, "ContentType": "application/json"}
        kwargs.update({"IfMatch": state["etag"]} if existing is not None else {"IfNoneMatch": "*"})
        put = self.s3.put_object(**kwargs)
        state.update(written=True, versionId=put.get("VersionId"), newEtag=put.get("ETag"))
        self.log(f"flags: config/flags.json written ({record['flagsSha256'][:19]}…)")
        return state

    def _edge_probes(self, api_url: str) -> None:
        def status(method: str) -> int:
            request = urllib.request.Request(api_url.rstrip("/") + "/invoke", method=method,
                                             data=b"{}" if method == "POST" else None,
                                             headers={"Content-Type": "application/json"})
            try:
                with open_url(request, timeout=30) as response:
                    return response.status
            except urllib.error.HTTPError as error:
                return error.code
        post, options = status("POST"), status("OPTIONS")
        self.log(f"edge probes: unauthenticated POST /invoke {post}, OPTIONS /invoke {options}")
        if post != 401 or options != 200:
            raise D.SmokeFailed(f"edge probes: POST {post} (want 401), OPTIONS {options} (want 200)")

    def _smoke(self, record: dict, out: dict, endpoint: str, version: str, release: str, sealed: dict) -> None:
        runner = self.ctx.assume(naming.eval_runner_role(self.stage), self.stage, f"smoke {self.stage}")
        core = runner.client("bedrock-agentcore", region_name=self.ctx.region)
        request_id = f"smoke-{uuid.uuid4().hex[:12]}"
        session = f"m11-{naming.stagekey(self.stage)}-{endpoint}-v{int(version)}-{uuid.uuid4()}"
        envelope = D.smoke_envelope(record, release, request_id)
        t = time.time()
        response = core.invoke_agent_runtime(agentRuntimeArn=out["runtimeArn"], qualifier=endpoint,
                                             runtimeSessionId=session, contentType="application/json",
                                             accept="text/event-stream", payload=json.dumps(envelope).encode())
        text = response["response"].read().decode("utf-8", "replace")
        try:
            core.stop_runtime_session(agentRuntimeArn=out["runtimeArn"], qualifier=endpoint, runtimeSessionId=session)
        except Exception as exc:  # the 60 s idle timeout ends it anyway
            self.log(f"WARN stop_runtime_session: {type(exc).__name__}")
        required = (sealed["orchestration.yaml"].get("requiredTools") or {}).get("companion", [])
        verdict = D.smoke_verdict(text, required)
        self.log(f"smoke on {endpoint} v{version}: {verdict.status}, tools {verdict.executed} in {int(time.time() - t)} s")
        if not verdict.ok:
            raise D.SmokeFailed(f"smoke: status {verdict.status}, missing {verdict.missing}, detail {verdict.detail}")

    def _restore(self, failure: Exception, plan, out, previous, targets, flags_state, full_current, control_name,
                 entries) -> None:
        """7a N5: endpoints back, then stage-wide restore (flags object, --reapply)."""
        self.log(f"smoke failed: {failure}; restoring")
        for name in targets:
            if previous.get(name) and previous[name] != "None":
                endpoints.retarget(self.ctl, out["runtimeId"], name, previous[name])
        if not full_current:
            return                               # a bootstrap stage has no /current to restore to
        steps = D.restore_plan(plan.kinds, flags_written=bool(flags_state.get("written")), has_current=True)
        if "flags" in steps and flags_state.get("previousVersionId"):
            bucket = naming.artifacts_bucket(self.stage, self.ctx.account, self.ctx.region)
            old = self.s3.get_object(Bucket=bucket, Key="config/flags.json", VersionId=flags_state["previousVersionId"])
            try:
                self.s3.put_object(Bucket=bucket, Key="config/flags.json", Body=old["Body"].read(),
                                   IfMatch=flags_state["newEtag"], ContentType="application/json")
            except Exception as exc:
                if "PreconditionFailed" in str(exc) or "412" in str(exc):
                    raise D.SmokeFailedFlagsChanged("the flags object changed since this deploy wrote it") from exc
                raise
        if "reapply" in steps:
            self.reapply()

    def destroy(self, *, remove_stack: bool = True) -> dict:
        """Destroy the stage's stack as its deployer (preview:destroy for mr-*), delete the
        service-created -DEFAULT runtime log group, then `stack rm`. Ledger entries are kept."""
        outputs = {}
        self.ensure_stack()
        if (self._stack_config().get("m11-harness:enableVercel") or {}).get("value") == "true":
            from . import ui_wiring
            self.extra_env.update(ui_wiring.token_env(self.ctx.admin))   # the Vercel variables are deleted too
        try:
            outputs = self.outputs()
        except DeployError:
            pass
        t = time.time()
        inputs = self.local / f"{self.stage}-release-inputs.json"
        self.pulumi("destroy", "--stack", self.ctx.stack(self.stage), "--yes", "--non-interactive",
                    "--suppress-progress", inputs_path=inputs if inputs.is_file() else None,
                    log_name=f"{self.stage}-destroy-{int(t)}.log")
        if outputs.get("runtimeId"):
            try:
                self.logs.delete_log_group(logGroupName=f"/aws/bedrock-agentcore/runtimes/{outputs['runtimeId']}-DEFAULT")
            except Exception as exc:
                self.log(f"WARN -DEFAULT log group: {type(exc).__name__}")
        if remove_stack:
            self.pulumi("stack", "rm", self.ctx.stack(self.stage), "--yes", "--non-interactive")
        self._ledger("stack-destroyed", seconds=int(time.time() - t), runtimeId=outputs.get("runtimeId"))
        self.log(f"destroyed {self.ctx.stack(self.stage)} in {int(time.time() - t)} s")
        return {"stage": self.stage, "seconds": int(time.time() - t)}

    # ------------------------------------------------------------ operator variants
    def reapply(self) -> dict:
        """`--reapply`: /current's recorded release inputs with retainedPins = pins_of(/current)."""
        current = params.stage_get(self.ssm, self.stage, "current")
        if not current:
            raise DeployError(f"{self.stage} has no /current to reapply")
        self.ensure_stack()                      # a fresh checkout (CodeBuild) has no Pulumi.<stage>.yaml
        dep = current["deployment"][self.stage]
        doc = json.loads(self._get_verified(dep["releaseInputs"], "current release inputs"))
        own = P.pins_of(current, self.stage, lambda uri: json.loads(releases.get(self.s3, uri)))
        doc["retainedPins"] = P.union(own).to_dict()
        from module11.release.pulumi_inputs import ReleaseInputs
        out = self.up(ReleaseInputs.from_dict(doc), "reapply", refresh=True)
        params.stage_put(self.ssm, self.stage, "applied", D.applied_doc(
            inputs_uri=out["_inputs"]["uri"], inputs_sha=out["_inputs"]["sha256"],
            manifest_version=current["manifestVersion"], applied_at=_now(), policy_digests=out["policyDigests"],
            pins_uri=dep["pinsUri"]))
        return out

    def prune(self) -> dict:
        """`release prune`: /applied's inputs with retainedPins = pins_of(/current)."""
        current = params.stage_get(self.ssm, self.stage, "current")
        applied = params.stage_get(self.ssm, self.stage, "applied")
        if not current or not applied:
            raise DeployError(f"{self.stage} has nothing to prune")
        self.ensure_stack()                      # a fresh checkout (CodeBuild) has no Pulumi.<stage>.yaml
        dep = current["deployment"][self.stage]
        live = {n: str(endpoints.get(self.ctl, dep["runtimeId"], n).get("liveVersion"))
                for n in naming.endpoints_for(self.stage)}
        control = control_table.get(self.ddb, naming.table(self.stage, "control")) or {}
        P.check_prune_safe(dep["runtimeVersion"], live, control.get("endpoints") or {})
        doc = json.loads(self._get_verified({"uri": applied["releaseInputsUri"], "sha256": applied["sha256"]}, "applied"))
        own = P.pins_of(current, self.stage, lambda uri: json.loads(releases.get(self.s3, uri)))
        doc["retainedPins"] = P.union(own).to_dict()
        from module11.release.pulumi_inputs import ReleaseInputs
        out = self.up(ReleaseInputs.from_dict(doc), "prune")
        granted = P.Pins.from_dict(out["effectivePins"]["granted"])
        stamp = _now().replace(":", "")
        pins_uri, _ = releases.put_json(self.s3, self.rb,
                                        f"release-inputs/{self.stage}/{current['manifestVersion']}.prune-{stamp}.pins.json",
                                        P.pins_object(self.stage, current["manifestVersion"], own, granted))
        params.stage_put(self.ssm, self.stage, "applied", D.applied_doc(
            inputs_uri=out["_inputs"]["uri"], inputs_sha=out["_inputs"]["sha256"], manifest_version=applied["manifestVersion"],
            applied_at=_now(), policy_digests=out["policyDigests"], pins_uri=pins_uri))
        return {"stage": self.stage, "granted": granted.to_dict()}

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`module11 partners up [--pipeline-only] [--preview]`: apply m11-partners.

- Reads devops-companion/partners/{gitlab-token,vercel-token} from Secrets Manager into memory and exports
  them only to its own `pulumi` subprocess (GITLAB_TOKEN, VERCEL_API_TOKEN). Nothing is printed or written.
- Resolves the bot user (GET /user) and the approvers: group IDs from the project's `shared_with_groups`,
  else the user form from `approverUsers` (the bot cannot see private subgroups).
- First run: imports the existing GitLab project (`importProject=true`, project URN only), then applies.
- `--pipeline-only` (the first-seed order, PARTNER_SETUP.md): `pulumi up --target` for the protected tags,
  the three protected environments, the sub-claim components (on the project) and the CI variables.
- Full: also removes GitLab's default protection of `main` right before the strict protection is first
  created, registers the bot's SSH signing key (or records why it could not) and runs the direct-and-inherited
  group-overlap check.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

STACK = "organization/m11-partners/shared"
PROJECT_DIR = "module11/infrastructure/partners"
GROUPS = ("harness-owners", "platform", "quality")
SIGNING_KEY_SECRET = "devops-companion/partners/gitlab-bot-signing-key"  # nosec B105 - a Secrets Manager secret name, not a secret value
# The merge gate the design requires. The import path declares the project with its name only (an import must
# match the live project), and the provider reads an unset allow_merge_on_skipped_pipeline as false, so Pulumi
# never applies these to an imported project; `ensure_merge_settings` sets and verifies them through the API.
MERGE_SETTINGS = {"only_allow_merge_if_pipeline_succeeds": True, "allow_merge_on_skipped_pipeline": False}


def merge_settings_drift(project: dict) -> dict:
    """The MERGE_SETTINGS the project does not hold exactly (None counts as unset, not as False)."""
    return {k: v for k, v in MERGE_SETTINGS.items() if project.get(k) is not v}


def _say(text: str = "", style: str | None = None) -> None:
    from .cli import say
    say(text, style)


def _secret(sm: Any, name: str) -> str:
    value = sm.get_secret_value(SecretId=name)["SecretString"].strip()
    if value.startswith("{"):
        value = str(json.loads(value).get("token", "")).strip()
    return value


def _user_ids(gl, group: str, names: list[str]) -> list[dict]:
    ids = []
    for name in names:
        found = [u for u in gl.get(f"/users?username={name}") or [] if u.get("username") == name]
        if not found:
            raise RuntimeError(f"GitLab user {name} not found")
        ids.append({"user_id": int(found[0]["id"])})
    return ids


def resolve_approvers(gl, group_root: str, approver_users: dict[str, list[str]]) -> tuple[dict, str]:
    """({group: [{group_id}] | [{user_id}, ...]}, 'groups' | 'users'). `quality` is always named users
    (gitlab_project.USER_ONLY): a group rule can count an Owner inherited from the parent group."""
    shared = {g.get("group_full_path"): g.get("group_id") for g in gl.project().get("shared_with_groups") or []}
    wanted = {g: shared.get(f"{group_root}/{g}") for g in GROUPS}
    if all(wanted.values()):
        if not approver_users.get("quality"):
            raise RuntimeError("name the quality approvers as users (--approver-users / M11_APPROVER_USERS): a group "
                               "rule can count an Owner inherited from the parent group")
        out = {g: [{"group_id": int(i)}] for g, i in wanted.items() if g != "quality"}
        out["quality"] = _user_ids(gl, "quality", approver_users["quality"])
        return out, "groups"
    out = {}
    for group in GROUPS:
        names = approver_users.get(group) or []
        if not names:
            raise RuntimeError(f"shared_with_groups does not show {group_root}/{group} to the bot and approverUsers "
                               f"names nobody for {group}")
        out[group] = _user_ids(gl, group, names)
    return out, "users"


class Partners:
    def __init__(self, root: Path, *, log=_say) -> None:
        from .cli_live import _context
        self.root, self.log = root, log
        self.ctx = _context(root)
        sm = self.ctx.admin.client("secretsmanager")
        self._gitlab_token = _secret(sm, os.environ.get("M11_GITLAB_TOKEN_SECRET", "devops-companion/partners/gitlab-token"))
        self._vercel_token = _secret(sm, os.environ.get("M11_VERCEL_TOKEN_SECRET", "devops-companion/partners/vercel-token"))
        from .aws import gitlab_api as G
        self.gl = G.GitLab(token=self._gitlab_token, project_id=os.environ.get("M11_GITLAB_PROJECT_ID", ""))
        self.dir = root / PROJECT_DIR

    def _env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in {"GITLAB_TOKEN", "VERCEL_API_TOKEN"}}
        env.update({"GITLAB_TOKEN": self._gitlab_token, "VERCEL_API_TOKEN": self._vercel_token,
                    "PULUMI_SKIP_UPDATE_CHECK": "1"})
        return env

    def pulumi(self, *args: str, check: bool = True, log_name: str | None = None) -> subprocess.CompletedProcess:
        result = subprocess.run(["pulumi", "-C", str(self.dir), *args], env=self._env(), capture_output=True, text=True)
        if log_name:
            out = self.root / "module11/.local" / log_name
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(result.stdout + "\n" + result.stderr, encoding="utf-8")
        if check and result.returncode != 0:
            tail = "\n".join((result.stdout + result.stderr).splitlines()[-40:])
            raise RuntimeError(f"pulumi {args[0]} failed ({result.returncode}):\n{tail}")
        return result

    def ensure_stack(self) -> None:
        venv = self.dir / "venv"
        if not venv.exists():
            target = Path(os.environ.get("VENV", "")) if os.environ.get("VENV") else None
            if target and target.is_dir():
                venv.symlink_to(target)
        listed = self.pulumi("stack", "ls", "--json", check=False)
        names = {s.get("name") for s in json.loads(listed.stdout or "[]")} if listed.returncode == 0 else set()
        if not names & {"shared", STACK, "organization/m11-partners/shared"}:
            alias = "alias/m11-pulumi-secrets"
            self.pulumi("stack", "init", STACK, "--secrets-provider", f"awskms://{alias}?region={self.ctx.region}")
            self.log(f"created stack {STACK}")

    def in_state(self, urn_suffix: str) -> bool:
        result = self.pulumi("stack", "export", "--stack", STACK, check=False)
        if result.returncode != 0:
            return False
        resources = (json.loads(result.stdout or "{}").get("deployment") or {}).get("resources") or []
        return any(str(r.get("urn", "")).endswith(urn_suffix) for r in resources)

    def configure(self, approver_users: dict[str, list[str]], vercel_team: str) -> dict:
        bot = self.gl.current_user()
        project = self.gl.project()
        group_root = project["namespace"]["full_path"]
        approvers, form = resolve_approvers(self.gl, group_root, approver_users)
        if form == "users":
            self.log("approvers: user form (shared_with_groups is not visible to the bot)", "yellow")
        values = {"gitlabProject": project["path_with_namespace"], "gitlabProjectId": str(project["id"]),
                  "gitlabProjectName": project["name"], "botUserId": str(bot["id"]), "botLogin": bot["username"],
                  "botCommitEmail": bot_commit_email(bot),
                  "vercelTeamId": vercel_team, "vercelTeamSlug": self.vercel_team_slug(vercel_team),
                  "approvers": json.dumps(approvers, sort_keys=True)}
        for key, value in values.items():
            self.pulumi("config", "set", "--stack", STACK, key, value)
        return {"bot": bot["username"], "approvers": form}

    def up(self, *, pipeline_only: bool, preview: bool = False) -> dict:
        from .infrastructure.partners import gitlab_project as GP   # pulumi-gitlab only when applying
        project_urn = f"{GP.TYPES['project']}::project"
        verb = "preview" if preview else "up"
        tail = ["--diff"] if preview else ["--yes"]
        if not self.in_state(project_urn):
            self.log("first run: importing the existing GitLab project (importProject=true)")
            urn = f"urn:pulumi:shared::m11-partners::{project_urn}"
            self.pulumi(verb, "--stack", STACK, "--config", "importProject=true", "--target", urn,
                        "--non-interactive", "--suppress-progress", *tail, log_name="partners-import.log")
            self.pulumi("config", "set", "--stack", STACK, "importProject", "false")
            if preview:
                return {"preview": "import"}
        args = [verb, "--stack", STACK, "--non-interactive", "--suppress-progress", *tail]
        if pipeline_only:
            keys = ["AWS_REGION", "M11_OIDC_AUD", "M11_GITLAB_CI_ROLE_ARN", "M11_GITLAB_CI_INFRA_ROLE_ARN",
                    "M11_PROMOTER_ROLE_ARN"]
            for urn in GP.pipeline_only_urns("shared", keys):
                args += ["--target", urn]
        elif not preview and not self.in_state("gitlab:index/branchProtection:BranchProtection::main-protection"):
            self._remove_default_main_protection()
        started = time.time()
        result = self.pulumi(*args, log_name=f"partners-{verb}{'-pipeline-only' if pipeline_only else ''}.log")
        self.log(f"pulumi {verb} ({'pipeline-only' if pipeline_only else 'full'}) in {int(time.time() - started)} s")
        return {"stdout": result.stdout[-2000:]}

    def vercel_team_slug(self, team_id: str) -> str:
        """The team's slug, the last label of every branch alias (`<project>-git-<branch>-<slug>.vercel.app`)."""
        from .aws import vercel_api as V
        team = V.Vercel(token=self._vercel_token, team_id=team_id)._request("GET", f"/v2/teams/{team_id}") or {}
        if not team.get("slug"):
            raise RuntimeError(f"Vercel team {team_id} has no slug (check the vercel-token's scope)")
        return str(team["slug"])

    def platform_config(self) -> dict:
        """Copy the outputs mr-* deploys need (the Vercel project, the bot's login and commit email) into m11-platform's
        config; its next apply writes them to /m11/devops-companion/shared/outputs."""
        out = json.loads(self.pulumi("stack", "output", "--stack", STACK, "--json").stdout or "{}")
        values = {"vercelProjectId": out.get("vercelProjectId"), "vercelTeamId": out.get("vercelTeamId"),
                  "vercelTeamSlug": out.get("vercelTeamSlug"), "botLogin": out.get("botLogin"),
                  "botCommitEmail": out.get("botCommitEmail")}
        platform = self.root / "module11/infrastructure/platform"
        for key, value in values.items():
            if value:
                subprocess.run(["pulumi", "-C", str(platform), "config", "set", "--stack", "shared", key, str(value)],
                               env=self._env(), capture_output=True, text=True, check=True)
        return values

    def ensure_merge_settings(self) -> str:
        """Apply after the 10/13 recording: set and read back the merge gate on the import path too."""
        wrong = merge_settings_drift(self.gl.project())
        if not wrong:
            return "ok"
        self.gl.put(self.gl.p, wrong)
        still = merge_settings_drift(self.gl.project())
        if still:
            raise RuntimeError(f"GitLab kept {', '.join(sorted(still))} after the update; set them in Settings > "
                               "Merge requests")
        return "set " + ", ".join(sorted(wrong))

    def ensure_web_commit_signing(self) -> str:
        """Web-based commit signing, so the bot's Commits-API commits show Verified. The provider has no
        input for it; the project API accepts it from a Maintainer token (found live)."""
        if self.gl.project().get("web_based_commit_signing_enabled"):
            return "on"
        self.gl.put(self.gl.p, {"web_based_commit_signing_enabled": True})
        return "on" if self.gl.project().get("web_based_commit_signing_enabled") else "unavailable"

    def overlap_check(self, group_root: str) -> list:
        """`quality` shares no member with harness-owners or platform, direct (what CODEOWNERS
        counts) and inherited. Returns check.py findings, or None when the bot cannot read the private subgroups,
        in which case the presenter confirms it in the group pages."""
        from .aws.gitlab_api import GitLabError
        from .release import check as C
        direct, inherited = {}, {}
        try:
            for group in GROUPS:
                path = f"/groups/{group_root}%2F{group}"
                direct[group] = [m["username"] for m in self.gl.paginate(f"{path}/members")]
                inherited[group] = [m["username"] for m in self.gl.paginate(f"{path}/members/all")]
        except GitLabError as exc:
            if exc.status in (403, 404):
                return None
            raise
        return C.groups_disjoint(direct, inherited)

    def _remove_default_main_protection(self) -> None:
        for branch in self.gl.get(f"{self.gl.p}/protected_branches") or []:
            if branch.get("name") == "main":
                self.gl.delete(f"{self.gl.p}/protected_branches/main")
                self.log("removed GitLab's default protection of main (the strict protection follows)")

    def register_signing_key(self) -> str:
        """Generate the bot's SSH signing key once (never printed), store it, register its public half."""
        sm = self.ctx.admin.client("secretsmanager")
        try:
            sm.describe_secret(SecretId=SIGNING_KEY_SECRET)
            return "present"
        except Exception as exc:  # noqa: BLE001 - ResourceNotFoundException means: create it
            if "ResourceNotFound" not in type(exc).__name__ and "ResourceNotFound" not in str(exc):
                raise
        with tempfile.TemporaryDirectory(prefix="m11-key-") as tmp:
            os.chmod(tmp, 0o700)  # nosemgrep: insecure-file-permissions - owner-only: the directory holds the new private key
            key = Path(tmp) / "bot"
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "m11-bot-signing", "-f", str(key)],
                           check=True, capture_output=True)
            private, public = key.read_text(), (Path(str(key) + ".pub")).read_text().strip()
        try:
            self.gl.add_signing_key("m11-bot-signing", public, "2026-12-20")
        except Exception as exc:  # noqa: BLE001 - a bot user may not add its own key
            self.log(f"the bot could not register a signing key ({type(exc).__name__}); bot commits stay "
                     "GitLab web commits", "yellow")
            return "refused"
        sm.create_secret(Name=SIGNING_KEY_SECRET, SecretString=private,
                         Tags=[{"Key": "Project", "Value": "m11-devops-companion"}, {"Key": "Stage", "Value": "shared"}])
        return "registered"


def bot_commit_email(bot: dict) -> str:
    """The address GitLab writes as the author of the bot's commits (a project bot has a noreply address)."""
    return str(bot.get("commit_email") or bot.get("public_email") or f"{bot['username']}@noreply.gitlab.com")


def cmd_partners_up(args) -> int:
    approver_users = json.loads(args.approver_users) if args.approver_users else {}
    partners = Partners(args.root)
    partners.ensure_stack()
    info = partners.configure(approver_users, args.vercel_team or os.environ.get("M11_VERCEL_TEAM_ID", ""))
    if not args.pipeline_only and not args.preview:
        _say(f"bot signing key: {partners.register_signing_key()}")
        _say(f"web-based commit signing: {partners.ensure_web_commit_signing()}")
        findings = partners.overlap_check(partners.gl.project()["namespace"]["full_path"])
        if findings is None:
            _say("group overlap: the bot cannot read the private subgroups; confirm in the group pages that "
                 "quality's direct members are not in harness-owners or platform (PARTNER_SETUP.md)", "yellow")
        else:
            for f in findings:
                _say(f"group overlap ({f.severity}): {f.message}", "red" if f.severity == "error" else "yellow")
            if any(f.severity == "error" for f in findings):
                return 1
            if not findings:
                _say("group overlap: none (direct and inherited)", "green")
    partners.up(pipeline_only=args.pipeline_only, preview=args.preview)
    if not args.preview:
        _say(f"merge settings: {partners.ensure_merge_settings()}")
    if not args.pipeline_only and not args.preview:
        copied = partners.platform_config()
        _say(f"m11-platform config: {', '.join(k for k, v in copied.items() if v)} copied; apply m11-platform next "
             "(it writes them to the shared outputs mr-* deploys read)", "yellow")
    _say(f"m11-partners {'previewed' if args.preview else 'applied'} "
         f"({'pipeline-only' if args.pipeline_only else 'full'}; bot {info['bot']}; approvers {info['approvers']})",
         "green")
    return 0


def register(sub) -> None:
    pa = sub.add_parser("partners", help="GitLab project governance and the Vercel project (m11-partners)")
    psub = pa.add_subparsers(dest="partners_command", required=True)
    up = psub.add_parser("up")
    up.add_argument("--pipeline-only", action="store_true",
                    help="only the protected tags, environments, sub-claim components and CI variables (first seed)")
    up.add_argument("--preview", action="store_true")
    up.add_argument("--approver-users", default=os.environ.get("M11_APPROVER_USERS", ""),
                    help='the approvers user form, JSON: {"harness-owners": ["user"], "platform": [...], "quality": [...]}')
    up.add_argument("--vercel-team", default=None)
    up.set_defaults(fn=cmd_partners_up)

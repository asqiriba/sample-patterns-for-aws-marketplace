# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Change detection: hash sets in, a ChangePlan out (5).

Pure. Callers pass hash sets and manifests they read; nothing here touches AWS or git.
"""
from __future__ import annotations

import fnmatch
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable, Mapping

from .naming import is_rollout_stage, MR_STAGE
from .stage import APPROVALS, ROLLOUTS, RUNTIME_VERSIONS, Rule

CANARY_CLASS = ("canary", "canary-if-runtime-changed", "shadow-then-canary")
STAGE_WIDE = ("flags", "edge_code")
MSG_OWN_MR = ("flags and edge changes apply to both arms at once and are not rolled back; "
              "land them in their own MR")
MSG_QUEUED = ("the release queued ahead of this one did not promote; revert it on main or re-land this change")
BOTH_ARMS = "applies to both arms immediately and is not reverted by rollback"


class ChangePlanRejected(ValueError):
    pass


class StaleBase(RuntimeError):
    pass


@dataclass
class ChangePlan:
    baseManifestVersion: str | None
    c16Base: str | None
    kinds: list[str]
    imageRebuild: bool
    handlerBuild: list[str]
    bundleVersion: bool
    runtimeVersion: str
    evalScope: str
    repeatsOverride: dict
    approval: str
    rollout: str
    releaseKind: str
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def variables(self) -> dict[str, str]:
        """The CodePipeline variables Analyze exports."""
        return {"IMAGE_REBUILD": str(self.imageRebuild).lower(), "RELEASE_KIND": self.releaseKind,
                "EVAL_SCOPE": self.evalScope, "APPROVAL": self.approval, "ROLLOUT": self.rollout}


def _rank(order: Iterable[str], value: str) -> int:
    return list(order).index(value)


def _max(order, values, default):
    values = list(values)
    return max(values, key=lambda v: _rank(order, v)) if values else default


def match_rule(artifact_id: str, rules: list[Rule], unknown: Rule) -> Rule:
    for rule in rules:
        if any(fnmatch.fnmatchcase(artifact_id, pattern) for pattern in rule.match):
            return rule
    return unknown


def changed(new: Mapping[str, str], base: Mapping[str, str] | None) -> list[str]:
    if base is None:
        return sorted(new)
    return sorted({a for a in new if new[a] != base.get(a)} | (set(base) - set(new)))


def _tool(artifact_id: str) -> str | None:
    parts = artifact_id.split("/")
    return parts[1] if len(parts) == 3 and parts[0] == "tool" else None


def _merge_scope(scopes: list[str]) -> str:
    if "full" in scopes:
        return "full"
    tags = sorted({s for s in scopes if s.startswith("tag:")})
    if len(tags) > 1:
        return "full"  # one scope per gate: two different subsets widen to the full suite
    if tags:
        return tags[0]
    return "smoke" if "smoke" in scopes else "none"


def detect(new: Mapping[str, str], base: Mapping[str, str] | None, rules: list[Rule], unknown: Rule, *,
           base_manifest_version: str | None = None, new_tools: Mapping | None = None,
           base_tools: Mapping | None = None, c16_base: str | None = None) -> ChangePlan:
    """Map each changed artifact to its first matching rule and merge by maximum."""
    ids = changed(new, base)
    if not ids:
        return ChangePlan(base_manifest_version, c16_base, [], False, [], False, "none", "none", {}, "default",
                          "none", "noop", ["hash set equals the base"])
    lambda_tools = sorted({_tool(a) for a in new if a.endswith("/handler") and _tool(a)})
    kinds: list[str] = []
    handlers: set[str] = set()
    scopes: list[str] = []
    repeats: dict[str, int] = {}
    reasons, notes = [], []
    image = bundle = False
    matched: list[Rule] = []
    for aid in ids:
        rule = match_rule(aid, rules, unknown)
        matched.append(rule)
        if rule.name not in kinds:
            kinds.append(rule.name)
        reasons.append(f"{aid} -> {rule.name}")
        tool = _tool(aid)
        image |= rule.image
        bundle |= rule.bundle
        if rule.handlers == "all":
            handlers |= set(lambda_tools)
        elif rule.handlers == "tool" and tool in lambda_tools:
            handlers.add(tool)
        elif rule.handlers == "tool_if_limits" and tool in lambda_tools:
            old = (base_tools or {}).get(tool)
            cur = (new_tools or {}).get(tool)
            if old is None or cur is None or any(old.get(k) != cur.get(k) for k in ("timeoutSeconds", "memoryMb")):
                handlers.add(tool)
        if rule.evalScope == "tagged":
            scopes.append(f"tag:{tool}" if tool else "full")
        elif rule.evalScope == "retrieval":
            scopes.append("tag:retrieval")
        elif rule.evalScope in {"full", "smoke"}:
            scopes.append(rule.evalScope)
        for key, value in rule.repeats.items():
            repeats[key] = max(repeats.get(key, 0), int(value))
        if rule.note and rule.note not in notes:
            notes.append(f"{rule.name}: {rule.note}")
    deploy_rules = [r for r in matched if r.releaseKind == "deploy"]
    if deploy_rules:
        release_kind = "deploy"
    elif any(r.releaseKind == "evalset" for r in matched):
        release_kind = "evalset"
    else:
        release_kind = "rebaseline"
    if release_kind == "deploy":
        eval_scope = _merge_scope(scopes)
        if any(r.releaseKind != "deploy" for r in matched):
            notes.append("evaluation files change with behavior: land the suite change first; the gate is "
                         "INCONCLUSIVE until the new baseline exists")
    else:
        eval_scope = release_kind
    if base is None:
        notes.append("bootstrap: no base manifest")
    rollout = _max(ROLLOUTS, [r.rollout for r in matched], "none")
    if rollout in CANARY_CLASS:
        for kind in ("tool_permissions", "infrastructure"):
            if kind in kinds:
                notes.append(f"{kind}: {BOTH_ARMS}")
    return ChangePlan(
        baseManifestVersion=base_manifest_version, c16Base=c16_base, kinds=kinds, imageRebuild=image,
        handlerBuild=sorted(handlers), bundleVersion=bundle,
        runtimeVersion=_max(RUNTIME_VERSIONS, [r.runtimeVersion for r in matched], "none"),
        evalScope=eval_scope, repeatsOverride=repeats,
        approval=_max(APPROVALS, [r.approval for r in matched], "default"),
        rollout=rollout, releaseKind=release_kind, reasons=reasons, notes=list(dict.fromkeys(notes)))


# ---------------------------------------------------------------- C16
def canary_class_kinds(rules: list[Rule], unknown: Rule) -> set[str]:
    return {r.name for r in [*rules, unknown] if r.rollout in CANARY_CLASS}


def c16_violation(plan: ChangePlan, has_base: bool = True) -> bool:
    return bool(has_base and plan.rollout in CANARY_CLASS and set(plan.kinds) & set(STAGE_WIDE))


def c16_message(plan: ChangePlan, rules: list[Rule], unknown: Rule, own_kinds: Iterable[str] | None = None,
                commit_range: str | None = None) -> str:
    """Which C16 message applies: "main is ahead of prod" when the failing canary-class
    kinds are not in the commit's (or MR's) own diff, else "land them in their own MR"."""
    canary = canary_class_kinds(rules, unknown)
    if own_kinds is None:
        return MSG_OWN_MR
    own = set(own_kinds)
    if own & canary:
        return MSG_OWN_MR
    ahead = [k for k in plan.kinds if k in canary and k not in own]
    if not ahead:
        return MSG_OWN_MR
    return (f"main is ahead of prod by {', '.join(ahead)} from {commit_range or '<prod>..<main>'} (not promoted); "
            "revert or fix them forward on main before landing flags or edge changes")


def enforce_c16(plan: ChangePlan, rules: list[Rule], unknown: Rule, has_base: bool = True,
                own_kinds: Iterable[str] | None = None, commit_range: str | None = None) -> None:
    if c16_violation(plan, has_base):
        raise ChangePlanRejected(c16_message(plan, rules, unknown, own_kinds, commit_range))


def commit_range(prod_commit: str | None, main_commit: str | None) -> str:
    return f"{(prod_commit or '')[:7]}..{(main_commit or '')[:7]}"


def c16_base(executions: list[Mapping], read_manifest: Callable[[str], Mapping], prod_current: Mapping | None,
             own_execution_id: str | None = None) -> tuple[Mapping | None, str]:
    """C16's base: the declared manifest of the newest *other* InProgress
    `m11-release` execution (started before this one, when an own ID is given), else prod
    `/current`. A failed lookup falls back to prod `/current` ("fallback": WARN c16_base_fallback)."""
    try:
        own = next((e for e in executions if e.get("pipelineExecutionId") == own_execution_id), None)
        candidates = [e for e in executions if e.get("status") == "InProgress"
                      and e.get("pipelineExecutionId") != own_execution_id
                      and (own is None or str(e.get("startTime")) < str(own.get("startTime")))]
        if not candidates:
            return prod_current, "prod-current"
        newest = max(candidates, key=lambda e: str(e.get("startTime")))
        commit = newest["sourceRevisions"][0]["revisionId"]
        return read_manifest(commit), "in-flight"
    except Exception:  # noqa: BLE001 - any lookup failure is the stricter fallback by design
        return prod_current, "fallback"


def merged_hashes(main_declared: Mapping[str, str], mr_hashes: Mapping[str, str],
                  mr_changed_ids: Iterable[str]) -> dict[str, str]:
    """main's declared hashes, with each artifact the MR changes taken from the MR tree."""
    out = dict(main_declared)
    for aid in mr_changed_ids:
        if aid in mr_hashes:
            out[aid] = mr_hashes[aid]
        else:
            out.pop(aid, None)
    return out


# ---------------------------------------------------------------- base moves
def _covers(old_scope: str, new_scope: str) -> bool:
    if old_scope == new_scope or new_scope in {"none", "smoke"}:
        return True
    return old_scope == "full" and new_scope.startswith("tag:")


def stricter(new: ChangePlan, old: ChangePlan) -> bool:
    """True when the new plan needs more than the old plan's gate and approvals covered."""
    return (_rank(APPROVALS, new.approval) > _rank(APPROVALS, old.approval)
            or not _covers(old.evalScope, new.evalScope)
            or _rank(ROLLOUTS, new.rollout) > _rank(ROLLOUTS, old.rollout)
            or new.releaseKind != old.releaseKind)


@dataclass
class BaseDecision:
    action: str                 # proceed | noop
    plan: ChangePlan            # the plan whose approval, scope and rollout apply
    effective_kinds: list[str]  # this release's actual delta from the current base (C16)
    log: str | None = None
    replan: ChangePlan | None = None  # the re-plan against the moved base, when it moved


def check_base(plan: ChangePlan, base_now: str | None, replan: Callable[[], ChangePlan], stage: str,
               moves: int = 1) -> BaseDecision:
    """StaleBase / BaseAdvanced. `moves` counts base moves seen so far for an mr-* build."""
    if base_now == plan.baseManifestVersion:
        return BaseDecision("proceed", plan, list(plan.kinds))
    new = replan()
    if MR_STAGE.match(stage):
        if moves > 1:
            raise StaleBase(f"StaleBase({plan.baseManifestVersion}, {base_now}): main moved twice during the build")
        return BaseDecision("proceed", new, list(new.kinds), f"BaseAdvanced({plan.baseManifestVersion}, {base_now})", new)
    if new.releaseKind == "noop":
        if is_rollout_stage(stage):
            return BaseDecision("noop", new, [], "BaseAdvanced(noop)", new)
        return BaseDecision("proceed", plan, list(plan.kinds), f"BaseAdvanced({plan.baseManifestVersion}, {base_now})", new)
    if stricter(new, plan):
        raise StaleBase(f"StaleBase({plan.baseManifestVersion}, {base_now})")
    return BaseDecision("proceed", plan, list(new.kinds), f"BaseAdvanced({plan.baseManifestVersion}, {base_now})", new)


def deploy_c16(decision: BaseDecision, rules: list[Rule], unknown: Rule, stage: str,
               recorded_c16_base: str | None, prod_current_version: str | None) -> None:
    """C16 against the actual base at deploy step 1, rollout stages only: on the re-plan
    when the base moved, otherwise on the recorded plan."""
    if not is_rollout_stage(stage) or decision.action == "noop" or prod_current_version is None:
        return
    effective = decision.replan or decision.plan
    if c16_violation(effective):
        if recorded_c16_base and recorded_c16_base != prod_current_version:
            raise ChangePlanRejected(MSG_QUEUED)
        raise ChangePlanRejected(MSG_OWN_MR)


# ---------------------------------------------------------------- deploy path
@dataclass
class Resolution:
    path: str                   # single | bootstrap | unchanged | equivalent | rollout
    logs: list[str] = field(default_factory=list)


def resolve_path(plan: ChangePlan, stage: str, new: Mapping[str, Any], stable: Mapping[str, Any] | None,
                 effective_kinds: Iterable[str] | None = None) -> Resolution:
    """Decide the runtime path from what `pulumi up` produced, not only from the plan (M8).

    `new`/`stable`: {runtimeVersion, runtimeIdentity, runtimeDigest, imageDigest, infrastructure}."""
    if not is_rollout_stage(stage):
        return Resolution("single")
    if stable is None:
        return Resolution("bootstrap")
    if str(new["runtimeVersion"]) == str(stable["runtimeVersion"]):
        return Resolution("unchanged")
    fields_ = ("runtimeDigest", "imageDigest", "infrastructure", "runtimeIdentity")
    differing = [f for f in fields_ if new.get(f) != stable.get(f)]
    if not differing:
        return Resolution("equivalent", [f"EquivalentRuntimeRetarget({stable['runtimeVersion']}, {new['runtimeVersion']})"])
    kinds = set(effective_kinds if effective_kinds is not None else plan.kinds)
    if kinds & set(STAGE_WIDE):
        raise ChangePlanRejected(f"{MSG_OWN_MR.split(';')[0]}; the deploy produced a runtime change: {differing[0]}")
    logs = [f"RolloutUpgraded({plan.rollout} -> canary)"] if plan.rollout in {"full", "none"} else []
    return Resolution("rollout", logs)


def profile_for(plan: ChangePlan, stage_profile: str = "prod") -> str | None:
    """Rollout value -> controller profile (N2)."""
    if plan.rollout in {"canary", "canary-if-runtime-changed"}:
        return stage_profile
    if plan.rollout == "shadow-then-canary":
        return "model"
    return None

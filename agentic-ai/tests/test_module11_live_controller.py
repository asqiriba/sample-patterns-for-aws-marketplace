# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Rollout controller live checks, opt-in with M11_LIVE_TESTS=1, on dev (prod-shaped, profile `demo`):

- test_live_controller: a real canary of the deliberately bad payments branch (`demo/wording.py: PAYMENTS_TWEAK`)
  with the S7 traffic shape reaches ROLLED_BACK within t0 + 4:30, M11_CONTROLLER_RUNS times (default 3); concurrent
  `rollout tick` invocations serialize; the latch blocks a re-advance; `memory purge` removes only that release's
  records; the run records per-session durations and the v8 effect size to module11/.local/.
- test_live_rollback_keeps_stable_working: canary a tool-handler change, a model change and an
  orchestration change that lowers deadlineSeconds, roll each back (`rollout abort`), invoke stable, check drift is
  clean after each ROLLED_BACK and the adapter timeout stays at /current's value + 30; `release prune`; then a
  flags-only release on the `full` path (EquivalentRuntimeRetarget, /current's bundle version, no rollout) and a
  second one that creates no runtime version (`unchanged`).

Each seeded change is a local branch `m11-seeded/<name>` (never pushed) committed from HEAD; the branches are
deleted at the end, and test latches are cleared with audit records. Run from agentic-ai/ after
`source module11/.local/env.sh`. The shadow probe (staging, no side effects) is test_live_shadow_probe.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("M11_LIVE_TESTS") != "1", reason="live tests need M11_LIVE_TESTS=1")

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
STAGE = "dev"
LOCAL = ROOT / "module11/.local"
BUDGET_SECONDS = 270                     # ROLLED_BACK <= t0 + 4:30
RUNS = int(os.environ.get("M11_CONTROLLER_RUNS", "3"))
SESSIONS = 180                           # the B5 / S7 shape (rollout_sim.S7_SESSIONS)
PROMPT = "agentic-ai/module11/harness/agent/prompts/companion.md"


# ---------------------------------------------------------------- helpers
def cli(*args: str, check: bool = True, log: str | None = None) -> subprocess.CompletedProcess:
    # nosemgrep: dangerous-subprocess-use-audit - list args, no shell; live test: this repo's module11 CLI with the test's arguments
    out = subprocess.run([sys.executable, "-m", "module11", *args], cwd=ROOT, capture_output=True, text=True)
    if log:
        (LOCAL / log).write_text(out.stdout + "\n" + out.stderr, encoding="utf-8")
    if check and out.returncode != 0:
        raise AssertionError(f"module11 {' '.join(args)} exited {out.returncode}:\n{out.stdout[-3000:]}\n{out.stderr[-2000:]}")
    return out


def git(*args: str, cwd: Path = REPO) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def seed(name: str, edits: dict, message: str) -> str:
    """A local branch m11-seeded/<name> = HEAD plus `edits` ({repo path: fn(text) -> text}); returns the branch."""
    branch = f"m11-seeded/{name}"
    if subprocess.run(["git", "-C", str(REPO), "rev-parse", "--verify", "-q", branch], capture_output=True).returncode == 0:
        git("branch", "-D", branch)
    with tempfile.TemporaryDirectory(prefix="m11-seed-") as tmp:
        wt = Path(tmp) / "wt"
        git("worktree", "add", "-q", "--no-checkout", "-b", branch, str(wt), "HEAD")
        try:
            git("sparse-checkout", "set", "--no-cone", *edits.keys(), cwd=wt)
            git("checkout", "-q", branch, cwd=wt)
            for path, fn in edits.items():
                target = wt / path
                target.write_text(fn(target.read_text(encoding="utf-8")), encoding="utf-8")
                git("add", path, cwd=wt)
            git("commit", "-q", "-m", message, cwd=wt)
        finally:
            git("worktree", "remove", "--force", str(wt))
    return branch


def seal(ref: str, log: str) -> str:
    out = cli("release", "seal", "--operator", "--ref", ref, log=log)
    match = re.search(r"^sealed (\S+): ", out.stdout, re.M)
    assert match, out.stdout[-2000:]
    return match.group(1)


def deploy(mv: str, log: str, check: bool = True) -> subprocess.CompletedProcess:
    return cli("release", "deploy", "--stage", STAGE, "--manifest", mv, check=check, log=log)


@pytest.fixture(scope="module")
def ctx():
    from module11.cli_live import _context
    return _context(ROOT)


def control(ctx) -> dict:
    from module11.aws import control_table
    from module11.release import naming
    return control_table.get(ctx.admin.client("dynamodb"), naming.table(STAGE, "control")) or {}


def wait_state(ctx, states: tuple, timeout: float, rollout_id: str | None = None) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        item = control(ctx)
        if item.get("state") in states and (rollout_id is None or item.get("rolloutId") == rollout_id):
            return item
        time.sleep(5)
    raise AssertionError(f"{STAGE} not in {states} within {timeout:.0f} s: {control(ctx).get('state')}")


def current(ctx) -> dict:
    from module11.aws import params
    return params.stage_get(ctx.admin.client("ssm"), STAGE, "current")


def clear_latch(digest: str, reason: str) -> None:
    cli("latch", "clear", digest, "--reason", reason)


def reset_if_terminal(ctx, reason: str) -> None:
    if control(ctx).get("state") in ("ROLLED_BACK", "PROMOTED"):
        cli("rollout", "reset", "--stage", STAGE, "--reason", reason)


def invoke_through_edge(ctx, team: str = "platform") -> dict:
    """One authenticated session through dev's edge (Module 10's terminal client)."""
    from module10.cloud_client import CloudClient
    outputs = LOCAL / "traffic-outputs-dev.json"
    outputs.write_text(json.dumps({"apiUrl": current(ctx)["deployment"][STAGE]["apiUrl"]}), encoding="utf-8")
    client = CloudClient(outputs, Path(os.environ["M11_MODULE10_CREDENTIALS_FILE"]))
    response = client.invoke(team, "The checkout-service is throwing 502s since the 14:20 deploy.",
                             f"m11-live-{uuid.uuid4().hex[:16]}")
    return {"http": response.status_code, **(response.body if isinstance(response.body, dict) else {})}


def drift_findings(ctx) -> list:
    from module11.aws import drift_reads
    report = drift_reads.check(stage=STAGE, admin=ctx.admin, account=ctx.account, region=ctx.region,
                               release_bucket=ctx.release_bucket)
    return [f.text() for f in report.findings if f.severity == "drift"]


def adapter_timeout(ctx) -> int:
    from module11.release import naming
    return int(ctx.admin.client("lambda").get_function_configuration(
        FunctionName=naming.function(STAGE, "adapter"))["Timeout"])


def outcomes(ctx, rollout_id: str) -> list[dict]:
    from module11.aws import control_table
    from module11.release import naming
    return control_table.outcomes(ctx.admin.client("dynamodb"), naming.table(STAGE, "outcomes"), rollout_id)


def payments_rates(rows: list[dict]) -> dict:
    out = {}
    for arm in ("candidate", "stable"):
        pay = [r for r in rows if r.get("arm") == arm and r.get("team") == "payments" and not r.get("shadow")
               and r.get("status") == "completed"]
        out[arm] = {"n": len(pay), "runbooks": sum("search_runbooks" in (r.get("toolsExecuted") or []) for r in pay)}
    return out


# ---------------------------------------------------------------- test_live_controller
def _tick_twice_concurrently(ctx) -> list[dict]:
    from module11.aws import rollouts
    results: list[dict] = []
    lam = rollouts.lambda_client(ctx.admin, ctx.region)

    def one():
        results.append(rollouts.invoke(lam, STAGE, "tick", check=False))
    threads = [threading.Thread(target=one) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    return results


def _canary(ctx, mv: str, digest: str, run: int, record: dict) -> dict:
    from module11.demo import state as S
    from module11.aws import params
    reset_if_terminal(ctx, f"test_live_controller run {run}: start from IDLE")
    if params.latched(ctx.admin.client("ssm"), f"sha256:{digest}"):
        clear_latch(digest, f"test_live_controller run {run + 1} of {RUNS}: re-run the same bad release (test data)")
    out = deploy(mv, log=f"live-controller-deploy-{run + 1}.log")
    rid = re.search(r"rollout (r-[0-9a-f]{16}) opened", out.stdout).group(1)
    # nosemgrep: dangerous-subprocess-use-audit - list args, no shell; live test: this repo's module11 CLI, fixed stage, the rollout ID it opened
    traffic = subprocess.Popen([sys.executable, "-m", "module11", "traffic", "--stage", STAGE, "--sessions", str(SESSIONS),
                                "--concurrency", "20", "--mix", "payments=0.6", "--rollout-id", rid],
                               cwd=ROOT, stdout=open(LOCAL / f"live-controller-traffic-{run + 1}.log", "w"),
                               stderr=subprocess.STDOUT)
    ticks: list[dict] = []
    try:
        deadline = time.time() + 600
        while time.time() < deadline:
            item = control(ctx)
            if item.get("state") == "OBSERVING" and not ticks:
                ticks = _tick_twice_concurrently(ctx)
            if item.get("state") in ("ROLLED_BACK", "PROMOTED") and item.get("rolloutId") == rid:
                break
            time.sleep(3)
        traffic.wait(timeout=400)
    finally:
        if traffic.poll() is None:
            traffic.terminate()
            traffic.wait(timeout=240)
    item = control(ctx)
    driver = S.read().get("traffic") or {}
    assert driver.get("rolloutId") == rid and driver.get("finishedAt"), driver
    t0 = __import__("module11.release.pins", fromlist=["ts"]).ts(driver["startedAt"]).timestamp()
    detected = float(item.get("rolledBackAt") or 0) - t0
    summary = json.loads((LOCAL / f"traffic-{rid}.json").read_text(encoding="utf-8"))
    rates = payments_rates(outcomes(ctx, rid))
    row = {"run": run + 1, "rolloutId": rid, "state": item.get("state"), "reason": item.get("reason"),
           "detectedSeconds": round(detected, 1), "t0": driver["startedAt"], "ticks": ticks, "rates": rates,
           "durations": [s["endedAt"] - s["startedAt"] for s in summary["sessions"] if s["httpStatus"] == 200]}
    record["runs"].append(row)
    (LOCAL / "live-controller-runs.json").write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    return row


def test_live_controller(ctx):
    from module11.demo import wording
    from module11.release import naming
    record = {"payments_tweak": wording.PAYMENTS_TWEAK, "runs": []}
    branch = seed("payments-tweak", {PROMPT: lambda text: wording.apply_edit(text, wording.PAYMENTS_TWEAK)},
                  "Payments: shorter incident answers")
    try:
        mv = seal(branch, "live-controller-seal.log")
        record["manifestVersion"] = mv
        seal_doc = json.loads(cli("manifest", "show", "--ref", branch).stdout)
        digest = seal_doc["digests"]["behavior"].removeprefix("sha256:")
        for run in range(RUNS):
            row = _canary(ctx, mv, digest, run, record)
            assert row["state"] == "ROLLED_BACK", row
            assert row["detectedSeconds"] <= BUDGET_SECONDS, row
            # Concurrent ticks serialize: both answered, and the reserved concurrency of 1 made one of them wait.
            assert len(row["ticks"]) == 2 and all(t.get("ok") for t in row["ticks"]), row["ticks"]
        # The latch blocks a re-advance of the rolled-back digest.
        blocked = deploy(mv, log="live-controller-latched.log", check=False)
        assert blocked.returncode != 0 and "latched" in (blocked.stdout + blocked.stderr).lower()
        # memory purge removes only that release's records: every stable-release record listed before it is still
        # there after it. Memory's index lags both writes and deletes, so survivors are the listed
        # IDs that GetMemoryRecord still finds; the namespace is checked for 150 s after the purge and must stay at 0.
        slug = control(ctx)["release"]
        from module11.aws import memory
        from module11.cli_ops import _memory_id
        memory_id = _memory_id(ctx, STAGE)
        plane = ctx.admin.client("bedrock-agentcore")

        def survivors() -> list[str]:
            alive = []
            for rid in memory.list_release(memory_id, stage=STAGE, release=slug, data_plane=plane):
                try:
                    plane.get_memory_record(memoryId=memory_id, memoryRecordId=rid)
                    alive.append(rid)
                except plane.exceptions.ResourceNotFoundException:
                    pass
            return alive
        stable_release = next(r["release"] for r in outcomes(ctx, record["runs"][-1]["rolloutId"])
                              if r.get("arm") == "stable")
        stable_before = set(memory.list_release(memory_id, stage=STAGE, release=stable_release, data_plane=plane))
        purge = cli("memory", "purge", "--release", slug, "--stage", STAGE, log="live-controller-purge.log")
        assert "memory purge /devops-companion/dev/release/" in purge.stdout
        assert "failed 0" in purge.stdout and "incomplete" not in purge.stdout, purge.stdout[-800:]
        found, end = [], time.time() + 150
        while time.time() < end and not found:
            found = survivors()
            time.sleep(15)
        record["purgeSurvivors"] = len(found)
        record["purgeWait"] = re.findall(r"in (\d+) s, failed", purge.stdout)
        assert found == [], found[:5]
        stable_after = set(memory.list_release(memory_id, stage=STAGE, release=stable_release, data_plane=plane))
        assert stable_before <= stable_after, sorted(stable_before - stable_after)[:5]
        record["stableRelease"] = {"release": stable_release, "before": len(stable_before), "after": len(stable_after)}
        record["latchBlocked"] = True
        record["purge"] = purge.stdout.strip().splitlines()[-1]
        # R10 over all runs (rollout_sim.R10): candidate payments search_runbooks rate <= candidateMax, stable >= stableMin.
        from module11.release.rollout_sim import R10
        pooled = {arm: [sum(r["rates"][arm][k] for r in record["runs"]) for k in ("runbooks", "n")]
                  for arm in ("candidate", "stable")}
        record["r10"] = {"bounds": R10, "pooled": pooled}
        (c_k, c_n), (s_k, s_n) = pooled["candidate"], pooled["stable"]
        assert c_n >= R10["minSessionsPerArm"] and s_n >= R10["minSessionsPerArm"], pooled
        assert c_k / c_n <= R10["candidateMax"] and s_k / s_n >= R10["stableMin"], pooled
    finally:
        reset_if_terminal(ctx, "test_live_controller cleanup")
        if record.get("runs"):
            clear_latch(digest, "test_live_controller cleanup: the seeded payments branch is test data")
        git("branch", "-D", branch)
        (LOCAL / "live-controller-runs.json").write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    assert naming.is_rollout_stage(STAGE)


def test_live_shadow_probe(ctx):
    """`shadow probe --stage staging`: the streamed and recorded tool event say shadow_suppressed; no S3 side effect
    and no Memory record for the probe session."""
    out = cli("shadow", "probe", "--stage", "staging", "--json", log="live-shadow-probe.log")
    doc = json.loads(out.stdout.strip().splitlines()[-1])
    assert doc["streamed"] and doc["recorded"] and doc["objects"] == 0 and doc["memoryRecords"] == 0


# ---------------------------------------------------------------- test_live_rollback_keeps_stable_working
HANDLER = "agentic-ai/module11/harness/agent/tools/get_service_logs/handler/logic.py"
MODELS = "agentic-ai/module11/harness/agent/models.yaml"
ORCHESTRATION = "agentic-ai/module11/harness/agent/orchestration.yaml"
FLAGS = "agentic-ai/module11/harness/flags/flags.json"


def _planner_model(text: str) -> str:
    head, planner = text.split("\nplanner:\n", 1)
    planner = planner.replace("us.openai.gpt-5.6-luna", "us.openai.gpt-5.6-terra", 1) \
                     .replace("openai.gpt-5.6-luna", "openai.gpt-5.6-terra", 1)
    return head + "\nplanner:\n" + planner


def _flags_version(version: int):
    def edit(text: str) -> str:
        doc = json.loads(text)
        doc["flags"]["module10-recovery-planning"]["version"] = version
        return json.dumps(doc, indent=2) + "\n"
    return edit


CHANGES = {
    "handler": ({HANDLER: lambda t: t.rstrip("\n") + "\n\n# Rollback probe (test_live_rollback_keeps_stable_working).\n"
                 "PROBE_REVISION = 1\n"}, "get_service_logs: probe revision"),
    "model": ({MODELS: _planner_model}, "Planner on gpt-5.6-terra"),
    "orchestration": ({ORCHESTRATION: lambda t: t.replace("deadlineSeconds: 150", "deadlineSeconds: 120", 1)},
                      "Shorter session deadline"),
}


def _canary_and_roll_back(ctx, name: str, record: dict) -> dict:
    edits, message = CHANGES[name]
    branch = seed(f"rollback-{name}", edits, message)
    record.setdefault("branches", []).append(branch)
    mv = seal(branch, f"live-rollback-{name}-seal.log")
    reset_if_terminal(ctx, f"rollback_keeps_stable_working: before the {name} canary")
    before = current(ctx)
    out = deploy(mv, log=f"live-rollback-{name}-deploy.log")
    assert "path rollout" in out.stdout, out.stdout[-1500:]
    rid = re.search(r"rollout (r-[0-9a-f]{16}) opened", out.stdout).group(1)
    timeouts = [adapter_timeout(ctx)]
    cli("rollout", "abort", "--stage", STAGE, "--reason", f"test_live_rollback_keeps_stable_working: {name}")
    item = wait_state(ctx, ("ROLLED_BACK",), 180, rid)
    timeouts.append(adapter_timeout(ctx))
    stable = invoke_through_edge(ctx)
    findings = drift_findings(ctx)
    row = {"change": name, "manifestVersion": mv, "rolloutId": rid, "reason": item.get("reason"),
           "stableStatus": stable.get("status"), "drift": [str(f) for f in findings], "adapterTimeouts": timeouts,
           "currentUnchanged": current(ctx)["manifestVersion"] == before["manifestVersion"],
           "digest": item["behaviorDigest"]}
    record.setdefault("rows", []).append(row)
    return row


def test_live_rollback_keeps_stable_working(ctx):
    record: dict = {}
    expected_timeout = int(current(ctx)["deployment"][STAGE].get("adapterTimeoutSeconds") or 180)
    try:
        for name in ("handler", "model", "orchestration"):
            row = _canary_and_roll_back(ctx, name, record)
            assert row["stableStatus"] == "completed", row
            assert row["drift"] == [], row
            assert row["currentUnchanged"], row
            if name == "orchestration":     # max(150, 120) + 30 throughout the canary and after the rollback
                assert row["adapterTimeouts"] == [expected_timeout, expected_timeout], row
        cli("release", "prune", "--stage", STAGE, log="live-rollback-prune.log")
        assert adapter_timeout(ctx) == expected_timeout
        assert invoke_through_edge(ctx).get("status") == "completed"
        # Flags-only releases after the handler revert (HEAD is the reverted state).
        bundle_before = current(ctx)["deployment"][STAGE]["bundle"]["versionId"]
        reset_if_terminal(ctx, "rollback_keeps_stable_working: before the flags-only release")
        b1 = seed("flags-only-1", {FLAGS: _flags_version(2)}, "Flags: bump the planning flag version")
        record["branches"].append(b1)
        out1 = deploy(seal(b1, "live-flags-1-seal.log"), log="live-flags-1-deploy.log")
        assert "path equivalent" in out1.stdout and "EquivalentRuntimeRetarget" in out1.stdout, out1.stdout[-1500:]
        assert "rollout" not in re.findall(r"path (\w+)", out1.stdout)
        cur = current(ctx)
        assert cur["deployment"][STAGE]["bundle"]["versionId"] == bundle_before
        entries = control(ctx)["endpoints"]
        assert {e["release"] for e in entries.values()} == {cur["deployment"][STAGE]["release"]}
        version_after_first = cur["deployment"][STAGE]["runtimeVersion"]
        b2 = seed("flags-only-2", {FLAGS: _flags_version(3)}, "Flags: bump the planning flag version again")
        record["branches"].append(b2)
        out2 = deploy(seal(b2, "live-flags-2-seal.log"), log="live-flags-2-deploy.log")
        assert "path unchanged" in out2.stdout, out2.stdout[-1500:]
        assert current(ctx)["deployment"][STAGE]["runtimeVersion"] == version_after_first
        record["flags"] = {"first": "equivalent", "second": "unchanged", "runtimeVersion": version_after_first}
    finally:
        (LOCAL / "live-rollback-keeps-stable.json").write_text(json.dumps(record, indent=2, default=str),
                                                               encoding="utf-8")
        reset_if_terminal(ctx, "rollback_keeps_stable_working cleanup")
        for row in record.get("rows", []):
            clear_latch(row["digest"], f"test_live_rollback_keeps_stable_working cleanup ({row['change']})")
        for branch in record.get("branches", []):
            subprocess.run(["git", "-C", str(REPO), "branch", "-D", branch], capture_output=True)

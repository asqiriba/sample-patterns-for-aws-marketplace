# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`python -m module11 <command>`: the Module 11 release tool (the talk track aliases it `module11`).

Offline commands (no AWS): tree, manifest build|show|diff, detect, check, closure update,
ci validate, gen router-consts, sim verify|run, corpus snapshot, runtime setup-local. Commands that need AWS or later phases print which build step adds them.
Run from agentic-ai/ (the sample repo) or the GitLab project root.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .release import artifacts as A
from .release import check as C
from .release import closure as closures
from .release import detect as D
from .release import manifest as M
from .release import stage as cfg
from .release.normalize import NormalizationError

ROOT = Path(__file__).resolve().parents[1]
NO_COMMIT = {"commit": "0000000", "timestamp": "1970-01-01T00:00:00Z", "tag": None, "subject": ""}

# Commands that arrive in later build steps (FEAT numbering of the build plan).
LATER: dict[str, str] = {}


# ---------------------------------------------------------------- output
def _console():
    try:
        from rich.console import Console
        return Console(highlight=False, soft_wrap=True)
    except ImportError:  # pragma: no cover - rich is pinned in requirements.txt
        return None


def say(text: str = "", style: str | None = None) -> None:
    console = _console()
    if console is None:
        print(text)
    else:
        console.print(text, style=style, markup=False)


# ---------------------------------------------------------------- git (read-only)
def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def git_info(root: Path, ref: str = "HEAD") -> dict:
    """{commit, timestamp (committer time, UTC), tag (harness/v* at that commit), subject}."""
    try:
        commit, epoch, subject = _git(root, "log", "-1", "--format=%H%x00%ct%x00%s", ref).strip().split("\0")
    except (RuntimeError, ValueError):
        return dict(NO_COMMIT)
    try:
        tag = _git(root, "describe", "--tags", "--exact-match", "--match", "harness/v*", commit).strip() or None
    except RuntimeError:
        tag = None
    stamp = datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"commit": commit, "timestamp": stamp, "tag": tag, "subject": subject[:300]}


@contextmanager
def tree_at(root: Path, ref: str) -> Iterator[Path]:
    """Export this root's subtree at `ref` into a temporary directory."""
    prefix = _git(root, "rev-parse", "--show-prefix").strip().rstrip("/")
    treeish = f"{ref}:{prefix}" if prefix else ref
    # Run from the top level: in a subdirectory, git archive limits the tree to that subdirectory again.
    top = _git(root, "rev-parse", "--show-toplevel").strip()
    data = subprocess.run(["git", "-C", top, "archive", "--format=tar", treeish], capture_output=True)
    if data.returncode != 0:
        raise RuntimeError(data.stderr.decode("utf-8", "replace").strip() or f"cannot export {ref}")
    with tempfile.TemporaryDirectory(prefix="m11-ref-") as tmp:
        with tarfile.open(fileobj=io.BytesIO(data.stdout)) as tar:
            tar.extractall(tmp, filter="data")
        yield Path(tmp)


def _declared(root: Path, git: dict) -> dict:
    return M.build_declared(root, git)


def declared_at(root: Path, ref: str) -> dict | None:
    """The declared manifest at `ref`, or None when that commit has no harness yet (bootstrap)."""
    git = git_info(root, ref)
    with tree_at(root, ref) as tree:
        if not (tree / A.HARNESS).is_dir():
            return None
        return _declared(tree, git)


# ---------------------------------------------------------------- commands
def cmd_tree(args) -> int:
    aset = A.compute(args.root)
    for which in ("behavior", "release"):
        ids = [a for a in aset.ids() if aset.artifacts[a].set == which]
        say(f"{which} set ({len(ids)})", style="bold")
        for aid in ids:
            art = aset.artifacts[aid]
            mark = " runtime" if art.runtime else ""
            say(f"  {aid:<42} {len(art.sources):>3} file(s){mark}")
    d = aset.digests()
    say(f"{d['artifactCount']} artifacts / {d['behaviorCount']} behavior / {d['runtimeCount']} runtime")
    return 0


def _counts_line(d: dict) -> str:
    return f"{d['artifactCount']} artifacts / {d['behaviorCount']} in the behavior set / {d['runtimeCount']} runtime"


def cmd_manifest_build(args) -> int:
    aset = A.compute(args.root)
    manifest = M.build_declared(args.root, git_info(args.root), aset)
    d = manifest["digests"]
    if args.explain_normalization:
        for row in M.explain_normalization(args.root, aset):
            say(f"{row['artifact']}  {row['hash']}")
            for path, digest in sorted(row["files"].items()):
                say(f"    {path}  {digest}")
    if args.out:
        Path(args.out).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    say(f"manifestVersion {manifest['manifestVersion']}")
    say(_counts_line(d))
    for key in ("behavior", "runtime", "release"):
        say(f"{key + 'Digest':<15} {d[key]}")
    say(f"{'bundleComponent':<15} {manifest['build']['bundleComponent']['sha256']}")
    golden = A.golden_counts(args.root)
    if golden and (golden["artifacts"], golden["behavior"], golden["runtime"]) != \
            (d["artifactCount"], d["behaviorCount"], d["runtimeCount"]):
        say(f"artifact count differs from the golden count {golden}", style="red")
        return 1
    return 0


def cmd_manifest_show(args) -> int:
    manifest = declared_at(args.root, args.ref) if args.ref else _declared(args.root, git_info(args.root))
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


def _stage_like(ref: str) -> bool:
    return ref in {"dev", "staging", "prod"} or bool(re.match(r"^mr-\d+$", ref))


def cmd_manifest_diff(args) -> int:
    declared = declared_at(args.root, args.declared) if args.declared else _declared(args.root, git_info(args.root))
    if declared is None:
        raise RuntimeError(f"{args.declared} has no declared harness")
    if _stage_like(args.deployed):
        from .cli_live import cmd_manifest_diff_deployed
        return cmd_manifest_diff_deployed(args, declared)       # the stage's /current (SSM), needs AWS
    deployed = declared_at(args.root, args.deployed)
    result = M.diff(deployed, declared)
    print(json.dumps(result, indent=2))
    return 0


def _plan(root: Path, base_ref: str | None) -> tuple[D.ChangePlan, bool]:
    rules, unknown, _ = cfg.change_types(root)
    new = M.build_declared(root, git_info(root))
    if base_ref is None:
        return D.detect(M.hash_set(new), None, rules, unknown, new_tools=new["tools"]), False
    base = declared_at(root, base_ref)
    if base is None:
        return D.detect(M.hash_set(new), None, rules, unknown, new_tools=new["tools"]), False
    return D.detect(M.hash_set(new), M.hash_set(base), rules, unknown, base_manifest_version=base["manifestVersion"],
                    new_tools=new["tools"], base_tools=base.get("tools")), True


def cmd_detect(args) -> int:
    plan, _ = _plan(args.root, args.base)
    print(json.dumps(plan.to_dict(), indent=2))
    return 0


def _report(findings: list[C.Finding]) -> int:
    errs = C.errors(findings)
    for f in findings:
        say(str(f), style="red" if f.severity == "error" else "yellow")
    say(f"checks: {len(errs)} error(s), {len(findings) - len(errs)} warning(s)",
        style="red" if errs else "green")
    return 1 if errs else 0


def cmd_check(args) -> int:
    plan, has_base = _plan(args.root, args.base) if args.base else (None, True)
    return _report(C.run(args.root, plan, has_base))


def cmd_closure_update(args) -> int:
    changed = closures.update(args.root)
    say("closures updated: " + (", ".join(changed) if changed else "none (already current)"))
    return 0


def cmd_ci_validate(args) -> int:
    """MR `validate` job: detect, checks C1-C16 (C16 offline against main's declared manifest),
    and the offline tests. No AWS credential."""
    plan, has_base = _plan(args.root, args.base)
    say(f"change plan: kinds={plan.kinds} rollout={plan.rollout} releaseKind={plan.releaseKind}")
    status = _report(C.run(args.root, plan, has_base))
    if args.skip_tests:
        return status
    tests = sorted(str(p.relative_to(args.root)) for p in (args.root / "tests").glob("test_module11_*.py"))
    env = {**os.environ, "M11_SIM_RUNS": os.environ.get("M11_SIM_RUNS", "1000")}
    # nosemgrep: dangerous-subprocess-use-audit - list args, no shell; pytest on the repo's own tests/test_module11_*.py
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", *tests], cwd=args.root, env=env)
    return status or result.returncode


def cmd_gen_router_consts(args) -> int:
    from .release import router_spec
    changed = router_spec.write_generated(args.root)
    say("router constants: " + (", ".join(changed) if changed else "already current"))
    return 0


def _sim_sizes(args) -> tuple[dict[str, int], str | None]:
    from .release import rollout_sim as sim
    sizes, cut = {}, None
    for c in sim.CASES:
        runs = sim.sim_runs(c.runs)
        if args.batch_runs and c.kind == "batch":
            runs = min(runs, args.batch_runs)
        sizes[c.name] = runs
    reduced = sorted(n for n, r in sizes.items() if r < sim.case(n).runs)
    if reduced:
        cut = args.cut or f"reduced runs for {len(reduced)} case(s)"
    return sizes, cut


def cmd_sim_verify(args) -> int:
    """Run every ACCEPTANCE case and write module11/release/controller_sim_report.json."""
    import time
    from .release import rollout_sim as sim
    sizes, cut = _sim_sizes(args)
    started = time.time()
    rows = sim.run([c.name for c in sim.CASES], args.root, runs=sizes, workers=args.workers)
    doc = sim.report(args.root, rows, cut)
    doc["wallSeconds"] = round(time.time() - started, 1)
    path = sim.write_report(args.root, doc)
    for row in rows:
        mark = "ok  " if row["passed"] else "FAIL"
        if "minPower" in row["threshold"]:
            extra = f"power {row['power']:.3f} median {row['medianMinutes']} min"
        elif "cpUpper" in row:
            extra = f"cpUpper {row['cpUpper']:.4f} stalled {row['stalled']} maxLooks {row['maxLooks']}"
        else:
            extra = f"rolledBack {row['rolledBack']}/{row['runs']} promoted {row['promoted']}"
        say(f"{mark} {row['case']:<30} runs {row['runs']:>6}  {extra}")
    say(f"wrote {path.relative_to(args.root)} closureSha256 {doc['closureSha256']} passed={doc['passed']} "
        f"in {doc['wallSeconds']} s", style="green" if doc["passed"] else "red")
    return 0 if doc["passed"] else 1


def cmd_sim_run(args) -> int:
    from .release import rollout_sim as sim
    rows = sim.run([args.case], args.root, runs={args.case: sim.sim_runs(args.runs or sim.case(args.case).runs)},
                   workers=args.workers)
    print(json.dumps(rows[0], indent=2, sort_keys=True))
    return 0 if rows[0]["passed"] else 1


def cmd_corpus_snapshot(args) -> int:
    """Build the policy snapshot offline (Module 9 chunk + quality gates) and optionally pin it."""
    from .corpus import snapshot as S
    out = Path(args.out) if args.out else args.root / "module11/.local"
    path, pin = S.write(args.root, out)
    say(f"snapshot {pin['snapshotId']} {pin['snapshotSha256']} ({pin['documentCount']} documents, "
        f"{pin['chunkCount']} chunks) -> {path}")
    if args.pin:
        changed = S.update_pin(args.root)
        say(f"{S.RETRIEVAL}: " + ("pinned" if changed else "already pinned"))
        return 0
    current = S.pinned(args.root)
    if current.get("snapshotSha256") != pin["snapshotSha256"]:
        say(f"{S.RETRIEVAL} pins {current.get('snapshotId')}; run with --pin to update it", style="yellow")
        return 1
    return 0


def cmd_corpus_publish(args) -> int:
    """Module 9's snapshot step as a release event: the corpus handler opens the GitLab MR."""
    from .cli_live import _context
    from .corpus import publish as PUB
    ctx = _context(args.root)
    out = PUB.publish(args.root, corpus=args.corpus, change=args.change, bucket=ctx.release_bucket,
                      s3=ctx.admin.client("s3"), events=ctx.admin.client("events"), log=say)
    say(f"manifest pin unchanged until the change request is released: retrieval.yaml still pins "
        f"{S_pinned(args.root)}", style="green")
    print(json.dumps({"snapshotId": out["pin"]["snapshotId"], "eventId": out["eventId"],
                      "branch": f"corpus/{args.corpus}/{out['pin']['snapshotId']}"}))
    return 0


def S_pinned(root: Path) -> str:
    from .corpus import snapshot as S
    return str(S.pinned(root).get("snapshotId"))


def setup_local_runtime(root: Path, out: Path) -> dict:
    """A local data directory for `python -m module11.runtime.app` on 127.0.0.1 (no AWS): a
    `file://` bucket with evidence, the pinned snapshot, flags and a bundle version, a local
    Memory directory, `local:<tool>` handlers, and two configs (normal and bootstrap)."""
    from .corpus import snapshot as S
    from .release.hashing import hex_of, sha256_bytes
    from .release.naming import manifest_version
    out = out.resolve()
    bucket = out / "bucket"
    aset = A.compute(root)
    evidence_sha = aset.hashes["evidence"]
    evidence_dir = bucket / "evidence" / hex_of(evidence_sha) / "incidents"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    sources = {p.stem: p for p in (root / A.HARNESS / "evidence" / "incidents").glob("*.json")}
    sources["reporting-service"] = root / A.MODULE10_INCIDENT
    for service, src in sorted(sources.items()):
        target = evidence_dir / f"{service}.json"
        target.write_bytes(src.read_bytes())
        Path(str(target) + ".metadata.json").write_text(json.dumps({"m11-sha256": evidence_sha}), encoding="utf-8")
    index = evidence_dir.parent / "index.json"            # as Seal writes it (aws/objects.known_services)
    index.write_text(json.dumps({"services": sorted(sources)}, sort_keys=True), encoding="utf-8")
    Path(str(index) + ".metadata.json").write_text(json.dumps({"m11-sha256": evidence_sha}), encoding="utf-8")
    snap_path, pin = S.write(root, bucket)
    flags = bucket / "config" / "flags.json"
    flags.parent.mkdir(parents=True, exist_ok=True)
    flags.write_bytes((root / A.SINGLE["flags"]).read_bytes())
    component = M.bundle_component(root)
    data = M.component_bytes(component)
    version = "local-" + hex_of(sha256_bytes(data))[:12]
    bundles = out / "bundles"
    bundles.mkdir(parents=True, exist_ok=True)
    (bundles / f"{version}.json").write_text(json.dumps(
        {"versionId": version, "components": {"local:runtime": {"configuration": component}}}), encoding="utf-8")
    config = {"stage": "local", "stagekey": "local", "bundleArn": bundles.as_uri(), "bundleVersion": version,
              "toolArns": {t: f"local:{t}" for t in closures.lambda_tools(root)},
              "guardrail": {"id": "local-passthrough", "version": "local"}, "flagsKey": "config/flags.json",
              "artifactsBucket": bucket.as_uri(),
              "snapshot": {"uri": snap_path.as_uri(), "sha256": pin["snapshotSha256"]},
              "evidence": {"uri": evidence_dir.parent.as_uri() + "/", "sha256": evidence_sha},
              "memoryId": (out / "memory").as_uri(), "runtimeDigest": aset.digests()["runtime"]}
    (out / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    (out / "config-bootstrap.json").write_text(json.dumps({**config, "bundleArn": None, "bundleVersion": None},
                                                          indent=2), encoding="utf-8")
    git = git_info(root)
    context = {"variant": "dev", "manifestVersion": manifest_version(git["timestamp"], git["commit"]),
               "behaviorDigest": aset.digests()["behavior"], "release": "local"}
    request = {"change_summary": "The checkout service is throwing 502s since the 14:20 deploy.",
               "request_id": "local-investigation-1"}
    envelopes = {
        "envelope-investigation.json": {"identity": {"team": "platform", "principal": "local-user"},
                                        "request": request, "context": context},
        "envelope-shadow.json": {"identity": {"team": "platform", "principal": "local-user"},
                                 "request": {**request, "request_id": "local-shadow-1"},
                                 "context": {**context, "variant": "shadow", "shadow": True}},
    }
    for name, doc in envelopes.items():
        (out / name).write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return {"dir": str(out), "config": str(out / "config.json"), "bootstrap": str(out / "config-bootstrap.json"),
            "bundleVersion": version, "snapshotId": pin["snapshotId"], "evidenceSha256": evidence_sha}


def cmd_runtime_setup_local(args) -> int:
    out = Path(args.dir) if args.dir else args.root / "module11/.local/runtime"
    info = setup_local_runtime(args.root, out)
    say(f"local runtime data in {info['dir']} (bundle {info['bundleVersion']}, snapshot {info['snapshotId']})")
    say(f"run: python -m module11.runtime.app --host 127.0.0.1 --port 8091 --fake-model --config-file {info['config']}")
    return 0


def cmd_later(name: str) -> int:
    say(f"`module11 {name}` is not built yet: it arrives in {LATER[name]}.", style="yellow")
    return 2


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="module11", description="Module 11 release tool (offline commands).")
    p.add_argument("--root", type=Path, default=ROOT, help="repository root holding module11/ (default: this checkout)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("tree", help="list the declared harness artifacts").set_defaults(fn=cmd_tree)

    man = sub.add_parser("manifest", help="build, show or diff the declared manifest")
    msub = man.add_subparsers(dest="manifest_command", required=True)
    b = msub.add_parser("build")
    b.add_argument("--explain-normalization", action="store_true")
    b.add_argument("--out", help="write the declared manifest JSON here")
    b.set_defaults(fn=cmd_manifest_build)
    s = msub.add_parser("show")
    s.add_argument("--ref", help="git ref (default: the working tree)")
    s.set_defaults(fn=cmd_manifest_show)
    d = msub.add_parser("diff")
    d.add_argument("--declared", help="git ref (default: the working tree)")
    d.add_argument("--deployed", required=True, help="git ref, or a stage (needs AWS)")
    d.set_defaults(fn=cmd_manifest_diff)

    det = sub.add_parser("detect", help="change plan of the working tree against a base ref")
    det.add_argument("--base", required=True)
    det.set_defaults(fn=cmd_detect)
    chk = sub.add_parser("check", help="run checks C1-C16")
    chk.add_argument("--base", help="git ref for C16 (default: no base, C16 skipped)")
    chk.set_defaults(fn=cmd_check)
    clo = sub.add_parser("closure", help="regenerate closure lists")
    csub = clo.add_subparsers(dest="closure_command", required=True)
    csub.add_parser("update").set_defaults(fn=cmd_closure_update)
    ci = sub.add_parser("ci", help="CI entry points")
    cisub = ci.add_subparsers(dest="ci_command", required=True)
    v = cisub.add_parser("validate")
    v.add_argument("--base", default="origin/main")
    v.add_argument("--skip-tests", action="store_true")
    v.set_defaults(fn=cmd_ci_validate)
    gen = sub.add_parser("gen", help="generated files")
    gsub = gen.add_subparsers(dest="gen_command", required=True)
    gsub.add_parser("router-consts", help="router_consts.mjs and router_vectors.json from release/router_spec.py") \
        .set_defaults(fn=cmd_gen_router_consts)
    sim = sub.add_parser("sim", help="seeded rollout simulator (release/rollout_sim.py)")
    ssub = sim.add_subparsers(dest="sim_command", required=True)
    sv = ssub.add_parser("verify", help="run the full grid and write release/controller_sim_report.json")
    sv.add_argument("--workers", type=int, default=None, help="processes (default: one per core)")
    sv.add_argument("--batch-runs", type=int, default=None, help="cap the prod/model cases (cut C1)")
    sv.add_argument("--cut", help="label recorded in the report when runs are reduced")
    sv.set_defaults(fn=cmd_sim_verify)
    sr = ssub.add_parser("run", help="run one case and print its row")
    sr.add_argument("case")
    sr.add_argument("--runs", type=int)
    sr.add_argument("--workers", type=int, default=None)
    sr.set_defaults(fn=cmd_sim_run)
    cor = sub.add_parser("corpus", help="policy corpus snapshots (Module 9 chunking)")
    corsub = cor.add_subparsers(dest="corpus_command", required=True)
    cs = corsub.add_parser("snapshot", help="build the snapshot offline; --pin writes it into retrieval.yaml")
    cs.add_argument("--out", help="directory for snapshots/policy/<id>.json (default module11/.local)")
    cs.add_argument("--pin", action="store_true")
    cs.set_defaults(fn=cmd_corpus_snapshot)
    cp = corsub.add_parser("publish", help="build an immutable snapshot, store it and emit CorpusSnapshotPublished")
    cp.add_argument("--corpus", default="policy", choices=["policy"])
    cp.add_argument("--change", help="apply module11/corpus/updates/<change>.md (e.g. runbook-checkout-v2)")
    cp.set_defaults(fn=cmd_corpus_publish)
    rt = sub.add_parser("runtime", help="local runtime helpers")
    rtsub = rt.add_subparsers(dest="runtime_command", required=True)
    rs = rtsub.add_parser("setup-local", help="data directory for the loopback local run (no AWS)")
    rs.add_argument("--dir", help="default module11/.local/runtime")
    rs.set_defaults(fn=cmd_runtime_setup_local)
    from . import cli_demo, cli_eval, cli_live, cli_ops, cli_partners, cli_phase8, cli_phase10, cli_pipeline
    cli_live.register(sub)                       # image, release, status, versions, resources, ...
    cli_eval.register(sub)                       # gate, calibrate, calibration, evalset, cost, baseline
    cli_ops.register(sub)                        # rollout, traffic, latch, side-effects, memory, ...
    cli_pipeline.register(sub)                   # pipeline actions, build image, timing, freeze
    cli_partners.register(sub)                   # partners up [--pipeline-only]
    cli_phase8.register(sub)                     # abtest, recommendation, model compare, scenario add
    cli_demo.register(sub)                       # demo prep, demo arm --stage dev, demo rearm
    cli_phase10.register(sub)                    # teardown, captures export
    for name in sorted(LATER):
        later = sub.add_parser(name, help=f"(not built yet: {LATER[name]})", add_help=False)
        later.add_argument("rest", nargs=argparse.REMAINDER)
        later.set_defaults(fn=lambda args, n=name: cmd_later(n))
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    args.root = Path(args.root).resolve()
    try:
        return args.fn(args)
    except (cfg.ConfigError, A.ArtifactError, M.ManifestError, D.ChangePlanRejected, NormalizationError,
            RuntimeError, ValueError) as exc:
        say(f"{type(exc).__name__}: {exc}", style="red")
        return 1
    except Exception as exc:  # AWS SDK errors (no credentials, AccessDenied) without a traceback
        if type(exc).__module__.startswith("botocore"):
            say(f"{type(exc).__name__}: {exc}", style="red")
            return 1
        raise

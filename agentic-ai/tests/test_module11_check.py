# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline tests for module11/release/check.py, C1-C16."""
import json
import re
import shutil
from pathlib import Path

import pytest
import yaml

from module11.release import artifacts as A
from module11.release import check as C
from module11.release import codeowners
from module11.release import detect as D
from module11.release import stage as cfg

ROOT = Path(__file__).resolve().parents[1]
RULES, UNKNOWN, _ = cfg.change_types(ROOT)
from test_module11_support import SAMPLE_LAYOUT  # noqa: E402

pytestmark = pytest.mark.skipif(not SAMPLE_LAYOUT, reason="check tests seed fixtures from the sample repository's "
                                "module11/gitlab/ sources; the harness repo runs the checks themselves in Analyze")
CODEOWNERS = (ROOT / "module11/gitlab/CODEOWNERS").read_text(encoding="utf-8") if SAMPLE_LAYOUT else ""


@pytest.fixture
def repo(tmp_path):
    """A copy of the parts of the repository the checks read."""
    root = tmp_path / "repo"
    shutil.copytree(ROOT / "module11", root / "module11",
                    ignore=shutil.ignore_patterns(".local", "__pycache__", "venv"))
    # Module 10's top-level modules (the runtime and planner-tool closures import them) and its
    # edge Lambda files (edge.txt packages them unchanged): C12 reads both.
    module10 = sorted(p.relative_to(ROOT).as_posix() for pattern in ("*.py", "deployment/lambda/*.mjs")
                      for p in (ROOT / "module10").glob(pattern))
    for rel in ("module10/fixtures/incident.json", "module5/engine/domain_adapter.py", "tests/conftest.py",
                "tests/test_module11_check.py", *module10):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, root / rel)
    (root / "tests/fixtures/module11").mkdir(parents=True)
    (root / "tests/fixtures/module11/golden.json").write_text("{}\n", encoding="utf-8")
    return root


def codes(findings):
    return sorted({f.code for f in C.errors(findings)})


def edit_json(root, rel, fn):
    path = root / rel
    doc = json.loads(path.read_text(encoding="utf-8"))
    fn(doc)
    path.write_text(json.dumps(doc), encoding="utf-8")


def test_real_harness_passes_every_check():
    assert C.errors(C.run(ROOT)) == []


def test_copy_passes(repo):
    assert C.errors(C.run(repo)) == []


# ---------------------------------------------------------------- C1-C3
def test_c1_missing_file_name_mismatch_and_delegate_handler(repo):
    tools = repo / A.TOOLS
    (tools / "query_metrics/description.md").unlink()
    edit_json(repo, f"{A.TOOLS}/get_service_logs/tool.json", lambda d: d.__setitem__("name", "get_logs"))
    (tools / "delegate_recovery_planning/handler").mkdir()
    messages = [f.message for f in C.c1(repo)]
    assert "missing tool definition file" in messages
    assert any("does not equal the directory" in m for m in messages)
    assert "a delegate tool has no handler" in messages


def test_c2_schema_shape(repo):
    edit_json(repo, f"{A.TOOLS}/query_metrics/schema.json", lambda d: d.pop("additionalProperties"))
    edit_json(repo, f"{A.TOOLS}/get_blast_radius/schema.json", lambda d: d.__setitem__("type", 7))
    assert len(C.c2(repo)) == 2


def test_c3_signature_mismatch_ignores_trailing_data(repo):
    logic = repo / A.TOOLS / "query_metrics/handler/logic.py"
    assert C.c3(repo) == []
    logic.write_text("def run(service, data):\n    return {}\n", encoding="utf-8")
    assert [f.code for f in C.c3(repo)] == ["C3", "C3"]
    assert C.run_signature("def run(a, b=1, data=None):\n    pass\n") == (["a", "b"], ["a"])


# ---------------------------------------------------------------- C4 / C5
def test_committed_prompts_pass_c4_c5_with_empty_allow_list():
    assert cfg.orchestration(ROOT)["allowUnreferenced"] == []
    assert C.c4_c5(ROOT) == []
    companion = (ROOT / A.AGENT / "prompts/companion.md").read_text(encoding="utf-8")
    assert set(C.TOOL_REF.findall(companion)) == {"get_deployment_history", "query_metrics", "search_runbooks",
                                                  "get_service_logs", "record_incident_note",
                                                  "delegate_recovery_planning"}
    planner = (ROOT / A.AGENT / "prompts/planner.md").read_text(encoding="utf-8")
    assert set(C.TOOL_REF.findall(planner)) == {"get_blast_radius", "check_rollback_readiness"}


def test_seeded_c4_reference_to_a_tool_not_offered(repo):
    prompt = repo / A.AGENT / "prompts/companion.md"
    prompt.write_text(prompt.read_text(encoding="utf-8") + "Call `get_blast_radius` yourself.\n", encoding="utf-8")
    assert codes(C.run(repo)) == ["C4"]


def test_seeded_c5_offered_but_never_referenced(repo):
    prompt = repo / A.AGENT / "prompts/companion.md"
    prompt.write_text(prompt.read_text(encoding="utf-8").replace("`query_metrics`", "query_metrics"), encoding="utf-8")
    assert codes(C.run(repo)) == ["C5"]


# ---------------------------------------------------------------- C6-C11
def test_c6_rejects_wildcards_and_actions_outside_the_ref(repo):
    edit_json(repo, f"{A.TOOLS}/query_metrics/permissions.json",
              lambda d: d.append({"actions": ["s3:Get*"], "resource": "evidence"}))
    edit_json(repo, f"{A.TOOLS}/get_service_logs/permissions.json",
              lambda d: d.append({"actions": ["s3:PutObject"], "resource": "evidence"}))
    assert len(C.c6(repo)) == 2


def test_c7_budget_and_c8_vars(repo):
    prompt = repo / A.AGENT / "prompts/planner.md"
    prompt.write_text(prompt.read_text(encoding="utf-8") + "Use {{team_name}}.\n" + "x" * 7000 + "\n", encoding="utf-8")
    assert sorted(f.code for f in C.c7_c8(repo)) == ["C7", "C8"]


def test_c9_flag_and_team(repo):
    edit_json(repo, f"{A.TOOLS}/delegate_recovery_planning/tool.json", lambda d: d.__setitem__("requiresFlag", "nope"))
    edit_json(repo, A.SINGLE["flags"], lambda d: d["flags"]["module10-recovery-planning"]["contextTargets"][0]["values"]
              .append("security"))
    assert len(C.c9(repo)) == 2


def test_c10_mutating_needs_shadow_result(repo):
    edit_json(repo, f"{A.TOOLS}/record_incident_note/tool.json", lambda d: d.pop("shadowResult"))
    assert [f.code for f in C.c10(repo)] == ["C10"]


def test_c11_ast_fields_and_extra_keys(repo):
    assert C.guardrail_policy_fields(ROOT) == ["pii_handling", "pii_entities", "denied_topics", "content_filters",
                                               "word_filters"]
    path = repo / A.SINGLE["guardrail"]
    path.write_text(path.read_text(encoding="utf-8") + "grounding_threshold: 0.8\n", encoding="utf-8")
    assert [f.message for f in C.c11(repo)] == ["grounding_threshold is not a GuardrailPolicy field"]


def test_c11_ast_equals_dataclass_fields_when_module5_imports():
    pytest.importorskip("langchain_core")
    import dataclasses
    from module5.engine.domain_adapter import GuardrailPolicy
    assert C.guardrail_policy_fields(ROOT) == [f.name for f in dataclasses.fields(GuardrailPolicy)]


def test_seeded_c12_stale_closure(repo):
    (repo / A.TOOLS / "search_runbooks/handler/extra.py").write_text("Y = 2\n", encoding="utf-8")
    assert codes(C.run(repo)) == ["C12"]


# ---------------------------------------------------------------- C13
def test_committed_codeowners_passes_c13_in_both_forms(repo):
    (repo / "tests/test_module11_controller.py").write_text("", encoding="utf-8")
    assert C.c13(repo) == []
    rendered = codeowners.render(CODEOWNERS, "example-group")
    assert "@example-group/quality" in rendered and C.c13(repo, rendered, ".gitlab/CODEOWNERS") == []


def test_committed_codeowners_has_disjoint_quality_sections():
    sections = {s.name: s for s in codeowners.parse(CODEOWNERS)}
    assert [s for s in sections] == ["Behavior", "Contracts", "Implementation", "Quality", "Quality-peer", "Platform"]
    assert {codeowners.owner_leaf(o) for o in sections["Quality"].owners} == {"quality"}
    assert [l.pattern for l in sections["Quality"].lines] == [l.pattern for l in sections["Quality-peer"].lines]
    assert {"/tests/conftest.py", "/demos/module11_demo.py"} <= {l.pattern for l in sections["Platform"].lines}


def test_seeded_c13_uncovered_path(repo):
    (repo / "module11/gitlab/CODEOWNERS").write_text(CODEOWNERS.replace("/module11/aws/\n", ""), encoding="utf-8")
    findings = C.c13(repo)
    assert any(f.message == "no code owner for module11/aws/__init__.py" for f in findings)
    assert codes(C.run(repo)) == ["C13"]


def test_c13_iteration3_single_quality_section_fails(repo):
    text = re.sub(r"\[Quality\]\[1\] @<group>/quality\n", "[Quality][2] @<group>/quality @<group>/harness-owners "
                  "@<group>/platform\n", CODEOWNERS)
    text = text.split("[Quality-peer]")[0] + "[Platform]" + text.split("[Platform]")[1]
    findings = C.c13(repo, text, "CODEOWNERS")
    assert findings and all("needs a quality owner and a peer section" in f.message for f in findings)


def test_c13_tests_lines_missing_or_only_in_platform(repo):
    (repo / "tests/test_module11_controller.py").write_text("", encoding="utf-8")
    without = CODEOWNERS.replace("/tests/test_module11_*\n", "")
    messages = [f.message for f in C.c13(repo, without, "CODEOWNERS")]
    assert "no code owner for tests/test_module11_controller.py" in messages
    platform_only = without.replace("[Platform][1] @<group>/platform\n", "[Platform][1] @<group>/platform\n/tests/test_module11_*\n")
    messages = [f.message for f in C.c13(repo, platform_only, "CODEOWNERS")]
    assert "quality path tests/test_module11_controller.py needs a quality owner and a peer section" in messages


def test_c13_conftest_and_demo_must_be_platform(repo):
    text = CODEOWNERS.replace("/tests/conftest.py\n/demos/module11_demo.py\n", "") + \
        "[Docs][1] @<group>/harness-owners\n/tests/conftest.py\n/demos/module11_demo.py\n"
    messages = [f.message for f in C.c13(repo, text, "CODEOWNERS")]
    assert "tests/conftest.py must match a [Platform] line" in messages
    assert "demos/module11_demo.py must match a [Platform] line" in messages


def test_codeowners_matching_rules():
    assert codeowners.matches("/module11/corpus/*.py", "module11/corpus/publish.py")
    assert not codeowners.matches("/module11/corpus/*.py", "module11/corpus/runbooks/x.py")
    assert codeowners.matches("/module11/release/", "module11/release/a/b.py")
    assert not codeowners.matches("/module11/release/", "module11/release")
    assert codeowners.matches("/module11/harness/agent/tools/*/handler/", "module11/harness/agent/tools/t/handler/index.py")
    assert codeowners.matches("*.md", "docs/x/readme.md")
    assert not codeowners.matches("/tests/test_module11_*", "tests/sub/test_module11_x.py")


# ---------------------------------------------------------------- C14
def _set_fast(repo, scenario_id, on):
    path = repo / cfg.SCENARIO_DIR / "regressions.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for row in rows:
        if row["scenario_id"] == scenario_id:
            row["tags"] = sorted(set(row["tags"]) | {"fast"}) if on else [t for t in row["tags"] if t != "fast"]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_committed_suite_has_42_scenarios_and_12_fast():
    rows = cfg.scenarios(ROOT)
    assert len(rows) == 42 and C.c14(ROOT, rows) == []
    triage = next(r for r in rows if r["scenario_id"] == "incident-triage-014")
    assert triage["expected_trajectory"] == ["get_deployment_history", "query_metrics", "search_runbooks",
                                             "get_service_logs"]
    assert triage["category"] == "regressions" and len(triage["assertions"]) == 3 and triage["expected_response"]


def test_seeded_c14_fast_set_without_incident_triage_014(repo):
    _set_fast(repo, "incident-triage-014", False)
    _set_fast(repo, "regression-planner-headings-001", True)
    findings = C.c14(repo)
    assert [f.message for f in findings] == ["fast subset must include incident-triage-014"]
    assert codes(C.run(repo)) == ["C14"]


def test_c14_count(repo):
    _set_fast(repo, "regression-note-once-001", True)
    assert any("13 scenarios are tagged fast" in f.message for f in C.c14(repo))


# ---------------------------------------------------------------- C15
def test_c15_stale_ci_image_and_unpinned_promote(repo):
    ci = repo / "module11/gitlab/gitlab-ci.yml"
    assert C.c15(repo) == []
    (repo / "module11/requirements-ci.txt").write_text("boto3==1.43.108\n", encoding="utf-8")
    assert [f.code for f in C.c15(repo)] == ["C15"]
    shutil.copy(ROOT / "module11/requirements-ci.txt", repo / "module11/requirements-ci.txt")
    doc = yaml.safe_load(ci.read_text(encoding="utf-8"))
    doc["promote:production"]["image"] = "$CI_REGISTRY_IMAGE/m11-ci:latest"
    ci.write_text(yaml.safe_dump(doc), encoding="utf-8")
    assert [f.message for f in C.c15(repo)] == [
        "promote:production must use an image pinned by @sha256: digest outside $CI_REGISTRY_IMAGE"]


# ---------------------------------------------------------------- C16
BASE = {a: "sha256:" + "0" * 64 for a in A.compute(ROOT / "tests/fixtures/module11/harness_min").ids()}


def _plan(*ids, base=BASE):
    new = {**BASE, **{a: "sha256:" + "1" * 64 for a in ids}}
    return D.detect(new, base, RULES, UNKNOWN)


def test_c16_rejects_mixed_plans_and_passes_single_kind():
    assert [f.code for f in C.c16(_plan("instructions/companion", "flags"), RULES, UNKNOWN)] == ["C16"]
    assert [f.code for f in C.c16(_plan("tool/query_metrics/handler", "edge/code"), RULES, UNKNOWN)] == ["C16"]
    assert C.c16(_plan("flags"), RULES, UNKNOWN) == []
    bootstrap = D.detect({**BASE}, None, RULES, UNKNOWN)
    assert C.c16(bootstrap, RULES, UNKNOWN, has_base=False) == []


def test_c16_message_choice():
    p = _plan("instructions/companion", "flags")
    ahead = C.c16(p, RULES, UNKNOWN, own_kinds=["flags"], commit_range="1111111..2222222")[0].message
    assert "main is ahead of prod by instructions from 1111111..2222222 (not promoted)" in ahead
    own = C.c16(p, RULES, UNKNOWN, own_kinds=["instructions", "flags"])[0].message
    assert own.endswith("land them in their own MR")


# ---------------------------------------------------------------- groups
def test_groups_disjoint_reports_direct_and_inherited_overlap():
    direct = {"quality": ["quality-reviewer"], "harness-owners": ["presenter"], "platform": ["presenter"]}
    assert C.groups_disjoint(direct) == []
    inherited = {"quality": ["quality-reviewer", "presenter"], "harness-owners": ["presenter", "quality-reviewer"],
                 "platform": ["presenter"]}
    findings = C.groups_disjoint(direct, inherited)
    assert {f.severity for f in findings} == {"warning"}
    assert "inherited members in both quality and harness-owners: presenter, quality-reviewer" in [f.message for f in findings]
    bad = C.groups_disjoint({"quality": ["alice"], "platform": ["alice"]})
    assert [(f.severity, f.message) for f in bad] == [("error", "direct members in both quality and platform: alice")]

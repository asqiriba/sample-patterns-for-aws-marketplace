# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`module11 teardown` ownership and the ledger read, against a fake release bucket with a
page size of 2: one paged ListObjectsV2(Prefix="ledger/") listing over ledger/prod/, ledger/staging/ and
ledger/shared/; the plan lists the staging A/B test, a latch/<hex> parameter from a prod entry and the bot signing
key from the ledger/shared/ entry (step 6, by key ID); the plan is complete before any delete, so the step-6 key is
still listed after a fake step 5 removes the bucket; name-prefix-only resources are ORPHAN?; log groups match by IDs
from stack state, never by prefix; Module 10 stays unless asked; and `captures export` redacts."""
from __future__ import annotations

import io
import json

from module11 import teardown as TD

LATCH = "/m11/devops-companion/latch/" + "ab" * 32


class FakeS3:
    def __init__(self, objects: dict[str, dict], page_size: int = 2):
        self.objects = {k: json.dumps(v).encode() for k, v in objects.items()}
        self.page_size = page_size
        self.listings: list[dict] = []
        self.gets = 0

    def list_objects_v2(self, Bucket, Prefix, MaxKeys=1000, ContinuationToken=None):
        self.listings.append({"Prefix": Prefix, "token": ContinuationToken})
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page = keys[start:start + min(MaxKeys, self.page_size)]
        out = {"Contents": [{"Key": k} for k in page]}
        if start + len(page) < len(keys):
            out["NextContinuationToken"] = str(start + len(page))
        return out

    def get_object(self, Bucket, Key):
        self.gets += 1
        return {"Body": io.BytesIO(self.objects[Key])}

    def delete_bucket(self):
        self.objects.clear()


LEDGER = {
    "ledger/prod/20261010T100000Z-a.json": {"kind": "latch", "stage": "prod", "parameter": LATCH, "rolloutId": "r-1"},
    "ledger/prod/20261010T100100Z-b.json": {"kind": "latch-clear", "stage": "prod", "parameter": LATCH},
    "ledger/prod/20261010T100200Z-c.json": {"kind": "recommendation", "stage": "prod", "recommendationId": "rec-1"},
    "ledger/staging/20261010T090000Z-d.json": {"kind": "abtest", "stage": "staging", "abTestId": "ab-123",
                                               "name": "m11_staging_ab"},
    "ledger/staging/20261010T090100Z-e.json": {"kind": "gateway-tracing", "stage": "staging", "deliveryId": "dl-9"},
    "ledger/dev/20261010T080000Z-f.json": {"kind": "batch-evaluation", "stage": "dev", "batchEvaluationId": "be-7"},
    "ledger/shared/20261005T120000Z-g.json": {"kind": "bot-signing-key", "stage": "shared", "keyId": 4711},
    "ledger/shared/20261005T120100Z-h.json": {"kind": "operator-seal", "stage": "shared", "manifestVersion": "mv"},
}
STATES = {
    "m11-harness/prod": [{"type": TD.RUNTIME_TYPE, "id": "rt", "outputs": {"agentRuntimeId": "m11_prod_companion-AAAA"}},
                         {"type": TD.ONLINE_EVAL_TYPE, "id": "oe", "outputs": {"onlineEvaluationConfigId": "m11_prod_oe-BBBB"}},
                         {"type": TD.LAMBDA_TYPE, "id": "m11-prod-adapter", "outputs": {"name": "m11-prod-adapter",
                                                                                         "arn": "arn:aws:lambda:x"}}],
    "m11-harness/staging": [], "m11-harness/dev": [], "m11-platform/shared": [{"type": "x", "id": "p"}],
    "m11-partners/shared": [],
}
PREVIEW = {"m11-harness/mr-7": [{"type": TD.RUNTIME_TYPE, "id": "x", "outputs": {"agentRuntimeId": "m11_mr7-CCCC"}}]}
GROUPS = ["/aws/bedrock-agentcore/runtimes/m11_prod_companion-AAAA-stable",
          "/aws/bedrock-agentcore/runtimes/m11_mr7-CCCC-preview",
          "/aws/bedrock-agentcore/runtimes/v2demo_agent-ZZZZ-DEFAULT",          # another project's runtime
          "/aws/bedrock-agentcore/runtimes/m11_old_spike-DDDD-DEFAULT",         # m11 prefix, not in state
          "/aws/bedrock-agentcore/evaluations/results/m11_prod_oe-BBBB",
          "/aws/lambda/m11-prod-adapter", "/aws/lambda/m11-stray"]


def plan(**kw):
    s3 = FakeS3(LEDGER)
    ledger = TD.read_ledger(s3, "bucket", page_size=2)
    return s3, ledger, TD.build_plan(ledger=ledger, states=STATES, preview_states=PREVIEW,
                                     tagged=["arn:aws:s3:::m11-release-x"],
                                     prefixed=[("runtime", "m11_prod_companion-AAAA"), ("runtime", "m11_old_spike-DDDD"),
                                               ("lambda", "m11-prod-adapter"), ("lambda", "m11-stray")],
                                     log_groups=GROUPS, **kw)


def test_the_ledger_is_read_with_one_paged_listing_of_ledger_prefix():
    s3, ledger, _ = plan()
    assert {l["Prefix"] for l in s3.listings} == {"ledger/"}
    assert len(s3.listings) == 4 and [l["token"] for l in s3.listings] == [None, "2", "4", "6"]
    assert len(ledger) == len(LEDGER) and s3.gets == len(LEDGER)
    assert {e["_key"].split("/")[1] for e in ledger} == {"prod", "staging", "dev", "shared"}


def test_the_plan_lists_ledgered_resources_in_their_steps():
    _, _, p = plan()
    step2 = {(i.kind, i.ident) for i in p.step(2)}
    assert ("A/B test", "ab-123") in step2 and ("latch parameter", LATCH) in step2
    assert ("recommendation", "rec-1") in step2 and ("batch evaluation", "be-7") in step2
    assert ("trace delivery", "dl-9") in step2
    assert [i.ident for i in p.step(2) if i.kind == "latch parameter"] == [LATCH]     # listed once
    assert [(i.kind, i.ident) for i in p.step(6) if i.kind == "bot signing key"] == [("bot signing key", "4711")]
    assert [i.ident for i in p.step(3)] == ["m11-harness/mr-7"]
    assert [i.ident for i in p.step(4) if i.kind == "stack"] == ["m11-harness/prod", "m11-harness/staging",
                                                                 "m11-harness/dev"]


def test_the_plan_is_complete_before_any_delete_so_step_6_survives_the_bucket():
    s3, _, p = plan()
    s3.delete_bucket()                                  # what step 5 does to the release bucket
    assert not s3.objects
    assert [i.ident for i in p.step(6) if i.kind == "bot signing key"] == ["4711"]


def test_log_groups_match_by_state_ids_and_orphans_are_kept():
    _, _, p = plan()
    groups = {i.ident for i in p.step(7)}
    assert groups == {"/aws/bedrock-agentcore/runtimes/m11_prod_companion-AAAA-stable",
                      "/aws/bedrock-agentcore/runtimes/m11_mr7-CCCC-preview",
                      "/aws/bedrock-agentcore/evaluations/results/m11_prod_oe-BBBB", "/aws/lambda/m11-prod-adapter"}
    orphans = {(i.kind, i.ident) for i in p.orphans}
    assert ("runtime", "m11_old_spike-DDDD") in orphans and ("lambda", "m11-stray") in orphans
    assert ("log group", "/aws/bedrock-agentcore/runtimes/m11_old_spike-DDDD-DEFAULT") in orphans
    assert all("v2demo" not in i.ident for i in p.items + p.orphans)       # never another project's resource
    assert ("runtime", "m11_prod_companion-AAAA") not in orphans


def test_module10_only_with_the_flag_and_the_render_prints_every_step():
    _, _, p = plan()
    assert not p.step(8) and any("Module 10 is kept" in n for n in p.notes)
    _, _, p10 = plan(include_module10=True)
    assert {i.ident for i in p10.step(8)} == set(TD.MODULE10_PROJECTS)
    lines = []
    TD.render(p, lambda text, style: lines.append(text))
    for n in range(1, 11):
        assert any(line.startswith(f"step {n}:") for line in lines)
    assert any(line.startswith("ORPHAN?") for line in lines)
    assert [i.ident for i in plan(delete_gitlab_project=True)[2].step(6) if i.kind == "GitLab project"] == ["delete"]


def test_captures_export_redacts_account_ids_names_and_hosts(tmp_path):
    from module11.demo import replay as RP
    src, dst = tmp_path / "local", tmp_path / "fixtures"
    RP.save({"capture": "b0", "section": 0, "recordedAt": "2026-10-10T10:00:00+00:00", "runIds": ["r"],
             "events": [{"t": 0, "sub": "0a", "kind": "out", "style": None,
                         "text": "arn:aws:iam::210987654321:user/alice at https://abc123.execute-api.us-east-1."
                                 "amazonaws.com/prod and https://gitlab.com/x"}], "responses": []}, src)
    out = RP.export(TD_ROOT, source=src, target=dst, names=["alice"])
    doc = json.loads(out[0].read_text(encoding="utf-8"))
    text = doc["events"][0]["text"]
    assert "210987654321" not in text and "alice" not in text and "execute-api" not in text
    assert "https://gitlab.com/x" in text and doc["redacted"] is True and not RP.leaks(doc, "210987654321")


TD_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]

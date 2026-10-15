# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Policy-corpus snapshots, `corpus publish`, the corpus event and its handler (4.6, 4.7, M15,
; 8.1 test_module11_corpus)."""
import io
import json
import shutil

import pytest

from module11.aws import objects
from module11.aws import gitlab_api as G
from module11.corpus import pin as P
from module11.corpus import publish as PUB
from module11.corpus import snapshot as S
from module11.lambdas.corpus_handler import handler as H
from test_module11_support import ROOT

BUCKET = "m11-release-111122223333-us-east-1"


def test_snapshot_is_deterministic_within_one_process():
    first, pin1 = S.build(ROOT)
    second, pin2 = S.build(ROOT)   # a fresh DedupIndex per build, so nothing is dropped as a duplicate
    assert first == second and pin1 == pin2
    assert pin1["chunkCount"] > 0 and json.loads(first)["rejected"] == []


def test_committed_pin_is_current():
    _, pin = S.build(ROOT)
    pinned = S.pinned(ROOT)
    assert {k: pinned[k] for k in ("snapshotId", "snapshotSha256", "documentCount")} == \
        {k: pin[k] for k in ("snapshotId", "snapshotSha256", "documentCount")}
    assert pinned["builtFrom"] == S.BUILT_FROM


def test_sources_are_module9_policy_seeds_then_runbooks():
    from module9.mock.seed_corpora_data import SEED_DOCS
    docs = S.sources(ROOT)
    seeds = [d for d in SEED_DOCS if d.get("corpus") == "policy"]
    runbooks = sorted(p.stem for p in (ROOT / S.RUNBOOKS).glob("*.md"))
    assert [d["docId"] for d in docs[len(seeds):]] == runbooks
    assert "runbook-checkout-502" in runbooks
    assert all(d["docId"].startswith("seed-") for d in docs[:len(seeds)])
    snapshot = json.loads(S.build(ROOT)[0])
    assert [d["docId"] for d in snapshot["documents"]] == [d["docId"] for d in docs]
    assert snapshot["snapshotId"].startswith("pol-") and snapshot["schema"] == S.SCHEMA


def test_snapshot_bytes_change_with_a_runbook(tmp_path):
    root = tmp_path / "repo"
    (root / S.RUNBOOKS).mkdir(parents=True)
    for path in (ROOT / S.RUNBOOKS).glob("*.md"):
        shutil.copy(path, root / S.RUNBOOKS / path.name)
    (root / S.RETRIEVAL).parent.mkdir(parents=True)
    shutil.copy(ROOT / S.RETRIEVAL, root / S.RETRIEVAL)
    _, before = S.build(root)
    assert S.update_pin(root) is False   # the copy pins the same snapshot
    extra = root / S.RUNBOOKS / "runbook-zz-extra.md"
    extra.write_text("# Extra runbook\nRestart the cache cluster only after the on-call engineer confirms the "
                     "replica lag is below one second and the write queue has drained completely.\n")
    _, after = S.build(root)
    assert after["snapshotId"] != before["snapshotId"] and after["documentCount"] == before["documentCount"] + 1
    assert S.update_pin(root) is True and S.pinned(root)["snapshotId"] == after["snapshotId"]
    assert S.update_pin(root) is False


def test_write_produces_the_seal_layout_with_hash_metadata(tmp_path):
    path, pin = S.write(ROOT, tmp_path)
    assert path == tmp_path / S.object_key(pin["snapshotId"])
    assert S.object_key(pin["snapshotId"]) == f"snapshots/policy/{pin['snapshotId']}.json"
    assert objects.head(path.as_uri())[objects.SHA_KEY] == pin["snapshotSha256"]
    assert objects.verified_get(path.as_uri(), pin["snapshotSha256"]) == S.build(ROOT)[0]


def test_search_runbooks_finds_the_checkout_runbook_for_incident_triage_014(tmp_path):
    from module11.runtime.tools import LocalInvoker
    path, pin = S.write(ROOT, tmp_path)
    scenario = json.loads((ROOT / "module11/harness/evaluation/scenarios/regressions.jsonl").read_text().splitlines()[0])
    assert scenario["scenario_id"] == "incident-triage-014"
    event = {"arguments": {"query": f"{scenario['service']} {scenario['turns'][0]['input']}"},
             "context": {"data": {"snapshot": {"uri": path.as_uri(), "sha256": pin["snapshotSha256"]}}}}
    out = LocalInvoker().invoke("local:search_runbooks", event, 10)
    assert out["ok"] is True
    assert out["result"]["results"][0]["docId"] == "runbook-checkout-502"
    assert out["result"]["snapshotId"] == pin["snapshotId"] and len(out["result"]["results"]) <= 3


# ---------------------------------------------------------------- corpus publish
class FakeS3:
    def __init__(self):
        self.objects = {}

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            err = Exception("404")
            err.response = {"Error": {"Code": "404"}}
            raise err
        return {"Metadata": self.objects[(Bucket, Key)][1]}

    def put_object(self, Bucket, Key, Body, ContentType=None, Metadata=None):
        self.objects[(Bucket, Key)] = (Body, Metadata or {})

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            err = Exception("NoSuchKey")
            err.response = {"Error": {"Code": "NoSuchKey"}}
            raise err
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)][0])}


class FakeEvents:
    def __init__(self):
        self.entries = []

    def put_events(self, Entries):
        self.entries += Entries
        return {"FailedEntryCount": 0, "Entries": [{"EventId": "ev-1"}]}


def _published(monkeypatch=None, demo=None):
    s3, events = FakeS3(), FakeEvents()
    out = PUB.publish(ROOT, corpus="policy", change="runbook-checkout-v2", bucket=BUCKET, s3=s3, events=events,
                      log=lambda _: None, demo_run=demo)
    return out, s3, events


def test_publish_builds_an_immutable_snapshot_with_the_change_and_emits_the_event():
    out, s3, events = _published(demo="run-7")
    pin, detail = out["pin"], out["detail"]
    assert pin["snapshotId"] != S.pinned(ROOT)["snapshotId"]            # the update changes the snapshot
    body, meta = s3.objects[(BUCKET, P.snapshot_key("policy", pin["snapshotId"]))]
    assert meta == {"m11-sha256": pin["snapshotSha256"]} and json.loads(body)["snapshotId"] == pin["snapshotId"]
    sources = json.loads(s3.objects[(BUCKET, P.sources_key("policy", pin["snapshotId"]))][0])
    assert list(sources) == ["module11/corpus/runbooks/runbook-checkout-502.md"]
    assert "checkout-pool-tuning" in sources["module11/corpus/runbooks/runbook-checkout-502.md"]
    assert detail["documentCounts"] == {"added": 0, "updated": 1, "deleted": 0} and detail["demoRun"] == "run-7"
    entry = events.entries[0]
    assert (entry["Source"], entry["DetailType"], entry["EventBusName"]) == (P.SOURCE, P.DETAIL_TYPE, "m11-releases")
    # Same sources, same bytes: a second publish writes nothing new.
    again, _, _ = (PUB.publish(ROOT, corpus="policy", change="runbook-checkout-v2", bucket=BUCKET, s3=s3,
                               events=FakeEvents(), log=lambda _: None), None, None)
    assert again["pin"] == pin and again["written"] is False


def test_the_changed_tree_builds_the_published_bytes(tmp_path):
    """An MR carrying the event's sources builds the same snapshot at Seal."""
    out, s3, _ = _published()
    root = tmp_path / "repo"
    (root / S.RUNBOOKS).mkdir(parents=True)
    for path in (ROOT / S.RUNBOOKS).glob("*.md"):
        shutil.copy(path, root / S.RUNBOOKS / path.name)
    for path, text in json.loads(s3.objects[(BUCKET, P.sources_key("policy", out["pin"]["snapshotId"]))][0]).items():
        (root / path).write_text(text, encoding="utf-8")
    assert S.build(root)[1] == out["pin"]


def test_publish_refuses_unknown_changes():
    with pytest.raises(PUB.PublishError):
        PUB.apply_change(ROOT, "../etc/passwd")
    with pytest.raises(PUB.PublishError):
        PUB.apply_change(ROOT, "no-such-update")


def _event(**over):
    out, _, _ = _published()
    detail = {**out["detail"], **over}
    return {"source": P.SOURCE, "detail-type": P.DETAIL_TYPE, "detail": detail}


@pytest.mark.parametrize("field,value", [("snapshotId", "pol-XYZ"), ("snapshotUri", "s3://other/snapshots/policy/x.json"),
                                         ("snapshotSha256", "md5:1"), ("corpus", "glossary"), ("demoRun", "a b"),
                                         ("documentCounts", {"added": -1, "updated": 0, "deleted": 0}),
                                         ("sources", ["/etc/passwd"]), ("ingestionJobId", "x" * 200)])
def test_event_validation_rejects(field, value):
    with pytest.raises(P.InvalidCorpusEvent):
        P.validate_event(_event(**{field: value}), BUCKET)


def test_event_validation_accepts_demo_run_and_names_the_branch():
    detail = P.validate_event(_event(demoRun="dry-run-1"), BUCKET)
    assert detail["demoRun"] == "dry-run-1"
    assert P.branch_for("policy", detail["snapshotId"]) == f"corpus/policy/{detail['snapshotId']}"
    assert P.branch_for("policy", detail["snapshotId"]) == P.branch_for("policy", detail["snapshotId"])


def test_pin_text_edits_only_the_corpus_entry_and_keeps_comments():
    text = (ROOT / P.RETRIEVAL).read_text()
    new = P.pin_text(text, "policy", {"snapshotId": "pol-0123456789ab", "snapshotSha256": "sha256:" + "a" * 64,
                                      "documentCount": 7})
    assert '"pol-0123456789ab"' in new and "documentCount: 7" in new and new.count("#") == text.count("#")
    assert "topK: 3" in new and "builtFrom: module9.chunk+quality" in new


class FakeSQS:
    def __init__(self):
        self.sent = []

    def send_message(self, QueueUrl, MessageBody):
        self.sent.append(json.loads(MessageBody))


class FakeGitLab:
    def __init__(self, open_mrs=(), fail=None, branch_exists=False):
        self.open_mrs, self.fail, self.branch_exists, self.calls = list(open_mrs), fail, branch_exists, []

    def merge_requests(self, source_branch=None, state="opened"):
        return self.open_mrs

    def file(self, path, ref):
        if path == P.RETRIEVAL:
            return (ROOT / P.RETRIEVAL).read_bytes()
        return b"old" if path.startswith("module11/corpus/runbooks/") else None

    def branch(self, name):
        return {"name": name} if self.branch_exists else None

    def delete_branch(self, name):
        self.calls.append(("delete_branch", name))

    def commit_actions(self, branch, message, actions, start_branch=None):
        if self.fail:
            raise G.GitLabError("POST", "/commits", self.fail, "server error")
        self.calls.append(("commit", branch, sorted(a["file_path"] for a in actions)))
        return {"id": "e" * 40}

    def create_merge_request(self, source, title, description, labels, target="main"):
        self.calls.append(("mr", source, title, list(labels)))
        return {"iid": 31, "web_url": "https://gitlab.example/mr/31"}


def _handle(event, s3, gl, monkeypatch):
    monkeypatch.setenv("DLQ_URL", "https://sqs.example/m11-corpus-dlq")
    sqs = FakeSQS()
    return H.handle(event, s3=s3, sqs=sqs, gitlab=lambda: gl, bucket=BUCKET), sqs


def test_handler_opens_the_labelled_mr_with_the_pin_and_the_sources(monkeypatch):
    out, s3, _ = _published(demo="run-9")
    event = {"source": P.SOURCE, "detail-type": P.DETAIL_TYPE, "detail": out["detail"]}
    gl = FakeGitLab()
    result, sqs = _handle(event, s3, gl, monkeypatch)
    sid = out["pin"]["snapshotId"]
    assert result["ok"] and result["iid"] == 31 and not result["reused"] and sqs.sent == []
    assert gl.calls[0] == ("commit", f"corpus/policy/{sid}", [
        "module11/corpus/runbooks/runbook-checkout-502.md", P.RETRIEVAL])
    assert gl.calls[1] == ("mr", f"corpus/policy/{sid}", f"corpus: policy snapshot {sid}",
                           ["change::retrieval-corpus", "m11-demo-run"])


def test_handler_reuses_the_open_mr_for_a_duplicate_event(monkeypatch):
    out, s3, _ = _published()
    event = {"source": P.SOURCE, "detail-type": P.DETAIL_TYPE, "detail": out["detail"]}
    gl = FakeGitLab(open_mrs=[{"iid": 12, "web_url": "u", "sha": "f" * 40}])
    result, _ = _handle(event, s3, gl, monkeypatch)
    assert result["reused"] and result["iid"] == 12 and gl.calls == []
    gl = FakeGitLab(branch_exists=True)       # a leftover branch with no open MR is deleted and recreated
    _handle(event, s3, gl, monkeypatch)
    assert gl.calls[0][0] == "delete_branch" and gl.calls[-1][0] == "mr"


def test_handler_sends_invalid_events_and_hash_mismatches_to_the_dlq_without_raising(monkeypatch):
    out, s3, _ = _published()
    bad = {"source": P.SOURCE, "detail-type": P.DETAIL_TYPE, "detail": {**out["detail"], "snapshotId": "nope"}}
    result, sqs = _handle(bad, s3, FakeGitLab(), monkeypatch)
    assert result == {"ok": False, "fatal": True, "reason": "snapshotId is invalid"}
    assert sqs.sent[0]["reason"].startswith("invalid event") and sqs.sent[0]["event"] == bad
    key = (BUCKET, P.snapshot_key("policy", out["pin"]["snapshotId"]))
    s3.objects[key] = (s3.objects[key][0], {"m11-sha256": "sha256:" + "0" * 64})
    event = {"source": P.SOURCE, "detail-type": P.DETAIL_TYPE, "detail": out["detail"]}
    result, sqs = _handle(event, s3, FakeGitLab(), monkeypatch)
    assert result["fatal"] and "hash mismatch" in sqs.sent[0]["reason"]


def test_handler_raises_on_gitlab_5xx_and_a_missing_snapshot_so_lambda_retries(monkeypatch):
    out, s3, _ = _published()
    event = {"source": P.SOURCE, "detail-type": P.DETAIL_TYPE, "detail": out["detail"]}
    with pytest.raises(G.GitLabError):
        _handle(event, s3, FakeGitLab(fail=503), monkeypatch)
    missing = {**out["detail"], "snapshotId": "pol-000000000000",
               "snapshotUri": f"s3://{BUCKET}/{P.snapshot_key('policy', 'pol-000000000000')}"}
    with pytest.raises(Exception) as exc:
        _handle({"source": P.SOURCE, "detail-type": P.DETAIL_TYPE, "detail": missing}, s3, FakeGitLab(), monkeypatch)
    assert not isinstance(exc.value, H.Fatal)


def test_handler_packaging_is_flat_and_standard_library_only():
    """The platform packages handler.py, gitlab_api.py, bot_changes.py and pin.py side by side."""
    import ast
    for rel in ("module11/lambdas/corpus_handler/handler.py", "module11/aws/gitlab_api.py",
                "module11/aws/bot_changes.py", "module11/corpus/pin.py"):
        tree = ast.parse((ROOT / rel).read_text())
        top = {n.names[0].name.split(".")[0] if isinstance(n, ast.Import) else (n.module or "").split(".")[0]
               for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) and getattr(n, "level", 0) == 0}
        assert top <= {"__future__", "json", "os", "re", "time", "typing", "urllib", "dataclasses", "base64",
                       "module11", "bot_changes", "gitlab_api", "pin", "boto3", "hashlib"}, (rel, top)

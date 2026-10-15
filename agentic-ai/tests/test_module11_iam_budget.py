# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""IAM role headroom (release/iam_budget.py, aws/quotas.py, live_check item 1b, the deploy's pre-bootstrap check).
The account's "Roles per account" quota (L-FE177D64) was hit live: an mr-* stack failed halfway
through its first `pulumi up` with LimitExceeded."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from module11.release import iam_budget as IB

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/module11/harness_min"


def test_role_counts_per_stack():
    """12 roles per harness stack plus the operations roles (controller, scheduler and drift checker everywhere, the
    shadow worker on dev and prod) and the experiment roles (online evaluation everywhere, staging's three Gateway
    roles): 17/19/17; 13 per mr-* stack (its own eval runner, no operations); 26 in the platform (8 before the
    pipeline)."""
    for stage, count in (("dev", 17), ("staging", 19), ("prod", 17)):
        names = IB.stack_roles(ROOT, stage)
        assert len(names) == count and len(set(names)) == count
        assert f"m11-{stage}-runtime" in names and f"m11-{stage}-eval-runner" not in names
        assert {f"m11-{stage}-controller", f"m11-{stage}-scheduler", f"m11-{stage}-drift-check",
                f"m11-{stage}-online-eval"} <= set(names)
    assert "m11-staging-shadow-worker" not in IB.stack_roles(ROOT, "staging")
    mr = IB.stack_roles(ROOT, "mr-12")
    assert len(mr) == 13 and "m11-mr-12-eval-runner" in mr and "m11-mr-12-controller" not in mr
    assert len(IB.platform_roles(pipeline=False)) == 8 and "m11-preview-deployer" in IB.platform_roles()
    assert len(IB.platform_roles()) == 26 and "m11-seal-experiment" in IB.platform_roles()
    assert "m11-corpus-handler" in IB.platform_roles()
    assert {"m11-staging-ab-gateway", "m11-staging-abtest-driver", "m11-staging-abtest"} <= set(IB.stack_roles(ROOT, "staging"))
    planned = IB.plan(ROOT)
    assert sum(len(v) for v in planned.values()) == 26 + 17 + 19 + 17 + IB.PREVIEW_STACKS * 13


@pytest.mark.parametrize("stage", ["mr-7", "dev"])
def test_stack_roles_match_the_harness_program(stage):
    """The budget is the program's own role set under Pulumi mocks, so a new role cannot go uncounted."""
    m = pytest.importorskip("test_module11_pulumi_mocks")
    _, mocks = m.execute(stage)
    created = sorted(i["name"] for _, i in mocks.of("Role"))
    assert created == IB.stack_roles(FIXTURE, stage)


def test_platform_role_call_sites_are_counted():
    """m11-platform declares roles at seven call sites: in __main__.py the CodeBuild roles (image build and the ten
    project roles), m11-operator-experiment, m11-pipeline, `_deployer` x4 and the eval runners x3; in oidc.py the
    three GitLab roles; in events.py the two Lambdas. A new `aws.iam.Role(` must be added to `platform_roles()`,
    and this count updated with it."""
    platform = ROOT / "module11/infrastructure/platform"
    counts = {name: len(re.findall(r"aws\.iam\.Role\(", (platform / name).read_text(encoding="utf-8")))
              for name in ("__main__.py", "oidc.py", "events.py", "builds.py", "pipeline.py")}
    assert counts == {"__main__.py": 5, "oidc.py": 1, "events.py": 1, "builds.py": 0, "pipeline.py": 0}
    source = (platform / "__main__.py").read_text(encoding="utf-8")
    assert '"m11-image-build"' in source and '"m11-preview-deployer"' in source and '"m11-operator-experiment"' in source


def _room(used, quota, existing=(), planned=None, pending=None):
    return IB.headroom(planned or IB.plan(ROOT), used=used, quota=quota, existing=existing, pending_request=pending)


def test_headroom_fails_with_the_quota_code_when_full():
    room = _room(1000, 1000)
    assert not room.ok and room.needed == 105 and room.free == 0
    text = room.message()
    assert IB.QUOTA_CODE in text and "--desired-value 1115" in text and "--service-code iam" in text


def test_existing_roles_need_no_new_slot():
    planned = IB.plan(ROOT, preview_stacks=0)
    present = [n for names in planned.values() for n in names]
    room = _room(1000, 1000, existing=present, planned=planned)
    assert room.ok and room.needed == 0
    partial = {"mr-0": IB.stack_roles(ROOT, "mr-0")}
    room = _room(1000, 1000, existing=partial["mr-0"][:-1], planned=partial)
    assert not room.ok and room.needed == 1 and room.breakdown == {"mr-0": 1}


def test_live_preview_stacks_count_against_the_planned_ones():
    planned = IB.plan(ROOT, preview_stacks=2)
    mr_roles = IB.stack_roles(ROOT, "mr-0")
    named = [n for k, names in planned.items() if not k.startswith("preview-") for n in names]
    room = _room(990, 1000, existing=[*named, *mr_roles], planned=planned)
    assert room.needed == 13 and not room.ok
    room = _room(900, 1000, existing=[*named, *mr_roles], planned=planned)
    assert room.ok and not room.warning


def test_low_margin_is_a_warning_and_pending_request_is_named():
    room = _room(890, 1000, pending="PENDING 1500")
    assert room.ok and room.warning and room.free - room.needed == 5
    assert "[increase request PENDING 1500]" in room.message()


class _IAM:
    def __init__(self, used, quota, names):
        self.used, self.quota, self.names = used, quota, names

    def get_account_summary(self):
        return {"SummaryMap": {"Roles": self.used, "RolesQuota": self.quota}}

    def get_paginator(self, name):
        assert name == "list_roles"
        names = self.names

        class _P:
            def paginate(self):
                yield {"Roles": [{"RoleName": n} for n in names[:2]]}
                yield {"Roles": [{"RoleName": n} for n in names[2:]]}
        return _P()


class _Quotas:
    def __init__(self, history=None, error=False):
        self.history, self.error = history or [], error

    def list_requested_service_quota_change_history_by_quota(self, **kw):
        assert kw == {"ServiceCode": "iam", "QuotaCode": IB.QUOTA_CODE}
        if self.error:
            raise RuntimeError("AccessDenied")
        return {"RequestedQuotas": self.history}


class _Session:
    def __init__(self, iam, quotas):
        self.iam, self.quotas = iam, quotas

    def client(self, name, region_name=None):
        if name == "service-quotas":
            assert region_name == "us-east-1"
            return self.quotas
        assert name == "iam"
        return self.iam


def test_quotas_adapter_reads_summary_names_and_pending_requests():
    from module11.aws import quotas
    iam = _IAM(998, 1000, ["m11-mr-0-runtime", "other-role", "m11-dev-runtime", "m11-dev-adapter"])
    assert quotas.role_usage(iam) == (998, 1000)
    assert quotas.role_names(iam) == {"m11-mr-0-runtime", "m11-dev-runtime", "m11-dev-adapter"}
    history = [{"Status": "APPROVED", "DesiredValue": 1200.0, "Created": 1},
               {"Status": "PENDING", "DesiredValue": 1500.0, "Created": 2}]
    assert quotas.pending_increase(_Quotas(history)) == "PENDING 1500"
    assert quotas.pending_increase(_Quotas(error=True)) is None
    room = quotas.headroom(_Session(iam, _Quotas()), {"mr-0": IB.stack_roles(ROOT, "mr-0")})
    assert room.needed == 12 and not room.ok


def test_deploy_stops_before_bootstrap_without_room():
    from module11.aws import deploy as AD
    lines = []

    class _Ctx:
        root = ROOT
        admin = _Session(_IAM(1000, 1000, IB.stack_roles(ROOT, "mr-0")[:-1]), _Quotas())

    fake = type("D", (), {"ctx": _Ctx, "stage": "mr-0", "log": staticmethod(lines.append)})()
    with pytest.raises(AD.DeployError, match=IB.QUOTA_CODE):
        AD.Deploy.check_role_headroom(fake)
    _Ctx.admin = _Session(_IAM(900, 1000, []), _Quotas())
    AD.Deploy.check_role_headroom(fake)
    assert "13 not yet created" in lines[-1]

    class _Broken:
        def client(self, *a, **k):
            raise RuntimeError("no credentials")
    _Ctx.admin = _Broken()
    AD.Deploy.check_role_headroom(fake)
    assert lines[-1] == "IAM role headroom not checked (RuntimeError)"


def test_live_check_item_1b():
    from module11.demo import live_check as LC
    full = LC.check_role_headroom(_Session(_IAM(1000, 1000, []), _Quotas()), root=ROOT)
    assert [(r.name, r.ok, r.warning) for r in full] == [("1b IAM role headroom", False, False)]
    assert IB.QUOTA_CODE in full[0].detail
    roomy = LC.check_role_headroom(_Session(_IAM(500, 1500, []), _Quotas()), root=ROOT)
    assert roomy[0].ok and not roomy[0].warning
